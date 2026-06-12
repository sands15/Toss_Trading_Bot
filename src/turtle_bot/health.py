from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import date as date_cls, datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import parse_qs, urlparse


PayloadProvider = Callable[[], Mapping[str, Any]]
EventsProvider = Callable[[int | None], list[Mapping[str, Any]]]
TOSS_LOGO_ASSET = Path(__file__).with_name("assets") / "toss-symbol.png"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class HealthSnapshot:
    mode: str = "idle"
    ready: bool = True
    blockers: tuple[str, ...] = field(default_factory=tuple)
    positions: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    open_orders: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    watchlist: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    generated_at: datetime = field(default_factory=_now_utc)

    def as_payload(self) -> dict[str, Any]:
        return {
            "status": "ready" if self.ready else "blocked",
            "mode": self.mode,
            "timestamp": self.generated_at.isoformat(),
            "ready": self.ready,
            "watchlist": {
                "count": len(self.watchlist),
                "items": list(self.watchlist),
            },
            "positions": {
                "count": len(self.positions),
                "items": list(self.positions),
            },
            "open_orders": {
                "count": len(self.open_orders),
                "items": list(self.open_orders),
            },
            "blockers": list(self.blockers),
        }

    def status_payload(self) -> dict[str, Any]:
        return {
            "status": "ready" if self.ready else "blocked",
            "mode": self.mode,
            "ready": self.ready,
            "blockers": list(self.blockers),
            "last_heartbeat_at": self.generated_at.isoformat(),
            "last_event_at": None,
        }

    def positions_payload(self) -> dict[str, Any]:
        return {"positions": list(self.positions), "count": len(self.positions)}

    def open_orders_payload(self) -> dict[str, Any]:
        return {"open_orders": list(self.open_orders), "count": len(self.open_orders)}

    def watchlist_payload(self) -> dict[str, Any]:
        return {"watchlist": list(self.watchlist), "count": len(self.watchlist)}


def _coerce_payload_items(value: Any) -> tuple[Mapping[str, Any], ...]:
    if isinstance(value, Mapping):
        return (dict(value),)
    if value is None:
        return ()
    if isinstance(value, Iterable):
        return tuple(dict(item) for item in value)
    return ()


def _normalize_payload(raw: Mapping[str, Any]) -> HealthSnapshot:
    positions = raw.get("positions", ())
    open_orders = raw.get("open_orders", ())
    watchlist = raw.get("watchlist", ())

    if isinstance(positions, Mapping):
        positions = positions.get("items", ())
    if isinstance(open_orders, Mapping):
        open_orders = open_orders.get("items", ())
    if isinstance(watchlist, Mapping):
        watchlist = watchlist.get("items", ())

    timestamp = raw.get("timestamp")
    if isinstance(timestamp, str):
        try:
            timestamp = datetime.fromisoformat(timestamp)
        except ValueError:
            timestamp = _now_utc()

    return HealthSnapshot(
        mode=str(raw.get("mode", "idle")),
        ready=bool(raw.get("ready", True)),
        blockers=tuple(str(item) for item in raw.get("blockers", ())),
        positions=_coerce_payload_items(positions),
        open_orders=_coerce_payload_items(open_orders),
        watchlist=_coerce_payload_items(watchlist),
        generated_at=timestamp if isinstance(timestamp, datetime) else _now_utc(),
    )


def _iso_datetime(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return value
    return None


def _extract_event_blockers(payload: Any) -> tuple[str, ...]:
    blockers = ()
    if not isinstance(payload, Mapping):
        return blockers
    raw = payload.get("blockers")
    if raw is None:
        return blockers
    if isinstance(raw, str):
        return (raw,)
    if not isinstance(raw, Iterable):
        return blockers
    return tuple(str(item) for item in raw)


def _coerce_events_payload(items: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for item in items:
        created = item.get("created_at")
        if isinstance(created, datetime):
            created = created.isoformat()
        elif created is not None and not isinstance(created, str):
            created = str(created)

        output.append(
            {
                "id": item.get("id"),
                "level": item.get("level"),
                "message": item.get("message"),
                "payload": item.get("payload"),
                "created_at": created,
            }
        )
    return output


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _summarize_events(items: list[Mapping[str, Any]]) -> dict[str, Any]:
    total = len(items)
    if total == 0:
        return {
            "total": 0,
            "first_event_at": None,
            "last_event_at": None,
            "by_level": {},
            "by_message": {},
            "blockers": [],
            "paper_order_intents": 0,
            "paper_fills": 0,
            "paper_guard_checks": 0,
            "paper_runtime_blocks": 0,
        }

    by_level = Counter()
    by_message = Counter()
    blockers: list[str] = []
    seen_blockers = set[str]()
    timestamps: list[datetime] = []

    for item in items:
        level = str(item.get("level", "UNKNOWN"))
        message = str(item.get("message", "UNKNOWN"))
        by_level[level] += 1
        by_message[message] += 1
        for blocker in _extract_event_blockers(item.get("payload")):
            if blocker not in seen_blockers:
                blockers.append(blocker)
                seen_blockers.add(blocker)
        created = _timestamp(item.get("created_at"))
        if created is not None:
            timestamps.append(created)

    return {
        "total": total,
        "first_event_at": min(timestamps).isoformat() if timestamps else None,
        "last_event_at": max(timestamps).isoformat() if timestamps else None,
        "by_level": dict(by_level),
        "by_message": dict(by_message),
        "blockers": blockers,
        "paper_order_intents": by_message.get("paper_order_intent", 0),
        "paper_fills": by_message.get("paper_fill", 0),
        "paper_guard_checks": by_message.get("paper_order_guard", 0),
        "paper_runtime_blocks": by_message.get("paper_service_blocked", 0),
    }


def _events_for_day(items: list[Mapping[str, Any]], target: date_cls) -> list[Mapping[str, Any]]:
    output: list[Mapping[str, Any]] = []
    for item in items:
        created = _timestamp(item.get("created_at"))
        if created is not None and created.date() == target:
            output.append(item)
    return output


def _first_day_event(items: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    if not items:
        return None
    if isinstance(items[0].get("created_at"), datetime):
        return items[0]
    if isinstance(items[0].get("created_at"), str):
        return items[0]
    return items[0]


def dashboard_html() -> str:
    _skeleton_reference = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Toss Turtle Bot</title>
  <style>
    :root {
      --bg: #f7f9fc;
      --panel: #ffffff;
      --panel-soft: #f8fafd;
      --text: #101828;
      --muted: #7b8aa3;
      --line: #e3e9f2;
      --line-soft: #edf1f7;
      --blue: #2563eb;
      --blue-soft: #edf4ff;
      --green: #36c690;
      --amber: #ffbd65;
      --shadow: 0 16px 36px rgba(31, 46, 76, 0.06);
    }

    * { box-sizing: border-box; }

    html, body {
      margin: 0;
      min-height: 100%;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      color: var(--text);
      background: var(--bg);
      overflow-x: hidden;
    }

    button, a { font: inherit; }

    .app {
      min-height: 100vh;
      display: grid;
      grid-template-rows: 86px 1fr;
      background:
        radial-gradient(circle at 68% 18%, rgba(37, 99, 235, 0.05), transparent 30%),
        linear-gradient(180deg, #fbfcff 0%, var(--bg) 48%, #f5f8fc 100%);
    }

    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 20px;
      padding: 0 28px;
      background: rgba(255, 255, 255, 0.9);
      border-bottom: 1px solid var(--line);
      backdrop-filter: blur(14px);
      position: sticky;
      top: 0;
      z-index: 10;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 16px;
      min-width: 0;
    }

    .logo {
      width: 58px;
      height: 46px;
      border-radius: 8px;
      background: #ffffff;
      flex: 0 0 auto;
      display: block;
      object-fit: contain;
    }

    .brand-title {
      display: flex;
      align-items: center;
      gap: 18px;
      min-width: 0;
    }

    .brand-title strong {
      font-size: 19px;
      white-space: nowrap;
    }

    .read-only {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: #1f2937;
      font-size: 13px;
      font-weight: 700;
      white-space: nowrap;
    }

    .read-only::before {
      content: "";
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--green);
    }

    .top-actions {
      display: flex;
      align-items: center;
      gap: 12px;
      min-width: 0;
    }

    .clock-line {
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
      color: #516079;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #f8fafc;
      padding: 9px 12px;
      box-shadow: 0 8px 18px rgba(31, 46, 76, 0.04);
    }

    .clock-line svg {
      width: 18px;
      height: 18px;
      stroke-width: 2;
    }

    .clock-line span {
      white-space: nowrap;
    }

    .clock-text {
      color: #516079;
      font-size: 12px;
      font-weight: 800;
      line-height: 1;
    }

    .ghost-line {
      display: inline-block;
      height: 10px;
      border-radius: 99px;
      background: #dfe5ee;
    }

    .btn {
      height: 48px;
      border-radius: 8px;
      border: 1px solid var(--line);
      background: #ffffff;
      color: #1f2a44;
      padding: 0 18px;
      display: inline-flex;
      align-items: center;
      gap: 10px;
      font-weight: 800;
      cursor: pointer;
      text-decoration: none;
      box-shadow: 0 10px 20px rgba(31, 46, 76, 0.04);
    }

    .btn.primary {
      background: linear-gradient(145deg, #2f6df4, #1d4ed8);
      color: #ffffff;
      border-color: #1d4ed8;
      box-shadow: 0 14px 26px rgba(37, 99, 235, 0.24);
    }

    .btn svg {
      width: 18px;
      height: 18px;
      stroke-width: 2.4;
    }

    .shell {
      display: grid;
      grid-template-columns: 178px minmax(0, 1fr);
      min-height: 0;
    }

    .sidebar {
      background: rgba(255, 255, 255, 0.82);
      border-right: 1px solid var(--line);
      padding: 30px 20px 22px;
      display: flex;
      flex-direction: column;
      gap: 22px;
      position: sticky;
      top: 86px;
      height: calc(100vh - 86px);
      overflow: auto;
    }

    .nav {
      display: grid;
      gap: 10px;
    }

    .nav a,
    .theme-button {
      min-height: 52px;
      border-radius: 8px;
      color: #8492aa;
      text-decoration: none;
      display: flex;
      align-items: center;
      gap: 14px;
      padding: 0 14px;
      font-size: 13px;
      font-weight: 800;
      border: 1px solid transparent;
    }

    .nav a.active {
      color: var(--blue);
      background: #edf4ff;
      border-color: #e7eefb;
    }

    .nav svg,
    .theme-button svg,
    .bottom-nav svg {
      width: 19px;
      height: 19px;
      stroke-width: 2.2;
      flex: 0 0 auto;
    }

    .theme-button {
      margin-top: auto;
      padding-left: 14px;
      min-height: 42px;
    }

    .sidebar-status {
      margin-top: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcff;
      padding: 14px;
      display: grid;
      gap: 10px;
    }

    .sidebar-status strong {
      font-size: 13px;
    }

    .sidebar-status p {
      margin: 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
      overflow-wrap: anywhere;
      display: -webkit-box;
      -webkit-line-clamp: 3;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }

    .sidebar-action {
      min-height: 34px;
      border-radius: 8px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 0 12px;
      background: var(--blue);
      color: #ffffff;
      text-decoration: none;
      font-size: 12px;
      font-weight: 900;
    }

    .main {
      min-width: 0;
      padding: 22px;
    }

    .view {
      display: none;
      min-width: 0;
    }

    .view.active {
      display: block;
    }

    .dashboard-grid {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1.12fr) minmax(320px, 0.98fr);
      gap: 20px;
      align-items: stretch;
    }

    .stat-row {
      grid-column: 1 / -1;
      display: grid;
      grid-template-columns: 1.55fr repeat(4, minmax(150px, 1fr));
      gap: 16px;
    }

    .card {
      background: rgba(255, 255, 255, 0.86);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      min-width: 0;
      overflow: hidden;
    }

    .stat-card {
      min-height: 122px;
      padding: 24px;
      display: flex;
      align-items: center;
      gap: 18px;
    }

    .stat-card.compact {
      justify-content: flex-start;
      gap: 20px;
    }

    .icon-tile {
      width: 64px;
      height: 64px;
      border-radius: 8px;
      background: var(--blue-soft);
      color: var(--blue);
      display: grid;
      place-items: center;
      flex: 0 0 auto;
    }

    .icon-tile.small {
      width: 46px;
      height: 46px;
      border-radius: 50%;
    }

    .icon-tile svg {
      width: 24px;
      height: 24px;
      stroke-width: 2.4;
    }

    .stat-label {
      margin: 0 0 12px;
      font-size: 13px;
      font-weight: 900;
      color: #0f172a;
    }

    .stat-value {
      margin: 0;
      font-size: 30px;
      line-height: 1.05;
      font-weight: 900;
    }

    .stat-lines {
      min-width: 0;
      flex: 1;
      display: grid;
      gap: 12px;
    }

    .pill-ghost {
      width: 54px;
      height: 22px;
      border-radius: 99px;
      background: #eaf1ff;
      position: relative;
      margin-left: auto;
      flex: 0 0 auto;
    }

    .pill-ghost::after {
      content: "";
      position: absolute;
      inset: 7px 10px;
      border-radius: 99px;
      background: #b9cdf8;
    }

    .section-card {
      min-height: 322px;
      padding: 0;
    }

    .section-card.short {
      min-height: 250px;
    }

    .section-card.wide {
      grid-column: 1 / -1;
      min-height: 198px;
    }

    .section-title {
      margin: 0;
      min-height: 66px;
      display: flex;
      align-items: center;
      padding: 0 24px;
      font-size: 17px;
      font-weight: 900;
      border-bottom: 1px solid transparent;
    }

    .operator-brief {
      grid-column: 1 / -1;
      padding: 18px;
      display: grid;
      grid-template-columns: minmax(0, 1.2fr) repeat(2, minmax(220px, 0.7fr));
      gap: 12px;
      align-items: stretch;
    }

    .brief-item {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcff;
      padding: 14px;
      display: grid;
      gap: 8px;
      min-width: 0;
    }

    .brief-item.primary {
      border-color: #bfdbfe;
      background: #eff6ff;
    }

    .brief-item.warn {
      border-color: #fed7aa;
      background: #fff7ed;
    }

    .brief-item.done {
      border-color: #a7f3d0;
      background: #ecfdf5;
    }

    .brief-kicker {
      color: var(--muted);
      font-size: 11px;
      font-weight: 900;
      text-transform: uppercase;
    }

    .brief-item strong {
      color: #102033;
      font-size: 15px;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }

    .brief-item p {
      margin: 0;
      color: #516079;
      font-size: 13px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }

    .brief-item a {
      width: fit-content;
      color: var(--blue);
      font-size: 13px;
      font-weight: 900;
      text-decoration: none;
    }

    .list-skeleton {
      display: grid;
    }

    .status-row {
      min-height: 49px;
      display: grid;
      grid-template-columns: 24px minmax(0, 1fr) 44px;
      gap: 16px;
      align-items: center;
      padding: 0 24px;
      border-top: 1px solid var(--line-soft);
    }

    .dot {
      width: 20px;
      height: 20px;
      border-radius: 50%;
      background: #dfe5ee;
    }

    .chart-area {
      height: 244px;
      margin: 0 26px 20px;
      position: relative;
      display: grid;
      align-content: stretch;
      padding: 6px 0 48px;
    }

    .dash-line {
      border-top: 1px dashed #dfe5ee;
    }

    .chart-center {
      position: absolute;
      left: 50%;
      top: 48%;
      transform: translate(-50%, -50%);
      width: 48px;
      height: 48px;
      border-radius: 50%;
      display: grid;
      place-items: center;
      background: rgba(237, 242, 249, 0.9);
      color: #b9c4d5;
    }

    .chart-center svg {
      width: 22px;
      height: 22px;
    }

    .chart-legend {
      position: absolute;
      left: 10px;
      right: 10px;
      bottom: 12px;
      display: flex;
      justify-content: space-around;
      gap: 18px;
    }

    .donut-wrap {
      display: grid;
      grid-template-columns: 160px minmax(0, 1fr);
      gap: 34px;
      align-items: center;
      padding: 0 28px;
      min-height: 168px;
    }

    .donut {
      width: 138px;
      height: 138px;
      border-radius: 50%;
      background: conic-gradient(#dfe4ec 0 8%, #f3f5f8 8% 33%, #dfe4ec 33% 66%, #eef1f6 66% 100%);
      position: relative;
    }

    .donut::after {
      content: "";
      position: absolute;
      inset: 43px;
      border-radius: 50%;
      background: #ffffff;
    }

    .summary-lines {
      display: grid;
      gap: 14px;
    }

    .summary-line {
      display: grid;
      grid-template-columns: 12px minmax(0, 1fr);
      gap: 12px;
      align-items: center;
    }

    .summary-dot {
      width: 12px;
      height: 12px;
      border-radius: 50%;
      background: #dfe5ee;
    }

    .mini-strip {
      margin: 20px 16px 0;
      min-height: 64px;
      border: 1px solid var(--line);
      border-radius: 8px;
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 20px;
      padding: 17px;
    }

    .table-skeleton {
      display: grid;
      border-top: 1px solid var(--line-soft);
    }

    .table-row {
      min-height: 43px;
      display: grid;
      grid-template-columns: 28px 1fr 0.65fr 0.75fr 0.55fr;
      gap: 18px;
      align-items: center;
      padding: 0 22px;
      border-bottom: 1px solid var(--line-soft);
    }

    .timeline {
      position: relative;
      margin: 10px 24px 0;
      padding: 0;
      list-style: none;
      display: grid;
      gap: 19px;
      min-width: 0;
      max-width: calc(100% - 48px);
    }

    .timeline::before {
      content: "";
      position: absolute;
      left: 5px;
      top: 6px;
      bottom: 6px;
      width: 1px;
      background: #dde5ef;
    }

    .timeline li {
      display: grid;
      grid-template-columns: 12px 58px minmax(0, 1fr) 50px;
      gap: 16px;
      align-items: center;
      position: relative;
      min-height: 18px;
      min-width: 0;
      max-width: 100%;
    }

    .event-dot {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: #76a7ff;
      z-index: 1;
    }

    .event-dot.warn { background: #ffd39c; }
    .event-dot.ok { background: #abe9cd; }

    .bot-summary {
      padding: 0 18px 18px;
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 18px;
    }

    .summary-tile {
      min-height: 74px;
      border: 1px solid var(--line);
      border-radius: 8px;
      display: flex;
      align-items: center;
      gap: 18px;
      padding: 14px;
    }

    .log-lines {
      padding: 0 24px 20px;
      display: grid;
      gap: 16px;
    }

    .log-line {
      display: grid;
      grid-template-columns: 16px minmax(0, 1fr) 48px;
      gap: 16px;
      align-items: center;
    }

    .log-menu {
      margin-left: auto;
      color: #8aa0bc;
    }

    .empty-view {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      gap: 20px;
    }

    .user-view {
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 20px;
    }

    .data-panel {
      padding: 24px;
    }

    .primary-panel {
      min-height: 260px;
    }

    .panel-heading {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 18px;
      margin-bottom: 18px;
    }

    .panel-heading h2 {
      margin-bottom: 0;
    }

    .eyebrow {
      margin: 0 0 6px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 900;
    }

    .panel-copy {
      margin: -6px 0 16px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.55;
    }

    .data-panel h2 {
      margin: 0 0 16px;
      font-size: 18px;
    }

    .data-table {
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: auto;
      background: #fff;
      max-height: 420px;
    }

    .data-table:empty {
      display: none;
    }

    .data-table table {
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
    }

    .data-table th {
      position: sticky;
      top: 0;
      background: #f8fafc;
      color: #475569;
      z-index: 1;
    }

    .data-table th,
    .data-table td {
      padding: 12px 14px;
      border-bottom: 1px solid var(--line-soft);
      text-align: left;
      font-size: 13px;
      word-break: break-word;
      overflow-wrap: anywhere;
    }

    .view-json {
      margin: 0;
      max-height: 420px;
      overflow: auto;
      border-radius: 8px;
      background: #101828;
      color: #d5deec;
      padding: 16px;
      font-size: 12px;
      white-space: pre;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }

    .hidden-json {
      display: none;
    }

    .empty-state {
      min-height: 154px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: linear-gradient(180deg, #fbfcff, #ffffff);
      display: flex;
      align-items: center;
      gap: 18px;
      padding: 22px;
    }

    .empty-state .icon-tile {
      width: 52px;
      height: 52px;
      border-radius: 50%;
    }

    .empty-state strong {
      display: block;
      margin-bottom: 6px;
      font-size: 15px;
    }

    .empty-state p {
      margin: 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.55;
    }

    .empty-state a {
      color: var(--blue);
      font-weight: 900;
      text-decoration: none;
    }

    .event-cards {
      border: 0;
      display: grid;
      gap: 10px;
      max-height: none;
      background: transparent;
      overflow: visible;
    }

    .event-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #ffffff;
      padding: 14px 16px;
      display: grid;
      grid-template-columns: minmax(68px, 84px) minmax(0, 1fr) minmax(72px, 94px);
      gap: 16px;
      align-items: start;
      min-width: 0;
      max-width: 100%;
    }

    .event-card > * {
      min-width: 0;
    }

    .event-card strong {
      display: block;
      margin-bottom: 4px;
      font-size: 13px;
      overflow-wrap: anywhere;
    }

    .event-card p {
      margin: 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
      overflow-wrap: anywhere;
      word-break: break-word;
    }

    .event-card .helper-text {
      justify-self: end;
      max-width: 100%;
      text-align: right;
      white-space: normal;
      overflow-wrap: anywhere;
    }

    .level-badge {
      width: fit-content;
      border-radius: 999px;
      padding: 4px 9px;
      background: #ecfdf5;
      color: #047857;
      font-size: 11px;
      font-weight: 900;
    }

    .level-badge.warn,
    .level-badge.error {
      background: #fff7ed;
      color: #b45309;
    }

    .endpoint-list {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }

    .endpoint-list button {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 9px 12px;
      cursor: pointer;
    }

    .status-copy,
    .blocker-list {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcff;
      padding: 14px;
      color: #516079;
      font-size: 13px;
      line-height: 1.55;
      word-break: break-word;
    }

    .action-list {
      list-style: none;
      padding: 0;
      margin: 0;
      display: grid;
      gap: 12px;
    }

    .action-list li {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 14px;
    }

    .status-pill {
      display: inline-flex;
      align-items: center;
      width: fit-content;
      border-radius: 999px;
      border: 1px solid #bfdbfe;
      background: #eff6ff;
      color: #1d4ed8;
      padding: 4px 10px;
      font-size: 12px;
      font-weight: 800;
    }

    .status-pill.warn {
      background: #fff7ed;
      border-color: #fed7aa;
      color: #b45309;
    }

    .status-pill.done {
      background: #ecfdf5;
      border-color: #a7f3d0;
      color: #047857;
    }

    .status-pill.blocked {
      background: #fff1f2;
      border-color: #fecdd3;
      color: #be123c;
    }

    .metric-value {
      display: block;
      margin: 4px 0 8px;
      font-size: 28px;
      line-height: 1;
      font-weight: 900;
      color: #0f172a;
    }

    .metric-note,
    .helper-text {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
    }

    .info-list {
      display: grid;
      gap: 0;
    }

    .info-row {
      min-height: 52px;
      display: grid;
      grid-template-columns: 12px minmax(0, 1fr) auto;
      gap: 14px;
      align-items: center;
      padding: 0 24px;
      border-top: 1px solid var(--line-soft);
    }

    .info-row strong,
    .event-line strong {
      color: #1f2a44;
      font-size: 13px;
    }

    .info-row span,
    .event-line span {
      min-width: 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }

    .next-step {
      margin: 14px 20px 20px;
      border: 1px solid #fed7aa;
      border-radius: 8px;
      background: #fff7ed;
      padding: 14px;
      display: grid;
      gap: 8px;
    }

    .next-step.done {
      border-color: #a7f3d0;
      background: #ecfdf5;
    }

    .next-step strong {
      color: #1f2a44;
      font-size: 13px;
    }

    .next-step p {
      margin: 0;
      color: #516079;
      font-size: 13px;
      line-height: 1.45;
    }

    .next-step a {
      width: fit-content;
      color: var(--blue);
      font-size: 13px;
      font-weight: 900;
      text-decoration: none;
    }

    .summary-stack {
      padding: 0 24px 22px;
      display: grid;
      gap: 12px;
    }

    .summary-chip {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcff;
      padding: 12px 14px;
      display: grid;
      gap: 4px;
      min-width: 0;
    }

    .summary-chip strong {
      font-size: 13px;
    }

    .event-line {
      display: grid;
      grid-template-columns: 12px 52px minmax(0, 1fr) 92px;
      gap: 14px;
      align-items: center;
      min-height: 32px;
      min-width: 0;
      max-width: 100%;
    }

    .event-line > * {
      min-width: 0;
    }

    .event-line .event-dot {
      align-self: center;
    }

    .event-line .helper-text {
      font-size: 11px;
      justify-self: end;
      text-align: right;
      white-space: normal;
      overflow-wrap: anywhere;
    }

    .sr-data {
      position: absolute;
      left: -10000px;
      width: 1px;
      height: 1px;
      overflow: hidden;
    }

    .bottom-nav {
      display: none;
    }

    @media (max-width: 1100px) {
      .stat-row {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .operator-brief {
        grid-template-columns: 1fr;
      }

      .dashboard-grid,
      .empty-view {
        grid-template-columns: 1fr;
      }

      .section-card,
      .section-card.short {
        min-height: auto;
      }
    }

    @media (max-width: 820px) {
      .app {
        grid-template-rows: auto 1fr;
      }

      .topbar {
        min-height: 76px;
        padding: 14px 14px;
        align-items: flex-start;
      }

      .brand-title {
        display: grid;
        gap: 4px;
      }

      .top-actions {
        display: none;
      }

      .shell {
        grid-template-columns: 1fr;
      }

      .sidebar {
        display: none;
      }

      .main {
        padding: 12px 12px 92px;
      }

      .stat-row {
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 10px;
      }

      .stat-row > .stat-card:first-child {
        grid-column: 1 / -1;
      }

      .stat-card {
        min-height: 112px;
        padding: 16px;
        align-items: flex-start;
        flex-direction: column;
      }

      .stat-card.compact {
        gap: 12px;
      }

      .icon-tile,
      .icon-tile.small {
        width: 42px;
        height: 42px;
        border-radius: 8px;
      }

      .stat-value {
        font-size: 24px;
      }

      .section-title {
        min-height: 54px;
        padding: 0 16px;
      }

      .status-row,
      .table-row {
        padding: 0 16px;
        gap: 10px;
      }

      .timeline {
        margin: 8px 16px 18px;
        max-width: calc(100% - 32px);
      }

      .event-line {
        grid-template-columns: 12px 44px minmax(0, 1fr);
      }

      .event-line .helper-text {
        grid-column: 3;
        justify-self: start;
        text-align: left;
      }

      .event-card {
        grid-template-columns: 1fr;
        gap: 8px;
      }

      .event-card .helper-text {
        justify-self: start;
        text-align: left;
      }

      .chart-area {
        margin: 0 18px 18px;
      }

      .donut-wrap {
        grid-template-columns: 1fr;
        justify-items: center;
        gap: 18px;
      }

      .mini-strip,
      .bot-summary {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .bottom-nav {
        position: fixed;
        left: max(8px, env(safe-area-inset-left));
        right: max(8px, env(safe-area-inset-right));
        width: auto;
        max-width: none;
        bottom: calc(12px + env(safe-area-inset-bottom));
        z-index: 20;
        display: grid;
        grid-template-columns: repeat(5, minmax(0, 1fr));
        gap: 4px;
        padding: 8px 6px;
        background: rgba(255, 255, 255, 0.96);
        border: 1px solid rgba(226, 232, 240, 0.92);
        border-radius: 999px;
        box-shadow: 0 18px 45px rgba(15, 23, 42, 0.16);
        backdrop-filter: blur(18px);
        -webkit-backdrop-filter: blur(18px);
        justify-self: stretch;
      }

      .bottom-nav a {
        min-width: 0;
        min-height: 48px;
        border-radius: 8px;
        color: #8492aa;
        text-decoration: none;
        display: grid;
        place-items: center;
        gap: 2px;
        font-size: 10px;
        font-weight: 800;
      }

      .bottom-nav a.active {
        color: var(--blue);
        background: #edf4ff;
      }
    }
  </style>
</head>
<body>
  <div class="app">
    <header class="topbar">
      <div class="brand">
        <img class="logo" src="/assets/toss-symbol.png" alt="Toss logo" loading="eager" decoding="async">
        <div class="brand-title">
          <strong>Toss Turtle Bot</strong>
          <span class="read-only">읽기 전용</span>
        </div>
      </div>
      <div class="top-actions">
        <div class="clock-line" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle cx="12" cy="12" r="9"></circle><path d="M12 7v5l3 2"></path></svg>
          <span id="dashboard-clock" class="clock-text">현재 --:--:--</span>
        </div>
        <button class="btn" type="button" id="refresh-button">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M21 12a9 9 0 0 1-9 9 8.7 8.7 0 0 1-6-2.3"></path><path d="M3 12a9 9 0 0 1 15-6.7"></path><path d="M3 19v-5h5"></path><path d="M21 5v5h-5"></path></svg>
          새로고침
        </button>
        <a class="btn primary" href="#raw" data-view="raw">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><rect x="7" y="3" width="10" height="18" rx="2"></rect><path d="M10 8h4"></path><path d="M10 12h4"></path><path d="M10 16h4"></path></svg>
          원본 데이터
        </a>
      </div>
    </header>

    <div class="shell">
      <aside class="sidebar">
        <nav class="nav" aria-label="Dashboard sections">
          <a class="active" href="#dashboard" data-view="dashboard"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><rect x="3" y="3" width="7" height="7"></rect><rect x="14" y="3" width="7" height="7"></rect><rect x="3" y="14" width="7" height="7"></rect><rect x="14" y="14" width="7" height="7"></rect></svg>대시보드</a>
          <a href="#watchlist" data-view="watchlist"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="m4 17 5-5 4 4 7-8"></path><path d="M16 8h4v4"></path></svg>관심</a>
          <a href="#positions" data-view="positions"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M4 19V7a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v12"></path><path d="M8 5V3h8v2"></path><path d="M4 11h16"></path></svg>포지션</a>
          <a href="#orders" data-view="orders"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><rect x="7" y="3" width="10" height="18" rx="2"></rect><path d="M10 8h4"></path><path d="M10 12h4"></path><path d="M10 16h2"></path></svg>주문</a>
          <a href="#events" data-view="events"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><rect x="7" y="3" width="10" height="18" rx="2"></rect><path d="M10 8h4"></path><path d="M10 12h4"></path><path d="M10 16h2"></path></svg>이벤트</a>
          <a href="#raw" data-view="raw"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M21 16v-2H3v2a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2ZM3 8h18v4H3z"></path><path d="M3 8l6 5 5-3 7 3"></path></svg>개발자</a>
          <a href="#settings" data-view="settings"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 0 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6V21a2 2 0 0 1-4 0v-.1a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 0 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.6-1H3a2 2 0 0 1 0-4h.1a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 0 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3 1.7 1.7 0 0 0 1-1.6V3a2 2 0 0 1 4 0v.1a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 0 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.1a2 2 0 0 1 0 4h-.1a1.7 1.7 0 0 0-1.6 1Z"></path></svg>설정</a>
        </nav>
        <section class="sidebar-status" aria-label="현재 운영 상태">
          <strong id="sidebar-ready">상태 확인 중</strong>
          <p id="sidebar-mode">모드: -</p>
          <p id="sidebar-blockers">차단 항목: -</p>
          <a class="sidebar-action" href="#settings" data-view="settings">설정 확인</a>
        </section>
      </aside>

      <main class="main">
        <section id="view-dashboard" class="view active" data-view="dashboard">
          <div class="dashboard-grid">
            <section class="stat-row">
              <article class="card stat-card">
                <div class="icon-tile"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="m12 3 7 4v10l-7 4-7-4V7l7-4Z"></path><path d="M12 8v8"></path><path d="m9 10 3-2 3 2"></path></svg></div>
                <div class="stat-lines">
                  <span class="ghost-line" style="width:124px"></span>
                  <span class="ghost-line" style="width:74px"></span>
                </div>
                <span class="pill-ghost"></span>
              </article>
              <article class="card stat-card compact">
                <div class="icon-tile small"><svg viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9L12 3Z"></path></svg></div>
                <div>
                  <p class="stat-label">관심 종목</p>
                  <span class="ghost-line" style="width:52px"></span>
                  <span class="ghost-line" style="width:82px;margin-top:12px;display:block"></span>
                </div>
              </article>
              <article class="card stat-card compact">
                <div class="icon-tile small"><svg viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M13 2a10 10 0 1 0 9 11h-9V2Z"></path><path d="M15 2.2V9h6.8A10 10 0 0 0 15 2.2Z"></path></svg></div>
                <div>
                  <p class="stat-label">보유 포지션</p>
                  <span class="ghost-line" style="width:52px"></span>
                  <span class="ghost-line" style="width:82px;margin-top:12px;display:block"></span>
                </div>
              </article>
              <article class="card stat-card compact">
                <div class="icon-tile small"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M8 4h8v16H8z"></path><path d="M10 8h4"></path><path d="M10 12h4"></path><path d="M10 16h2"></path></svg></div>
                <div>
                  <p class="stat-label">미체결 주문</p>
                  <span class="ghost-line" style="width:52px"></span>
                  <span class="ghost-line" style="width:82px;margin-top:12px;display:block"></span>
                </div>
              </article>
              <article class="card stat-card compact">
                <div class="icon-tile small"><svg viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M12 22a2.5 2.5 0 0 0 2.4-1.8H9.6A2.5 2.5 0 0 0 12 22ZM18 16v-5a6 6 0 1 0-12 0v5l-2 2v1h16v-1l-2-2Z"></path></svg></div>
                <div>
                  <p class="stat-label">총 이벤트</p>
                  <span class="ghost-line" style="width:52px"></span>
                  <span class="ghost-line" style="width:82px;margin-top:12px;display:block"></span>
                </div>
              </article>
            </section>

            <section id="dashboard-operator-brief" class="card operator-brief" aria-label="운영 요약">
              <div class="brief-item primary">
                <span class="brief-kicker">우선 확인</span>
                <strong>상태를 불러오는 중</strong>
                <p>현재 설정과 최근 이벤트를 확인하고 있습니다.</p>
              </div>
              <div class="brief-item">
                <span class="brief-kicker">최근 기록</span>
                <strong>-</strong>
                <p>이벤트가 들어오면 여기에 마지막 기록이 표시됩니다.</p>
              </div>
              <div class="brief-item">
                <span class="brief-kicker">데이터</span>
                <strong>-</strong>
                <p>관심 종목, 포지션, 주문 수를 요약합니다.</p>
              </div>
            </section>

            <article class="card section-card">
              <h2 class="section-title">시스템 상태</h2>
              <div id="dashboard-health-list" class="list-skeleton">
                <div class="status-row"><span class="dot"></span><span class="ghost-line" style="width:74%"></span><span class="ghost-line"></span></div>
                <div class="status-row"><span class="dot"></span><span class="ghost-line" style="width:74%"></span><span class="ghost-line"></span></div>
                <div class="status-row"><span class="dot"></span><span class="ghost-line" style="width:74%"></span><span class="ghost-line"></span></div>
                <div class="status-row"><span class="dot"></span><span class="ghost-line" style="width:74%"></span><span class="ghost-line"></span></div>
                <div class="status-row"><span class="dot"></span><span class="ghost-line" style="width:74%"></span><span class="ghost-line"></span></div>
              </div>
            </article>

            <article class="card section-card">
              <h2 class="section-title">관심 종목</h2>
              <div class="chart-area">
                <span class="dash-line"></span>
                <span class="dash-line"></span>
                <span class="dash-line"></span>
                <span class="dash-line"></span>
                <div class="chart-center"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="m4 17 5-5 4 4 7-8"></path><path d="M16 8h4v4"></path></svg></div>
                <div class="chart-legend">
                  <span class="ghost-line" style="width:45px"></span>
                  <span class="ghost-line" style="width:45px"></span>
                  <span class="ghost-line" style="width:45px"></span>
                  <span class="ghost-line" style="width:45px"></span>
                  <span class="ghost-line" style="width:45px"></span>
                </div>
              </div>
            </article>

            <article class="card section-card">
              <h2 class="section-title">보유 포지션</h2>
              <div class="donut-wrap">
                <div class="donut"></div>
                <div class="summary-lines">
                  <span class="ghost-line" style="width:78%"></span>
                  <span class="ghost-line" style="width:44%"></span>
                  <span style="height:14px"></span>
                  <div class="summary-line"><span class="summary-dot"></span><span class="ghost-line" style="width:64%"></span></div>
                  <div class="summary-line"><span class="summary-dot"></span><span class="ghost-line" style="width:54%"></span></div>
                </div>
              </div>
              <div class="mini-strip">
                <span class="ghost-line"></span><span class="ghost-line"></span><span class="ghost-line"></span><span class="ghost-line"></span>
                <span class="ghost-line"></span><span class="ghost-line"></span><span class="ghost-line"></span><span class="ghost-line"></span>
              </div>
            </article>

            <article class="card section-card short">
              <h2 class="section-title">미체결 주문</h2>
              <div id="dashboard-open-orders-table" class="table-skeleton">
                <div class="table-row"><span class="dot"></span><span class="ghost-line"></span><span class="ghost-line"></span><span class="ghost-line"></span><span class="ghost-line"></span></div>
                <div class="table-row"><span class="dot"></span><span class="ghost-line"></span><span class="ghost-line"></span><span class="ghost-line"></span><span class="ghost-line"></span></div>
                <div class="table-row"><span class="dot"></span><span class="ghost-line"></span><span class="ghost-line"></span><span class="ghost-line"></span><span class="ghost-line"></span></div>
              </div>
            </article>

            <article class="card section-card short">
              <h2 class="section-title">최근 이벤트</h2>
              <ul id="dashboard-events-timeline" class="timeline">
                <li><span class="event-dot"></span><span class="ghost-line"></span><span class="ghost-line"></span><span class="ghost-line"></span></li>
                <li><span class="event-dot warn"></span><span class="ghost-line"></span><span class="ghost-line" style="width:86%"></span><span class="ghost-line"></span></li>
                <li><span class="event-dot ok"></span><span class="ghost-line"></span><span class="ghost-line" style="width:66%"></span><span class="ghost-line"></span></li>
                <li><span class="event-dot"></span><span class="ghost-line"></span><span class="ghost-line" style="width:92%"></span><span class="ghost-line"></span></li>
              </ul>
            </article>

            <article class="card section-card short">
              <h2 class="section-title">봇 요약</h2>
              <div class="bot-summary">
                <div class="summary-tile"><div class="icon-tile small"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle cx="12" cy="12" r="9"></circle><path d="M12 7v5l3 2"></path></svg></div><div class="stat-lines"><span class="ghost-line"></span><span class="ghost-line" style="width:60%"></span></div></div>
                <div class="summary-tile"><div class="icon-tile small"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="m4 17 5-5 4 4 7-8"></path><path d="M16 8h4v4"></path></svg></div><div class="stat-lines"><span class="ghost-line"></span><span class="ghost-line" style="width:60%"></span></div></div>
                <div class="summary-tile"><div class="icon-tile small"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M12 3 5 6v6c0 4 3 7 7 9 4-2 7-5 7-9V6l-7-3Z"></path></svg></div><div class="stat-lines"><span class="ghost-line"></span><span class="ghost-line" style="width:60%"></span></div></div>
                <div class="summary-tile"><div class="icon-tile small"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M4 7h16"></path><path d="M4 12h16"></path><path d="M4 17h16"></path></svg></div><div class="stat-lines"><span class="ghost-line"></span><span class="ghost-line" style="width:60%"></span></div></div>
              </div>
            </article>

            <article class="card section-card wide">
              <h2 class="section-title">실시간 로그 <span class="log-menu">⋮</span></h2>
              <div class="log-lines">
                <div class="log-line"><span class="dot"></span><span class="ghost-line"></span><span class="ghost-line"></span></div>
                <div class="log-line"><span class="dot"></span><span class="ghost-line"></span><span class="ghost-line"></span></div>
                <div class="log-line"><span class="dot"></span><span class="ghost-line"></span><span class="ghost-line"></span></div>
                <div class="log-line"><span class="dot"></span><span class="ghost-line"></span><span class="ghost-line"></span></div>
              </div>
            </article>
          </div>
        </section>

        <section id="view-watchlist" class="view" data-view="watchlist">
          <div class="user-view">
            <article class="card data-panel primary-panel">
              <div class="panel-heading">
                <div>
                  <p class="eyebrow">감시 대상</p>
                  <h2>관심 종목</h2>
                </div>
                <span id="watchlist-count-badge" class="status-pill">0개</span>
              </div>
              <div id="watchlist-table" class="data-table"></div>
              <pre id="watchlist-json" class="view-json hidden-json"></pre>
            </article>
          </div>
        </section>

        <section id="view-positions" class="view" data-view="positions">
          <div class="user-view">
            <article class="card data-panel primary-panel">
              <div class="panel-heading">
                <div>
                  <p class="eyebrow">현재 보유</p>
                  <h2>포지션</h2>
                </div>
                <span id="positions-count-badge" class="status-pill">0개</span>
              </div>
              <div id="positions-table" class="data-table"></div>
              <pre id="positions-json" class="view-json hidden-json"></pre>
            </article>
          </div>
        </section>

        <section id="view-orders" class="view" data-view="orders">
          <div class="user-view">
            <article class="card data-panel primary-panel">
              <div class="panel-heading">
                <div>
                  <p class="eyebrow">읽기 전용</p>
                  <h2>주문</h2>
                </div>
                <span id="orders-count-badge" class="status-pill">0개</span>
              </div>
              <div id="orders-table" class="data-table"></div>
              <pre id="orders-json" class="view-json hidden-json"></pre>
            </article>
          </div>
        </section>

        <section id="view-events" class="view" data-view="events">
          <div class="user-view">
            <article class="card data-panel primary-panel">
              <div class="panel-heading">
                <div>
                  <p class="eyebrow">최근 기록</p>
                  <h2>이벤트</h2>
                </div>
                <span id="events-count-badge" class="status-pill">0개</span>
              </div>
              <div id="events-table" class="data-table event-cards"></div>
              <pre id="events-json" class="view-json hidden-json"></pre>
            </article>
          </div>
        </section>

        <section id="view-raw" class="view" data-view="raw">
          <div class="empty-view">
            <article class="card data-panel"><h2>개발자 엔드포인트</h2><p class="panel-copy">문제 확인이 필요할 때만 원본 응답을 확인하세요.</p><div id="endpoint-list" class="endpoint-list"></div></article>
            <article class="card data-panel"><h2>선택한 원본 데이터</h2><pre id="raw-endpoint-json" class="view-json"></pre></article>
          </div>
          <article class="card data-panel" style="margin-top:20px"><h2>전체 대시보드 원본</h2><pre id="raw-aggregate-json" class="view-json"></pre></article>
        </section>

        <section id="view-settings" class="view" data-view="settings">
          <div class="empty-view">
            <article class="card data-panel">
              <h2>먼저 확인할 설정</h2>
              <p id="settings-headline" class="status-copy">신규 사용자는 필요한 항목부터 순서대로 설정하세요.</p>
              <ul id="settings-onboarding-list" class="action-list"></ul>
            </article>
            <article class="card data-panel">
              <h2>현재 막힌 이유</h2>
              <div id="settings-blockers-list" class="blocker-list"></div>
              <p class="panel-copy">영문 환경변수 이름은 개발자 탭의 원본 데이터에서만 확인합니다.</p>
              <pre id="settings-raw-links" class="view-json hidden-json"></pre>
            </article>
          </div>
        </section>

        <p class="sr-data">Local dashboard only. This interface reads bot state and never submits orders. Data is loaded from read-only endpoints.</p>
      </main>
    </div>
  </div>

  <nav class="bottom-nav" aria-label="Mobile dashboard sections">
    <a class="active" href="#dashboard" data-view="dashboard"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><rect x="3" y="3" width="7" height="7"></rect><rect x="14" y="3" width="7" height="7"></rect><rect x="3" y="14" width="7" height="7"></rect><rect x="14" y="14" width="7" height="7"></rect></svg>대시보드</a>
    <a href="#watchlist" data-view="watchlist"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="m4 17 5-5 4 4 7-8"></path><path d="M16 8h4v4"></path></svg>관심 종목</a>
    <a href="#positions" data-view="positions"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M13 2.1a10 10 0 1 0 8.9 8.9H13V2.1Z"></path><path d="M15 2.1V9h6.9"></path></svg>포지션</a>
    <a href="#orders" data-view="orders"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><rect x="7" y="3" width="10" height="18" rx="2"></rect><path d="M10 8h4"></path><path d="M10 12h4"></path><path d="M10 16h2"></path></svg>주문</a>
    <a href="#settings" data-view="settings"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 0 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6V21a2 2 0 0 1-4 0v-.1a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 0 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.6-1H3a2 2 0 0 1 0-4h.1a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 0 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3 1.7 1.7 0 0 0 1-1.6V3a2 2 0 0 1 4 0v.1a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 0 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.1a2 2 0 0 1 0 4h-.1a1.7 1.7 0 0 0-1.6 1Z"></path></svg>설정</a>
  </nav>

  <script>
    const ONBOARDING_STEPS = [
      {
        title: "Toss API 인증 정보",
        body: "TOSS_CLIENT_ID와 TOSS_CLIENT_SECRET을 로컬 환경변수에 저장하세요.",
        group: "필수",
        match: (blocker) => blocker.includes("TOSS_CLIENT_ID") || blocker.includes("TOSS_CLIENT_SECRET")
      },
      {
        title: "거래 계좌 연결",
        body: "config/local.yaml의 toss.account_seq 값을 설정하세요.",
        group: "필수",
        match: (blocker) => blocker.includes("account_seq")
      },
      {
        title: "감시 종목 후보",
        body: "runtime.symbols 또는 universe_candidate_symbols를 설정하세요.",
        group: "필수",
        match: (blocker) => blocker.includes("runtime.symbols") || blocker.includes("universe_candidate_symbols")
      },
      {
        title: "페이퍼 서비스 확인",
        body: "이벤트 탭에서 페이퍼 서비스 heartbeat가 최근에 기록됐는지 확인하세요.",
        group: "확인",
        match: () => false,
        eventMessage: "paper_service_heartbeat"
      }
    ];

    const EVENT_LABELS = {
      paper_service_started: "페이퍼 서비스 시작",
      paper_service_heartbeat: "페이퍼 서비스 점검 완료",
      paper_service_blocked: "설정 미완료로 중지",
      market_session_state: "시장 상태 확인",
      paper_service_market_closed: "시장 휴장/대기",
      premarket_watchlist_blocked: "관심 종목 생성 일부 실패",
      premarket_watchlist_generated: "관심 종목 생성 완료",
      universe_generated: "후보 종목 필터링 완료",
      paper_reconcile_blocked: "계좌 대조 차단",
      paper_order_guard: "주문 안전 조건 확인",
      paper_order_intent: "페이퍼 주문 후보 기록",
      paper_fill: "페이퍼 체결 반영",
      paper_runtime_blocked: "페이퍼 런타임 차단"
    };

    const COLUMN_LABELS = {
      time: "시간",
      level: "레벨",
      event: "이벤트",
      detail: "상세",
      symbol: "종목",
      name: "이름",
      status: "상태",
      side: "방향",
      quantity: "수량",
      filled_quantity: "체결 수량",
      remaining_quantity: "잔여 수량",
      price: "가격",
      observed_price: "관측가",
      fill_price: "체결가",
      avg_price: "평단",
      average_price: "평단",
      stop_price: "손절가",
      entry_price: "진입가",
      nearest_distance: "진입선 거리",
      created_at: "생성 시각",
      updated_at: "갱신 시각",
      reason: "사유"
    };

    const TABLE_COLUMNS = {
      watchlist: ["symbol", "name", "nearest_distance", "status", "updated_at"],
      positions: ["symbol", "status", "quantity", "average_price", "stop_price", "updated_at"],
      orders: ["symbol", "side", "quantity", "price", "status", "created_at"]
    };

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    function displayValue(value) {
      if (Array.isArray(value)) return value.length ? value.join(", ") : "-";
      if (value && typeof value === "object") return JSON.stringify(value);
      if (value === true) return "예";
      if (value === false) return "아니요";
      return value == null || value === "" ? "-" : value;
    }

    function shortTimestamp(value) {
      if (!value) return "-";
      const parsed = new Date(value);
      if (Number.isNaN(parsed.getTime())) return String(value);
      return parsed.toLocaleString("ko-KR", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit"
      });
    }

    function currentClockText() {
      const time = new Intl.DateTimeFormat("ko-KR", {
        timeZone: "Asia/Seoul",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false
      }).format(new Date());
      return `현재 ${time}`;
    }

    function updateDashboardClock() {
      const clockText = document.getElementById("dashboard-clock");
      if (clockText) clockText.textContent = currentClockText();
    }

    function eventLabel(message) {
      return EVENT_LABELS[message] || String(message || "이벤트");
    }

    function levelLabel(level) {
      const text = String(level || "INFO").toUpperCase();
      if (text === "ERROR") return "오류";
      if (text === "WARN") return "확인";
      return "정보";
    }

    function levelClass(level) {
      const text = String(level || "INFO").toUpperCase();
      if (text === "ERROR") return "error";
      if (text === "WARN") return "warn";
      return "";
    }

    function columnLabel(key) {
      return COLUMN_LABELS[key] || String(key || "").replaceAll("_", " ");
    }

    function blockerLabel(blocker) {
      const text = String(blocker || "");
      if (text.includes("TOSS_CLIENT_ID") || text.includes("TOSS_CLIENT_SECRET")) {
        return "Toss API 인증 정보가 아직 없습니다.";
      }
      if (text.includes("account_seq")) {
        return "거래 계좌 번호가 아직 연결되지 않았습니다.";
      }
      if (text.includes("runtime.symbols") || text.includes("universe_candidate_symbols")) {
        return "감시할 종목 후보가 아직 없습니다.";
      }
      if (text.includes("market_session_not_open")) {
        return "현재 시장 세션이 주문 평가 시간대가 아닙니다.";
      }
      if (text.includes("market_calendar_unknown")) {
        return "시장 개장 여부를 확인하지 못했습니다.";
      }
      if (text.includes("universe_empty")) {
        return "조건을 통과한 후보 종목이 없습니다.";
      }
      return text;
    }

    function blockerDetail(blocker) {
      const friendly = blockerLabel(blocker);
      const raw = String(blocker || "");
      return friendly === raw ? friendly : `${friendly} (${raw})`;
    }

    function groupedBlockerDetails(blockers) {
      const groups = new Map();
      blockers.forEach((blocker) => {
        const friendly = blockerLabel(blocker);
        const raw = String(blocker || "");
        const existing = groups.get(friendly) || [];
        if (friendly !== raw) existing.push(raw);
        groups.set(friendly, existing);
      });
      return [...groups.entries()].map(([friendly, raws]) => {
        const uniqueRaw = uniqueValues(raws);
        return uniqueRaw.length ? `${friendly} (${uniqueRaw.join(", ")})` : friendly;
      });
    }

    function groupedBlockerLabels(blockers) {
      return uniqueValues((blockers || []).map(blockerLabel));
    }

    function blockerShortLabel(blocker) {
      const text = String(blocker || "");
      if (text.includes("TOSS_CLIENT_ID") || text.includes("TOSS_CLIENT_SECRET")) return "Toss 인증 필요";
      if (text.includes("account_seq")) return "계좌 연결 필요";
      if (text.includes("runtime.symbols") || text.includes("universe_candidate_symbols")) return "종목 후보 없음";
      if (text.includes("market_session_not_open")) return "시장 시간 아님";
      if (text.includes("market_calendar_unknown")) return "개장 정보 확인 필요";
      if (text.includes("universe_empty")) return "후보 종목 없음";
      return blockerLabel(blocker);
    }

    function groupedBlockerShortLabels(blockers) {
      return uniqueValues((blockers || []).map(blockerShortLabel));
    }

    function primaryAction(status) {
      const blockers = Array.isArray(status && status.blockers) ? status.blockers : [];
      const first = blockers.find((blocker) => String(blocker).includes("runtime.symbols") || String(blocker).includes("universe_candidate_symbols"))
        || blockers.find((blocker) => String(blocker).includes("TOSS_CLIENT_ID") || String(blocker).includes("TOSS_CLIENT_SECRET"))
        || blockers.find((blocker) => String(blocker).includes("account_seq"))
        || blockers[0];
      if (!first) {
        return {
          title: "운영 상태를 확인하세요",
          body: "막힌 설정은 없습니다. 이벤트 탭에서 최근 heartbeat와 시장 상태를 확인하면 됩니다.",
          href: "#events",
          label: "이벤트 보기",
          kind: "done"
        };
      }
      const text = String(first);
      if (text.includes("runtime.symbols") || text.includes("universe_candidate_symbols")) {
        return {
          title: "감시 종목 후보를 먼저 넣으세요",
          body: "종목 후보가 없으면 관심 종목과 페이퍼 주문 후보를 만들 수 없습니다.",
          href: "#settings",
          label: "설정 확인",
          kind: "warn"
        };
      }
      if (text.includes("TOSS_CLIENT_ID") || text.includes("TOSS_CLIENT_SECRET")) {
        return {
          title: "Toss API 인증 정보를 설정하세요",
          body: "인증 정보가 없으면 계좌, 장 정보, 종목 데이터를 Toss에서 확인할 수 없습니다.",
          href: "#settings",
          label: "설정 확인",
          kind: "warn"
        };
      }
      if (text.includes("account_seq")) {
        return {
          title: "거래 계좌 번호를 연결하세요",
          body: "계좌가 연결되어야 포지션과 주문 상태를 읽을 수 있습니다.",
          href: "#settings",
          label: "설정 확인",
          kind: "warn"
        };
      }
      return {
        title: blockerLabel(first),
        body: "설정 탭에서 세부 항목을 확인하세요.",
        href: "#settings",
        label: "설정 확인",
        kind: "warn"
      };
    }

    function eventDetail(entry) {
      const payload = entry && entry.payload && typeof entry.payload === "object" ? entry.payload : {};
      if (Array.isArray(payload.blockers) && payload.blockers.length) {
        return uniqueValues(payload.blockers.map(blockerLabel)).join(" ");
      }
      if (payload.market_session && payload.market_session.status) {
        return `시장 상태: ${payload.market_session.status}`;
      }
      if (payload.symbol) {
        return `종목 ${payload.symbol}${payload.side ? ` / ${payload.side}` : ""}`;
      }
      if (payload.count != null) {
        return `${payload.count}건`;
      }
      return "추가 확인 사항은 없습니다.";
    }

    function statusText(kind) {
      if (kind === "done") return "완료";
      if (kind === "warn") return "진행 필요";
      if (kind === "blocked") return "차단";
      return "확인";
    }

    function uniqueValues(items) {
      return [...new Set(items.filter((item) => item != null && item !== ""))];
    }

    function getJson(path) {
      return fetch(path, { cache: "no-store" }).then((response) => {
        if (!response.ok) throw new Error(`${path} failed: ${response.status}`);
        return response.json();
      });
    }

    function setActiveView(target) {
      document.querySelectorAll(".view").forEach((view) => {
        view.classList.toggle("active", view.dataset.view === target);
      });
      document.querySelectorAll("a[data-view]").forEach((anchor) => {
        const active = anchor.dataset.view === target;
        anchor.classList.toggle("active", active);
        if (active) anchor.setAttribute("aria-current", "true");
        else anchor.removeAttribute("aria-current");
      });
    }

    function emptyState(title, body, href = "#settings", label = "설정 확인") {
      return `<div class="empty-state">
        <div class="icon-tile"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle cx="12" cy="12" r="9"></circle><path d="M8 12h8"></path><path d="M12 8v8"></path></svg></div>
        <div>
          <strong>${escapeHtml(title)}</strong>
          <p>${escapeHtml(body)} <a href="${escapeHtml(href)}" data-view="${href.replace("#", "")}">${escapeHtml(label)}</a></p>
        </div>
      </div>`;
    }

    function renderTable(elementId, rows, columns, fallbackTitle, fallbackBody, fallbackHref = "#settings") {
      const container = document.getElementById(elementId);
      if (!container) return;
      if (!rows || !rows.length) {
        container.innerHTML = emptyState(fallbackTitle, fallbackBody, fallbackHref);
        return;
      }
      const keys = columns || Object.keys(rows[0] || {});
      const head = keys.map((key) => `<th>${escapeHtml(columnLabel(key))}</th>`).join("");
      const body = rows.map((row) => `<tr>${keys.map((key) => `<td>${escapeHtml(displayValue(row[key]))}</td>`).join("")}</tr>`).join("");
      container.innerHTML = `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
    }

    function setCountBadge(id, count, suffix = "개") {
      const badge = document.getElementById(id);
      if (badge) badge.textContent = `${count}${suffix}`;
    }

    function payloadItems(payload, key) {
      if (!payload) return [];
      if (Array.isArray(payload.items)) return payload.items;
      if (Array.isArray(payload[key])) return payload[key];
      return [];
    }

    function statusKind(ready) {
      return ready ? "done" : "blocked";
    }

    function modeLabel(mode) {
      const text = String(mode || "idle");
      if (text === "paper") return "페이퍼";
      if (text === "live") return "실거래";
      if (text === "idle") return "대기";
      return text;
    }

    function renderSidebarStatus(status) {
      const readyText = document.getElementById("sidebar-ready");
      const modeText = document.getElementById("sidebar-mode");
      const blockerText = document.getElementById("sidebar-blockers");
      if (!readyText || !modeText || !blockerText) return;
      const blockers = Array.isArray(status && status.blockers) ? status.blockers : [];
      const ready = Boolean(status && status.ready);
      readyText.textContent = ready ? "운영 가능" : "확인 필요";
      readyText.className = `status-pill ${statusKind(ready)}`;
      modeText.textContent = `모드: ${modeLabel(status && status.mode)}`;
      const labels = groupedBlockerShortLabels(blockers);
      blockerText.textContent = labels.length
        ? `확인할 항목 ${labels.length}개: ${labels.slice(0, 2).join(" / ")}${labels.length > 2 ? " ..." : ""}`
        : "현재 막힌 항목이 없습니다.";
    }

    function renderMetricCards(status, watchRows, positionRows, orderRows, summary) {
      const row = document.querySelector(".stat-row");
      if (!row) return;
      const ready = Boolean(status && status.ready);
      const mode = status && status.mode ? status.mode : "idle";
      const eventTotal = summary && summary.total != null ? summary.total : 0;
      row.innerHTML = `
        <article class="card stat-card">
          <div class="icon-tile"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="m12 3 7 4v10l-7 4-7-4V7l7-4Z"></path><path d="M12 8v8"></path><path d="m9 10 3-2 3 2"></path></svg></div>
          <div class="stat-lines">
            <p class="stat-label">운영 모드</p>
            <span class="metric-value">${escapeHtml(modeLabel(mode))}</span>
            <span class="status-pill ${statusKind(ready)}">${ready ? "준비됨" : "확인 필요"}</span>
          </div>
        </article>
        <article class="card stat-card compact">
          <div class="icon-tile small"><svg viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9L12 3Z"></path></svg></div>
          <div><p class="stat-label">관심 종목</p><span class="metric-value">${watchRows.length}</span><span class="metric-note">감시 중</span></div>
        </article>
        <article class="card stat-card compact">
          <div class="icon-tile small"><svg viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M13 2a10 10 0 1 0 9 11h-9V2Z"></path><path d="M15 2.2V9h6.8A10 10 0 0 0 15 2.2Z"></path></svg></div>
          <div><p class="stat-label">보유 포지션</p><span class="metric-value">${positionRows.length}</span><span class="metric-note">open</span></div>
        </article>
        <article class="card stat-card compact">
          <div class="icon-tile small"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M8 4h8v16H8z"></path><path d="M10 8h4"></path><path d="M10 12h4"></path><path d="M10 16h2"></path></svg></div>
          <div><p class="stat-label">미체결 주문</p><span class="metric-value">${orderRows.length}</span><span class="metric-note">read-only</span></div>
        </article>
        <article class="card stat-card compact">
          <div class="icon-tile small"><svg viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M12 22a2.5 2.5 0 0 0 2.4-1.8H9.6A2.5 2.5 0 0 0 12 22ZM18 16v-5a6 6 0 1 0-12 0v5l-2 2v1h16v-1l-2-2Z"></path></svg></div>
          <div><p class="stat-label">총 이벤트</p><span class="metric-value">${eventTotal}</span><span class="metric-note">latest</span></div>
        </article>`;
    }

    function renderOperatorBrief(status, eventRows, watchRows, positionRows, orderRows) {
      const container = document.getElementById("dashboard-operator-brief");
      if (!container) return;
      const action = primaryAction(status);
      const lastEvent = eventRows && eventRows.length ? eventRows[0] : null;
      const blockers = Array.isArray(status && status.blockers) ? status.blockers : [];
      const dataSummary = [
        `관심 ${watchRows.length}개`,
        `포지션 ${positionRows.length}개`,
        `주문 ${orderRows.length}개`
      ].join(" / ");
      const lastEventTitle = lastEvent ? eventLabel(lastEvent.message) : "아직 이벤트가 없습니다";
      const lastEventBody = lastEvent
        ? `${levelLabel(lastEvent.level)} · ${eventDetail(lastEvent)} · ${shortTimestamp(lastEvent.created_at)}`
        : "페이퍼 서비스가 실행되면 최근 기록이 표시됩니다.";
      const blockerBody = blockers.length
        ? groupedBlockerShortLabels(blockers).slice(0, 3).join(" / ")
        : "차단 항목 없음";
      container.innerHTML = `
        <div class="brief-item primary ${action.kind}">
          <span class="brief-kicker">우선 확인</span>
          <strong>${escapeHtml(action.title)}</strong>
          <p>${escapeHtml(action.body)}</p>
          <a href="${escapeHtml(action.href)}" data-view="${escapeHtml(action.href.replace("#", ""))}">${escapeHtml(action.label)}</a>
        </div>
        <div class="brief-item">
          <span class="brief-kicker">최근 기록</span>
          <strong>${escapeHtml(lastEventTitle)}</strong>
          <p>${escapeHtml(lastEventBody)}</p>
        </div>
        <div class="brief-item">
          <span class="brief-kicker">데이터 상태</span>
          <strong>${escapeHtml(dataSummary)}</strong>
          <p>${escapeHtml(blockerBody)}</p>
        </div>`;
    }

    function renderHealthPanel(status) {
      const container = document.getElementById("dashboard-health-list");
      if (!container) return;
      const blockers = Array.isArray(status && status.blockers) ? status.blockers : [];
      const labels = groupedBlockerLabels(blockers);
      const rows = [
        ["상태", status && status.ready ? "준비됨" : "설정 확인 필요", status && status.ready ? "done" : "blocked"],
        ["모드", status && status.mode ? status.mode : "idle", ""],
        ["마지막 heartbeat", shortTimestamp(status && status.last_heartbeat_at), ""],
        ["마지막 이벤트", shortTimestamp(status && status.last_event_at), ""],
        ["차단 항목", blockers.length ? `${blockers.length}개` : "없음", blockers.length ? "warn" : "done"]
      ];
      container.className = "info-list";
      const nextStep = labels.length
        ? `<div class="next-step"><strong>다음 할 일</strong><p>${escapeHtml(labels[0])}</p><a href="#settings" data-view="settings">설정에서 확인</a></div>`
        : `<div class="next-step done"><strong>다음 할 일</strong><p>현재 막힌 설정이 없습니다. 최근 이벤트를 확인하세요.</p><a href="#events" data-view="events">이벤트 보기</a></div>`;
      container.innerHTML = rows.map(([label, value, kind]) => `
        <div class="info-row">
          <span class="dot"></span>
          <span><strong>${escapeHtml(label)}</strong><br>${escapeHtml(value)}</span>
          ${kind ? `<span class="status-pill ${kind}">${escapeHtml(value)}</span>` : `<span class="helper-text">read</span>`}
        </div>`).join("") + nextStep;
    }

    function renderWatchSummary(watchRows) {
      const chart = document.querySelector(".chart-area");
      if (!chart) return;
      const top = watchRows.slice(0, 5);
      if (!top.length) {
        chart.innerHTML = `<div class="summary-stack">${emptyState("관심 종목이 없습니다", "감시할 종목 후보를 설정하면 이곳에 표시됩니다.")}</div>`;
        return;
      }
      chart.innerHTML = `<div class="summary-stack">${top.map((row) => `
        <div class="summary-chip">
          <strong>${escapeHtml(row.symbol || "-")}</strong>
          <span class="helper-text">nearest ${escapeHtml(displayValue(row.nearest_distance))}</span>
        </div>`).join("")}</div>`;
    }

    function renderPositionSummary(positionRows) {
      const donut = document.querySelector(".donut-wrap");
      const strip = document.querySelector(".mini-strip");
      if (donut) {
        const open = positionRows.filter((row) => String(row.status || "").toUpperCase() === "OPEN").length;
        donut.innerHTML = `
          <div class="donut"></div>
          <div class="summary-lines">
            <div class="summary-chip"><strong>${positionRows.length} positions</strong><span class="helper-text">${open} open positions</span></div>
            <div class="summary-chip"><strong>paper mode</strong><span class="helper-text">실거래 주문은 제출하지 않습니다.</span></div>
          </div>`;
      }
      if (strip) {
        strip.innerHTML = positionRows.slice(0, 4).map((row) => `<span class="ghost-line" title="${escapeHtml(row.symbol || "-")}"></span>`).join("") || `<span class="helper-text">보유 포지션 없음</span>`;
      }
    }

    function renderEventCards(elementId, items) {
      const container = document.getElementById(elementId);
      if (!container) return;
      if (!items || !items.length) {
        container.innerHTML = emptyState("아직 이벤트가 없습니다", "페이퍼 서비스가 실행되면 시작, 차단, heartbeat 기록이 여기에 쌓입니다.", "#dashboard", "대시보드 보기");
        return;
      }
      container.innerHTML = items.slice(0, 12).map((entry) => {
        const level = String(entry.level || "INFO").toUpperCase();
        const detail = eventDetail(entry);
        return `<div class="event-card">
          <span class="level-badge ${levelClass(level)}">${escapeHtml(levelLabel(level))}</span>
          <div><strong>${escapeHtml(eventLabel(entry.message))}</strong><p>${escapeHtml(detail)}</p></div>
          <span class="helper-text">${escapeHtml(shortTimestamp(entry.created_at))}</span>
        </div>`;
      }).join("");
    }

    function renderTimeline(elementId, items) {
      const container = document.getElementById(elementId);
      if (!container) return;
      if (!items || !items.length) {
        container.innerHTML = `<li class="event-line"><span class="event-dot"></span><strong>-</strong><span>아직 이벤트가 없습니다.</span><span></span></li>`;
        return;
      }
      container.innerHTML = items.slice(0, 6).map((entry) => {
        const level = String(entry.level || "INFO").toUpperCase();
        const dot = level === "WARN" ? "warn" : level === "ERROR" ? "warn" : "ok";
        const detail = eventDetail(entry);
        const label = detail === "추가 확인 사항은 없습니다." ? eventLabel(entry.message) : `${eventLabel(entry.message)}: ${detail}`;
        return `<li class="event-line"><span class="event-dot ${dot}"></span><strong>${escapeHtml(levelLabel(level))}</strong><span>${escapeHtml(label)}</span><span class="helper-text">${escapeHtml(shortTimestamp(entry.created_at))}</span></li>`;
      }).join("");
    }

    function renderBotSummary(status, summary, watchRows, orderRows) {
      const container = document.querySelector(".bot-summary");
      if (!container) return;
      const blockers = Array.isArray(status && status.blockers) ? status.blockers.length : 0;
      const eventTotal = summary && summary.total != null ? summary.total : 0;
      const tiles = [
        ["준비 상태", status && status.ready ? "운영 가능" : `${blockers}개 확인 필요`],
        ["관심 종목", `${watchRows.length}개`],
        ["주문 상태", `${orderRows.length}개 read-only`],
        ["이벤트", `${eventTotal}개 기록`]
      ];
      container.innerHTML = tiles.map(([label, value]) => `
        <div class="summary-tile">
          <div class="icon-tile small"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M4 12h16"></path><path d="M4 7h16"></path><path d="M4 17h16"></path></svg></div>
          <div><strong>${escapeHtml(label)}</strong><br><span class="helper-text">${escapeHtml(value)}</span></div>
        </div>`).join("");
    }

    function renderLogLines(events) {
      const container = document.querySelector(".log-lines");
      if (!container) return;
      if (!events || !events.length) {
        container.innerHTML = `<div class="log-line"><span class="dot"></span><span class="helper-text">표시할 로그가 없습니다.</span><span></span></div>`;
        return;
      }
      container.innerHTML = events.slice(0, 4).map((event) => `
        <div class="log-line">
          <span class="dot"></span>
          <span class="helper-text">${escapeHtml(eventLabel(event.message))}</span>
          <span class="helper-text">${escapeHtml(event.level || "")}</span>
        </div>`).join("");
    }

    function renderEndpointList(rawLinks) {
      const container = document.getElementById("endpoint-list");
      if (!container || !rawLinks) return;
      container.innerHTML = Object.entries(rawLinks).map(([name, path]) => {
        return `<button type="button" data-endpoint="${escapeHtml(path)}">${escapeHtml(name)}: ${escapeHtml(path)}</button>`;
      }).join("");
      container.querySelectorAll("button").forEach((button) => {
        button.addEventListener("click", async () => {
          const target = button.dataset.endpoint;
          if (!target) return;
          const data = await getJson(target);
          document.getElementById("raw-endpoint-json").textContent = JSON.stringify(data, null, 2);
        });
      });
    }

    function renderOnboarding(blockers, rawLinks, events) {
      const list = document.getElementById("settings-onboarding-list");
      const rawBlockers = Array.isArray(blockers) ? blockers : [];
      const recentEvents = Array.isArray(events) ? events : [];
      const headline = document.getElementById("settings-headline");
      if (headline) {
        headline.textContent = rawBlockers.length
          ? `먼저 ${groupedBlockerDetails(rawBlockers)[0]} 항목부터 확인하세요.`
          : "필수 설정은 통과했습니다. 이벤트 탭에서 최근 heartbeat와 주문 가드를 확인하세요.";
      }
      if (list) {
        list.innerHTML = ONBOARDING_STEPS.map((step, index) => {
          const matched = rawBlockers.filter(step.match);
          const eventSeen = step.eventMessage
            ? recentEvents.some((event) => event.message === step.eventMessage)
            : true;
          const kind = matched.length || !eventSeen ? "warn" : "done";
          const detail = matched.length
            ? uniqueValues(matched.map(blockerLabel)).join(" ")
            : eventSeen
              ? step.body
              : "아직 최근 heartbeat가 보이지 않습니다.";
          return `<li>
            <strong>${index + 1}. ${escapeHtml(step.title)}</strong>
            <p><span class="status-pill">${escapeHtml(step.group)}</span></p>
            <p>${escapeHtml(detail)}</p>
            <span class="status-pill ${kind}">${statusText(kind)}</span>
          </li>`;
        }).join("");
      }
      const blockerBox = document.getElementById("settings-blockers-list");
      if (blockerBox) blockerBox.textContent = rawBlockers.length
        ? groupedBlockerDetails(rawBlockers).join("\\n")
        : "현재 표시할 차단 항목이 없습니다.";
      const rawBox = document.getElementById("settings-raw-links");
      if (rawBox) rawBox.textContent = JSON.stringify(rawLinks || {}, null, 2);
    }

    async function refresh() {
      const [dashboard, health, positions, openOrders, watchlist, events, summary] = await Promise.all([
        getJson("/dashboard"),
        getJson("/health"),
        getJson("/positions"),
        getJson("/orders/open"),
        getJson("/watchlist"),
        getJson("/events?limit=50"),
        getJson("/events/summary?limit=50")
      ]);

      const status = dashboard.status || health || {};
      const watchRows = payloadItems(watchlist, "watchlist");
      const positionRows = payloadItems(positions, "positions");
      const orderRows = payloadItems(openOrders, "open_orders").length
        ? payloadItems(openOrders, "open_orders")
        : payloadItems(dashboard.paper_intents, "open_orders");
      const eventRows = payloadItems(events, "items");

      renderMetricCards(status, watchRows, positionRows, orderRows, summary);
      renderOperatorBrief(status, eventRows, watchRows, positionRows, orderRows);
      renderSidebarStatus(status);
      renderHealthPanel(status);
      renderWatchSummary(watchRows);
      renderPositionSummary(positionRows);
      renderTable("dashboard-open-orders-table", orderRows, null, "미체결 주문이 없습니다", "페이퍼 모드에서 주문 후보가 생기면 여기에 표시됩니다.", "#events");
      renderTimeline("dashboard-events-timeline", eventRows);
      renderBotSummary(status, summary, watchRows, orderRows);
      renderLogLines(eventRows);

      setCountBadge("watchlist-count-badge", watchRows.length);
      setCountBadge("positions-count-badge", positionRows.length);
      setCountBadge("orders-count-badge", orderRows.length);
      setCountBadge("events-count-badge", eventRows.length);

      renderTable("watchlist-table", watchRows, TABLE_COLUMNS.watchlist, "관심 종목이 없습니다", "감시할 종목 후보를 설정하면 매수 후보와 진입선 거리가 표시됩니다.");
      renderTable("positions-table", positionRows, TABLE_COLUMNS.positions, "보유 포지션이 없습니다", "포지션이 열리면 수량, 평단, 손절가를 여기서 확인할 수 있습니다.", "#dashboard");
      renderTable("orders-table", orderRows, TABLE_COLUMNS.orders, "미체결 주문이 없습니다", "현재 대기 중인 주문이 없습니다. 이 화면은 주문을 실행하지 않고 상태만 보여줍니다.", "#dashboard");
      renderEventCards("events-table", eventRows);
      document.getElementById("watchlist-json").textContent = JSON.stringify(watchlist, null, 2);
      document.getElementById("positions-json").textContent = JSON.stringify(positions, null, 2);
      document.getElementById("orders-json").textContent = JSON.stringify(openOrders, null, 2);
      document.getElementById("events-json").textContent = JSON.stringify({ summary, events }, null, 2);
      document.getElementById("raw-aggregate-json").textContent = JSON.stringify(dashboard, null, 2);
      renderEndpointList(dashboard.raw_links || {});
      renderOnboarding(status.blockers || [], dashboard.raw_links || {}, eventRows);
    }

    function bindNavigation() {
      document.body.addEventListener("click", (event) => {
        const anchor = event.target.closest("a[data-view]");
        if (!anchor) return;
        const view = anchor.dataset.view;
        if (!view) return;
        event.preventDefault();
        setActiveView(view);
        history.replaceState(null, "", `#${view}`);
      });
      const button = document.getElementById("refresh-button");
      if (button) button.addEventListener("click", () => refresh().catch(console.error));
    }

    function initialView() {
      const allowed = new Set(["dashboard", "watchlist", "positions", "orders", "events", "raw", "settings"]);
      const hash = window.location.hash ? window.location.hash.slice(1) : "dashboard";
      return allowed.has(hash) ? hash : "dashboard";
    }

    bindNavigation();
    setActiveView(initialView());
    updateDashboardClock();
    window.addEventListener("hashchange", () => setActiveView(initialView()));
    refresh().catch(console.error);
    setInterval(updateDashboardClock, 1000);
    setInterval(() => refresh().catch(console.error), 6000);
  </script>
</body>
</html>"""
    return _skeleton_reference


def _legacy_dashboard_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Toss Turtle Bot Dashboard</title>
  <style>
    :root {
      --bg: #f6f8fb;
      --panel: #ffffff;
      --text: #0f172a;
      --muted: #475569;
      --line: #dbe2ea;
      --ok: #0ea5a5;
      --warn: #f59e0b;
      --bad: #dc2626;
      --shadow: 0 14px 30px rgba(15, 23, 42, 0.08);
    }

    * {
      box-sizing: border-box;
    }

    html, body {
      margin: 0;
      width: 100%;
      min-height: 100%;
      font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at 70% 10%, rgba(37, 99, 235, 0.05), transparent 30%),
        linear-gradient(180deg, #fbfcff 0%, var(--bg) 48%, #f5f8fc 100%);
      overflow-x: hidden;
    }

    a,
    button {
      font: inherit;
    }

    .topbar {
      height: 82px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 20px;
      padding: 0 28px;
      background: rgba(255, 255, 255, 0.9);
      border-bottom: 1px solid var(--line);
      backdrop-filter: blur(14px);
      -webkit-backdrop-filter: blur(14px);
      position: sticky;
      top: 0;
      z-index: 20;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 14px;
      min-width: 0;
    }

    .logo {
      width: 40px;
      height: 40px;
      border-radius: 8px;
      display: grid;
      place-items: center;
      background: linear-gradient(145deg, #3b82f6, #1d4ed8);
      box-shadow: 0 12px 24px rgba(37, 99, 235, 0.22);
      color: #ffffff;
      flex: 0 0 auto;
    }

    .logo::after {
      content: "";
      width: 18px;
      height: 18px;
      border-radius: 50%;
      background: #ffffff;
      opacity: 0.96;
    }

    .brand-text {
      min-width: 0;
      display: grid;
      gap: 3px;
    }

    .brand-text strong {
      font-size: 19px;
      line-height: 1.1;
      white-space: nowrap;
    }

    .brand-text span {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.25;
      white-space: nowrap;
    }

    .top-actions {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 10px;
      min-width: 0;
    }

    .top-clock {
      color: var(--muted);
      font-size: 13px;
      font-weight: 700;
      white-space: nowrap;
    }

    .btn {
      height: 44px;
      border-radius: 8px;
      border: 1px solid var(--line);
      background: #ffffff;
      color: #1f2a44;
      padding: 0 15px;
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-weight: 800;
      cursor: pointer;
      text-decoration: none;
      box-shadow: 0 10px 20px rgba(31, 46, 76, 0.04);
      white-space: nowrap;
    }

    .btn.primary {
      background: linear-gradient(145deg, #2f6df4, #1d4ed8);
      color: #ffffff;
      border-color: #1d4ed8;
      box-shadow: 0 14px 26px rgba(37, 99, 235, 0.22);
    }

    .btn svg {
      width: 18px;
      height: 18px;
      stroke-width: 2.4;
      flex: 0 0 auto;
    }

    .page-shell {
      min-height: calc(100vh - 82px);
      display: grid;
      grid-template-columns: 190px minmax(0, 1fr);
      width: 100%;
      min-width: 0;
    }

    .sidebar {
      background: rgba(255, 255, 255, 0.82);
      color: var(--text);
      border-right: 1px solid var(--line);
      padding: 28px 18px 22px;
      display: flex;
      flex-direction: column;
      gap: 20px;
    }

    .nav {
      display: grid;
      gap: 14px;
    }

    .nav a,
    .icon-button {
      min-height: 50px;
      color: #8492aa;
      text-decoration: none;
      border-radius: 8px;
      padding: 0 14px;
      display: flex;
      align-items: center;
      gap: 12px;
      border: 1px solid transparent;
      font-size: 13px;
      font-weight: 800;
    }

    .nav a.active {
      color: #2563eb;
      background: #edf4ff;
      border-color: #e7eefb;
    }

    .nav a svg,
    .icon-button svg {
      width: 19px;
      height: 19px;
      stroke-width: 2.2;
      flex: 0 0 auto;
    }

    .icon-button {
      margin-top: auto;
      min-height: 42px;
    }

    .main {
      min-width: 0;
      padding: 22px;
      max-width: 100%;
    }

    .view-shell {
      padding: 0;
      background: transparent;
      display: grid;
      gap: 16px;
      min-width: 0;
      max-width: 100%;
    }

    .dashboard-header {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      margin-bottom: 2px;
    }

    .dashboard-title {
      margin: 0;
      font-size: 24px;
      line-height: 1.15;
    }

    .dashboard-header > div {
      min-width: 0;
    }

    .dashboard-subtitle {
      margin: 5px 0 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }

    .card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      box-shadow: var(--shadow);
      min-width: 0;
      max-width: 100%;
    }

    .summary-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
    }

    .metric-title {
      margin: 0;
      color: var(--muted);
      font-size: 13px;
      text-transform: uppercase;
      letter-spacing: 0.2px;
    }

    .metric-value {
      font-size: 30px;
      font-weight: 700;
      display: block;
      margin-top: 6px;
    }

    .view {
      display: none;
      flex-direction: column;
      gap: 12px;
      min-width: 0;
    }

    .view.active {
      display: grid;
      gap: 14px;
    }

    .view-grid,
    .view-split,
    .raw-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 14px;
    }

    .panel-title {
      margin: 0 0 8px;
      font-size: 16px;
    }

    .status-copy,
    .status-strip,
    .status-mini {
      border-radius: 6px;
      padding: 10px;
      border: 1px solid var(--line);
      background: #f8fafc;
      color: var(--muted);
      display: grid;
      gap: 4px;
      min-width: 0;
      overflow-wrap: anywhere;
    }

    .status-mini-label {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
    }

    .status-mini-value,
    .status-copy p {
      margin: 0;
      color: var(--text);
      line-height: 1.35;
      font-size: 13px;
    }

    .action-list {
      display: grid;
      gap: 8px;
      margin: 0;
      padding: 0;
      list-style: none;
    }

    .action-list li {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 12px;
      display: grid;
      gap: 4px;
      min-width: 0;
    }

    .action-list li strong {
      font-size: 14px;
      overflow-wrap: anywhere;
    }

    .action-list li p {
      margin: 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.4;
      overflow-wrap: anywhere;
    }

    .status-pill {
      border-radius: 99px;
      padding: 2px 10px;
      width: fit-content;
      font-size: 12px;
      background: #e2e8f0;
      color: #1f2937;
      border: 1px solid #cbd5e1;
    }

    .status-pill.ok { background: #dcfce7; border-color: #86efac; color: #166534; }
    .status-pill.warn { background: #fef3c7; border-color: #f59e0b; color: #92400e; }
    .status-pill.blocked { background: #fee2e2; border-color: #fca5a5; color: #991b1b; }
    .status-pill.todo { background: #eff6ff; border-color: #bfdbfe; color: #1d4ed8; }
    .status-pill.done { background: #dcfce7; border-color: #86efac; color: #166534; }

    .detail-table {
      border: 1px solid var(--line);
      border-radius: 8px;
      margin: 0;
      padding: 10px;
      max-height: 320px;
      overflow: auto;
      background: #fff;
    }

    .health-cards {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
    }

    .health-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      background: #f8fafc;
      min-width: 0;
    }

    .health-card strong {
      display: block;
      font-size: 12px;
      color: var(--muted);
      margin-bottom: 6px;
    }

    .health-card span {
      display: block;
      font-size: 13px;
      line-height: 1.4;
      overflow-wrap: anywhere;
      color: var(--text);
    }

    .detail-table table {
      width: 100%;
      border-collapse: collapse;
    }

    .detail-table th,
    .detail-table td {
      text-align: left;
      border-bottom: 1px solid var(--line);
      font-size: 12px;
      padding: 8px 6px;
      vertical-align: top;
      overflow-wrap: anywhere;
      word-break: break-word;
    }

    .timeline {
      list-style: none;
      margin: 0;
      padding: 0;
      display: grid;
      gap: 8px;
    }

    .timeline li {
      border-left: 3px solid #94a3b8;
      padding-left: 10px;
      font-size: 13px;
      color: var(--muted);
    }

    .timeline .level-ERROR { border-color: var(--bad); color: #b91c1c; }
    .timeline .level-WARN { border-color: var(--warn); color: #b45309; }
    .timeline .level-INFO { border-color: var(--ok); color: #065f46; }

    .endpoint-list {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }

    .endpoint-list button {
      border: 1px solid #cbd5e1;
      border-radius: 99px;
      background: #fff;
      padding: 6px 10px;
      font-size: 12px;
      cursor: pointer;
    }

    .endpoint-list button.active {
      background: #e2e8f0;
      border-color: #64748b;
    }

    .view-json {
      background: #0f172a;
      color: #cbd5e1;
      border-radius: 8px;
      padding: 10px;
      margin: 0;
      overflow: auto;
      max-height: 340px;
      font-size: 12px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      white-space: pre;
      overflow-wrap: anywhere;
    }

    .view-meta {
      margin: 0 0 8px;
      color: #64748b;
      font-size: 13px;
    }

    .blocker-list {
      display: block;
      white-space: pre-wrap;
      font-size: 13px;
      color: var(--text);
      padding: 10px;
      border: 1px dashed var(--line);
      border-radius: 8px;
      background: #f8fafc;
    }

    .bottom-nav {
      position: fixed;
      left: 14px;
      right: 14px;
      bottom: calc(14px + env(safe-area-inset-bottom));
      transform: none;
      width: auto;
      background: rgba(255, 255, 255, 0.94);
      display: none;
      gap: 0;
      padding: 10px 12px;
      border: 1px solid rgba(226, 232, 240, 0.92);
      border-radius: 999px;
      box-shadow: 0 18px 45px rgba(15, 23, 42, 0.16);
      justify-content: stretch;
      z-index: 50;
      box-sizing: border-box;
      backdrop-filter: blur(18px);
      -webkit-backdrop-filter: blur(18px);
    }

    .bottom-nav a {
      color: #8b95a1;
      text-decoration: none;
      flex: 1 1 0;
      min-width: 0;
      border-radius: 18px;
      padding: 7px 2px 6px;
      display: flex;
      justify-content: center;
      align-items: center;
      gap: 5px;
      font-size: 11px;
      font-weight: 700;
      line-height: 1.1;
      text-align: center;
      border: 1px solid transparent;
      flex-direction: column;
      white-space: nowrap;
      transition: color 160ms ease, transform 160ms ease;
    }

    .bottom-nav a.active {
      color: #2563eb;
    }

    .bottom-nav a:active {
      transform: translateY(1px);
    }

    .bottom-nav a svg {
      width: 22px;
      height: 22px;
      stroke-width: 2.2;
    }

    .sr-data {
      position: absolute;
      width: 1px;
      height: 1px;
      overflow: hidden;
      left: -10000px;
      top: auto;
    }

    @media (max-width: 1100px) {
      .page-shell {
        grid-template-columns: 172px minmax(0, 1fr);
      }

      .topbar {
        padding: 0 20px;
      }

      .summary-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .top-clock {
        display: none;
      }
    }

    @media (max-width: 820px) {
      .topbar {
        display: none;
      }

      .page-shell {
        min-height: 100vh;
        display: block;
      }

      .sidebar {
        display: none;
      }

      .main {
        padding: 0;
      }

      .view-shell {
        padding: 10px;
        padding-bottom: 128px;
      }

      .summary-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .view-grid,
      .view-split,
      .raw-grid {
        grid-template-columns: 1fr;
      }

      .dashboard-header {
        display: grid;
        gap: 8px;
      }

      .dashboard-title {
        font-size: 20px;
      }

      .dashboard-subtitle {
        display: none;
      }

      .metric-value {
        font-size: 28px;
      }

      .health-cards {
        grid-template-columns: 1fr;
      }

      .bottom-nav {
        display: flex;
        left: 10px;
        right: 10px;
        width: auto;
        max-width: none;
        transform: none;
        bottom: calc(12px + env(safe-area-inset-bottom));
        padding: 9px 8px;
      }

      .bottom-nav a {
        font-size: 11px;
      }

      .bottom-nav a svg {
        width: 22px;
        height: 22px;
      }
    }

    @media (max-width: 420px) {
      .bottom-nav {
        left: 8px;
        right: 8px;
        width: auto;
        max-width: none;
        padding: 8px 6px;
      }

      .bottom-nav a {
        font-size: 10px;
        gap: 4px;
      }

      .bottom-nav a svg {
        width: 21px;
        height: 21px;
      }
    }

  </style>
</head>
<body>
  <header class="topbar">
    <div class="brand">
      <span class="logo" aria-hidden="true"></span>
      <span class="brand-text">
        <strong>Toss Turtle Bot</strong>
        <span>Read-only runtime dashboard</span>
      </span>
    </div>
    <div class="top-actions">
      <span class="top-clock">Local dashboard</span>
      <button class="btn primary" type="button" onclick="refresh()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M21 12a9 9 0 0 1-9 9 9.8 9.8 0 0 1-6.4-2.4"></path><path d="M3 12a9 9 0 0 1 15.4-6.4"></path><path d="M18 2v4h-4"></path><path d="M6 22v-4h4"></path></svg>
        새로고침
      </button>
      <a class="btn" href="/dashboard" target="_blank" rel="noopener">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"></path><path d="M14 2v6h6"></path><path d="M8 13h8"></path><path d="M8 17h5"></path></svg>
        JSON 열기
      </a>
    </div>
  </header>
  <div class="page-shell">
    <aside class="sidebar">
      <nav class="nav" aria-label="Dashboard sections">
        <a class="active" href="#dashboard" data-view="dashboard"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><rect x="3" y="3" width="7" height="7"></rect><rect x="14" y="3" width="7" height="7"></rect><rect x="3" y="14" width="7" height="7"></rect><rect x="14" y="14" width="7" height="7"></rect></svg>Dashboard</a>
        <a href="#watchlist" data-view="watchlist"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="m4 17 5-5 4 4 7-8"></path><path d="M16 8h4v4"></path></svg>Watchlist</a>
        <a href="#positions" data-view="positions"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M4 19V7a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v12"></path><path d="M8 5V3h8v2"></path><path d="M4 11h16"></path></svg>Positions</a>
        <a href="#events" data-view="events"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><rect x="7" y="3" width="10" height="18" rx="2"></rect><path d="M10 8h4"></path><path d="M10 12h4"></path><path d="M10 16h2"></path></svg>Events</a>
        <a href="#raw" data-view="raw"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M21 16v-2H3v2a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2ZM3 8h18v4H3z"></path><path d="M3 8l6 5 5-3 7 3"></path></svg>Raw/API</a>
        <a href="#settings" data-view="settings"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 0 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6V21a2 2 0 0 1-4 0v-.1a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 0 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.6-1H3a2 2 0 0 1 0-4h.1a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 0 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3 1.7 1.7 0 0 0 1-1.6V3a2 2 0 0 1 4 0v.1a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 0 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.1a2 2 0 0 1 0 4h-.1a1.7 1.7 0 0 0-1.6 1Z"></path></svg>Settings</a>
      </nav>
      <a class="icon-button" style="margin-top:auto" href="#theme" title="Theme">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M12 3a8.8 8.8 0 0 0 9 11.7A9 9 0 1 1 12 3Z"></path></svg>
      </a>
    </aside>

    <main class="main">
      <div class="view-shell">
        <header class="dashboard-header">
          <div>
            <h1 class="dashboard-title">Toss Turtle Bot</h1>
            <p class="dashboard-subtitle">Read-only dashboard for setup, scan status, paper positions, and runtime events.</p>
          </div>
          <span class="status-pill todo">Read-only</span>
        </header>

        <section id="view-dashboard" class="view active" data-view="dashboard">
          <section class="summary-grid">
            <article class="card">
              <strong class="metric-title">Mode</strong>
              <span id="metric-mode-text" class="metric-value">Loading</span>
              <span id="metric-mode-pill" class="status-pill">status</span>
            </article>
            <article class="card">
              <strong class="metric-title">Watchlist</strong>
              <span id="metric-watchlist-text" class="metric-value">0</span>
              <span class="status-pill">symbols</span>
            </article>
            <article class="card">
              <strong class="metric-title">Positions</strong>
              <span id="metric-positions-text" class="metric-value">0</span>
              <span class="status-pill">open</span>
            </article>
            <article class="card">
              <strong class="metric-title">Paper Intents</strong>
              <span id="metric-intents-text" class="metric-value">0</span>
              <span class="status-pill">today</span>
            </article>
          </section>

          <section class="card">
            <h2 class="panel-title">System Status</h2>
            <div id="dashboard-status-strip" class="status-strip status-copy"></div>
            <div id="dashboard-status-copy" class="status-copy"></div>
            <div id="dashboard-action-list" class="action-list"></div>
            <div id="dashboard-blockers-list" class="blocker-list"></div>
          </section>

          <section class="card">
            <h2 class="panel-title">Health Details</h2>
            <div id="dashboard-health-list" class="detail-table"></div>
          </section>

          <section class="card">
            <h2 class="panel-title">Open Orders</h2>
            <div id="dashboard-open-orders-table" class="detail-table"></div>
          </section>

          <section class="card">
            <h2 class="panel-title">Events Summary</h2>
            <div id="dashboard-events-summary" class="status-copy"></div>
          </section>

          <section class="card">
            <h2 class="panel-title">Latest Events</h2>
            <ul id="dashboard-events-timeline" class="timeline"></ul>
          </section>
        </section>

        <section id="view-watchlist" class="view" data-view="watchlist">
          <section class="view-grid">
            <article class="card">
              <h2 class="panel-title">Watchlist Items</h2>
              <div id="watchlist-table" class="detail-table"></div>
            </article>
            <article class="card">
              <h2 class="panel-title">Watchlist Raw JSON</h2>
              <pre id="watchlist-json" class="view-json"></pre>
            </article>
          </section>
          <section class="card">
            <h2 class="panel-title">Watchlist Signals</h2>
            <div class="status-mini">
              <span class="status-mini-label">Source</span>
              <span class="status-mini-value">Watchlist data is loaded from /watchlist and dashboard data.</span>
            </div>
          </section>
        </section>

        <section id="view-positions" class="view" data-view="positions">
          <section class="view-grid">
            <article class="card">
              <h2 class="panel-title">Positions Items</h2>
              <div id="positions-table" class="detail-table"></div>
            </article>
            <article class="card">
              <h2 class="panel-title">Positions Raw JSON</h2>
              <pre id="positions-json" class="view-json"></pre>
            </article>
          </section>
          <section class="card">
            <h2 class="panel-title">Position Signals</h2>
            <div class="status-mini">
              <span class="status-mini-label">Signal</span>
              <span class="status-mini-value">Position data is loaded from /positions and dashboard health checks.</span>
            </div>
          </section>
        </section>

        <section id="view-orders" class="view" data-view="orders">
          <section class="view-grid">
            <article class="card">
              <h2 class="panel-title">Open Orders</h2>
              <div id="orders-table" class="detail-table"></div>
            </article>
            <article class="card">
              <h2 class="panel-title">Orders Raw JSON</h2>
              <pre id="orders-json" class="view-json"></pre>
            </article>
          </section>
          <section class="card">
            <h2 class="panel-title">Order Signals</h2>
            <div class="status-mini">
              <span class="status-mini-label">Source</span>
              <span class="status-mini-value">Open order data is loaded from /orders/open and dashboard paper intents.</span>
            </div>
          </section>
        </section>

        <section id="view-events" class="view" data-view="events">
          <section class="view-grid">
            <article class="card">
              <h2 class="panel-title">Events Summary</h2>
              <div id="events-summary-strip" class="status-strip status-copy"></div>
            </article>
            <article class="card">
              <h2 class="panel-title">Events Timeline</h2>
              <ul id="events-timeline" class="timeline"></ul>
            </article>
          </section>
          <section class="card">
            <h2 class="panel-title">Event Items</h2>
            <div id="events-table" class="detail-table"></div>
          </section>
          <section class="card">
            <h2 class="panel-title">Events Raw JSON</h2>
            <pre id="events-json" class="view-json"></pre>
          </section>
        </section>

        <section id="view-raw" class="view" data-view="raw">
          <section class="view-grid raw-grid">
            <article class="card">
              <h2 class="panel-title">Raw Endpoint List</h2>
              <div id="endpoint-list" class="endpoint-list"></div>
            </article>
            <article class="card">
              <h2 class="panel-title">Endpoint JSON</h2>
              <p class="view-meta">Selected endpoint: <span id="raw-endpoint-label"></span></p>
              <pre id="raw-endpoint-json" class="view-json"></pre>
            </article>
          </section>
          <section class="card">
            <h2 class="panel-title">Aggregate Payload</h2>
            <pre id="raw-aggregate-json" class="view-json"></pre>
          </section>
        </section>

        <section id="view-settings" class="view" data-view="settings">
          <section class="card">
            <h2 class="panel-title">Settings and Toss Onboarding</h2>
            <p id="settings-headline" class="status-copy">
              <strong>New user onboarding</strong>
              This guide helps you set up Toss credentials safely before running paper service.
            </p>
            <ul id="settings-onboarding-list" class="action-list"></ul>
          </section>
          <section class="card">
            <h2 class="panel-title">Credential Safety</h2>
            <div class="status-copy">
              <strong>Never paste secrets in chat.</strong>
              <p>Store <code>TOSS_CLIENT_ID</code> and <code>TOSS_CLIENT_SECRET</code> only in environment variables or OS secure storage.</p>
              <p>Do not commit <code>config/local.yaml</code> or any secret values to git.</p>
            </div>
          </section>
          <section class="card">
            <h2 class="panel-title">Checklist Signals</h2>
            <div id="settings-blockers-list" class="blocker-list"></div>
            <div class="status-copy">
              <strong>Raw/API diagnostics</strong>
              <pre id="settings-raw-links" class="view-json"></pre>
            </div>
          </section>
        </section>

        <p class="sr-data">Local dashboard only. This interface reads bot state and never submits orders. Data is loaded from read-only endpoints.</p>
      </div>
    </main>
  </div>

  <nav class="bottom-nav" aria-label="Mobile dashboard sections">
    <a class="active" href="#dashboard" data-view="dashboard"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><rect x="3" y="3" width="7" height="7"></rect><rect x="14" y="3" width="7" height="7"></rect><rect x="3" y="14" width="7" height="7"></rect><rect x="14" y="14" width="7" height="7"></rect></svg>대시보드</a>
    <a href="#watchlist" data-view="watchlist"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="m4 17 5-5 4 4 7-8"></path><path d="M16 8h4v4"></path></svg>관심 종목</a>
    <a href="#positions" data-view="positions"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M13 2.1a10 10 0 1 0 8.9 8.9H13V2.1Z"></path><path d="M15 2.1V9h6.9"></path></svg>포지션</a>
    <a href="#orders" data-view="orders"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><rect x="7" y="3" width="10" height="18" rx="2"></rect><path d="M10 8h4"></path><path d="M10 12h4"></path><path d="M10 16h2"></path></svg>주문</a>
    <a href="#settings" data-view="settings"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 0 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6V21a2 2 0 0 1-4 0v-.1a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 0 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.6-1H3a2 2 0 0 1 0-4h.1a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 0 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3 1.7 1.7 0 0 0 1-1.6V3a2 2 0 0 1 4 0v.1a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 0 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.1a2 2 0 0 1 0 4h-.1a1.7 1.7 0 0 0-1.6 1Z"></path></svg>설정</a>
  </nav>

  <script>
    const ONBOARDING_STEPS = [
      { title: "Step 1: Prepare config/local.yaml", body: "Keep config local and live trading off." },
      { title: "Step 2: Issue Toss API credentials", body: "Create a Toss app and get the two API keys." },
      { title: "Step 3: Store credentials safely", body: "Store keys locally, not in chat or git." },
      { title: "Step 4: Configure toss.account_seq", body: "Add the target account sequence locally." },
      { title: "Step 5: Add scan universe and symbols", body: "Add symbols for the scanner to inspect." },
      { title: "Step 6: Run paper service and verify events", body: "Run paper service, then check Events." }
    ];

    function setActiveView(target) {
      document.querySelectorAll('.view').forEach((view) => {
        view.classList.toggle('active', view.getAttribute('data-view') === target);
      });
      document.querySelectorAll('[href^=\"#\"]').forEach((anchor) => {
        if (anchor.getAttribute('data-view') === target) {
          anchor.classList.add('active');
          anchor.setAttribute('aria-current', 'true');
        } else if (anchor.classList.contains('active') && anchor.getAttribute('href').startsWith('#')) {
          anchor.classList.remove('active');
          anchor.removeAttribute('aria-current');
        }
      });
    }

    function setText(elementId, value) {
      const element = document.getElementById(elementId);
      if (!element) {
        return;
      }
      element.textContent = value == null ? "" : String(value);
    }

    function setBadge(elementId, value, kind) {
      const element = document.getElementById(elementId);
      if (!element) {
        return;
      }
      element.textContent = value;
      element.classList.remove("ok", "warn", "blocked");
      if (kind === "ok") {
        element.classList.add("ok");
      } else if (kind === "warn") {
        element.classList.add("warn");
      } else if (kind === "blocked") {
        element.classList.add("blocked");
      }
    }

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    function displayValue(value) {
      if (Array.isArray(value)) {
        return value.length ? value.join(", ") : "-";
      }
      if (value && typeof value === "object") {
        return JSON.stringify(value);
      }
      if (value === true) {
        return "true";
      }
      if (value === false) {
        return "false";
      }
      return value == null || value === "" ? "-" : value;
    }

    function renderHealthDetails(elementId, status) {
      const container = document.getElementById(elementId);
      if (!container) {
        return;
      }
      if (!status) {
        container.innerHTML = `<p class="status-mini-value">No health data</p>`;
        return;
      }
      const blockers = Array.isArray(status.blockers) && status.blockers.length
        ? status.blockers.join("\\n")
        : "No blockers";
      const items = [
        ["Status", status.status || "unknown"],
        ["Mode", status.mode || "idle"],
        ["Ready", status.ready ? "true" : "false"],
        ["Blockers", blockers],
        ["Last heartbeat", status.last_heartbeat_at || "-"],
        ["Last event", status.last_event_at || "-"]
      ];
      container.innerHTML = `<div class="health-cards">${items.map(([label, value]) => `
        <div class="health-card">
          <strong>${escapeHtml(label)}</strong>
          <span>${escapeHtml(value)}</span>
        </div>`).join("")}</div>`;
    }

    function renderActionList(status) {
      const container = document.getElementById("dashboard-action-list");
      if (!container) {
        return;
      }
      const blockers = Array.isArray(status && status.blockers) ? status.blockers : [];
      const actions = [];
      if (blockers.some((item) => item.includes("TOSS_CLIENT_ID") || item.includes("TOSS_CLIENT_SECRET"))) {
        actions.push(["Toss API credentials", "Issue the Toss API keys and store them locally."]);
      }
      if (blockers.some((item) => item.includes("account_seq"))) {
        actions.push(["Account sequence", "Add the target account sequence to local config."]);
      }
      if (blockers.some((item) => item.includes("runtime.symbols") || item.includes("universe_candidate_symbols"))) {
        actions.push(["Scan universe", "Add symbols so the bot has something to scan."]);
      }
      if (!actions.length) {
        actions.push(["Ready for paper checks", "No setup blocker is visible. Use Events to confirm the latest paper-service run."]);
      }
      container.innerHTML = actions.map(([title, body]) => `
        <li>
          <strong>${escapeHtml(title)}</strong>
          <p>${escapeHtml(body)}</p>
        </li>`).join("");
    }

    function renderTable(elementId, rows, columns, fallback) {
      const container = document.getElementById(elementId);
      if (!container) {
        return;
      }
      if (!rows || !rows.length) {
        container.innerHTML = `<p class="status-mini-value">${escapeHtml(fallback)}</p>`;
        return;
      }
      const keys = columns || Object.keys(rows[0] || {});
      const header = `<tr>${keys.map((column) => `<th>${escapeHtml(column)}</th>`).join("")}</tr>`;
      const body = rows
        .map((item) => `<tr>${keys.map((column) => `<td>${escapeHtml(displayValue(item[column]))}</td>`).join("")}</tr>`)
        .join("");
      container.innerHTML = `<table><thead>${header}</thead><tbody>${body}</tbody></table>`;
    }

    function renderTimeline(elementId, items) {
      const container = document.getElementById(elementId);
      if (!container) {
        return;
      }
      if (!items || !items.length) {
        container.innerHTML = "<li>No events yet.</li>";
        return;
      }
      container.innerHTML = items.map((entry) => {
        const level = (entry.level || "UNKNOWN");
        const created = entry.created_at || "";
        const blockers = Array.isArray(entry.payload && entry.payload.blockers)
          ? entry.payload.blockers.join(", ")
          : "";
        const suffix = blockers ? ` (${blockers})` : "";
        return `<li class=\"level-${level}\"><strong>${created}</strong> [${level}] ${entry.message || "unknown"}${suffix}</li>`;
      }).join("");
    }

    function renderEndpointList(rawLinks) {
      const endpointList = document.getElementById('endpoint-list');
      if (!endpointList || !rawLinks) {
        return;
      }
      endpointList.innerHTML = Object.entries(rawLinks).map(([name, path]) => {
        return `<button data-endpoint="${path}" type="button">${name}: ${path}</button>`;
      }).join("");
      endpointList.querySelectorAll('button').forEach((button) => {
        button.addEventListener('click', async (event) => {
          const target = event.currentTarget.getAttribute('data-endpoint');
          if (!target) {
            return;
          }
          endpointList.querySelectorAll('button').forEach((element) => element.classList.remove('active'));
          event.currentTarget.classList.add('active');
          document.getElementById('raw-endpoint-label').textContent = target;
          const data = await getJson(target);
          const payload = JSON.stringify(data, null, 2);
          document.getElementById('raw-endpoint-json').textContent = payload;
          document.getElementById('raw-aggregate-json').textContent = payload;
        });
      });
    }

    function setupStepState(index, blockers) {
      const joined = (blockers || []).join(" ");
      if (index === 1 || index === 2) {
        return joined.includes("TOSS_CLIENT_ID") || joined.includes("TOSS_CLIENT_SECRET")
          ? ["Needed", "todo"]
          : ["Done", "done"];
      }
      if (index === 3) {
        return joined.includes("account_seq") ? ["Needed", "todo"] : ["Done", "done"];
      }
      if (index === 4) {
        return joined.includes("runtime.symbols") || joined.includes("universe_candidate_symbols")
          ? ["Needed", "todo"]
          : ["Done", "done"];
      }
      if (index === 5) {
        return blockers && blockers.length ? ["Waiting", "warn"] : ["Ready", "done"];
      }
      return ["Check", "warn"];
    }

    function renderOnboarding(blockers, rawLinks) {
      const list = document.getElementById('settings-onboarding-list');
      if (!list) {
        return;
      }
      const status = blockers && blockers.length ? `${blockers.length} blockers` : "No blockers";
      const badgeType = blockers && blockers.length ? "warn" : "ok";
      const items = ONBOARDING_STEPS.map((step, index) => {
        const [label, kind] = setupStepState(index, blockers || []);
        return `
        <li>
          <strong>${escapeHtml(step.title)}</strong>
          <p>${escapeHtml(step.body)}</p>
          <span class="status-pill ${kind}">${escapeHtml(label)}</span>
        </li>`;
      }).join("");
      list.innerHTML = `${items}<li><strong>Current readiness</strong><p>${escapeHtml(status)}</p><span class="status-pill ${badgeType}">${escapeHtml(status)}</span></li>`;
      const blockersPanel = document.getElementById('settings-blockers-list');
      if (blockersPanel) {
        blockersPanel.textContent = blockers && blockers.length ? blockers.join("\\n") : "No blockers detected.";
      }
      const rawPanel = document.getElementById('settings-raw-links');
      if (rawPanel) {
        rawPanel.textContent = JSON.stringify(rawLinks || {}, null, 2);
      }
      const headline = document.getElementById('settings-headline');
      if (headline) {
        headline.innerHTML = `<strong>New user onboarding</strong> Start with the first Needed step.`;
      }
    }

    async function getJson(path) {
      const response = await fetch(path, { cache: "no-store" });
      if (!response.ok) {
        const body = await response.text();
        throw new Error(`${path} failed: ${response.status} ${body}`);
      }
      return response.json();
    }

    async function refresh() {
      try {
        const [dashboard, health, positions, openOrders, watchlist, events, summary] = await Promise.all([
          getJson("/dashboard"),
          getJson("/health"),
          getJson("/positions"),
          getJson("/orders/open"),
          getJson("/watchlist"),
          getJson("/events?limit=50"),
          getJson("/events/summary?limit=50")
        ]);

        const status = dashboard.status || {};
        setText("metric-mode-text", status.mode || "idle");
        setText("metric-watchlist-text", watchlist.count || 0);
        setText("metric-positions-text", positions.count || 0);
        setText("metric-intents-text", (dashboard.paper_intents || {}).count || 0);
        setBadge("metric-mode-pill", status.ready ? "READY" : "BLOCKED", status.ready ? "ok" : "blocked");
        setText("dashboard-status-strip", status.ready ? "Paper service can run" : "Setup is not complete");
        setText("dashboard-status-copy", status.ready
          ? "No setup blocker is visible."
          : "Fix setup items below. Read-only.");
        renderActionList(status);

        renderTimeline("dashboard-events-timeline", dashboard.runtime_events ? dashboard.runtime_events.items || [] : []);
        renderHealthDetails("dashboard-health-list", dashboard.status);
        renderTable("dashboard-open-orders-table", (dashboard.paper_intents || {}).items || [], null, "No paper intents");
        renderTable("orders-table", openOrders.items || (dashboard.paper_intents || {}).items || [], null, "No open orders");

        setText("dashboard-events-summary", `Total events: ${(dashboard.runtime_summary || {}).total || 0}`);
        renderTable("watchlist-table", watchlist.items || [], null, "No watchlist entries");
        renderTable("positions-table", positions.items || [], null, "No positions");
        renderTimeline("events-timeline", events.items || []);
        renderTable("events-table", events.items || [], ["id", "level", "message", "created_at"], "No events yet");
        setText("events-summary-strip", `Total events: ${summary.total || 0}`);

        document.getElementById("watchlist-json").textContent = JSON.stringify(watchlist, null, 2);
        document.getElementById("positions-json").textContent = JSON.stringify(positions, null, 2);
        document.getElementById("orders-json").textContent = JSON.stringify(openOrders, null, 2);
        document.getElementById("events-json").textContent = JSON.stringify({ summary, events }, null, 2);
        document.getElementById("raw-aggregate-json").textContent = JSON.stringify(dashboard, null, 2);

        if (dashboard.raw_links) {
          renderEndpointList(dashboard.raw_links);
          renderOnboarding(status.blockers || [], dashboard.raw_links);
        }
      } catch (error) {
        console.error(error);
      }
    }

    function bindNavigation() {
      document.querySelectorAll('a[data-view]').forEach((anchor) => {
        anchor.addEventListener('click', (event) => {
          const href = anchor.getAttribute('href');
          if (!href || !href.startsWith('#')) {
            return;
          }
          const target = href.substring(1);
          event.preventDefault();
          setActiveView(target);
          history.replaceState(null, "", `#${target}`);
        });
      });
    }

    function initialView() {
      const allowed = new Set(["dashboard", "watchlist", "positions", "orders", "events", "raw", "settings"]);
      const current = window.location.hash ? window.location.hash.substring(1) : "dashboard";
      return allowed.has(current) ? current : "dashboard";
    }

    bindNavigation();
    setActiveView(initialView());
    window.addEventListener("hashchange", () => setActiveView(initialView()));
    refresh();
    setInterval(refresh, 6000);
  </script>
</body>
</html>"""


class HealthServer:
    """Read-only health payload producer and optional local HTTP endpoint host."""

    def __init__(
        self,
        snapshot_provider: PayloadProvider | HealthSnapshot,
        *,
        events_provider: EventsProvider | None = None,
        host: str = "127.0.0.1",
        port: int = 8765,
        start_server: bool = False,
    ) -> None:
        self._snapshot_provider = (
            snapshot_provider
            if callable(snapshot_provider)
            else lambda: snapshot_provider
        )
        self._events_provider = events_provider if events_provider is not None else (lambda *_: [])
        self.host = host
        self.port = port
        self._server: HTTPServer | None = None
        if start_server:
            self.start()

    def _snapshot(self) -> HealthSnapshot:
        raw = self._snapshot_provider()
        if isinstance(raw, HealthSnapshot):
            return raw
        if isinstance(raw, Mapping):
            return _normalize_payload(raw)
        return HealthSnapshot()

    def _events(self, limit: int | None = None) -> list[Mapping[str, Any]]:
        if limit is None:
            try:
                events = self._events_provider()
            except TypeError:
                events = self._events_provider(None)
        else:
            try:
                events = self._events_provider(limit)
            except TypeError:
                events = self._events_provider()
        if not isinstance(events, list):
            return []
        if limit is not None:
            return events[:limit]
        return events

    def _event_payload_items(self, query: Mapping[str, list[str]] | None = None) -> list[Mapping[str, Any]]:
        limit = None
        if query is not None:
            raw_limit = query.get("limit")
            if raw_limit:
                try:
                    limit = int(str(raw_limit[0]))
                except ValueError:
                    limit = None
        events = self._events(limit)
        return _coerce_events_payload(events)

    def _events_summary(self, query: Mapping[str, list[str]] | None = None) -> dict[str, Any]:
        events = self._events()
        if query is not None:
            raw_date = query.get("date")
            if raw_date:
                try:
                    target = date_cls.fromisoformat(str(raw_date[0]))
                    events = _events_for_day(events, target)
                except ValueError:
                    pass
        return _summarize_events(events)

    def _dashboard_payload(self) -> dict[str, Any]:
        snapshot = self._snapshot()
        events = self._events()
        first = _first_day_event(events)
        status = snapshot.status_payload()
        if first is not None:
            status["last_event_at"] = _iso_datetime(first.get("created_at"))

        return {
            "generated_at": snapshot.generated_at.isoformat(),
            "status": status,
            "watchlist": {
                **snapshot.watchlist_payload(),
                "generated_at": None,
            },
            "positions": snapshot.positions_payload(),
            "paper_intents": snapshot.open_orders_payload(),
            "runtime_events": {
                "count": len(events),
                "items": _coerce_events_payload(events),
            },
            "runtime_summary": _summarize_events(events),
            "raw_links": {
                "health": "/health",
                "positions": "/positions",
                "open_orders": "/orders/open",
                "watchlist": "/watchlist",
                "events": "/events",
                "events_summary": "/events/summary?limit=50",
                "dashboard": "/dashboard",
            },
        }

    def payload_for_path(
        self,
        path: str,
        query: Mapping[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        normalized = path.lower()
        snapshot = self._snapshot()

        if normalized in {"/health", "/status"}:
            return snapshot.as_payload()
        if normalized == "/positions":
            return snapshot.positions_payload()
        if normalized == "/orders/open":
            return snapshot.open_orders_payload()
        if normalized == "/watchlist":
            return snapshot.watchlist_payload()
        if normalized == "/events":
            return {
                "count": len(self._event_payload_items(query)),
                "items": self._event_payload_items(query),
            }
        if normalized == "/events/summary":
            return self._events_summary(query)
        if normalized == "/dashboard":
            return self._dashboard_payload()
        raise ValueError(f"unsupported read-only path: {path}")

    def start(self) -> None:
        if self._server is not None:
            return

        server_ref = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse(self.path)
                path = parsed.path or "/"
                query = parse_qs(parsed.query)

                if path in {"/", "/dashboard.html"}:
                    body = dashboard_html().encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if path == "/assets/toss-symbol.png":
                    try:
                        body = TOSS_LOGO_ASSET.read_bytes()
                    except OSError:
                        self.send_response(404)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"error": "not found"}).encode("utf-8"))
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Cache-Control", "public, max-age=3600")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if path in {
                    "/health",
                    "/status",
                    "/positions",
                    "/orders/open",
                    "/watchlist",
                    "/events",
                    "/events/summary",
                    "/dashboard",
                }:
                    payload = server_ref.payload_for_path(path, query)
                    body = json.dumps(payload).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                body = json.dumps({"error": "not found"}).encode("utf-8")
                self.wfile.write(body)

            def do_POST(self):
                self.send_response(405)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                body = json.dumps({"error": "method not allowed"}).encode("utf-8")
                self.wfile.write(body)

            def log_message(
                self,
                format: str,
                *args: object,
            ) -> None:  # pragma: no cover
                return

        self._server = HTTPServer((self.host, self.port), _Handler)
        thread = Thread(target=self._server.serve_forever, daemon=True)
        thread.start()

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
