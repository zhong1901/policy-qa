# -*- coding: utf-8 -*-
"""给 clauses.embedding 回填向量（调硅基流动 bge-m3，批量）。

为什么用 API 而不是本地跑 bge-m3：
- 本地要下 2.2GB 权重，2C4G 的服务器跑不动；
- 7690 条走 API 批量（一批 32 条）大概 240 次请求，几分钟跑完，成本几毛钱。

用法：
    python scripts/embed_backfill.py --probe           # 只测 API 是否可批量，不连库
    python scripts/embed_backfill.py --limit 200       # 先跑 200 条看看效果
    python scripts/embed_backfill.py                   # 全量
    python scripts/embed_backfill.py --dsn "postgresql://..."

可断点续跑：已经填过的行会被跳过（WHERE embedding IS NULL）。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from urllib.parse import urlparse

try:
    import asyncpg
    import httpx
except ImportError:
    sys.exit("缺少依赖，请先 pip install -r requirements.txt")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DEFAULT_DIM = 1024


def connect_kwargs(dsn: str, no_statement_cache: bool) -> dict:
    """云数据库要 SSL；连接池地址要关 prepared statement 缓存。逻辑同 import_data.py。"""
    host = (urlparse(dsn).hostname or "").lower()
    is_local = host in ("localhost", "127.0.0.1", "::1", "")
    kwargs: dict = {"command_timeout": 120}
    if not is_local:
        kwargs["ssl"] = "require"
    if no_statement_cache:
        kwargs["statement_cache_size"] = 0
    return kwargs


def fmt_vec(vec: list[float]) -> str:
    """pgvector 的文本输入格式：'[0.1,0.2,...]'。"""
    return "[" + ",".join(f"{x:.8g}" for x in vec) + "]"


async def embed_batch(
    client: httpx.AsyncClient, url: str, key: str, model: str, texts: list[str]
) -> list[list[float]]:
    """一次请求把一批文本转成向量。返回顺序与入参一致。"""
    resp = await client.post(
        url,
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "input": texts},
    )
    resp.raise_for_status()
    data = resp.json().get("data") or []
    if len(data) != len(texts):
        raise RuntimeError(f"返回条数不符：请求 {len(texts)}，返回 {len(data)}")
    # 服务端通常会带 index，按它排序保证对齐
    if all("index" in d for d in data):
        data.sort(key=lambda d: d["index"])
    return [d["embedding"] for d in data]


async def probe(base_url: str, key: str, model: str) -> None:
    """不发数据库，只验证 API 能批量返回。"""
    url = f"{base_url.rstrip('/')}/embeddings"
    async with httpx.AsyncClient(timeout=60.0) as client:
        t = time.time()
        vecs = await embed_batch(
            client, url, key, model, ["上海应届生落户", "租房补贴申请材料", "居住证积分"]
        )
        print(f"批量返回 {len(vecs)} 条，耗时 {(time.time() - t) * 1000:.0f}ms")
        for i, v in enumerate(vecs):
            print(f"  [{i}] 维度={len(v)} 前3值={[round(x, 4) for x in v[:3]]}")


async def backfill(dsn: str, base_url: str, key: str, model: str,
                   dim: int, batch_size: int, limit: int | None,
                   no_cache: bool = False) -> None:
    url = f"{base_url.rstrip('/')}/embeddings"
    conn = await asyncpg.connect(dsn, **connect_kwargs(dsn, no_cache))
    try:
        total = await conn.fetchval("SELECT count(*) FROM clauses WHERE embedding IS NULL")
        print(f"待向量化 {total} 条，本次上限 {limit or '全部'}")
        target = min(total, limit) if limit else total

        done = 0
        started = time.time()
        async with httpx.AsyncClient(timeout=120.0) as client:
            while done < target:
                rows = await conn.fetch(
                    """SELECT id, content FROM clauses
                        WHERE embedding IS NULL
                        ORDER BY id
                        LIMIT $1""",
                    min(batch_size, target - done),
                )
                if not rows:
                    break

                texts = [r["content"] for r in rows]
                try:
                    vecs = await embed_batch(client, url, key, model, texts)
                except Exception as exc:
                    print(f"\n批量请求失败，稍后重试一次: {exc}")
                    await asyncio.sleep(3)
                    vecs = await embed_batch(client, url, key, model, texts)

                for v in vecs:
                    if len(v) != dim:
                        raise RuntimeError(f"维度不符：期望 {dim}，实际 {len(v)}")

                await conn.executemany(
                    "UPDATE clauses SET embedding = $1::text::vector WHERE id = $2",
                    [(fmt_vec(v), r["id"]) for v, r in zip(vecs, rows)],
                )

                done += len(rows)
                elapsed = time.time() - started
                rate = done / elapsed if elapsed else 0
                eta = (target - done) / rate if rate else 0
                print(f"  {done}/{target}  {rate:.1f} 条/秒  剩余约 {eta / 60:.1f} 分钟",
                      flush=True)

        filled = await conn.fetchval("SELECT count(*) FROM clauses WHERE embedding IS NOT NULL")
        print(f"\n完成：已向量化 {filled} / {total + 0} 条")
    finally:
        await conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=os.getenv("DATABASE_URL"))
    ap.add_argument("--base-url", default=os.getenv("EMBEDDING_BASE_URL",
                                                    "https://api.siliconflow.cn/v1"))
    ap.add_argument("--key", default=os.getenv("EMBEDDING_API_KEY"))
    ap.add_argument("--model", default=os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3"))
    ap.add_argument("--dim", type=int, default=int(os.getenv("EMBEDDING_DIM", DEFAULT_DIM)))
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 条，用于试跑")
    ap.add_argument("--probe", action="store_true", help="只测 API，不连数据库")
    ap.add_argument("--no-statement-cache", action="store_true",
                    help="连连接池地址（如 Supabase 6543）时加上")
    args = ap.parse_args()

    if not args.key:
        sys.exit("没有 EMBEDDING_API_KEY，请设置环境变量或在 .env 里填")

    if args.probe:
        asyncio.run(probe(args.base_url, args.key, args.model))
        return

    if not args.dsn:
        sys.exit("没有数据库连接串。设置环境变量 DATABASE_URL 或传 --dsn")

    asyncio.run(backfill(args.dsn, args.base_url, args.key, args.model,
                         args.dim, args.batch_size, args.limit,
                         args.no_statement_cache))


if __name__ == "__main__":
    main()
