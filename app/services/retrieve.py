"""混合检索：向量召回 + 关键词召回 + RRF 融合 + 时间过滤。

性能上的关键取舍：
数据库部署在美国东部，从国内访问单次往返约 500 ms。实测「查询本身只要 1.5 ms，
但客户端要等 2.6 s」——即耗时几乎全在数据传输上。
所以召回阶段**只回 id 和分数**（几十个整数），
融合后再单独取最终那几条的正文。实测检索耗时从 15 s 降到 1–2 s。
"""
import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import date

import asyncpg

from app.config import Settings
from app.services.embed import embed_query

logger = logging.getLogger(__name__)

# 停用词：这些 bigram 会召回海量无关条款（"什么""可以"几乎出现在每条通知里），必须丢。
_STOP_TERMS = {
    "什么", "怎么", "可以", "需要", "我是", "请问", "多少", "是否", "哪些",
    "如何", "应该", "能否", "是不", "不是", "有没", "没有", "这个", "那个",
    "我们", "你们", "他们", "的话", "如果", "就是", "还是", "或者", "以及",
    "一个", "一下", "现在", "目前", "具体", "相关",
}

_TERM_LIMIT = 30

# 关键词路多取几倍再做 RRF。因为回传的只是 id，多取不心疼。
_KEYWORD_OVERFETCH = 3


@dataclass
class ClauseHit:
    """检索命中的一条条款，供后续融合、生成、引用回查复用。"""

    clause_id: int
    doc_key: str
    doc_title: str
    clause_no: str | None
    clause_path: str | None
    content: str
    effective_from: date | None
    url: str | None
    score: float = 0.0               # RRF 融合分，仅用于排序
    vector_sim: float | None = None  # 向量余弦相似度（拒答阈值用，只有向量路有值）


def _extract_terms(question: str) -> list[str]:
    """从问题里抽关键词，给 ILIKE 召回用。

    中文没装分词器（tsv 是 simple 配置，对中文切分基本失效），所以靠规则抽。
    不能按标点切完就整段拿去 ILIKE：「租房补贴需要什么材料」整段做 pattern
    在 7690 条条款里命中数约为 0，必须切成 2 字 bigram 才能真的命中。
    """
    terms: list[str] = []

    # 1) 条款号：高精度，直接命中「第5条」
    terms += re.findall(r"第[一二三四五六七八九十百零\d]+[条款项]", question)
    # 2) 书名号里的法规名：高精度
    terms += re.findall(r"[《〈]([^》〉]{2,40})[》〉]", question)

    # 3) 中文片段：短片段整体保留 + 2 字滑窗
    for seg in re.split(r"[^\u4e00-\u9fff]+", question):
        if len(seg) < 2:
            continue
        if len(seg) <= 6 and seg not in _STOP_TERMS:
            terms.append(seg)
        for i in range(len(seg) - 1):
            bigram = seg[i:i + 2]
            if bigram not in _STOP_TERMS:
                terms.append(bigram)

    seen: set[str] = set()
    result: list[str] = []
    for term in terms:
        term = term.strip()
        if term and term not in seen:
            seen.add(term)
            result.append(term)
    return result[:_TERM_LIMIT]


_VECTOR_SQL = """
    SELECT c.id,
           1 - (c.embedding <=> $1::text::vector) AS sim
      FROM clauses c
      JOIN documents d ON d.id = c.doc_id
     WHERE c.embedding IS NOT NULL
       AND c.level <> 'preamble'
       AND (COALESCE(c.effective_from, d.effective_from) IS NULL
              OR COALESCE(c.effective_from, d.effective_from) <= $2::date)
       AND (COALESCE(c.effective_to, d.effective_to) IS NULL
              OR COALESCE(c.effective_to, d.effective_to) >= $2::date)
     ORDER BY c.embedding <=> $1::text::vector
     LIMIT $3
"""

_KEYWORD_SQL = """
    SELECT c.id,
           (SELECT count(*) FROM unnest($1::text[]) AS p
             WHERE c.content ILIKE p) AS hit
      FROM clauses c
      JOIN documents d ON d.id = c.doc_id
     WHERE c.content ILIKE ANY($1::text[])
       AND c.level <> 'preamble'
       AND (COALESCE(c.effective_from, d.effective_from) IS NULL
              OR COALESCE(c.effective_from, d.effective_from) <= $2::date)
       AND (COALESCE(c.effective_to, d.effective_to) IS NULL
              OR COALESCE(c.effective_to, d.effective_to) >= $2::date)
     LIMIT $3
"""

# 注意：这里刻意「不在 SQL 里 ORDER BY hit DESC」。
# 实测按命中词数排序会系统性偏向长条款（长文本天然命中更多 bigram），
# 把精确的短条款挤出候选，评测 Recall@5 从 0.93 掉到 0.7 左右。
# 改为取一个更大的候选池，再在 Python 侧按命中数取前 N —— 与调优前的行为一致。

_FETCH_SQL = """
    SELECT c.id, c.doc_key, c.clause_no, c.clause_path, c.content,
           d.title, d.source_url,
           COALESCE(c.effective_from, d.effective_from) AS eff_from
      FROM clauses c
      JOIN documents d ON d.id = c.doc_id
     WHERE c.id = ANY($1::bigint[])
"""


async def _vector_candidates(
    pool: asyncpg.Pool, vec: list[float], as_of: date, k: int
) -> list[tuple[int, float]]:
    """向量路：只返回 (id, 相似度)。

    $1::text::vector 的双重转换不能省：直接写 $1::vector 会让 Postgres 把参数类型
    推断成 vector，而 asyncpg 没有该类型的 codec。
    """
    vec_text = "[" + ",".join(f"{x:.8g}" for x in vec) + "]"
    rows = await pool.fetch(_VECTOR_SQL, vec_text, as_of, k)
    return [(r["id"], float(r["sim"])) for r in rows]


async def _keyword_candidates(
    pool: asyncpg.Pool, question: str, terms: list[str], as_of: date, k: int
) -> list[tuple[int, float]]:
    """关键词路：只返回 (id, 命中词数)。命中词数在 SQL 里算，避免回传正文。"""
    if not terms:
        return []
    patterns = [f"%{t}%" for t in terms]
    rows = await pool.fetch(_KEYWORD_SQL, patterns, as_of, max(k * _KEYWORD_OVERFETCH, 120))
    cand = [(r["id"], float(r["hit"])) for r in rows]
    # 在 Python 侧排序：SQL 里排会偏向长条款（见 _KEYWORD_SQL 上方的注释）
    cand.sort(key=lambda x: x[1], reverse=True)
    return cand[:k]


def _rrf_fuse(
    vector_ids: list[tuple[int, float]],
    keyword_ids: list[tuple[int, float]],
    k: int = 60,
) -> list[tuple[int, float, float | None]]:
    """RRF 融合：score = Σ 1/(k + rank)。返回 (id, 融合分, 向量相似度)。"""
    scores: dict[int, float] = {}
    sims: dict[int, float] = {}

    for route in (vector_ids, keyword_ids):
        for rank, (cid, _score) in enumerate(route, start=1):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)

    for cid, sim in vector_ids:
        sims[cid] = sim

    fused = [(cid, sc, sims.get(cid)) for cid, sc in scores.items()]
    fused.sort(key=lambda x: x[1], reverse=True)
    return fused


async def _fetch_hits(pool: asyncpg.Pool, ids: list[int]) -> dict[int, ClauseHit]:
    """按 id 取回正文。这是唯一一次传输大字段的查询，只对最终要用的几条发起。"""
    if not ids:
        return {}
    rows = await pool.fetch(_FETCH_SQL, ids)
    out: dict[int, ClauseHit] = {}
    for r in rows:
        out[r["id"]] = ClauseHit(
            clause_id=r["id"],
            doc_key=r["doc_key"],
            doc_title=r["title"],
            clause_no=r["clause_no"],
            clause_path=r["clause_path"],
            content=r["content"],
            effective_from=r["eff_from"],
            url=r["source_url"],
        )
    return out


async def hybrid_retrieve(
    pool: asyncpg.Pool,
    question: str,
    as_of: date,
    settings: Settings,
    limit: int | None = None,
    meta: dict | None = None,
) -> list[ClauseHit]:
    """混合检索主入口：两路并行召回 → RRF 融合 → 取前 limit 条正文。

    降级策略：两路互不拖累。embedding 挂了关键词路照样出结果，反之亦然。
    只有两路都失败时才向上抛错。

    meta 是可选输出参数，会写入：
      vector_ok / keyword_ok  两路是否成功
      kw_top_score            关键词路最高命中词数
    上层用它决定「没有向量信号时该怎么保守地判拒答」。
    """
    limit = limit or settings.top_k_final
    terms = _extract_terms(question)

    vector_task = asyncio.create_task(
        _search_vector_guard(pool, question, as_of, settings)
    )
    keyword_task = asyncio.create_task(
        _search_keyword_guard(pool, question, terms, as_of, settings)
    )
    (vector_ids, vector_error), (keyword_ids, keyword_error) = await asyncio.gather(
        vector_task, keyword_task
    )

    if vector_error is not None:
        logger.warning("向量路不可用，已降级为纯关键词检索: %s", vector_error)
    if keyword_error is not None:
        logger.warning("关键词路不可用，已降级为纯向量检索: %s", keyword_error)

    if meta is not None:
        meta["vector_ok"] = vector_error is None
        meta["keyword_ok"] = keyword_error is None
        meta["kw_top_score"] = max((s for _, s in keyword_ids), default=0.0)

    if vector_error is not None and keyword_error is not None:
        raise keyword_error

    # 两路进入融合的候选数必须对称。
    # 关键词路按命中词数排序后可能长达 90 条，如果全部参与 RRF，
    # 它会系统性地压过只在向量路命中的高分条款（实测 B01 的 gold 排向量第 1 却被挤到 top5 之外）。
    # 所以关键词只取前 top_k_keyword 条，与向量路对齐。
    fused = _rrf_fuse(vector_ids, keyword_ids[:settings.top_k_keyword])[:limit]

    hits_map = await _fetch_hits(pool, [cid for cid, _, _ in fused])

    result: list[ClauseHit] = []
    for cid, score, sim in fused:
        hit = hits_map.get(cid)
        if hit is None:
            continue
        hit.score = score
        hit.vector_sim = sim
        result.append(hit)
    return result


async def _search_vector_guard(
    pool: asyncpg.Pool, question: str, as_of: date, settings: Settings
) -> tuple[list[tuple[int, float]], Exception | None]:
    """向量路的异常护栏：把异常收成返回值，让另一路能继续跑。"""
    try:
        vec = await embed_query(question, settings)
        return await _vector_candidates(pool, vec, as_of, settings.top_k_vector), None
    except Exception as exc:  # noqa: BLE001
        return [], exc


async def _search_keyword_guard(
    pool: asyncpg.Pool, question: str, terms: list[str], as_of: date, settings: Settings
) -> tuple[list[tuple[int, float]], Exception | None]:
    """关键词路的异常护栏。"""
    try:
        return await _keyword_candidates(pool, question, terms, as_of,
                                         settings.top_k_keyword), None
    except Exception as exc:  # noqa: BLE001
        return [], exc
