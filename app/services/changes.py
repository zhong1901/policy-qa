"""政策变更检测：判断源站上的文件相比库里有没有变化。

为什么要单独做一层：
知识库产品最致命的失败不是"答得不准"，而是"政策已经改了，它还在答旧版"。
而且这种错**看不出来**——引用是真的、条款是真的，只是过期了。

检测口径（按可靠性排序）：
1. 内容哈希变了  → changed（最可靠）
2. URL 消失      → gone    （可能是下架，也可能是改版）
3. 出现新 URL    → new

"gone 很多 + new 很多 + 标题重合" 基本可以判定为站点改版，而不是政策变动。
这是两种完全不同的处置：改版只要重抓，政策变动要走人工复核。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

NEW = "new"
CHANGED = "changed"
GONE = "gone"


def hash_text(s: str | None) -> str:
    return hashlib.md5((s or "").encode("utf-8")).hexdigest()


@dataclass
class IncomingDoc:
    url: str
    title: str | None = None
    content_hash: str = ""


@dataclass
class DiffResult:
    new: list[dict] = field(default_factory=list)
    changed: list[dict] = field(default_factory=list)
    gone: list[dict] = field(default_factory=list)
    rewrite_suspected: bool = False
    rewrite_reason: str = ""
    current_count: int = 0
    incoming_count: int = 0

    @property
    def total_changes(self) -> int:
        return len(self.new) + len(self.changed) + len(self.gone)

    @property
    def is_clean(self) -> bool:
        return self.total_changes == 0


CURRENT_SQL = """
    SELECT source_url, doc_key, title, doc_no,
           md5(coalesce(body, '')) AS content_hash,
           coalesce(body_len, 0) AS body_len
      FROM documents
     WHERE source_url IS NOT NULL
"""


async def load_current(pool) -> list[dict]:
    rows = await pool.fetch(CURRENT_SQL)
    return [dict(r) for r in rows]


def diff(current: list[dict], incoming: list[IncomingDoc]) -> DiffResult:
    """比对库里现状与源站清单。纯函数，方便单测。"""
    cur = {d["source_url"]: d for d in current if d.get("source_url")}
    inc = {i.url: i for i in incoming if i.url}

    res = DiffResult(current_count=len(cur), incoming_count=len(inc))

    for url, doc in inc.items():
        old = cur.get(url)
        if old is None:
            res.new.append({"url": url, "title": doc.title,
                            "content_hash": doc.content_hash})
        elif doc.content_hash and old["content_hash"] != doc.content_hash:
            res.changed.append({
                "url": url,
                "title": doc.title or old.get("title"),
                "doc_key": old.get("doc_key"),
                "old_hash": old["content_hash"],
                "new_hash": doc.content_hash,
                "old_len": old.get("body_len"),
            })

    for url, doc in cur.items():
        if url not in inc:
            res.gone.append({"url": url, "title": doc.get("title"),
                             "doc_key": doc.get("doc_key")})

    # 改版判定：大量 gone 与大量 new 同时出现，且标题大量重合
    if res.gone and res.new:
        gone_titles = {(d.get("title") or "").strip() for d in res.gone}
        new_titles = {(d.get("title") or "").strip() for d in res.new}
        overlap = len(gone_titles & new_titles)
        ratio = overlap / max(1, min(len(gone_titles), len(new_titles)))
        if len(res.gone) >= 10 and len(res.new) >= 10 and ratio >= 0.5:
            res.rewrite_suspected = True
            res.rewrite_reason = (
                f"{len(res.gone)} 条 URL 消失、{len(res.new)} 条新 URL 出现，"
                f"其中 {overlap} 条标题完全一致 —— 更像站点改版而不是政策变动"
            )

    return res


CHANGE_KIND_LABEL = {
    NEW: "新增文件",
    CHANGED: "内容变更",
    GONE: "文件消失",
}


def summarize_diff(res: DiffResult) -> str:
    """给人看的一段话。"""
    if res.is_clean:
        return f"无变化（比对 {res.current_count} 条）"
    parts = []
    if res.new:
        parts.append(f"新增 {len(res.new)}")
    if res.changed:
        parts.append(f"变更 {len(res.changed)}")
    if res.gone:
        parts.append(f"消失 {len(res.gone)}")
    text = f"{' / '.join(parts)}（库内 {res.current_count} 条，源站 {res.incoming_count} 条）"
    if res.rewrite_suspected:
        text += "\n⚠ 疑似站点改版：" + res.rewrite_reason
    return text
