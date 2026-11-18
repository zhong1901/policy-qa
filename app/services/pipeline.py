"""问答主流程，写成异步生成器，供「一次性返回」和「流式返回」共用。

为什么这么设计：
方案三的流式要在检索完成时先把条款吐给用户（1–2 秒内就有反馈），
但生成与校验还要 6–20 秒。把流程改成一个会 yield 中间事件的生成器，
流式接口直接转发事件；普通接口则只取最后那个 answer 事件。

多轮与记忆（本轮新增）：
- 短期：从 turns 表取最近 3 轮，用于查询改写和注入 prompt；
- 改写：把「那需要什么材料」补全成独立问题再检索，否则召回全是噪声；
- 长期：user_profiles 里的画像是用户主动填写的，不是模型猜的；
- 缓存：**只在没有历史时启用**。同一个问题在不同上下文里含义不同，
  带历史还命中缓存等于答错。

事件类型：
  status    进度提示（stage + 文案）
  retrieval 检索完成的条款列表
  answer    最终响应（与 /api/ask 的响应体完全一致）
  error     出错
"""
import json
import logging
import re
import time
from datetime import date
from typing import AsyncIterator

import asyncpg

from app import cache
from app.config import Settings
from app.schemas import AskResponse, Citation, Material
from app.services import memory
from app.services.eligibility import Profile, evaluate, summarize
from app.services.embed import EmbeddingFailed, EmbeddingNotConfigured
from app.services.generate import (
    GenerationFailed,
    LLMNotConfigured,
    generate,
    regenerate_with_feedback,
    rewrite_query,
)
from app.services.retrieve import (
    ClauseHit, _fetch_hits, cross_version_retrieve, hybrid_retrieve,
    wants_cross_version,
)
from app.services.verify import verify

logger = logging.getLogger(__name__)

REFUSAL_TEXT = "未找到明确的政策依据"


def _refuse(as_of: date, started: float, reason: str) -> AskResponse:
    logger.info("拒答：%s", reason)
    return AskResponse(
        refused=True,
        conclusion=REFUSAL_TEXT,
        as_of=as_of,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


def _to_response(answer: dict, as_of: date, started: float) -> AskResponse:
    return AskResponse(
        refused=answer.get("refused", False),
        conclusion=answer.get("conclusion") or "",
        conditions=[str(x) for x in answer.get("conditions") or []],
        materials=[
            Material(name=str(m.get("name", "")), note=str(m.get("note", "")))
            for m in answer.get("materials") or []
            if isinstance(m, dict) and m.get("name")
        ],
        process=[str(x) for x in answer.get("process") or []],
        deadline=answer.get("deadline"),
        citations=[Citation(**c) for c in answer.get("citations") or []],
        confidence=answer.get("confidence") or "low",
        conflicts=[str(x) for x in answer.get("conflicts") or []],
        as_of=as_of,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


async def _log_query(
    pool: asyncpg.Pool,
    question: str,
    as_of: date,
    retrieved_ids: list[int],
    resp: AskResponse,
    refused_reason: str | None,
    session_id: str = "",
    visitor_hash: str = "",
) -> None:
    """写查询日志。失败只记 warning，绝不影响用户拿到答案。"""
    try:
        await pool.execute(
            """INSERT INTO query_log
               (question, as_of, retrieved_ids, answer, refused, latency_ms,
                refused_reason, session_id, visitor_hash)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
            question,
            as_of,
            retrieved_ids,
            json.dumps(resp.model_dump(mode="json"), ensure_ascii=False),
            resp.refused,
            resp.latency_ms,
            refused_reason,
            session_id or None,
            visitor_hash or None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("写 query_log 失败（不影响返回）：%s", exc)


def _clause_brief(h: ClauseHit) -> dict:
    """检索阶段先给用户看的条款摘要，不含完整正文。"""
    return {
        "clause_id": h.clause_id,
        "doc_title": h.doc_title,
        "clause_no": h.clause_no,
        "snippet": (h.content or "")[:90],
    }


# ── 意图路由：判断「我能不能落户」这类问题该交给规则引擎 ──
# 用正则而不是字符串包含：「我现在能申请居转户吗」里「我」和「能」隔了「现在」，
# 纯字符串匹配会漏掉；而「我能申请租房补贴吗」又能正确排除（不是落户问题）。
_SELF_RE = re.compile(r"我[^。？?！!]{0,6}(能|可以|够|符合|是否)")
_PATH_HINTS = ("落户", "户口", "居转户", "引进人才", "应届")


def _wants_eligibility(q: str) -> bool:
    """「问自己」+「问落户」同时出现，才交给规则引擎。

    宁可漏判也不要误判：把「我能申请租房补贴吗」错送进规则引擎，
    会得到「三条落户路径都不满足」这种驴唇不对马嘴的答案。
    """
    return bool(_SELF_RE.search(q)) and any(p in q for p in _PATH_HINTS)


def _profile_from_dict(d: dict) -> Profile:
    fields = Profile.__dataclass_fields__
    return Profile(**{k: v for k, v in d.items() if k in fields})


def _named_path(q: str) -> str | None:
    """问题里点名了哪条落户路径。点名了就只答那一条。"""
    if "居转户" in q or "居住证转" in q or "持证人员" in q:
        return "juzhuanhu"
    if "引进人才" in q or "人才引进" in q:
        return "talent"
    if "应届" in q or "毕业生落户" in q:
        return "fresh"
    return None


async def ask_events(
    pool: asyncpg.Pool,
    settings: Settings,
    question: str,
    as_of: date,
    session_id: str = "",
    visitor_hash: str = "",
) -> AsyncIterator[tuple[str, dict]]:
    """主流程。yield (事件类型, 数据)；最后一次必定是 answer 或 error。"""
    started = time.perf_counter()

    # ── 记忆：短期历史 + 长期画像 ──
    history = await memory.get_history(pool, session_id) if session_id else []
    profile = await memory.get_profile(pool, session_id) if session_id else None
    profile_text = memory.profile_to_text(profile) if profile else ""

    # 有历史就不走缓存：同一个问题在不同上下文里含义不同，命中缓存等于答错
    use_cache = settings.cache_enabled and not history
    ckey = cache.make_key(question, as_of.isoformat())

    async def remember(resp: AskResponse) -> None:
        if not session_id:
            return
        await memory.ensure_session(pool, session_id)
        await memory.append_turn(pool, session_id, "user", question)
        await memory.append_turn(pool, session_id, "assistant",
                                 resp.conclusion or "", resp.model_dump(mode="json"))

    async def finish(resp: AskResponse, ids: list[int], reason: str | None) -> AskResponse:
        if use_cache:
            await cache.put(pool, ckey, question, as_of,
                            resp.model_dump(mode="json"), ids)
        await _log_query(pool, question, as_of, ids, resp, reason,
                         session_id, visitor_hash)
        await remember(resp)
        return resp

    # ---- 关 ⓪：意图路由 ----
    # 有画像 + 问的是「我能不能落户」→ 交给规则引擎，不走 RAG。
    # 这类问题要的是判定而不是检索，走 RAG 只会得到「未找到依据」。
    if profile and _wants_eligibility(question):
        yield ("status", {"stage": "eligibility", "text": "正在按你的档案逐条判定"})
        verdicts = evaluate(_profile_from_dict(profile))

        # 如果问题点名了某条路径，就只答那一条。
        # 否则会出现「问居转户、答应届生」这种错位。
        target = _named_path(question)
        if target:
            v = next((x for x in verdicts if x.key == target), None)
            if v is not None:
                if v.result == "符合":
                    concl = f"按你填写的情况，你符合「{v.name}」"
                elif v.result == "待确认":
                    concl = f"「{v.name}」还有信息需要确认"
                else:
                    concl = f"按你填写的情况，目前不符合「{v.name}」"
                miss = [c for c in v.checks if c.state != "MET"]
                resp = AskResponse(
                    refused=False,
                    conclusion=concl,
                    conditions=[f"{c.label} —— {c.detail}" for c in v.checks
                                if c.state != "满足" and c.detail],
                    process=[v.note] if v.note else [],
                    citations=[],
                    confidence="high",
                    note=f"（依据：{v.name}的申办条件）",
                    as_of=as_of,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
                hits_map = await _fetch_hits(pool, v.clause_ids)
                for i, cid in enumerate(v.clause_ids, 1):
                    h = hits_map.get(cid)
                    if h is None:
                        continue
                    resp.citations.append(Citation(
                        ref=f"C{i}", quote=(h.content or "")[:160], clause_id=h.clause_id,
                        doc_title=h.doc_title, clause_no=h.clause_no,
                        effective_from=h.effective_from.isoformat() if h.effective_from else None,
                        url=h.url))
                resp = await finish(resp, v.clause_ids, "eligibility_route_one")
                yield ("answer", resp.model_dump(mode="json"))
                return

        s = summarize(verdicts)
        best = next((v for v in verdicts if v.key == s.best_key), None)
        clause_ids = best.clause_ids if best else []
        hits_map = await _fetch_hits(pool, clause_ids)
        cits: list[Citation] = []
        for i, cid in enumerate(clause_ids, 1):
            h = hits_map.get(cid)
            if h is None:
                continue
            cits.append(Citation(
                ref=f"C{i}", quote=(h.content or "")[:160], clause_id=h.clause_id,
                doc_title=h.doc_title, clause_no=h.clause_no,
                effective_from=h.effective_from.isoformat() if h.effective_from else None,
                url=h.url))
        resp = AskResponse(
            refused=False,
            conclusion=s.headline,
            conditions=list(s.gaps),
            process=list(s.suggestions),
            citations=cits,
            confidence="high",
            note=(s.detail + ("　" + s.points_note if s.points_note else "")).strip(),
            as_of=as_of,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        resp = await finish(resp, clause_ids, "eligibility_route")
        yield ("answer", resp.model_dump(mode="json"))
        return

    # ---- 关 ⓪b：缓存（仅首轮）----
    if use_cache:
        hit = await cache.get(pool, ckey, settings.cache_ttl_days)
        if hit is not None:
            data = dict(hit["answer"])
            data["cached"] = True
            data["latency_ms"] = int((time.perf_counter() - started) * 1000)
            resp = AskResponse(**data)
            await _log_query(pool, question, as_of, hit["retrieved_ids"], resp,
                            "cache_hit", session_id, visitor_hash)
            await remember(resp)
            yield ("answer", resp.model_dump(mode="json"))
            return

    # ---- 多轮：改写追问 ----
    search_query = question
    if history:
        yield ("status", {"stage": "rewriting", "text": "正在理解你的追问"})
        search_query = await rewrite_query(question, history, settings)
        if search_query != question:
            yield ("status", {"stage": "rewritten", "text": f"理解为：{search_query}"})

    # ---- 检索 ----
    yield ("status", {"stage": "retrieving", "text": "正在检索政策条款"})
    meta: dict = {}
    hits: list[ClauseHit] = []
    cross_used = False

    # 版本对比类问题走专门通道：普通检索带时间过滤，只会拿到现行版本，
    # 「2020 版和 2024 版有什么不同」永远只答一半。这里按政策族跨版本召回。
    if wants_cross_version(search_query):
        try:
            hits = await cross_version_retrieve(pool, search_query, as_of, settings)
            cross_used = bool(hits)
        except Exception as exc:  # noqa: BLE001
            logger.warning("跨版本召回失败，回退普通检索：%s", exc)
        if cross_used:
            yield ("status", {"stage": "cross_version",
                              "text": f"已取到 {len(hits)} 个历史版本，正在比对"})

    try:
        if not cross_used:
            hits = await hybrid_retrieve(pool, search_query, as_of, settings,
                                         limit=settings.top_k_final, meta=meta)
    except EmbeddingNotConfigured as exc:
        yield ("error", {"detail": str(exc), "status": 503})
        return
    except EmbeddingFailed as exc:
        logger.error("检索失败：%s", exc)
        yield ("error", {"detail": "检索服务暂不可用", "status": 503})
        return
    except asyncpg.PostgresError as exc:
        logger.exception("数据库查询失败")
        yield ("error", {"detail": "数据库查询失败", "status": 503})
        return

    degraded = not meta.get("vector_ok", True)
    if degraded:
        yield ("status", {"stage": "degraded",
                          "text": "向量检索不可用，已降级为关键词检索"})

    ids = [h.clause_id for h in hits]

    if hits:
        yield ("retrieval", {
            "clauses": [_clause_brief(h) for h in hits],
            "top_sim": round(hits[0].vector_sim, 3) if hits[0].vector_sim is not None else None,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "rewritten": search_query if search_query != question else None,
        })

    # ---- 闸 ①：空 ----
    if not hits:
        resp = await finish(_refuse(as_of, started, "检索结果为空"), ids, "empty")
        yield ("answer", resp.model_dump(mode="json"))
        return

    # ---- 闸 ②：证据强度 ----
    if degraded:
        kw_score = meta.get("kw_top_score", 0)
        if kw_score < settings.degraded_min_keyword_hits:
            resp = await finish(
                _refuse(as_of, started,
                        f"降级模式关键词命中不足（{kw_score:g} < "
                        f"{settings.degraded_min_keyword_hits}）"),
                ids, "degraded_weak")
            yield ("answer", resp.model_dump(mode="json"))
            return
    else:
        top_sim = hits[0].vector_sim
        if top_sim is not None and top_sim < settings.refusal_threshold:
            resp = await finish(
                _refuse(as_of, started,
                        f"top1 相似度 {top_sim:.3f} < {settings.refusal_threshold}"),
                ids, "low_similarity")
            yield ("answer", resp.model_dump(mode="json"))
            return

    # ---- 生成 ----
    yield ("status", {"stage": "generating", "text": "正在生成答案并核对出处"})
    try:
        answer = await generate(question, hits, settings, profile_text, history)
    except LLMNotConfigured as exc:
        yield ("error", {"detail": str(exc), "status": 503})
        return
    except GenerationFailed as exc:
        logger.error("生成失败：%s", exc)
        # 把上游原因带出去：之前统一成「生成服务暂不可用」，
        # 结果评测里 69 题全挂也看不出是限流(429)还是服务端挂了(5xx)。
        yield ("error", {"detail": f"生成服务暂不可用（{str(exc)[:160]}）",
                         "status": 503})
        return

    if answer.get("refused"):
        resp = await finish(_refuse(as_of, started, "模型判定资料不足"), ids, "model_refused")
        yield ("answer", resp.model_dump(mode="json"))
        return

    # ---- 闸 ③④：引用校验 ----
    yield ("status", {"stage": "verifying", "text": "正在校验引用是否真实存在"})
    ok, problems = verify(answer, hits, settings)
    if not ok:
        logger.info("首次校验失败，带问题重试：%s", problems)
        try:
            answer = await regenerate_with_feedback(
                question, hits, settings, problems, profile_text, history)
            ok, problems2 = verify(answer, hits, settings)
            if not ok:
                resp = await finish(
                    _refuse(as_of, started, f"引用校验两次失败：{problems2}"),
                    ids, "verify_failed")
                yield ("answer", resp.model_dump(mode="json"))
                return
        except GenerationFailed as exc:
            logger.error("重试生成失败：%s", exc)
            resp = await finish(_refuse(as_of, started, "重试生成失败"), ids, "retry_failed")
            yield ("answer", resp.model_dump(mode="json"))
            return

    resp = _to_response(answer, as_of, started)
    resp = await finish(resp, ids, None)
    yield ("answer", resp.model_dump(mode="json"))
