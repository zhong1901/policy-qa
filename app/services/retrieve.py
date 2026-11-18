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


# 口语 → 公文用语。这是关键词路由最大的坑：
# 用户问「社保交满几年」，公文写的是「参加本市职工社会保险满 7 年」，
# ILIKE '%社保%' 一个字都匹配不上。同样，「落户」在正式条文里写作「申办常住户口」。
# 不做这层映射，关键词路等于对这些最常见的提问方式失明。
_SYNONYMS = {
    "社保": ["社会保险"],
    "五险": ["社会保险"],
    "个税": ["个人所得税"],
    "落户": ["常住户口", "申办本市常住户口"],
    "户口": ["常住户口"],
    "公积金": ["住房公积金"],
    "学位证": ["学历学位证书"],
    "居住证": ["《上海市居住证》"],
    "积分": ["总积分", "标准分值"],
    "职称": ["专业技术职务", "专业技术职称"],
    "毕业": ["毕业生"],
    "补贴": ["补贴", "补助"],
}


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

    # 同义词扩展：把口语词补上公文写法，否则「社保」这类高频词等于没检索
    for term in list(result):
        for syn in _SYNONYMS.get(term, []):
            if syn not in seen:
                seen.add(syn)
                result.append(syn)

    return result[:_TERM_LIMIT]


_VECTOR_SQL = """
    SELECT c.id,
           1 - (c.embedding <=> $1::text::vector) AS sim
      FROM clauses c
      JOIN documents d ON d.id = c.doc_id
     WHERE c.embedding IS NOT NULL
       AND c.level <> 'preamble'
       AND ($4::boolean
              OR (COALESCE(c.effective_from, d.effective_from) IS NULL
                    OR COALESCE(c.effective_from, d.effective_from) <= $2::date)
                 AND (COALESCE(c.effective_to, d.effective_to) IS NULL
                    OR COALESCE(c.effective_to, d.effective_to) >= $2::date))
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
       AND ($4::boolean
              OR (COALESCE(c.effective_from, d.effective_from) IS NULL
                    OR COALESCE(c.effective_from, d.effective_from) <= $2::date)
                 AND (COALESCE(c.effective_to, d.effective_to) IS NULL
                    OR COALESCE(c.effective_to, d.effective_to) >= $2::date))
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
    pool: asyncpg.Pool, vec: list[float], as_of: date, k: int,
    include_expired: bool = False,
) -> list[tuple[int, float]]:
    """向量路：只返回 (id, 相似度)。

    $1::text::vector 的双重转换不能省：直接写 $1::vector 会让 Postgres 把参数类型
    推断成 vector，而 asyncpg 没有该类型的 codec。

    为什么要显式调 hnsw.ef_search：
    pgvector 的 HNSW 是近似检索，ef_search 默认 40，意味着「只探 40 个候选就收工」。
    实测同一查询 LIMIT 30 完全找不到的条款，把 ef_search 调大后能排进前几。
    这是召回率的一个隐藏上限，不加这一行就会莫名其妙漏条款。

    include_expired：版本对比问题必须能取到已失效的旧版本，
    否则「2020 版和 2024 版有什么不同」永远只能拿到一半。
    """
    vec_text = "[" + ",".join(f"{x:.8g}" for x in vec) + "]"
    async with pool.acquire() as conn:
        async with conn.transaction():
            # SET LOCAL 只在当前事务生效，不会污染连接池里的其它请求
            await conn.execute("SET LOCAL hnsw.ef_search = 200")
            rows = await conn.fetch(_VECTOR_SQL, vec_text, as_of, k, include_expired)
    return [(r["id"], float(r["sim"])) for r in rows]


async def _keyword_candidates(
    pool: asyncpg.Pool, question: str, terms: list[str], as_of: date, k: int,
    include_expired: bool = False,
) -> list[tuple[int, float]]:
    """关键词路：只返回 (id, 命中词数)。命中词数在 SQL 里算，避免回传正文。"""
    if not terms:
        return []
    patterns = [f"%{t}%" for t in terms]
    rows = await pool.fetch(_KEYWORD_SQL, patterns, as_of,
                            max(k * _KEYWORD_OVERFETCH, 120), include_expired)
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
    include_expired: bool = False,
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
        _search_vector_guard(pool, question, as_of, settings, include_expired)
    )
    keyword_task = asyncio.create_task(
        _search_keyword_guard(pool, question, terms, as_of, settings, include_expired)
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

    # 兜底：保证每一路的「第一名」一定出现在最终结果里。
    # RRF 的固有缺陷是「两路都出现的中等名次」会赢过「只在一路出现的第一名」，
    # 而后者往往是更确定的答案。实测 B01 的 gold 是向量路第 1，却被挤出前 5。
    # 只补每路 top-1，是最小代价的补偿——不要扩大到 top-3，那样会把无关条款塞进来。
    fused_ids = {cid for cid, _, _ in fused}
    forced = []
    for route in (vector_ids, keyword_ids):
        if route and route[0][0] not in fused_ids:
            cid = route[0][0]
            forced.append((cid, 0.0,
                           next((s for i, s in vector_ids if i == cid), None)))
    if forced:
        fused = fused + forced

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
    pool: asyncpg.Pool, question: str, as_of: date, settings: Settings,
    include_expired: bool = False,
) -> tuple[list[tuple[int, float]], Exception | None]:
    """向量路的异常护栏：把异常收成返回值，让另一路能继续跑。"""
    try:
        vec = await embed_query(question, settings)
        return await _vector_candidates(pool, vec, as_of, settings.top_k_vector,
                                       include_expired), None
    except Exception as exc:  # noqa: BLE001
        return [], exc


async def _search_keyword_guard(
    pool: asyncpg.Pool, question: str, terms: list[str], as_of: date,
    settings: Settings, include_expired: bool = False,
) -> tuple[list[tuple[int, float]], Exception | None]:
    """关键词路的异常护栏。"""
    try:
        return await _keyword_candidates(pool, question, terms, as_of,
                                         settings.top_k_keyword,
                                         include_expired), None
    except Exception as exc:  # noqa: BLE001
        return [], exc


# ────────────────── 跨版本召回 ──────────────────
# 「2024版和2020版有什么区别」这类问题，普通检索注定答不对：
# 时间过滤会把旧版本排掉，只剩一半的素材。
# 所以单独开一条路径：先认出问题要跨版本，再按「同族文档 + 同条号」各取一条。
CROSS_VERSION_HINTS = ("版", "相比", "区别", "不同", "变化", "修订",
                       "最新版", "历次", "调整过", "旧版", "新版")

# 标题相似度阈值。同族版本的标题几乎只差年份和文号，0.45 足够稳，
# 又不会把无关文件拉进来。
SIBLING_SQL = """
    SELECT d.id, left(d.title, 60) AS title,
           COALESCE(d.effective_from, d.publish_date) AS eff,
           COALESCE(d.effective_to, DATE '9999-01-01') AS until,
           similarity(d.title, $1) AS sim
      FROM documents d
     WHERE similarity(d.title, $1) > 0.45
     ORDER BY sim DESC, eff DESC NULLS LAST
     LIMIT 6
"""

CLAUSE_BY_NO_SQL = """
    SELECT c.id, c.doc_key, c.clause_no, c.clause_path, c.content,
           d.title, d.source_url,
           COALESCE(c.effective_from, d.effective_from) AS eff_from,
           COALESCE(c.effective_to, d.effective_to) AS eff_to
      FROM clauses c JOIN documents d ON d.id = c.doc_id
     WHERE d.id = $1 AND c.clause_no = $2
     ORDER BY c.id LIMIT 1
"""


# 政策族识别：从问题里认出它问的是哪部法规。
# 不能依赖「模糊检索 top1」——实测问「居转户 2024 版和 2020 版」，top1 会跑到
# 「城乡居民基本养老保险政策问答」上去，基准一错，后面全错。
FAMILIES = [
    ("居转户", ("居转户", "常住户口", "居住证转")),
    ("引进人才", ("引进人才", "人才引进", "人才落户")),
    ("居住证申办实施细则", ("申办实施细则", "居住证申办", "办理居住证")),
    ("居住证积分", ("居住证积分", "积分管理", "标准分值", "积分达标")),
    ("海外人才居住证", ("海外人才居住证", "海外人才")),
    ("应届生落户", ("非上海生源", "应届毕业生", "应届生落户")),
]

FAMILY_DOC_SQL = """
    SELECT d.id, left(d.title, 60) AS title,
           COALESCE(d.effective_from, d.publish_date) AS eff,
           COALESCE(d.effective_to, DATE '9999-01-01') AS until
      FROM documents d
     WHERE d.title LIKE ANY($1::text[])
     ORDER BY eff DESC NULLS LAST
     LIMIT 8
"""

# 在一篇文档里选「最贴合问题」的那一条：按问题关键词的命中数排
BEST_CLAUSE_SQL = """
    SELECT c.id, c.doc_key, c.clause_no, c.clause_path, c.content,
           d.title, d.source_url,
           COALESCE(c.effective_from, d.effective_from) AS eff_from,
           (SELECT count(*) FROM unnest($2::text[]) AS p
             WHERE c.content ILIKE p) AS hit
      FROM clauses c JOIN documents d ON d.id = c.doc_id
     WHERE d.id = $1 AND c.level <> 'preamble'
     ORDER BY hit DESC, c.ordinal
     LIMIT 1
"""


def wants_cross_version(question: str) -> bool:
    """判断问题是不是在问版本差异。宁可漏判——误判会让无关问题拿到一堆旧版本。"""
    return any(h in question for h in CROSS_VERSION_HINTS)


def detect_family(question: str) -> tuple[str, tuple[str, ...]] | None:
    for name, hints in FAMILIES:
        if any(h in question for h in hints):
            return name, hints
    return None


async def cross_version_retrieve(
    pool: asyncpg.Pool,
    question: str,
    as_of: date,
    settings: Settings,
    max_versions: int = 4,
) -> list[ClauseHit]:
    """跨版本召回：拿到同一部法规多个版本的对应条款，供模型比对。

    步骤：认政策族 → 找该族全部版本 → 每个版本选最贴合问题的那一条。
    没有这一步，「2024 版和 2020 版有什么不同」这类问题永远只能拿到一半素材
    （旧版本被时间过滤排掉了）。
    """
    fam = detect_family(question)
    if fam is None:
        return []
    name, hints = fam
    # 用标题关键词去匹配，比如「常住户口办法」能同时命中 2020 版和 2024 版
    patterns = [f"%{name}%"] + [f"%{h}%" for h in hints]
    docs = await pool.fetch(FAMILY_DOC_SQL, patterns)
    if not docs:
        logger.info("跨版本：未找到「%s」族文档", name)
        return []

    terms = _extract_terms(question)
    pats = [f"%{t}%" for t in terms]

    # 第一步：每个版本各自选「最贴合问题」的那一条，看它们落在哪个条号上。
    best_rows: list[tuple[dict, dict]] = []
    for d in docs[:max_versions]:
        row = await pool.fetchrow(BEST_CLAUSE_SQL, d["id"], pats)
        if row is not None:
            best_rows.append((d, row))
    if not best_rows:
        return []

    # 第二步：取众数条号。必须对齐到「同一条」才谈得上比较——
    # 实测不投票时，2024 版选到第 5 条、2020 版选到第 6 条，
    # 模型拿到的是两条不同的条文，根本没法比。
    from collections import Counter
    counter = Counter(r["clause_no"] for _, r in best_rows)
    target_no, votes = counter.most_common(1)[0]
    logger.info("跨版本 %s：共识条号 %s（%d/%d 版本命中）",
                name, target_no, votes, len(best_rows))

    out: list[ClauseHit] = []
    seen: set[int] = set()
    for d, _ in best_rows:
        row = await pool.fetchrow(CLAUSE_BY_NO_SQL, d["id"], target_no)
        if row is None or row["id"] in seen:
            continue
        seen.add(row["id"])
        out.append(ClauseHit(
            clause_id=row["id"],
            doc_key=row["doc_key"],
            doc_title=row["title"],
            clause_no=row["clause_no"],
            clause_path=row["clause_path"],
            content=row["content"],
            effective_from=row["eff_from"],
            url=row["source_url"],
            score=float(votes),
            vector_sim=None,
        ))

    out.sort(key=lambda h: (h.effective_from is None, h.effective_from), reverse=True)
    logger.info("跨版本召回 %s：%d 个版本（%s）", name, len(out),
                "、".join(f"{h.clause_no}@{h.effective_from}" for h in out))
    return out
