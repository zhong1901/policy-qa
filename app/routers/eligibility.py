"""资格自测接口：POST /api/eligibility

规则判定由 app/services/eligibility.py 完成（确定性的硬规则）；
本层只负责把判定依据的条款原文取出来，一起返回给前端。
"""
import logging
from datetime import date

import asyncpg
from fastapi import APIRouter, Depends

from app.config import Settings, get_settings
from app.db import get_pool
from app.schemas import (
    EligibilityCitation,
    EligibilityCheck,
    EligibilityRequest,
    EligibilityResponse,
    EligibilitySummary,
    EligibilityVerdict,
)
from app.services.eligibility import Profile, evaluate, summarize

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["eligibility"])


@router.post("/eligibility", response_model=EligibilityResponse)
async def eligibility(
    req: EligibilityRequest,
    pool: asyncpg.Pool = Depends(get_pool),
    settings: Settings = Depends(get_settings),
) -> EligibilityResponse:
    profile = Profile(
        degree=req.degree,
        has_degree_cert=req.has_degree_cert,
        grad_year=req.grad_year,
        is_shanghai_school=req.is_shanghai_school,
        social_months=req.social_months,
        resident_years=req.resident_years,
        title=req.title,
        tax_paid=req.tax_paid,
        employer=req.employer,
        age=req.age,
    )
    verdicts = evaluate(profile)

    # 一次把用到的条款全取回来
    all_ids = sorted({cid for v in verdicts for cid in v.clause_ids})
    clause_map: dict[int, dict] = {}
    if all_ids:
        rows = await pool.fetch(
            """SELECT c.id, c.clause_no, c.content, c.content_len,
                      d.title, d.doc_no, d.source_url
                 FROM clauses c JOIN documents d ON d.id = c.doc_id
                WHERE c.id = ANY($1::bigint[])""",
            all_ids,
        )
        for r in rows:
            clause_map[r["id"]] = {
                "clause_id": r["id"],
                "doc_title": r["title"],
                "clause_no": r["clause_no"],
                "doc_no": r["doc_no"],
                # 条款可能很长，前端只展示前 400 字，完整内容点开抽屉看
                "quote": (r["content"] or "")[:400],
                "url": r["source_url"],
            }

    out: list[EligibilityVerdict] = []
    for v in verdicts:
        out.append(EligibilityVerdict(
            key=v.key,
            name=v.name,
            result=v.result,
            checks=[EligibilityCheck(label=c.label, state=c.state, detail=c.detail)
                    for c in v.checks],
            citations=[EligibilityCitation(**clause_map[cid])
                       for cid in v.clause_ids if cid in clause_map],
            note=v.note,
            extra=v.extra,
        ))

    logger.info("资格自测完成：%s", {v.key: v.result for v in verdicts})
    s = summarize(verdicts)
    return EligibilityResponse(
        summary=EligibilitySummary(
            verdict=s.verdict, headline=s.headline, detail=s.detail,
            best_key=s.best_key, best_name=s.best_name, gaps=s.gaps,
            alternatives=s.alternatives, suggestions=s.suggestions,
            points_note=s.points_note,
        ),
        verdicts=out,
        as_of=date.today(),
    )
