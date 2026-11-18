"""访问埋点接口。前端每次打开页面调一次。

只记匿名信息（访客哈希、页面、来源域名、是否移动端），不记 IP 和 UA 明文。
"""
import logging

import asyncpg
from fastapi import APIRouter, Depends, Request

from app.db import get_pool
from app.services.tracking import is_mobile, referer_host, visitor_hash

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["track"])


@router.post("/visit")
async def record_visit(
    request: Request,
    pool: asyncpg.Pool = Depends(get_pool),
) -> dict:
    """记一次页面访问。失败也不影响用户，所以异常一律吞掉。"""
    try:
        await pool.execute(
            """INSERT INTO visits (path, visitor_hash, referer_host, mobile)
               VALUES ($1, $2, $3, $4)""",
            "check" if "check" in str(request.headers.get("referer", "")) else "index",
            visitor_hash(request),
            referer_host(request),
            is_mobile(request),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("写访问记录失败（不影响使用）：%s", exc)
    return {"ok": True}
