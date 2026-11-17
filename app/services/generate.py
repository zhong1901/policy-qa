"""生成层：把检索到的条款拼成 prompt，调大模型输出结构化答案。

职责边界：
- 只负责「拼资料 → 调模型 → 解析 JSON → 归一化引用」
- 不判断该不该答（那是 ask.py 的事）
- 不校验引用真伪（那是 verify.py 的事）

关键设计：
1. 引用编号 [C1] 由本模块分配，与 hits 顺序严格一一对应；
   生成后由 verify.py 反查，编号错位就是编造。
2. 强制 json_object 模式 + 推理模型要给足 max_tokens，否则 content 为空。
3. 模型可能包 ```json 围栏或多给文本，解析要容错。
"""
import json
import logging
import re

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.config import Settings
from app.services.embed import get_http_client
from app.services.retrieve import ClauseHit

logger = logging.getLogger(__name__)


class LLMNotConfigured(RuntimeError):
    """没有配置 llm key —— 部署配置问题，路由层转 503。"""


class GenerationFailed(RuntimeError):
    """模型不可用或返回无法解析。"""


SYSTEM_PROMPT = """你是上海人才与就业政策的解读助手。你只能依据【参考资料】作答，不得使用任何外部知识或常识推断。

规则：
1. 每个结论句后必须标注引用编号，形如 [C1]、[C2]，编号必须来自【参考资料】。
2. 参考资料中没有的信息，一律不要写。整题都无法回答时，把 refused 设为 true，
   conclusion 写"未找到明确的政策依据"，citations 留空数组。
3. 金额、年限、日期、比例必须与原文一致，不得改写、换算或四舍五入。
4. 如果多份资料对同一事项表述不一致，写进 conflicts 数组，不要自行取舍。
5. 只输出 JSON，不要输出任何解释文字，不要用 markdown 代码块包裹。
6. 不要出现「参考资料」「资料中」「根据提供的内容」这类内部用语。
   直接陈述政策内容；确实要说明局限时，用「现行公开政策中未见…」这样的说法。
7. materials 只放政策要求的办理材料（如身份证明、营业执照）。问题不涉及材料时，
   materials 必须是空数组 []，不要把文档名称、条款出处写进 materials。

输出 JSON 结构：
{
  "refused": false,
  "conclusion": "一句话结论",
  "conditions": ["适用条件"],
  "materials": [{"name": "材料名称", "note": "补充说明，没有就空字符串"}],
  "process": ["办理步骤"],
  "deadline": "截止时间，没有就 null",
  "citations": [{"ref": "C1", "quote": "逐字摘录的原文片段"}],
  "confidence": "high|medium|low",
  "conflicts": ["资料之间表述不一致之处"]
}"""

EMPTY_ANSWER = {
    "refused": True,
    "conclusion": "未找到明确的政策依据",
    "conditions": [],
    "materials": [],
    "process": [],
    "deadline": None,
    "citations": [],
    "confidence": "low",
    "conflicts": [],
}


def build_context(hits: list[ClauseHit]) -> list[str]:
    """把条款拼成【参考资料】。编号 C1..Cn 与 hits 下标一一对应。"""
    blocks = []
    for i, h in enumerate(hits, 1):
        head = f"[C{i}] 《{h.doc_title}》"
        if h.clause_no:
            head += f" {h.clause_no}"
        lines = [head]
        if h.effective_from:
            lines.append(f"生效日期：{h.effective_from.isoformat()}")
        lines.append(f"正文：{h.content.strip()}")
        blocks.append("\n".join(lines))
    return blocks


def build_messages(question: str, hits: list[ClauseHit]) -> list[dict]:
    context = "\n\n".join(build_context(hits))
    user = f"""【参考资料】
{context}

【用户问题】
{question}

请严格按规则输出 JSON。"""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def parse_json(raw: str) -> dict:
    """容错解析：去掉 ```json 围栏、截取第一个 { 到最后一个 }。"""
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            raise GenerationFailed(f"返回内容不含 JSON：{text[:120]}")
        text = text[start:end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise GenerationFailed(f"JSON 解析失败：{exc}；原文前 200 字：{text[:200]}")


def normalize(data: dict, hits: list[ClauseHit]) -> dict:
    """补齐字段 + 把 citations 的 ref 映射回真实条款。"""
    out = dict(EMPTY_ANSWER)
    for key in EMPTY_ANSWER:
        if key in data:
            out[key] = data[key]

    out["refused"] = bool(out.get("refused"))
    for key in ("conditions", "process", "conflicts"):
        if not isinstance(out.get(key), list):
            out[key] = []

    mapped = []
    for c in out.get("citations") or []:
        if not isinstance(c, dict):
            continue
        m = re.match(r"^C?(\d+)$", str(c.get("ref", "")).strip(), re.I)
        if not m:
            continue
        idx = int(m.group(1)) - 1
        if not (0 <= idx < len(hits)):
            continue  # 编造的编号，交给 verify 报错；这里先丢掉
        h = hits[idx]
        mapped.append({
            "ref": f"C{idx + 1}",
            "quote": str(c.get("quote", "")).strip(),
            "clause_id": h.clause_id,
            "doc_title": h.doc_title,
            "clause_no": h.clause_no,
            "effective_from": h.effective_from.isoformat() if h.effective_from else None,
            "url": h.url,
        })
    # 去重：模型常把同一条条款拆成多段分别引用，界面上会出现 3 个 C1，
    # 看起来像 bug。按 clause_id 去重，保留第一次出现的 ref（正文里的 [Cn] 标记不变）。
    seen_ids: set[int] = set()
    unique: list[dict] = []
    for c in mapped:
        if c["clause_id"] in seen_ids:
            continue
        seen_ids.add(c["clause_id"])
        unique.append(c)
    out["citations"] = unique

    if out["refused"]:
        out["citations"] = []
        out["confidence"] = "low"
    return out


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return isinstance(exc, httpx.TransportError)


@retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=12),
    reraise=True,
)
async def _call_llm(messages: list[dict], settings: Settings) -> str:
    url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": settings.llm_model,
        "messages": messages,
        "response_format": {"type": "json_object"},
        # 推理模型的 reasoning_tokens 会先吃掉 max_tokens，给少了 content 会是空字符串
        "max_tokens": settings.llm_max_tokens,
        "temperature": 0.1,
    }
    resp = await get_http_client().post(
        url,
        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        json=payload,
        timeout=settings.llm_timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise GenerationFailed(f"模型返回没有 choices：{str(data)[:200]}")

    choice = choices[0]
    finish = choice.get("finish_reason")
    content = choice["message"].get("content") or ""
    reasoning = choice["message"].get("reasoning_content") or ""

    # finish_reason=length 表示被 max_tokens 截断：推理模型把额度耗在思考上，
    # 正文只写了一半，JSON 必然不完整。必须单独报出来，不能笼统说"不含 JSON"。
    if finish == "length":
        raise GenerationFailed(
            f"输出被 max_tokens={settings.llm_max_tokens} 截断"
            f"（reasoning 已用 {len(reasoning)} 字），请调大 LLM_MAX_TOKENS"
        )
    if not content.strip():
        raise GenerationFailed(
            f"模型返回空内容 finish_reason={finish}，reasoning {len(reasoning)} 字"
        )
    return content


async def generate(question: str, hits: list[ClauseHit], settings: Settings) -> dict:
    """生成结构化答案。失败抛 GenerationFailed，由路由层转 503。"""
    if not hits:
        return dict(EMPTY_ANSWER)

    if not settings.llm_api_key:
        raise LLMNotConfigured(
            "未配置 LLM_API_KEY，无法生成答案；请在 .env 中填入后重启服务"
        )

    messages = build_messages(question, hits)
    try:
        raw = await _call_llm(messages, settings)
    except httpx.HTTPStatusError as exc:
        raise GenerationFailed(
            f"模型接口返回 {exc.response.status_code}: {exc.response.text[:200]}"
        ) from exc
    except httpx.HTTPError as exc:
        raise GenerationFailed(f"模型接口不可达：{exc}") from exc

    data = parse_json(raw)
    answer = normalize(data, hits)
    logger.info(
        "生成完成 refused=%s citations=%d", answer["refused"], len(answer["citations"])
    )
    return answer


async def regenerate_with_feedback(
    question: str, hits: list[ClauseHit], settings: Settings, problems: list[str]
) -> dict:
    """校验失败后带着问题重试一次。

    把 verify 发现的问题回灌给模型，比单纯重试有效得多。
    """
    messages = build_messages(question, hits)
    messages.append({
        "role": "user",
        "content": (
            "你上一次的输出有以下问题，请严格修正后重新输出完整 JSON：\n- "
            + "\n- ".join(problems)
        ),
    })
    raw = await _call_llm(messages, settings)
    return normalize(parse_json(raw), hits)
