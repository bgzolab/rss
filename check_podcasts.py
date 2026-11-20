#!/usr/bin/env python3
"""Check the podcast feeds in an OPML file.

Classifies every feed as active, stale (no episode within --max-age-days),
or dead (unreachable / not a parseable feed / no episodes). Feeds blocked by
geo/IP restrictions are checked via their Xiaoyuzhou page when possible.
Writes the report into README.md between markers and exports the active
subscriptions as OPML.

Usage:
    python3 check_podcasts.py
    python3 check_podcasts.py --max-age-days 365 --timeout 20
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import email.utils
import gzip
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UA = "Overcast/3.0 (+http://overcast.fm/; Apple Watch Podcasts/6.0)"
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
MAX_BYTES = 25 * 1024 * 1024
START = "<!-- feed-check:start -->"
END = "<!-- feed-check:end -->"
DATE_TAGS = {"pubdate", "published", "updated", "date", "lastbuilddate"}


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_date(text: str | None) -> dt.datetime | None:
    if not text:
        return None
    text = text.strip()
    try:
        stamp = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        stamp = None
    if stamp is None:
        try:
            stamp = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp


def fetch(url: str, timeout: float) -> tuple[bytes, str]:
    headers = {
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        "Accept-Encoding": "gzip",
    }
    last_error: Exception | None = None
    for attempt, agent in enumerate((UA, BROWSER_UA)):
        headers["User-Agent"] = agent
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = response.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise ValueError(f"响应超过 {MAX_BYTES // 1024 // 1024} MB")
                if response.headers.get("Content-Encoding", "").lower() == "gzip":
                    data = gzip.decompress(data)
                return data, response.geturl()
        except urllib.error.HTTPError as error:
            last_error = error
            if attempt == 0 and error.code in (403, 406, 429):
                continue
            break
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = error
            if attempt == 0:
                continue
            break
    raise last_error or RuntimeError("未知错误")


def latest_episode(root: ET.Element) -> dt.datetime | None:
    dates: list[dt.datetime] = []
    for child in root.iter():
        if local_name(child.tag) in ("item", "entry"):
            for field in child:
                if local_name(field.tag) in ("pubdate", "published", "updated", "date"):
                    stamp = parse_date(field.text)
                    if stamp:
                        dates.append(stamp)
    if dates:
        return max(dates)
    for field in root.iter():
        if local_name(field.tag) in DATE_TAGS:
            stamp = parse_date(field.text)
            if stamp:
                return stamp
    return None


def describe_error(error: Exception) -> str:
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code}"
    text = str(getattr(error, "reason", error))
    if "CERTIFICATE_VERIFY_FAILED" in text:
        return "HTTPS 证书已过期或无效"
    if "timed out" in text:
        return "连接超时"
    if "UNEXPECTED_EOF" in text or "SSL" in text or "TLS" in text:
        return "TLS 连接失败"
    if "Name or service not known" in text or "nodename nor servname" in text:
        return "域名无法解析"
    return f"请求失败：{text}"


def block_page_reason(data: bytes) -> str | None:
    sample = data[:4096].decode("utf-8", "replace").lower()
    if "<html" not in sample and "<!doctype html" not in sample:
        return None
    if "just a moment" in sample or "cf-chl" in sample:
        return "被 Cloudflare 拦截（403），无法验证"
    if "ip acl" in sample or "无法访问" in sample or "403 forbidden" in sample:
        return "被 IP/地区限制（403），无法验证"
    return None


def latest_from_xiaoyuzhou(html_url: str, timeout: float) -> dt.datetime | None:
    host = urllib.parse.urlparse(html_url).netloc.lower()
    if not host.endswith("xiaoyuzhoufm.com"):
        return None
    try:
        data, _ = fetch(html_url, timeout)
    except Exception:  # noqa: BLE001 - fallback is best effort
        return None
    text = data.decode("utf-8", "replace")
    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', text, re.S)
    if match:
        try:
            podcast = json.loads(match.group(1))["props"]["pageProps"]["podcast"]
            stamp = parse_date(podcast.get("latestEpisodePubDate"))
            if stamp:
                return stamp
        except (ValueError, KeyError, TypeError):
            pass
    dates = [parse_date(value) for value in re.findall(r'"pubDate"\s*:\s*"([^"]+)"', text)]
    dates = [date for date in dates if date]
    return max(dates) if dates else None


def finish_with_latest(result: dict, latest: dt.datetime, now: dt.datetime, max_age_days: int, note: str = "") -> dict:
    result["latest"] = latest
    result["age"] = (now - latest).days
    result["status"] = "stale" if result["age"] > max_age_days else "ok"
    result["detail"] = f"{note}{result['age']} 天前"
    return result


def mark_blocked(result: dict, feed: dict[str, str], reason: str, timeout: float, max_age_days: int, now: dt.datetime) -> dict:
    latest = latest_from_xiaoyuzhou(feed.get("htmlUrl", ""), timeout)
    if latest:
        return finish_with_latest(result, latest, now, max_age_days, f"{reason}；小宇宙页面显示 ")
    result["status"] = "blocked"
    result["detail"] = reason
    return result


def check(feed: dict[str, str], timeout: float, max_age_days: int, now: dt.datetime) -> dict:
    name = feed.get("text") or feed.get("title") or feed["xmlUrl"]
    result = {"name": name, "url": feed["xmlUrl"], "feed": feed, "status": "dead", "detail": "", "latest": None}
    try:
        data, _ = fetch(feed["xmlUrl"], timeout)
    except urllib.error.HTTPError as error:
        if error.code in (403, 429):
            return mark_blocked(result, feed, f"HTTP {error.code}，无法验证", timeout, max_age_days, now)
        result["detail"] = describe_error(error)
        return result
    except Exception as error:  # noqa: BLE001 - every failure is a dead feed
        result["detail"] = describe_error(error)
        return result

    reason = block_page_reason(data)
    if reason:
        return mark_blocked(result, feed, reason, timeout, max_age_days, now)

    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        result["detail"] = "返回的不是有效 XML"
        return result

    if local_name(root.tag) not in ("rss", "feed", "rdf"):
        result["detail"] = f"不是 RSS/Atom（根节点 {local_name(root.tag)}）"
        return result

    items = [node for node in root.iter() if local_name(node.tag) in ("item", "entry")]
    if not items:
        result["detail"] = "订阅为空（没有单集）"
        return result

    latest = latest_episode(root)
    if latest is None:
        result["status"] = "undated"
        result["detail"] = "有单集但找不到日期"
        return result
    return finish_with_latest(result, latest, now, max_age_days)


def md(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render(results: list[dict], max_age_days: int, generated: dt.datetime) -> str:
    ok = [r for r in results if r["status"] == "ok"]
    stale = sorted([r for r in results if r["status"] == "stale"], key=lambda r: r["latest"], reverse=True)
    dead = [r for r in results if r["status"] == "dead"]
    blocked = [r for r in results if r["status"] == "blocked"]
    undated = [r for r in results if r["status"] == "undated"]

    lines = [
        "## 订阅健康检查",
        "",
        f"> 由 `check_podcasts.py` 生成于 {generated:%Y-%m-%d %H:%M} UTC。"
        f"判定标准：可访问、可解析为 RSS/Atom、且最近一集在 {max_age_days} 天内。",
        "",
        f"共 **{len(results)}** 个订阅：活跃 **{len(ok)}**，"
        f"{max_age_days} 天未更新 **{len(stale)}**，失效 **{len(dead)}**，"
        f"被网络限制、无法验证 **{len(blocked)}**，无法判断 **{len(undated)}**。",
        "",
        "活跃订阅已导出到 [`snipd-active.opml`](snipd-active.opml)，可直接导入 Overcast。"
        "小宇宙（`feed.xyzfm.space`）的 Feed 会拒绝境外 IP，脚本改用节目页面判断更新时间。"
        "重新检测：`python3 check_podcasts.py`。",
        "",
        "### 失效",
        "",
    ]
    if dead:
        lines += ["| 播客 | Feed | 原因 |", "| --- | --- | --- |"]
        lines += [f"| {md(r['name'])} | <{r['url']}> | {md(r['detail'])} |" for r in dead]
    else:
        lines.append("无。")

    lines += ["", f"### {max_age_days} 天未更新", ""]
    if stale:
        lines += ["| 播客 | Feed | 最后更新 |", "| --- | --- | --- |"]
        lines += [
            f"| {md(r['name'])} | <{r['url']}> | {r['latest']:%Y-%m-%d}（{r['age']} 天前） |"
            for r in stale
        ]
    else:
        lines.append("无。")

    if blocked:
        lines += ["", "### 无法验证", "", "| 播客 | Feed | 原因 |", "| --- | --- | --- |"]
        lines += [f"| {md(r['name'])} | <{r['url']}> | {md(r['detail'])} |" for r in blocked]

    if undated:
        lines += ["", "### 无法判断", "", "| 播客 | Feed | 原因 |", "| --- | --- | --- |"]
        lines += [f"| {md(r['name'])} | <{r['url']}> | {md(r['detail'])} |" for r in undated]

    return "\n".join(lines)


def write_readme(path: Path, section: str) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else "# RSS Subscription\n"
    block = f"{START}\n{section}\n{END}"
    pattern = re.compile(re.escape(START) + r".*?" + re.escape(END), re.S)
    if pattern.search(text):
        text = pattern.sub(lambda _: block, text)
    else:
        text = text.rstrip() + "\n\n" + block + "\n"
    path.write_text(text, encoding="utf-8")


def write_opml(path: Path, feeds: list[dict]) -> None:
    opml = ET.Element("opml", {"version": "1.0"})
    head = ET.SubElement(opml, "head")
    ET.SubElement(head, "title").text = "Overcast Podcast Subscriptions"
    body = ET.SubElement(opml, "body")
    for feed in feeds:
        attributes = {key: value.strip() for key, value in feed.items()}
        ET.SubElement(body, "outline", attributes)
    ET.indent(opml)
    path.write_text('<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(opml, encoding="unicode") + "\n", encoding="utf-8")


def load_opml(path: Path) -> list[dict[str, str]]:
    root = ET.parse(path).getroot()
    feeds, seen = [], set()
    for outline in root.iter("outline"):
        url = (outline.get("xmlUrl") or "").strip()
        if url and url not in seen:
            seen.add(url)
            feed = dict(outline.attrib)
            feed["xmlUrl"] = url
            feeds.append(feed)
    return feeds


def self_test() -> None:
    rss = b'<rss><channel><item><pubDate>Wed, 01 Jan 2025 00:00:00 GMT</pubDate></item></channel></rss>'
    expected = dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc)
    assert latest_episode(ET.fromstring(rss)) == expected
    assert parse_date("2025-01-01T00:00:00Z") == expected
    assert parse_date("not a date") is None
    assert block_page_reason("<html>403 · 无法访问</html>".encode())
    assert block_page_reason(b"<rss><channel/></rss>") is None
    print("self-test ok", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opml", default=str(ROOT / "snipd.opml"))
    parser.add_argument("--output", default=str(ROOT / "snipd-active.opml"))
    parser.add_argument("--readme", default=str(ROOT / "README.md"))
    parser.add_argument("--max-age-days", type=int, default=365)
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--self-test", action="store_true", help="只运行内置自检")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return 0

    feeds = load_opml(Path(args.opml))
    if not feeds:
        print("OPML 里没有 xmlUrl", file=sys.stderr)
        return 1

    now = dt.datetime.now(dt.timezone.utc)
    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(check, feed, args.timeout, args.max_age_days, now): feed for feed in feeds}
        for index, future in enumerate(concurrent.futures.as_completed(futures), 1):
            result = future.result()
            results.append(result)
            print(f"[{index}/{len(feeds)}] {result['status']:7} {result['name']} - {result['detail']}", file=sys.stderr)

    results.sort(key=lambda r: str(r["name"]).lower())
    write_readme(Path(args.readme), render(results, args.max_age_days, now))
    write_opml(Path(args.output), [r["feed"] for r in results if r["status"] == "ok"])

    counts = {status: sum(r["status"] == status for r in results) for status in ("ok", "stale", "dead", "blocked", "undated")}
    print(f"ok={counts['ok']} stale={counts['stale']} dead={counts['dead']} blocked={counts['blocked']} undated={counts['undated']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
