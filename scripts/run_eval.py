# -*- coding: utf-8 -*-
r"""跑评测：对 qa_eval 表里的每条问题调用 /api/ask 的完整链路，算指标。

指标定义：
  Recall@5         = |gold ∩ 召回| / |gold|          —— 该找到的条款找到没有
  引用精确率        = |引用 ∩ gold| / |引用|           —— 引用是不是指向正确条款
  拒答准确率        = 正确拒答数 / 应拒答数            —— 该拒的拒了没有
  误拒率            = 误拒数 / 应可答数                —— 该答的却被拒了
  回答率            = 实际作答数 / 应可答数

用法：
    python scripts/run_eval.py --limit 5      # 先跑 5 条试试
    python scripts/run_eval.py                # 全量
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import asyncpg
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings            # noqa: E402
from app.routers.ask import ask                # noqa: E402
from app.schemas import AskRequest             # noqa: E402

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

OUT_DIR = Path(os.environ.get("EVAL_OUT") or (Path(__file__).resolve().parent.parent / "eval"))


def connect_kwargs(dsn: str) -> dict:
    host = (urlparse(dsn).hostname or "").lower()
    kw: dict = {"command_timeout": 120}
    if host not in ("localhost", "127.0.0.1", "::1", ""):
        kw["ssl"] = "require"
    return kw


async def run_one(row, pool, settings, repeat: int = 1) -> dict:
    qid = row["qid"]
    gold = set(row["gold_clause_ids"] or [])
    expected = row["expected"]
    t = time.time()

    # 多次采样取多数：实测同一份数据在 temperature=0 下跑两次，
    # 12 题里有 2 题行为不同（16.7%）——模型本身不是逐位确定的。
    # 单次采样测出来的「退化」有三分之一可能只是抖了一下，所以拒答判定取多数票。
    votes: list[bool] = []
    cited: set[int] = set()
    error = None
    for _ in range(max(1, repeat)):
        try:
            resp = await ask(
                AskRequest(question=row["question"], as_of=date.today()),
                pool=pool,
                settings=settings,
            )
            votes.append(resp.refused)
            cited |= {c.clause_id for c in resp.citations}
        except HTTPException as exc:
            error = f"HTTP {exc.status_code}: {exc.detail}"
            break
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            break

    if votes:
        # 平票时按「作答」处理：宁可算作答了，也不要因为随机性把拒答当成稳定行为
        refused = votes.count(True) > len(votes) / 2
    else:
        refused = False
    latency = int((time.time() - t) * 1000)

    # 从 query_log 拿本次召回。必须按 question 精确匹配 + 取最新一条：
    # 如果只按 id DESC 取全表最新，一旦某题中途 503 没写日志，就会读到上一题的行，
    # 导致 recall 计算串题。
    #
    # 还要加时间下界：本题生成失败时根本不会写日志，此时会读到**上一次运行**的旧记录，
    # 拿旧召回算 recall。2026-11-19 那次 CI 就是这样显示了一个假的 79.6%。
    log = await pool.fetchrow(
        """SELECT retrieved_ids FROM query_log
            WHERE question = $1 AND ($2::timestamp IS NULL OR created_at >= $2)
            ORDER BY id DESC LIMIT 1""",
        row["question"], _RUN_STARTED_AT,
    )
    retrieved = set(log["retrieved_ids"] or []) if log else set()

    hit = gold & retrieved if gold else set()
    return {
        "qid": qid,
        "stratum": row["stratum"],
        "question": row["question"],
        "expected": expected,
        "refused": refused,
        "gold": sorted(gold),
        "retrieved": sorted(retrieved),
        "hit": sorted(hit),
        "recall": (len(hit) / len(gold)) if gold else None,
        "cited": sorted(cited),
        "citation_precision": (
            len(cited & gold) / len(cited) if (cited and gold) else None
        ),
        # 引用命中率：答案里是否包含标注的那一条。
        # 比 precision 更贴合实际——一条答案合理引用多条条款时，
        # 只有部分被标注，precision 会被系统性压低。
        "citation_hit": bool(cited & gold) if (cited and gold) else None,
        "latency_ms": latency,
        "error": error,
    }


def summarize(results: list[dict]) -> dict:
    ans = [r for r in results if r["expected"] == "答"]
    ref = [r for r in results if r["expected"] == "拒"]

    recalls = [r["recall"] for r in ans if r["recall"] is not None]
    cps = [r["citation_precision"] for r in results if r["citation_precision"] is not None]
    chs = [r["citation_hit"] for r in results if r.get("citation_hit") is not None]

    answered = [r for r in ans if not r["refused"]]
    false_refusals = [r for r in ans if r["refused"]]
    correct_refusals = [r for r in ref if r["refused"]]

    by_stratum = defaultdict(lambda: {"n": 0, "recall": [], "answered": 0, "correct_ref": 0})
    for r in results:
        s = by_stratum[r["stratum"]]
        s["n"] += 1
        if r["recall"] is not None:
            s["recall"].append(r["recall"])
        if r["expected"] == "答" and not r["refused"]:
            s["answered"] += 1
        if r["expected"] == "拒" and r["refused"]:
            s["correct_ref"] += 1

    return {
        "total": len(results),
        "answerable": len(ans),
        "should_refuse": len(ref),
        "recall_at_5": sum(recalls) / len(recalls) if recalls else None,
        "citation_precision": sum(cps) / len(cps) if cps else None,
        "citation_hit_rate": (
            sum(1 for x in chs if x) / len(chs) if chs else None
        ),
        "answered": len(answered),
        "answer_rate": len(answered) / len(ans) if ans else None,
        "false_refusal": len(false_refusals),
        "false_refusal_rate": len(false_refusals) / len(ans) if ans else None,
        "correct_refusal": len(correct_refusals),
        "refusal_accuracy": len(correct_refusals) / len(ref) if ref else None,
        "avg_latency_ms": sum(r["latency_ms"] for r in results) / len(results),
        "errors": sum(1 for r in results if r["error"]),
        "by_stratum": {
            k: {
                "n": v["n"],
                "recall": (sum(v["recall"]) / len(v["recall"])) if v["recall"] else None,
                "answered": v["answered"],
                "correct_refusal": v["correct_ref"],
            }
            for k, v in by_stratum.items()
        },
    }


def fmt_pct(x):
    return f"{x*100:.1f}%" if x is not None else "—"


_RUN_STARTED_AT = None


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--only-with-gold", action="store_true",
                    help="只跑有 gold_clause_ids 的题（用于快速冒烟）")
    ap.add_argument("--sleep", type=float, default=0.3)
    ap.add_argument("--temperature", type=float, default=0.0,
                    help="生成温度。评测默认 0，把模型随机性从指标里剔掉——"
                         "否则测出来的「退化」可能只是抖了一下")
    ap.add_argument("--repeat", type=int, default=1,
                    help="每题重复采样几次，取多数票。实测同一数据两次运行有 16.7% 的题会翻转，"
                         "做回归门禁时建议至少 3 次")
    ap.add_argument("--stratum", default=None,
                    help="只跑某一层（如 拒答），用于低成本测噪声")
    args = ap.parse_args()

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        sys.exit("没有 DATABASE_URL")

    # 覆盖温度：只影响本次评测进程，不动生产默认值
    global _RUN_STARTED_AT
    from datetime import datetime as _dt
    _RUN_STARTED_AT = _dt.now()
    settings = get_settings().model_copy(update={"llm_temperature": args.temperature})
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3, **connect_kwargs(dsn))

    try:
        rows = await pool.fetch(
            "SELECT qid, stratum, question, expected, gold_clause_ids "
            # 撤回的题不参与评测（例如 gold 出自已失效文件的题，
            # 系统过滤掉它是正确行为，不该算它错）
            "FROM qa_eval WHERE status IS DISTINCT FROM '已撤回' "
            + ("AND gold_clause_ids IS NOT NULL AND array_length(gold_clause_ids,1) > 0 " if args.only_with_gold else "")
            + "ORDER BY qid"
        )
        if args.stratum:
            rows = [r for r in rows if r["stratum"] == args.stratum]
        if args.limit:
            rows = rows[: args.limit]
        print(f"共 {len(rows)} 题，模型={settings.llm_model}，"
              f"阈值={settings.refusal_threshold}，temperature={args.temperature}\n")

        results = []
        for i, row in enumerate(rows, 1):
            r = await run_one(row, pool, settings, args.repeat)
            results.append(r)
            flag = "✓" if (
                (r["expected"] == "答" and not r["refused"])
                or (r["expected"] == "拒" and r["refused"])
            ) else "✗"
            tag = "拒" if r["refused"] else "答"
            print(f"  [{i}/{len(rows)}] {flag} {r['qid']} {tag} "
                  f"recall={fmt_pct(r['recall'])} {r['latency_ms']}ms", flush=True)
            if r["error"]:
                print(f"        ERROR {r['error'][:120]}", flush=True)
            await asyncio.sleep(args.sleep)
    finally:
        await pool.close()

    s = summarize(results)
    payload = json.dumps({"summary": s, "details": results},
                         ensure_ascii=False, indent=2)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = OUT_DIR / "eval_report.json"
    try:
        report_path.write_text(payload, encoding="utf-8")
    except PermissionError:
        # 某些环境（如本机以受限账户运行）不允许写项目目录。
        # 评测本身已经跑完，不能因为写不了报告就白跑——降级到临时目录。
        import tempfile
        report_path = Path(tempfile.gettempdir()) / "eval_report.json"
        report_path.write_text(payload, encoding="utf-8")
        print(f"\n注意：项目目录不可写，报告已降级写入 {report_path}")

    print("\n" + "=" * 60)
    print(f"题目总数        {s['total']}（可答 {s['answerable']} / 应拒 {s['should_refuse']}）")
    print(f"Recall@5        {fmt_pct(s['recall_at_5'])}")
    print(f"引用命中率       {fmt_pct(s.get('citation_hit_rate'))}   ← 答案里是否含标注条款")
    print(f"引用精确率       {fmt_pct(s['citation_precision'])}   ← 引用中有多少是标注条款（一条答案引多条时偏低）")
    print(f"回答率          {fmt_pct(s['answer_rate'])}（{s['answered']}/{s['answerable']}）")
    print(f"误拒率          {fmt_pct(s['false_refusal_rate'])}（{s['false_refusal']} 题）")
    print(f"拒答准确率       {fmt_pct(s['refusal_accuracy'])}（{s['correct_refusal']}/{s['should_refuse']}）")
    print(f"平均耗时        {s['avg_latency_ms']:.0f}ms")
    print(f"异常            {s['errors']} 题")
    print("\n分层：")
    for k, v in s["by_stratum"].items():
        print(f"  {k:<6} n={v['n']:<3} recall={fmt_pct(v['recall'])} "
              f"作答={v['answered']} 正确拒={v['correct_refusal']}")
    print(f"\n报告 -> {OUT_DIR / 'eval_report.json'}")

    # 守卫：异常率过高说明这次评测根本没测出东西，不能当成功。
    # 2026-11-19 那次 CI，69 题里全挂，但因为脚本返回 0，任务显示绿色，
    # 实际跑出来的指标全是垃圾。评测失败必须让 CI 红。
    errors = s["errors"]
    total = s["total"] or 1
    if errors / total > 0.2:
        print(f"\n✗ 异常率 {errors}/{total} 超过 20%，本次评测结果不可信。")
        print("  常见原因：LLM_API_KEY / EMBEDDING_API_KEY 没配或失效、模型接口限流。")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
