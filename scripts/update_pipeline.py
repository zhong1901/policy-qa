"""政策更新流水线：检测源站变化 → 记录变更 → 跑回归 → 出报告。

这是给「数据会变」这件事准备的。没有它，知识库产品只能靠人工重新灌一遍然后祈祷。

用法：
    # 演练：把库里现状当源站清单，应该 0 变更
    python scripts/update_pipeline.py --from-db

    # 模拟 3 条变更，验证检测是否有效（不改库，只演示）
    python scripts/update_pipeline.py --simulate 3

    # 真实使用：传入源站清单（由爬虫产出）
    python scripts/update_pipeline.py --incoming crawl_result.json

    # 检测完顺手跑回归，和上一次基线比
    python scripts/update_pipeline.py --incoming crawl_result.json --regress

源站清单格式（JSON 数组）：
    [{"url": "https://...", "title": "...", "content_hash": "md5..."}]
如果没有 content_hash，就退化成「只比对 URL 集合」，检测不到内容变更。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
from datetime import datetime
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.changes import (  # noqa: E402
    CHANGED, GONE, NEW, IncomingDoc, diff, load_current, summarize_diff,
)

load_dotenv(ROOT / ".env")

CHANGE_KIND = {"new": NEW, "changed": CHANGED, "gone": GONE}


def connect_kwargs(dsn: str) -> dict:
    kw = {"dsn": dsn}
    if "supabase" in dsn or "sslmode=require" in dsn:
        kw["ssl"] = "require"
    return kw


def build_incoming_from_db(current: list[dict]) -> list[IncomingDoc]:
    """拿库内现状当源站清单。用于验证「没有变化时不会误报」。"""
    return [IncomingDoc(url=d["source_url"], title=d.get("title"),
                        content_hash=d["content_hash"]) for d in current]


def simulate(current: list[dict], n: int) -> list[IncomingDoc]:
    """模拟 n 条变更：改哈希 + 下线 2 条 + 新增 2 条。

    必须以**全部**文档为基础构造源站清单——只取一个子集当源站清单，
    剩下的会被正确判为「消失」，那是模拟器的 bug 不是检测器的 bug。
    """
    rnd = random.Random(42)
    idx = set(rnd.sample(range(len(current)), min(n, len(current))))
    out: list[IncomingDoc] = []
    for i, d in enumerate(current):
        if i in idx:
            out.append(IncomingDoc(url=d["source_url"], title=d.get("title"),
                                   content_hash="simulated-change-" + str(i)))
        else:
            out.append(IncomingDoc(url=d["source_url"], title=d.get("title"),
                                   content_hash=d["content_hash"]))
    # 模拟下线两条
    drop = {d["source_url"] for d in current[:2]}
    out = [x for x in out if x.url not in drop]
    # 模拟新增两条
    out.append(IncomingDoc(url="https://example.invalid/new-1.pdf",
                           title="模拟新增文件 1", content_hash="h1"))
    out.append(IncomingDoc(url="https://example.invalid/new-2.html",
                           title="模拟新增文件 2", content_hash="h2"))
    return out


async def write_change_log(conn: asyncpg.Connection, res) -> int:
    rows = []
    for d in res.new:
        rows.append((NEW, d.get("url"), d.get("title"), None,
                     json.dumps({"content_hash": d.get("content_hash")}, ensure_ascii=False)))
    for d in res.changed:
        rows.append((CHANGED, d.get("url"), d.get("title"), d.get("doc_key"),
                     json.dumps({"old_hash": d.get("old_hash"),
                                 "new_hash": d.get("new_hash"),
                                 "old_len": d.get("old_len")}, ensure_ascii=False)))
    for d in res.gone:
        rows.append((GONE, d.get("url"), d.get("title"), d.get("doc_key"), None))
    if not rows:
        return 0
    await conn.executemany(
        """INSERT INTO change_log (kind, url, title, doc_key, detail)
           VALUES ($1, $2, $3, $4, $5::jsonb)""", rows)
    return len(rows)


async def main() -> int:
    ap = argparse.ArgumentParser(description="政策更新流水线")
    ap.add_argument("--incoming", help="源站清单 JSON 文件")
    ap.add_argument("--from-db", action="store_true", help="用库内现状当源站清单（演练）")
    ap.add_argument("--simulate", type=int, metavar="N", help="模拟 N 条变更（演练）")
    ap.add_argument("--regress", action="store_true", help="检测后跑回归")
    ap.add_argument("--no-write", action="store_true", help="不写 change_log（只演练）")
    args = ap.parse_args()

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("缺少 DATABASE_URL")
        return 2

    conn = await asyncpg.connect(**connect_kwargs(dsn))
    try:
        current = await load_current(conn)
        print(f"库内文档：{len(current)} 条\n")

        if args.incoming:
            raw = json.loads(Path(args.incoming).read_text(encoding="utf-8"))
            incoming = [IncomingDoc(url=r.get("url", ""), title=r.get("title"),
                                    content_hash=r.get("content_hash", "")) for r in raw]
            print(f"源站清单：{len(incoming)} 条（来自 {args.incoming}）")
        elif args.simulate is not None:
            incoming = simulate(current, args.simulate)
            print(f"源站清单：{len(incoming)} 条（模拟，改 {args.simulate} 条 + 下线 2 条 + 新增 2 条）")
        elif args.from_db:
            incoming = build_incoming_from_db(current)
            print(f"源站清单：{len(incoming)} 条（取自库内现状）")
        else:
            ap.print_help()
            return 2

        res = diff(current, incoming)
        print("\n" + "=" * 58)
        print("检测结果：" + summarize_diff(res))
        print("=" * 58)

        if res.new:
            print(f"\n新增（前 5 / 共 {len(res.new)}）：")
            for d in res.new[:5]:
                print(f"  + {d.get('title') or '(无标题)'}  {d.get('url')}")
        if res.changed:
            print(f"\n内容变更（前 5 / 共 {len(res.changed)}）：")
            for d in res.changed[:5]:
                print(f"  ~ {d.get('title') or '(无标题)'}  {d.get('url')}")
        if res.gone:
            print(f"\n消失（前 5 / 共 {len(res.gone)}）：")
            for d in res.gone[:5]:
                print(f"  - {d.get('title') or '(无标题)'}  {d.get('url')}")

        if res.total_changes and not args.no_write:
            n = await write_change_log(conn, res)
            print(f"\n已写入 change_log：{n} 条（reviewed=false，待人工复核）")
        elif res.is_clean:
            print("\n没有变化，无需处理。")

        if res.changed or res.new:
            print("\n下一步：重新导入受影响的文档 → 跑回归确认没把已答对的题搞坏。")
    finally:
        await conn.close()

    if args.regress:
        print("\n" + "=" * 58)
        print("开始回归对比")
        print("=" * 58)
        rc = await run_regression()
        return rc
    return 0


async def run_regression() -> int:
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "run_regression", ROOT / "scripts" / "run_regression.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return await mod.compare()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
