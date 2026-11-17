"""抓取前置检查：robots + 数量校验。合规和防呆都在这一层。

用法：
    # 检查一批 URL 能不能抓（按 host 分组查 robots）
    python scripts/crawl_preflight.py --urls urls.txt

    # 只检查某个站点
    python scripts/crawl_preflight.py --robots https://rsj.sh.gov.cn/some/page.html

    # 数量校验：本次抓到 N 条，和上次成功比
    python scripts/crawl_preflight.py --count 1200 --source rsj.sh.gov.cn

退出码：0 = 可以继续；1 = 中止（不要写库）
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.crawl_safety import (  # noqa: E402
    check_count, check_robots, last_ok_count,
)

load_dotenv(ROOT / ".env")


def connect_kwargs(dsn: str) -> dict:
    kw = {"dsn": dsn}
    if "supabase" in dsn or "sslmode=require" in dsn:
        kw["ssl"] = "require"
    return kw


async def main() -> int:
    ap = argparse.ArgumentParser(description="抓取前置检查")
    ap.add_argument("--urls", help="URL 清单文件，每行一个")
    ap.add_argument("--robots", help="只检查这一个 URL")
    ap.add_argument("--count", type=int, help="本次抓到的条数，做数量校验")
    ap.add_argument("--source", default=None, help="来源标识（数量校验用）")
    args = ap.parse_args()

    ok = True

    # ── robots ──
    targets: list[str] = []
    if args.robots:
        targets = [args.robots]
    elif args.urls:
        p = Path(args.urls)
        if not p.exists():
            print(f"找不到清单文件：{p}")
            return 1
        # utf-8-sig：PowerShell 写出的 UTF8 带 BOM，不处理会让第一行 URL 解析失败
        targets = [ln.strip().lstrip("\ufeff") for ln in
                   p.read_text(encoding="utf-8-sig").splitlines()
                   if ln.strip() and not ln.lstrip().startswith("#")]

    if targets:
        # 每个 host 只查一次 robots，别把 robots.txt 自己刷成 DDoS
        by_host: dict[str, str] = {}
        for u in targets:
            host = u.split("/")[2] if "://" in u else u
            by_host.setdefault(host, u)
        print(f"检查 {len(by_host)} 个站点的 robots.txt（共 {len(targets)} 条 URL）\n")
        for host, sample in by_host.items():
            v = await check_robots(sample)
            mark = "✓" if v.allowed else "✗"
            extra = f"  crawl-delay={v.crawl_delay:g}s" if v.crawl_delay else ""
            print(f"  {mark} {v.host}  {v.reason}{extra}")
            if not v.allowed:
                ok = False
                print(f"     禁止示例路径：{sample}")

    # ── 数量校验 ──
    if args.count is not None:
        dsn = os.getenv("DATABASE_URL")
        conn = await asyncpg.connect(**connect_kwargs(dsn))
        try:
            pool = conn
            last = await last_ok_count(pool, args.source)
        finally:
            await conn.close()
        r = check_count(args.count, last)
        print(f"\n数量校验：{'✓' if r.ok else '✗'} {r.reason}")
        if not r.ok:
            ok = False

    if not targets and args.count is None:
        ap.print_help()
        return 2

    print()
    if ok:
        print("结论：可以继续抓取。")
        return 0
    print("结论：中止。这次不写库。")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
