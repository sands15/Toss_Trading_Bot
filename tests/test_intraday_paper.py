from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from turtle_bot.intraday_paper import (
    IntradayPaperConfig,
    IntradayPaperStore,
    PaperSimulationBlocked,
    PaperSimulationError,
    PaperStreamInstanceInactive,
    assert_simulation_topology,
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


def _plan(
    config: IntradayPaperConfig,
    *,
    session: date = SESSION,
    symbol: str = "AAPL",
) -> dict[str, object]:
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
        "symbol": symbol,
        "quantity": 10,
        "available_cash": format(config.initial_cash_usd, "f"),
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
        "symbol": symbol,
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


@pytest.mark.parametrize("table_name", ["intraday_runs", "news_articles", "unrelated"])
@pytest.mark.parametrize("entrypoint", ["store", "topology"])
def test_paper_entrypoints_reject_foreign_schema_before_mutation(
    tmp_path: Path,
    table_name: str,
    entrypoint: str,
) -> None:
    path = tmp_path / f"{entrypoint}-{table_name}.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(f"CREATE TABLE {table_name} (id TEXT PRIMARY KEY)")
    original_bytes = path.read_bytes()

    with pytest.raises(PaperSimulationError, match="not paper-owned"):
        if entrypoint == "store":
            IntradayPaperStore(path, _config())
        else:
            assert_simulation_topology(path, simulation_id="foreign", lanes=1)

    assert path.read_bytes() == original_bytes
    with sqlite3.connect(path) as connection:
        assert {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        } == {table_name}


@pytest.mark.parametrize("entrypoint", ["store", "topology"])
def test_paper_entrypoints_reject_foreign_key_orphans_before_mutation(
    tmp_path: Path,
    entrypoint: str,
) -> None:
    path = tmp_path / f"{entrypoint}-orphan.sqlite3"
    config = _config(run_id=f"orphan-{entrypoint}")
    with IntradayPaperStore(path, config) as store:
        store.ensure_plan(_plan(config), registered_at=OPEN)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "DELETE FROM paper_runs WHERE run_id = ?",
            (config.run_id,),
        )
        connection.execute(
            """
            INSERT INTO paper_simulation_topology_runs (
                run_id, simulation_id, lane
            ) VALUES ('orphan-topology-run', 'missing-simulation', 'SINGLE')
            """
        )
        connection.execute(
            """
            INSERT INTO market_frames (
                run_id, plan_id, event_kind, event_hash,
                event_at, frame_json, accepted_at
            ) VALUES (
                'missing-run', 'missing-plan', 'trade', 'orphan-frame',
                ?, '{}', ?
            )
            """,
            (OPEN.isoformat(), OPEN.isoformat()),
        )
        connection.execute(
            """
            INSERT INTO paper_cohorts (
                cohort_id, lane_a_run_id, lane_b_run_id,
                lane_a_initial_cash, lane_b_initial_cash,
                start_date, end_date,
                lane_a_config_hash, lane_b_config_hash, created_at
            ) VALUES (
                'orphan-cohort', 'missing-lane-a', 'missing-lane-b',
                '10000', '10000', ?, ?, 'hash-a', 'hash-b', ?
            )
            """,
            (
                SESSION.isoformat(),
                date(2026, 9, 30).isoformat(),
                OPEN.isoformat(),
            ),
        )
        violations_before = tuple(connection.execute("PRAGMA foreign_key_check"))
    original_bytes = path.read_bytes()
    assert {str(row[0]) for row in violations_before} >= {
        "paper_simulation_topology_runs",
        "paper_plans",
        "market_frames",
        "paper_cash_ledger",
        "paper_cohorts",
    }

    with pytest.raises(PaperSimulationError, match="foreign key check failed"):
        if entrypoint == "store":
            IntradayPaperStore(path, _config(run_id=f"new-{entrypoint}"))
        else:
            assert_simulation_topology(path, simulation_id="new", lanes=1)

    assert path.read_bytes() == original_bytes
    with sqlite3.connect(path) as connection:
        assert tuple(connection.execute("PRAGMA foreign_key_check")) == (
            violations_before
        )


def _forge_paper_table_sql(
    path: Path,
    *,
    table: str,
    original: str,
    replacement: str,
) -> None:
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        assert row is not None and isinstance(row[0], str)
        normalized = " ".join(row[0].split())
        assert original in normalized
        forged = normalized.replace(original, replacement, 1)
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = ?",
            (forged, table),
        )
        connection.execute("PRAGMA writable_schema = OFF")
        version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
        connection.execute(f"PRAGMA schema_version = {version + 1}")


@pytest.mark.parametrize("entrypoint", ["store", "topology"])
@pytest.mark.parametrize(
    ("table", "original", "replacement"),
    [
        (
            "paper_simulation_topologies",
            "lanes INTEGER NOT NULL CHECK (lanes IN (1, 2))",
            "lanes INTEGER NOT NULL",
        ),
        (
            "paper_cohort_sessions",
            "CHECK ( lane_a_status <> 'PLAN' OR lane_b_status <> 'PLAN' "
            "OR lane_a_symbol <> lane_b_symbol )",
            "CHECK (1)",
        ),
        (
            "paper_simulation_topologies",
            "simulation_id TEXT PRIMARY KEY",
            "simulation_id TEXT PRIMARY KEY ON CONFLICT REPLACE",
        ),
        (
            "market_frames",
            "frame_id INTEGER PRIMARY KEY AUTOINCREMENT",
            "frame_id INTEGER PRIMARY KEY",
        ),
    ],
    ids=("lanes-check", "cohort-symbol-check", "pk-conflict", "autoincrement"),
)
def test_paper_entrypoints_reject_forged_table_sql_before_mutation(
    tmp_path: Path,
    entrypoint: str,
    table: str,
    original: str,
    replacement: str,
) -> None:
    path = tmp_path / f"{entrypoint}-{table}.sqlite3"
    with IntradayPaperStore(path, _config(run_id=f"owner-{entrypoint}")):
        pass
    _forge_paper_table_sql(
        path,
        table=table,
        original=original,
        replacement=replacement,
    )
    original_bytes = path.read_bytes()

    with pytest.raises(PaperSimulationError, match="not paper-owned"):
        if entrypoint == "store":
            IntradayPaperStore(path, _config(run_id=f"new-{entrypoint}"))
        else:
            assert_simulation_topology(path, simulation_id="new", lanes=1)

    assert path.read_bytes() == original_bytes


@pytest.mark.parametrize("entrypoint", ["store", "topology"])
def test_paper_entrypoints_reject_forged_index_xinfo_before_mutation(
    tmp_path: Path,
    entrypoint: str,
) -> None:
    path = tmp_path / f"{entrypoint}-index.sqlite3"
    with IntradayPaperStore(path, _config(run_id=f"owner-{entrypoint}")):
        pass
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX idx_paper_frames_plan_time")
        connection.execute(
            """
            CREATE INDEX idx_paper_frames_plan_time
            ON market_frames(run_id COLLATE NOCASE, plan_id, event_at)
            """
        )
    original_bytes = path.read_bytes()

    with pytest.raises(PaperSimulationError, match="not paper-owned"):
        if entrypoint == "store":
            IntradayPaperStore(path, _config(run_id=f"new-{entrypoint}"))
        else:
            assert_simulation_topology(path, simulation_id="new", lanes=1)

    assert path.read_bytes() == original_bytes


def test_paper_store_rejects_hardlink_without_mutating_target(tmp_path: Path) -> None:
    target = tmp_path / "planner.sqlite3"
    with sqlite3.connect(target) as connection:
        connection.execute("CREATE TABLE intraday_runs (id TEXT PRIMARY KEY)")
    original_bytes = target.read_bytes()
    alias = tmp_path / "intraday-paper.sqlite3"
    try:
        alias.hardlink_to(target)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")

    with pytest.raises(PaperSimulationError, match="path is not isolated"):
        IntradayPaperStore(alias, _config())

    assert target.read_bytes() == original_bytes
    assert alias.read_bytes() == original_bytes


def test_paper_store_rejects_symlink_without_mutating_target(tmp_path: Path) -> None:
    target = tmp_path / "news.sqlite3"
    with sqlite3.connect(target) as connection:
        connection.execute("CREATE TABLE news_articles (id TEXT PRIMARY KEY)")
    original_bytes = target.read_bytes()
    alias = tmp_path / "intraday-paper.sqlite3"
    try:
        alias.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symbolic links unavailable: {exc}")

    with pytest.raises(PaperSimulationError, match="path is not isolated"):
        IntradayPaperStore(alias, _config())
    assert target.read_bytes() == original_bytes


def test_paper_store_rejects_non_regular_path(tmp_path: Path) -> None:
    directory = tmp_path / "paper-directory"
    directory.mkdir()
    with pytest.raises(PaperSimulationError, match="path is not isolated"):
        IntradayPaperStore(directory, _config())


def test_empty_and_topology_only_paper_databases_are_compatible(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty.sqlite3"
    with sqlite3.connect(empty):
        pass
    with IntradayPaperStore(empty, _config(run_id="empty")) as store:
        assert store.current_cash() == Decimal("10000")

    topology_only = tmp_path / "topology-only.sqlite3"
    assert_simulation_topology(
        topology_only,
        simulation_id="topology-only",
        lanes=1,
    )
    with sqlite3.connect(topology_only) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert tables == {
        "paper_simulation_topologies",
        "paper_simulation_topology_runs",
    }
    with IntradayPaperStore(
        topology_only,
        _config(run_id="topology-only"),
    ) as store:
        assert store.current_cash() == Decimal("10000")


def test_legacy_paper_schema_migrates_and_reopens(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    config = _config(run_id="legacy")
    with IntradayPaperStore(path, config):
        pass
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TABLE paper_cohort_sessions")
        connection.execute("DROP TABLE paper_cohorts")
        connection.execute("DROP TABLE paper_simulation_topology_runs")
        connection.execute("DROP TABLE paper_simulation_topologies")
        connection.execute(
            "ALTER TABLE paper_stream_instances DROP COLUMN last_seen_at"
        )

    with IntradayPaperStore(path, config):
        pass
    with IntradayPaperStore(path, config) as reopened:
        assert reopened.current_cash() == Decimal("10000")
        stream_columns = {
            str(row[1])
            for row in reopened._conn.execute(
                "PRAGMA table_info(paper_stream_instances)"
            )
        }
        assert "last_seen_at" in stream_columns
        assert {
            str(row[0])
            for row in reopened._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        } >= {
            "paper_simulation_topologies",
            "paper_simulation_topology_runs",
            "paper_cohorts",
            "paper_cohort_sessions",
        }


def test_backup_quiescence_requires_terminal_plan_and_closed_stream(
    tmp_path: Path,
) -> None:
    _config_value, store, plan_id = _registered(tmp_path)
    try:
        assert store.session_is_quiescent_for_backup(SESSION) is False
        assert store.run_is_quiescent_for_backup() is False
        store.begin_stream_instance(plan_id, "backup-race", started_at=OPEN)
        store.record_data_gap(
            plan_id,
            "stream_coverage_incomplete",
            at=OPEN + timedelta(minutes=1),
        )
        assert store.load_plan(plan_id)["status"] == "INVALID"
        assert store.session_is_quiescent_for_backup(SESSION) is False
        assert store.run_is_quiescent_for_backup() is False

        store.end_stream_instance(
            "backup-race",
            ended_at=OPEN + timedelta(minutes=2),
            reason="context_inactive",
        )
        assert store.session_is_quiescent_for_backup(SESSION) is True
        assert store.run_is_quiescent_for_backup() is True
        assert store.session_is_quiescent_for_backup(
            SESSION + timedelta(days=1)
        ) is False
    finally:
        store.close()


def test_existing_simulation_id_cannot_switch_lane_topology(tmp_path: Path) -> None:
    one_lane_path = tmp_path / "one-lane.sqlite3"
    with IntradayPaperStore(
        one_lane_path,
        _config(run_id="immutable-topology"),
    ):
        pass
    assert_simulation_topology(
        one_lane_path, simulation_id="immutable-topology", lanes=1
    )
    with pytest.raises(PaperSimulationBlocked, match="different lane topology"):
        assert_simulation_topology(
            one_lane_path, simulation_id="immutable-topology", lanes=2
        )

    two_lane_path = tmp_path / "two-lane.sqlite3"
    lane_a = _config(run_id="immutable-topology-a")
    lane_b = _config(run_id="immutable-topology-b")
    with IntradayPaperStore(two_lane_path, lane_a) as store:
        store.ensure_two_lane_cohort(
            cohort_id="immutable-topology", lane_b_config=lane_b
        )
    assert_simulation_topology(
        two_lane_path, simulation_id="immutable-topology", lanes=2
    )
    with pytest.raises(PaperSimulationBlocked, match="different lane topology"):
        assert_simulation_topology(
            two_lane_path, simulation_id="immutable-topology", lanes=1
        )


def test_topology_atomically_owns_derived_run_names_without_banning_legacy_suffixes(
    tmp_path: Path,
) -> None:
    for index in range(4):
        path = tmp_path / f"race-{index}.sqlite3"

        def claim(simulation_id: str, lanes: int) -> str:
            try:
                assert_simulation_topology(
                    path, simulation_id=simulation_id, lanes=lanes
                )
            except PaperSimulationBlocked:
                return "blocked"
            return "claimed"

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(
                executor.map(
                    lambda args: claim(*args),
                    (("overlap", 2), ("overlap-a", 1)),
                )
            )
        assert sorted(results) == ["blocked", "claimed"]

    legacy = tmp_path / "legacy-suffix.sqlite3"
    with IntradayPaperStore(legacy, _config(run_id="legacy-a")):
        pass
    assert_simulation_topology(legacy, simulation_id="legacy-a", lanes=1)
    with pytest.raises(PaperSimulationBlocked, match="different lane topology"):
        assert_simulation_topology(legacy, simulation_id="legacy", lanes=2)


def test_two_lane_stores_can_open_concurrently_on_a_fresh_wal_database(
    tmp_path: Path,
) -> None:
    for index in range(12):
        path = tmp_path / f"fresh-cohort-{index}.sqlite3"
        assert_simulation_topology(path, simulation_id=f"fresh-{index}", lanes=2)
        configs = (
            _config(run_id=f"fresh-{index}-a"),
            _config(run_id=f"fresh-{index}-b"),
        )

        def open_store(config: IntradayPaperConfig) -> Decimal:
            with IntradayPaperStore(path, config) as store:
                return store.current_cash()

        with ThreadPoolExecutor(max_workers=2) as executor:
            balances = tuple(executor.map(open_store, configs))
        assert balances == (Decimal("10000"), Decimal("10000"))


def test_reaped_stream_cannot_flush_a_buffered_tail_after_backup_quiescence(
    tmp_path: Path,
) -> None:
    config, paused, plan_id = _registered(tmp_path)
    peer = IntradayPaperStore(tmp_path / "paper.sqlite3", config)
    instance_id = "paused-before-backup"
    try:
        paused.begin_stream_instance(plan_id, instance_id, started_at=OPEN)
        event_at = OPEN + timedelta(minutes=2)
        paused.queue_payload(
            plan_id,
            _stream(event_at),
            event_kind="trade",
            now=event_at,
            stream_instance_id=instance_id,
        )
        peer.record_data_gap(
            plan_id,
            "stream_coverage_incomplete",
            at=event_at,
        )
        assert peer.reap_stale_terminal_streams(
            at=event_at + timedelta(seconds=6)
        ) == 1
        assert peer.run_is_quiescent_for_backup() is True

        with pytest.raises(PaperStreamInstanceInactive):
            paused.flush_pending(stream_instance_id=instance_id)
        paused.discard_pending()
        assert peer.load_plan(plan_id)["journaled_frame_count"] == 0
    finally:
        paused.discard_pending()
        paused.close()
        peer.close()


def test_two_lane_cohort_has_separate_immutable_cash_accounts(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    lane_a = _config(
        run_id="cohort-A",
        end_date=SESSION,
        initial_cash_usd=Decimal("6000"),
    )
    lane_b = _config(
        run_id="cohort-B",
        end_date=SESSION,
        initial_cash_usd=Decimal("4000"),
    )
    with IntradayPaperStore(path, lane_a) as store:
        first = store.ensure_two_lane_cohort(
            cohort_id="cohort-20260831",
            lane_b_config=lane_b,
            created_at=OPEN,
        )
        repeated = store.ensure_two_lane_cohort(
            cohort_id="cohort-20260831",
            lane_b_config=lane_b,
            created_at=OPEN + timedelta(hours=1),
        )

        assert repeated == first
        assert first["lanes"]["A"]["run_id"] == "cohort-A"
        assert first["lanes"]["A"]["initial_cash_usd"] == "6000"
        assert first["lanes"]["B"]["run_id"] == "cohort-B"
        assert first["lanes"]["B"]["initial_cash_usd"] == "4000"

        different_b = _config(
            run_id="cohort-C",
            end_date=SESSION,
            initial_cash_usd=Decimal("4000"),
        )
        with pytest.raises(PaperSimulationError, match="different immutable config"):
            store.ensure_two_lane_cohort(
                cohort_id="cohort-20260831",
                lane_b_config=different_b,
            )

    with sqlite3.connect(path) as connection:
        runs = connection.execute(
            "SELECT run_id, initial_cash, current_cash FROM paper_runs ORDER BY run_id"
        ).fetchall()
        ledger = connection.execute(
            "SELECT run_id, amount FROM paper_cash_ledger ORDER BY run_id"
        ).fetchall()
        assert runs == [
            ("cohort-A", "6000", "6000"),
            ("cohort-B", "4000", "4000"),
        ]
        assert ledger == [("cohort-A", "6000"), ("cohort-B", "4000")]


def test_two_lane_cohort_session_is_atomic_idempotent_and_complete(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.sqlite3"
    lane_a = _config(run_id="cohort-A", end_date=SESSION)
    lane_b = _config(run_id="cohort-B", end_date=SESSION)
    with IntradayPaperStore(path, lane_a) as store:
        store.ensure_two_lane_cohort(
            cohort_id="cohort-20260831", lane_b_config=lane_b
        )
        first = store.record_cohort_session(
            cohort_id="cohort-20260831",
            session_date=SESSION,
            lane_a_status="NO_CANDIDATE",
            lane_b_status="NO_CANDIDATE",
            recorded_at=OPEN,
        )
        repeated = store.record_cohort_session(
            cohort_id="cohort-20260831",
            session_date=SESSION,
            lane_a_status="NO_CANDIDATE",
            lane_b_status="NO_CANDIDATE",
            recorded_at=OPEN + timedelta(hours=1),
        )

        assert repeated == first
        assert first["lanes"]["A"]["status"] == "NO_CANDIDATE"
        assert first["lanes"]["B"]["status"] == "NO_CANDIDATE"
        assert store.cohort_coverage("cohort-20260831") == {
            "cohort_id": "cohort-20260831",
            "status": "COMPLETE",
            "start_date": SESSION.isoformat(),
            "end_date_inclusive": SESSION.isoformat(),
            "expected": [SESSION.isoformat()],
            "covered": [SESSION.isoformat()],
            "missing": [],
            "expected_count": 1,
            "covered_count": 1,
            "missing_count": 0,
            "lanes": {
                "A": {
                    "run_id": "cohort-A",
                    "initial_cash_usd": "10000",
                    "covered": [SESSION.isoformat()],
                    "missing": [],
                },
                "B": {
                    "run_id": "cohort-B",
                    "initial_cash_usd": "10000",
                    "covered": [SESSION.isoformat()],
                    "missing": [],
                },
            },
        }
        with pytest.raises(PaperSimulationError, match="different immutable data"):
            store.record_cohort_session(
                cohort_id="cohort-20260831",
                session_date=SESSION,
                lane_a_status="PLAN",
                lane_a_plan_id="different-plan",
                lane_a_symbol="AAPL",
                lane_b_status="NO_CANDIDATE",
            )

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM paper_no_candidate_sessions"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM paper_cohort_sessions"
        ).fetchone()[0] == 1


def test_exhausted_lane_can_record_terminal_no_candidate_coverage(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.sqlite3"
    lane_a = _config(run_id="cash-split-a", end_date=SESSION)
    lane_b = _config(run_id="cash-split-b", end_date=SESSION)
    with IntradayPaperStore(path, lane_a) as store:
        store.ensure_two_lane_cohort(
            cohort_id="cash-split", lane_b_config=lane_b
        )
        with store._write():
            store._conn.execute(
                "UPDATE paper_runs SET current_cash = '0' WHERE run_id = ?",
                (lane_a.run_id,),
            )

        result = store.record_cohort_session(
            cohort_id="cash-split",
            session_date=SESSION,
            lane_a_status="NO_CANDIDATE",
            lane_b_status="NO_CANDIDATE",
            recorded_at=OPEN,
        )

        assert result["lanes"]["A"]["status"] == "NO_CANDIDATE"
        assert store.cohort_coverage("cash-split")["missing_count"] == 0


def test_two_lane_cohort_supports_plan_and_no_candidate(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    lane_a = _config(run_id="cohort-A", end_date=SESSION)
    lane_b = _config(run_id="cohort-B", end_date=SESSION)
    with IntradayPaperStore(path, lane_a) as store:
        store.ensure_two_lane_cohort(
            cohort_id="cohort-20260831", lane_b_config=lane_b
        )
        plan = _plan(lane_a, symbol="AAPL")
        store.ensure_plan(plan, registered_at=OPEN)
        result = store.record_cohort_session(
            cohort_id="cohort-20260831",
            session_date=SESSION,
            lane_a_status="PLAN",
            lane_a_plan_id=str(plan["plan_id"]),
            lane_a_symbol="AAPL",
            lane_b_status="NO_CANDIDATE",
            recorded_at=OPEN,
        )

        assert result["lanes"]["A"] == {
            "run_id": "cohort-A",
            "status": "PLAN",
            "plan_id": plan["plan_id"],
            "symbol": "AAPL",
        }
        assert result["lanes"]["B"]["status"] == "NO_CANDIDATE"

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            """
            SELECT run_id, session_date FROM paper_no_candidate_sessions
            """
        ).fetchall() == [("cohort-B", SESSION.isoformat())]


def test_two_lane_cohort_rolls_back_first_lane_when_second_conflicts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.sqlite3"
    lane_a = _config(run_id="cohort-A", end_date=SESSION)
    lane_b = _config(run_id="cohort-B", end_date=SESSION)
    with IntradayPaperStore(path, lane_a) as store:
        store.ensure_two_lane_cohort(
            cohort_id="cohort-20260831", lane_b_config=lane_b
        )
        with IntradayPaperStore(path, lane_b) as lane_b_store:
            lane_b_store.record_market_closed(SESSION, recorded_at=OPEN)

        with pytest.raises(PaperSimulationError, match="MARKET_CLOSED"):
            store.record_cohort_session(
                cohort_id="cohort-20260831",
                session_date=SESSION,
                lane_a_status="NO_CANDIDATE",
                lane_b_status="NO_CANDIDATE",
                recorded_at=OPEN,
            )

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            """
            SELECT COUNT(*) FROM paper_no_candidate_sessions
            WHERE run_id = 'cohort-A'
            """
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM paper_cohort_sessions"
        ).fetchone()[0] == 0


def test_two_lane_cohort_database_rejects_same_plan_symbol(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    lane_a = _config(run_id="cohort-A", end_date=SESSION)
    lane_b = _config(run_id="cohort-B", end_date=SESSION)
    with IntradayPaperStore(path, lane_a) as store:
        store.ensure_two_lane_cohort(
            cohort_id="cohort-20260831", lane_b_config=lane_b
        )

    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            connection.execute(
                """
                INSERT INTO paper_cohort_sessions (
                    cohort_id, session_date,
                    lane_a_status, lane_a_plan_id, lane_a_symbol,
                    lane_b_status, lane_b_plan_id, lane_b_symbol, recorded_at
                ) VALUES (?, ?, 'PLAN', 'plan-a', 'AAPL',
                    'PLAN', 'plan-b', 'AAPL', ?)
                """,
                ("cohort-20260831", SESSION.isoformat(), OPEN.isoformat()),
            )


def test_two_lane_cohort_partial_manifest_is_incomplete(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    lane_a = _config(run_id="cohort-A", end_date=SESSION)
    lane_b = _config(run_id="cohort-B", end_date=SESSION)
    with IntradayPaperStore(path, lane_a) as store:
        store.ensure_two_lane_cohort(
            cohort_id="cohort-20260831", lane_b_config=lane_b
        )
        store.record_no_candidate(SESSION, recorded_at=OPEN)
        store._conn.execute(
            """
            INSERT INTO paper_cohort_sessions (
                cohort_id, session_date, lane_a_status, recorded_at
            ) VALUES (?, ?, 'NO_CANDIDATE', ?)
            """,
            ("cohort-20260831", SESSION.isoformat(), OPEN.isoformat()),
        )

        coverage = store.cohort_coverage("cohort-20260831")

        assert coverage["status"] == "INCOMPLETE"
        assert coverage["covered"] == []
        assert coverage["missing"] == [SESSION.isoformat()]
        assert coverage["lanes"]["A"]["covered"] == [SESSION.isoformat()]
        assert coverage["lanes"]["B"]["missing"] == [SESSION.isoformat()]


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
            "no_candidate": [],
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


def test_no_candidate_is_immutable_coverage_but_zero_plan_run_is_incomplete(
    tmp_path: Path,
) -> None:
    config = _config(end_date=SESSION)
    with IntradayPaperStore(tmp_path / "paper.sqlite3", config) as store:
        first = store.record_no_candidate(SESSION, recorded_at=OPEN)
        repeated = store.record_no_candidate(
            SESSION,
            recorded_at=OPEN + timedelta(hours=1),
        )

        assert repeated == first
        assert store.daily_summary(SESSION) == first
        summary = store.month_summary(
            as_of=datetime(2026, 9, 1, tzinfo=UTC)
        )
        assert summary["status"] == "INCOMPLETE"
        assert summary["plan_count"] == 0
        assert summary["no_candidate_count"] == 1
        assert summary["coverage"]["no_candidate"] == [SESSION.isoformat()]
        assert summary["coverage"]["missing"] == []

        with pytest.raises(PaperSimulationBlocked, match="NO_CANDIDATE"):
            store.ensure_plan(_plan(config), registered_at=OPEN)
        with pytest.raises(PaperSimulationError, match="NO_CANDIDATE"):
            store.record_market_closed(SESSION, recorded_at=OPEN)


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
