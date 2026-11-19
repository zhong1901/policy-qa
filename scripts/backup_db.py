# -*- coding: utf-8 -*-
"""分层备份：核心资产全量导出，语料导正文不导向量，缓存和日志不备。

为什么要分层（按 2026-11-19 实测体积）：

    clauses        158.5 MB   ← 94%，几乎全是 1024 维向量
    documents        7.0 MB
    query_log        2.1 MB
    其余 9 张表     < 1 MB    ← 真正不可再生的东西都在这

两条判断：

1. **向量是派生数据，不备份。** 它由 documents 的正文经 bge-m3 算出，
   正文在就能重算。备了只会让备份包从 10 MB 涨到 160 MB。
2. **缓存和访问日志不备份。** answer_cache 是可重建的，query_log 是流水账，
   丢了不影响系统正确性。

所以默认备份 = 全部小表 + documents + clauses 的「正文部分（不含向量）」，
压缩后大约 10–15 MB，可以随便存。

用法：
    python scripts/backup_db.py                  # 默认：小表 + 语料正文
    python scripts/backup_db.py --small-only     # 只要小表（< 1 MB）
    python scripts/backup_db.py --out D:\\backup  # 指定输出目录

恢复思路（不提供一键脚本，避免误操作）：
    ① 建表：python scripts/ensure_tables.py
    ② 用 jsonl 里导出的行，按表逐条 INSERT（或先转成 CSV 用 import_data.py）
    ③ 向量重建：python scripts/embed_backfill.py
    ④ 对照 manifest.json 里的行数校验
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
import zipfile
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# 核心表：行数少、体积小、丢了不可再生。合计不到 1 MB，几十秒能备完。
CORE_TABLES = [
    "qa_eval",          # 69 题人工标注，最值钱
    "eval_baselines",   # 历史基线
    "user_profiles",    # 用户画像
    "sessions",
    "turns",            # 对话记录
    "change_log",       # 政策变更台账
    "crawl_runs",       # 抓取台账
    "visits",           # 访问埋点
]

# 语料表：体积大但可重建（documents 重新爬、clauses 重新切）
# documents 单独放这里，因为它有 7 MB 正文，拉一遍要 5 分钟——
# 放进「小表」会让每周备份从 40 秒变成 10 分钟。
CORPUS_DOCUMENTS = "documents"

# 语料表：只导正文，不导向量
CORPUS_COLUMNS = (
    "id, doc_id, doc_key, segment, clause_no, clause_path, level, ordinal, "
    "content, content_len, effective_from, effective_to, policy_family"
)

# 明确不备份的表，以及原因
SKIPPED = {
    "answer_cache": "缓存，可重建",
    "query_log": "流水账，不影响系统正确性",
}


def connect_kwargs(dsn: str) -> dict:
    kw = {"dsn": dsn}
    if "supabase" in dsn or "sslmode=require" in dsn:
        kw["ssl"] = "require"
    return kw


def _json_default(v):
    """把 date / Decimal 之类转成可序列化。"""
    from datetime import date, datetime
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return str(v)


async def dump_table(conn, table: str, path: Path, columns: str = "*") -> tuple[int, str]:
    """导出一张表为 jsonl，返回 (行数, 内容哈希)。

    用游标流式读，不用 LIMIT/OFFSET 分页：
    documents 的正文每行几 KB，OFFSET 分页在跨境链路上会把连接拖断，
    而且越往后翻越慢（OFFSET 越大扫得越多）。
    """
    n = 0
    h = hashlib.md5()
    with open(path, "w", encoding="utf-8") as f:
        async with conn.transaction():
            async for r in conn.cursor(
                    f"SELECT {columns} FROM {table} ORDER BY 1", prefetch=100):
                line = json.dumps(dict(r), ensure_ascii=False, default=_json_default)
                f.write(line + "\n")
                h.update(line.encode("utf-8"))
                n += 1
                if n % 1000 == 0:
                    print(f"    … {table} 已导 {n} 行", flush=True)
    return n, h.hexdigest()


async def main() -> int:
    ap = argparse.ArgumentParser(description="分层备份")
    ap.add_argument("--out", default=str(ROOT / "backups"), help="输出根目录")
    ap.add_argument("--small-only", action="store_true",
                    help="只备小表，跳过语料正文")
    args = ap.parse_args()

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("缺少 DATABASE_URL")
        return 2

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out) / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = await asyncpg.connect(**connect_kwargs(dsn))
    manifest = {"created_at": stamp, "tables": {}, "skipped": SKIPPED, "note": ""}
    try:
        print(f"备份到 {out_dir}\n")
        print(f"{'表':<20}{'行数':>10}{'耗时':>8}")
        print("-" * 40)

        for t in CORE_TABLES:
            t0 = time.time()
            n, h = await dump_table(conn, t, out_dir / f"{t}.jsonl")
            manifest["tables"][t] = {"rows": n, "md5": h}
            print(f"{t:<20}{n:>10}{time.time()-t0:>7.1f}s")

        if not args.small_only:
            # 语料：只导正文，向量由 embed_backfill.py 重算
            t0 = time.time()
            n, h = await dump_table(conn, CORPUS_DOCUMENTS,
                                    out_dir / "documents.jsonl")
            manifest["tables"][CORPUS_DOCUMENTS] = {"rows": n, "md5": h}
            print(f"{CORPUS_DOCUMENTS:<20}{n:>10}{time.time()-t0:>7.1f}s")

            t0 = time.time()
            n, h = await dump_table(conn, "clauses", out_dir / "clauses.jsonl",
                                    CORPUS_COLUMNS)
            manifest["tables"]["clauses"] = {
                "rows": n, "md5": h,
                "columns": CORPUS_COLUMNS,
                "note": "不含 embedding 与 tsv，恢复后用 scripts/embed_backfill.py 重建",
            }
            print(f"{'clauses(无向量)':<20}{n:>10}{time.time()-t0:>7.1f}s")
            manifest["note"] = "clauses 只含正文，向量需重建"
        else:
            manifest["note"] = "仅小表；语料未备"

        (out_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        # 打包
        zip_path = out_dir.with_suffix(".zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for f in out_dir.iterdir():
                z.write(f, f.name)
        size_mb = zip_path.stat().st_size / 1024 / 1024

        print("-" * 40)
        print(f"\n✓ 打包完成：{zip_path}")
        print(f"  压缩后 {size_mb:.1f} MB")
        print(f"  表数 {len(manifest['tables'])}，总行数 "
              f"{sum(v['rows'] for v in manifest['tables'].values())}")
        print("\n建议：把这个 zip 复制到网盘，或让 CI 上传成 artifact。")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
