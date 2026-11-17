"""搜索接口：GET /api/search，走混合检索。"""
import logging
from datetime import date

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query

from app.config import Settings, get_settings
from app.db import get_pool
from app.schemas import SearchItem
from app.services.embed import EmbeddingFailed, EmbeddingNotConfigured
from app.services.retrieve import hybrid_retrieve

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["search"])


@router.get("/search", response_model=list[SearchItem])
async def search(
    q: str = Query(..., min_length=1, max_length=200, description="查询内容"),
    as_of: date | None = Query(None, description="时间点，默认今天"),
    limit: int = Query(10, ge=1, le=50, description="返回条数"),
    pool: asyncpg.Pool = Depends(get_pool),
    settings: Settings = Depends(get_settings),
) -> list[SearchItem]:
    """关键词搜索：返回按相关度降序的条款列表。"""
    # Query 的 min_length 在 strip 之前生效，"   " 能通过校验，所以这里再挡一次
    q = q.strip()
    if not q:
        raise HTTPException(status_code=422, detail="查询内容不能为空白")

    as_of = as_of or date.today()

    try:
        hits = await hybrid_retrieve(pool, q, as_of, settings, limit=limit)
    except EmbeddingNotConfigured as exc:
        # 配置缺失：直接告诉调用方缺什么，别让运维去翻日志猜
        logger.error("检索不可用：%s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except EmbeddingFailed as exc:
        logger.error("检索失败：%s", exc)
        raise HTTPException(
            status_code=503, detail="检索服务暂不可用，请稍后重试"
        ) from exc
    except asyncpg.PostgresError as exc:
        logger.exception("数据库查询失败 q=%r", q)
        raise HTTPException(
            status_code=503, detail="数据库查询失败，请稍后重试"
        ) from exc
    # 其余异常故意不捕获：KeyError/TypeError 是代码 bug，应该以 500 暴露，
    # 包装成"服务不可用"只会让 bug 藏得更深。

    return [
        SearchItem(
            clause_id=h.clause_id,
            doc_title=h.doc_title,
            clause_no=h.clause_no,
            content=h.content,
            score=round(h.score, 6),
            url=h.url,
        )
        for h in hits
    ]
