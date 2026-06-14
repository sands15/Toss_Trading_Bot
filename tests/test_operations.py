from __future__ import annotations

import json
import plistlib
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from turtle_bot.cli import run
from turtle_bot.domain import Candle
from turtle_bot.operations import (
    LaunchdServiceConfig,
    build_dashboard_server,
    check_operations_config,
    operations_checks_payload,
    render_launchd_plist,
    run_paper_service,
    update_dashboard_settings,
    update_momentum_settings,
)
from turtle_bot.state_store import SQLiteStateStore
from turtle_bot.toss_client import TossHttpResponse


@dataclass
class RecordedRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    query: Mapping[str, Any] | None
    form_body: Mapping[str, Any] | None


class FakeTransport:
    def __init__(self, responses: list[TossHttpResponse]) -> None:
        self.responses = responses
        self.requests: list[RecordedRequest] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        query: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        form_body: Mapping[str, Any] | None = None,
    ) -> TossHttpResponse:
        self.requests.append(
            RecordedRequest(
                method=method,
                url=url,
                headers=dict(headers),
                query=dict(query) if query is not None else None,
                form_body=form_body,
            )
        )
        if not self.responses:
            raise AssertionError("no fake response queued")
        return self.responses.pop(0)


def _write_config(
    path: Path,
    *,
    live_enabled: bool = False,
    symbols: tuple[str, ...] = (),
    account_seq: str | None = None,
    universe_enabled: bool = False,
    universe_candidate_symbols: tuple[str, ...] = (),
    strategy_kind: str = "turtle",
    runtime_mode: str = "paper",
    client_id_env: str = "TOSS_CLIENT_ID",
    client_secret_env: str = "TOSS_CLIENT_SECRET",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    symbol_lines = ["  symbols:"]
    symbol_lines.extend(f"    - {symbol}" for symbol in symbols)
    universe_symbol_lines = ["  universe_candidate_symbols:"]
    universe_symbol_lines.extend(f"    - {symbol}" for symbol in universe_candidate_symbols)
    path.write_text(
        "\n".join(
            [
                "toss:",
                f"  live_enabled: {str(live_enabled).lower()}",
                f"  account_seq: {account_seq or ''}",
                "  base_url: https://example.test",
                f"  client_id_env: {client_id_env}",
                f"  client_secret_env: {client_secret_env}",
                "runtime:",
                f"  mode: {runtime_mode}",
                "  market: KR",
                "  timezone: Asia/Seoul",
                "  use_market_calendar: true",
                *symbol_lines,
                f"  universe_enabled: {str(universe_enabled).lower()}",
                *universe_symbol_lines,
                "  universe_min_price: 1000",
                "  universe_min_average_daily_value: 100000000",
                "  universe_min_completed_candles: 21",
                "  candle_count: 25",
                "  exclude_current_session: true",
                "strategy:",
                f"  kind: {strategy_kind}",
                "  minimum_tick: 1",
                "  n_method: turtle",
                "  momentum:",
                "    market_symbol: SPY",
                "    lookback_days: 5",
                "    skip_days: 1",
                "    trend_ma_days: 3",
                "    exit_ma_days: 2",
                "    max_positions: 1",
                "    accept_top_n: 1",
                "    target_position_pct: 0.10",
                "    min_price: 0",
                "    min_average_daily_value: 0",
                "    average_daily_value_days: 2",
                "    use_market_filter: true",
                "  risk:",
                "    risk_pct_per_unit: 0.005",
                "    stop_n: 2",
                "    pyramid_step_n: 0.5",
                "    max_units_per_symbol: 4",
                "    max_total_long_units: 12",
            ]
        ),
        encoding="utf-8",
    )


def _token_payload() -> dict[str, Any]:
    return {"access_token": "tok", "token_type": "Bearer", "expires_in": 3600}


def _api_candle(day: int) -> dict[str, Any]:
    candle = Candle(
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=day),
        symbol="TEST",
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("1000"),
    )
    return {
        "timestamp": candle.timestamp.isoformat(),
        "openPrice": str(candle.open),
        "highPrice": str(candle.high),
        "lowPrice": str(candle.low),
        "closePrice": str(candle.close),
        "volume": str(candle.volume),
        "currency": "KRW",
    }


def _api_trend_candle(day: int, *, close: Decimal, volume: str = "1000000") -> dict[str, Any]:
    candle = Candle(
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=day),
        symbol="TEST",
        open=close,
        high=close + Decimal("1"),
        low=close - Decimal("1"),
        close=close,
        volume=Decimal(volume),
    )
    return {
        "timestamp": candle.timestamp.isoformat(),
        "openPrice": str(candle.open),
        "highPrice": str(candle.high),
        "lowPrice": str(candle.low),
        "closePrice": str(candle.close),
        "volume": str(candle.volume),
        "currency": "USD",
    }


def test_render_launchd_plist_is_valid_paper_service_plist(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "local.yaml"
    state_db = tmp_path / "state" / "turtle.sqlite3"
    log_dir = tmp_path / "logs"
    service = LaunchdServiceConfig.default(
        repo_dir=tmp_path,
        config_path=config_path,
        state_db=state_db,
        log_dir=log_dir,
        python_executable=sys.executable,
        interval_seconds=30,
    )

    plist = plistlib.loads(render_launchd_plist(service).encode("utf-8"))

    assert plist["Label"] == "com.sands15.toss-turtle-bot"
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] == {"Crashed": True}
    assert plist["ProgramArguments"] == [
        str(Path(sys.executable).resolve()),
        "-m",
        "turtle_bot",
        "--config",
        str(config_path.resolve()),
        "--state-db",
        str(state_db.resolve()),
        "--log-dir",
        str(log_dir.resolve()),
        "--paper-service",
        "--interval-seconds",
        "30",
    ]
    assert plist["StandardOutPath"].endswith("turtle-paper.out.log")
    assert plist["StandardErrorPath"].endswith("turtle-paper.err.log")


def test_checked_in_launchd_template_is_valid_plist() -> None:
    template = Path("ops/launchd/com.sands15.toss-turtle-bot.plist")

    plist = plistlib.loads(template.read_bytes())

    assert plist["Label"] == "com.sands15.toss-turtle-bot"
    assert "--paper-service" in plist["ProgramArguments"]


def test_operations_check_blocks_live_enabled_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "local.yaml"
    state_db = tmp_path / "state" / "turtle.sqlite3"
    log_dir = tmp_path / "logs"
    _write_config(config_path, live_enabled=True)
    state_db.parent.mkdir()
    log_dir.mkdir()

    payload = operations_checks_payload(
        check_operations_config(
            config_path=config_path,
            state_db=state_db,
            log_dir=log_dir,
        )
    )

    assert payload["status"] == "blocked"
    assert any("live trading is enabled" in blocker for blocker in payload["blockers"])


def test_operations_check_validates_toss_readiness_with_custom_env_names(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "local.yaml"
    state_db = tmp_path / "state" / "turtle.sqlite3"
    log_dir = tmp_path / "logs"
    state_db.parent.mkdir()
    log_dir.mkdir()
    _write_config(
        config_path,
        symbols=("TEST",),
        account_seq="7",
        client_id_env="MY_TOSS_CLIENT_ID",
        client_secret_env="MY_TOSS_CLIENT_SECRET",
    )

    payload = operations_checks_payload(
        check_operations_config(
            config_path=config_path,
            state_db=state_db,
            log_dir=log_dir,
            env={"MY_TOSS_CLIENT_ID": "id"},
        )
    )

    assert payload["status"] == "blocked"
    assert "MY_TOSS_CLIENT_SECRET is not configured" in payload["blockers"]
    assert "runtime.symbols or runtime.universe_candidate_symbols is required" not in payload["blockers"]


def test_operations_check_treats_blank_account_seq_as_missing(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "local.yaml"
    state_db = tmp_path / "state" / "turtle.sqlite3"
    log_dir = tmp_path / "logs"
    state_db.parent.mkdir()
    log_dir.mkdir()
    _write_config(
        config_path,
        symbols=("TEST",),
        account_seq="",
        client_id_env="TOSS_CLIENT_ID",
        client_secret_env="TOSS_CLIENT_SECRET",
    )

    payload = operations_checks_payload(
        check_operations_config(
            config_path=config_path,
            state_db=state_db,
            log_dir=log_dir,
            env={"TOSS_CLIENT_ID": "id", "TOSS_CLIENT_SECRET": "secret"},
        )
    )

    assert "toss.account_seq is not configured" in payload["blockers"]
    assert payload["status"] == "blocked"


def test_paper_service_once_records_heartbeat_without_live_orders(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "local.yaml"
    state_db = tmp_path / "state" / "turtle.sqlite3"
    log_dir = tmp_path / "logs"
    _write_config(config_path)

    snapshot = run_paper_service(
        config_path=config_path,
        state_db=state_db,
        log_dir=log_dir,
        interval_seconds=5,
        once=True,
        sleep=lambda _: None,
    )

    store = SQLiteStateStore(state_db)
    events = store.list_runtime_events(limit=2)
    assert snapshot.mode == "paper"
    assert snapshot.ready is False
    assert snapshot.blockers == (
        "runtime.symbols or runtime.universe_candidate_symbols is required",
        "TOSS_CLIENT_ID is not configured",
        "TOSS_CLIENT_SECRET is not configured",
        "toss.account_seq is not configured",
    )
    assert [event["message"] for event in events] == [
        "paper_service_heartbeat",
        "paper_service_blocked",
    ]
    assert store.has_unresolved_client_order_id("anything") is False


def test_dashboard_server_reads_runtime_events_from_sqlite(tmp_path: Path) -> None:
    state_db = tmp_path / "state" / "turtle.sqlite3"
    with SQLiteStateStore(state_db) as store:
        store.record_runtime_event(
            "WARN",
            "paper_service_blocked",
            {
                "mode": "paper",
                "ready": False,
                "blockers": ["TOSS_CLIENT_ID is not configured"],
                "positions": {"count": 0, "items": []},
                "open_orders": {"count": 0, "items": []},
                "watchlist": {"count": 0, "items": []},
                "timestamp": "2026-06-12T01:00:00+00:00",
            },
        )

    server = build_dashboard_server(state_db=state_db)

    dashboard = server.payload_for_path("/dashboard")
    events = server.payload_for_path("/events")
    summary = server.payload_for_path("/events/summary")

    assert dashboard["status"]["mode"] == "paper"
    assert dashboard["status"]["ready"] is False
    assert dashboard["status"]["blockers"] == ["TOSS_CLIENT_ID is not configured"]
    assert dashboard["runtime_events"]["count"] == 1
    assert events["items"][0]["message"] == "paper_service_blocked"
    assert summary["blockers"] == ["TOSS_CLIENT_ID is not configured"]


def test_update_momentum_settings_writes_user_friendly_cash_reserve(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "local.yaml"
    _write_config(config_path, strategy_kind="momentum")

    saved = update_momentum_settings(
        config_path,
        {
            "momentum": {
                "cash_reserve_pct": 0.35,
                "target_position_pct": 0.12,
                "max_positions": 6,
                "accept_top_n": 3,
                "exit_ma_days": 80,
                "lookback_days": 140,
                "skip_days": 20,
                "trend_ma_days": 210,
            }
        },
    )

    momentum = saved["strategy"]["momentum"]
    assert momentum["cash_reserve_pct"] == 0.35
    assert momentum["max_exposure_pct"] == 0.65
    assert momentum["target_position_pct"] == 0.12
    assert momentum["max_positions"] == 6
    assert momentum["accept_top_n"] == 3
    assert momentum["exit_ma_days"] == 80
    assert momentum["lookback_days"] == 140
    assert momentum["skip_days"] == 20
    assert momentum["trend_ma_days"] == 210

    loaded = config_path.read_text(encoding="utf-8")
    assert "cash_reserve_pct: 0.35" in loaded


def test_update_momentum_settings_rejects_invalid_skip_window(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "local.yaml"
    _write_config(config_path, strategy_kind="momentum")

    try:
        update_momentum_settings(
            config_path,
            {
                "momentum": {
                    "cash_reserve_pct": 0.5,
                    "target_position_pct": 0.1,
                    "max_positions": 5,
                    "accept_top_n": 2,
                    "exit_ma_days": 75,
                    "lookback_days": 20,
                    "skip_days": 20,
                    "trend_ma_days": 200,
                }
            },
        )
    except ValueError as exc:
        assert "skip_days" in str(exc)
    else:
        raise AssertionError("invalid momentum settings unexpectedly saved")


def test_update_dashboard_settings_saves_toss_connection_without_secret_echo(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config" / "local.yaml"
    _write_config(config_path, strategy_kind="momentum")
    env: dict[str, str] = {}

    saved = update_dashboard_settings(
        config_path,
        {
            "toss": {
                "account_seq": "7",
                "client_id_env": "MY_TOSS_CLIENT_ID",
                "client_secret_env": "MY_TOSS_CLIENT_SECRET",
                "client_id": "client-id-value",
                "client_secret": "client-secret-value",
            }
        },
        env=env,
    )

    assert saved["toss"]["account_seq"] == "7"
    assert saved["toss"]["client_id_env"] == "MY_TOSS_CLIENT_ID"
    assert saved["toss"]["client_secret_env"] == "MY_TOSS_CLIENT_SECRET"
    assert env == {
        "MY_TOSS_CLIENT_ID": "client-id-value",
        "MY_TOSS_CLIENT_SECRET": "client-secret-value",
    }
    loaded = config_path.read_text(encoding="utf-8")
    assert "client-id-value" not in loaded
    assert "client-secret-value" not in loaded


def test_paper_service_with_toss_read_only_data_runs_paper_iteration(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config" / "local.yaml"
    state_db = tmp_path / "state" / "turtle.sqlite3"
    log_dir = tmp_path / "logs"
    _write_config(config_path, symbols=("TEST",), account_seq="7")
    transport = FakeTransport(
        [
            TossHttpResponse(200, {}, _token_payload()),
            TossHttpResponse(200, {}, {"isOpen": True}),
            TossHttpResponse(
                200,
                {},
                {"candles": [_api_candle(day) for day in range(21)]},
            ),
            TossHttpResponse(200, {}, {"items": []}),
            TossHttpResponse(200, {}, {"orders": [], "nextCursor": None}),
            TossHttpResponse(
                200,
                {},
                {"prices": [{"symbol": "TEST", "lastPrice": "105"}]},
            ),
        ]
    )

    snapshot = run_paper_service(
        config_path=config_path,
        state_db=state_db,
        log_dir=log_dir,
        once=True,
        env={"TOSS_CLIENT_ID": "id", "TOSS_CLIENT_SECRET": "secret"},
        transport=transport,
        now=lambda: datetime(2026, 2, 1, tzinfo=timezone.utc),
    )

    store = SQLiteStateStore(state_db)
    events = store.list_runtime_events(limit=5)
    assert snapshot.mode == "paper"
    assert snapshot.ready is True
    assert snapshot.open_orders[0]["symbol"] == "TEST"
    assert snapshot.watchlist[0]["symbol"] == "TEST"
    assert store.load_paper_position("TEST") is not None
    assert store.load_latest_watchlist(name="premarket").symbols() == ("TEST",)
    assert [request.method for request in transport.requests] == [
        "POST",
        "GET",
        "GET",
        "GET",
        "GET",
        "GET",
    ]
    assert [event["message"] for event in events[:4]] == [
        "paper_service_heartbeat",
        "paper_fill",
        "paper_order_intent",
        "paper_order_guard",
    ]


def test_paper_service_can_run_momentum_strategy_from_config(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config" / "local.yaml"
    state_db = tmp_path / "state" / "turtle.sqlite3"
    log_dir = tmp_path / "logs"
    _write_config(
        config_path,
        symbols=("AAA", "BBB"),
        account_seq="7",
        strategy_kind="momentum",
    )
    spy_candles = [
        _api_trend_candle(day, close=Decimal("100") + Decimal(day))
        for day in range(12)
    ]
    aaa_candles = [
        _api_trend_candle(day, close=Decimal("100") + Decimal(day * 4))
        for day in range(12)
    ]
    bbb_candles = [
        _api_trend_candle(day, close=Decimal("100") + Decimal(day * 2))
        for day in range(12)
    ]
    transport = FakeTransport(
        [
            TossHttpResponse(200, {}, _token_payload()),
            TossHttpResponse(200, {}, {"isOpen": True}),
            TossHttpResponse(200, {}, {"candles": aaa_candles}),
            TossHttpResponse(200, {}, {"candles": bbb_candles}),
            TossHttpResponse(200, {}, {"items": []}),
            TossHttpResponse(200, {}, {"orders": [], "nextCursor": None}),
            TossHttpResponse(200, {}, {"candles": spy_candles}),
            TossHttpResponse(200, {}, {"prices": [{"symbol": "SPY", "lastPrice": "112"}]}),
            TossHttpResponse(200, {}, {"prices": [{"symbol": "AAA", "lastPrice": "148"}]}),
            TossHttpResponse(200, {}, {"prices": [{"symbol": "BBB", "lastPrice": "124"}]}),
        ]
    )

    snapshot = run_paper_service(
        config_path=config_path,
        state_db=state_db,
        log_dir=log_dir,
        once=True,
        env={"TOSS_CLIENT_ID": "id", "TOSS_CLIENT_SECRET": "secret"},
        transport=transport,
        now=lambda: datetime(2026, 2, 1, tzinfo=timezone.utc),
    )

    store = SQLiteStateStore(state_db)
    events = store.list_runtime_events(limit=8)
    saved_position = store.load_paper_position("AAA")
    assert snapshot.ready is True
    assert snapshot.open_orders[0]["system"] == "MOMENTUM"
    assert snapshot.open_orders[0]["symbol"] == "AAA"
    assert saved_position is not None
    assert saved_position.system.value == "MOMENTUM"
    assert any(event["message"] == "momentum_runtime_ranked" for event in events)


def test_shadow_service_allows_read_only_broker_mismatches_for_validation(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config" / "local.yaml"
    state_db = tmp_path / "state" / "turtle.sqlite3"
    log_dir = tmp_path / "logs"
    _write_config(
        config_path,
        symbols=("AAA", "BBB"),
        account_seq="7",
        strategy_kind="momentum",
        runtime_mode="shadow",
    )
    spy_candles = [
        _api_trend_candle(day, close=Decimal("100") + Decimal(day))
        for day in range(12)
    ]
    aaa_candles = [
        _api_trend_candle(day, close=Decimal("100") + Decimal(day * 4))
        for day in range(12)
    ]
    bbb_candles = [
        _api_trend_candle(day, close=Decimal("100") + Decimal(day * 2))
        for day in range(12)
    ]
    transport = FakeTransport(
        [
            TossHttpResponse(200, {}, _token_payload()),
            TossHttpResponse(200, {}, {"isOpen": True}),
            TossHttpResponse(200, {}, {"candles": aaa_candles}),
            TossHttpResponse(200, {}, {"candles": bbb_candles}),
            TossHttpResponse(
                200,
                {},
                {"items": [{"symbol": "OTHER", "quantity": "1"}]},
            ),
            TossHttpResponse(200, {}, {"orders": [], "nextCursor": None}),
            TossHttpResponse(200, {}, {"candles": spy_candles}),
            TossHttpResponse(200, {}, {"prices": [{"symbol": "SPY", "lastPrice": "112"}]}),
            TossHttpResponse(200, {}, {"prices": [{"symbol": "AAA", "lastPrice": "148"}]}),
            TossHttpResponse(200, {}, {"prices": [{"symbol": "BBB", "lastPrice": "124"}]}),
        ]
    )

    snapshot = run_paper_service(
        config_path=config_path,
        state_db=state_db,
        log_dir=log_dir,
        once=True,
        env={"TOSS_CLIENT_ID": "id", "TOSS_CLIENT_SECRET": "secret"},
        transport=transport,
        now=lambda: datetime(2026, 2, 1, tzinfo=timezone.utc),
    )

    store = SQLiteStateStore(state_db)
    messages = [event["message"] for event in store.list_runtime_events(limit=10)]
    saved_position = store.load_paper_position("AAA")
    assert snapshot.mode == "shadow"
    assert snapshot.ready is True
    assert snapshot.open_orders[0]["mode"] == "shadow"
    assert saved_position is not None
    assert "shadow_reconcile_warning" in messages
    assert "shadow_order_intent" in messages
    assert "shadow_service_heartbeat" in messages


def test_paper_service_uses_rule_based_universe_when_enabled(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config" / "local.yaml"
    state_db = tmp_path / "state" / "turtle.sqlite3"
    log_dir = tmp_path / "logs"
    _write_config(
        config_path,
        account_seq="7",
        universe_enabled=True,
        universe_candidate_symbols=("PASS", "LOW"),
    )
    pass_candles = [
        _api_candle(day)
        | {
            "symbol": "PASS",
            "openPrice": "50000",
            "highPrice": "50001",
            "lowPrice": "49999",
            "closePrice": "50000",
            "volume": "10000",
        }
        for day in range(21)
    ]
    low_candles = [
        _api_candle(day) | {"symbol": "LOW", "closePrice": "500", "volume": "1"}
        for day in range(21)
    ]
    transport = FakeTransport(
        [
            TossHttpResponse(200, {}, _token_payload()),
            TossHttpResponse(200, {}, {"isOpen": True}),
            TossHttpResponse(
                200,
                {},
                {
                    "stocks": [
                        {"symbol": "PASS", "market": "KR", "name": "Pass"},
                        {"symbol": "LOW", "market": "KR", "name": "Low"},
                    ]
                },
            ),
            TossHttpResponse(200, {}, {"warnings": []}),
            TossHttpResponse(200, {}, {"candles": pass_candles}),
            TossHttpResponse(200, {}, {"warnings": []}),
            TossHttpResponse(200, {}, {"candles": low_candles}),
            TossHttpResponse(200, {}, {"items": []}),
            TossHttpResponse(200, {}, {"orders": [], "nextCursor": None}),
            TossHttpResponse(
                200,
                {},
                {"prices": [{"symbol": "PASS", "lastPrice": "60000"}]},
            ),
        ]
    )

    snapshot = run_paper_service(
        config_path=config_path,
        state_db=state_db,
        log_dir=log_dir,
        once=True,
        env={"TOSS_CLIENT_ID": "id", "TOSS_CLIENT_SECRET": "secret"},
        transport=transport,
        now=lambda: datetime(2026, 2, 1, tzinfo=timezone.utc),
    )

    store = SQLiteStateStore(state_db)
    events = store.list_runtime_events(limit=8)
    universe_event = next(event for event in events if event["message"] == "universe_generated")
    assert snapshot.ready is True
    assert snapshot.watchlist[0]["symbol"] == "PASS"
    assert all(item["symbol"] != "LOW" for item in snapshot.watchlist)
    assert store.load_paper_position("PASS") is not None
    assert universe_event["payload"]["symbols"] == ["PASS"]
    assert any(
        decision["symbol"] == "LOW" and not decision["included"]
        for decision in universe_event["payload"]["decisions"]
    )


def test_paper_service_builds_watchlist_but_blocks_during_preopen(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config" / "local.yaml"
    state_db = tmp_path / "state" / "turtle.sqlite3"
    log_dir = tmp_path / "logs"
    _write_config(config_path, symbols=("TEST",), account_seq="7")
    transport = FakeTransport(
        [
            TossHttpResponse(200, {}, _token_payload()),
            TossHttpResponse(200, {}, {"status": "PREOPEN"}),
            TossHttpResponse(
                200,
                {},
                {"candles": [_api_candle(day) for day in range(21)]},
            ),
        ]
    )

    snapshot = run_paper_service(
        config_path=config_path,
        state_db=state_db,
        log_dir=log_dir,
        once=True,
        env={"TOSS_CLIENT_ID": "id", "TOSS_CLIENT_SECRET": "secret"},
        transport=transport,
        now=lambda: datetime(2026, 2, 1, tzinfo=timezone.utc),
    )

    store = SQLiteStateStore(state_db)
    events = store.list_runtime_events(limit=5)
    assert snapshot.ready is False
    assert snapshot.blockers == ("market_session_not_open:preopen",)
    assert snapshot.watchlist[0]["symbol"] == "TEST"
    assert store.load_paper_position("TEST") is None
    assert [request.url for request in transport.requests] == [
        "https://example.test/oauth2/token",
        "https://example.test/api/v1/market-calendar/KR",
        "https://example.test/api/v1/candles",
    ]
    assert [event["message"] for event in events[:4]] == [
        "paper_service_heartbeat",
        "paper_service_market_closed",
        "premarket_watchlist_generated",
        "market_session_state",
    ]


def test_paper_service_blocks_when_market_calendar_is_closed(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config" / "local.yaml"
    state_db = tmp_path / "state" / "turtle.sqlite3"
    log_dir = tmp_path / "logs"
    _write_config(config_path, symbols=("TEST",), account_seq="7")
    transport = FakeTransport(
        [
            TossHttpResponse(200, {}, _token_payload()),
            TossHttpResponse(200, {}, {"status": "HOLIDAY"}),
        ]
    )

    snapshot = run_paper_service(
        config_path=config_path,
        state_db=state_db,
        log_dir=log_dir,
        once=True,
        env={"TOSS_CLIENT_ID": "id", "TOSS_CLIENT_SECRET": "secret"},
        transport=transport,
        now=lambda: datetime(2026, 2, 1, tzinfo=timezone.utc),
    )

    store = SQLiteStateStore(state_db)
    events = store.list_runtime_events(limit=4)
    assert snapshot.ready is False
    assert snapshot.blockers == ("market_session_not_open:holiday",)
    assert [request.url for request in transport.requests] == [
        "https://example.test/oauth2/token",
        "https://example.test/api/v1/market-calendar/KR",
    ]
    assert [event["message"] for event in events[:3]] == [
        "paper_service_heartbeat",
        "paper_service_market_closed",
        "market_session_state",
    ]


def test_cli_writes_launchd_plist_and_runs_paper_service_once(
    tmp_path: Path,
    capsys,
) -> None:
    config_path = tmp_path / "config" / "local.yaml"
    state_db = tmp_path / "state" / "turtle.sqlite3"
    log_dir = tmp_path / "logs"
    plist_path = tmp_path / "LaunchAgents" / "bot.plist"
    _write_config(config_path)

    assert run(
        [
            "--config",
            str(config_path),
            "--state-db",
            str(state_db),
            "--log-dir",
            str(log_dir),
            "--ensure-runtime-dirs",
        ]
    ) == 0
    assert run(
        [
            "--config",
            str(config_path),
            "--repo-dir",
            str(tmp_path),
            "--python-executable",
            sys.executable,
            "--state-db",
            str(state_db),
            "--log-dir",
            str(log_dir),
            "--write-launchd-plist",
            str(plist_path),
        ]
    ) == 0
    capsys.readouterr()
    assert "--paper-service" in plistlib.loads(plist_path.read_bytes())[
        "ProgramArguments"
    ]

    assert run(
        [
            "--config",
            str(config_path),
            "--state-db",
            str(state_db),
            "--log-dir",
            str(log_dir),
            "--paper-service",
            "--once",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "paper"
    assert payload["ready"] is False
