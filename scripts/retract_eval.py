# -*- coding: utf-8 -*-
"""撤掉 3 道出错的评测题：它们依据的文件已失效。

B19/B20/B24 的 gold 条款都出自《上海市事业单位聘用合同管理办法》，
该办法 2023-12-31 就失效了。系统正确地把它过滤掉了——
错的是我的出题方式：只按关键词挑条款，没核对文件还在不在有效期。

顺带加一条自检：以后往评测集加题，先检查 gold 条款所属文件是否仍然有效。
"""
import asyncio
import os
from datetime import date

import asyncpg
from dotenv import load_dotenv

load_dotenv(r"E:\pycharm项目\上海应届生\.env")

RETRACT = ["B19", "B20", "B24"]


async def main():
    conn = await asyncpg.connect(os.getenv("DATABASE_URL"), ssl="require")
    try:
        # 先给全部 gold 条款做一次有效期体检，把问题暴露出来
        rows = await conn.fetch("""
            SELECT q.qid, q.question, g.gid,
                   COALESCE(c.effective_to, d.effective_to) AS expired_on,
                   left(d.title, 30) AS title
              FROM qa_eval q, unnest(q.gold_clause_ids) g(gid)
              JOIN clauses c ON c.id = g.gid
              JOIN documents d ON d.id = c.doc_id
             WHERE COALESCE(c.effective_to, d.effective_to) < $1::date
               -- 「版本对比」类题目**故意**引用已失效的旧版本（答案就是新旧差异），
               -- 它们引用过期文件是正确的，不能当标注错误处理。
               AND q.status IS DISTINCT FROM '版本对比'
             ORDER BY q.qid""", date.today())

        print(f"体检：{len(rows)} 条 gold 取自已失效文件")
        for r in rows:
            print(f"  {r['qid']}  {r['title']}  失效于 {r['expired_on']}")

        n = 0
        for qid in RETRACT:
            got = await conn.fetchval(
                "UPDATE qa_eval SET status='已撤回', expected='撤回', "
                "note=COALESCE(note,'') || ' [撤回：gold 文件已失效，系统正确过滤]' "
                "WHERE qid=$1 RETURNING qid", qid)
            if got:
                n += 1
        left = await conn.fetchval("SELECT count(*) FROM qa_eval WHERE status <> '已撤回'")
        print(f"\n已撤回 {n} 题，评测集有效题数：{left}")
    finally:
        await conn.close()


asyncio.run(main())
