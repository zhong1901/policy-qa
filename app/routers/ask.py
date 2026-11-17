"""问答接口。

两个入口共用同一份流程（app/services/pipeline.py）：
  POST /api/ask         一次性返回完整答案
  POST /api/ask/stream  流式返回：先推检索到的条款，再推校验后的答案

注意：路由顺序上 /ask/stream 要写在 /ask 之前吗？不需要——FastAPI 按完整路径匹配，
/ask 不会吃掉 /ask/stream。但为了可读性，下面把流式放在前面。
"""
import json
import logging
from datetime import date

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.config import Settings, get_settings
from app.db import get_pool
from app.schemas import AskRequest, AskResponse
from app.services.pipeline import ask_events

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["ask"])


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/ask/stream")
async def ask_stream(
    req: AskRequest,
    pool: asyncpg.Pool = Depends(get_pool),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    """流式问答。SSE 事件：status / retrieval / answer / error。"""
    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=422, detail="问题不能为空白")
    as_of = req.as_of or date.today()

    async def gen():
        try:
            async for typ, data in ask_events(pool, settings, question, as_of):
                yield _sse(typ, data)
        except Exception as exc:  # noqa: BLE001
            # 流已经开始，没法再改 HTTP 状态码，只能作为 error 事件推给前端
            logger.exception("流式处理异常")
            yield _sse("error", {"detail": f"{type(exc).__name__}: {exc}"[:200]})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 告诉 nginx 别缓冲，否则事件会被攒到最后一次性发出
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/ask", response_model=AskResponse)
async def ask(
    req: AskRequest,
    pool: asyncpg.Pool = Depends(get_pool),
    settings: Settings = Depends(get_settings),
) -> AskResponse:
    """一次性问答：返回带引用的结构化答案，无法回答时明确拒答。"""
    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=422, detail="问题不能为空白")
    as_of = req.as_of or date.today()

    async for typ, data in ask_events(pool, settings, question, as_of):
        if typ == "answer":
            return AskResponse(**data)
        if typ == "error":
            raise HTTPException(
                status_code=data.get("status", 503),
                detail=data.get("detail", "服务暂不可用"),
            )
        # status / retrieval 事件在非流式路径直接丢弃
    raise HTTPException(status_code=500, detail="流程异常结束")
