"""条款详情接口：GET /api/clause/{id}

前端点击引用卡片时调它，拉出该条款的原文和前后文，用于抽屉展示。
"""
import logging

import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from app.config import Settings, get_settings
from app.db import get_pool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["docs"])


@router.get("/clause/{clause_id}")
async def get_clause(
    clause_id: int,
    pool: asyncpg.Pool = Depends(get_pool),
    settings: Settings = Depends(get_settings),
) -> dict:
    """返回单条条款 + 同文档相邻条款，供前端高亮与上下文阅读。"""
    row = await pool.fetchrow(
        """SELECT c.id, c.clause_no, c.clause_path, c.content,
                  c.ordinal, c.doc_id, c.effective_from,
                  d.title, d.doc_no, d.source_url, d.publish_date, d.section
             FROM clauses c
             JOIN documents d ON d.id = c.doc_id
            WHERE c.id = $1""",
        clause_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="条款不存在")

    # 取前后各一条，方便用户在原文里找到位置
    siblings = await pool.fetch(
        """SELECT id, clause_no, content
             FROM clauses
            WHERE doc_id = $1 AND ordinal BETWEEN $2 AND $3
            ORDER BY ordinal""",
        row["doc_id"], row["ordinal"] - 1, row["ordinal"] + 1,
    )

    return {
        "clause_id": row["id"],
        "clause_no": row["clause_no"],
        "clause_path": row["clause_path"],
        "content": row["content"],
        "doc_title": row["title"],
        "doc_no": row["doc_no"],
        "section": row["section"],
        "publish_date": row["publish_date"].isoformat() if row["publish_date"] else None,
        "effective_from": row["effective_from"].isoformat() if row["effective_from"] else None,
        "source_url": row["source_url"],
        "siblings": [
            {"id": s["id"], "clause_no": s["clause_no"], "content": s["content"]}
            for s in siblings
        ],
    }
