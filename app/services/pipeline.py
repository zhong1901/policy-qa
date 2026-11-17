"""问答主流程，写成异步生成器，供「一次性返回」和「流式返回」共用。

为什么这么设计：
方案三的流式要在检索完成时先把条款吐给用户（1–2 秒内就有反馈），
但生成与校验还要 6–20 秒。把流程改成一个会 yield 中间事件的生成器，
流式接口直接转发事件；普通接口则只取最后那个 answer 事件。

好处是不复制业务逻辑——四道闸、缓存、写日志只有一份实现，
两条路径不可能出现「流式版漏了某个校验」这种问题。

事件类型：
  status    进度提示（stage + 文案）
  retrieval 检索完成的条款列表
  answer    最终响应（与 /api/ask 的响应体完全一致）
  error     出错
"""
import json
import logging
import time
from datetime import date
from typing import AsyncIterator

import asyncpg
from fastapi import HTTPException

from app import cache
from app.config import Settings
from app.schemas import AskResponse, Citation, Material
from app.services.embed import EmbeddingFailed, EmbeddingNotConfigured
from app.services.generate import (
    GenerationFailed,
    LLMNotConfigured,
    generate,
    regenerate_with_feedback,
)
from app.services.retrieve import ClauseHit, hybrid_retrieve
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
) -> None:
    """写查询日志。失败只记 warning，绝不影响用户拿到答案。"""
    try:
        await pool.execute(
            """INSERT INTO query_log
               (question, as_of, retrieved_ids, answer, refused, latency_ms, refused_reason)
               VALUES ($1, $2, $3, $4, $5, $6, $7)""",
            question,
            as_of,
            retrieved_ids,
            json.dumps(resp.model_dump(mode="json"), ensure_ascii=False),
            resp.refused,
            resp.latency_ms,
            refused_reason,
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


async def ask_events(
    pool: asyncpg.Pool,
    settings: Settings,
    question: str,
    as_of: date,
) -> AsyncIterator[tuple[str, dict]]:
    """主流程。yield (事件类型, 数据)；最后一次必定是 answer 或 error。"""
    started = time.perf_counter()
    ckey = cache.make_key(question, as_of.isoformat())

    async def finish(resp: AskResponse, ids: list[int], reason: str | None) -> AskResponse:
        if settings.cache_enabled:
            await cache.put(pool, ckey, question, as_of,
                            resp.model_dump(mode="json"), ids)
        await _log_query(pool, question, as_of, ids, resp, reason)
        return resp

    # ---- 关 ⓪：缓存 ----
    if settings.cache_enabled:
        hit = await cache.get(pool, ckey, settings.cache_ttl_days)
        if hit is not None:
            data = dict(hit["answer"])
            data["cached"] = True
            data["latency_ms"] = int((time.perf_counter() - started) * 1000)
            resp = AskResponse(**data)
            await _log_query(pool, question, as_of, hit["retrieved_ids"], resp, "cache_hit")
            yield ("answer", resp.model_dump(mode="json"))
            return

    # ---- 检索 ----
    yield ("status", {"stage": "retrieving", "text": "正在检索政策条款"})
    meta: dict = {}
    try:
        hits = await hybrid_retrieve(pool, question, as_of, settings,
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

    # 检索一出来就先推给前端：这是「方案三」的全部意义
    if hits:
        yield ("retrieval", {
            "clauses": [_clause_brief(h) for h in hits],
            "top_sim": round(hits[0].vector_sim, 3) if hits[0].vector_sim is not None else None,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        })

    # ---- 闸 ①：空 ----
    if not hits:
        resp = await finish(_refuse(as_of, started, "检索结果为空"), ids, "empty")
        yield ("answer", resp.model_dump(mode="json"))
        return

    # ---- 闸 ②：证据强度 ----
    # 这里有个曾经真实存在的漏洞：向量路挂了就没有 vector_sim，
    # 旧写法 `if top_sim is not None and ...` 会直接短路，等于降级时什么都能答。
    # 现在按「有没有向量信号」分成两条判据。
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
        answer = await generate(question, hits, settings)
    except LLMNotConfigured as exc:
        yield ("error", {"detail": str(exc), "status": 503})
        return
    except GenerationFailed as exc:
        logger.error("生成失败：%s", exc)
        yield ("error", {"detail": "生成服务暂不可用", "status": 503})
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
            answer = await regenerate_with_feedback(question, hits, settings, problems)
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
