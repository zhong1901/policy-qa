# -*- coding: utf-8 -*-
r"""把 documents_all.csv / clauses.csv 导入 Postgres（pgvector）。

为什么不直接用 psql 跑 import.sql：
1. 你没装 Postgres，就没有 psql 客户端；
2. 如果用云数据库（Supabase / Neon），\copy 是 psql 专属命令，网页 SQL 编辑器里跑不了；
3. 纯 Python 走 asyncpg，本地库和云库同一套代码，不用改。

用法：
    python scripts/import_data.py --dry-run          # 只解析 CSV，不连库
    python scripts/import_data.py                    # 正式导入
    python scripts/import_data.py --force            # 表已存在时先删再建
    python scripts/import_data.py --dsn "postgresql://..."

期望结果：documents 1301 / clauses 7690
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

try:
    import asyncpg
except ImportError:
    sys.exit("缺少 asyncpg，请先 pip install -r requirements.txt")

# 直接运行脚本时也要能读到 .env（uvicorn 不会替脚本加载）
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# 默认数据目录：项目根目录的上一级里的 policy-data
DEFAULT_DATA_DIR = Path(r"E:\deep seek\policy-data")

DOC_COLUMNS = [
    "doc_key", "section", "title", "doc_no", "publish_date", "effective_from",
    "effective_to", "category", "source_url", "body", "body_len", "mentions_repeal",
]
CLAUSE_COLUMNS = [
    "doc_id", "doc_key", "segment", "clause_no", "clause_path", "level", "ordinal",
    "content", "content_len", "effective_from", "effective_to",
]


def connect_kwargs(dsn: str, no_statement_cache: bool) -> dict:
    """根据 DSN 决定连接参数。

    云数据库（Supabase / Neon / RDS）两个坑：
    1. 必须走 SSL，否则直接被拒；
    2. 如果给的是连接池地址（端口 6543），必须关掉 prepared statement 缓存，
       否则 asyncpg 会报 "prepared statement already exists"。
    本地 localhost 两者都不需要。
    """
    host = (urlparse(dsn).hostname or "").lower()
    is_local = host in ("localhost", "127.0.0.1", "::1", "")
    kwargs: dict = {"command_timeout": 120}
    if not is_local:
        kwargs["ssl"] = "require"
    if no_statement_cache:
        kwargs["statement_cache_size"] = 0
    return kwargs


def _date(value: str):
    """CSV 里的空日期是空字符串，必须转成 None，否则 ::date 会报错。"""
    value = (value or "").strip()
    if not value:
        return None
    return date.fromisoformat(value[:10])


def _int(value, default: int = 0) -> int:
    value = (value or "").strip()
    if not value:
        return default
    return int(float(value))


DDL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
DROP TABLE IF EXISTS clauses;
DROP TABLE IF EXISTS documents;

CREATE TABLE documents (
    id              BIGSERIAL PRIMARY KEY,
    doc_key         TEXT UNIQUE NOT NULL,
    section         TEXT NOT NULL,
    title           TEXT NOT NULL,
    doc_no          TEXT,
    publish_date    DATE,
    effective_from  DATE,
    effective_to    DATE,
    status          TEXT DEFAULT 'active',
    category        TEXT,
    source_url      TEXT UNIQUE,
    body            TEXT NOT NULL,
    body_len        INT,
    mentions_repeal BOOLEAN DEFAULT FALSE,
    fetched_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE clauses (
    id              BIGSERIAL PRIMARY KEY,
    doc_id          BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    doc_key         TEXT NOT NULL,
    segment         INT DEFAULT 1,
    clause_no       TEXT,
    clause_path     TEXT,
    level           TEXT,
    ordinal         INT,
    content         TEXT NOT NULL,
    content_len     INT,
    effective_from  DATE,
    effective_to    DATE,
    embedding       vector(1024),
    tsv             tsvector,
    UNIQUE (doc_id, ordinal)
);
"""

INDEXES = """
CREATE INDEX idx_doc_pub  ON documents (publish_date DESC);
CREATE INDEX idx_doc_sec  ON documents (section);
CREATE INDEX idx_doc_trgm ON documents USING gin (title gin_trgm_ops);

CREATE INDEX idx_cl_doc  ON clauses (doc_id);
CREATE INDEX idx_cl_key  ON clauses (doc_key);
CREATE INDEX idx_cl_eff  ON clauses (effective_from, effective_to);
CREATE INDEX idx_cl_tsv  ON clauses USING gin (tsv);
-- 关键词路走 ILIKE / trigram，这个索引必须有，否则每次全表扫 7690 行
CREATE INDEX idx_cl_content_trgm ON clauses USING gin (content gin_trgm_ops);
CREATE INDEX idx_cl_hnsw ON clauses USING hnsw (embedding vector_cosine_ops)
       WITH (m = 16, ef_construction = 64);
"""


def load_csvs(data_dir: Path):
    doc_path = data_dir / "documents_all.csv"
    clause_path = data_dir / "clauses.csv"
    for p in (doc_path, clause_path):
        if not p.exists():
            sys.exit(f"找不到 {p}")

    with open(doc_path, encoding="utf-8") as f:
        docs = list(csv.DictReader(f))
    with open(clause_path, encoding="utf-8") as f:
        clauses = list(csv.DictReader(f))

    doc_records = [
        (
            r["doc_key"], r["section"], r["title"], r["doc_no"] or None,
            _date(r["publish_date"]), _date(r["effective_from"]), _date(r["effective_to"]),
            r["category"], r["source_url"], r["body"], _int(r["body_len"]),
            r["mentions_repeal"].strip() == "1",
        )
        for r in docs
    ]

    # 条款先存 dict，doc_id 要等 documents 插完、拿到映射后再填
    clause_rows = [
        (
            r["doc_key"], _int(r["segment"], 1), r["clause_no"], r["clause_path"],
            r["level"], _int(r["ordinal"]), r["content"], _int(r["content_len"]),
            _date(r["effective_from"]), _date(r["effective_to"]),
        )
        for r in clauses
    ]
    return doc_records, clause_rows


async def run_import(dsn: str, data_dir: Path, force: bool, no_cache: bool) -> None:
    doc_records, clause_rows = load_csvs(data_dir)
    print(f"CSV 解析完成：documents={len(doc_records)} clauses={len(clause_rows)}")

    conn = await asyncpg.connect(dsn, **connect_kwargs(dsn, no_cache))
    try:
        exists = await conn.fetchval(
            "SELECT to_regclass('public.documents') IS NOT NULL"
        )
        if exists and not force:
            sys.exit("表已存在。确认要重建请加 --force（会删掉现有数据）")

        print("建扩展和表结构 ...")
        await conn.execute(DDL)

        print("导入 documents ...")
        await conn.copy_records_to_table(
            "documents", records=doc_records, columns=DOC_COLUMNS
        )

        print("读取 doc_key -> id 映射 ...")
        rows = await conn.fetch("SELECT doc_key, id FROM documents")
        key_to_id = {r["doc_key"]: r["id"] for r in rows}

        print("导入 clauses ...")
        clause_records = [
            (key_to_id[ck], ck) + tuple(rest)
            for (ck, *rest) in clause_rows
            if ck in key_to_id
        ]
        skipped = len(clause_rows) - len(clause_records)
        if skipped:
            print(f"  跳过 {skipped} 条：对应文档不在 documents 表里")
        await conn.copy_records_to_table(
            "clauses", records=clause_records, columns=CLAUSE_COLUMNS
        )

        print("生成全文索引 tsv（中文用 simple，暂未装 zhparser）...")
        await conn.execute(
            "UPDATE clauses SET tsv = to_tsvector('simple', content)"
        )

        print("建索引（HNSW 在向量全空时建得很快）...")
        await conn.execute(INDEXES)

        docs_n = await conn.fetchval("SELECT count(*) FROM documents")
        cl_n = await conn.fetchval("SELECT count(*) FROM clauses")
        eff_n = await conn.fetchval(
            "SELECT count(*) FROM clauses WHERE effective_from IS NOT NULL"
        )
        print("\n=== 导入完成 ===")
        print(f"documents               : {docs_n}")
        print(f"clauses                 : {cl_n}")
        print(f"clauses 有生效日期        : {eff_n}")
        print(f"clauses 待向量化         : {cl_n}")
    finally:
        await conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=os.getenv("DATABASE_URL"))
    ap.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    ap.add_argument("--force", action="store_true", help="表已存在时先删再建")
    ap.add_argument("--dry-run", action="store_true", help="只解析 CSV，不连数据库")
    ap.add_argument("--no-statement-cache", action="store_true",
                    help="连连接池地址（如 Supabase 6543）时加上，避免 prepared statement 冲突")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)

    if args.dry_run:
        docs, clauses = load_csvs(data_dir)
        print(f"[dry-run] documents={len(docs)} clauses={len(clauses)}")
        print("[dry-run] 首条 document:", docs[0][:4])
        print("[dry-run] 首条 clause  :", clauses[0][:6])
        return

    if not args.dsn:
        sys.exit("没有数据库连接串。设置环境变量 DATABASE_URL 或传 --dsn")

    asyncio.run(run_import(args.dsn, data_dir, args.force, args.no_statement_cache))


if __name__ == "__main__":
    main()
