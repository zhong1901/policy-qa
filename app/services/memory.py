"""记忆层：短期会话历史 + 长期用户画像。

设计取舍（重要，面试会问）：

1. 为什么短期记忆只存最近几轮，不做全量摘要？
   摘要本身要调一次模型，成本和延迟都翻倍，而多轮追问通常只依赖上一两轮。
   只带最近 3 轮是成本与效果的平衡点；更早的内容如果真重要，用户会再说一次。

2. 为什么长期画像不由模型从对话里抽取？
   三个原因：
   - 隐私：从对话里抽画像 = 收集个人信息，与「不存明文提问」的原则冲突；
   - 准确：「我社保交了几年」这种模糊表述抽取极易出错，而错画像会污染后续所有回答；
   - 复用：资格自测本来就有一张结构化表单，让用户主动保存，比模型猜准得多。
   所以：画像是用户主动确认后写入的，不是猜出来的。

3. 为什么画像按字段覆盖而不是累加？
   用户可能从「本科」变成「硕士」。累加会得到互相矛盾的画像，覆盖才是对的。
"""
from __future__ import annotations

import json
import logging

import asyncpg

logger = logging.getLogger(__name__)

# 送进 prompt 的最近轮数
HISTORY_TURNS = 3


async def get_history(pool: asyncpg.Pool, session_id: str,
                      limit: int = HISTORY_TURNS) -> list[dict]:
    """取最近若干轮对话，按时间正序返回 [{role, content}]。"""
    if not session_id:
        return []
    try:
        rows = await pool.fetch(
            """SELECT role, content FROM turns
                WHERE session_id = $1
                ORDER BY id DESC
                LIMIT $2""",
            session_id, limit * 2,   # 一问一答算两条
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("读会话历史失败（按空处理）：%s", exc)
        return []
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


async def append_turn(pool: asyncpg.Pool, session_id: str, role: str,
                      content: str, answer: dict | None = None) -> None:
    """追加一条对话。失败只记日志，不影响主流程。"""
    if not session_id:
        return
    try:
        await pool.execute(
            """INSERT INTO turns (session_id, role, content, answer)
               VALUES ($1, $2, $3, $4::jsonb)""",
            session_id, role, content,
            json.dumps(answer, ensure_ascii=False) if answer else None,
        )
        await pool.execute(
            "UPDATE sessions SET updated_at = now() WHERE id = $1", session_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("写对话历史失败（不影响返回）：%s", exc)


async def ensure_session(pool: asyncpg.Pool, session_id: str) -> None:
    if not session_id:
        return
    try:
        await pool.execute(
            "INSERT INTO sessions (id) VALUES ($1) ON CONFLICT (id) DO NOTHING",
            session_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("建会话失败：%s", exc)


async def get_profile(pool: asyncpg.Pool, session_id: str) -> dict | None:
    """取长期画像。没有就返回 None。"""
    if not session_id:
        return None
    try:
        row = await pool.fetchrow(
            "SELECT profile FROM user_profiles WHERE session_id = $1", session_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("读画像失败：%s", exc)
        return None
    if row is None:
        return None
    raw = row["profile"]
    return json.loads(raw) if isinstance(raw, str) else raw


async def save_profile(pool: asyncpg.Pool, session_id: str, profile: dict) -> None:
    """保存画像。整条覆盖，不合并——避免新旧信息互相矛盾。"""
    await pool.execute(
        """INSERT INTO user_profiles (session_id, profile, updated_at)
           VALUES ($1, $2::jsonb, now())
           ON CONFLICT (session_id) DO UPDATE
             SET profile = EXCLUDED.profile, updated_at = now()""",
        session_id, json.dumps(profile, ensure_ascii=False),
    )


async def clear_session(pool: asyncpg.Pool, session_id: str) -> None:
    """清空一个会话的全部记忆（对话 + 画像）。用户应该有能力一键清掉。"""
    await pool.execute("DELETE FROM turns WHERE session_id = $1", session_id)
    await pool.execute("DELETE FROM user_profiles WHERE session_id = $1", session_id)
    await pool.execute("DELETE FROM sessions WHERE id = $1", session_id)


def profile_to_text(p: dict) -> str:
    """把画像压成一句话，注入 system prompt。

    注意：只描述事实，不做推断——推断应该由资格自测的规则层做。
    """
    parts = []
    if p.get("degree"):
        parts.append(f"最高学历{p['degree']}")
        if p.get("has_degree_cert") is False:
            parts.append("（未取得学位）")
    if p.get("grad_year"):
        parts.append(f"{p['grad_year']} 年毕业")
    if p.get("title") and p["title"] != "无":
        parts.append(f"职称「{p['title']}」")
    if p.get("social_months") is not None:
        parts.append(f"社保累计 {p['social_months']} 个月")
    if p.get("resident_years") is not None:
        parts.append(f"居住证持有 {p['resident_years']:g} 年")
    if p.get("employer"):
        parts.append(f"用人单位为{p['employer']}")
    if p.get("age"):
        parts.append(f"{p['age']} 岁")
    return "；".join(parts)


DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS turns (
    id          BIGSERIAL PRIMARY KEY,
    session_id  TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    answer      JSONB,
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns (session_id, id DESC);

CREATE TABLE IF NOT EXISTS user_profiles (
    session_id  TEXT PRIMARY KEY,
    profile     JSONB NOT NULL,
    updated_at  TIMESTAMPTZ DEFAULT now()
);
"""
