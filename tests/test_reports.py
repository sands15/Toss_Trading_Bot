from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal

from turtle_bot.domain import PositionState, PositionStatus, TurtleSystem, UnitState
from turtle_bot.cli import run
from turtle_bot.reports import (
    DailyReportConfig,
    build_daily_report,
    export_daily_report_json,
    summarize_runtime_events,
)
from turtle_bot.state_store import SQLiteStateStore
from turtle_bot.watchlist import Watchlist, WatchlistRow


def _watchlist() -> Watchlist:
    return Watchlist(
        generated_at=datetime(2026, 6, 12, 7, 30, tzinfo=timezone.utc),
        rows=(
            WatchlistRow(
                symbol="AAA",
                current_price=Decimal("100"),
                entry_high_20=Decimal("101"),
                entry_high_55=Decimal("110"),
                distance_to_20=Decimal("1"),
                distance_to_55=Decimal("10"),
                nearest_distance=Decimal("1"),
                is_new=True,
            ),
        ),
    )


def _position() -> PositionState:
    return PositionState(
        symbol="AAA",
        system=TurtleSystem.S1,
        status=PositionStatus.OPEN,
        total_qty=Decimal("2"),
        avg_entry_price=Decimal("100"),
        entry_n=Decimal("2"),
        current_stop_price=Decimal("96"),
        last_unit_entry_price=Decimal("100"),
        units=(
            UnitState(
                unit_no=1,
                qty=Decimal("2"),
                entry_price=Decimal("100"),
                n_at_entry=Decimal("2"),
                stop_price=Decimal("96"),
            ),
        ),
    )


def test_summarize_runtime_events_counts_messages_and_blockers() -> None:
    events = [
        {
            "level": "WARN",
            "message": "paper_service_market_closed",
            "payload": {
                "blockers": ["market_session_not_open:holiday"],
                "market_session": {"blocker": "market_session_not_open:holiday"},
            },
            "created_at": datetime(2026, 6, 12, 16, 0, tzinfo=timezone.utc),
        },
        {
            "level": "INFO",
            "message": "paper_order_intent",
            "payload": {"symbol": "AAA"},
            "created_at": datetime(2026, 6, 12, 15, 0, tzinfo=timezone.utc),
        },
    ]

    summary = summarize_runtime_events(events)

    assert summary["total"] == 2
    assert summary["by_level"] == {"INFO": 1, "WARN": 1}
    assert summary["by_message"]["paper_order_intent"] == 1
    assert summary["paper_order_intents"] == 1
    assert summary["paper_runtime_blocks"] == 1
    assert summary["blockers"] == ["market_session_not_open:holiday"]


def test_build_daily_report_includes_events_watchlist_positions_and_snapshots() -> None:
    with SQLiteStateStore() as store:
        store.save_watchlist(_watchlist(), name="premarket")
        store.save_paper_position(_position())
        store.record_broker_snapshot("holdings", {"items": [{"symbol": "AAA"}]})
        store.record_broker_snapshot("open_orders", {"orders": []})
        store.record_runtime_event("INFO", "paper_order_guard", {"passed": True})
        store.record_runtime_event("INFO", "paper_order_intent", {"symbol": "AAA"})
        report = build_daily_report(
            store,
            config=DailyReportConfig(
                report_date=datetime.now(timezone.utc).date(),
                timezone_name="UTC",
            ),
        )

    assert report["report_type"] == "postmarket_daily"
    assert report["runtime_event_summary"]["paper_order_intents"] == 1
    assert report["watchlist"]["items"][0]["symbol"] == "AAA"
    assert report["paper_positions"][0]["symbol"] == "AAA"
    assert report["broker_snapshots"]["holdings"] == {"items": [{"symbol": "AAA"}]}
    assert report["ai_summary_context"]["facts_only"] is True


def test_export_daily_report_json_writes_reviewable_file(tmp_path) -> None:
    report_path = tmp_path / "reports" / "daily.json"
    with SQLiteStateStore() as store:
        store.record_runtime_event("WARN", "paper_runtime_blocked", {"blockers": ["stale"]})
        report = export_daily_report_json(
            store,
            report_path,
            config=DailyReportConfig(
                report_date=datetime.now(timezone.utc).date(),
                timezone_name="UTC",
            ),
        )

    loaded = json.loads(report_path.read_text(encoding="utf-8"))
    assert loaded == report
    assert loaded["runtime_event_summary"]["blockers"] == ["stale"]


def test_cli_daily_report_writes_file_and_prints_payload(tmp_path, capsys) -> None:
    state_db = tmp_path / "state" / "turtle.sqlite3"
    report_path = tmp_path / "reports" / "daily.json"
    with SQLiteStateStore(state_db) as store:
        store.record_runtime_event("INFO", "paper_order_intent", {"symbol": "AAA"})

    result = run(
        [
            "--state-db",
            str(state_db),
            "--daily-report",
            str(report_path),
            "--report-date",
            datetime.now(timezone.utc).date().isoformat(),
            "--report-timezone",
            "UTC",
        ]
    )

    printed = json.loads(capsys.readouterr().out)
    saved = json.loads(report_path.read_text(encoding="utf-8"))
    assert result == 0
    assert printed == saved
    assert saved["runtime_event_summary"]["paper_order_intents"] == 1
