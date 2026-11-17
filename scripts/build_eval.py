# -*- coding: utf-8 -*-
r"""生成评测集素材：
1. 读取 eval/eval_questions.csv（题库）
2. 每条问题跑一遍混合检索，取 top-3 候选条款
3. 产出两份文件：
   - eval/qa_eval_candidates.md  给你读的（每条候选的全文）
   - eval/qa_eval_template.csv   给你填的（gold_clause_id / gold_answer / difficulty / note）

你只需要：读 md 里的候选 → 判断能不能答 → 把答案抄进 csv。

用法：
    python scripts/build_eval.py
"""
from __future__ import annotations

import asyncio
import csv
import os
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings              # noqa: E402
from app.services.retrieve import hybrid_retrieve  # noqa: E402

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = Path(os.environ.get("EVAL_OUT") or (ROOT / "eval"))
Q_FILE = ROOT / "eval" / "eval_questions.csv"
MD_FILE = OUT_DIR / "qa_eval_candidates.md"
TPL_FILE = OUT_DIR / "qa_eval_template.csv"

TOP_N = 3


def connect_kwargs(dsn: str) -> dict:
    host = (urlparse(dsn).hostname or "").lower()
    kw: dict = {"command_timeout": 120}
    if host not in ("localhost", "127.0.0.1", "::1", ""):
        kw["ssl"] = "require"
    return kw


async def main() -> None:
    questions = list(csv.DictReader(open(Q_FILE, encoding="utf-8")))
    print(f"题库 {len(questions)} 条")

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        sys.exit("没有 DATABASE_URL")

    settings = get_settings()
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3,
                                     **connect_kwargs(dsn))

    md: list[str] = [
        "# 评测集候选条款",
        "",
        "每条问题下面是混合检索返回的 top-3 条款。",
        "你要做的：判断这 3 条里有没有能回答的 —— 有就记下 clause_id 并把答案抄进 csv；",
        "没有就把 gold_clause_id 留空、expected 改成 `拒`。",
        "",
    ]
    tpl_rows = []

    try:
        for i, q in enumerate(questions, 1):
            qid = q["id"]
            text = q["question"]
            hits = await hybrid_retrieve(pool, text, date.today(), settings, limit=TOP_N)

            ids = [str(h.clause_id) for h in hits]

            md.append(f"## {qid}　[{q['stratum']}]　预期：{q['expected']}")
            md.append("")
            md.append(f"**问题：** {text}")
            md.append("")
            if not hits:
                md.append("> 检索无结果")
                md.append("")
            for rank, h in enumerate(hits, 1):
                body = h.content.replace("\n", " ").strip()[:500]
                md.append(f"**候选 {rank}**　clause_id = `{h.clause_id}`　"
                          f"《{h.doc_title}》　{h.clause_no or ''}")
                md.append("")
                md.append(f"> {body}")
                md.append("")
            md.append("---")
            md.append("")

            tpl_rows.append({
                "id": qid,
                "stratum": q["stratum"],
                "question": text,
                "expected": q["expected"],
                "candidate_ids": "/".join(ids),
                "gold_clause_id": "",
                "gold_answer": "",
                "difficulty": "",
                "note": "",
            })
            print(f"  [{i}/{len(questions)}] {qid} -> {len(hits)} 条候选", flush=True)
    finally:
        await pool.close()

    MD_FILE.parent.mkdir(parents=True, exist_ok=True)
    MD_FILE.write_text("\n".join(md), encoding="utf-8")

    with open(TPL_FILE, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(tpl_rows[0].keys()))
        w.writeheader()
        w.writerows(tpl_rows)

    print(f"\n生成：\n  {MD_FILE}\n  {TPL_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
