"""访问口令：给公网部署加一道最简单的门。

为什么需要：服务一旦公网可访问，任何人拿到链接都能提问，每次都在烧
DeepSeek 和 embedding 的额度。内测版应该要口令。

做法：
- 配置了 ACCESS_CODE 才启用；不配就是公开访问（本地开发保持无感）
- 通过 /auth 提交口令，种一个 7 天的 HttpOnly cookie
- 未通过的请求：API 返回 401 JSON，页面返回一个极简口令页
"""
import hashlib
import hmac
import logging
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

COOKIE_NAME = "pq_access"
COOKIE_MAX_AGE = 7 * 24 * 3600

# 不校验口令的路径：健康检查要能被 Render 探针访问，/auth 是入口
PUBLIC_PATHS = {"/health", "/auth"}


def _sign(code: str) -> str:
    """把口令变成 cookie 值，避免明文落在浏览器里。"""
    return hmac.new(b"policy-qa-access", code.encode("utf-8"), hashlib.sha256).hexdigest()


LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>需要访问口令</title>
<style>
  :root{--paper:#faf8f5;--ink:#1a1a18;--ink2:#57534e;--line:#e6e1d8;--accent:#1f5f57;--soft:#ecf3f1}
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--paper);
       color:var(--ink);font-family:system-ui,-apple-system,"Segoe UI","PingFang SC",sans-serif}
  .box{width:min(380px,90vw);background:#fff;border:1px solid var(--line);border-radius:12px;padding:30px 28px}
  h1{font-family:"Noto Serif SC","Songti SC",serif;font-size:20px;margin:0 0 8px}
  p{margin:0 0 20px;color:var(--ink2);font-size:13.5px;line-height:1.7}
  input{width:100%;padding:11px 13px;border:1px solid var(--line);border-radius:8px;
        font-size:15px;outline:0;font-family:inherit}
  input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--soft)}
  button{width:100%;margin-top:12px;padding:11px;border:0;border-radius:8px;background:var(--accent);
         color:#fff;font-size:14.5px;font-weight:600;cursor:pointer;font-family:inherit}
  .err{color:#a33;font-size:13px;margin-top:10px;min-height:18px}
</style></head>
<body>
  <form class="box" method="post" action="/auth">
    <h1>需要访问口令</h1>
    <p>这是一个个人作品演示，输入口令后继续。口令有效期 7 天。</p>
    <input name="code" type="password" placeholder="请输入口令" autofocus autocomplete="off">
    <button type="submit">进入</button>
    <div class="err">__ERR__</div>
  </form>
</body></html>"""


@router.post("/auth")
async def auth(request: Request):
    """校验口令，通过则种 cookie 并跳回首页。"""
    # 手动解析表单：FastAPI 的 request.form() 依赖 python-multipart，
    # 而这里只有一个字段，不值得为它多装一个包。
    body = await request.body()
    form = parse_qs(body.decode("utf-8", "ignore"))
    code = (form.get("code") or [""])[0]
    settings = get_settings()

    if not settings.access_code:
        return RedirectResponse("/", status_code=303)

    if not hmac.compare_digest(code, settings.access_code):
        logger.warning("口令错误")
        return HTMLResponse(LOGIN_HTML.replace("__ERR__", "口令不对，请重新输入"),
                            status_code=401)

    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        COOKIE_NAME,
        _sign(settings.access_code),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return resp


async def access_guard(request: Request, call_next):
    """中间件：未通过口令的请求挡在门外。"""
    settings = get_settings()
    if not settings.access_code:
        return await call_next(request)

    path = request.url.path
    if path in PUBLIC_PATHS:
        return await call_next(request)

    if hmac.compare_digest(request.cookies.get(COOKIE_NAME, ""), _sign(settings.access_code)):
        return await call_next(request)

    if path.startswith("/api/"):
        return JSONResponse({"detail": "需要访问口令"}, status_code=401)
    return HTMLResponse(LOGIN_HTML.replace("__ERR__", ""), status_code=401)
