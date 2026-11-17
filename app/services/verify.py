"""校验层：判断模型给的引用是不是真的、是不是真的支撑结论。

这是防幻觉的最后一道闸。它不判断"政策对不对"，只判断三件事：
1. 引用编号存在于本次召回吗（防编造 C7）
2. 引文是原文的逐字片段吗（防模型改写条文）
3. 有结论却没有引用吗（防裸答）

三项任一不过，就交给上层重试或降级拒答。
"""
import difflib
import logging
import re

from app.config import Settings
from app.services.retrieve import ClauseHit

logger = logging.getLogger(__name__)

_REF_RE = re.compile(r"\[C(\d+)\]")
_WS_RE = re.compile(r"\s+")


def _norm(t: str) -> str:
    """压缩空白，避免换行/全角空格导致误判。"""
    return _WS_RE.sub("", t or "")


def containment_ratio(quote: str, source: str) -> float:
    """quote 有多大比例能在 source 里逐字找到。

    不用 SequenceMatcher.ratio()：它是整体相似度，短串 vs 长串会算出极低分，
    哪怕 quote 完全就是原文的一段。这里用「最长公共子串长度 / quote 长度」。
    """
    q, s = _norm(quote), _norm(source)
    if not q:
        return 0.0
    if q in s:
        return 1.0
    sm = difflib.SequenceMatcher(None, q, s, autojunk=False)
    return sm.find_longest_match(0, len(q), 0, len(s)).size / len(q)


def _answer_text(answer: dict) -> str:
    """收集所有可能带 [Cn] 标记的正文，用于反查编号。"""
    parts = [str(answer.get("conclusion") or "")]
    for key in ("conditions", "process", "conflicts"):
        parts.extend(str(x) for x in (answer.get(key) or []))
    for m in answer.get("materials") or []:
        if isinstance(m, dict):
            parts.append(str(m.get("name") or ""))
            parts.append(str(m.get("note") or ""))
    return "\n".join(parts)


def verify(
    answer: dict, hits: list[ClauseHit], settings: Settings
) -> tuple[bool, list[str]]:
    """返回 (是否通过, 问题列表)。拒答的答案一律通过。"""
    if answer.get("refused"):
        return True, []

    problems: list[str] = []
    by_id = {h.clause_id: h for h in hits}

    # ---- 1. 正文里的 [Cn] 必须落在召回范围内 ----
    used: set[int] = set()
    for m in _REF_RE.finditer(_answer_text(answer)):
        i = int(m.group(1))
        if not (1 <= i <= len(hits)):
            problems.append(
                f"正文引用了 [C{i}]，但本次只召回 C1..C{len(hits)}，属于编造编号"
            )
        else:
            used.add(i)

    citations = answer.get("citations") or []

    # ---- 2. 有结论却没给引用 ----
    conclusion = (answer.get("conclusion") or "").strip()
    if conclusion and not citations and not problems:
        problems.append("给出了结论但没有提供任何 citations")

    # ---- 2b. 给了 citations，但结论里没打角标 ----
    # 这一条是产品底线：结论必须句句可溯源，只在数组里给引用、正文不标，
    # 用户点不了、对不上，等于没有溯源。
    if citations and conclusion and not _REF_RE.search(conclusion):
        problems.append("结论句没有标注引用角标 [Cn]，用户无法对应到具体条款")

    # ---- 3. 引文必须是原文的逐字片段 ----
    for c in citations:
        ref = c.get("ref") or "?"
        h = by_id.get(c.get("clause_id"))
        if h is None:
            problems.append(f"{ref} 的 clause_id 不在本次召回集里")
            continue
        quote = (c.get("quote") or "").strip()
        if len(quote) < 8:
            problems.append(f"{ref} 的 quote 过短（{len(quote)} 字），无法核对")
            continue
        ratio = containment_ratio(quote, h.content)
        if ratio < settings.citation_min_sim:
            problems.append(
                f"{ref} 的引文只有 {ratio:.0%} 能在原文中找到，疑似改写条文"
                f"（原文 clause_id={h.clause_id}）"
            )

    # ---- 4. 引用了某条却没在正文里出现对应标记（次要提示，不判失败） ----
    missing_mark = [c.get("ref") for c in citations
                    if c.get("ref") and int(str(c["ref"]).lstrip("C") or 0) not in used]
    if missing_mark and not problems:
        logger.info("以下引用未在正文中出现标记：%s", missing_mark)

    ok = not problems
    if not ok:
        logger.warning("引用校验未通过：%s", "；".join(problems))
    return ok, problems
