"""抓取安全层：robots 检查 + 数量校验 + 运行留痕。

为什么先做这个再做爬虫：
爬虫最危险的失败不是报错，而是**静默地抓到 0 条**。它不会抛异常，
只会返回空列表；流水线检测不到变化，系统继续用旧数据自信作答。
所以接爬虫之前，先把「抓多少算异常」「允许抓哪些路径」「这次抓取的痕迹」定下来。

三条原则：
1. robots 优先：站点禁止的路径一律不抓；没有 robots.txt 才视为未限制。
2. 数量骤降即中止：本次数量低于上次成功量的 70%，不写库、直接告警。
   宁可这次不更新，也不能把好数据覆盖成空的。
3. 每次运行留痕：谁在什么时候抓了多少、有没有中止、为什么。
"""
from __future__ import annotations

import logging
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# 抓取方标识：写清楚是谁、干什么用的。冒充浏览器更不礼貌，也更容易被封。
USER_AGENT = (
    "policy-qa-bot/0.1 (personal research project; "
    "respects robots.txt; 1 req/sec)"
)

# 数量骤降阈值：低于上次成功的 70% 就中止
ABORT_RATIO = 0.7

# 抓取间隔（秒）。政府站点不是 CDN，1 秒一次已经很克制。
CRAWL_DELAY = 1.0


@dataclass
class RobotsVerdict:
    host: str
    allowed: bool
    has_robots: bool
    reason: str
    crawl_delay: float | None = None


@dataclass
class GuardResult:
    ok: bool
    reason: str = ""
    checks: list[str] = field(default_factory=list)


async def check_robots(
    url: str, client: httpx.AsyncClient | None = None
) -> RobotsVerdict:
    """检查某 URL 是否被 robots.txt 允许抓取。

    404 / 取不到 → 视为「没有限制」，但会在 reason 里写明，
    因为「没找到规则」和「规则允许」在合规上不是一回事。
    """
    parsed = urlparse(url)
    host = f"{parsed.scheme}://{parsed.netloc}"
    robots_url = host + "/robots.txt"

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=20, follow_redirects=True)
    try:
        resp = await client.get(robots_url, headers={"User-Agent": USER_AGENT})
    except Exception as exc:  # noqa: BLE001
        logger.warning("取 robots.txt 失败（按未限制处理）：%s", exc)
        return RobotsVerdict(host, True, False, f"取不到 robots.txt：{exc}", None)
    finally:
        if owns_client:
            await client.aclose()

    if resp.status_code != 200:
        return RobotsVerdict(
            host, True, False,
            f"robots.txt 返回 {resp.status_code}，视为未设置限制", None)

    rp = urllib.robotparser.RobotFileParser()
    rp.parse(resp.text.splitlines())
    agent = USER_AGENT.split("/")[0]
    allowed = rp.can_fetch(USER_AGENT, url) and rp.can_fetch(agent, url)
    delay = rp.crawl_delay(USER_AGENT) or rp.crawl_delay(agent)
    reason = "robots.txt 允许" if allowed else "robots.txt 明确禁止该路径"
    return RobotsVerdict(host, allowed, True, reason,
                         float(delay) if delay else None)


def check_count(current: int, last_ok: int | None, ratio: float = ABORT_RATIO) -> GuardResult:
    """数量校验：本次抓取量是否合理。

    last_ok 为 None（第一次抓）时不做比较，但要求至少抓到 1 条——
    一条都没抓到的运行永远不该写库。
    """
    if current <= 0:
        return GuardResult(False, "抓到 0 条，中止（不写库）")
    if last_ok is None:
        return GuardResult(True, f"首次抓取 {current} 条，无历史可比")
    if last_ok > 0 and current < last_ok * ratio:
        return GuardResult(
            False,
            f"本次 {current} 条 < 上次成功 {last_ok} 条的 {ratio:.0%}，"
            f"疑似站点改版或抓取失败，中止（不写库）")
    delta = current - last_ok
    return GuardResult(True, f"本次 {current} 条，上次 {last_ok} 条（{delta:+d}）")


LAST_OK_SQL = """
    SELECT docs_found FROM crawl_runs
     WHERE ok = TRUE AND ($1::text IS NULL OR source = $1)
     ORDER BY id DESC LIMIT 1
"""


async def last_ok_count(pool, source: str | None = None) -> int | None:
    try:
        return await pool.fetchval(LAST_OK_SQL, source)
    except Exception as exc:  # noqa: BLE001
        logger.warning("读上次抓取量失败（按首次处理）：%s", exc)
        return None


async def start_run(pool, source: str) -> int | None:
    """开始一次抓取，返回 run id。失败返回 None（不阻塞流程）。"""
    try:
        return await pool.fetchval(
            "INSERT INTO crawl_runs (source) VALUES ($1) RETURNING id", source)
    except Exception as exc:  # noqa: BLE001
        logger.warning("写 crawl_runs 失败：%s", exc)
        return None


async def finish_run(pool, run_id: int | None, *, docs_found: int,
                     docs_stored: int = 0, ok: bool = True,
                     aborted_reason: str | None = None,
                     detail: dict | None = None) -> None:
    if run_id is None:
        return
    import json
    try:
        await pool.execute(
            """UPDATE crawl_runs
                  SET finished_at = now(), docs_found = $2, docs_stored = $3,
                      ok = $4, aborted_reason = $5, detail = $6::jsonb
                WHERE id = $1""",
            run_id, docs_found, docs_stored, ok, aborted_reason,
            json.dumps(detail or {}, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        logger.warning("更新 crawl_runs 失败：%s", exc)


DDL = """
CREATE TABLE IF NOT EXISTS crawl_runs (
    id            BIGSERIAL PRIMARY KEY,
    started_at    TIMESTAMPTZ DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    source        TEXT,
    docs_found    INT DEFAULT 0,
    docs_stored   INT DEFAULT 0,
    ok            BOOLEAN DEFAULT FALSE,
    aborted_reason TEXT,
    detail        JSONB
);
CREATE INDEX IF NOT EXISTS idx_crawl_runs_time ON crawl_runs (started_at DESC);

ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_hash TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS crawl_run_id BIGINT;
CREATE INDEX IF NOT EXISTS idx_doc_hash ON documents (content_hash);
"""
