"""混合检索：向量召回 + 关键词召回 + RRF 融合 + 时间过滤。"""
import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import date

import asyncpg

from app.config import Settings
from app.services.embed import EmbeddingFailed, embed_query

logger = logging.getLogger(__name__)

# 停用词：这些 bigram 会召回海量无关条款（"什么""可以"几乎出现在每条通知里），必须丢。
_STOP_TERMS = {
    "什么", "怎么", "可以", "需要", "我是", "请问", "多少", "是否", "哪些",
    "如何", "应该", "能否", "是不", "不是", "有没", "没有", "这个", "那个",
    "我们", "你们", "他们", "的话", "如果", "就是", "还是", "或者", "以及",
    "一个", "一下", "现在", "目前", "具体", "相关",
}

# 关键词数量上限：ILIKE ANY 的 pattern 数组越大，SQL 越慢
_TERM_LIMIT = 30

# 关键词路先多取几倍，再在 Python 侧按命中词数重排 —— 这样打分逻辑可读、可测，
# 也不用在 SQL 里写相关子查询（那会让 7690 行变成 7690×N 次 ILIKE）。
_KEYWORD_OVERFETCH = 4


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

    关键点：不能按标点切完就整段拿去 ILIKE。
    「租房补贴需要什么材料」整段做 pattern 在 7690 条条款里命中数约为 0；
    必须切成 2 字 bigram（"租房""补贴""材料"），才能真的命中。
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

    # 去重保序（先出现的优先级更高：条款号/法规名在最前面）
    seen: set[str] = set()
    result: list[str] = []
    for term in terms:
        term = term.strip()
        if term and term not in seen:
            seen.add(term)
            result.append(term)
    return result[:_TERM_LIMIT]


_VECTOR_SQL = """
    SELECT c.id, c.doc_key, c.clause_no, c.clause_path, c.content,
           d.title, d.source_url,
           COALESCE(c.effective_from, d.effective_from) AS eff_from,
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
    SELECT c.id, c.doc_key, c.clause_no, c.clause_path, c.content,
           d.title, d.source_url,
           COALESCE(c.effective_from, d.effective_from) AS eff_from
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


async def _vector_search(
    pool: asyncpg.Pool, vec: list[float], as_of: date, k: int
) -> list[ClauseHit]:
    """向量路：按余弦相似度召回 top-k。embedding 为空的行直接排除。

    时间过滤用 COALESCE 回落：clauses 的有效期为空时，继承所属 documents 的有效期，
    这是最容易漏掉的一处——漏了会把历史已废止的文件也当成「仍有效」召回。

    注意 $1::text::vector 的双重转换：先让参数类型确定是 text（asyncpg 认识），
    再在库里转 vector。直接写 $1::vector 会让 Postgres 把参数类型推断成 vector，
    而 asyncpg 没有 vector 类型的 codec，能否回落成 text 取决于版本行为。
    """
    # pgvector 的文本输入格式就是 '[0.1,0.2,...]'
    vec_text = "[" + ",".join(f"{x:.8g}" for x in vec) + "]"
    rows = await pool.fetch(_VECTOR_SQL, vec_text, as_of, k)
    return [_row_to_hit(r, vector_sim=float(r["sim"])) for r in rows]


async def _keyword_search(
    pool: asyncpg.Pool, question: str, terms: list[str], as_of: date, k: int
) -> list[ClauseHit]:
    """关键词路：ILIKE 命中任意一个 bigram，再按「命中词数」重排。

    为什么不用 similarity()/%：pg_trgm 的 similarity 是对称相似度，对长度差极敏感——
    几百字的条款 vs 十几个字的问题，分值通常只有 0.02~0.08，永远低于 % 的默认阈值 0.3，
    等于那一路是死代码。所以这里只用 ILIKE 做召回，打分挪到 Python。
    """
    if not terms:
        return []

    patterns = [f"%{t}%" for t in terms]
    rows = await pool.fetch(
        _KEYWORD_SQL, patterns, as_of, max(k * _KEYWORD_OVERFETCH, 60)
    )

    hits: list[ClauseHit] = []
    for row in rows:
        content = row["content"]
        # 命中词数越多越相关；同分时短条款优先（长条款容易"擦边"命中很多词）
        hit_count = sum(1 for t in terms if t in content)
        hit = _row_to_hit(row)
        hit.score = float(hit_count)
        hits.append(hit)

    hits.sort(key=lambda h: (-h.score, len(h.content)))
    return hits[:k]


def _row_to_hit(row: asyncpg.Record, vector_sim: float | None = None) -> ClauseHit:
    """把数据库一行转成 ClauseHit。两路查询列名一致，所以可复用。"""
    return ClauseHit(
        clause_id=row["id"],
        doc_key=row["doc_key"],
        doc_title=row["title"],
        clause_no=row["clause_no"],
        clause_path=row["clause_path"],
        content=row["content"],
        effective_from=row["eff_from"],
        url=row["source_url"],
        vector_sim=vector_sim,
    )


def _rrf_fuse(
    vector_hits: list[ClauseHit], keyword_hits: list[ClauseHit], k: int = 60
) -> list[ClauseHit]:
    """RRF 融合：score = Σ 1/(k + rank)，rank 从 1 起。

    RRF 的好处是不用给两路调权重，两路结果按各自排名自然合并；
    同一 clause 在两路都命中时分数叠加，天然提升「双路都认为相关」的结果。
    """
    fused: dict[int, ClauseHit] = {}
    scores: dict[int, float] = {}

    for route in (vector_hits, keyword_hits):
        for rank, hit in enumerate(route, start=1):
            scores[hit.clause_id] = scores.get(hit.clause_id, 0.0) + 1.0 / (k + rank)
            existing = fused.get(hit.clause_id)
            if existing is None:
                fused[hit.clause_id] = hit
            elif hit.vector_sim is not None and existing.vector_sim is None:
                # 两路都命中时补上向量相似度（拒答阈值要用）
                existing.vector_sim = hit.vector_sim

    for cid, hit in fused.items():
        hit.score = scores[cid]
    return sorted(fused.values(), key=lambda h: h.score, reverse=True)


async def hybrid_retrieve(
    pool: asyncpg.Pool,
    question: str,
    as_of: date,
    settings: Settings,
    limit: int | None = None,
) -> list[ClauseHit]:
    """混合检索主入口：两路并行召回 → RRF 融合 → 取前 limit 条。

    降级策略：两路互不拖累。embedding 挂了关键词路照样出结果；关键词路挂了向量路照样出。
    只有两路都失败时才向上抛错，路由层转 503。
    """
    limit = limit or settings.top_k_final
    terms = _extract_terms(question)

    vector_task = asyncio.create_task(
        _search_vector_guard(pool, question, as_of, settings)
    )
    keyword_task = asyncio.create_task(
        _search_keyword_guard(pool, question, terms, as_of, settings)
    )
    vector_result, keyword_result = await asyncio.gather(vector_task, keyword_task)

    vector_hits, vector_error = vector_result
    keyword_hits, keyword_error = keyword_result

    if vector_error is not None:
        logger.warning("向量路不可用，已降级为纯关键词检索: %s", vector_error)
    if keyword_error is not None:
        logger.warning("关键词路不可用，已降级为纯向量检索: %s", keyword_error)

    # 两路都挂了才算真失败；单路空结果只是"没搜到"
    if vector_error is not None and keyword_error is not None:
        raise EmbeddingFailed(f"检索两路均失败: {vector_error} / {keyword_error}")

    fused = _rrf_fuse(vector_hits, keyword_hits)
    return fused[:limit]


async def _search_vector_guard(
    pool: asyncpg.Pool, question: str, as_of: date, settings: Settings
) -> tuple[list[ClauseHit], Exception | None]:
    """向量路的异常护栏：把异常收成返回值，让另一路能继续跑。"""
    try:
        vec = await embed_query(question, settings)
        return await _vector_search(pool, vec, as_of, settings.top_k_vector), None
    except Exception as exc:  # noqa: BLE001 - 这里就要兜住一切，避免拖垮另一路
        return [], exc


async def _search_keyword_guard(
    pool: asyncpg.Pool, question: str, terms: list[str], as_of: date, settings: Settings
) -> tuple[list[ClauseHit], Exception | None]:
    """关键词路的异常护栏。"""
    try:
        return await _keyword_search(pool, question, terms, as_of, settings.top_k_keyword), None
    except Exception as exc:  # noqa: BLE001
        return [], exc
