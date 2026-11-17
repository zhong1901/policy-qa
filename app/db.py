"""数据库连接：asyncpg 连接池，应用启动时创建、关闭时释放。"""
import logging

import asyncpg
from fastapi import HTTPException

from app.config import get_settings

logger = logging.getLogger(__name__)

# 全局连接池单例：所有请求共享同一个池，避免每个请求都新建 TCP 连接。
_pool: asyncpg.Pool | None = None


async def create_pool() -> asyncpg.Pool:
    """创建全局连接池并赋值给 _pool，供 lifespan 调用。

    min_size=0 表示不预建连接：这样即使数据库还没起来，/health 也能正常返回，
    真正需要查库的请求再按需建连；建连失败由各 service 捕获并返回 503。
    """
    global _pool
    settings = get_settings()
    _pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=0,
        max_size=10,
        # 单条 SQL 最长 30s：防止慢查询把连接占死、把请求拖到超时
        command_timeout=30,
    )
    logger.info("数据库连接池已创建")
    return _pool


async def close_pool() -> None:
    """关闭连接池，应用退出时调用。"""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("数据库连接池已关闭")


async def get_pool() -> asyncpg.Pool:
    """FastAPI 依赖：取出全局连接池。

    未初始化时抛 503 而不是 500 —— 这是「服务还没就绪」，属于上游不可用，
    不是程序错误，客户端应该稍后重试。
    """
    if _pool is None:
        logger.error("连接池尚未初始化，lifespan 可能没有执行")
        raise HTTPException(status_code=503, detail="数据库尚未就绪，请稍后重试")
    return _pool
