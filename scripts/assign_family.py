# -*- coding: utf-8 -*-
"""给条款打上「所属办法」标签（policy_family）。

为什么需要这个字段：
跨版本召回要拿「同一部法规在不同年份的第 5 条」来对比。
原来的做法是按文档标题模糊匹配，遇到**合订本**就串了——
2025 年那份文件把《引进人才申办办法》和《持证人员申办办法》印在同一个文件里，
按文档找族会把两部办法的条款混在一起。

根因是一个隐含假设：「一个文档 = 一部法规」。合订本让这个假设不成立。
所以从文档级下沉到条款级：每条条款自己知道属于哪部办法。

判定规则：
  普通文档  → 整篇按标题/正文关键词判一个族
  合订本    → 同一个文档里出现重复 clause_no 就是合订本，
              再按 segment 分段，每段单独判族

用法：
    python scripts/assign_family.py --dry-run   # 只出核验表，不写库
    python scripts/assign_family.py             # 写库
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# 判族只认「标题里出现法规全名」。
# 之前用正文关键词判，结果「应届生落户」收了 70 个文档——事业单位招聘问答、
# 就业补贴通知里全是「毕业生」「就业」这类词。**标题才是身份，正文只是内容。**
FAMILY_TITLE = {
    "居转户": "人员申办本市常住户口办法",
    "引进人才": "引进人才申办本市常住户口办法",
    "居住证积分": "居住证积分管理办法",
    "居住证申办实施细则": "居住证申办实施细则",
    "海外人才居住证": "海外人才居住证管理办法",
    "应届生落户": "非上海生源应届普通高校毕业生进沪就业",
}

# 合订本内部分段时，才用正文关键词在这几个候选族之间做区分
SEGMENT_KEYWORDS = {
    "居转户": ["持证人员", "先落户", "居住证满7年"],
    "引进人才": ["引进的人才", "用人单位引进", "高层次人才", "重点机构"],
}


def families_in_title(title: str) -> list[str]:
    """标题里出现了哪几部办法的名字。合订本会返回多个。"""
    return [fam for fam, name in FAMILY_TITLE.items() if name in title]


def pick_segment_family(text: str, candidates: list[str]) -> str | None:
    """合订本内部：在这几部办法之间，按正文关键词判定这一段属于哪部。"""
    best, best_n = None, 0
    for fam in candidates:
        n = sum(text.count(k) for k in SEGMENT_KEYWORDS.get(fam, []))
        if n > best_n:
            best, best_n = fam, n
    return best or (candidates[0] if candidates else None)


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
        await conn.execute(
            "ALTER TABLE clauses ADD COLUMN IF NOT EXISTS policy_family TEXT")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_clause_family "
            "ON clauses (policy_family, clause_no)")

        # 分页取。跨境链路上一次拉 7729 条（约 2.3MB）会被中途掐断，
        # 分批 150 个文档稳稳取完。
        rows = []
        doc_ids = [r["id"] for r in await conn.fetch("SELECT id FROM documents ORDER BY id")]
        for i in range(0, len(doc_ids), 150):
            chunk = doc_ids[i:i + 150]
            rows.extend(await conn.fetch(
                """SELECT c.id, c.doc_id, c.segment, c.clause_no,
                          left(c.content, 300) AS content, d.title
                     FROM clauses c JOIN documents d ON d.id = c.doc_id
                    WHERE c.doc_id = ANY($1::bigint[])
                    ORDER BY c.doc_id, c.ordinal""", chunk))
        rows.sort(key=lambda r: (r["doc_id"], r["clause_no"]))
        print(f"共 {len(rows)} 条条款，{len({r['doc_id'] for r in rows})} 个文档\n")

        by_doc: dict[int, list] = defaultdict(list)
        for r in rows:
            by_doc[r["doc_id"]].append(r)

        updates: list[tuple[str | None, int]] = []
        merged_docs = 0
        for doc_id, items in by_doc.items():
            title = items[0]["title"]
            # 「政策问答」是对法规的解读，不是法规本身，不参与版本对比
            if "政策问答" in title or "问答" == title[-2:]:
                for r in items:
                    updates.append((None, r["id"]))
                continue
            candidates = families_in_title(title)
            if not candidates:
                # 标题不含任何法规全名 → 不是政策版本，不归类
                for r in items:
                    updates.append((None, r["id"]))
                continue

            nos = [r["clause_no"] for r in items]
            is_merged = len(nos) != len(set(nos)) and len(candidates) > 1
            if is_merged:
                merged_docs += 1

            if not is_merged:
                for r in items:
                    updates.append((candidates[0], r["id"]))
            else:
                segs: dict[int, list] = defaultdict(list)
                for r in items:
                    segs[r["segment"]].append(r)
                for seg, seg_items in segs.items():
                    text = " ".join(r["content"][:300] for r in seg_items[:6])
                    fam = pick_segment_family(text, candidates)
                    for r in seg_items:
                        updates.append((fam, r["id"]))

        print(f"识别为合订本的文档：{merged_docs} 个\n")

        # ── 核验表：每个族、每个版本的条款数 ──
        cid_to_doc = {r["id"]: r["doc_id"] for r in rows}
        fam_title = {r["doc_id"]: r["title"] for r in rows}
        fam_doc: Counter = Counter()
        for fam, cid in updates:
            if fam:
                fam_doc[(fam, cid_to_doc[cid])] += 1

        print("=== 核验表：每个族的版本与条款数 ===")
        grouped: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for (fam, doc_id), n in fam_doc.items():
            grouped[fam].append((doc_id, n))
        for fam in sorted(grouped, key=lambda f: -len(grouped[f])):
            print(f"\n【{fam}】{len(grouped[fam])} 个版本")
            for doc_id, n in sorted(grouped[fam]):
                print(f"   doc#{doc_id:<5} {n:>3} 条  《{fam_title[doc_id][:44]}》")

        unassigned = sum(1 for fam, _ in updates if fam is None)
        print(f"\n未归类（非政策版本类文档）：{unassigned} 条")

        if args.dry_run:
            print("\n（--dry-run，未写库）")
            return 0

        await conn.executemany(
            "UPDATE clauses SET policy_family = $1 WHERE id = $2", updates)
        n = await conn.fetchval(
            "SELECT count(*) FROM clauses WHERE policy_family IS NOT NULL")
        print(f"\n已写入：{n} 条条款带上 policy_family")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
