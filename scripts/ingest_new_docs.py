"""把增量抓取到的新文档入库：切分 → 向量化 → 写 documents + clauses。

这是「发现 → 抓取 → 切分 → 入库 → 回归」链路的第四环。

设计要点：
1. 幂等：按 source_url 判重，重复执行不会产生重复文档。
2. 事务：一篇文档的 documents + clauses 在同一个事务里写完，要么全成要么全不成，
   避免出现「有文档没条款」这种半截数据。
3. 向量化先于写库：embedding 失败就在写库前中止，不留脏数据。
4. doc_key = md5(source_url)[:16]，与既有 1301 篇的生成方式一致。

用法：
    python scripts/ingest_new_docs.py                        # 入库 crawl-out/new_docs.jsonl
    python scripts/ingest_new_docs.py --dry-run              # 只切分，不向量化不写库
    python scripts/ingest_new_docs.py --limit 3              # 只处理前 3 篇（试跑）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.services.clausesplit import split_document  # noqa: E402
from app.services.embed import embed_texts  # noqa: E402

load_dotenv(ROOT / ".env")

DEFAULT_INPUT = Path(os.environ.get("CRAWL_OUT_DIR") or (ROOT / "crawl-out")) / "new_docs.jsonl"

DOC_INSERT = """
INSERT INTO documents
    (doc_key, section, title, doc_no, publish_date, effective_from, effective_to,
     status, category, source_url, body, body_len, mentions_repeal,
     fetched_at, content_hash, crawl_run_id)
VALUES ($1,$2,$3,$4,$5::date,$6::date,NULL,'active',$7,$8,$9,$10,$11,now(),$12,$13)
RETURNING id
"""

CLAUSE_INSERT = """
INSERT INTO clauses
    (doc_id, doc_key, segment, clause_no, clause_path, level, ordinal,
     content, content_len, effective_from, effective_to, embedding, tsv)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,
        (SELECT effective_from FROM documents WHERE id=$1), NULL,
        $10::text::vector, to_tsvector('simple', $8))
"""


def connect_kwargs(dsn: str) -> dict:
    kw = {"dsn": dsn}
    if "supabase" in dsn or "sslmode=require" in dsn:
        kw["ssl"] = "require"
    return kw


def parse_date(s: str | None):
    """把 'YYYY-MM-DD' 转成 date。

    asyncpg 对 $n::date 参数要求是 date 对象，传字符串会报
    "'str' object has no attribute 'toordinal'" —— 这个坑踩过两次了。
    """
    if not s:
        return None
    from datetime import date
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.8g}" for x in vec) + "]"


async def main() -> int:
    ap = argparse.ArgumentParser(description="新文档入库")
    ap.add_argument("--input", default=str(DEFAULT_INPUT), help="new_docs.jsonl 路径")
    ap.add_argument("--dry-run", action="store_true", help="只切分，不向量化不写库")
    ap.add_argument("--limit", type=int, help="只处理前 N 篇")
    args = ap.parse_args()

    path = Path(args.input)
    if not path.exists():
        print(f"找不到输入文件：{path}")
        print("先跑 scripts/crawl_incremental.py 生成 new_docs.jsonl")
        return 2

    docs = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if args.limit:
        docs = docs[:args.limit]
    print(f"待处理 {len(docs)} 篇\n")

    dsn = os.getenv("DATABASE_URL")
    settings = get_settings()
    conn = await asyncpg.connect(**connect_kwargs(dsn))

    stats = Counter()
    try:
        existing = {r["source_url"] for r in await conn.fetch(
            "SELECT source_url FROM documents WHERE source_url IS NOT NULL")}
        run_id = await conn.fetchval(
            "SELECT id FROM crawl_runs WHERE ok ORDER BY id DESC LIMIT 1")

        for i, doc in enumerate(docs, 1):
            url = doc.get("url")
            title = (doc.get("title") or "").strip()
            body = doc.get("body") or ""

            if not url or not body:
                stats["skip_empty"] += 1
                print(f"  [{i}/{len(docs)}] 跳过（正文为空，多为图解页）{title[:30]}")
                continue
            if url in existing:
                stats["skip_exists"] += 1
                print(f"  [{i}/{len(docs)}] 跳过（已存在）{title[:30]}")
                continue

            mode, clauses = split_document(body)
            if not clauses:
                stats["no_clause"] += 1
                print(f"  [{i}/{len(docs)}] 跳过（正文仅 {len(body)} 字，切不出条款）{title[:26]}")
                continue

            if args.dry_run:
                stats["dry_ok"] += 1
                print(f"  [{i}/{len(docs)}] {mode:<7} {len(clauses):>3} 条  {title[:36]}")
                continue

            # 先向量化，失败就在写库前中止
            vectors = await embed_texts([c["content"] for c in clauses], settings)

            doc_key = hashlib.md5(url.encode("utf-8")).hexdigest()[:16]
            content_hash = hashlib.md5(body.encode("utf-8")).hexdigest()

            async with conn.transaction():
                doc_id = await conn.fetchval(
                    DOC_INSERT, doc_key, doc.get("section"), title,
                    doc.get("doc_no") or None,
                    parse_date(doc.get("publish_date")),
                    parse_date(doc.get("effective_from")),
                    (url.split("/")[3] if len(url.split("/")) > 3 else None),
                    url, body, len(body),
                    bool("废止" in body[:600] or "失效" in body[:600]),
                    content_hash, run_id)
                for ordinal, (c, vec) in enumerate(zip(clauses, vectors), 1):
                    await conn.execute(
                        CLAUSE_INSERT, doc_id, doc_key, c["segment"], c["clause_no"],
                        c["clause_path"], c["level"], ordinal, c["content"],
                        len(c["content"]), vec_literal(vec))

            existing.add(url)
            stats["ingested"] += 1
            stats["clauses_" + mode] += len(clauses)
            print(f"  [{i}/{len(docs)}] ✓ {mode:<7} {len(clauses):>3} 条  {title[:36]}")
    finally:
        await conn.close()

    print("\n" + "=" * 56)
    print(f"入库 {stats['ingested']} 篇，"
          f"新增条款 {sum(v for k, v in stats.items() if k.startswith('clauses_'))} 条")
    for k, v in sorted(stats.items()):
        if k.startswith("clauses_"):
            print(f"  {k[8:]:<8} {v} 条")
    if stats["skip_exists"]:
        print(f"  跳过（已存在）{stats['skip_exists']} 篇")
    if stats["skip_empty"] or stats["no_clause"]:
        print(f"  跳过（空/切不出）{stats['skip_empty'] + stats['no_clause']} 篇")
    if args.dry_run:
        print("\n（--dry-run，未写库）")
    else:
        print("\n下一步：python scripts/run_regression.py --run")
    return 0


if __name__ == "__main__":
    sys.exit(__import__("asyncio").run(main()))
