"""两个纯 ASGI 中间件。

为什么不用 @app.middleware("http")：
FastAPI 的装饰器写法基于 Starlette 的 BaseHTTPMiddleware，它会把整个响应体
包一层（内部用队列转发），流式响应（SSE）会被攒住——实测第一个事件延迟 8.4 秒
才到浏览器。改成纯 ASGI 中间件后，一个字都不缓冲，事件按 yield 顺序立即发出。
"""
import logging
import time
from http.cookies import SimpleCookie

from fastapi.responses import HTMLResponse, JSONResponse
from starlette.datastructures import Headers, MutableHeaders

from app.config import get_settings

logger = logging.getLogger(__name__)

COOKIE_NAME = "pq_access"
PUBLIC_PATHS = {"/health", "/auth", "/stats"}


class TimingMiddleware:
    """记录耗时并写入 X-Process-Time-Ms；纯 ASGI，不缓冲。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        started = time.perf_counter()

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                elapsed = (time.perf_counter() - started) * 1000
                headers = MutableHeaders(scope=message)
                headers.append("X-Process-Time-Ms", f"{elapsed:.0f}")
                logger.info("%s %s -> %s %s%.0fms",
                            scope.get("method"), scope.get("path"),
                            message.get("status"), "", elapsed)
            await send(message)

        await self.app(scope, receive, send_wrapper)


class AccessControlMiddleware:
    """访问口令守卫。未配置 ACCESS_CODE 时完全放行。"""

    def __init__(self, app, login_html: str = "", sign=None):
        self.app = app
        self.login_html = login_html
        self.sign = sign

    def _token_from_scope(self, scope) -> str:
        raw = Headers(scope=scope).get("cookie", "")
        if not raw:
            return ""
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except Exception:  # noqa: BLE001
            return ""
        morsel = jar.get(COOKIE_NAME)
        return morsel.value if morsel else ""

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        settings = get_settings()
        if not settings.access_code:
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        if path in PUBLIC_PATHS:
            return await self.app(scope, receive, send)

        import hmac
        if self.sign and hmac.compare_digest(self._token_from_scope(scope),
                                             self.sign(settings.access_code)):
            return await self.app(scope, receive, send)

        if path.startswith("/api/"):
            response = JSONResponse({"detail": "需要访问口令"}, status_code=401)
        else:
            response = HTMLResponse((self.login_html or "").replace("__ERR__", ""),
                                    status_code=401)
        await response(scope, receive, send)
