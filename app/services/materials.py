"""申办材料清单：把条款原文里的「（一）（二）（三）」切开，逐项列出。

设计原则：**不抽取、不改写、不让模型参与。**

条款原文本来就是枚举结构：

    第八条（申请材料） 申办本市常住户口的申请材料具体包括：
    （一）有效身份凭证和《居住证持有人办理本市常住户口申请表》；
    （二）区以上税务机关出具的个人所得税或者企业纳税完税凭证；
    （三）…

正则按（一）（二）切开就行。每一条材料都保留原文、条款号、来源文件，
用户可以点回原文核对。

为什么不让模型抽取：
- 抽取会把「（四）无刑事犯罪记录等情况的个人承诺」压缩成「无犯罪证明」，
  改掉了原文的准确表述；
- 而且模型可能凭空加一条不存在的材料。
材料清单是给人拿去办事用的，错一条就是白跑一趟。
"""
from __future__ import annotations

import logging
import re

import asyncpg

logger = logging.getLogger(__name__)

# 按「（一）（二）」或「(一)(二)」切分
_RE_ITEM = re.compile(r"[（(]([一二三四五六七八九十]{1,3})[）)]")

# 条款末尾常有「符合第六条规定的…还应当提交相应材料」这类补充句，
# 不属于任何一项材料，切掉。
_RE_TAIL = re.compile(r"(符合第[一二三四五六七八九十]+条规定的|在本市[“\"]?一网通办)")

# 哪条路径用哪一条材料条款。
# 只配「现行版本」的条款 id，写死是为了可核对——改一次要人能看懂。
PATH_MATERIAL_CLAUSE = {
    "juzhuanhu": 7374,   # 居转户 2024 版 第八条（申请材料）
    "talent": 7336,      # 引进人才 2025 版 第六条（申办材料）
    # fresh（应届生）：那份通知的材料格式是「一、二、三」而不是（一）（二），
    # 需要单独处理，暂时留空 —— 宁可说「待补充」，也不要给一份切错的清单。
}


def split_items(content: str) -> list[dict]:
    """把一条枚举型条款切成若干项。返回 [{label, text}]。"""
    marks = list(_RE_ITEM.finditer(content))
    if len(marks) < 2:
        return []

    items: list[dict] = []
    for i, m in enumerate(marks):
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(content)
        text = content[start:end].strip(" ；;。\n")
        # 最后一项可能挂着补充说明，切掉
        cut = _RE_TAIL.search(text)
        if cut:
            text = text[:cut.start()].strip(" ；;。\n")
        if text:
            items.append({"label": m.group(1), "text": text})
    return items


async def get_materials(pool: asyncpg.Pool, path_key: str) -> dict | None:
    """取某条路径的申办材料清单。

    返回 {"clause_id", "clause_no", "doc_title", "items": [...]}，取不到返回 None。
    """
    clause_id = PATH_MATERIAL_CLAUSE.get(path_key)
    if not clause_id:
        return None

    try:
        row = await pool.fetchrow(
            """SELECT c.id, c.clause_no, c.content, d.title
                 FROM clauses c JOIN documents d ON d.id = c.doc_id
                WHERE c.id = $1""", clause_id)
    except asyncpg.PostgresError:
        logger.exception("读取材料条款失败 clause_id=%s", clause_id)
        return None

    if row is None:
        logger.warning("材料条款不存在 clause_id=%s", clause_id)
        return None

    items = split_items(row["content"] or "")
    if not items:
        logger.warning("材料条款切不出枚举项 clause_id=%s", clause_id)
        return None

    return {
        "clause_id": row["id"],
        "clause_no": row["clause_no"],
        "doc_title": row["title"],
        "items": items,
    }
