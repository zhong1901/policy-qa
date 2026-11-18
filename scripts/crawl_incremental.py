"""增量爬虫：只抓「新出现的」和「被抽查到的」文档，不重爬全站。

为什么是增量而不是全量重爬：
全站 1301 篇要 1474 次请求、约 25 分钟。而每天真正新增的政策通常只有 0–3 篇。
全量重爬既慢又给源站增加不必要的压力。增量的做法是：只翻列表页（4 栏目 × 3 页 = 12 次请求），
发现新 URL 才去抓详情。

正文提取逻辑**必须**和当初入库时用的是同一套（crawl_details.py 的 extract），
否则同一篇文档提取出来的文本会有细微差异，哈希对不上，全部被误判成「内容变更」。

用法：
    python scripts/crawl_incremental.py --pages 2              # 抓列表，只抓新文档
    python scripts/crawl_incremental.py --pages 2 --recheck 20 # 额外抽查 20 篇老文档比对哈希
    python scripts/crawl_incremental.py --verify-one <url>     # 验证提取器与库内一致
    python scripts/crawl_incremental.py --dry-run              # 只看会发现什么，不抓详情
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import asyncpg
import lxml.html as LH
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

BASE = "https://rsj.sh.gov.cn"
# 输出目录可配置：本地默认写到仓库下的 crawl-out，
# CI 里由环境变量指定。写死 Windows 路径会导致 GitHub Actions 直接跑不起来。
OUT_DIR = Path(os.environ.get("CRAWL_OUT_DIR") or (ROOT / "crawl-out"))

# 抓取方标识：写明用途。注意这里**不冒充浏览器**——
# 冒充 UA 既不诚实，也更容易在对方升级反爬时被一起封掉。
UA = ("policy-qa-bot/0.1 (personal research project; "
      "respects robots.txt; 1 req/sec)")

DELAY = 1.0   # 请求间隔，秒

SECTIONS = [
    ("规范性文件", "/tgwgfx_17726/"),
    ("其他文件", "/tgwqt_17736/"),
    ("政策解读", "/tzcjd_17351/"),
    ("民声直通车", "/tmsztc_17502/"),
]

DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
DETAIL_RE = re.compile(r"^/[a-z]+_\d+(?:_\d+)?/(\d{8})/[\w]+\.html$")


# ────────────────── 抓取与解析（与当初入库用的是同一套） ──────────────────
def decode(raw: bytes) -> str:
    m = re.search(rb'charset\s*=\s*["\']?\s*([\w-]+)', raw[:3000], re.I)
    enc = m.group(1).decode().lower() if m else "utf-8"
    if enc in ("gb2312", "gbk"):
        enc = "gb18030"
    for e in (enc, "gb18030", "utf-8"):
        try:
            return raw.decode(e)
        except Exception:  # noqa: BLE001
            continue
    return raw.decode("utf-8", "ignore")


def fetch(url: str, tries: int = 3) -> str:
    last: Exception | None = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Accept-Encoding": "gzip, deflate",
                "Referer": BASE + "/",
            })
            r = urllib.request.urlopen(req, timeout=30)
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return decode(raw)
        except urllib.error.HTTPError as e:
            if e.code in (404, 403):
                raise
            last = e
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(2.0 * (i + 1))
    raise last if last else RuntimeError("fetch failed")


BOILER = re.compile(
    r"(扫一扫在手机打开当前页|打印本页|关闭窗口|function\s+getAllImg.*$|"
    r"分享到|字体[：:]\s*\[?大\s*中\s*小|【字体：大 中 小】)")


def clean_text(t: str) -> str:
    t = re.sub(r"<script.*?</script>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<style.*?</style>", " ", t, flags=re.S | re.I)
    t = re.sub(r"\s+", " ", t)
    t = BOILER.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def extract(h: str) -> dict:
    doc = LH.fromstring(h)
    for bad in doc.xpath("//script|//style"):
        bad.getparent().remove(bad)

    container = None
    for xp in ['//div[contains(@class,"TRS_Editor")]',
               '//*[@id="zhengwen"]',
               '//div[contains(@class,"article-content")]',
               '//div[contains(@class,"content")]']:
        nodes = doc.xpath(xp)
        if nodes:
            cand = max(nodes, key=lambda n: len(n.text_content()))
            if len(cand.text_content()) > 80:
                container = cand
                break
    if container is None:
        body = doc.xpath("//body")
        container = body[0] if body else doc

    text = clean_text(container.text_content())

    title = ""
    tt = doc.xpath("//title/text()")
    if tt:
        title = re.split(r"_[^_]*_上海市人力资源和社会保障局$", tt[0].strip())[0]
    h1 = doc.xpath("//h1//text()") or doc.xpath('//div[contains(@class,"title")]//text()')
    if h1:
        cand = re.sub(r"\s+", " ", "".join(h1)).strip()
        if 4 < len(cand) < 200:
            title = cand

    pub = ""
    m = re.search(r"(?:发布时间|发布日期|成文日期)[：:]\s*(\d{4}-\d{2}-\d{2})", text)
    if m:
        pub = m.group(1)

    eff = ""
    m = re.search(r"自\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日起(?:施行|实施|执行)", text)
    if m:
        eff = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    doc_no = ""
    m = re.search(r"([\u4e00-\u9fa5]{2,10}〔\d{4}〕\d+号)", text)
    if m:
        doc_no = m.group(1)

    return {"title": title, "body": text, "body_len": len(text),
            "publish_date": pub, "effective_from": eff, "doc_no": doc_no}


def parse_list(html: str, section: str) -> list[dict]:
    out, seen = [], set()
    doc = LH.fromstring(html)
    for a in doc.xpath("//a[@href]"):
        href = (a.get("href") or "").strip()
        m = DETAIL_RE.match(href)
        if not m or href in seen:
            continue
        seen.add(href)
        title = re.sub(r"\s+", " ", (a.get("title") or a.text_content())).strip()
        if len(title) < 4:
            continue
        d = m.group(1)
        out.append({"section": section, "url": BASE + href, "title": title,
                    "publish_date": f"{d[:4]}-{d[4:6]}-{d[6:]}"})
    return out


def md5_text(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


# ────────────────── 主流程 ──────────────────
def connect_kwargs(dsn: str) -> dict:
    kw = {"dsn": dsn}
    if "supabase" in dsn or "sslmode=require" in dsn:
        kw["ssl"] = "require"
    return kw


async def load_known(conn) -> dict[str, str]:
    rows = await conn.fetch(
        "SELECT source_url, content_hash FROM documents WHERE source_url IS NOT NULL")
    return {r["source_url"]: r["content_hash"] for r in rows}


async def verify_one(conn, url: str) -> int:
    """验证：同一个 URL，用现在的提取器算出的哈希是否等于库里的。

    这一步很重要——如果对不上，说明提取逻辑与当初入库时不一致，
    那么「增量检测」会把所有文档都判成已变更，整条流水线就废了。
    """
    row = await conn.fetchrow(
        "SELECT source_url, title, body, content_hash FROM documents WHERE source_url=$1",
        url)
    if row is None:
        print(f"库里没有这个 URL：{url}")
        return 1
    print(f"抓取 {url} …")
    html = fetch(url)
    got = extract(html)
    h_now, h_db = md5_text(got["body"]), row["content_hash"]
    print(f"  标题  库内：{row['title'][:44]}")
    print(f"        本次：{got['title'][:44]}")
    print(f"  正文字数  库内 {len(row['body'] or '')}  本次 {got['body_len']}")
    print(f"  哈希  库内 {h_db}")
    print(f"        本次 {h_now}")
    if h_now == h_db:
        print("\n✓ 一致：提取器与入库时相同，可以放心做增量比对。")
        return 0
    print("\n✗ 不一致：提取器或页面结构变了。别急着跑增量，先查清楚差异。")
    return 1


async def main() -> int:
    ap = argparse.ArgumentParser(description="增量爬虫")
    ap.add_argument("--pages", type=int, default=2, help="每个栏目翻几页（默认 2）")
    ap.add_argument("--recheck", type=int, default=0, help="额外抽查多少篇老文档比对哈希")
    ap.add_argument("--verify-one", metavar="URL", help="只验证某 URL 的提取一致性")
    ap.add_argument("--dry-run", action="store_true", help="只发现，不抓详情")
    args = ap.parse_args()

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("缺少 DATABASE_URL")
        return 2
    conn = await asyncpg.connect(**connect_kwargs(dsn))
    try:
        if args.verify_one:
            return await verify_one(conn, args.verify_one)

        known = await load_known(conn)
        print(f"库内已有 {len(known)} 篇\n")

        # 1) 翻列表页
        candidates: dict[str, dict] = {}
        pages_ok = pages_fail = 0
        for section, path in SECTIONS:
            for p in range(1, args.pages + 1):
                url = f"{BASE}{path}index.html" if p == 1 else f"{BASE}{path}index_{p}.html"
                try:
                    html = fetch(url)
                except urllib.error.HTTPError as e:
                    print(f"  [{section}] 第 {p} 页 HTTP {e.code}，跳过")
                    pages_fail += 1
                    continue
                except Exception as e:  # noqa: BLE001
                    print(f"  [{section}] 第 {p} 页失败：{e}")
                    pages_fail += 1
                    continue
                rows = parse_list(html, section)
                for r in rows:
                    candidates.setdefault(r["url"], r)
                pages_ok += 1
                print(f"  [{section}] 第 {p} 页 → {len(rows)} 条")
                time.sleep(DELAY)

        # ── 抓取阶段的数量守卫 ──
        # 增量抓取只抓新文档，所以「发现条数」不能和库内总数比。
        # 真正该盯的是**列表页抓取成功率**——站点改版或反爬升级会直接体现为整页抓不到。
        expected = len(SECTIONS) * args.pages
        if pages_ok < expected * 0.8:
            print(f"\n✗ 列表页只成功 {pages_ok}/{expected}，"
                  f"疑似站点改版或反爬拦截。本次中止，不写任何结果。")
            return 1
        print(f"\n列表页成功 {pages_ok}/{expected} 页，失败 {pages_fail} 页")

        print(f"列表页共发现 {len(candidates)} 个详情链接")
        new_urls = [u for u in candidates if u not in known]
        print(f"其中新文档 {len(new_urls)} 个")

        if args.dry_run:
            for u in new_urls[:20]:
                print(f"  + {candidates[u]['title'][:50]}  {u}")
            return 0

        # 2) 抽查老文档（可选）
        recheck_urls: list[str] = []
        if args.recheck > 0:
            pool = [u for u in candidates if u in known]
            recheck_urls = random.sample(pool, min(args.recheck, len(pool)))
            print(f"抽查老文档 {len(recheck_urls)} 篇比对哈希")

        # 3) 抓详情
        results: list[dict] = []
        new_docs: list[dict] = []
        for i, u in enumerate(new_urls + recheck_urls, 1):
            meta = candidates.get(u) or {}
            try:
                html = fetch(u)
                got = extract(html)
            except Exception as e:  # noqa: BLE001
                print(f"  [{i}] 失败 {u}：{e}")
                continue
            h = md5_text(got["body"])
            is_new = u not in known
            results.append({
                "url": u, "title": got["title"] or meta.get("title"),
                "section": meta.get("section"),
                "content_hash": h,
                "body_len": got["body_len"],
                "doc_no": got["doc_no"],
                "publish_date": got["publish_date"] or meta.get("publish_date"),
                "effective_from": got["effective_from"],
                "changed": (not is_new) and h != known.get(u),
            })
            if is_new:
                new_docs.append({**results[-1], "body": got["body"]})
            flag = "新增" if is_new else ("已变更" if results[-1]["changed"] else "未变")
            print(f"  [{i}/{len(new_urls)+len(recheck_urls)}] {flag}  {got['title'][:38]}")
            time.sleep(DELAY)

        # 4) 输出
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (OUT_DIR / f"crawl_result-{stamp}.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        (OUT_DIR / "crawl_result.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        if new_docs:
            with open(OUT_DIR / "new_docs.jsonl", "a", encoding="utf-8") as f:
                for d in new_docs:
                    f.write(json.dumps(d, ensure_ascii=False) + "\n")

        print(f"\n结果已写入 {OUT_DIR}\\crawl_result.json")
        print(f"  新文档 {len(new_docs)} 篇（正文追加到 new_docs.jsonl）")
        print(f"  抽查中判定已变更 {sum(1 for r in results if r['changed'])} 篇")
        print("\n下一步：python scripts/update_pipeline.py --incoming "
              f"\"{OUT_DIR / 'crawl_result.json'}\" --regress")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
