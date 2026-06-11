from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from turtle_bot import PositionState, PositionStatus, TurtleSystem, UnitState
from turtle_bot.state_store import SQLiteStateStore
from turtle_bot.watchlist import Watchlist, WatchlistRow


def _watchlist(now_iso: str) -> Watchlist:
    return Watchlist(
        generated_at=datetime.fromisoformat(now_iso),
        rows=(
            WatchlistRow(
                symbol="AAA",
                current_price=Decimal("110.5"),
                entry_high_20=Decimal("120"),
                entry_high_55=Decimal("121"),
                distance_to_20=Decimal("9.5"),
                distance_to_55=Decimal("10.5"),
                nearest_distance=Decimal("9.5"),
                is_new=True,
            ),
            WatchlistRow(
                symbol="BBB",
                current_price=Decimal("211.75"),
                entry_high_20=Decimal("205"),
                entry_high_55=Decimal("206"),
                distance_to_20=Decimal("6.75"),
                distance_to_55=Decimal("5.75"),
                nearest_distance=Decimal("5.75"),
                is_new=False,
            ),
        ),
    )


def test_initialize_schema_is_idempotent() -> None:
    store = SQLiteStateStore()
    store.initialize_schema()

    row = store._conn.execute(
        "SELECT COUNT(*) AS count FROM schema_migrations WHERE version = 1"
    ).fetchone()
    assert row["count"] == 1


def test_watchlist_roundtrip_preserves_decimal_and_is_new() -> None:
    watchlist = _watchlist("2026-01-01T09:00:00+00:00")
    with SQLiteStateStore() as store:
        store.save_watchlist(watchlist, name="premarket")
        loaded = store.load_latest_watchlist(name="premarket")

    assert loaded == watchlist


def test_latest_watchlist_returns_newest() -> None:
    old_watchlist = _watchlist("2026-01-01T09:00:00+00:00")
    new_watchlist = _watchlist("2026-01-02T09:00:00+00:00")

    with SQLiteStateStore() as store:
        store.save_watchlist(old_watchlist, name="premarket")
        store.save_watchlist(new_watchlist, name="premarket")
        loaded = store.load_latest_watchlist(name="premarket")

    assert loaded == new_watchlist


def test_position_roundtrip_with_units() -> None:
    position = PositionState(
        symbol="SAMPLE",
        system=TurtleSystem.S1,
        status=PositionStatus.OPEN,
        total_qty=Decimal("2"),
        avg_entry_price=Decimal("100.5"),
        entry_n=Decimal("1.5"),
        current_stop_price=Decimal("98.0"),
        last_unit_entry_price=Decimal("100.25"),
        units=(
            UnitState(
                unit_no=1,
                qty=Decimal("1"),
                entry_price=Decimal("100.25"),
                n_at_entry=Decimal("1.5"),
                stop_price=Decimal("98.0"),
                broker_order_id="broker-1",
                client_order_id="client-1",
            ),
            UnitState(
                unit_no=2,
                qty=Decimal("1"),
                entry_price=Decimal("101.0"),
                n_at_entry=Decimal("1.5"),
                stop_price=Decimal("99.0"),
                broker_order_id=None,
                client_order_id=None,
            ),
        ),
    )

    with SQLiteStateStore() as store:
        store.save_position(position)
        loaded = store.load_position("SAMPLE")

    assert loaded == position


def test_unresolved_client_order_id_blocks_duplicates_until_resolved() -> None:
    with SQLiteStateStore() as store:
        store.record_broker_order(
            "client-1",
            symbol="AAA",
            side="BUY",
            status="OPEN",
        )
        assert store.has_unresolved_client_order_id("client-1") is True

        store.record_broker_order(
            "client-1",
            symbol="AAA",
            side="BUY",
            status="PARTIAL_FILLED",
        )
        assert store.has_unresolved_client_order_id("client-1") is True

        store.record_broker_order(
            "client-1",
            symbol="AAA",
            side="BUY",
            status="FILLED",
        )
        assert store.has_unresolved_client_order_id("client-1") is False


def test_market_data_snapshot_roundtrip() -> None:
    with SQLiteStateStore() as store:
        store.record_market_data_snapshot(
            "price",
            "AAPL",
            {
                "bid": Decimal("123.45"),
                "ask": Decimal("123.46"),
                "metadata": {"venue": "paper"},
            },
            captured_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        )
        snapshot = store.latest_market_data_snapshot("price", "AAPL")

    assert snapshot == {
        "bid": "123.45",
        "ask": "123.46",
        "metadata": {"venue": "paper"},
    }


def test_runtime_events_recorded_and_listed_newest_first() -> None:
    with SQLiteStateStore() as store:
        store.record_runtime_event("INFO", "first event", {"count": 1})
        store.record_runtime_event("WARN", "second event")
        events = store.list_runtime_events(limit=2)

    assert [event["message"] for event in events] == ["second event", "first event"]


def test_file_database_can_reopen_and_still_load_data(tmp_path) -> None:
    path = tmp_path / "state.sqlite"
    watchlist = _watchlist("2026-01-01T09:00:00+00:00")
    with SQLiteStateStore(path) as store:
        store.save_watchlist(watchlist, name="premarket")

    with SQLiteStateStore(path) as reopened:
        loaded = reopened.load_latest_watchlist(name="premarket")

    assert loaded == watchlist


def test_file_database_creates_parent_and_persists_snapshots_and_events(tmp_path) -> None:
    path = tmp_path / "nested" / "state.sqlite"
    with SQLiteStateStore(path) as store:
        store.record_market_data_snapshot(
            "price",
            "AAPL",
            {"last": Decimal("123.45")},
            captured_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        )
        store.record_runtime_event("INFO", "started", {"mode": "paper"})

    with SQLiteStateStore(path) as reopened:
        snapshot = reopened.latest_market_data_snapshot("price", "AAPL")
        events = reopened.list_runtime_events(limit=1)

    assert snapshot == {"last": "123.45"}
    assert events[0]["message"] == "started"
    assert events[0]["payload"] == {"mode": "paper"}
