# -*- coding: utf-8 -*-
"""心跳检查：确认数据库活着，顺便看容量和行数。

用途：
1. Supabase 免费项目一周不活跃会被暂停，每周跑一次这个就能保活；
2. 演示前跑一次，确认库没被暂停、数据还在。

用法：
    python scripts/keepalive.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from urllib.parse import urlparse

try:
    import asyncpg
except ImportError:
    sys.exit("缺少 asyncpg，请先 pip install -r requirements.txt")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


async def main() -> None:
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        sys.exit("没有 DATABASE_URL，请检查 .env")

    host = (urlparse(dsn).hostname or "").lower()
    kwargs = {"timeout": 20, "command_timeout": 20}
    if host not in ("localhost", "127.0.0.1", "::1", ""):
        kwargs["ssl"] = "require"

    try:
        conn = await asyncpg.connect(dsn, **kwargs)
    except Exception as exc:
        print("连接失败:", type(exc).__name__, str(exc)[:200])
        print()
        print("如果是 'tenant/user not found' 或超时，说明项目被暂停了：")
        print("  打开 supabase.com/dashboard -> 找到项目卡片上的 Paused 标签")
        print("  -> 点进去，顶部点 Restore project -> 等 1~2 分钟再试")
        sys.exit(1)

    try:
        ver = await conn.fetchval("show server_version")
        size_mb = (await conn.fetchval(
            "SELECT pg_database_size(current_database())")) / 1024 / 1024
        docs = await conn.fetchval("SELECT count(*) FROM documents")
        cls = await conn.fetchval("SELECT count(*) FROM clauses")
        emb = await conn.fetchval(
            "SELECT count(*) FROM clauses WHERE embedding IS NOT NULL")
        print("数据库活着 [OK]")
        print(f"  版本      : PostgreSQL {ver}")
        print(f"  占用空间  : {size_mb:.1f} MB / 500 MB (免费额度)")
        print(f"  documents : {docs}")
        print(f"  clauses   : {cls}")
        print(f"  已向量化  : {emb}")
        if emb == cls and cls:
            print("  状态      : 可以正常检索")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
