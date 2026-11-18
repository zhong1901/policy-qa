"""访问埋点：记录"谁来过、问了什么"，但不存个人信息。

为什么不接第三方统计：
数据留在别人手里，而且注册账号得你本人来。
我们已经有数据库和接口，自己记更直接——还能把"访问"和"提问"关联起来。

隐私边界（重要）：
- **不存 IP 明文，不存 UserAgent 明文**，只存它们拼起来的哈希前 12 位。
  这个哈希只能用来"区分是不是同一个人"，反推不出是谁。
- 不做跨站追踪，不用 cookie 做画像。
- 用户点「新对话」会清空会话，但访问记录保留（匿名，无法关联到人）。
"""
from __future__ import annotations

import hashlib

from fastapi import Request


def _client_ip(request: Request) -> str:
    """取真实客户端 IP。

    Render 在反向代理后面，request.client.host 拿到的是代理地址，
    必须读 X-Forwarded-For 的第一个值。
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else ""


def visitor_hash(request: Request) -> str:
    """访客指纹：IP + UA 的哈希前 12 位。

    同一个人同一天换浏览器会被算成两个访客——这是可接受的，
    因为我们本来就不想做精确追踪，只要"大概有几个人"。
    """
    raw = f"{_client_ip(request)}|{request.headers.get('user-agent', '')}"
    return hashlib.md5(raw.encode("utf-8", "ignore")).hexdigest()[:12]


def is_mobile(request: Request) -> bool:
    ua = (request.headers.get("user-agent") or "").lower()
    return any(k in ua for k in ("mobile", "android", "iphone", "ipad"))


def referer_host(request: Request) -> str:
    """来源域名。只看域名，不看完整 URL（完整 URL 可能带搜索词）。"""
    ref = request.headers.get("referer") or ""
    if "://" not in ref:
        return ""
    host = ref.split("://", 1)[1].split("/", 1)[0]
    return host[:80]


DDL = """
CREATE TABLE IF NOT EXISTS visits (
    id           BIGSERIAL PRIMARY KEY,
    visited_at   TIMESTAMPTZ DEFAULT now(),
    path         TEXT,
    visitor_hash TEXT,
    referer_host TEXT,
    mobile       BOOLEAN DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_visits_time ON visits (visited_at DESC);
CREATE INDEX IF NOT EXISTS idx_visits_visitor ON visits (visitor_hash);

ALTER TABLE query_log ADD COLUMN IF NOT EXISTS session_id TEXT;
ALTER TABLE query_log ADD COLUMN IF NOT EXISTS visitor_hash TEXT;
CREATE INDEX IF NOT EXISTS idx_qlog_visitor ON query_log (visitor_hash);
"""
