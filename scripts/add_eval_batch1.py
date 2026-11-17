# -*- coding: utf-8 -*-
"""扩建评测集：第一批（档案 / 社保 / 工伤 / 劳动合同 / 积分），16 题。

出题原则：
1. 每题都有 gold_clause_id，且该条款原文确实回答了这个问题（不是凭印象标的）。
2. 优先补「条款很多但一道题都没测」的主题——档案 118 条 0 题、工伤 1128 条 1 题。
3. 问题用用户会说的话问，不用法条术语问（测的是检索，不是背名词）。

用法：
    python scripts/add_eval_batch1.py --dry-run   # 先核对每条 gold 的原文
    python scripts/add_eval_batch1.py
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

# (qid, stratum, question, gold_ids, note)
QUESTIONS = [
    # ── 档案（此前 0 题覆盖）──
    ("A19", "事实", "申请居住证积分时，人才服务中心会调阅我的人事档案吗？",
     [7473], "档案主题首题；条款明确「按流动人员档案管理有关规定调阅人事档案」"),
    ("A20", "事实", "不定时工作制的行政许可档案要保存多少年？",
     [6226], "档案保管年限，答案在条款末尾"),
    ("B17", "条件组合", "哪些人可以申报档案专业技术职称？",
     [72], "范围题，需要排除「人事档案」和「院校教学教师」两类"),

    # ── 社保（325 条条款，此前只覆盖 1 条）──
    ("A21", "事实", "平台企业怎么给新就业形态人员办理职业伤害保障参保？",
     [6940], "实名制 + 按日归集 + 报送全国信息平台"),
    ("A22", "事实", "超龄就业人员和实习生参加工伤保险，费用由谁缴纳？",
     [7038], "答案是「用人单位按月缴纳」，缴费基数按劳动报酬确定"),
    ("B18", "条件组合", "失业人员什么时候可以去办失业登记？",
     [7250], "两个前提：退工手续办妥 + 不再缴社保；还有一个「最后参保地或户籍地在本市」的条件"),

    # ── 劳动合同 / 事业单位聘用合同 ──
    ("A23", "事实", "劳动合同变更需要什么形式？",
     [2318], "答案是「书面形式确定」，容易和口头变更混淆"),
    ("B19", "条件组合", "事业单位受聘人员在试用期内可以随时解除聘用合同吗？",
     [3882], "属于「随时解除」的四种情形之一"),
    ("B20", "条件组合", "事业单位在哪些情形下应当解除聘用合同？",
     [3876], "三类情形：试用期满考核不合格、被判刑、以及其他"),

    # ── 工伤（1128 条条款，此前只覆盖 1 条）──
    ("A24", "事实", "工伤人员在劳动能力鉴定前就需要配置辅助器具，该怎么办？",
     [4875], "明确「可以向用人单位注册地的区县劳鉴委提出申请」"),
    ("A25", "事实", "骗取劳动能力鉴定结论会怎么处理？",
     [7012], "答案是撤销结论"),
    ("B21", "条件组合", "做劳动能力鉴定会提前通知吗？要带什么材料？",
     [6341], "通知时间地点材料，本人必须按通知参加"),

    # ── 积分（补充未覆盖条款）──
    ("A26", "事实", "居住证积分申请材料审核通过后，多久能核定完成？",
     [7474], "20 个工作日"),
    ("B22", "条件组合", "有什么情况会直接被取消居住证积分申请资格？",
     [7618], "一票否决：违反计划生育政策记录 + 严重刑事犯罪记录"),
    ("B23", "条件组合", "同一个单项指标能不能重复积分？",
     [7467], "不重复计算、取最高分；减分项按累计扣减"),

    # ── 事业单位待遇 ──
    ("B24", "条件组合", "事业单位依据规定解除聘用合同时，除了经济补偿还要付医疗补助费吗？",
     [3891], "答案是另付 6 个月工资的医疗补助费"),
]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只核对 gold 原文，不写库")
    args = ap.parse_args()

    dsn = os.getenv("DATABASE_URL")
    kw = {"dsn": dsn, "ssl": "require"} if ("supabase" in dsn or "sslmode=require" in dsn) else {"dsn": dsn}
    conn = await asyncpg.connect(**kw)
    try:
        ids = [g for _, _, _, gs, _ in QUESTIONS for g in gs]
        rows = await conn.fetch(
            """SELECT c.id, c.clause_no, left(c.content, 90) AS body, d.title
                 FROM clauses c JOIN documents d ON d.id = c.doc_id
                WHERE c.id = ANY($1::bigint[])""", ids)
        got = {r["id"]: r for r in rows}

        missing = [i for i in ids if i not in got]
        if missing:
            print(f"✗ 这些 gold id 不存在：{missing}")
            return 1

        print(f"共 {len(QUESTIONS)} 题，gold 条款全部存在\n")
        for qid, stratum, q, gs, note in QUESTIONS:
            print(f"[{qid}] {q}")
            for g in gs:
                r = got[g]
                print(f"    gold {g} {r['clause_no']} 《{r['title'][:26]}》")
                print(f"         {r['body']}")
            print(f"    备注：{note}")

        if args.dry_run:
            print("\n（--dry-run，未写库）")
            return 0

        n = 0
        for qid, stratum, q, gs, note in QUESTIONS:
            exists = await conn.fetchval("SELECT 1 FROM qa_eval WHERE qid=$1", qid)
            if exists:
                print(f"  跳过 {qid}（已存在）")
                continue
            await conn.execute(
                """INSERT INTO qa_eval (qid, stratum, question, expected, verdict,
                                        gold_clause_ids, difficulty, status, note)
                   VALUES ($1,$2,$3,'答','可答',$4,'medium','新增',$5)""",
                qid, stratum, q, gs, note)
            n += 1
        total = await conn.fetchval("SELECT count(*) FROM qa_eval")
        print(f"\n写入 {n} 题，评测集现在共 {total} 题")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
