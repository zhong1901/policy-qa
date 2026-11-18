"""FastAPI 入口：创建 app 实例，注册中间件、生命周期和路由。"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.auth import LOGIN_HTML, _sign, router as auth_router
from app.config import get_settings
from app.db import close_pool, create_pool
from app.middleware import AccessControlMiddleware, TimingMiddleware
from app.routers import ask, docs, eligibility, search, session, stats, track
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

# 注意顺序：add_middleware 是「后加的在最外层」。
# 所以先加口令守卫，再加计时器 —— 计时器才会把守卫自身也算进去。
app.add_middleware(AccessControlMiddleware, login_html=LOGIN_HTML, sign=_sign)
app.add_middleware(TimingMiddleware)


@app.get("/health")
async def health() -> dict:
    """健康检查：返回固定 ok，供部署探针和冒烟测试用。不查库，数据库未就绪也能通。"""
    return {"ok": True}


app.include_router(auth_router)
app.include_router(search.router)
app.include_router(ask.router)
app.include_router(eligibility.router)
app.include_router(session.router)
app.include_router(track.router)
app.include_router(stats.router)
app.include_router(docs.router)

# 静态前端挂在最后：Starlette 按注册顺序匹配，/api/* 与 /health 会先命中，
# 只有其余路径才落到静态目录。
_STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"
if _STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="web")
