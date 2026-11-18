# -*- coding: utf-8 -*-
"""使用情况统计 v2：能看访客、访问量、来源，也能看谁问了什么。

用法：
    python scripts/usage_stats.py            # 全部
    python scripts/usage_stats.py --days 7   # 只看最近 7 天
"""
import argparse
import asyncio
import os
import sys

import asyncpg
from dotenv import load_dotenv

load_dotenv(r"E:\pycharm项目\上海应届生\.env")


def bar(n: int, mx: int, width: int = 24) -> str:
    if mx <= 0:
        return ""
    return "█" * max(1, round(n / mx * width))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    args = ap.parse_args()

    conn = await asyncpg.connect(os.getenv("DATABASE_URL"), ssl="require")
    try:
        print("=" * 62)
        print(f"访问（最近 {args.days} 天）")
        print("=" * 62)
        row = await conn.fetchrow("""
            SELECT count(*) AS pv,
                   count(DISTINCT visitor_hash) AS uv,
                   count(*) FILTER (WHERE mobile) AS mobile
              FROM visits WHERE visited_at > now() - ($1 || ' days')::interval""",
                                  str(args.days))
        print(f"  页面浏览 PV       {row['pv']}")
        print(f"  独立访客 UV       {row['uv']}")
        print(f"  其中移动端        {row['mobile']}")

        print("\n  按天：")
        rows = await conn.fetch("""
            SELECT visited_at::date AS d, count(*) AS pv,
                   count(DISTINCT visitor_hash) AS uv
              FROM visits WHERE visited_at > now() - ($1 || ' days')::interval
             GROUP BY 1 ORDER BY 1 DESC""", str(args.days))
        mx = max((r["pv"] for r in rows), default=0)
        for r in rows:
            print(f"    {str(r['d']):<12} PV {r['pv']:>3}  UV {r['uv']:>3}  {bar(r['pv'], mx)}")

        print("\n  来源：")
        rows = await conn.fetch("""
            SELECT coalesce(nullif(referer_host, ''), '（直接打开）') AS src,
                   count(*) AS n
              FROM visits WHERE visited_at > now() - ($1 || ' days')::interval
             GROUP BY 1 ORDER BY n DESC LIMIT 8""", str(args.days))
        for r in rows:
            print(f"    {r['n']:>4}  {r['src'][:44]}")

        print("\n" + "=" * 62)
        print("提问（最近 {0} 天）".format(args.days))
        print("=" * 62)
        row = await conn.fetchrow("""
            SELECT count(*) AS n,
                   count(DISTINCT visitor_hash) AS visitors,
                   count(DISTINCT session_id) AS sessions,
                   count(*) FILTER (WHERE refused) AS refused,
                   round(avg(latency_ms)) AS avg_ms
              FROM query_log
             WHERE created_at > now() - ($1 || ' days')::interval""", str(args.days))
        print(f"  提问次数          {row['n']}")
        print(f"  提问的访客        {row['visitors']}")
        print(f"  会话数            {row['sessions']}")
        print(f"  拒答              {row['refused']}")
        print(f"  平均耗时          {int(row['avg_ms'] or 0)} ms")

        print("\n  每个访客问了什么：")
        rows = await conn.fetch("""
            SELECT coalesce(visitor_hash, '(未记录)') AS vh,
                   count(*) AS n,
                   min(created_at) AS first_seen,
                   max(created_at) AS last_seen
              FROM query_log
             WHERE created_at > now() - ($1 || ' days')::interval
             GROUP BY 1 ORDER BY n DESC LIMIT 10""", str(args.days))
        for r in rows:
            span = f"{r['first_seen']:%m-%d %H:%M} → {r['last_seen']:%m-%d %H:%M}"
            print(f"    {r['vh']:<14} {r['n']:>4} 次   {span}")

        print("\n" + "=" * 62)
        print("最常被问的问题")
        print("=" * 62)
        rows = await conn.fetch("""
            SELECT question, count(*) n FROM query_log
             WHERE created_at > now() - ($1 || ' days')::interval
             GROUP BY 1 ORDER BY n DESC LIMIT 12""", str(args.days))
        for r in rows:
            print(f"  {r['n']:>3} 次  {r['question'][:46]}")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
