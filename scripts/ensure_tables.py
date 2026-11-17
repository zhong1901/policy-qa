# -*- coding: utf-8 -*-
r"""补齐 import_data.py 没建的辅助表（qa_eval / query_log），并导入评测集。

用法：
    python scripts/ensure_tables.py
"""
from __future__ import annotations

import asyncio
import csv
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import asyncpg

ROOT = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DDL = """
CREATE TABLE IF NOT EXISTS qa_eval (
    id              BIGSERIAL PRIMARY KEY,
    qid             TEXT UNIQUE,
    stratum         TEXT,
    question        TEXT NOT NULL,
    expected        TEXT,
    verdict         TEXT,
    gold_clause_ids BIGINT[],
    difficulty      TEXT,
    status          TEXT,
    gold_answer     TEXT,
    note            TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS query_log (
    id              BIGSERIAL PRIMARY KEY,
    question        TEXT,
    as_of           DATE,
    retrieved_ids   BIGINT[],
    answer          JSONB,
    refused         BOOLEAN DEFAULT FALSE,
    latency_ms      INT,
    prompt_tokens   INT,
    completion_tokens INT,
    feedback        SMALLINT,
    feedback_note   TEXT,
    client_ip_hash  TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_log_time ON query_log (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_qa_stratum ON qa_eval (stratum);

CREATE TABLE IF NOT EXISTS answer_cache (
    cache_key     TEXT PRIMARY KEY,
    question      TEXT NOT NULL,
    as_of         DATE NOT NULL,
    answer        JSONB NOT NULL,
    retrieved_ids BIGINT[],
    created_at    TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_cache_time ON answer_cache (created_at DESC);
"""


def connect_kwargs(dsn: str) -> dict:
    host = (urlparse(dsn).hostname or "").lower()
    kw: dict = {"command_timeout": 60}
    if host not in ("localhost", "127.0.0.1", "::1", ""):
        kw["ssl"] = "require"
    return kw


async def main() -> None:
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        sys.exit("没有 DATABASE_URL")

    conn = await asyncpg.connect(dsn, **connect_kwargs(dsn))
    try:
        await conn.execute(DDL)
        print("qa_eval / query_log 已就绪")

        csv_path = ROOT / "eval" / "qa_eval.csv"
        if csv_path.exists():
            rows = list(csv.DictReader(open(csv_path, encoding="utf-8-sig")))
            n = 0
            for r in rows:
                ids = [int(x) for x in (r.get("gold_clause_ids") or "").split("/") if x.strip()]
                await conn.execute(
                    """INSERT INTO qa_eval
                       (qid, stratum, question, expected, verdict,
                        gold_clause_ids, difficulty, status, gold_answer, note)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                       ON CONFLICT (qid) DO UPDATE SET
                         stratum=EXCLUDED.stratum, question=EXCLUDED.question,
                         expected=EXCLUDED.expected, verdict=EXCLUDED.verdict,
                         gold_clause_ids=EXCLUDED.gold_clause_ids,
                         difficulty=EXCLUDED.difficulty, status=EXCLUDED.status,
                         gold_answer=EXCLUDED.gold_answer, note=EXCLUDED.note""",
                    r["id"], r["stratum"], r["question"], r["expected"], r["verdict"],
                    ids or None, r["difficulty"] or None, r["status"],
                    r["gold_answer"] or None, r["note"] or None,
                )
                n += 1
            print(f"导入评测集 {n} 条")
        else:
            print("没找到 eval/qa_eval.csv，跳过评测集导入")

        for table in ("documents", "clauses", "qa_eval", "query_log"):
            cnt = await conn.fetchval(f"SELECT count(*) FROM {table}")
            print(f"  {table:<12} {cnt}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
