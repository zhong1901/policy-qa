"""会话与记忆接口。

- GET    /api/session/{sid}/profile   读取长期画像
- POST   /api/session/{sid}/profile   保存长期画像（用户在表单里主动确认的）
- DELETE /api/session/{sid}           一键清空该会话的全部记忆
- GET    /api/session/{sid}           看这个会话记了多少轮

设计说明：画像是「用户主动填写并保存」的，不是模型从对话里抽取的。
这样既不收集用户没说过的信息，也避免错画像污染后续回答。
"""
import logging

import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from app.db import get_pool
from app.schemas import EligibilityRequest
from app.services import memory

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/session", tags=["session"])


@router.get("/{session_id}")
async def get_session(
    session_id: str,
    pool: asyncpg.Pool = Depends(get_pool),
) -> dict:
    """看这个会话记住了多少东西。"""
    if len(session_id) > 64:
        raise HTTPException(status_code=422, detail="session_id 过长")
    try:
        turns = await pool.fetchval(
            "SELECT count(*) FROM turns WHERE session_id = $1", session_id)
        has_profile = await pool.fetchval(
            "SELECT EXISTS(SELECT 1 FROM user_profiles WHERE session_id = $1)",
            session_id)
    except asyncpg.PostgresError as exc:
        logger.exception("查询会话失败")
        raise HTTPException(status_code=503, detail="查询会话失败") from exc
    return {"session_id": session_id, "turns": turns, "has_profile": bool(has_profile)}


@router.get("/{session_id}/profile")
async def get_profile(
    session_id: str,
    pool: asyncpg.Pool = Depends(get_pool),
) -> dict:
    profile = await memory.get_profile(pool, session_id)
    return {"session_id": session_id, "profile": profile}


@router.post("/{session_id}/profile")
async def save_profile(
    session_id: str,
    req: EligibilityRequest,
    pool: asyncpg.Pool = Depends(get_pool),
) -> dict:
    """保存画像。整条覆盖，不合并——避免新旧信息互相矛盾。"""
    if len(session_id) > 64:
        raise HTTPException(status_code=422, detail="session_id 过长")
    await memory.ensure_session(pool, session_id)
    await memory.save_profile(pool, session_id, req.model_dump())
    logger.info("已保存画像：session=%s", session_id[:8])
    return {"ok": True, "profile": req.model_dump()}


@router.delete("/{session_id}")
async def clear_session(
    session_id: str,
    pool: asyncpg.Pool = Depends(get_pool),
) -> dict:
    """清空记忆。用户应该有能力一键清掉自己留下的东西。"""
    await memory.clear_session(pool, session_id)
    logger.info("已清空会话：session=%s", session_id[:8])
    return {"ok": True}
