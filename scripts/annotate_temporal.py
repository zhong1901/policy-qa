# -*- coding: utf-8 -*-
"""补 6 道「版本时效」题的 gold 标注。

标注过程中发现一个架构问题，值得记下来：
C01 / C03 / C07 问的是「新旧版本有什么不同」，答案必须同时包含两个版本。
但旧版本的文件已经过了有效期（2020 版居转户截止 2024-12-31，
2020 版引进人才截止 2025-11-30），而检索带 as_of 时间过滤，
**会正确地排除已失效文件**。

于是这几题在当前架构下必然答不对——不是检索不准，是设计上没留
「取历史版本」的通道。这不是 bug，是缺口，标出来让它显形。

用法：python scripts/annotate_temporal.py --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# qid -> (gold_ids, 说明, 是否刻意引用旧版本)
ANNOTATIONS = {
    "C01": ([7594, 7371],
            "2020版第5条(城镇社会保险) vs 2024版第5条(职工社会保险)；"
            "旧版文件已于2024-12-31失效，需跨版本召回", True),
    "C03": ([7487, 7335],
            "2020版第5条 vs 2025版第5条；旧版文件已于2025-11-30失效，需跨版本召回", True),
    "C04": ([7481],
            "2023版《居住证积分管理办法》第二十四条：自2023-01-01起施行，有效期至2027-12-31", False),
    "C06": ([7381],
            "2024版《居转户办法》第十五条：自2025-01-01起施行，有效期至2029-12-31", False),
    "C07": ([7621, 7469],
            "2018版与2023版第十二条均为120分（结论：标准分值未调整）；"
            "2018版已过期，需跨版本召回", True),
    "C08": ([7449],
            "2023版《居住证申办实施细则》第十九条：自2023-01-01起施行，有效期至2027-12-31", False),
}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    dsn = os.getenv("DATABASE_URL")
    kw = {"dsn": dsn}
    if "supabase" in dsn or "sslmode=require" in dsn:
        kw["ssl"] = "require"
    conn = await asyncpg.connect(**kw)
    try:
        ids = [g for g, _, _ in ANNOTATIONS.values() for g in g]
        rows = await conn.fetch(
            """SELECT c.id, c.clause_no, left(c.content, 70) AS body,
                      COALESCE(c.effective_to, d.effective_to) AS until,
                      left(d.title, 30) AS title
                 FROM clauses c JOIN documents d ON d.id = c.doc_id
                WHERE c.id = ANY($1::bigint[])""", ids)
        info = {r["id"]: r for r in rows}
        missing = [i for i in ids if i not in info]
        if missing:
            print(f"✗ gold id 不存在：{missing}")
            return 1

        for qid, (golds, note, cross) in ANNOTATIONS.items():
            q = await conn.fetchval("SELECT question FROM qa_eval WHERE qid=$1", qid)
            print(f"[{qid}] {q}")
            for g in golds:
                r = info[g]
                print(f"    [{g}] {r['clause_no']} 《{r['title']}》 截止 {r['until']}")
                print(f"         {r['body']}")
            print(f"    {'⚠ 需跨版本召回' if cross else '✓ 现行版本可答'}")
            print()

        if args.dry_run:
            print("（--dry-run，未写库）")
            return 0

        n = 0
        for qid, (golds, note, cross) in ANNOTATIONS.items():
            # 跨版本的题打上「版本对比」标记，供 retract_eval 的有效期体检豁免：
            # 它们引用已失效文件是**故意的**，不是标注错误。
            status = "版本对比" if cross else "已标注"
            await conn.execute(
                """UPDATE qa_eval
                      SET gold_clause_ids = $2, status = $3,
                          note = COALESCE(note,'') || $4
                    WHERE qid = $1""",
                qid, golds, status, f" [补标注：{note}]")
            n += 1
        left = await conn.fetchval(
            "SELECT count(*) FROM qa_eval WHERE stratum='版本时效' "
            "AND (gold_clause_ids IS NULL OR array_length(gold_clause_ids,1)=0)")
        print(f"已补标注 {n} 题；版本时效层仍无 gold 的题：{left}")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
