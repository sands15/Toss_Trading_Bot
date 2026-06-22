from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from turtle_bot.domain import Candle
from turtle_bot import PositionDirection, PositionState, PositionStatus, TurtleSystem, UnitState
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
                reason="20일 돌파선 근접",
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
                reason="55일 돌파선 근접",
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


def test_latest_candles_snapshot_roundtrip() -> None:
    captured_at = datetime(2026, 1, 2, tzinfo=timezone.utc)
    candle = Candle(
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        symbol="AAPL",
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("90"),
        close=Decimal("105"),
        volume=Decimal("1000"),
        currency="USD",
    )
    with SQLiteStateStore() as store:
        store.record_market_data_snapshot(
            "candles",
            "AAPL",
            {
                "interval": "1d",
                "count": 1,
                "candles": [
                    {
                        "timestamp": candle.timestamp.isoformat(),
                        "symbol": candle.symbol,
                        "openPrice": str(candle.open),
                        "highPrice": str(candle.high),
                        "lowPrice": str(candle.low),
                        "closePrice": str(candle.close),
                        "volume": str(candle.volume),
                        "currency": candle.currency,
                        "source": candle.source,
                    }
                ],
            },
            captured_at=captured_at,
        )
        loaded = store.latest_candles_snapshot("AAPL", interval="1d")

    assert loaded is not None
    candles, loaded_at = loaded
    assert loaded_at == captured_at
    assert candles == (candle,)


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
        direction=PositionDirection.SHORT,
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


def test_existing_position_schema_migrates_direction_column(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite"
    with SQLiteStateStore(path) as store:
        with store._conn:
            store._conn.execute("ALTER TABLE positions RENAME TO old_positions")
            store._conn.execute(
                """
                CREATE TABLE positions (
                  symbol TEXT PRIMARY KEY,
                  system TEXT NOT NULL,
                  status TEXT NOT NULL,
                  total_qty TEXT NOT NULL,
                  avg_entry_price TEXT NOT NULL,
                  entry_n TEXT NOT NULL,
                  current_stop_price TEXT NOT NULL,
                  last_unit_entry_price TEXT NOT NULL
                )
                """
            )
            store._conn.execute(
                """
                INSERT INTO positions (
                    symbol,
                    system,
                    status,
                    total_qty,
                    avg_entry_price,
                    entry_n,
                    current_stop_price,
                    last_unit_entry_price
                )
                VALUES ('LEGACY', 'S1', 'OPEN', '1', '100', '2', '96', '100')
                """
            )
            store._conn.execute("DROP TABLE old_positions")

    with SQLiteStateStore(path) as reopened:
        loaded = reopened.load_position("LEGACY")

    assert loaded is not None
    assert loaded.direction == PositionDirection.LONG


def test_list_positions_filters_by_status() -> None:
    open_position = PositionState(
        symbol="AAA",
        system=TurtleSystem.S1,
        status=PositionStatus.OPEN,
        total_qty=Decimal("1"),
        avg_entry_price=Decimal("100"),
        entry_n=Decimal("2"),
        current_stop_price=Decimal("96"),
        last_unit_entry_price=Decimal("100"),
        units=(
            UnitState(
                unit_no=1,
                qty=Decimal("1"),
                entry_price=Decimal("100"),
                n_at_entry=Decimal("2"),
                stop_price=Decimal("96"),
            ),
        ),
    )
    closed_position = replace(
        open_position,
        symbol="BBB",
        status=PositionStatus.CLOSED,
    )

    with SQLiteStateStore() as store:
        store.save_position(open_position)
        store.save_position(closed_position)

        assert [position.symbol for position in store.list_positions()] == ["AAA", "BBB"]
        assert [
            position.symbol for position in store.list_positions(status=PositionStatus.OPEN)
        ] == ["AAA"]


def test_paper_positions_are_separate_from_live_positions() -> None:
    live_position = PositionState(
        symbol="AAA",
        system=TurtleSystem.S1,
        status=PositionStatus.OPEN,
        total_qty=Decimal("1"),
        avg_entry_price=Decimal("100"),
        entry_n=Decimal("2"),
        current_stop_price=Decimal("96"),
        last_unit_entry_price=Decimal("100"),
        units=(
            UnitState(
                unit_no=1,
                qty=Decimal("1"),
                entry_price=Decimal("100"),
                n_at_entry=Decimal("2"),
                stop_price=Decimal("96"),
            ),
        ),
    )
    paper_position = replace(live_position, total_qty=Decimal("2"))

    with SQLiteStateStore() as store:
        store.save_position(live_position)
        store.save_paper_position(paper_position)

        assert store.load_position("AAA").total_qty == Decimal("1")
        assert store.load_paper_position("AAA").total_qty == Decimal("2")
        assert [position.total_qty for position in store.list_positions()] == [Decimal("1")]
        assert [position.total_qty for position in store.list_paper_positions()] == [Decimal("2")]


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


def test_list_unresolved_execution_orders_excludes_final_statuses() -> None:
    with SQLiteStateStore() as store:
        store.record_execution_order(
            intent_id="intent-ack",
            idempotency_key="idem-ack",
            symbol="AAA",
            side="BUY",
            status="ACKNOWLEDGED",
            broker_order_id="broker-ack",
            raw={"status": "ack"},
        )
        store.record_execution_order(
            intent_id="intent-cancel",
            idempotency_key="idem-cancel",
            symbol="AAA",
            side="BUY",
            status="PENDING_CANCEL",
            broker_order_id="broker-cancel",
            raw={"status": "pending_cancel"},
        )
        store.record_execution_order(
            intent_id="intent-filled",
            idempotency_key="idem-filled",
            symbol="AAA",
            side="BUY",
            status="FILLED",
            broker_order_id="broker-filled",
            raw={"status": "filled"},
        )

        unresolved = store.list_unresolved_execution_orders()

    assert [order["intent_id"] for order in unresolved] == [
        "intent-ack",
        "intent-cancel",
    ]
    assert unresolved[0]["raw"] == {"status": "ack"}


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


def test_broker_snapshot_roundtrip() -> None:
    with SQLiteStateStore() as store:
        store.record_broker_snapshot("holdings", {"items": [{"symbol": "005930"}]})
        snapshot = store.latest_broker_snapshot("holdings")
        record = store.latest_broker_snapshot_record("holdings")

    assert snapshot == {"items": [{"symbol": "005930"}]}
    assert record is not None
    assert record["kind"] == "holdings"
    assert record["payload"] == {"items": [{"symbol": "005930"}]}
    assert isinstance(record["captured_at"], datetime)


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
