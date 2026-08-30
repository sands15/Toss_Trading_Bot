from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from turtle_bot.intraday_paper import (
    IntradayPaperConfig,
    IntradayPaperStore,
    PaperSimulationBlocked,
    PaperSimulationError,
    simulation_account_key,
)


UTC = timezone.utc
SESSION = date(2026, 8, 31)
OPEN = datetime(2026, 8, 31, 13, 30, tzinfo=UTC)


def _config(**changes: object) -> IntradayPaperConfig:
    values: dict[str, object] = {
        "run_id": "forward-20260831",
        "start_date": SESSION,
        "end_date": date(2026, 9, 30),
        "initial_cash_usd": Decimal("10000"),
        "slippage_fraction": Decimal("0.0005"),
        "quote_max_age_seconds": 5,
    }
    values.update(changes)
    return IntradayPaperConfig(**values)  # type: ignore[arg-type]


def _plan(config: IntradayPaperConfig, *, session: date = SESSION) -> dict[str, object]:
    account = simulation_account_key(config)
    plan_id = f"intraday-{session:%Y%m%d}"
    base_open = OPEN + timedelta(days=(session - SESSION).days)
    payload: dict[str, object] = {
        "plan_id": plan_id,
        "account_id": account,
        "session_date": session.isoformat(),
        "mode": "shadow",
        "status": "SHADOW_PLANNED",
        "live_order_submission": False,
        "symbol": "AAPL",
        "quantity": 10,
        "available_cash": "10000",
        "entry_start": (base_open + timedelta(minutes=1)).isoformat(),
        "entry_expiry": (base_open + timedelta(minutes=30)).isoformat(),
        "force_exit_at": (base_open + timedelta(hours=6, minutes=15)).isoformat(),
        "regular_close": (base_open + timedelta(hours=6, minutes=30)).isoformat(),
        "entry_trigger": "100",
        "entry_limit": "101",
        "target_trigger": "102",
        "target_limit": "102",
        "stop_trigger": "98",
        "stop_limit": "97.5",
        "estimated_round_trip_cost_fraction": "0.002",
        "estimated_fixed_round_trip_cost": "1",
        "commission_snapshot": {
            "market_country": "US",
            "broker_commission_fraction": "0.0005",
        },
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return {
        "plan_id": plan_id,
        "account_key": account,
        "session_date": session,
        "symbol": "AAPL",
        "mode": "shadow",
        "plan_hash": hashlib.sha256(encoded.encode()).hexdigest(),
        "payload": payload,
        "created_at": base_open - timedelta(hours=1),
    }


def _stream(
    at: datetime,
    *,
    trade: str = "100",
    bid: str = "99.99",
    ask: str = "100",
    bid_volume: str = "100",
    ask_volume: str = "100",
    shadow_usable: bool = True,
    include_trade: bool = True,
    include_book: bool = True,
    trade_at: datetime | None = None,
    book_at: datetime | None = None,
) -> dict[str, object]:
    trade_event_at = trade_at or at
    book_event_at = book_at or at
    payload: dict[str, object] = {
        "schema_version": 1,
        "mode": "shadow",
        "live_order_submission": False,
        "ready_for_live_entry": False,
        "symbol": "AAPL",
        "session_date": SESSION.isoformat(),
        "generation": 1,
        "shadow_usable": shadow_usable,
        "valid_until": (at + timedelta(seconds=5)).isoformat(),
        "error_codes": [],
    }
    if include_trade:
        payload["trade"] = {
            "price": trade,
            "volume": "2",
            "currency": "USD",
            "broker_at": trade_event_at.isoformat(),
            "received_at": trade_event_at.isoformat(),
            "source": "websocket",
        }
    if include_book:
        payload["orderbook"] = {
            "best_bid": bid,
            "best_bid_volume": bid_volume,
            "best_ask": ask,
            "best_ask_volume": ask_volume,
            "currency": "USD",
            "broker_at": book_event_at.isoformat(),
            "received_at": book_event_at.isoformat(),
            "timestamp_source": "broker",
            "source": "websocket",
        }
    return payload


def _registered(tmp_path: Path) -> tuple[IntradayPaperConfig, IntradayPaperStore, str]:
    config = _config()
    store = IntradayPaperStore(tmp_path / "paper.sqlite3", config)
    record = _plan(config)
    store.ensure_plan(record, registered_at=OPEN)
    return config, store, str(record["plan_id"])


def _enter(store: IntradayPaperStore, plan_id: str) -> None:
    trigger_at = OPEN + timedelta(minutes=2)
    armed = store.process_payload(
        plan_id,
        _stream(trigger_at),
        event_kind="trade",
        now=trigger_at,
    )
    assert armed["action"] == "ENTRY_ARMED"
    filled_at = trigger_at + timedelta(seconds=1)
    filled = store.process_payload(
        plan_id,
        _stream(filled_at),
        event_kind="orderbook",
        now=filled_at,
    )
    assert filled["action"] == "ENTRY_FILLED"


def _cover_through(
    store: IntradayPaperStore,
    plan_id: str,
    boundary: datetime,
    *,
    end_offset_seconds: int = 0,
) -> None:
    instance_id = f"coverage-{plan_id}"
    store.begin_stream_instance(plan_id, instance_id, started_at=OPEN)
    ended_at = boundary + timedelta(seconds=end_offset_seconds)
    store.touch_stream_instance(instance_id, observed_at=ended_at)
    store.end_stream_instance(
        instance_id,
        ended_at=ended_at,
        reason="context_inactive",
    )


def test_store_is_wal_durable_and_run_config_is_immutable(tmp_path: Path) -> None:
    config = _config()
    path = tmp_path / "paper.sqlite3"
    with IntradayPaperStore(path, config) as store:
        assert store.current_cash() == Decimal("10000")
        assert store.account_key == simulation_account_key(config)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("SELECT COUNT(*) FROM paper_cash_ledger").fetchone()[0] == 1

    changed = _config(initial_cash_usd=Decimal("9999"))
    with pytest.raises(PaperSimulationError, match="different immutable config"):
        IntradayPaperStore(path, changed)


def test_warmup_journals_each_frame_without_false_gap(tmp_path: Path) -> None:
    _, store, plan_id = _registered(tmp_path)
    at = OPEN + timedelta(minutes=1)
    try:
        trade_only = _stream(at, shadow_usable=False, include_book=False)
        result = store.process_payload(
            plan_id, trade_only, event_kind="trade", now=at
        )

        assert result["action"] == "WARMING_UP"
        assert result["plan"]["status"] == "WAITING_ENTRY"
        assert result["plan"]["data_gap_count"] == 0
        assert result["plan"]["journaled_frame_count"] == 1
    finally:
        store.close()


def test_bounded_queue_flushes_frames_with_one_explicit_batch(tmp_path: Path) -> None:
    _, store, plan_id = _registered(tmp_path)
    at = OPEN + timedelta(minutes=1)
    try:
        assert store.queue_payload(
            plan_id,
            _stream(at, shadow_usable=False, include_book=False),
            event_kind="trade",
            now=at,
        ) == []
        assert store.pending_event_count == 1
        assert store.load_plan(plan_id)["journaled_frame_count"] == 0

        flushed = store.flush_pending()

        assert [item["action"] for item in flushed] == ["WARMING_UP"]
        assert store.pending_event_count == 0
        assert store.load_plan(plan_id)["journaled_frame_count"] == 1
        assert store.summary(as_of=at)["journal_policy"] == {
            "sqlite_synchronous": "FULL",
            "wal": True,
            "max_unflushed_tail_events": 127,
            "gap_free_claim": False,
        }
    finally:
        store.close()


def test_batch_preserves_frame_order_for_economic_transitions(tmp_path: Path) -> None:
    _, store, plan_id = _registered(tmp_path)
    trigger_at = OPEN + timedelta(minutes=2)
    try:
        store.queue_payload(
            plan_id,
            _stream(trigger_at),
            event_kind="trade",
            now=trigger_at,
        )
        book_at = trigger_at + timedelta(seconds=1)
        store.queue_payload(
            plan_id,
            _stream(book_at),
            event_kind="orderbook",
            now=book_at,
        )

        results = store.flush_pending()

        assert [item["action"] for item in results] == ["ENTRY_ARMED", "ENTRY_FILLED"]
        assert store.current_cash() == Decimal("8998.49975")
    finally:
        store.close()


def test_trigger_uses_only_a_subsequent_orderbook_and_replay_is_idempotent(
    tmp_path: Path,
) -> None:
    _, store, plan_id = _registered(tmp_path)
    at = OPEN + timedelta(minutes=2)
    try:
        first = store.process_payload(
            plan_id, _stream(at), event_kind="trade", now=at
        )
        replay = store.process_payload(
            plan_id, _stream(at), event_kind="trade", now=at
        )
        same_book_trade = _stream(at, trade="100.01")
        waiting = store.process_payload(
            plan_id, same_book_trade, event_kind="trade", now=at
        )

        assert first["action"] == "ENTRY_ARMED"
        assert replay["duplicate"] is True
        assert replay["action"] == "ENTRY_ARMED"
        assert waiting["action"] == "ENTRY_WAIT_NEW_BOOK"
        assert store.load_plan(plan_id)["status"] == "WAITING_ENTRY"

        short_at = at + timedelta(seconds=1)
        short = store.process_payload(
            plan_id,
            _stream(short_at, ask_volume="9"),
            event_kind="orderbook",
            now=short_at,
        )
        fill_at = short_at + timedelta(seconds=1)
        fill = store.process_payload(
            plan_id,
            _stream(fill_at),
            event_kind="orderbook",
            now=fill_at,
        )

        assert short["action"] == "ENTRY_WAIT_DEPTH"
        assert fill["action"] == "ENTRY_FILLED"
        assert fill["plan"]["entry_price"] == "100.05"
        assert fill["plan"]["status"] == "OPEN"
        assert store.current_cash() == Decimal("8998.49975")
    finally:
        store.close()


def test_identical_book_then_trade_are_distinct_but_trade_replay_is_idempotent(
    tmp_path: Path,
) -> None:
    _, store, plan_id = _registered(tmp_path)
    at = OPEN + timedelta(minutes=2)
    payload = _stream(at)
    try:
        book = store.process_payload(
            plan_id, payload, event_kind="orderbook", now=at
        )
        trade = store.process_payload(plan_id, payload, event_kind="trade", now=at)
        replay = store.process_payload(plan_id, payload, event_kind="trade", now=at)

        assert book["action"] == "WAIT_ENTRY_TRIGGER"
        assert book["duplicate"] is False
        assert trade["action"] == "ENTRY_ARMED"
        assert trade["duplicate"] is False
        assert replay["action"] == "ENTRY_ARMED"
        assert replay["duplicate"] is True
    finally:
        store.close()


def test_entry_arming_requires_a_current_trade_frame_inside_the_entry_window(
    tmp_path: Path,
) -> None:
    _, store, plan_id = _registered(tmp_path)
    start = OPEN + timedelta(minutes=1)
    stale_trade_at = start - timedelta(seconds=1)
    try:
        book_at = start + timedelta(seconds=1)
        carried = store.process_payload(
            plan_id,
            _stream(book_at, trade_at=stale_trade_at, book_at=book_at),
            event_kind="orderbook",
            now=book_at,
        )
        delayed_at = book_at + timedelta(seconds=1)
        delayed = store.process_payload(
            plan_id,
            _stream(delayed_at, trade_at=stale_trade_at, book_at=delayed_at),
            event_kind="trade",
            now=delayed_at,
        )

        assert carried["action"] == "WAIT_ENTRY_TRIGGER"
        assert delayed["action"] == "BEFORE_ENTRY_WINDOW"
        assert store.load_plan(plan_id)["entry_armed_at"] is None

        fresh_at = delayed_at + timedelta(seconds=1)
        armed = store.process_payload(
            plan_id,
            _stream(fresh_at),
            event_kind="trade",
            now=fresh_at,
        )
        assert armed["action"] == "ENTRY_ARMED"
        assert armed["plan"]["entry_armed_at"] == fresh_at.isoformat()
    finally:
        store.close()


def test_exit_arming_requires_a_current_post_entry_trade_frame(tmp_path: Path) -> None:
    _, store, plan_id = _registered(tmp_path)
    try:
        _enter(store, plan_id)
        entry_at = datetime.fromisoformat(store.load_plan(plan_id)["entry_at"])
        stale_trade_at = entry_at - timedelta(milliseconds=500)
        book_at = entry_at + timedelta(seconds=1)
        carried = store.process_payload(
            plan_id,
            _stream(
                book_at,
                trade="102",
                bid="102",
                ask="102.01",
                trade_at=stale_trade_at,
                book_at=book_at,
            ),
            event_kind="orderbook",
            now=book_at,
        )
        delayed_at = book_at + timedelta(seconds=1)
        delayed = store.process_payload(
            plan_id,
            _stream(
                delayed_at,
                trade="102",
                bid="102",
                ask="102.01",
                trade_at=stale_trade_at,
                book_at=delayed_at,
            ),
            event_kind="trade",
            now=delayed_at,
        )

        assert carried["action"] == "WAIT_EXIT_TRIGGER"
        assert delayed["action"] == "WAIT_EXIT_TRIGGER"
        assert store.load_plan(plan_id)["exit_armed_reason"] is None

        fresh_at = delayed_at + timedelta(seconds=1)
        armed = store.process_payload(
            plan_id,
            _stream(fresh_at, trade="102", bid="102", ask="102.01"),
            event_kind="trade",
            now=fresh_at,
        )
        assert armed["action"] == "TARGET_ARMED"
        assert armed["plan"]["exit_armed_reason"] == "TARGET"
    finally:
        store.close()


def test_target_exit_costs_reports_and_alert_outbox(tmp_path: Path) -> None:
    _, store, plan_id = _registered(tmp_path)
    try:
        _enter(store, plan_id)
        target_at = OPEN + timedelta(minutes=3)
        armed = store.process_payload(
            plan_id,
            _stream(target_at, trade="102", bid="102.10", ask="102.11"),
            event_kind="trade",
            now=target_at,
        )
        exit_at = target_at + timedelta(seconds=1)
        closed = store.process_payload(
            plan_id,
            _stream(exit_at, trade="102", bid="102.10", ask="102.11"),
            event_kind="orderbook",
            now=exit_at,
            commission_fraction="0.0005",
        )

        assert armed["action"] == "TARGET_ARMED"
        assert closed["action"] == "TARGET_EXIT_FILLED"
        assert closed["plan"]["status"] == "CLOSED"
        assert closed["plan"]["exit_price"] == "102.04"
        assert closed["plan"]["realized_pnl"] == "17.88955"
        assert store.current_cash() == Decimal("10017.88955")

        day = store.daily_summary(SESSION)
        assert day["gross_pnl"] == "19.9"
        assert day["total_fees"] == "2.01045"
        assert day["net_pnl"] == "17.88955"
        current = SESSION + timedelta(days=1)
        while current <= date(2026, 9, 30):
            if current.weekday() < 5:
                store.record_market_closed(current, recorded_at=exit_at)
            current += timedelta(days=1)
        summary = store.summary(as_of=datetime(2026, 10, 1, tzinfo=UTC))
        assert summary["status"] == "COMPLETE"
        assert summary["coverage"]["missing"] == []
        assert summary["coverage"]["expected_count"] == 23
        assert summary["coverage"]["covered_count"] == 23
        assert summary["final_return_fraction"] == "0.001788955"
        assert summary["clean_trade_count"] == 1
        assert summary["win_rate"] == "1"
        assert summary["profit_factor"] is None
        assert summary["exit_reason_counts"] == {"TARGET": 1}

        alerts = store.list_alerts()
        assert [item["event"] for item in alerts] == [
            "plan_registered",
            "entry_filled",
            "exit_filled",
        ]
        assert alerts[-1]["payload"]["session_date"] == SESSION.isoformat()
        assert alerts[-1]["payload"]["cash_after"] == "10017.88955"
        assert store.mark_alert_forwarded(alerts[0]["alert_id"], forwarded_at=exit_at)
        assert not store.mark_alert_forwarded(alerts[0]["alert_id"], forwarded_at=exit_at)
    finally:
        store.close()


def test_stop_overrides_armed_target_and_waits_for_new_book(tmp_path: Path) -> None:
    _, store, plan_id = _registered(tmp_path)
    try:
        _enter(store, plan_id)
        target_at = OPEN + timedelta(minutes=3)
        store.process_payload(
            plan_id,
            _stream(target_at, trade="102", bid="101.90", ask="101.91"),
            event_kind="trade",
            now=target_at,
        )
        stop_at = target_at + timedelta(seconds=1)
        overridden = store.process_payload(
            plan_id,
            _stream(stop_at, trade="97.9", bid="97.8", ask="97.81"),
            event_kind="trade",
            now=stop_at,
        )
        exit_at = stop_at + timedelta(seconds=1)
        stopped = store.process_payload(
            plan_id,
            _stream(exit_at, trade="97.9", bid="97.8", ask="97.81"),
            event_kind="orderbook",
            now=exit_at,
        )

        assert overridden["action"] == "STOP_ARMED"
        assert stopped["action"] == "STOP_EXIT_FILLED"
        assert stopped["plan"]["exit_reason"] == "STOP"
    finally:
        store.close()


def test_open_position_data_gap_forces_invalid_exit_and_excludes_metrics(
    tmp_path: Path,
) -> None:
    _, store, plan_id = _registered(tmp_path)
    try:
        _enter(store, plan_id)
        gap_at = OPEN + timedelta(minutes=3)
        gap = store.record_data_gap(plan_id, "ws_connection_lost", at=gap_at)
        assert gap["plan"]["status"] == "OPEN"
        assert gap["plan"]["data_quality_invalid"] is True

        fresh_at = gap_at + timedelta(seconds=1)
        invalid = store.process_payload(
            plan_id,
            _stream(fresh_at, trade="99", bid="99", ask="99.01"),
            event_kind="orderbook",
            now=fresh_at,
        )
        summary = store.summary(as_of=fresh_at)

        assert invalid["action"] == "DATA_GAP_EXIT_FILLED"
        assert invalid["plan"]["status"] == "INVALID"
        assert invalid["plan"]["exit_reason"] == "DATA_GAP"
        assert summary["clean_trade_count"] == 0
        assert summary["invalid_result_count"] == 1
        assert summary["data_gap_count"] == 1
        assert summary["status"] == "INVALID"
    finally:
        store.close()


def test_fresh_quote_can_finalize_force_exit(tmp_path: Path) -> None:
    _, store, plan_id = _registered(tmp_path)
    try:
        _enter(store, plan_id)
        force_at = OPEN + timedelta(hours=6, minutes=15)
        waiting = store.process_payload(
            plan_id,
            _stream(force_at, trade="101", bid="101", ask="101.01"),
            event_kind="trade",
            now=force_at,
        )
        assert waiting["action"] == "FORCE_EXIT_WAIT_NEW_BOOK"
        _cover_through(store, plan_id, force_at)

        finalized = store.finalize_session(plan_id, now=force_at)

        assert finalized["status"] == "CLOSED"
        assert finalized["exit_reason"] == "FORCE"
        assert finalized["exit_price"] == "100.94"
    finally:
        store.close()


def test_never_started_stream_cannot_finalize_as_clean_no_entry(
    tmp_path: Path,
) -> None:
    _, store, plan_id = _registered(tmp_path)
    try:
        expiry = OPEN + timedelta(minutes=30)
        observed_at = expiry - timedelta(seconds=1)
        waiting = store.process_payload(
            plan_id,
            _stream(observed_at, trade="99"),
            event_kind="trade",
            now=observed_at,
        )
        assert waiting["action"] == "WAIT_ENTRY_TRIGGER"

        finalized = store.finalize_session(
            plan_id,
            now=expiry + timedelta(seconds=5),
        )

        assert finalized["status"] == "INVALID"
        assert finalized["exit_reason"] == "stream_coverage_incomplete"
        assert finalized["data_gap_count"] == 1
    finally:
        store.close()


def test_stream_ending_before_force_boundary_invalidates_instead_of_clean_exit(
    tmp_path: Path,
) -> None:
    _, store, plan_id = _registered(tmp_path)
    try:
        instance_id = "coverage-ended-early"
        store.begin_stream_instance(plan_id, instance_id, started_at=OPEN)
        _enter(store, plan_id)
        force_at = OPEN + timedelta(hours=6, minutes=15)
        store.end_stream_instance(
            instance_id,
            ended_at=force_at - timedelta(seconds=1),
            reason="stream_process_closed",
        )
        store.process_payload(
            plan_id,
            _stream(force_at, trade="101", bid="101", ask="101.01"),
            event_kind="trade",
            now=force_at,
        )

        finalized = store.finalize_session(plan_id, now=force_at)

        assert finalized["status"] == "INVALID"
        assert finalized["exit_reason"] == "DATA_GAP"
        assert finalized["data_quality_invalid"] is True
        assert finalized["data_gap_count"] == 1
        gap_alert = next(
            alert
            for alert in store.list_alerts()
            if alert["event"] == "market_data_gap"
        )
        assert gap_alert["payload"]["reason"] == "stream_coverage_incomplete"
    finally:
        store.close()


def test_clean_stream_close_at_force_boundary_keeps_result_valid(
    tmp_path: Path,
) -> None:
    _, store, plan_id = _registered(tmp_path)
    try:
        _enter(store, plan_id)
        force_at = OPEN + timedelta(hours=6, minutes=15)
        store.process_payload(
            plan_id,
            _stream(force_at, trade="101", bid="101", ask="101.01"),
            event_kind="trade",
            now=force_at,
        )
        _cover_through(store, plan_id, force_at)

        finalized = store.finalize_session(plan_id, now=force_at)

        assert finalized["status"] == "CLOSED"
        assert finalized["exit_reason"] == "FORCE"
        assert finalized["data_gap_count"] == 0
    finally:
        store.close()


def test_finalize_rejects_book_older_than_latest_data_gap(tmp_path: Path) -> None:
    _, store, plan_id = _registered(tmp_path)
    try:
        _enter(store, plan_id)
        force_at = OPEN + timedelta(hours=6, minutes=15)
        stale_at = force_at - timedelta(seconds=1)
        store.process_payload(
            plan_id,
            _stream(stale_at, trade="101", bid="101", ask="101.01"),
            event_kind="orderbook",
            now=stale_at,
        )
        store.record_data_gap(plan_id, "ws_connection_lost", at=force_at)
        _cover_through(store, plan_id, force_at)

        still_open = store.finalize_session(
            plan_id, now=force_at + timedelta(seconds=1)
        )
        assert still_open["status"] == "OPEN"

        fresh_at = force_at + timedelta(seconds=2)
        waiting = store.process_payload(
            plan_id,
            _stream(fresh_at, trade="101", bid="101", ask="101.01"),
            event_kind="trade",
            now=fresh_at,
        )
        assert waiting["action"] == "DATA_GAP_EXIT_WAIT_NEW_BOOK"
        finalized = store.finalize_session(plan_id, now=fresh_at)
        assert finalized["status"] == "INVALID"
        assert finalized["exit_reason"] == "DATA_GAP"
    finally:
        store.close()


def test_unresolved_force_exit_blocks_later_plans_and_end_is_inclusive(
    tmp_path: Path,
) -> None:
    config, store, plan_id = _registered(tmp_path)
    try:
        _enter(store, plan_id)
        regular_close = OPEN + timedelta(hours=6, minutes=30)
        _cover_through(
            store,
            plan_id,
            OPEN + timedelta(hours=6, minutes=15),
        )
        unresolved = store.finalize_session(plan_id, now=regular_close)

        assert unresolved["status"] == "UNRESOLVED"
        assert store.month_summary(as_of=regular_close)["status"] == "UNRESOLVED"
        with pytest.raises(PaperSimulationBlocked, match="unresolved_simulated_position"):
            store.assert_ready(date(2026, 9, 1))
        with pytest.raises(PaperSimulationBlocked, match="outside the inclusive"):
            # A separate store is unnecessary: range validation happens before blocker state.
            store.assert_ready(date(2026, 10, 1))
        assert config.end_date == date(2026, 9, 30)
    finally:
        store.close()


def test_month_coverage_never_completes_without_every_expected_weekday(
    tmp_path: Path,
) -> None:
    config = _config(start_date=SESSION, end_date=date(2026, 9, 1))
    with IntradayPaperStore(tmp_path / "paper.sqlite3", config) as store:
        empty = store.month_summary(as_of=datetime(2026, 9, 2, tzinfo=UTC))
        assert empty["status"] == "INCOMPLETE"
        assert empty["coverage"] == {
            "expected": ["2026-08-31", "2026-09-01"],
            "covered": [],
            "missing": ["2026-08-31", "2026-09-01"],
            "planned": [],
            "market_closed": [],
            "expected_count": 2,
            "covered_count": 0,
            "missing_count": 2,
        }

        first = store.record_market_closed(SESSION, recorded_at=OPEN)
        repeated = store.record_market_closed(
            SESSION, recorded_at=OPEN + timedelta(hours=1)
        )
        assert repeated == first
        assert store.daily_summary(SESSION)["status"] == "MARKET_CLOSED"
        store.record_market_closed(date(2026, 9, 1), recorded_at=OPEN)
        covered_but_empty = store.month_summary(
            as_of=datetime(2026, 9, 2, tzinfo=UTC)
        )
        assert covered_but_empty["status"] == "INCOMPLETE"
        assert covered_but_empty["coverage"]["missing"] == []

        record = _plan(config, session=date(2026, 9, 1))
        with pytest.raises(PaperSimulationBlocked, match="MARKET_CLOSED"):
            store.ensure_plan(record, registered_at=OPEN + timedelta(days=1))

    config = _config(start_date=SESSION, end_date=date(2026, 9, 1))
    with IntradayPaperStore(tmp_path / "planned.sqlite3", config) as store:
        store.record_market_closed(SESSION, recorded_at=OPEN)
        record = _plan(config, session=date(2026, 9, 1))
        store.ensure_plan(record, registered_at=OPEN + timedelta(days=1))
        waiting = store.month_summary(as_of=datetime(2026, 9, 2, tzinfo=UTC))
        assert waiting["status"] == "WAITING"
        assert waiting["coverage"]["missing"] == []
        assert waiting["coverage"]["market_closed"] == ["2026-08-31"]

        with pytest.raises(PaperSimulationError, match="already has"):
            store.record_market_closed(date(2026, 9, 1), recorded_at=OPEN)


def test_open_plan_is_reported_as_open_not_complete(tmp_path: Path) -> None:
    config = _config(end_date=SESSION)
    store = IntradayPaperStore(tmp_path / "paper.sqlite3", config)
    record = _plan(config)
    plan_id = str(record["plan_id"])
    try:
        store.ensure_plan(record, registered_at=OPEN)
        _enter(store, plan_id)

        summary = store.month_summary(as_of=datetime(2026, 9, 1, tzinfo=UTC))

        assert summary["status"] == "OPEN"
        assert summary["coverage"]["missing"] == []
    finally:
        store.close()


def test_plan_hash_account_and_stream_identity_fail_closed(tmp_path: Path) -> None:
    config = _config()
    with IntradayPaperStore(tmp_path / "paper.sqlite3", config) as store:
        record = _plan(config)
        record["plan_hash"] = "0" * 64
        with pytest.raises(PaperSimulationError, match="integrity"):
            store.ensure_plan(record)

        valid = _plan(config)
        store.ensure_plan(valid, registered_at=OPEN)
        wrong = _stream(OPEN + timedelta(minutes=2))
        wrong["symbol"] = "MSFT"
        with pytest.raises(PaperSimulationError, match="symbol"):
            store.process_payload(
                str(valid["plan_id"]),
                wrong,
                event_kind="trade",
                now=OPEN + timedelta(minutes=2),
            )
