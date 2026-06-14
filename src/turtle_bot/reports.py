from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol
from zoneinfo import ZoneInfo

from .domain import PositionState
from .watchlist import Watchlist, WatchlistRow


class DailyReportStore(Protocol):
    def list_runtime_events(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        ...

    def load_latest_watchlist(self, *, name: str = "premarket") -> Watchlist | None:
        ...

    def list_paper_positions(self, *, status=None) -> list[PositionState]:
        ...

    def latest_broker_snapshot(self, kind: str) -> dict[str, Any] | None:
        ...


@dataclass(frozen=True)
class DailyReportConfig:
    report_date: date
    timezone_name: str = "Asia/Seoul"
    watchlist_name: str = "premarket"
    event_limit: int | None = None


def build_daily_report(
    store: DailyReportStore,
    *,
    config: DailyReportConfig,
) -> dict[str, Any]:
    events = _events_for_date(
        store.list_runtime_events(limit=config.event_limit),
        report_date=config.report_date,
        timezone_name=config.timezone_name,
    )
    summary = summarize_runtime_events(events)
    watchlist = store.load_latest_watchlist(name=config.watchlist_name)

    return {
        "report_type": "postmarket_daily",
        "report_date": config.report_date.isoformat(),
        "timezone": config.timezone_name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runtime_event_summary": summary,
        "watchlist": _watchlist_payload(watchlist),
        "paper_positions": [
            _position_payload(position)
            for position in store.list_paper_positions()
        ],
        "broker_snapshots": {
            "holdings": store.latest_broker_snapshot("holdings"),
            "open_orders": store.latest_broker_snapshot("open_orders"),
        },
        "ai_summary_context": {
            "allowed_use": (
                "AI may summarize these recorded facts for the operator, but "
                "must not create signals, choose symbols, change risk, or "
                "override blockers."
            ),
            "facts_only": True,
        },
    }


def export_daily_report_json(
    store: DailyReportStore,
    path: str | Path,
    *,
    config: DailyReportConfig,
) -> dict[str, Any]:
    report = build_daily_report(store, config=config)
    report_path = Path(path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return report


def summarize_runtime_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    by_level = Counter(str(event.get("level", "UNKNOWN")) for event in events)
    by_message = Counter(str(event.get("message", "")) for event in events)
    blockers = _collect_blockers(events)
    first_event = events[-1]["created_at"].isoformat() if events else None
    last_event = events[0]["created_at"].isoformat() if events else None

    return {
        "total": len(events),
        "first_event_at": first_event,
        "last_event_at": last_event,
        "by_level": dict(sorted(by_level.items())),
        "by_message": dict(sorted(by_message.items())),
        "blockers": blockers,
        "paper_order_intents": by_message.get("paper_order_intent", 0),
        "paper_fills": by_message.get("paper_fill", 0),
        "paper_guard_checks": by_message.get("paper_order_guard", 0),
        "shadow_order_intents": by_message.get("shadow_order_intent", 0),
        "shadow_fills": by_message.get("shadow_fill", 0),
        "shadow_guard_checks": by_message.get("shadow_order_guard", 0),
        "paper_runtime_blocks": sum(
            count
            for message, count in by_message.items()
            if "blocked" in message or message.endswith("_closed")
        ),
    }


def _events_for_date(
    events: list[dict[str, Any]],
    *,
    report_date: date,
    timezone_name: str,
) -> list[dict[str, Any]]:
    tz = ZoneInfo(timezone_name)
    return [
        event
        for event in events
        if _event_date(event, tz) == report_date
    ]


def _event_date(event: Mapping[str, Any], tz: ZoneInfo) -> date | None:
    created_at = event.get("created_at")
    if not isinstance(created_at, datetime):
        return None
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at.astimezone(tz).date()


def _collect_blockers(events: list[dict[str, Any]]) -> list[str]:
    blockers: list[str] = []
    for event in events:
        payload = event.get("payload")
        if isinstance(payload, Mapping):
            blockers.extend(_blockers_from_payload(payload))
    return sorted(set(blockers))


def _blockers_from_payload(payload: Mapping[str, Any]) -> list[str]:
    blockers: list[str] = []
    raw_blockers = payload.get("blockers")
    if isinstance(raw_blockers, list):
        blockers.extend(str(item) for item in raw_blockers)
    raw_blocker = payload.get("blocker")
    if raw_blocker:
        blockers.append(str(raw_blocker))
    market_session = payload.get("market_session")
    if isinstance(market_session, Mapping) and market_session.get("blocker"):
        blockers.append(str(market_session["blocker"]))
    return blockers


def _watchlist_payload(watchlist: Watchlist | None) -> dict[str, Any]:
    if watchlist is None:
        return {"generated_at": None, "count": 0, "items": []}
    return {
        "generated_at": watchlist.generated_at.isoformat(),
        "count": len(watchlist.rows),
        "items": [_watchlist_row_payload(row) for row in watchlist.rows],
    }


def _watchlist_row_payload(row: WatchlistRow) -> dict[str, Any]:
    return {
        "symbol": row.symbol,
        "current_price": str(row.current_price),
        "entry_high_20": str(row.entry_high_20) if row.entry_high_20 is not None else None,
        "entry_high_55": str(row.entry_high_55) if row.entry_high_55 is not None else None,
        "distance_to_20": str(row.distance_to_20) if row.distance_to_20 is not None else None,
        "distance_to_55": str(row.distance_to_55) if row.distance_to_55 is not None else None,
        "nearest_distance": str(row.nearest_distance),
        "is_new": row.is_new,
    }


def _position_payload(position: PositionState) -> dict[str, Any]:
    return {
        "symbol": position.symbol,
        "system": position.system.value,
        "status": position.status.value,
        "total_qty": str(position.total_qty),
        "avg_entry_price": str(position.avg_entry_price),
        "entry_n": str(position.entry_n),
        "current_stop_price": str(position.current_stop_price),
        "last_unit_entry_price": str(position.last_unit_entry_price),
        "units": len(position.units),
    }
