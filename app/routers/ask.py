"""问答接口：POST /api/ask，串起 检索 → 生成 → 校验 → 拒答。

四道闸（任何一道触发就拒答，而不是硬答）：
  ① 检索为空
  ② top1 相似度低于阈值
  ③ 生成后引用校验不通过 → 带问题重试一次
  ④ 重试后仍不通过 → 拒答
"""
import json
import logging
import time
from datetime import date

import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from app.config import Settings, get_settings
from app.db import get_pool
from app.schemas import AskRequest, AskResponse, Citation, Material
from app.services.embed import EmbeddingFailed, EmbeddingNotConfigured
from app.services.generate import (
    LLMNotConfigured,
    GenerationFailed,
    generate,
    regenerate_with_feedback,
)
from app.services.retrieve import ClauseHit, hybrid_retrieve
from app.services.verify import verify

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["ask"])

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
    hits: list[ClauseHit],
    resp: AskResponse,
    refused_reason: str | None,
) -> None:
    """写查询日志。失败只记 warning，绝不影响用户拿到答案。

    refused_reason 必须落库：否则"召回对了却拒答"这种问题无法归因
    （是相似度闸、模型自拒，还是引用校验拦的）。
    """
    try:
        await pool.execute(
            """INSERT INTO query_log
               (question, as_of, retrieved_ids, answer, refused, latency_ms, refused_reason)
               VALUES ($1, $2, $3, $4, $5, $6, $7)""",
            question,
            as_of,
            [h.clause_id for h in hits],
            json.dumps(resp.model_dump(mode="json"), ensure_ascii=False),
            resp.refused,
            resp.latency_ms,
            refused_reason,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("写 query_log 失败（不影响返回）：%s", exc)


@router.post("/ask", response_model=AskResponse)
async def ask(
    req: AskRequest,
    pool: asyncpg.Pool = Depends(get_pool),
    settings: Settings = Depends(get_settings),
) -> AskResponse:
    """提问：返回带引用的结构化答案，无法回答时明确拒答。"""
    started = time.perf_counter()
    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=422, detail="问题不能为空白")

    as_of = req.as_of or date.today()

    # ---- 检索 ----
    try:
        hits = await hybrid_retrieve(
            pool, question, as_of, settings, limit=settings.top_k_final
        )
    except EmbeddingNotConfigured as exc:
        logger.error("检索不可用：%s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except EmbeddingFailed as exc:
        logger.error("检索失败：%s", exc)
        raise HTTPException(status_code=503, detail="检索服务暂不可用") from exc
    except asyncpg.PostgresError as exc:
        logger.exception("数据库查询失败")
        raise HTTPException(status_code=503, detail="数据库查询失败") from exc

    # ---- 闸 ①：什么都没检索到 ----
    if not hits:
        resp = _refuse(as_of, started, "检索结果为空")
        await _log_query(pool, question, as_of, hits, resp, "empty")
        return resp

    # ---- 闸 ②：最相关的一条都不够相关 ----
    top_sim = hits[0].vector_sim
    if top_sim is not None and top_sim < settings.refusal_threshold:
        resp = _refuse(as_of, started, f"top1 相似度 {top_sim:.3f} < {settings.refusal_threshold}")
        await _log_query(pool, question, as_of, hits, resp, "low_similarity")
        return resp

    # ---- 生成 ----
    try:
        answer = await generate(question, hits, settings)
    except LLMNotConfigured as exc:
        logger.error("生成不可用：%s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except GenerationFailed as exc:
        logger.error("生成失败：%s", exc)
        raise HTTPException(status_code=503, detail="生成服务暂不可用") from exc

    if answer.get("refused"):
        resp = _refuse(as_of, started, "模型判定资料不足")
        await _log_query(pool, question, as_of, hits, resp, "model_refused")
        return resp

    # ---- 闸 ③④：引用校验 + 带反馈重试 ----
    ok, problems = verify(answer, hits, settings)
    if not ok:
        logger.info("首次校验失败，带问题重试：%s", problems)
        try:
            answer = await regenerate_with_feedback(question, hits, settings, problems)
            ok, problems2 = verify(answer, hits, settings)
            if not ok:
                resp = _refuse(as_of, started, f"引用校验两次失败：{problems2}")
                await _log_query(pool, question, as_of, hits, resp, "verify_failed")
                return resp
        except GenerationFailed as exc:
            logger.error("重试生成失败：%s", exc)
            resp = _refuse(as_of, started, "重试生成失败")
            await _log_query(pool, question, as_of, hits, resp, "retry_failed")
            return resp

    resp = _to_response(answer, as_of, started)
    await _log_query(pool, question, as_of, hits, resp, None)
    return resp
