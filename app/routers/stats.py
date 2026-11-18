"""使用统计页：给作者自己看的看板。

为什么不加进主站导航：这是内部数据，用户不需要看到。
用一个独立的 STATS_KEY 保护，和访问口令分开——
就算口令泄露了，统计页也进不来。
"""
import hmac
import html
import logging
import os
from datetime import date

import asyncpg
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.db import get_pool

logger = logging.getLogger(__name__)

router = APIRouter(tags=["stats"])

PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>使用统计</title>
<style>
  :root{{--paper:#faf8f5;--card:#fff;--ink:#1a1a18;--ink2:#57534e;--ink3:#8a857e;
        --line:#e6e1d8;--accent:#1f5f57;--accent-soft:#ecf3f1}}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--paper);color:var(--ink);line-height:1.65;
       font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}}
  .wrap{{max-width:760px;margin:0 auto;padding:28px 18px 60px}}
  h1{{font-size:22px;margin:0 0 4px}}
  .sub{{color:var(--ink3);font-size:12.5px;margin-bottom:22px}}
  .cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:22px}}
  .card{{background:var(--card);border:1px solid var(--line);border-radius:10px;
        padding:14px 12px;text-align:center}}
  .card .n{{font-size:26px;font-weight:700;color:var(--accent);line-height:1.2}}
  .card .k{{font-size:11.5px;color:var(--ink3);margin-top:4px}}
  h2{{font-size:14px;margin:22px 0 10px;color:var(--ink2);
     border-left:3px solid var(--accent);padding-left:9px}}
  table{{width:100%;border-collapse:collapse;background:var(--card);
        border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:13px}}
  th,td{{padding:9px 12px;text-align:left;border-bottom:1px solid var(--line)}}
  th{{background:var(--accent-soft);font-weight:600;color:var(--ink2);font-size:12px}}
  tr:last-child td{{border-bottom:0}}
  .dim{{color:var(--ink3)}}
  .bar{{height:6px;background:var(--accent-soft);border-radius:3px;overflow:hidden;margin-top:4px}}
  .bar i{{display:block;height:100%;background:var(--accent);border-radius:3px}}
  @media(max-width:560px){{.cards{{grid-template-columns:repeat(2,1fr)}}}}
</style></head><body><div class="wrap">
<h1>使用统计</h1>
<div class="sub">生成于 {now} · 访问数据从 2026-11-18 起才有（埋点是后加的）</div>
<div class="cards">
  <div class="card"><div class="n">{uv7}</div><div class="k">近 7 天访客</div></div>
  <div class="card"><div class="n">{pv7}</div><div class="k">近 7 天浏览</div></div>
  <div class="card"><div class="n">{q7}</div><div class="k">近 7 天提问</div></div>
  <div class="card"><div class="n">{uv_today}</div><div class="k">今天访客</div></div>
</div>

<h2>按天</h2>
<table><tr><th>日期</th><th>访客</th><th>浏览</th><th>提问</th><th></th></tr>
{daily}
</table>

<h2>每个访客</h2>
<table><tr><th>访客</th><th>提问</th><th>首次</th><th>最近</th></tr>
{visitors}
</table>

<h2>最近被问的问题</h2>
<table><tr><th>问题</th><th>次数</th></tr>
{questions}
</table>

<h2>来源</h2>
<table><tr><th>来源</th><th>次数</th></tr>
{sources}
</table>
</div></body></html>"""


async def _collect(pool: asyncpg.Pool) -> dict:
    """一次把所有数据取回来。查询都很轻（表小、有索引）。"""
    d = {}

    r = await pool.fetchrow("""
        SELECT count(*) AS pv, count(DISTINCT visitor_hash) AS uv,
               count(*) FILTER (WHERE mobile) AS mobile
          FROM visits WHERE visited_at > now() - interval '7 days'""")
    d["pv7"], d["uv7"], d["mobile"] = r["pv"], r["uv"], r["mobile"]

    d["uv_today"] = await pool.fetchval("""
        SELECT count(DISTINCT visitor_hash) FROM visits
         WHERE visited_at >= $1::date""", date.today())

    d["q7"] = await pool.fetchval("""
        SELECT count(*) FROM query_log WHERE created_at > now() - interval '7 days'""")

    d["daily"] = await pool.fetch("""
        SELECT d::date AS day,
               (SELECT count(DISTINCT visitor_hash) FROM visits
                 WHERE visited_at::date = d::date) AS uv,
               (SELECT count(*) FROM visits WHERE visited_at::date = d::date) AS pv,
               (SELECT count(*) FROM query_log WHERE created_at::date = d::date) AS q
          FROM generate_series(now() - interval '6 days', now(), interval '1 day') AS d
         ORDER BY 1 DESC""")

    d["visitors"] = await pool.fetch("""
        SELECT visitor_hash, count(*) AS n,
               min(created_at) AS first_seen, max(created_at) AS last_seen
          FROM query_log
         WHERE created_at > now() - interval '7 days' AND visitor_hash IS NOT NULL
         GROUP BY 1 ORDER BY n DESC LIMIT 15""")

    d["questions"] = await pool.fetch("""
        SELECT question, count(*) AS n FROM query_log
         WHERE created_at > now() - interval '7 days'
         GROUP BY 1 ORDER BY n DESC LIMIT 12""")

    d["sources"] = await pool.fetch("""
        SELECT coalesce(nullif(referer_host,''), '（直接打开）') AS src, count(*) AS n
          FROM visits WHERE visited_at > now() - interval '7 days'
         GROUP BY 1 ORDER BY n DESC LIMIT 8""")
    return d


@router.get("/stats", response_class=HTMLResponse)
async def stats(
    request: Request,
    key: str = Query("", description="统计页口令"),
    pool: asyncpg.Pool = Depends(get_pool),
) -> HTMLResponse:
    settings = get_settings()
    expect = (settings.stats_key or os.getenv("STATS_KEY") or "").strip()

    if not expect:
        return HTMLResponse("<h1>统计页未启用</h1>"
                            "<p>请在环境变量里设置 STATS_KEY</p>", status_code=404)
    if not hmac.compare_digest(key, expect):
        logger.info("统计页口令错误，来自 %s", request.headers.get("user-agent", "")[:40])
        return HTMLResponse("<h1>口令不对</h1><p>用法：/stats?key=你的STATS_KEY</p>",
                            status_code=401)

    data = await _collect(pool)

    mx_pv = max([r["pv"] for r in data["daily"]] or [1]) or 1
    daily = "".join(
        f"<tr><td>{r['day']}</td><td>{r['uv']}</td><td>{r['pv']}</td>"
        f"<td>{r['q']}</td><td style='width:90px'>"
        f"<div class='bar'><i style='width:{max(2, round(r['pv']/mx_pv*100))}%'></i></div>"
        f"</td></tr>"
        for r in data["daily"]
    ) or "<tr><td colspan=5 class='dim'>还没有数据</td></tr>"

    visitors = "".join(
        f"<tr><td>{html.escape(r['visitor_hash'])}</td><td>{r['n']}</td>"
        f"<td class='dim'>{r['first_seen']:%m-%d %H:%M}</td>"
        f"<td class='dim'>{r['last_seen']:%m-%d %H:%M}</td></tr>"
        for r in data["visitors"]
    ) or "<tr><td colspan=4 class='dim'>还没有访客</td></tr>"

    questions = "".join(
        f"<tr><td>{html.escape(r['question'][:52])}</td><td>{r['n']}</td></tr>"
        for r in data["questions"]
    ) or "<tr><td colspan=2 class='dim'>还没有提问</td></tr>"

    sources = "".join(
        f"<tr><td>{html.escape(r['src'][:44])}</td><td>{r['n']}</td></tr>"
        for r in data["sources"]
    ) or "<tr><td colspan=2 class='dim'>还没有来源数据</td></tr>"

    from datetime import datetime
    return HTMLResponse(PAGE.format(
        now=datetime.now().strftime("%Y-%m-%d %H:%M"),
        uv7=data["uv7"], pv7=data["pv7"], q7=data["q7"],
        uv_today=data["uv_today"] or 0,
        daily=daily, visitors=visitors, questions=questions, sources=sources,
    ))
