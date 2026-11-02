"""Collect major US stock market headlines from public RSS feeds."""
from __future__ import annotations

import email.utils
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .net import get

FEEDS = {
    "MarketWatch Top": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "MarketWatch Pulse": "https://feeds.content.dowjones.io/public/rss/mw_marketpulse",
    "WSJ Markets": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    "Nasdaq Markets": "https://www.nasdaq.com/feed/rssoutbound?category=Markets",
    "Seeking Alpha Currents": "https://seekingalpha.com/market_currents.xml",
    "Investing.com Stocks": "https://www.investing.com/rss/news_25.rss",
}

TAG_RE = re.compile(r"<[^>]+>")


def _text(el, tag: str) -> str:
    node = el.find(tag)
    return (node.text or "").strip() if node is not None and node.text else ""


def _parse_date(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fetch_feed(name: str, url: str) -> list[dict]:
    root = ET.fromstring(get(url))
    items = []
    for item in root.iter("item"):
        title = _text(item, "title")
        if not title:
            continue
        desc = TAG_RE.sub("", _text(item, "description"))[:400]
        dt = _parse_date(_text(item, "pubDate"))
        items.append({
            "source": name,
            "title": title,
            "link": _text(item, "link"),
            "summary": desc,
            "published": dt.isoformat() if dt else None,
        })
    return items


PER_SOURCE = 15


def collect(hours: int = 6) -> tuple[list[dict], dict[str, str]]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    seen: set[str] = set()
    out: list[dict] = []
    errors: dict[str, str] = {}
    for name, url in FEEDS.items():
        try:
            items = fetch_feed(name, url)
        except Exception as exc:  # noqa: BLE001
            errors[name] = str(exc)[:200]
            continue
        kept = 0
        for it in items:
            if kept >= PER_SOURCE:
                break
            key = re.sub(r"\W+", "", it["title"].lower())[:80]
            if key in seen:
                continue
            if it["published"] and datetime.fromisoformat(it["published"]) < cutoff:
                continue
            seen.add(key)
            kept += 1
            out.append(it)
    out.sort(key=lambda x: x["published"] or "", reverse=True)
    return out, errors


def save(items: list[dict], errors: dict[str, str], root: Path) -> Path:
    now = datetime.now(timezone.utc)
    day = root / "news" / now.strftime("%Y-%m-%d")
    day.mkdir(parents=True, exist_ok=True)
    stem = now.strftime("%H%MZ")
    (day / f"{stem}.json").write_text(json.dumps({"collected_at": now.isoformat(), "errors": errors, "items": items}, ensure_ascii=False, indent=1))
    lines = [f"# 미국 증시 뉴스 {now:%Y-%m-%d %H:%M} UTC", "", f"헤드라인 {len(items)}건 (최근 6시간)", ""]
    if errors:
        lines += ["실패한 피드: " + ", ".join(errors), ""]
    for it in items:
        lines.append(f"- **{it['title']}** — {it['source']} ({(it['published'] or '')[:16]})")
        if it["summary"]:
            lines.append(f"  - {it['summary'][:240]}")
    md = day / f"{stem}.md"
    md.write_text("\n".join(lines) + "\n")
    return md
