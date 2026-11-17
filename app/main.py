"""FastAPI 入口：创建 app 实例，注册中间件、生命周期和路由。"""
import logging
import time
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.db import close_pool, create_pool
from app.routers import ask, docs, search
from app.services.embed import close_http_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时建连接池，关闭时释放连接池和 HTTP 客户端。"""
    await create_pool()
    logger.info("服务启动完成")
    try:
        yield
    finally:
        # 放在 finally 里：即使运行期间抛异常，也要把资源放掉
        await close_pool()
        await close_http_client()
        logger.info("服务已关闭")


app = FastAPI(title="政策问答助手", version="0.1.0", lifespan=lifespan)

# 本地开发时前端跑在 3000 端口，必然跨域；生产同源部署时这个配置不生效也无妨。
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_latency(request: Request, call_next):
    """记录每次请求的耗时并写入响应头。

    X-Process-Time-Ms 既方便本地 curl 看延迟，也是后面统计 P95 的原始数据来源。
    """
    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - started) * 1000
    response.headers["X-Process-Time-Ms"] = f"{elapsed_ms:.0f}"
    logger.info(
        "%s %s -> %s %.0fms",
        request.method, request.url.path, response.status_code, elapsed_ms,
    )
    return response


@app.get("/health")
async def health() -> dict:
    """健康检查：返回固定 ok，供部署探针和冒烟测试用。不查库，数据库未就绪也能通。"""
    return {"ok": True}


app.include_router(search.router)
app.include_router(ask.router)
app.include_router(docs.router)

# 静态前端挂在最后：Starlette 按注册顺序匹配，/api/* 与 /health 会先命中，
# 只有其余路径才落到静态目录。
_STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"
if _STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="web")
