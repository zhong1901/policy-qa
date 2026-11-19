# -*- coding: utf-8 -*-
"""从 backup_db.py 产出的 zip 恢复数据。

**这个脚本默认只检查、不写库。** 真导入必须显式加 --yes。

为什么这么设计：
恢复脚本一旦写错，会把生产库覆盖成空。所以默认行为必须是「安全的那一边」——
先让你看清楚要往哪张表写多少行、目标表现在有多少行，再决定要不要动手。

三步用法：

    # 1. 先看要恢复什么（不写库）
    python scripts/restore_db.py --zip 备份.zip

    # 2. 真导入（目标表有数据会被跳过）
    python scripts/restore_db.py --zip 备份.zip --yes

    # 3. 强制覆盖目标表已有数据（危险，会先 DELETE）
    python scripts/restore_db.py --zip 备份.zip --yes --force

恢复后如果 clauses 缺向量，跑：
    python scripts/embed_backfill.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import zipfile
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# 带自增主键的表，导入后要重设序列，否则后续插入会撞主键
SERIAL_TABLES = {"qa_eval", "sessions", "turns", "change_log", "crawl_runs",
                 "visits", "documents", "clauses", "eval_baselines",
                 "query_log", "answer_cache"}


def connect_kwargs(dsn: str) -> dict:
    kw = {"dsn": dsn}
    if "supabase" in dsn or "sslmode=require" in dsn:
        kw["ssl"] = "require"
    return kw


async def column_casts(conn, table: str) -> dict[str, str]:
    """查目标表的列类型，决定每个参数要不要加显式类型转换。

    jsonl 里的日期是 ISO 字符串，直接塞进 date 列会报
    「'str' object has no attribute 'toordinal'」。
    """
    rows = await conn.fetch("""
        SELECT column_name, data_type
          FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = $1""", table)
    casts: dict[str, str] = {}
    for r in rows:
        dt = r["data_type"]
        if dt == "date":
            casts[r["column_name"]] = "::text::date"
        elif dt == "timestamp with time zone":
            casts[r["column_name"]] = "::text::timestamptz"
        elif dt == "timestamp without time zone":
            casts[r["column_name"]] = "::text::timestamp"
        elif dt == "jsonb":
            casts[r["column_name"]] = "::text::jsonb"
    return casts


async def main() -> int:
    ap = argparse.ArgumentParser(description="从备份恢复数据（默认只检查）")
    ap.add_argument("--zip", required=True, help="backup_db.py 产出的 zip")
    ap.add_argument("--yes", action="store_true", help="真的写入数据库")
    ap.add_argument("--force", action="store_true",
                    help="目标表已有数据时先清空再导入（危险）")
    ap.add_argument("--tables", help="只恢复这些表，逗号分隔")
    args = ap.parse_args()

    zpath = Path(args.zip)
    if not zpath.exists():
        print(f"找不到备份文件：{zpath}")
        return 2

    only = {t.strip() for t in args.tables.split(",")} if args.tables else None

    with zipfile.ZipFile(zpath) as z:
        names = set(z.namelist())
        if "manifest.json" not in names:
            print("这个 zip 里没有 manifest.json，不像 backup_db.py 的产物")
            return 2
        manifest = json.loads(z.read("manifest.json"))
        tables = {t: v for t, v in manifest["tables"].items()
                  if only is None or t in only}

    print(f"备份文件：{zpath.name}（{zpath.stat().st_size/1024/1024:.1f} MB）")
    print(f"备份时间：{manifest.get('created_at')}")
    print(f"备注：{manifest.get('note') or '—'}\n")

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("缺少 DATABASE_URL")
        return 2
    conn = await asyncpg.connect(**connect_kwargs(dsn))

    plan: list[tuple[str, int, int]] = []   # (表, 备份行数, 目标当前行数)
    try:
        print(f"{'表':<20}{'备份':>8}{'目标现有':>10}   处理")
        print("-" * 56)
        for t, meta in tables.items():
            exists = await conn.fetchval(
                "SELECT to_regclass($1) IS NOT NULL", f"public.{t}")
            if not exists:
                print(f"{t:<20}{meta['rows']:>8}{'表不存在':>10}   跳过")
                continue
            cur = await conn.fetchval(f"SELECT count(*) FROM {t}")
            if cur > 0 and not args.force:
                action = "跳过（已有数据，加 --force 才覆盖）"
            elif cur > 0:
                action = f"清空 {cur} 行后导入"
            else:
                action = "导入"
            plan.append((t, meta["rows"], cur))
            print(f"{t:<20}{meta['rows']:>8}{cur:>10}   {action}")

        total = sum(rows for _, rows, _ in plan)
        print("-" * 56)
        print(f"计划导入 {len(plan)} 张表、{total} 行\n")

        if not args.yes:
            print("这是检查模式，没有写任何数据。")
            print("确认无误后，加 --yes 再执行一次：")
            print(f"  python scripts/restore_db.py --zip \"{zpath}\" --yes")
            return 0

        # ── 真正导入 ──
        print("开始导入…\n")
        with zipfile.ZipFile(zpath) as z:
            for t, expect_rows, cur in plan:
                if cur > 0 and not args.force:
                    print(f"  跳过 {t}（已有 {cur} 行）")
                    continue
                fname = f"{t}.jsonl"
                if fname not in names:
                    print(f"  跳过 {t}（zip 里没有 {fname}）")
                    continue

                lines = z.read(fname).decode("utf-8").splitlines()
                if not lines:
                    print(f"  跳过 {t}（文件为空）")
                    continue
                rows = [json.loads(ln) for ln in lines if ln.strip()]
                cols = list(rows[0].keys())

                casts = await column_casts(conn, t)
                jsonb_cols = {c for c, cast in casts.items() if cast == "::text::jsonb"}
                placeholders = ", ".join(
                    f"${i+1}{casts.get(c, '')}" for i, c in enumerate(cols))
                col_list = ", ".join(cols)
                sql = f"INSERT INTO {t} ({col_list}) VALUES ({placeholders})"

                async with conn.transaction():
                    if cur > 0 and args.force:
                        await conn.execute(f"DELETE FROM {t}")
                    for r in rows:
                        vals = []
                        for c in cols:
                            v = r.get(c)
                            # jsonb 列：导出的 JSON 是对象/数组，必须先转回字符串
                            if c in jsonb_cols and v is not None and not isinstance(v, str):
                                v = json.dumps(v, ensure_ascii=False)
                            vals.append(v)
                        await conn.execute(sql, *vals)
                    # 重设自增序列，否则后续插入会撞主键
                    if t in SERIAL_TABLES and "id" in cols:
                        seq = await conn.fetchval(
                            "SELECT pg_get_serial_sequence($1, 'id')", f"public.{t}")
                        if seq:
                            await conn.execute(
                                f"SELECT setval($1, "
                                f"(SELECT coalesce(max(id), 1) FROM {t}))", seq)

                now = await conn.fetchval(f"SELECT count(*) FROM {t}")
                ok = "✓" if now >= expect_rows else "⚠"
                print(f"  {ok} {t:<20} 导入 {len(rows):>5} 行，现有 {now} 行")

        print("\n" + "=" * 56)
        print("检查结果：")
        warn = False
        for t, expect_rows, _ in plan:
            now = await conn.fetchval(f"SELECT count(*) FROM {t}")
            if now < expect_rows:
                print(f"  ⚠ {t} 期望 {expect_rows} 行，实际 {now} 行")
                warn = True
        if not warn:
            print("  ✓ 所有表行数符合预期")

        clauses_now = await conn.fetchval(
            "SELECT count(*) FROM clauses WHERE embedding IS NULL") \
            if await conn.fetchval("SELECT to_regclass('public.clauses') IS NOT NULL") else 0
        if clauses_now:
            print(f"\n注意：{clauses_now} 条条款没有向量，跑这个补：")
            print("  python scripts/embed_backfill.py")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
