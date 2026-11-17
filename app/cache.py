"""答案缓存：相同问题 + 相同时间点，直接返回上次的结果。

为什么要缓存：
1. 演示时同一个问题经常问两遍，第二遍还要等 15–25 秒，观感很差；
2. 每次提问要花一次 embedding + 一次大模型，缓存能省掉重复开销。

设计取舍：
- 缓存键 = 归一化问题 + as_of + CACHE_VERSION。
  CACHE_VERSION 是关键：改了 prompt、检索逻辑或校验规则后，把版本号 +1，
  旧缓存自动失效，不用手动清库。这比「按时间全清」精确得多。
- 带 TTL，默认 7 天。政策文件不会天天变，但也不能永久缓存。
- 拒答结果也缓存。否则同一个编造问题会被反复调模型，反而更浪费。
- 召回 id 一起缓存：命中时仍能写完整的 query_log，不影响后续评测统计。
"""
import hashlib
import json
import logging
import re

import asyncpg

logger = logging.getLogger(__name__)

# 改了生成 prompt / 检索逻辑 / 校验规则时，把这个版本号加一
CACHE_VERSION = "v1"

_WS = re.compile(r"\s+")


def normalize(question: str) -> str:
    """归一化：去空白、统一小写。避免「租房补贴」和「租房 补贴」走两份缓存。"""
    return _WS.sub("", question or "").strip().lower()


def make_key(question: str, as_of: str, version: str = CACHE_VERSION) -> str:
    raw = f"{version}|{normalize(question)}|{as_of}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def get(pool: asyncpg.Pool, key: str, ttl_days: int) -> dict | None:
    """取缓存。返回 {answer, retrieved_ids}；过期或不存在返回 None。"""
    try:
        row = await pool.fetchrow(
            """SELECT answer, retrieved_ids FROM answer_cache
                WHERE cache_key = $1
                  AND created_at > now() - make_interval(days => $2::int)""",
            key, ttl_days,
        )
    except Exception as exc:  # noqa: BLE001
        # 缓存表不存在或查询失败，不能让主流程挂掉，直接当未命中
        logger.warning("读缓存失败（按未命中处理）：%s", exc)
        return None
    if row is None:
        return None
    raw = row["answer"]
    # asyncpg 默认把 jsonb 读成字符串，不注册 codec 的话必须自己解析
    answer = json.loads(raw) if isinstance(raw, str) else raw
    return {"answer": answer, "retrieved_ids": list(row["retrieved_ids"] or [])}


async def put(
    pool: asyncpg.Pool,
    key: str,
    question: str,
    as_of,            # 传 datetime.date 对象，不要传字符串：
                      # SQL 里的 $3::date 会让 asyncpg 把参数类型推断成 date，
                      # 传 str 会报 "str object has no attribute toordinal"。
    answer: dict,
    retrieved_ids: list[int],
) -> None:
    """写缓存。失败只记日志，绝不影响用户拿到答案。"""
    try:
        await pool.execute(
            """INSERT INTO answer_cache (cache_key, question, as_of, answer, retrieved_ids)
               VALUES ($1, $2, $3::date, $4::jsonb, $5::bigint[])
               ON CONFLICT (cache_key) DO UPDATE
                 SET answer = EXCLUDED.answer,
                     retrieved_ids = EXCLUDED.retrieved_ids,
                     created_at = now()""",
            key, question, as_of, json.dumps(answer, ensure_ascii=False), retrieved_ids,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("写缓存失败（不影响返回）：%s", exc)


DDL = """
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
