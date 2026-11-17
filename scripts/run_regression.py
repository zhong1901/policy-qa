"""回归对比：把最新的评测结果和上一次基线比，找出退化的题。

这是「更新流水线」的最后一环，也是最关键的一环：
改了数据或改了代码之后，怎么知道有没有把原本答对的题搞坏？

用法：
    python scripts/run_regression.py --save 基线名     # 把当前报告存成基线
    python scripts/run_regression.py                  # 和最近一次基线比
    python scripts/run_regression.py --run            # 先跑一遍评测再比
    python scripts/run_regression.py --run --save 更新后

退出码：0 = 无退化；1 = 发现退化
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REPORT = ROOT / "eval" / "eval_report.json"
if not REPORT.exists():
    REPORT = Path(r"E:\deep seek\_eval_out\eval_report.json")

load_dotenv(ROOT / ".env")

# 指标允许的下降幅度。超过就算退化。
THRESHOLDS = {
    "recall_at_5": 0.03,
    "citation_hit_rate": 0.05,
    "refusal_accuracy": 0.05,
    "answer_rate": 0.05,
}

METRIC_LABEL = {
    "recall_at_5": "Recall@5",
    "citation_hit_rate": "引用命中率",
    "refusal_accuracy": "拒答准确率",
    "answer_rate": "回答率",
}


def connect_kwargs(dsn: str) -> dict:
    kw = {"dsn": dsn}
    if "supabase" in dsn or "sslmode=require" in dsn:
        kw["ssl"] = "require"
    return kw


def is_correct(row: dict) -> bool:
    """单题是否"做对了"：该答的答了、该拒的拒了。"""
    if row.get("expected") == "拒":
        return bool(row.get("refused"))
    return not row.get("refused")


async def save_baseline(label: str) -> None:
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    dsn = os.getenv("DATABASE_URL")
    conn = await asyncpg.connect(**connect_kwargs(dsn))
    try:
        doc_n = await conn.fetchval("SELECT count(*) FROM documents")
        cl_n = await conn.fetchval("SELECT count(*) FROM clauses")
        await conn.execute(
            """INSERT INTO eval_baselines (label, metrics, details, doc_count, clause_count)
               VALUES ($1, $2::jsonb, $3::jsonb, $4, $5)""",
            label,
            json.dumps(report["summary"], ensure_ascii=False),
            json.dumps(report["details"], ensure_ascii=False),
            doc_n, cl_n,
        )
    finally:
        await conn.close()
    s = report["summary"]
    print(f"已保存基线「{label}」：Recall@5 {s.get('recall_at_5', 0):.1%}，"
          f"文档 {doc_n} / 条款 {cl_n}")


async def compare() -> int:
    if not REPORT.exists():
        print(f"找不到评测报告：{REPORT}")
        return 1
    current = json.loads(REPORT.read_text(encoding="utf-8"))

    dsn = os.getenv("DATABASE_URL")
    conn = await asyncpg.connect(**connect_kwargs(dsn))
    try:
        row = await conn.fetchrow(
            """SELECT id, created_at, label, metrics, details, doc_count, clause_count
                 FROM eval_baselines ORDER BY id DESC LIMIT 1""")
    finally:
        await conn.close()

    if row is None:
        print("还没有基线。先跑一次 `--save 初始` 再比较。")
        return 0

    base = json.loads(row["metrics"]) if isinstance(row["metrics"], str) else row["metrics"]
    base_details = json.loads(row["details"]) if isinstance(row["details"], str) else row["details"]
    cur = current["summary"]

    print(f"基线：{row['label'] or '(未命名)'}  建立于 {row['created_at']:%Y-%m-%d %H:%M}")
    print(f"基线数据量：文档 {row['doc_count']} / 条款 {row['clause_count']}")
    print()

    regressions: list[str] = []
    print(f"{'指标':<14}{'基线':>10}{'当前':>10}{'变化':>10}")
    print("-" * 44)
    for key, limit in THRESHOLDS.items():
        b = base.get(key)
        c = cur.get(key)
        if b is None or c is None:
            continue
        delta = c - b
        flag = ""
        if delta < -limit:
            flag = "  ← 退化"
            regressions.append(
                f"{METRIC_LABEL[key]} 从 {b:.1%} 降到 {c:.1%}（-{abs(delta)*100:.1f}pt）")
        print(f"{METRIC_LABEL[key]:<14}{b:>10.1%}{c:>10.1%}{delta:>+10.1%}{flag}")

    # 逐题对比：找出从"做对"变成"做错"的题
    base_map = {d["qid"]: d for d in base_details}
    turned_bad, turned_good = [], []
    for d in current["details"]:
        b = base_map.get(d["qid"])
        if not b:
            continue
        was, now = is_correct(b), is_correct(d)
        if was and not now:
            turned_bad.append(d)
        elif not was and now:
            turned_good.append(d)

    if turned_bad:
        print(f"\n从「正确」变成「错误」的题：{len(turned_bad)}")
        for d in turned_bad:
            print(f"  ✗ {d['qid']}  {d['question'][:34]}")
            print(f"      期望={d['expected']}  实际={'拒答' if d['refused'] else '作答'}")
            regressions.append(f"{d['qid']} 由正确变为错误")
    if turned_good:
        print(f"\n从「错误」变成「正确」的题：{len(turned_good)}")
        for d in turned_good:
            print(f"  ✓ {d['qid']}  {d['question'][:34]}")

    print("\n" + "=" * 56)
    if regressions:
        print(f"结论：发现 {len(regressions)} 处退化")
        for r in regressions:
            print("  -", r)
        print("\n建议：先定位这次改动影响了什么，不要直接发布。")
        return 1
    print("结论：没有退化。")
    return 0


async def main() -> int:
    ap = argparse.ArgumentParser(description="评测回归对比")
    ap.add_argument("--run", action="store_true", help="先跑一遍完整评测")
    ap.add_argument("--save", metavar="LABEL", help="把当前报告保存为基线")
    args = ap.parse_args()

    if args.run:
        print("跑评测中（约 10 分钟）…\n")
        code = subprocess.call([sys.executable, str(ROOT / "scripts" / "run_eval.py")])
        if code != 0:
            print(f"评测失败，退出码 {code}")
            return code

    # 顺序很重要：**先和旧基线比，再把新结果存成基线**。
    # 反过来写就成了「拿自己和自己比」，永远返回「没有退化」。
    rc = await compare()
    if args.save:
        await save_baseline(args.save)
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
