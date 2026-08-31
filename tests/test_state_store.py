from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
from threading import Barrier

import pytest

import turtle_bot.state_store as state_store_module
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
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_migrations (
          version INTEGER PRIMARY KEY,
          applied_at TEXT NOT NULL
        );
        INSERT INTO schema_migrations VALUES (1, '2026-08-28T00:00:00+00:00');
        CREATE TABLE positions (
          symbol TEXT PRIMARY KEY,
          system TEXT NOT NULL,
          status TEXT NOT NULL,
          total_qty TEXT NOT NULL,
          avg_entry_price TEXT NOT NULL,
          entry_n TEXT NOT NULL,
          current_stop_price TEXT NOT NULL,
          last_unit_entry_price TEXT NOT NULL
        );
        INSERT INTO positions VALUES ('LEGACY', 'S1', 'OPEN', '1', '100', '2', '96', '100');
        """
    )
    connection.close()

    with SQLiteStateStore(path) as reopened:
        loaded = reopened.load_position("LEGACY")

    assert loaded is not None
    assert loaded.direction == PositionDirection.LONG


def test_legacy_appended_reason_column_is_an_explicit_supported_variant(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-appended-reason.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_migrations (
          version INTEGER PRIMARY KEY,
          applied_at TEXT NOT NULL
        );
        INSERT INTO schema_migrations VALUES (1, '2026-08-28T00:00:00+00:00');
        INSERT INTO schema_migrations VALUES (2, '2026-08-28T00:00:00+00:00');
        INSERT INTO schema_migrations VALUES (3, '2026-08-28T00:00:00+00:00');
        CREATE TABLE watchlists (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          generated_at TEXT NOT NULL
        );
        CREATE TABLE watchlist_items (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          watchlist_id INTEGER NOT NULL,
          rank INTEGER NOT NULL,
          symbol TEXT NOT NULL,
          current_price TEXT NOT NULL,
          entry_high_20 TEXT,
          entry_high_55 TEXT,
          distance_to_20 TEXT,
          distance_to_55 TEXT,
          nearest_distance TEXT NOT NULL,
          is_new INTEGER NOT NULL,
          FOREIGN KEY (watchlist_id) REFERENCES watchlists(id) ON DELETE CASCADE,
          UNIQUE (watchlist_id, symbol)
        );
        ALTER TABLE watchlist_items ADD COLUMN reason TEXT NOT NULL DEFAULT '';
        """
    )
    connection.close()

    with SQLiteStateStore(path) as migrated:
        versions = tuple(
            row[0]
            for row in migrated._conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        )

    assert versions == (1, 2, 3, 4, 5, 6)


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


@pytest.mark.parametrize("foreign_table", ["news_articles", "paper_runs", "unrelated"])
def test_planner_store_refuses_foreign_database_without_mutation(
    tmp_path: Path, foreign_table: str
) -> None:
    path = tmp_path / f"{foreign_table}.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(f"CREATE TABLE {foreign_table} (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    before = path.read_bytes()

    with pytest.raises(sqlite3.DatabaseError, match="planner_db_schema_invalid"):
        SQLiteStateStore(path)

    assert path.read_bytes() == before


def test_planner_store_refuses_database_hardlink_without_mutation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "foreign.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    alias = tmp_path / "planner.sqlite3"
    try:
        alias.hardlink_to(database)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")
    before = database.read_bytes()

    with pytest.raises(sqlite3.DatabaseError, match="planner_db_path_invalid"):
        SQLiteStateStore(alias)

    assert database.read_bytes() == before
    assert alias.read_bytes() == before


def test_planner_store_refuses_database_symlink_without_mutation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "foreign.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    alias = tmp_path / "planner.sqlite3"
    try:
        alias.symlink_to(database)
    except OSError as exc:
        pytest.skip(f"symbolic links unavailable: {exc}")
    before = database.read_bytes()

    with pytest.raises(sqlite3.DatabaseError, match="planner_db_path_invalid"):
        SQLiteStateStore(alias)

    assert database.read_bytes() == before


def test_planner_store_rechecks_identity_before_schema_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "planner.sqlite3"
    with SQLiteStateStore(path):
        pass
    before = path.read_bytes()
    real_identity = state_store_module._planner_database_identity
    calls = 0

    def changed_identity(candidate: Path) -> tuple[int, int] | None:
        nonlocal calls
        calls += 1
        identity = real_identity(candidate)
        if calls == 3 and identity is not None:
            return (identity[0], identity[1] + 1)
        return identity

    monkeypatch.setattr(
        state_store_module, "_planner_database_identity", changed_identity
    )
    with pytest.raises(sqlite3.DatabaseError, match="planner_db_path_invalid"):
        SQLiteStateStore(path)

    assert path.read_bytes() == before


def test_planner_store_rejects_legacy_table_with_changed_column_constraints(
    tmp_path: Path,
) -> None:
    path = tmp_path / "forged-legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_migrations (
          version INTEGER PRIMARY KEY,
          applied_at TEXT
        );
        INSERT INTO schema_migrations VALUES (1, '2026-08-28T00:00:00+00:00');
        """
    )
    connection.close()
    before = path.read_bytes()

    with pytest.raises(sqlite3.DatabaseError, match="planner_db_schema_invalid"):
        SQLiteStateStore(path)

    assert path.read_bytes() == before


def test_planner_store_rejects_legacy_table_with_missing_foreign_key(
    tmp_path: Path,
) -> None:
    path = tmp_path / "forged-foreign-key.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_migrations (
          version INTEGER PRIMARY KEY,
          applied_at TEXT NOT NULL
        );
        INSERT INTO schema_migrations VALUES (1, '2026-08-28T00:00:00+00:00');
        CREATE TABLE position_units (
          position_symbol TEXT NOT NULL,
          unit_no INTEGER NOT NULL,
          qty TEXT NOT NULL,
          entry_price TEXT NOT NULL,
          n_at_entry TEXT NOT NULL,
          stop_price TEXT NOT NULL,
          broker_order_id TEXT,
          client_order_id TEXT,
          PRIMARY KEY (position_symbol, unit_no)
        );
        """
    )
    connection.close()
    before = path.read_bytes()

    with pytest.raises(sqlite3.DatabaseError, match="planner_db_schema_invalid"):
        SQLiteStateStore(path)

    assert path.read_bytes() == before


def test_planner_store_rejects_nonunique_replacement_for_unique_index(
    tmp_path: Path,
) -> None:
    path = tmp_path / "forged-index.sqlite3"
    with SQLiteStateStore(path):
        pass
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        DROP INDEX ux_intraday_one_entry;
        CREATE INDEX ux_intraday_one_entry
        ON order_intents(plan_id) WHERE order_role = 'ENTRY';
        """
    )
    connection.close()
    before = path.read_bytes()

    with pytest.raises(sqlite3.DatabaseError, match="planner_db_schema_invalid"):
        SQLiteStateStore(path)

    assert path.read_bytes() == before


def test_planner_store_rejects_unique_index_with_changed_partial_predicate(
    tmp_path: Path,
) -> None:
    path = tmp_path / "forged-index-predicate.sqlite3"
    with SQLiteStateStore(path):
        pass
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        DROP INDEX ux_intraday_one_entry;
        CREATE UNIQUE INDEX ux_intraday_one_entry
        ON order_intents(plan_id) WHERE 0;
        """
    )
    connection.close()
    before = path.read_bytes()

    with pytest.raises(sqlite3.DatabaseError, match="planner_db_schema_invalid"):
        SQLiteStateStore(path)

    assert path.read_bytes() == before


def test_planner_store_rejects_current_table_with_changed_check_constraint(
    tmp_path: Path,
) -> None:
    path = tmp_path / "forged-check.sqlite3"
    with SQLiteStateStore(path):
        pass
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA writable_schema = ON")
    connection.execute(
        """
        UPDATE sqlite_master
        SET sql = replace(
          sql,
          'mode TEXT NOT NULL CHECK (mode = ''shadow'')',
          'mode TEXT NOT NULL CHECK (mode <> '''')'
        )
        WHERE type = 'table' AND name = 'intraday_plans'
        """
    )
    connection.execute("PRAGMA writable_schema = OFF")
    connection.commit()
    connection.close()
    before = path.read_bytes()

    with pytest.raises(sqlite3.DatabaseError, match="planner_db_schema_invalid"):
        SQLiteStateStore(path)

    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("table", "original", "replacement"),
    [
        (
            "intraday_plans",
            "plan_id TEXT PRIMARY KEY,",
            "plan_id TEXT PRIMARY KEY ON CONFLICT REPLACE,",
        ),
        (
            "runtime_events",
            "id INTEGER PRIMARY KEY AUTOINCREMENT,",
            "id INTEGER PRIMARY KEY,",
        ),
    ],
)
def test_planner_store_rejects_changed_current_table_sql_semantics(
    tmp_path: Path,
    table: str,
    original: str,
    replacement: str,
) -> None:
    path = tmp_path / f"forged-{table}.sqlite3"
    with SQLiteStateStore(path):
        pass
    connection = sqlite3.connect(path)
    schema_version = int(
        connection.execute("PRAGMA schema_version").fetchone()[0]
    )
    connection.execute("PRAGMA writable_schema = ON")
    updated = connection.execute(
        """
        UPDATE sqlite_master SET sql = replace(sql, ?, ?)
        WHERE type = 'table' AND name = ?
        """,
        (original, replacement, table),
    )
    assert updated.rowcount == 1
    connection.execute("PRAGMA writable_schema = OFF")
    connection.execute(f"PRAGMA schema_version = {schema_version + 1}")
    connection.commit()
    assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    connection.close()
    before = path.read_bytes()

    with pytest.raises(sqlite3.DatabaseError, match="planner_db_schema_invalid"):
        SQLiteStateStore(path)

    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("name", "schema"),
    [
        (
            "migration-conflict-policy",
            """
            CREATE TABLE schema_migrations (
              version INTEGER PRIMARY KEY ON CONFLICT REPLACE,
              applied_at TEXT NOT NULL
            );
            INSERT INTO schema_migrations VALUES (1, '2026-08-28T00:00:00+00:00');
            """,
        ),
        (
            "runtime-autoincrement",
            """
            CREATE TABLE schema_migrations (
              version INTEGER PRIMARY KEY,
              applied_at TEXT NOT NULL
            );
            INSERT INTO schema_migrations VALUES (1, '2026-08-28T00:00:00+00:00');
            CREATE TABLE runtime_events (
              id INTEGER PRIMARY KEY,
              level TEXT NOT NULL,
              message TEXT NOT NULL,
              payload TEXT,
              created_at TEXT NOT NULL
            );
            """,
        ),
        (
            "plan-conflict-policy",
            """
            CREATE TABLE schema_migrations (
              version INTEGER PRIMARY KEY,
              applied_at TEXT NOT NULL
            );
            INSERT INTO schema_migrations VALUES (1, '2026-08-28T00:00:00+00:00');
            INSERT INTO schema_migrations VALUES (2, '2026-08-28T00:00:00+00:00');
            CREATE TABLE intraday_plans (
              plan_id TEXT PRIMARY KEY ON CONFLICT REPLACE,
              account_key TEXT NOT NULL,
              session_date TEXT NOT NULL,
              symbol TEXT NOT NULL,
              mode TEXT NOT NULL CHECK (mode = 'shadow'),
              plan_hash TEXT NOT NULL CHECK (length(plan_hash) = 64),
              payload TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE (account_key, session_date)
            );
            """,
        ),
    ],
)
def test_planner_store_rejects_changed_legacy_table_sql_before_migration(
    tmp_path: Path,
    name: str,
    schema: str,
) -> None:
    path = tmp_path / f"forged-legacy-{name}.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(schema)
    connection.close()
    before = path.read_bytes()

    with pytest.raises(sqlite3.DatabaseError, match="planner_db_schema_invalid"):
        SQLiteStateStore(path)

    assert path.read_bytes() == before


def test_planner_store_rejects_changed_v5_table_sql_before_v6_migration(
    tmp_path: Path,
) -> None:
    path = tmp_path / "forged-v5.sqlite3"
    with SQLiteStateStore(path):
        pass
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE intraday_plan_cohorts")
    connection.execute("DELETE FROM schema_migrations WHERE version = 6")
    schema_version = int(
        connection.execute("PRAGMA schema_version").fetchone()[0]
    )
    connection.execute("PRAGMA writable_schema = ON")
    connection.execute(
        """
        UPDATE sqlite_master
        SET sql = replace(
          sql,
          'id INTEGER PRIMARY KEY AUTOINCREMENT,',
          'id INTEGER PRIMARY KEY,'
        )
        WHERE type = 'table' AND name = 'runtime_events'
        """
    )
    connection.execute("PRAGMA writable_schema = OFF")
    connection.execute(f"PRAGMA schema_version = {schema_version + 1}")
    connection.commit()
    assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    connection.close()
    before = path.read_bytes()

    with pytest.raises(sqlite3.DatabaseError, match="planner_db_schema_invalid"):
        SQLiteStateStore(path)

    assert path.read_bytes() == before


def _intraday_payload(plan_id: str = "intraday-plan-1") -> dict[str, object]:
    return {
        "plan_id": plan_id,
        "account_id": "acct-hash",
        "session_date": "2026-08-28",
        "mode": "shadow",
        "status": "SHADOW_PLANNED",
        "symbol": "AAPL",
        "quantity": 1,
        "entry_limit": "100.10",
    }


def _intraday_notification() -> dict[str, object]:
    return {
        "notification_key": "intraday-plan:acct-hash:2026-08-28",
        "message": "intraday_shadow_plan_created",
        "level": "info",
        "payload": {"mode": "shadow", "live_order_submission": False},
    }


def _cohort_lane(
    plan_id: str, account_key: str, symbol: str
) -> tuple[dict[str, object], dict[str, object]]:
    payload = _intraday_payload(plan_id)
    payload["account_id"] = account_key
    payload["symbol"] = symbol
    notification = _intraday_notification()
    notification["notification_key"] = f"intraday-plan:{account_key}:2026-08-28"
    return payload, notification


def test_intraday_cohort_locks_two_distinct_plans_atomically_and_idempotently() -> None:
    lane_a, notification_a = _cohort_lane("plan-a", "acct-a", "AAPL")
    lane_b, notification_b = _cohort_lane("plan-b", "acct-b", "MSFT")
    with SQLiteStateStore() as store:
        first, inserted = store.save_intraday_cohort_once(
            cohort_id="cohort-1",
            session_date="2026-08-28",
            lane_a_status="PLAN",
            lane_a_account_key="acct-a",
            lane_a_symbol="AAPL",
            lane_a_payload=lane_a,
            lane_a_notification=notification_a,
            lane_b_status="PLAN",
            lane_b_account_key="acct-b",
            lane_b_symbol="MSFT",
            lane_b_payload=lane_b,
            lane_b_notification=notification_b,
        )
        repeated, repeated_inserted = store.save_intraday_cohort_once(
            cohort_id="cohort-1",
            session_date="2026-08-28",
            lane_a_status="PLAN",
            lane_a_account_key="acct-a",
            lane_a_symbol="AAPL",
            lane_a_payload=lane_a,
            lane_a_notification=notification_a,
            lane_b_status="PLAN",
            lane_b_account_key="acct-b",
            lane_b_symbol="MSFT",
            lane_b_payload=lane_b,
            lane_b_notification=notification_b,
        )

        assert inserted is True
        assert repeated_inserted is False
        assert repeated == first
        assert first["lanes"]["A"]["status"] == "PLAN"
        assert first["lanes"]["A"]["plan"]["symbol"] == "AAPL"
        assert first["lanes"]["B"]["plan"]["symbol"] == "MSFT"
        assert len(store.list_intraday_plans()) == 2
        assert len(store.list_notification_outbox()) == 2

        changed_b = dict(lane_b)
        changed_b["quantity"] = 2
        with pytest.raises(ValueError, match="different data"):
            store.save_intraday_cohort_once(
                cohort_id="cohort-1",
                session_date="2026-08-28",
                lane_a_status="PLAN",
                lane_a_account_key="acct-a",
                lane_a_symbol="AAPL",
                lane_a_payload=lane_a,
                lane_a_notification=notification_a,
                lane_b_status="PLAN",
                lane_b_account_key="acct-b",
                lane_b_symbol="MSFT",
                lane_b_payload=changed_b,
                lane_b_notification=notification_b,
            )

        changed_notification = dict(notification_b)
        changed_notification["payload"] = {"plan_id": "changed-notification"}
        with pytest.raises(ValueError, match="different data"):
            store.save_intraday_cohort_once(
                cohort_id="cohort-1",
                session_date="2026-08-28",
                lane_a_status="PLAN",
                lane_a_account_key="acct-a",
                lane_a_symbol="AAPL",
                lane_a_payload=lane_a,
                lane_a_notification=notification_a,
                lane_b_status="PLAN",
                lane_b_account_key="acct-b",
                lane_b_symbol="MSFT",
                lane_b_payload=lane_b,
                lane_b_notification=changed_notification,
            )


def test_intraday_cohort_supports_non_plan_lane_outcomes() -> None:
    lane_a, notification_a = _cohort_lane("plan-a", "acct-a", "AAPL")
    with SQLiteStateStore() as store:
        mixed, _ = store.save_intraday_cohort_once(
            cohort_id="cohort-mixed",
            session_date="2026-08-28",
            lane_a_status="PLAN",
            lane_a_account_key="acct-a",
            lane_a_symbol="AAPL",
            lane_a_payload=lane_a,
            lane_a_notification=notification_a,
            lane_b_status="NO_CANDIDATE",
            lane_b_account_key="acct-b",
        )
        assert mixed["lanes"]["B"] == {"status": "NO_CANDIDATE", "plan": None}
        assert mixed["manifest"]["lanes"]["B"]["symbol"] is None

    with SQLiteStateStore() as store:
        closed, _ = store.save_intraday_cohort_once(
            cohort_id="cohort-closed",
            session_date="2026-08-28",
            lane_a_status="MARKET_CLOSED",
            lane_a_account_key="acct-a",
            lane_b_status="MARKET_CLOSED",
            lane_b_account_key="acct-b",
        )
        assert closed["lanes"]["A"]["plan"] is None
        assert closed["lanes"]["B"]["status"] == "MARKET_CLOSED"
        with pytest.raises(ValueError, match="both"):
            store.save_intraday_cohort_once(
                cohort_id="cohort-invalid",
                session_date="2026-08-29",
                lane_a_status="MARKET_CLOSED",
                lane_a_account_key="acct-a",
                lane_b_status="NO_CANDIDATE",
                lane_b_account_key="acct-b",
            )


def test_intraday_cohort_failure_rolls_back_both_plans_and_notifications() -> None:
    lane_a, notification_a = _cohort_lane("plan-a", "acct-a", "AAPL")
    lane_b, notification_b = _cohort_lane("plan-b", "acct-b", "MSFT")
    with SQLiteStateStore() as store:
        store._conn.execute(
            """
            CREATE TRIGGER reject_cohort BEFORE INSERT ON intraday_plan_cohorts
            BEGIN SELECT RAISE(ABORT, 'forced cohort failure'); END
            """
        )
        with pytest.raises(ValueError, match="transaction failed"):
            store.save_intraday_cohort_once(
                cohort_id="cohort-1",
                session_date="2026-08-28",
                lane_a_status="PLAN",
                lane_a_account_key="acct-a",
                lane_a_symbol="AAPL",
                lane_a_payload=lane_a,
                lane_a_notification=notification_a,
                lane_b_status="PLAN",
                lane_b_account_key="acct-b",
                lane_b_symbol="MSFT",
                lane_b_payload=lane_b,
                lane_b_notification=notification_b,
            )
        assert store.list_intraday_plans() == []
        assert store.list_notification_outbox() == []


def test_intraday_cohort_database_check_rejects_duplicate_plan_symbol() -> None:
    lane_a, _ = _cohort_lane("plan-a", "acct-a", "AAPL")
    lane_b, _ = _cohort_lane("plan-b", "acct-b", "AAPL")
    with SQLiteStateStore() as store:
        store.save_intraday_plan_once(
            account_key="acct-a",
            session_date="2026-08-28",
            symbol="AAPL",
            payload=lane_a,
        )
        store.save_intraday_plan_once(
            account_key="acct-b",
            session_date="2026-08-28",
            symbol="AAPL",
            payload=lane_b,
        )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            store._conn.execute(
                """
                INSERT INTO intraday_plan_cohorts (
                    cohort_id, session_date, lane_a_status, lane_b_status,
                    lane_a_plan_id, lane_b_plan_id,
                    lane_a_account_key, lane_b_account_key,
                    lane_a_symbol, lane_b_symbol,
                    manifest_hash, manifest, created_at
                ) VALUES (?, ?, 'PLAN', 'PLAN', ?, ?, ?, ?, ?, ?, ?, '{}', ?)
                """,
                (
                    "cohort-1",
                    "2026-08-28",
                    "plan-a",
                    "plan-b",
                    "acct-a",
                    "acct-b",
                    "AAPL",
                    "AAPL",
                    "0" * 64,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )


def test_intraday_plan_is_immutable_and_idempotent_per_account_session() -> None:
    with SQLiteStateStore() as store:
        first, inserted = store.save_intraday_plan_once(
            account_key="acct-hash",
            session_date=date(2026, 8, 28),
            symbol="aapl",
            payload=_intraday_payload(),
        )
        duplicate, duplicate_inserted = store.save_intraday_plan_once(
            account_key="acct-hash",
            session_date="2026-08-28",
            symbol="AAPL",
            payload=_intraday_payload(),
        )

        assert inserted is True
        assert duplicate_inserted is False
        assert first == duplicate
        assert first["session_date"] == date(2026, 8, 28)
        assert first["mode"] == "shadow"

        changed = _intraday_payload("intraday-plan-2")
        changed["symbol"] = "MSFT"
        with pytest.raises(ValueError, match="already locked"):
            store.save_intraday_plan_once(
                account_key="acct-hash",
                session_date="2026-08-28",
                symbol="MSFT",
                payload=changed,
            )


def test_intraday_plan_unique_constraint_holds_across_connections(tmp_path) -> None:
    path = tmp_path / "state.sqlite"
    with SQLiteStateStore(path) as first, SQLiteStateStore(path) as second:
        first.save_intraday_plan_once(
            account_key="acct-hash",
            session_date="2026-08-28",
            symbol="AAPL",
            payload=_intraday_payload(),
        )
        changed = _intraday_payload("intraday-plan-2")
        changed["symbol"] = "MSFT"
        with pytest.raises(ValueError, match="already locked"):
            second.save_intraday_plan_once(
                account_key="acct-hash",
                session_date="2026-08-28",
                symbol="MSFT",
                payload=changed,
            )


def test_intraday_plan_detects_payload_tampering(tmp_path) -> None:
    path = tmp_path / "state.sqlite"
    with SQLiteStateStore(path) as store:
        store.save_intraday_plan_once(
            account_key="acct-hash",
            session_date="2026-08-28",
            symbol="AAPL",
            payload=_intraday_payload(),
        )
        store._conn.execute(  # deliberate corruption for integrity verification
            "UPDATE intraday_plans SET payload = ?",
            ('{"plan_id":"changed"}',),
        )
        store._conn.commit()

        with pytest.raises(RuntimeError, match="integrity"):
            store.load_intraday_plan(
                account_key="acct-hash",
                session_date="2026-08-28",
            )


def test_intraday_plan_and_notification_are_atomic_and_retryable(tmp_path) -> None:
    path = tmp_path / "state.sqlite"
    at = datetime(2026, 8, 28, 12, 30, tzinfo=timezone.utc)
    with SQLiteStateStore(path) as store:
        store.save_intraday_plan_once(
            account_key="acct-hash",
            session_date="2026-08-28",
            symbol="AAPL",
            payload=_intraday_payload(),
            created_at=at,
            notification=_intraday_notification(),
        )

        pending = store.list_notification_outbox()
        assert len(pending) == 1
        assert pending[0]["status"] == "PENDING"

        first_claim = store.claim_pending_notification(now=at)
        assert first_claim is not None
        store.mark_notification_failed(
            notification_key=first_claim["notification_key"],
            claim_token=first_claim["claim_token"],
            error_code="discord_send_failed",
        )
        second_claim = store.claim_pending_notification(now=at + timedelta(seconds=1))
        assert second_claim is not None
        store.mark_notification_sent(
            notification_key=second_claim["notification_key"],
            claim_token=second_claim["claim_token"],
            sent_at=at + timedelta(seconds=1),
        )

        sent = store.list_notification_outbox()[0]
        assert sent["status"] == "SENT"
        assert sent["attempt_count"] == 2
        assert sent["last_error_code"] is None


def test_invalid_notification_rolls_back_intraday_plan_insert() -> None:
    invalid = _intraday_notification()
    invalid["payload"] = "not-an-object"
    with SQLiteStateStore() as store:
        with pytest.raises(TypeError, match="payload"):
            store.save_intraday_plan_once(
                account_key="acct-hash",
                session_date="2026-08-28",
                symbol="AAPL",
                payload=_intraday_payload(),
                notification=invalid,
            )

        assert store.list_intraday_plans() == []
        assert store.list_notification_outbox() == []


def test_notification_claim_is_exclusive_across_connections(tmp_path) -> None:
    path = tmp_path / "state.sqlite"
    at = datetime(2026, 8, 28, 12, 30, tzinfo=timezone.utc)
    with SQLiteStateStore(path) as first, SQLiteStateStore(path) as second:
        first.enqueue_notification_once(
            notification_key="intraday-blocked:acct:2026-08-28:AAPL:stale",
            message="intraday_shadow_plan_blocked",
            level="warn",
            payload={"blocker": "stale"},
            created_at=at,
        )

        claimed = first.claim_pending_notification(now=at)
        assert claimed is not None
        assert second.claim_pending_notification(now=at) is None

        first.mark_notification_failed(
            notification_key=claimed["notification_key"],
            claim_token=claimed["claim_token"],
            error_code="discord_send_failed",
        )
        assert second.claim_pending_notification(now=at) is not None


def test_notification_stale_lease_recovers_and_rejects_old_claim() -> None:
    at = datetime(2026, 8, 28, 12, 30, tzinfo=timezone.utc)
    with SQLiteStateStore() as store:
        store.enqueue_notification_once(
            notification_key="intraday-plan:acct:2026-08-28",
            message="intraday_shadow_plan_created",
            level="info",
            payload={"mode": "shadow"},
            created_at=at,
        )
        stale = store.claim_pending_notification(now=at, lease_seconds=300)
        assert stale is not None
        assert (
            store.claim_pending_notification(
                now=at + timedelta(seconds=299), lease_seconds=300
            )
            is None
        )

        recovered = store.claim_pending_notification(
            now=at + timedelta(seconds=301), lease_seconds=300
        )
        assert recovered is not None
        assert recovered["claim_token"] != stale["claim_token"]
        with pytest.raises(ValueError, match="no longer active"):
            store.mark_notification_sent(
                notification_key=stale["notification_key"],
                claim_token=stale["claim_token"],
            )

        store.mark_notification_sent(
            notification_key=recovered["notification_key"],
            claim_token=recovered["claim_token"],
        )


def test_outbox_insert_failure_rolls_back_plan_transaction() -> None:
    with SQLiteStateStore() as store:
        store._conn.execute(
            """
            CREATE TRIGGER reject_notification
            BEFORE INSERT ON notification_outbox
            BEGIN
              SELECT RAISE(ABORT, 'forced outbox failure');
            END
            """
        )
        with pytest.raises(ValueError):
            store.save_intraday_plan_once(
                account_key="acct-hash",
                session_date="2026-08-28",
                symbol="AAPL",
                payload=_intraday_payload(),
                notification=_intraday_notification(),
            )

        assert store.list_intraday_plans() == []
        assert store.list_notification_outbox() == []


@pytest.mark.parametrize("existing_versions", [(1,), (1, 2), (1, 2, 3)])
def test_existing_database_receives_v4_intraday_schema_migration(
    tmp_path, existing_versions: tuple[int, ...]
) -> None:
    path = tmp_path / f"v{existing_versions[-1]}.sqlite"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    connection.executemany(
        "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        [
            (version, datetime.now(timezone.utc).isoformat())
            for version in existing_versions
        ],
    )
    connection.commit()
    connection.close()

    with SQLiteStateStore(path) as store:
        versions = [
            row["version"]
            for row in store._conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        tables = {
            row["name"]
            for row in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert versions == [1, 2, 3, 4, 5, 6]
    assert "intraday_plans" in tables
    assert "notification_outbox" in tables
    assert "intraday_runs" in tables
    assert "intraday_plan_cohorts" in tables


def _create_intraday_run(
    store: SQLiteStateStore,
    *,
    plan_id: str = "intraday-plan-1",
    account_key: str = "acct-hash",
    session_date: str = "2026-08-28",
    created_at: datetime | None = None,
) -> dict[str, object]:
    payload = _intraday_payload(plan_id)
    payload["account_id"] = account_key
    payload["session_date"] = session_date
    store.save_intraday_plan_once(
        account_key=account_key,
        session_date=session_date,
        symbol="AAPL",
        payload=payload,
        created_at=created_at,
    )
    return store.create_intraday_run(plan_id=plan_id, created_at=created_at)


def _advance_intraday_run_to_ready(
    store: SQLiteStateStore, *, at: datetime, writer_id: str = "writer-a"
) -> dict[str, object]:
    plan = store.load_intraday_plan(
        account_key="acct-hash", session_date="2026-08-28"
    )
    assert plan is not None
    approved = store.consume_intraday_approval(
        plan_id="intraday-plan-1",
        plan_hash=plan["plan_hash"],
        envelope_sha256="a" * 64,
        receipt_sha256="c" * 64,
        interaction_id="interaction-intraday-plan-1",
        boot_id_hash="b" * 64,
        approval_generation=1,
        approved_writer_fence=1,
        writer_id=writer_id,
        writer_fence=1,
        approved_at=at,
        approval_expires_at=at + timedelta(minutes=5),
        now=at,
    )
    assert approved is not None
    reconciling = store.cas_intraday_run(
        plan_id="intraday-plan-1",
        expected_state="APPROVED",
        expected_version=1,
        next_state="RECONCILING",
        writer_id=writer_id,
        writer_fence=1,
        event_type="reconcile_started",
        now=at,
    )
    assert reconciling is not None
    ready = store.cas_intraday_run(
        plan_id="intraday-plan-1",
        expected_state="RECONCILING",
        expected_version=2,
        next_state="READY_TO_ENTER",
        writer_id=writer_id,
        writer_fence=1,
        event_type="ready",
        now=at,
    )
    assert ready is not None
    return ready


def test_v4_migration_is_complete_and_skips_when_already_applied() -> None:
    with SQLiteStateStore() as store:
        intent_columns = {
            row["name"] for row in store._conn.execute("PRAGMA table_info(order_intents)")
        }
        versions_before = store._conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 4"
        ).fetchone()[0]

        store._migrate_v4()

        assert versions_before == 1
        assert store._conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 4"
        ).fetchone()[0] == 1
        assert {"plan_id", "request_hash", "reserved_writer_fence"} <= intent_columns
        assert store._conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert store._conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_partial_v4_schema_is_rejected_before_any_migration(tmp_path) -> None:
    path = tmp_path / "broken-v3.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        INSERT INTO schema_migrations VALUES (1, '2026-08-28T00:00:00+00:00');
        CREATE TABLE order_intents (
          intent_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL,
          symbol TEXT NOT NULL, side TEXT NOT NULL, quantity TEXT NOT NULL,
          order_type TEXT NOT NULL, limit_price TEXT, payload TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE execution_orders (
          intent_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL,
          symbol TEXT NOT NULL, side TEXT NOT NULL, status TEXT NOT NULL,
          broker_order_id TEXT, raw TEXT, updated_at TEXT NOT NULL,
          filled_quantity TEXT NOT NULL DEFAULT '0'
        );
        """
    )
    connection.close()
    before = path.read_bytes()

    with pytest.raises(sqlite3.DatabaseError, match="planner_db_schema_invalid"):
        SQLiteStateStore(path)

    assert path.read_bytes() == before
    connection = sqlite3.connect(path)
    intent_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(order_intents)")
    }
    versions = [
        row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
    ]
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    connection.close()
    assert "account_key" not in intent_columns
    assert "intraday_runs" not in tables
    assert versions == [1]


def test_intraday_writer_lease_fences_stale_process_and_cas_is_versioned() -> None:
    at = datetime(2026, 8, 28, 12, 30, tzinfo=timezone.utc)
    with SQLiteStateStore() as store:
        _create_intraday_run(store, created_at=at)
        first = store.claim_intraday_writer(
            plan_id="intraday-plan-1", writer_id="writer-a", now=at
        )
        assert first is not None and first["writer_fence"] == 1
        assert store.claim_intraday_writer(
            plan_id="intraday-plan-1",
            writer_id="writer-a",
            now=at + timedelta(seconds=1),
        ) is None
        assert store.renew_intraday_writer(
            plan_id="intraday-plan-1",
            writer_id="writer-a",
            writer_fence=1,
            now=at + timedelta(seconds=1),
        ) is not None
        plan = store.load_intraday_plan(
            account_key="acct-hash", session_date="2026-08-28"
        )
        assert plan is not None
        transitioned = store.consume_intraday_approval(
            plan_id="intraday-plan-1",
            plan_hash=plan["plan_hash"],
            envelope_sha256="a" * 64,
            receipt_sha256="c" * 64,
            interaction_id="interaction-intraday-plan-1",
            boot_id_hash="b" * 64,
            approval_generation=1,
            approved_writer_fence=1,
            writer_id="writer-a",
            writer_fence=1,
            approved_at=at,
            approval_expires_at=at + timedelta(minutes=5),
            now=at,
        )
        assert transitioned is not None and transitioned["version"] == 1
        assert store.cas_intraday_run(
            plan_id="intraday-plan-1",
            expected_state="APPROVED",
            expected_version=0,
            next_state="RECONCILING",
            writer_id="writer-a",
            writer_fence=1,
            event_type="stale_transition",
            now=at,
        ) is None

        takeover_at = at + timedelta(seconds=46)
        second = store.claim_intraday_writer(
            plan_id="intraday-plan-1", writer_id="writer-b", now=takeover_at
        )
        assert second is not None and second["writer_fence"] == 2
        assert second["broker_sync_fence"] == -1
        assert store.cas_intraday_run(
            plan_id="intraday-plan-1",
            expected_state="APPROVED",
            expected_version=1,
            next_state="RECONCILING",
            writer_id="writer-a",
            writer_fence=1,
            event_type="stale_writer",
            now=takeover_at,
        ) is None
        current = store.cas_intraday_run(
            plan_id="intraday-plan-1",
            expected_state="APPROVED",
            expected_version=1,
            next_state="RECONCILING",
            writer_id="writer-b",
            writer_fence=2,
            event_type="reconcile_complete",
            now=takeover_at,
        )
        assert current is not None and current["version"] == 2

        versions = [
            row[0]
            for row in store._conn.execute(
                "SELECT run_version FROM execution_events WHERE plan_id = ? ORDER BY run_version",
                ("intraday-plan-1",),
            )
        ]
        assert versions == [0, 1, 2]


def test_intraday_intent_reservation_is_immutable_and_send_gate_is_fenced() -> None:
    at = datetime(2026, 8, 28, 12, 30, tzinfo=timezone.utc)
    with SQLiteStateStore() as store:
        _create_intraday_run(store, created_at=at)
        claimed = store.claim_intraday_writer(
            plan_id="intraday-plan-1", writer_id="writer-a", now=at
        )
        assert claimed is not None
        ready = _advance_intraday_run_to_ready(store, at=at)
        assert ready is not None
        assert store.mark_intraday_broker_synced(
            plan_id="intraday-plan-1", writer_id="writer-a", writer_fence=1, now=at
        ) is not None
        with pytest.raises(ValueError, match="not allowed"):
            store.cas_intraday_run(
                plan_id="intraday-plan-1",
                expected_state="READY_TO_ENTER",
                expected_version=3,
                next_state="PROTECTED",
                writer_id="writer-a",
                writer_fence=1,
                event_type="impossible_jump",
                now=at,
            )

        arguments = {
            "plan_id": "intraday-plan-1",
            "account_key": "acct-hash",
            "intent_id": "entry-intent-1",
            "idempotency_key": "entry_20260828_1",
            "order_role": "ENTRY",
            "method": "POST",
            "path": "/api/v1/orders",
            "body": {
                "clientOrderId": "entry_20260828_1",
                "symbol": "AAPL",
                "side": "BUY",
                "orderType": "LIMIT",
                "quantity": Decimal("1.0"),
                "price": "100",
            },
            "symbol": "AAPL",
            "side": "BUY",
            "quantity": "1.0",
            "order_type": "LIMIT",
            "limit_price": "100.00",
            "expected_state": "READY_TO_ENTER",
            "expected_version": 3,
            "next_state": "ENTRY_SUBMITTING",
            "writer_id": "writer-a",
            "writer_fence": 1,
            "send_by": at + timedelta(seconds=5),
            "now": at + timedelta(seconds=1),
        }
        reserved = store.reserve_intraday_order_intent(**arguments)
        assert reserved["inserted"] is True
        assert reserved["run"]["version"] == 4
        assert reserved["run"]["entry_submit_count"] == 1
        intent = reserved["intent"]
        assert intent["quantity"] == Decimal("1")
        assert intent["limit_price"] == Decimal("100")
        assert intent["body"]["quantity"] == "1"
        assert intent["reserved_run_version"] == 4
        assert intent["recovery_deadline_at"] == at + timedelta(seconds=1, minutes=8)
        with pytest.raises(ValueError, match="fenced action"):
            store.record_execution_order(
                intent_id="entry-intent-1",
                idempotency_key="entry_20260828_1",
                symbol="AAPL",
                side="BUY",
                status="FILLED",
            )

        duplicate = store.reserve_intraday_order_intent(**arguments)
        assert duplicate["inserted"] is False
        changed = dict(arguments)
        changed["body"] = {
            **arguments["body"],
            "requestVariant": "different",
        }
        with pytest.raises(ValueError, match="conflicts"):
            store.reserve_intraday_order_intent(**changed)

        assert store.intraday_reservation_is_sendable(
            intent_id="entry-intent-1",
            plan_id="intraday-plan-1",
            expected_state="ENTRY_SUBMITTING",
            expected_run_version=4,
            writer_id="writer-a",
            writer_fence=1,
            request_hash=intent["request_hash"],
            now=at + timedelta(seconds=2),
        )
        assert not store.intraday_reservation_is_sendable(
            intent_id="entry-intent-1",
            plan_id="intraday-plan-1",
            expected_state="ENTRY_SUBMITTING",
            expected_run_version=4,
            writer_id="writer-a",
            writer_fence=1,
            request_hash=intent["request_hash"],
            now=at + timedelta(seconds=6),
        )


def test_intraday_recovery_latch_and_order_completion_are_atomic() -> None:
    at = datetime(2026, 8, 28, 12, 30, tzinfo=timezone.utc)
    with SQLiteStateStore() as store:
        _create_intraday_run(store, created_at=at)
        store.claim_intraday_writer(
            plan_id="intraday-plan-1", writer_id="writer-a", now=at
        )
        _advance_intraday_run_to_ready(store, at=at)
        store.mark_intraday_broker_synced(
            plan_id="intraday-plan-1", writer_id="writer-a", writer_fence=1, now=at
        )
        reserved = store.reserve_intraday_order_intent(
            plan_id="intraday-plan-1",
            account_key="acct-hash",
            intent_id="entry-intent-1",
            idempotency_key="entry_20260828_1",
            order_role="ENTRY",
            method="POST",
            path="/api/v1/orders",
            body={
                "clientOrderId": "entry_20260828_1",
                "symbol": "AAPL",
                "side": "BUY",
                "orderType": "LIMIT",
                "quantity": "1",
                "price": "100",
            },
            symbol="AAPL",
            side="BUY",
            quantity="1",
            order_type="LIMIT",
            limit_price="100",
            expected_state="READY_TO_ENTER",
            expected_version=3,
            next_state="ENTRY_SUBMITTING",
            writer_id="writer-a",
            writer_fence=1,
            send_by=at + timedelta(seconds=5),
            now=at + timedelta(seconds=1),
        )
        assert reserved["inserted"] is True
        unknown = store.complete_intraday_order_action(
            plan_id="intraday-plan-1",
            intent_id="entry-intent-1",
            expected_state="ENTRY_SUBMITTING",
            expected_version=4,
            next_state="ENTRY_UNKNOWN",
            writer_id="writer-a",
            writer_fence=1,
            event_type="entry_result_unknown",
            status="UNKNOWN",
            remaining_quantity="1",
            now=at + timedelta(seconds=2),
        )
        assert unknown is not None and unknown["run"]["version"] == 5
        latched = store.reserve_intraday_action_event(
            plan_id="intraday-plan-1",
            intent_id="entry-intent-1",
            event_type="identity_recovery_send_reserved",
            expected_state="ENTRY_UNKNOWN",
            expected_version=5,
            writer_id="writer-a",
            writer_fence=1,
            now=at + timedelta(seconds=2),
        )
        assert latched is not None and latched["version"] == 6
        assert store.intraday_action_is_sendable(
            plan_id="intraday-plan-1",
            intent_id="entry-intent-1",
            event_type="identity_recovery_send_reserved",
            expected_state="ENTRY_UNKNOWN",
            expected_run_version=6,
            writer_id="writer-a",
            writer_fence=1,
            request_hash=reserved["intent"]["request_hash"],
            now=at + timedelta(seconds=3),
        )
        assert store.reserve_intraday_action_event(
            plan_id="intraday-plan-1",
            intent_id="entry-intent-1",
            event_type="identity_recovery_send_reserved",
            expected_state="ENTRY_UNKNOWN",
            expected_version=6,
            writer_id="writer-a",
            writer_fence=1,
            now=at + timedelta(seconds=3),
        ) is None

        completed = store.complete_intraday_order_action(
            plan_id="intraday-plan-1",
            intent_id="entry-intent-1",
            expected_state="ENTRY_UNKNOWN",
            expected_version=6,
            next_state="OPEN_UNPROTECTED",
            writer_id="writer-a",
            writer_fence=1,
            event_type="entry_filled",
            status="FILLED",
            broker_order_id="broker-order-1",
            filled_quantity="1",
            remaining_quantity="0",
            average_fill_price="100",
            run_updates={"owned_qty": "1", "unprotected_since": at + timedelta(seconds=3)},
            payload={"source": "fake-broker"},
            now=at + timedelta(seconds=3),
        )
        assert completed is not None
        assert completed["run"]["version"] == 7
        assert completed["run"]["owned_qty"] == Decimal("1")
        assert completed["order"]["filled_quantity"] == Decimal("1")
        assert completed["order"]["broker_order_id"] == "broker-order-1"
        assert store.append_intraday_observation_event(
            plan_id="intraday-plan-1",
            intent_id="entry-intent-1",
            event_type="broker_snapshot_observed",
            status="FILLED",
            writer_id="writer-a",
            writer_fence=1,
            payload={"source": "fake-broker"},
            now=at + timedelta(seconds=4),
        )
        assert not store.append_intraday_observation_event(
            plan_id="intraday-plan-1",
            intent_id="entry-intent-1",
            event_type="stale_snapshot",
            status="FILLED",
            writer_id="writer-a",
            writer_fence=0,
            now=at + timedelta(seconds=4),
        )
        observation = store._conn.execute(
            """
            SELECT run_version FROM execution_events
            WHERE intent_id = 'entry-intent-1' AND event_type = 'broker_snapshot_observed'
            """
        ).fetchone()
        assert observation["run_version"] is None
        assert store.complete_intraday_order_action(
            plan_id="intraday-plan-1",
            intent_id="entry-intent-1",
            expected_state="ENTRY_UNKNOWN",
            expected_version=6,
            next_state="ENTRY_WORKING",
            writer_id="writer-a",
            writer_fence=1,
            event_type="stale_result",
            status="ACKNOWLEDGED",
            now=at + timedelta(seconds=4),
        ) is None


def test_entry_partial_fills_advance_atomically_and_reject_decrease_or_stale_fence() -> None:
    at = datetime(2026, 8, 28, 12, 30, tzinfo=timezone.utc)
    with SQLiteStateStore() as store:
        _create_intraday_run(store, created_at=at)
        store.claim_intraday_writer(
            plan_id="intraday-plan-1", writer_id="writer-a", now=at
        )
        _advance_intraday_run_to_ready(store, at=at)
        store.mark_intraday_broker_synced(
            plan_id="intraday-plan-1", writer_id="writer-a", writer_fence=1, now=at
        )
        reserved = store.reserve_intraday_order_intent(
            plan_id="intraday-plan-1",
            account_key="acct-hash",
            intent_id="entry-intent-1",
            idempotency_key="entry_20260828_1",
            order_role="ENTRY",
            method="POST",
            path="/api/v1/orders",
            body={
                "clientOrderId": "entry_20260828_1",
                "symbol": "AAPL",
                "side": "BUY",
                "orderType": "LIMIT",
                "quantity": "1",
                "price": "100",
            },
            symbol="AAPL",
            side="BUY",
            quantity="1",
            order_type="LIMIT",
            limit_price="100",
            expected_state="READY_TO_ENTER",
            expected_version=3,
            next_state="ENTRY_SUBMITTING",
            writer_id="writer-a",
            writer_fence=1,
            send_by=at + timedelta(seconds=5),
            now=at + timedelta(seconds=1),
        )
        working = store.complete_intraday_order_action(
            plan_id="intraday-plan-1",
            intent_id="entry-intent-1",
            expected_state="ENTRY_SUBMITTING",
            expected_version=4,
            next_state="ENTRY_WORKING",
            writer_id="writer-a",
            writer_fence=1,
            event_type="entry_acknowledged",
            status="ACKNOWLEDGED",
            broker_order_id="broker-order-1",
            remaining_quantity="1",
            now=at + timedelta(seconds=2),
        )
        assert reserved["inserted"] is True
        assert working is not None and working["run"]["version"] == 5

        first = store.complete_intraday_order_action(
            plan_id="intraday-plan-1",
            intent_id="entry-intent-1",
            expected_state="ENTRY_WORKING",
            expected_version=5,
            next_state="ENTRY_WORKING",
            writer_id="writer-a",
            writer_fence=1,
            event_type="entry_partial_fill",
            status="PARTIAL_FILLED",
            broker_order_id="broker-order-1",
            filled_quantity="0.5",
            remaining_quantity="0.5",
            average_fill_price="100",
            run_updates={"owned_qty": "0.5", "average_entry_price": "100"},
            now=at + timedelta(seconds=3),
        )
        second = store.complete_intraday_order_action(
            plan_id="intraday-plan-1",
            intent_id="entry-intent-1",
            expected_state="ENTRY_WORKING",
            expected_version=6,
            next_state="ENTRY_WORKING",
            writer_id="writer-a",
            writer_fence=1,
            event_type="entry_partial_fill",
            status="PARTIAL_FILLED",
            broker_order_id="broker-order-1",
            filled_quantity="0.75",
            remaining_quantity="0.25",
            average_fill_price="100.1",
            run_updates={"owned_qty": "0.75", "average_entry_price": "100.1"},
            now=at + timedelta(seconds=4),
        )
        assert first is not None and first["run"]["owned_qty"] == Decimal("0.5")
        assert second is not None and second["run"]["version"] == 7
        assert second["run"]["owned_qty"] == Decimal("0.75")
        assert second["order"]["filled_quantity"] == Decimal("0.75")

        with pytest.raises(ValueError, match="may not decrease"):
            store.complete_intraday_order_action(
                plan_id="intraday-plan-1",
                intent_id="entry-intent-1",
                expected_state="ENTRY_WORKING",
                expected_version=7,
                next_state="ENTRY_WORKING",
                writer_id="writer-a",
                writer_fence=1,
                event_type="entry_partial_fill",
                status="PARTIAL_FILLED",
                filled_quantity="0.6",
                remaining_quantity="0.4",
                run_updates={"owned_qty": "0.6"},
                now=at + timedelta(seconds=5),
            )
        assert store.complete_intraday_order_action(
            plan_id="intraday-plan-1",
            intent_id="entry-intent-1",
            expected_state="ENTRY_WORKING",
            expected_version=7,
            next_state="ENTRY_WORKING",
            writer_id="writer-a",
            writer_fence=0,
            event_type="stale_partial_fill",
            status="FILLED",
            filled_quantity="1",
            remaining_quantity="0",
            run_updates={"owned_qty": "1"},
            now=at + timedelta(seconds=6),
        ) is None
        unchanged_run = store.load_intraday_run("intraday-plan-1")
        unchanged_order = store.load_execution_order("entry-intent-1")
        assert unchanged_run is not None and unchanged_run["version"] == 7
        assert unchanged_run["owned_qty"] == Decimal("0.75")
        assert unchanged_order is not None
        assert unchanged_order["filled_quantity"] == Decimal("0.75")


def test_consume_intraday_approval_rejects_bad_boundary_inputs_without_mutation() -> None:
    at = datetime(2026, 8, 28, 12, 30, tzinfo=timezone.utc)
    with SQLiteStateStore() as store:
        _create_intraday_run(store, created_at=at)
        store.claim_intraday_writer(
            plan_id="intraday-plan-1", writer_id="writer-a", now=at
        )
        plan = store.load_intraday_plan(
            account_key="acct-hash", session_date="2026-08-28"
        )
        assert plan is not None
        approval = {
            "plan_id": "intraday-plan-1",
            "plan_hash": plan["plan_hash"],
            "envelope_sha256": "a" * 64,
            "receipt_sha256": "c" * 64,
            "interaction_id": "interaction-1",
            "boot_id_hash": "b" * 64,
            "approval_generation": 1,
            "approved_writer_fence": 1,
            "writer_id": "writer-a",
            "writer_fence": 1,
            "approved_at": at,
            "approval_expires_at": at + timedelta(minutes=5),
            "now": at,
        }

        for override in (
            {"plan_hash": "0" * 64},
            {"approval_generation": 2},
            {"writer_fence": 2},
            {"approved_writer_fence": 0},
            {"writer_id": "writer-b"},
        ):
            assert store.consume_intraday_approval(**(approval | override)) is None
        with pytest.raises(ValueError, match="SHA-256"):
            store.consume_intraday_approval(**(approval | {"receipt_sha256": "not-a-hash"}))
        with pytest.raises(ValueError, match="future"):
            store.consume_intraday_approval(
                **(approval | {"approved_at": at + timedelta(seconds=1)})
            )
        with pytest.raises(ValueError, match="requires consume"):
            store.cas_intraday_run(
                plan_id="intraday-plan-1",
                expected_state="PLANNED",
                expected_version=0,
                next_state="APPROVED",
                writer_id="writer-a",
                writer_fence=1,
                event_type="unsafe_approval",
                now=at,
            )
        untouched = store.load_intraday_run("intraday-plan-1")
        assert untouched is not None
        assert untouched["state"] == "PLANNED" and untouched["version"] == 0
        assert untouched["approval_generation"] == 0
        assert untouched["approval_receipt_sha256"] is None

        consumed = store.consume_intraday_approval(**approval)
        assert consumed is not None
        assert consumed["state"] == "APPROVED" and consumed["version"] == 1
        assert consumed["approval_generation"] == 1
        assert consumed["approved_envelope_sha256"] == "a" * 64
        assert consumed["approval_receipt_sha256"] == "c" * 64
        assert consumed["approval_interaction_id"] == "interaction-1"
        assert consumed["boot_id_hash"] == "b" * 64
        assert consumed["approved_writer_fence"] == 1
        assert store.consume_intraday_approval(**approval) is None
        events = store.list_execution_events(intent_id="run:intraday-plan-1")
        assert [event["event_type"] for event in events].count("approval_consumed") == 1


def test_approval_receipt_and_interaction_are_unique_across_runs() -> None:
    at = datetime(2026, 8, 28, 12, 30, tzinfo=timezone.utc)
    with SQLiteStateStore() as store:
        _create_intraday_run(store, created_at=at)
        _create_intraday_run(
            store,
            plan_id="intraday-plan-2",
            account_key="acct-two",
            session_date="2026-08-29",
            created_at=at,
        )
        store.claim_intraday_writer(
            plan_id="intraday-plan-1", writer_id="writer-a", now=at
        )
        store.claim_intraday_writer(
            plan_id="intraday-plan-2", writer_id="writer-b", now=at
        )
        first_plan = store.load_intraday_plan(
            account_key="acct-hash", session_date="2026-08-28"
        )
        second_plan = store.load_intraday_plan(
            account_key="acct-two", session_date="2026-08-29"
        )
        assert first_plan is not None and second_plan is not None
        common = {
            "envelope_sha256": "a" * 64,
            "receipt_sha256": "c" * 64,
            "interaction_id": "interaction-1",
            "boot_id_hash": "b" * 64,
            "approval_generation": 1,
            "approved_writer_fence": 1,
            "writer_fence": 1,
            "approved_at": at,
            "approval_expires_at": at + timedelta(minutes=5),
            "now": at,
        }
        assert store.consume_intraday_approval(
            plan_id="intraday-plan-1",
            plan_hash=first_plan["plan_hash"],
            writer_id="writer-a",
            **common,
        ) is not None
        assert store.consume_intraday_approval(
            plan_id="intraday-plan-2",
            plan_hash=second_plan["plan_hash"],
            writer_id="writer-b",
            **(common | {"interaction_id": "interaction-2"}),
        ) is None
        assert store.consume_intraday_approval(
            plan_id="intraday-plan-2",
            plan_hash=second_plan["plan_hash"],
            writer_id="writer-b",
            **(common | {"receipt_sha256": "d" * 64}),
        ) is None
        second = store.load_intraday_run("intraday-plan-2")
        assert second is not None and second["state"] == "PLANNED"
        assert second["version"] == 0 and second["approval_receipt_sha256"] is None


def test_concurrent_approval_consumers_have_exactly_one_winner(tmp_path) -> None:
    path = tmp_path / "approval-race.sqlite"
    at = datetime(2026, 8, 28, 12, 30, tzinfo=timezone.utc)
    with SQLiteStateStore(path) as store:
        _create_intraday_run(store, created_at=at)
        store.claim_intraday_writer(
            plan_id="intraday-plan-1", writer_id="writer-a", now=at
        )
        plan = store.load_intraday_plan(
            account_key="acct-hash", session_date="2026-08-28"
        )
        assert plan is not None
        plan_hash = plan["plan_hash"]
    barrier = Barrier(2)

    def consume() -> bool:
        with SQLiteStateStore(path) as worker:
            barrier.wait()
            return worker.consume_intraday_approval(
                plan_id="intraday-plan-1",
                plan_hash=plan_hash,
                envelope_sha256="a" * 64,
                receipt_sha256="c" * 64,
                interaction_id="interaction-1",
                boot_id_hash="b" * 64,
                approval_generation=1,
                approved_writer_fence=1,
                writer_id="writer-a",
                writer_fence=1,
                approved_at=at,
                approval_expires_at=at + timedelta(minutes=5),
                now=at,
            ) is not None

    with ThreadPoolExecutor(max_workers=2) as executor:
        winners = list(executor.map(lambda _: consume(), range(2)))
    assert winners.count(True) == 1
    with SQLiteStateStore(path) as store:
        run = store.load_intraday_run("intraday-plan-1")
        assert run is not None and run["state"] == "APPROVED" and run["version"] == 1
        events = store.list_execution_events(intent_id="run:intraday-plan-1")
        assert [event["event_type"] for event in events].count("approval_consumed") == 1


def test_intraday_approval_fields_are_all_or_none_and_interaction_is_unique() -> None:
    at = datetime(2026, 8, 28, 12, 30, tzinfo=timezone.utc)
    with SQLiteStateStore() as store:
        _create_intraday_run(store, created_at=at)
        _create_intraday_run(
            store,
            plan_id="intraday-plan-2",
            account_key="acct-two",
            session_date="2026-08-29",
            created_at=at,
        )
        with pytest.raises(sqlite3.IntegrityError):
            with store._conn:
                store._conn.execute(
                    "UPDATE intraday_runs SET approval_interaction_id = 'interaction-1' WHERE plan_id = ?",
                    ("intraday-plan-1",),
                )

        approval = (
            "e" * 64,
            "r" * 64,
            "interaction-1",
            at.isoformat(),
            0,
            "b" * 64,
        )
        with store._conn:
            store._conn.execute(
                """
                UPDATE intraday_runs
                SET approved_envelope_sha256 = ?, approval_receipt_sha256 = ?,
                    approval_interaction_id = ?, approved_at = ?,
                    approved_writer_fence = ?, boot_id_hash = ?
                WHERE plan_id = 'intraday-plan-1'
                """,
                approval,
            )
        with pytest.raises(sqlite3.IntegrityError):
            with store._conn:
                store._conn.execute(
                    """
                    UPDATE intraday_runs
                    SET approved_envelope_sha256 = ?, approval_receipt_sha256 = ?,
                        approval_interaction_id = ?, approved_at = ?,
                        approved_writer_fence = ?, boot_id_hash = ?
                    WHERE plan_id = 'intraday-plan-2'
                    """,
                    ("x" * 64, "y" * 64, "interaction-1", at.isoformat(), 0, "z" * 64),
                )


def test_expired_approval_cannot_transition_back_to_ready() -> None:
    at = datetime(2026, 8, 28, 12, 30, tzinfo=timezone.utc)
    with SQLiteStateStore() as store:
        _create_intraday_run(store, created_at=at)
        assert store.claim_intraday_writer(
            plan_id="intraday-plan-1", writer_id="writer-a", now=at
        ) is not None
        plan = store.load_intraday_plan(
            account_key="acct-hash", session_date="2026-08-28"
        )
        assert plan is not None
        approved = store.consume_intraday_approval(
            plan_id="intraday-plan-1",
            plan_hash=plan["plan_hash"],
            envelope_sha256="a" * 64,
            receipt_sha256="c" * 64,
            interaction_id="interaction-expiring",
            boot_id_hash="b" * 64,
            approval_generation=1,
            approved_writer_fence=1,
            writer_id="writer-a",
            writer_fence=1,
            approved_at=at,
            approval_expires_at=at + timedelta(minutes=5),
            now=at,
        )
        assert approved is not None
        reconciling = store.cas_intraday_run(
            plan_id="intraday-plan-1",
            expected_state="APPROVED",
            expected_version=1,
            next_state="RECONCILING",
            writer_id="writer-a",
            writer_fence=1,
            event_type="reconcile_started",
            now=at + timedelta(seconds=1),
        )
        assert reconciling is not None

        assert store.cas_intraday_run(
            plan_id="intraday-plan-1",
            expected_state="RECONCILING",
            expected_version=2,
            next_state="READY_TO_ENTER",
            writer_id="writer-a",
            writer_fence=1,
            event_type="ready_after_expiry",
            now=at + timedelta(minutes=5),
        ) is None
        unchanged = store.load_intraday_run("intraday-plan-1")

    assert unchanged is not None
    assert unchanged["state"] == "RECONCILING"
    assert unchanged["version"] == 2


def test_writer_takeover_invalidates_old_approval_for_ready_and_entry_reservation() -> None:
    at = datetime(2026, 8, 28, 12, 30, tzinfo=timezone.utc)
    takeover_at = at + timedelta(seconds=46)
    with SQLiteStateStore() as store:
        _create_intraday_run(store, created_at=at)
        assert store.claim_intraday_writer(
            plan_id="intraday-plan-1", writer_id="writer-a", now=at
        ) is not None
        ready = _advance_intraday_run_to_ready(store, at=at)
        assert ready["approved_writer_fence"] == 1
        taken = store.claim_intraday_writer(
            plan_id="intraday-plan-1", writer_id="writer-b", now=takeover_at
        )
        assert taken is not None and taken["writer_fence"] == 2
        assert store.mark_intraday_broker_synced(
            plan_id="intraday-plan-1",
            writer_id="writer-b",
            writer_fence=2,
            now=takeover_at,
        ) is not None

        reservation = store.reserve_intraday_order_intent(
            plan_id="intraday-plan-1",
            account_key="acct-hash",
            intent_id="entry-after-takeover",
            idempotency_key="entry_after_takeover",
            order_role="ENTRY",
            method="POST",
            path="/api/v1/orders",
            body={
                "clientOrderId": "entry_after_takeover",
                "symbol": "AAPL",
                "side": "BUY",
                "orderType": "LIMIT",
                "quantity": "1",
                "price": "100",
            },
            symbol="AAPL",
            side="BUY",
            quantity="1",
            order_type="LIMIT",
            limit_price="100",
            expected_state="READY_TO_ENTER",
            expected_version=3,
            next_state="ENTRY_SUBMITTING",
            writer_id="writer-b",
            writer_fence=2,
            send_by=takeover_at + timedelta(seconds=5),
            now=takeover_at,
        )
        assert reservation == {"inserted": False, "intent": None, "run": None}

        reconciling = store.cas_intraday_run(
            plan_id="intraday-plan-1",
            expected_state="READY_TO_ENTER",
            expected_version=3,
            next_state="RECONCILING",
            writer_id="writer-b",
            writer_fence=2,
            event_type="takeover_reconcile",
            now=takeover_at,
        )
        assert reconciling is not None
        assert store.cas_intraday_run(
            plan_id="intraday-plan-1",
            expected_state="RECONCILING",
            expected_version=4,
            next_state="READY_TO_ENTER",
            writer_id="writer-b",
            writer_fence=2,
            event_type="ready_with_old_approval",
            now=takeover_at,
        ) is None


def test_action_reservation_and_cancel_ack_require_exact_intent_state_and_root_id() -> None:
    at = datetime(2026, 8, 28, 12, 30, tzinfo=timezone.utc)
    with SQLiteStateStore() as store:
        _create_intraday_run(store, created_at=at)
        store.claim_intraday_writer(
            plan_id="intraday-plan-1", writer_id="writer-a", now=at
        )
        _advance_intraday_run_to_ready(store, at=at)
        store.mark_intraday_broker_synced(
            plan_id="intraday-plan-1", writer_id="writer-a", writer_fence=1, now=at
        )
        store.reserve_intraday_order_intent(
            plan_id="intraday-plan-1",
            account_key="acct-hash",
            intent_id="entry-intent-1",
            idempotency_key="entry_20260828_1",
            order_role="ENTRY",
            method="POST",
            path="/api/v1/orders",
            body={
                "clientOrderId": "entry_20260828_1",
                "symbol": "AAPL",
                "side": "BUY",
                "orderType": "LIMIT",
                "quantity": "1",
                "price": "100",
            },
            symbol="AAPL",
            side="BUY",
            quantity="1",
            order_type="LIMIT",
            limit_price="100",
            expected_state="READY_TO_ENTER",
            expected_version=3,
            next_state="ENTRY_SUBMITTING",
            writer_id="writer-a",
            writer_fence=1,
            send_by=at + timedelta(seconds=5),
            now=at + timedelta(seconds=1),
        )
        working = store.complete_intraday_order_action(
            plan_id="intraday-plan-1",
            intent_id="entry-intent-1",
            expected_state="ENTRY_SUBMITTING",
            expected_version=4,
            next_state="ENTRY_WORKING",
            writer_id="writer-a",
            writer_fence=1,
            event_type="entry_acknowledged",
            status="ACKNOWLEDGED",
            broker_order_id="broker-entry-1",
            remaining_quantity="1",
            now=at + timedelta(seconds=2),
        )
        assert working is not None
        store.mark_intraday_broker_synced(
            plan_id="intraday-plan-1",
            writer_id="writer-a",
            writer_fence=1,
            now=at + timedelta(seconds=2),
        )

        with pytest.raises(ValueError, match="does not match"):
            store.reserve_intraday_action_event(
                plan_id="intraday-plan-1",
                intent_id="entry-intent-1",
                event_type="conditional_cancel_send_reserved",
                expected_state="ENTRY_WORKING",
                expected_version=5,
                next_state="ENTRY_CANCELING",
                writer_id="writer-a",
                writer_fence=1,
                now=at + timedelta(seconds=3),
            )
        with pytest.raises(ValueError, match="does not match"):
            store.reserve_intraday_action_event(
                plan_id="intraday-plan-1",
                intent_id="entry-intent-1",
                event_type="entry_cancel_send_reserved",
                expected_state="ENTRY_WORKING",
                expected_version=5,
                next_state="ENTRY_WORKING",
                writer_id="writer-a",
                writer_fence=1,
                now=at + timedelta(seconds=3),
            )
        assert not store.append_intraday_observation_event(
            plan_id="intraday-plan-1",
            intent_id="entry-intent-1",
            event_type="entry_cancel_acknowledged",
            status="PENDING_CANCEL",
            writer_id="writer-a",
            writer_fence=1,
            payload={"root_order_id": "broker-entry-1"},
            now=at + timedelta(seconds=3),
        )

        reserved = store.reserve_intraday_action_event(
            plan_id="intraday-plan-1",
            intent_id="entry-intent-1",
            event_type="entry_cancel_send_reserved",
            expected_state="ENTRY_WORKING",
            expected_version=5,
            next_state="ENTRY_CANCELING",
            writer_id="writer-a",
            writer_fence=1,
            payload={"request_hash": "d" * 64},
            now=at + timedelta(seconds=3),
        )
        assert reserved is not None
        assert not store.append_intraday_observation_event(
            plan_id="intraday-plan-1",
            intent_id="entry-intent-1",
            event_type="entry_cancel_acknowledged",
            status="PENDING_CANCEL",
            writer_id="writer-a",
            writer_fence=1,
            payload={"root_order_id": "forged-broker-id"},
            now=at + timedelta(seconds=4),
        )
        forged = [
            event
            for event in store.list_execution_events(intent_id="entry-intent-1")
            if event["event_type"] == "entry_cancel_acknowledged"
        ]

    assert forged == []
