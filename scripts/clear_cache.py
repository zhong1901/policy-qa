# -*- coding: utf-8 -*-
r"""清空答案缓存。

什么时候要清：
- 改了生成 prompt、检索逻辑或校验规则（也可以把 app/cache.py 里的 CACHE_VERSION +1，
  那样更精确，只让旧版本失效，不用全清）
- 补了新数据，希望旧问题重新检索

用法：
    python scripts/clear_cache.py            # 全部清空
    python scripts/clear_cache.py --older 7  # 只清 7 天前的
    python scripts/clear_cache.py --stats    # 只看统计，不删
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from urllib.parse import urlparse

import asyncpg

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def connect_kwargs(dsn: str) -> dict:
    host = (urlparse(dsn).hostname or "").lower()
    kw: dict = {"command_timeout": 60}
    if host not in ("localhost", "127.0.0.1", "::1", ""):
        kw["ssl"] = "require"
    return kw


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--older", type=int, default=None, help="只清 N 天前的缓存")
    ap.add_argument("--stats", action="store_true", help="只统计，不删除")
    args = ap.parse_args()

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        sys.exit("没有 DATABASE_URL")

    conn = await asyncpg.connect(dsn, **connect_kwargs(dsn))
    try:
        total = await conn.fetchval("SELECT count(*) FROM answer_cache")
        print(f"缓存条数: {total}")

        if args.stats:
            rows = await conn.fetch(
                """SELECT question, created_at FROM answer_cache
                    ORDER BY created_at DESC LIMIT 10"""
            )
            for r in rows:
                print(f"  {r['created_at']:%Y-%m-%d %H:%M}  {r['question'][:40]}")
            return

        if args.older is not None:
            n = await conn.fetchval(
                """WITH del AS (
                     DELETE FROM answer_cache
                      WHERE created_at < now() - make_interval(days => $1::int)
                      RETURNING 1)
                   SELECT count(*) FROM del""",
                args.older,
            )
            print(f"已删除 {n} 条（{args.older} 天前）")
        else:
            await conn.execute("TRUNCATE answer_cache")
            print(f"已清空 {total} 条")

        left = await conn.fetchval("SELECT count(*) FROM answer_cache")
        print(f"剩余: {left}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
