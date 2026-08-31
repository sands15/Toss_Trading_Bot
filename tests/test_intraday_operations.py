from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

import turtle_bot.operations as operations
from turtle_approval import ApprovalConfig, load_envelope
from turtle_bot.config import intraday_simulation_experiment_hash, load_config
from turtle_bot.intraday_paper import IntradayPaperStore, assert_simulation_topology
from turtle_bot.operations import (
    IntradayPlanBlocked,
    _refresh_intraday_approval_envelope,
    _refresh_intraday_news_context,
    _strict_intraday_cash,
    run_paper_service,
)
from turtle_bot.notifier import DiscordTradeNotifier
from turtle_bot.state_store import SQLiteStateStore
from turtle_bot.toss_client import TossHttpResponse
from turtle_bot.toss_client import SimulationReadOnlyTossTransport


@dataclass
class RecordedRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    query: Mapping[str, Any] | None
    json_body: Mapping[str, Any] | None
    form_body: Mapping[str, Any] | None


class FakeTransport:
    def __init__(self, responses: list[TossHttpResponse | BaseException]) -> None:
        self.responses = list(responses)
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
                json_body=dict(json_body) if json_body is not None else None,
                form_body=dict(form_body) if form_body is not None else None,
            )
        )
        if not self.responses:
            raise AssertionError("no fake response queued")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


NOW = datetime(2026, 8, 28, 12, 30, tzinfo=timezone.utc)


def _write_intraday_config(
    path: Path,
    *,
    runtime_mode: str = "shadow",
    live_enabled: bool = False,
    live_execution_enabled: bool = False,
    base_url: str = "https://example.test",
    news_context_path: Path | None = None,
    approval_envelope_path: Path | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""toss:
  live_enabled: {str(live_enabled).lower()}
  account_alias: test-account
  account_seq: raw-account-7
  base_url: {base_url}
  client_id_env: TOSS_CLIENT_ID
  client_secret_env: TOSS_CLIENT_SECRET
runtime:
  mode: {runtime_mode}
  market: US
  timezone: America/New_York
  use_market_calendar: true
  symbols: [AAPL]
  interval_seconds: 60
  watchlist_enabled: false
  universe_enabled: false
strategy:
  kind: intraday
  intraday:
    cash_allocation_fraction: 0.50
    risk_fraction: 0.01
    take_profit_fraction: 0.02
    stop_fraction: 0.01
    stop_limit_buffer_fraction: 0.001
    max_entry_slippage_fraction: 0.001
    estimated_round_trip_cost_fraction: 0.0021
    estimated_fixed_round_trip_cost: 0.01
    minimum_reward_risk_ratio: 1.2
    max_spread_fraction: 0.003
    max_last_mid_deviation_fraction: 0.005
    max_notional: 500
    max_quantity: 1
    plan_lead_minutes: 90
    minimum_plan_lead_minutes: 15
    quote_max_age_seconds: 15
    orderbook_max_age_seconds: 15
    max_quote_skew_seconds: 2
    entry_start_minutes_after_open: 5
    entry_expiry_minutes_after_open: 60
    force_exit_minutes_before_close: 15
    regular_session_only: true
    live_execution_enabled: {str(live_execution_enabled).lower()}
    news_context_path: {json.dumps(str(news_context_path)) if news_context_path else "null"}
    approval_envelope_path: {json.dumps(str(approval_envelope_path)) if approval_envelope_path else "null"}
""",
        encoding="utf-8",
    )


def _token() -> TossHttpResponse:
    return TossHttpResponse(
        200,
        {},
        {"access_token": "tok", "token_type": "Bearer", "expires_in": 3600},
    )


def test_shadow_service_hard_lock_rejects_all_unsafe_flags_before_db_or_transport(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    state_db = tmp_path / "state" / "shadow.sqlite3"
    log_dir = tmp_path / "logs"
    _write_intraday_config(
        config_path,
        runtime_mode="live",
        live_enabled=True,
        live_execution_enabled=True,
        base_url="https://evil.test",
    )
    text = config_path.read_text(encoding="utf-8").replace(
        "kind: intraday", "kind: turtle"
    )
    config_path.write_text(
        text + "\nlive:\n  emergency_stop: false\n  allowed_symbols: [AAPL]\n",
        encoding="utf-8",
    )
    transport = FakeTransport([])

    with pytest.raises(RuntimeError) as exc:
        run_paper_service(
            config_path=config_path,
            state_db=state_db,
            log_dir=log_dir,
            once=True,
            expected_mode="shadow",
            env={"TOSS_CLIENT_ID": "id", "TOSS_CLIENT_SECRET": "secret"},
            transport=transport,
        )

    message = str(exc.value)
    assert "strategy.kind must be intraday" in message
    assert "runtime.mode must be shadow" in message
    assert "toss.live_enabled must be false" in message
    assert "live_execution_enabled must be false" in message
    assert "live.emergency_stop must be true" in message
    assert "live.allowed_symbols must be empty" in message
    assert "toss.base_url must be https://openapi.tossinvest.com" in message
    assert transport.requests == []
    assert not state_db.exists()
    assert not log_dir.exists()


def test_service_rejects_invalid_effective_interval_before_io(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_intraday_config(config_path)
    transport = FakeTransport([])

    for invalid in (True, 0, -1, 1.5):
        with pytest.raises(ValueError, match="positive integer"):
            run_paper_service(
                config_path=config_path,
                state_db=tmp_path / "state.sqlite3",
                log_dir=tmp_path / "logs",
                interval_seconds=invalid,
                once=True,
                transport=transport,
            )

    assert transport.requests == []


def test_simulation_rejects_same_planner_and_paper_path_before_creation(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "simulation.yaml"
    shared_db = (tmp_path / "intraday-paper.sqlite3").resolve()
    _write_intraday_config(
        config_path,
        news_context_path=(tmp_path / "news-context.json").resolve(),
    )
    _enable_one_day_simulation(config_path, shared_db)
    transport = FakeTransport([])

    with pytest.raises(RuntimeError, match="ledger paths must be distinct"):
        run_paper_service(
            config_path=config_path,
            state_db=shared_db,
            log_dir=tmp_path / "logs",
            once=True,
            expected_mode="shadow",
            env={"TOSS_CLIENT_ID": "id", "TOSS_CLIENT_SECRET": "secret"},
            transport=transport,
        )

    assert not shared_db.exists()
    assert transport.requests == []


def test_simulation_rejects_hardlinked_ledgers_without_mutation(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "simulation.yaml"
    planner_db = (tmp_path / "intraday.sqlite3").resolve()
    paper_db = (tmp_path / "intraday-paper.sqlite3").resolve()
    with sqlite3.connect(planner_db) as connection:
        connection.execute("CREATE TABLE foreign_ledger(value TEXT)")
    try:
        paper_db.hardlink_to(planner_db)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")
    before = planner_db.read_bytes()
    _write_intraday_config(
        config_path,
        news_context_path=(tmp_path / "news-context.json").resolve(),
    )
    _enable_one_day_simulation(config_path, paper_db)
    transport = FakeTransport([])

    with pytest.raises(RuntimeError, match="isolated regular file"):
        run_paper_service(
            config_path=config_path,
            state_db=planner_db,
            log_dir=tmp_path / "logs",
            once=True,
            expected_mode="shadow",
            env={"TOSS_CLIENT_ID": "id", "TOSS_CLIENT_SECRET": "secret"},
            transport=transport,
        )

    assert planner_db.read_bytes() == before
    assert paper_db.read_bytes() == before
    with sqlite3.connect(planner_db) as connection:
        assert {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        } == {"foreign_ledger"}
    assert transport.requests == []


def test_service_reuses_one_oauth_token_across_unchanged_iterations(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class StopLoop(Exception):
        pass

    config_path = tmp_path / "config.yaml"
    _write_intraday_config(
        config_path,
        base_url="https://openapi.tossinvest.com",
    )
    transport = FakeTransport(
        [
            _token(),
            TossHttpResponse(200, {}, {"result": {}}),
            TossHttpResponse(200, {}, {"result": {}}),
        ]
    )
    clients = []

    def fake_iteration(**kwargs):
        clients.append(kwargs["client"])
        kwargs["client"].get_market_calendar("US", date="2026-08-28")
        return operations.HealthSnapshot(mode="shadow")

    def stop_after_second_iteration(_seconds: float) -> None:
        if len(clients) == 2:
            raise StopLoop

    monkeypatch.setattr(operations, "_paper_service_iteration", fake_iteration)

    with pytest.raises(StopLoop):
        run_paper_service(
            config_path=config_path,
            state_db=tmp_path / "state.sqlite3",
            log_dir=tmp_path / "logs",
            expected_mode="shadow",
            env={"TOSS_CLIENT_ID": "id", "TOSS_CLIENT_SECRET": "secret"},
            transport=transport,
            sleep=stop_after_second_iteration,
            now=lambda: NOW,
        )

    assert clients[0] is clients[1]
    assert (
        sum(request.url.endswith("/oauth2/token") for request in transport.requests) == 1
    )
    assert sum(request.method == "GET" for request in transport.requests) == 2


def test_shadow_service_revalidates_hot_swapped_config_before_next_iteration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    state_db = tmp_path / "state" / "shadow.sqlite3"
    _write_intraday_config(
        config_path,
        base_url="https://openapi.tossinvest.com",
    )
    iteration_count = 0

    def fake_iteration(**_kwargs):
        nonlocal iteration_count
        iteration_count += 1
        _kwargs["client"].get_market_calendar("US", date="2026-08-28")
        return operations.HealthSnapshot(mode="shadow")

    def swap_config(_seconds: float) -> None:
        text = config_path.read_text(encoding="utf-8")
        config_path.write_text(
            text.replace("live_enabled: false", "live_enabled: true"),
            encoding="utf-8",
        )

    monkeypatch.setattr(operations, "_paper_service_iteration", fake_iteration)
    transport = FakeTransport(
        [_token(), TossHttpResponse(200, {}, {"result": {}})]
    )

    with pytest.raises(RuntimeError, match="toss.live_enabled must be false"):
        run_paper_service(
            config_path=config_path,
            state_db=state_db,
            log_dir=tmp_path / "logs",
            expected_mode="shadow",
            env={"TOSS_CLIENT_ID": "id", "TOSS_CLIENT_SECRET": "secret"},
            transport=transport,
            sleep=swap_config,
        )

    assert iteration_count == 1
    assert (
        sum(request.url.endswith("/oauth2/token") for request in transport.requests) == 1
    )
    assert sum(request.method == "GET" for request in transport.requests) == 1
    with SQLiteStateStore(state_db) as store:
        assert [
            event["message"] for event in store.list_runtime_events(limit=10)
        ] == ["shadow_service_heartbeat", "shadow_service_started"]


@pytest.mark.parametrize(
    ("old_value", "new_value", "expected_message"),
    [
        ("      enabled: true", "      enabled: false", "simulation service hard-lock failed"),
        ("  account_seq: planner-account", "  account_seq: another-account", "planner account authority changed"),
    ],
)
def test_deployed_simulation_lock_revalidates_manifest_and_transport_each_loop(
    tmp_path: Path,
    monkeypatch,
    old_value: str,
    new_value: str,
    expected_message: str,
) -> None:
    config_path = tmp_path / "private" / "planner" / "intraday-simulation.yaml"
    state_db = (tmp_path / "private" / "intraday.sqlite3").resolve()
    paper_db = (tmp_path / "private" / "intraday-paper.sqlite3").resolve()
    context_path = (tmp_path / "private" / "news-context.json").resolve()
    config_path.parent.mkdir(parents=True)
    template = (
        Path(__file__).resolve().parents[1]
        / "config"
        / "intraday-simulation.example.yaml"
    ).read_text(encoding="utf-8")
    template = template.replace(
        "state_db: state/intraday.sqlite3",
        f"state_db: {json.dumps(str(state_db))}",
    ).replace(
        "db_path: ../state/intraday-paper.sqlite3",
        f"db_path: {json.dumps(str(paper_db))}",
    ).replace(
        "news_context_path: ../state/news-context.json",
        f"news_context_path: {json.dumps(str(context_path))}",
    )
    template = template.replace("  account_alias:\n", "  account_alias: planner\n", 1)
    template = template.replace(
        "  account_seq:\n", "  account_seq: planner-account\n", 1
    )
    config_path.write_text(template, encoding="utf-8")
    parsed = load_config(config_path)
    expected = {
        "run_id": "2026-09-forward-test",
        "start_date": "2026-08-31",
        "end_date": "2026-09-30",
        "paper_db": str(paper_db),
        "experiment_hash": intraday_simulation_experiment_hash(parsed),
    }
    iterations = 0

    def fake_iteration(**kwargs):
        nonlocal iterations
        iterations += 1
        assert isinstance(kwargs["transport"], SimulationReadOnlyTossTransport)
        return operations.HealthSnapshot(mode="shadow")

    def drift_locked_manifest(_seconds: float) -> None:
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(old_value, new_value),
            encoding="utf-8",
        )

    monkeypatch.setattr(operations, "_paper_service_iteration", fake_iteration)
    with pytest.raises(RuntimeError, match=expected_message):
        run_paper_service(
            config_path=config_path,
            state_db=state_db,
            log_dir=tmp_path / "private" / "logs",
            env={},
            transport=FakeTransport([]),
            expected_mode="shadow",
            expected_simulation=expected,
            expected_account_fingerprint=(
                operations._intraday_account_authority_fingerprint(parsed)
            ),
            sleep=drift_locked_manifest,
        )

    assert iterations == 1


def _calendar(*, holiday: bool = False, regular_close: str = "2026-08-29T05:00:00+09:00"):
    today: dict[str, Any] = {
        "date": "2026-08-28",
        "preMarket": None,
        "regularMarket": None,
    }
    if not holiday:
        today.update(
            {
                "preMarket": {
                    "startTime": "2026-08-28T17:00:00+09:00",
                    "endTime": "2026-08-28T22:30:00+09:00",
                },
                "regularMarket": {
                    "startTime": "2026-08-28T22:30:00+09:00",
                    "endTime": regular_close,
                },
            }
        )
    return TossHttpResponse(200, {}, {"result": {"today": today}})


def _successful_responses(
    *,
    price_timestamp: str = "2026-08-28T12:29:55+00:00",
    book_timestamp: str = "2026-08-28T12:29:56+00:00",
    buying_power: Mapping[str, Any] | None = None,
    bids: list[Mapping[str, Any]] | None = None,
    asks: list[Mapping[str, Any]] | None = None,
) -> list[TossHttpResponse]:
    return [
        _token(),
        _calendar(),
        TossHttpResponse(200, {}, {"result": {"items": []}}),
        TossHttpResponse(
            200,
            {},
            {"result": {"orders": [], "nextCursor": None, "hasNext": False}},
        ),
        TossHttpResponse(
            200,
            {},
            {
                "result": {
                    "conditionalOrders": [],
                    "nextCursor": None,
                    "hasNext": False,
                }
            },
        ),
        TossHttpResponse(
            200,
            {},
            {
                "result": dict(
                    buying_power
                    or {"currency": "USD", "cashBuyingPower": "1000"}
                )
            },
        ),
        TossHttpResponse(
            200,
            {},
            {
                "result": [
                    {
                        "marketCountry": "US",
                        "commissionRate": "0.001",
                        "startDate": None,
                        "endDate": None,
                    }
                ]
            },
        ),
        TossHttpResponse(
            200,
            {},
            {
                "result": [
                    {
                        "symbol": "AAPL",
                        "timestamp": price_timestamp,
                        "lastPrice": "100.00",
                        "currency": "USD",
                    }
                ]
            },
        ),
        TossHttpResponse(
            200,
            {},
            {
                "result": {
                    "timestamp": book_timestamp,
                    "currency": "USD",
                    "bids": (
                        [{"price": "99.98", "volume": "10"}]
                        if bids is None
                        else bids
                    ),
                    "asks": (
                        [{"price": "100.02", "volume": "10"}]
                        if asks is None
                        else asks
                    ),
                }
            },
        ),
    ]


def _write_automatic_intraday_config(
    path: Path,
    *,
    news_context_path: Path | None = None,
) -> None:
    if news_context_path is None:
        news_context_path = path.parent / "news-context.json"
    _write_intraday_config(path, news_context_path=news_context_path)
    raw = path.read_text(encoding="utf-8")
    raw = raw.replace("  symbols: [AAPL]\n", "  symbols: []\n", 1)
    raw = raw.replace(
        "  intraday:\n",
        """  intraday:
    selection:
      mode: automatic
      rank_max_age_seconds: 120
      min_price: 5
      min_trading_amount: 1000000
      min_change_fraction: 0.005
      max_change_fraction: 0.08
      min_average_daily_value: 50000000
      max_average_daily_range_fraction: 0.08
      max_premarket_range_fraction: 0.05
""",
        1,
    )
    path.write_text(raw, encoding="utf-8")


def _enable_one_day_simulation(
    path: Path, paper_db: Path, *, lanes: int = 1
) -> None:
    lanes_line = "      lanes: 2\n" if lanes == 2 else ""
    raw = path.read_text(encoding="utf-8").replace(
        "base_url: https://example.test",
        "base_url: https://openapi.tossinvest.com",
    ).replace(
        "    minimum_plan_lead_minutes: 15\n",
        "    minimum_plan_lead_minutes: 60\n",
    ).replace(
        "    approval_envelope_path: null\n",
        (
            "    approval_envelope_path: null\n"
            "    simulation:\n"
            "      enabled: true\n"
            "      id: no-candidate-test\n"
            "      start_date: 2026-08-28\n"
            "      end_date: 2026-08-28\n"
            "      initial_cash: 10000\n"
            f"{lanes_line}"
            "      slippage_fraction: 0.0005\n"
            f"      db_path: {json.dumps(str(paper_db.resolve()))}\n"
        ),
    )
    path.write_text(raw, encoding="utf-8")


def _candle(timestamp: str) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "openPrice": "100",
        "highPrice": "100.5",
        "lowPrice": "99.5",
        "closePrice": "100",
        "volume": "1000",
        "currency": "USD",
    }


def _automatic_successful_responses(
    *,
    ranked_at: str = "2026-08-28T12:29:50+00:00",
    warnings: list[Mapping[str, Any]] | None = None,
    price_timestamp: str = "2026-08-28T12:29:55+00:00",
    book_timestamp: str = "2026-08-28T12:29:56+00:00",
    final_last_price: str = "100",
    final_warnings: list[Mapping[str, Any]] | None = None,
) -> list[TossHttpResponse]:
    daily_candles = [
        {
            **_candle(
                (
                    datetime(2026, 8, 27, 4, tzinfo=timezone.utc)
                    - timedelta(days=index)
                ).isoformat()
            ),
            "volume": "1000000",
        }
        for index in range(20)
    ]
    premarket_candles = [
        _candle(timestamp)
        for timestamp in (
            "2026-08-28T08:00:00+00:00",
            "2026-08-28T12:27:00+00:00",
            "2026-08-28T12:28:00+00:00",
            "2026-08-28T12:29:00+00:00",
        )
    ]
    last_price = Decimal(final_last_price)
    book_offset = min(Decimal("0.02"), last_price * Decimal("0.0005"))
    final_market = _successful_responses(
        price_timestamp=price_timestamp,
        book_timestamp=book_timestamp,
        bids=[{"price": str(last_price - book_offset), "volume": "10"}],
        asks=[{"price": str(last_price + book_offset), "volume": "10"}],
    )[7:]
    final_market[0].payload["result"][0]["lastPrice"] = final_last_price
    return [
        *_successful_responses()[:7],
        TossHttpResponse(
            200,
            {},
            {
                "result": {
                    "rankedAt": ranked_at,
                    "rankings": [
                        {
                            "rank": 1,
                            "symbol": "AAPL",
                            "currency": "USD",
                            "price": {
                                "lastPrice": "100",
                                "basePrice": "98",
                                "changeRate": "0.02",
                            },
                            "tradingVolume": "200000",
                            "tradingAmount": "20000000",
                        }
                    ],
                }
            },
        ),
        TossHttpResponse(
            200,
            {},
            {
                "result": [
                    {
                        "symbol": "AAPL",
                        "securityType": "STOCK",
                        "isCommonShare": True,
                        "isinCode": "US0378331005",
                    }
                ]
            },
        ),
        TossHttpResponse(
            200,
            {},
            {
                "result": [
                    {
                        "symbol": "IBM",
                        "securityType": "STOCK",
                        "isCommonShare": True,
                        "isinCode": "US4592001014",
                    }
                ]
            },
        ),
        TossHttpResponse(
            200,
            {},
            {
                "result": [
                    {
                        "symbol": "XYZ",
                        "securityType": "STOCK",
                        "isCommonShare": True,
                        "isinCode": "US0000000001",
                    }
                ]
            },
        ),
        TossHttpResponse(
            200,
            {},
            {
                "result": [
                    {
                        "symbol": "AAPL",
                        "status": "ACTIVE",
                        "currency": "USD",
                        "securityType": "STOCK",
                        "isCommonShare": True,
                        "market": "NASDAQ",
                    }
                ]
            },
        ),
        TossHttpResponse(200, {}, {"result": list(warnings or [])}),
        TossHttpResponse(
            200,
            {},
            {"result": {"candles": daily_candles, "nextBefore": None}},
        ),
        TossHttpResponse(
            200,
            {},
            {"result": {"candles": premarket_candles, "nextBefore": None}},
        ),
        *_successful_responses()[2:6],
        *final_market,
        TossHttpResponse(200, {}, {"result": list(final_warnings or [])}),
        *_successful_responses()[2:5],
        *_successful_responses()[5:6],
    ]


def _automatic_two_candidate_responses(
    *,
    second_daily_incomplete: bool = False,
) -> list[TossHttpResponse]:
    first = _automatic_successful_responses()
    second = _automatic_successful_responses()
    first[7].payload["result"]["rankings"].append(
        {
            **first[7].payload["result"]["rankings"][0],
            "rank": 2,
            "symbol": "MSFT",
        }
    )
    first[9].payload["result"].append(
        {
            "symbol": "MSFT",
            "securityType": "STOCK",
            "isCommonShare": True,
            "isinCode": "US5949181045",
        }
    )
    first[11].payload["result"].append(
        {
            **first[11].payload["result"][0],
            "symbol": "MSFT",
            "isinCode": "US5949181045",
            "market": "NYSE",
        }
    )
    second[19].payload["result"][0]["symbol"] = "MSFT"
    if second_daily_incomplete:
        second[13].payload["result"]["candles"] = second[13].payload["result"][
            "candles"
        ][:-1]
    return [
        first[0],
        first[1],
        first[6],
        *first[7:12],
        first[12],
        first[13],
        first[14],
        first[19],
        first[20],
        second[12],
        second[13],
        second[14],
        second[19],
        second[20],
        first[21],
        second[21],
    ]


def _run(
    tmp_path: Path,
    transport: FakeTransport,
    *,
    at: datetime = NOW,
    config_name: str = "intraday.yaml",
    env: Mapping[str, str] | None = None,
    clock=None,
    interval_seconds: int = 60,
):
    config_path = tmp_path / config_name
    if not config_path.exists():
        _write_intraday_config(config_path)
    state_db = tmp_path / "state.sqlite"
    snapshot = run_paper_service(
        config_path=config_path,
        state_db=state_db,
        log_dir=tmp_path / "logs",
        interval_seconds=interval_seconds,
        once=True,
        env={
            "TOSS_CLIENT_ID": "id",
            "TOSS_CLIENT_SECRET": "secret",
            **dict(env or {}),
        },
        transport=transport,
        now=clock or (lambda: at),
    )
    return snapshot, state_db


def test_intraday_service_creates_one_cash_based_shadow_plan_with_gets_only(tmp_path) -> None:
    transport = FakeTransport(_successful_responses())

    snapshot, state_db = _run(tmp_path, transport)

    assert snapshot.ready is True
    assert snapshot.mode == "shadow"
    assert all(
        request.method == "GET" or request.url.endswith("/oauth2/token")
        for request in transport.requests
    )
    assert not any(
        request.method != "GET" and "/orders" in request.url
        for request in transport.requests
    )
    with SQLiteStateStore(state_db) as store:
        plans = store.list_intraday_plans()
        events = store.list_runtime_events(limit=20)
        assert store.list_paper_positions() == []

    assert len(plans) == 1
    plan = plans[0]
    payload = plan["payload"]
    assert plan["account_key"].startswith("toss-")
    assert plan["account_key"] != "raw-account-7"
    assert "raw-account-7" not in json.dumps(payload)
    assert payload["mode"] == "shadow"
    assert payload["live_order_submission"] is False
    assert payload["quantity"] == 1
    assert Decimal(payload["cash_reserved"]) <= Decimal(payload["allocated_cash"])
    assert Decimal(payload["planned_risk"]) <= Decimal(payload["risk_budget"])
    assert payload["market_snapshot"]["reference_price"] == "100.02"
    assert payload["commission_snapshot"]["broker_commission_fraction"] == "0.001"
    assert [event["message"] for event in events].count(
        "intraday_shadow_plan_created"
    ) == 1


def test_intraday_automatic_selection_locks_one_read_only_plan(
    tmp_path, monkeypatch
) -> None:
    config_path = tmp_path / "intraday.yaml"
    context_path = tmp_path / "news-context.json"
    _write_automatic_intraday_config(
        config_path,
        news_context_path=context_path,
    )
    monkeypatch.setattr(operations.time, "sleep", lambda _seconds: None)
    transport = FakeTransport(_automatic_successful_responses())

    snapshot, state_db = _run(tmp_path, transport)

    assert snapshot.ready is True
    assert all(
        request.method == "GET" or request.url.endswith("/oauth2/token")
        for request in transport.requests
    )
    assert all(request.json_body is None for request in transport.requests)
    selector_requests = transport.requests[7:]
    assert [
        (request.url.removeprefix("https://example.test"), request.query)
        for request in selector_requests
    ] == [
        (
            "/api/v1/rankings",
            {
                "type": "MARKET_TRADING_AMOUNT",
                "marketCountry": "US",
                "duration": "realtime",
                "excludeInvestmentCaution": True,
                "count": 20,
            },
        ),
        (
            "/api/v1/stocks/all",
            {
                "market": "NASDAQ",
                "status": "ACTIVE",
                "securityType": "STOCK",
                "commonShare": True,
            },
        ),
        (
            "/api/v1/stocks/all",
            {
                "market": "NYSE",
                "status": "ACTIVE",
                "securityType": "STOCK",
                "commonShare": True,
            },
        ),
        (
            "/api/v1/stocks/all",
            {
                "market": "AMEX",
                "status": "ACTIVE",
                "securityType": "STOCK",
                "commonShare": True,
            },
        ),
        ("/api/v1/stocks", {"symbols": "AAPL"}),
        ("/api/v1/stocks/AAPL/warnings", None),
        (
            "/api/v1/candles",
            {
                "symbol": "AAPL",
                "interval": "1d",
                "count": 21,
                "adjusted": False,
                "before": "2026-08-28T22:30:00+09:00",
            },
        ),
        (
            "/api/v1/candles",
            {
                "symbol": "AAPL",
                "interval": "1m",
                "count": 200,
                "adjusted": False,
                "before": "2026-08-28T12:30:00+00:00",
            },
        ),
        ("/api/v1/holdings", None),
        ("/api/v1/orders", {"status": "OPEN", "limit": 100}),
        ("/api/v1/conditional-orders", {"status": "OPEN", "limit": 100}),
        ("/api/v1/buying-power", {"currency": "USD"}),
        ("/api/v1/prices", {"symbols": "AAPL"}),
        ("/api/v1/orderbook", {"symbol": "AAPL"}),
        ("/api/v1/stocks/AAPL/warnings", None),
        ("/api/v1/holdings", None),
        ("/api/v1/orders", {"status": "OPEN", "limit": 100}),
        ("/api/v1/conditional-orders", {"status": "OPEN", "limit": 100}),
        ("/api/v1/buying-power", {"currency": "USD"}),
    ]
    assert all(request.method == "GET" for request in selector_requests)
    assert all(request.form_body is None for request in selector_requests)
    account_rechecks = (*selector_requests[8:12], *selector_requests[15:19])
    assert all(
        "X-Tossinvest-Account" in request.headers for request in account_rechecks
    )
    assert all(
        "X-Tossinvest-Account" not in request.headers
        for request in (*selector_requests[:8], *selector_requests[12:15])
    )

    with SQLiteStateStore(state_db) as store:
        plans = store.list_intraday_plans()
    assert len(plans) == 1
    payload = plans[0]["payload"]
    assert payload["symbol"] == "AAPL"
    assert payload["live_order_submission"] is False
    assert payload["llm_influence"] is False
    assert payload["selection_snapshot"] == {
        "mode": "automatic",
        "source": "MARKET_TRADING_AMOUNT:US:realtime",
        "ranked_at": "2026-08-28T12:29:50+00:00",
        "rank": 1,
        "symbol": "AAPL",
        "ranking_last_price": "100",
        "ranking_base_price": "98",
        "ranking_change_fraction": "0.02",
        "final_change_fraction": "0.02040816326530612244897959184",
        "ranking_trading_volume": "200000",
        "ranking_trading_amount": "20000000",
        "market": "NASDAQ",
        "warnings_clear": True,
        "warnings_checked_at": "2026-08-28T12:30:00+00:00",
        "account_checked_at": "2026-08-28T12:30:00+00:00",
        "candles_adjusted": False,
        "openapi_version": "1.2.14",
        "openapi_sha256": (
            "a7b32ba754401d13fa649ba91eebd212420eb1afab28e9c2c0d6ea8d43055fed"
        ),
        "completed_daily_candles": 20,
        "average_daily_value": "100000000",
        "average_daily_range_fraction": "0.01",
        "latest_completed_daily_candle": "2026-08-27T04:00:00+00:00",
        "completed_premarket_candles": 4,
        "premarket_volume": "4000",
        "premarket_range_fraction": "0.01",
        "latest_completed_premarket_candle": "2026-08-28T12:29:00+00:00",
        "news_or_llm_influence": False,
    }
    assert json.loads(context_path.read_text(encoding="utf-8"))["symbol"] == "AAPL"


def test_intraday_simulation_sizes_from_virtual_cash_and_blocks_personal_reads(
    tmp_path, monkeypatch
) -> None:
    config_path = tmp_path / "simulation.yaml"
    paper_db = tmp_path / "intraday-paper.sqlite3"
    context_path = tmp_path / "news-context.json"
    _write_automatic_intraday_config(
        config_path, news_context_path=context_path
    )
    raw = config_path.read_text(encoding="utf-8").replace(
        "base_url: https://example.test",
        "base_url: https://openapi.tossinvest.com",
    )
    raw = raw.replace(
        "    approval_envelope_path: null\n",
        (
            "    approval_envelope_path: null\n"
            "    simulation:\n"
            "      enabled: true\n"
            "      id: august-forward-test\n"
            "      start_date: 2026-08-01\n"
            "      end_date: 2026-08-31\n"
            "      initial_cash: 10000\n"
            "      slippage_fraction: 0.0005\n"
            f"      db_path: {json.dumps(str(paper_db))}\n"
        ),
    )
    config_path.write_text(raw, encoding="utf-8")
    monkeypatch.setattr(operations.time, "sleep", lambda _seconds: None)
    responses = _automatic_successful_responses()
    transport = FakeTransport(
        [responses[0], responses[1], responses[6], *responses[7:15], *responses[19:22]]
    )
    state_db = tmp_path / "intraday.sqlite3"

    snapshot = run_paper_service(
        config_path=config_path,
        state_db=state_db,
        log_dir=tmp_path / "logs",
        once=True,
        expected_mode="shadow",
        env={"TOSS_CLIENT_ID": "id", "TOSS_CLIENT_SECRET": "secret"},
        transport=transport,
        now=lambda: NOW,
    )

    assert snapshot.ready is True
    paths = [
        request.url.removeprefix("https://openapi.tossinvest.com")
        for request in transport.requests
    ]
    assert not any(
        path in {
            "/api/v1/accounts",
            "/api/v1/holdings",
            "/api/v1/orders",
            "/api/v1/conditional-orders",
            "/api/v1/buying-power",
        }
        for path in paths
    )
    assert "/api/v1/commissions" in paths
    with SQLiteStateStore(state_db) as store:
        plan = store.list_intraday_plans()[0]
    assert plan["account_key"].startswith("simulation-")
    assert plan["payload"]["available_cash"] == "10000"
    assert plan["payload"]["cash_snapshot"]["source"] == "virtual_usd_ledger"
    expectation = json.loads(
        (tmp_path / "stream-expectation.json").read_text(encoding="utf-8")
    )
    assert expectation == {
        "schema_version": 1,
        "session_date": "2026-08-28",
        "expected_from": plan["created_at"].isoformat(),
        "expected_until": "2026-08-28T20:00:00+00:00",
        "reason": "intraday_paper_stream",
    }
    config = load_config(config_path)
    with IntradayPaperStore(
        paper_db,
        operations._intraday_paper_config(config),
    ) as paper:
        assert paper.daily_summary(date(2026, 8, 28))["status"] == "WAITING_ENTRY"
        assert paper.current_cash() == Decimal("10000")


def test_two_lane_simulation_locks_distinct_plans_and_publishes_cohort_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "two-lane.yaml"
    paper_db = tmp_path / "intraday-paper.sqlite3"
    context_path = tmp_path / "news-context.json"
    _write_automatic_intraday_config(config_path, news_context_path=context_path)
    _enable_one_day_simulation(config_path, paper_db, lanes=2)
    monkeypatch.setattr(operations.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        operations,
        "_refresh_intraday_approval_envelope",
        lambda **_kwargs: pytest.fail("two-lane simulation reached approval code"),
    )
    transport = FakeTransport(_automatic_two_candidate_responses())

    snapshot, state_db = _run(
        tmp_path,
        transport,
        config_name="two-lane.yaml",
    )

    assert snapshot.ready is True
    assert sum(request.url.endswith("/api/v1/rankings") for request in transport.requests) == 1
    assert all(
        request.method == "GET" or request.url.endswith("/oauth2/token")
        for request in transport.requests
    )
    assert all(request.json_body is None for request in transport.requests)
    config = load_config(config_path)
    paper_configs = dict(operations._intraday_paper_configs(config))
    with SQLiteStateStore(state_db) as planner:
        cohort = planner.load_intraday_cohort(
            cohort_id="no-candidate-test",
            session_date=date(2026, 8, 28),
        )
        assert cohort is not None
        assert [
            cohort["lanes"][lane]["plan"]["symbol"] for lane in ("A", "B")
        ] == ["AAPL", "MSFT"]
        assert len(planner.list_intraday_plans()) == 2
        for lane in ("A", "B"):
            simulation = cohort["lanes"][lane]["plan"]["payload"]["guardrails"][
                "simulation"
            ]
            assert simulation["lanes"] == 2
            assert simulation["lane_initial_cash"] == "5000"
            assert simulation["cash_split"] == "50:50"
            assert simulation["inter_lane_transfer_allowed"] is False

    paper_stores = {
        lane: IntradayPaperStore(paper_db, paper_configs[lane])
        for lane in ("A", "B")
    }
    try:
        assert [paper_stores[lane].current_cash() for lane in ("A", "B")] == [
            Decimal("5000"),
            Decimal("5000"),
        ]
        assert [
            paper_stores[lane].daily_summary(date(2026, 8, 28))["symbol"]
            for lane in ("A", "B")
        ] == ["AAPL", "MSFT"]
        coverage = paper_stores["A"].cohort_coverage("no-candidate-test")
        assert coverage["status"] == "COMPLETE"
        received: list[tuple[dict[str, Any], dict[str, Any]]] = []
        operations._publish_intraday_paper_status(
            config=config,
            snapshot=snapshot,
            sink=lambda month, **metadata: received.append((month, metadata)),
        )
        month, metadata = received[0]
        assert month["initial_cash"] == "10000"
        assert month["current_cash"] == "10000"
        assert month["distinct_trading_session_count"] == 1
        assert month["cohort_coverage"]["covered"] == ["2026-08-28"]
        assert month["sessions"][0]["distinct_symbols"] is True
        assert set(month["lanes"]) == {"A", "B"}
        assert metadata["latest_day"] is None
    finally:
        for paper_store in paper_stores.values():
            paper_store.close()
    assert json.loads(context_path.read_text(encoding="utf-8")) == {
        "schema_version": 2,
        "generated_at": NOW.isoformat(),
        "market": "US",
        "session_date": "2026-08-28",
        "active_until": "2026-08-29T05:00:00+09:00",
        "symbols": ["AAPL", "MSFT"],
        "reason": "intraday_plan",
    }
    maintenance_calls: list[date] = []
    monkeypatch.setattr(
        operations,
        "_run_intraday_post_close_maintenance",
        lambda **kwargs: maintenance_calls.append(kwargs["session_date"]),
    )
    late, _ = _run(
        tmp_path,
        FakeTransport([_token(), _calendar()]),
        at=datetime(2026, 8, 28, 20, 0, 16, tzinfo=timezone.utc),
        config_name="two-lane.yaml",
    )
    assert late.ready is True
    assert maintenance_calls == [date(2026, 8, 28)]


def test_two_lane_exhausted_lane_does_not_block_the_funded_lane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "two-lane.yaml"
    paper_db = tmp_path / "intraday-paper.sqlite3"
    _write_automatic_intraday_config(config_path)
    _enable_one_day_simulation(config_path, paper_db, lanes=2)
    config = load_config(config_path)
    paper_configs = dict(operations._intraday_paper_configs(config))
    assert_simulation_topology(
        paper_db,
        simulation_id=config.intraday.simulation_id,
        lanes=2,
    )
    with IntradayPaperStore(paper_db, paper_configs["A"]) as lane_a:
        lane_a.ensure_two_lane_cohort(
            cohort_id=config.intraday.simulation_id,
            lane_b_config=paper_configs["B"],
        )
        with lane_a._write():
            lane_a._conn.execute(
                "UPDATE paper_runs SET current_cash = '0' WHERE run_id = ?",
                (paper_configs["A"].run_id,),
            )
    monkeypatch.setattr(operations.time, "sleep", lambda _seconds: None)

    snapshot, state_db = _run(
        tmp_path,
        FakeTransport(_automatic_two_candidate_responses()),
        config_name="two-lane.yaml",
    )

    assert snapshot.ready is True
    with SQLiteStateStore(state_db) as planner:
        cohort = planner.load_intraday_cohort(
            cohort_id="no-candidate-test",
            session_date=date(2026, 8, 28),
        )
        assert cohort is not None
        assert cohort["lanes"]["A"] == {
            "status": "NO_CANDIDATE",
            "plan": None,
        }
        assert cohort["lanes"]["B"]["status"] == "PLAN"
        assert cohort["lanes"]["B"]["plan"]["symbol"] == "AAPL"
    with IntradayPaperStore(paper_db, paper_configs["A"]) as lane_a:
        assert lane_a.daily_summary(date(2026, 8, 28))["status"] == "NO_CANDIDATE"
        assert lane_a.cohort_coverage("no-candidate-test")["status"] == "COMPLETE"
    with IntradayPaperStore(paper_db, paper_configs["B"]) as lane_b:
        assert lane_b.daily_summary(date(2026, 8, 28))["status"] == "WAITING_ENTRY"


def test_two_lane_candidate_shortage_waits_then_locks_plan_and_no_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "two-lane.yaml"
    paper_db = tmp_path / "intraday-paper.sqlite3"
    _write_automatic_intraday_config(config_path)
    _enable_one_day_simulation(config_path, paper_db, lanes=2)
    monkeypatch.setattr(operations.time, "sleep", lambda _seconds: None)
    early_responses = _automatic_successful_responses(
        ranked_at="2026-08-28T12:28:50+00:00",
        price_timestamp="2026-08-28T12:28:55+00:00",
        book_timestamp="2026-08-28T12:28:56+00:00",
    )
    early_responses[14].payload["result"]["candles"] = [
        _candle(f"2026-08-28T12:{minute}:00+00:00")
        for minute in ("25", "26", "27")
    ]
    early, state_db = _run(
        tmp_path,
        FakeTransport(
            [
                early_responses[0],
                early_responses[1],
                early_responses[6],
                *early_responses[7:15],
                *early_responses[19:22],
            ]
        ),
        at=NOW - timedelta(minutes=1, seconds=1),
        config_name="two-lane.yaml",
    )

    assert early.blockers == ("intraday_no_eligible_candidate",)
    with SQLiteStateStore(state_db) as planner:
        assert planner.load_intraday_cohort(
            cohort_id="no-candidate-test", session_date=date(2026, 8, 28)
        ) is None

    final_responses = _automatic_successful_responses()
    final, _ = _run(
        tmp_path,
        FakeTransport(
            [
                final_responses[0],
                final_responses[1],
                final_responses[6],
                final_responses[7],
                *final_responses[11:15],
                *final_responses[19:22],
            ]
        ),
        config_name="two-lane.yaml",
    )
    assert final.ready is True
    with SQLiteStateStore(state_db) as planner:
        cohort = planner.load_intraday_cohort(
            cohort_id="no-candidate-test", session_date=date(2026, 8, 28)
        )
        assert cohort is not None
        assert cohort["lanes"]["A"]["status"] == "PLAN"
        assert cohort["lanes"]["B"] == {"status": "NO_CANDIDATE", "plan": None}

    config = load_config(config_path)
    paper_configs = dict(operations._intraday_paper_configs(config))
    with IntradayPaperStore(paper_db, paper_configs["A"]) as lane_a:
        assert lane_a.daily_summary(date(2026, 8, 28))["status"] == "WAITING_ENTRY"
        assert lane_a.cohort_coverage("no-candidate-test")["status"] == "COMPLETE"
    with IntradayPaperStore(paper_db, paper_configs["B"]) as lane_b:
        assert lane_b.daily_summary(date(2026, 8, 28))["status"] == "NO_CANDIDATE"
    received: list[dict[str, Any]] = []
    operations._publish_intraday_paper_status(
        config=config,
        snapshot=final,
        sink=lambda month, **_metadata: received.append(month),
    )
    assert received[0]["coverage_covered"] == 1
    assert received[0]["no_candidate_sessions"] == 1
    assert received[0]["distinct_trading_session_count"] == 1
    assert received[0]["lanes"]["B"]["no_candidate_sessions"] == 1
    assert received[0]["drawdown_policy"] == "conservative_sum_of_lane_maxima"
    from turtle_runtime.paper_status import PaperStatusWriter

    writer = PaperStatusWriter(
        (tmp_path / "status" / "paper-status.json").resolve(),
        release_sha="a" * 40,
        boot_id_hash="b" * 64,
        clock=lambda: NOW,
    )
    operations._publish_intraday_paper_status(
        config=config,
        snapshot=final,
        sink=writer.write,
    )
    status_payload = json.loads(writer.path.read_text(encoding="ascii"))
    assert status_payload["no_candidate_count"] == 1
    assert status_payload["simulation_lanes"] == 2
    assert status_payload["distinct_trading_session_count"] == 1
    assert status_payload["lanes"]["B"]["no_candidate_count"] == 1
    context = json.loads((tmp_path / "news-context.json").read_text(encoding="utf-8"))
    assert context["schema_version"] == 2
    assert context["symbols"] == ["AAPL"]

    restarted, _ = _run(
        tmp_path,
        FakeTransport([_token(), _calendar()]),
        config_name="two-lane.yaml",
    )
    assert restarted.ready is True


def test_two_lane_data_quality_error_never_creates_cohort_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "two-lane.yaml"
    paper_db = tmp_path / "intraday-paper.sqlite3"
    _write_automatic_intraday_config(config_path)
    _enable_one_day_simulation(config_path, paper_db, lanes=2)
    monkeypatch.setattr(operations.time, "sleep", lambda _seconds: None)

    snapshot, state_db = _run(
        tmp_path,
        FakeTransport(
            _automatic_two_candidate_responses(second_daily_incomplete=True)
        ),
        config_name="two-lane.yaml",
    )

    assert snapshot.blockers == ("intraday_daily_candles_incomplete",)
    with SQLiteStateStore(state_db) as planner:
        assert planner.load_intraday_cohort(
            cohort_id="no-candidate-test", session_date=date(2026, 8, 28)
        ) is None
    config = load_config(config_path)
    paper_configs = dict(operations._intraday_paper_configs(config))
    with IntradayPaperStore(paper_db, paper_configs["A"]) as lane_a:
        coverage = lane_a.cohort_coverage("no-candidate-test")
        assert coverage["covered"] == []
        assert coverage["missing"] == ["2026-08-28"]


def test_two_lane_earlier_data_error_blocks_two_later_valid_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "two-lane.yaml"
    paper_db = tmp_path / "intraday-paper.sqlite3"
    _write_automatic_intraday_config(config_path)
    _enable_one_day_simulation(config_path, paper_db, lanes=2)
    monkeypatch.setattr(operations.time, "sleep", lambda _seconds: None)

    first = _automatic_successful_responses()
    second = _automatic_successful_responses()
    third = _automatic_successful_responses()
    ranking = first[7].payload["result"]["rankings"]
    ranking.extend(
        [
            {**ranking[0], "rank": 2, "symbol": "MSFT"},
            {**ranking[0], "rank": 3, "symbol": "GOOG"},
        ]
    )
    first[9].payload["result"].extend(
        [
            {
                "symbol": "MSFT",
                "securityType": "STOCK",
                "isCommonShare": True,
                "isinCode": "US5949181045",
            },
            {
                "symbol": "GOOG",
                "securityType": "STOCK",
                "isCommonShare": True,
                "isinCode": "US02079K1079",
            },
        ]
    )
    first[11].payload["result"].extend(
        [
            {
                **first[11].payload["result"][0],
                "symbol": "MSFT",
                "isinCode": "US5949181045",
                "market": "NYSE",
            },
            {
                **first[11].payload["result"][0],
                "symbol": "GOOG",
                "isinCode": "US02079K1079",
                "market": "NYSE",
            },
        ]
    )
    first[13].payload["result"]["candles"] = first[13].payload["result"][
        "candles"
    ][:-1]
    second[19].payload["result"][0]["symbol"] = "MSFT"
    third[19].payload["result"][0]["symbol"] = "GOOG"

    snapshot, state_db = _run(
        tmp_path,
        FakeTransport(
            [
                first[0],
                first[1],
                first[6],
                *first[7:14],
                second[12],
                second[13],
                second[14],
                second[19],
                second[20],
                third[12],
                third[13],
                third[14],
                third[19],
                third[20],
            ]
        ),
        config_name="two-lane.yaml",
    )

    assert snapshot.blockers == ("intraday_daily_candles_incomplete",)
    with SQLiteStateStore(state_db) as planner:
        assert planner.load_intraday_cohort(
            cohort_id="no-candidate-test",
            session_date=date(2026, 8, 28),
        ) is None


def test_two_lane_empty_ranking_counts_one_distinct_no_candidate_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "two-lane.yaml"
    paper_db = tmp_path / "intraday-paper.sqlite3"
    _write_automatic_intraday_config(config_path)
    _enable_one_day_simulation(config_path, paper_db, lanes=2)
    monkeypatch.setattr(operations.time, "sleep", lambda _seconds: None)
    responses = _automatic_successful_responses()
    responses[7].payload["result"]["rankings"] = []

    snapshot, state_db = _run(
        tmp_path,
        FakeTransport([responses[0], responses[1], responses[6], responses[7]]),
        config_name="two-lane.yaml",
    )

    assert snapshot.ready is True
    with SQLiteStateStore(state_db) as planner:
        cohort = planner.load_intraday_cohort(
            cohort_id="no-candidate-test", session_date=date(2026, 8, 28)
        )
        assert cohort is not None
        assert [cohort["lanes"][lane]["status"] for lane in ("A", "B")] == [
            "NO_CANDIDATE",
            "NO_CANDIDATE",
        ]
    config = load_config(config_path)
    received: list[dict[str, Any]] = []
    operations._publish_intraday_paper_status(
        config=config,
        snapshot=snapshot,
        sink=lambda month, **_metadata: received.append(month),
    )
    month = received[0]
    assert month["coverage_covered"] == 1
    assert month["no_candidate_sessions"] == 1
    assert month["distinct_trading_session_count"] == 0
    assert month["lanes"]["A"]["no_candidate_sessions"] == 1
    assert month["lanes"]["B"]["no_candidate_sessions"] == 1


def test_two_lane_drawdown_uses_conservative_sum_of_lane_maxima() -> None:
    coverage = {
        "status": "ACTIVE",
        "expected": [],
        "covered": [],
        "missing": [],
        "expected_count": 0,
        "covered_count": 0,
        "missing_count": 0,
    }

    def paper_store(*, drawdown: str, realized: str):
        summary = {
            "run_id": f"lane-{drawdown}",
            "status": "ACTIVE",
            "start_date": "2026-08-28",
            "end_date_inclusive": "2026-08-28",
            "initial_cash_usd": "5000",
            "current_cash_usd": str(Decimal("5000") + Decimal(realized)),
            "final_equity_usd": "5000",
            "realized_pnl_usd": realized,
            "clean_realized_pnl_usd": realized,
            "return_fraction": "0",
            "clean_return_fraction": str(Decimal(realized) / Decimal("5000")),
            "total_fees_usd": "0",
            "max_closed_equity_drawdown_usd": drawdown,
            "max_drawdown_fraction": str(Decimal(drawdown) / Decimal("5000")),
            "trade_count": 1,
            "wins": 0,
            "losses": 1,
            "no_entry_count": 0,
            "no_candidate_count": 0,
            "invalid_result_count": 0,
            "unresolved_position_count": 0,
            "waiting_plan_count": 0,
            "accepted_event_count": 0,
            "journaled_frame_count": 0,
            "data_gap_count": 0,
            "coverage": coverage,
        }
        return SimpleNamespace(
            config=SimpleNamespace(end_date=date(2026, 8, 28)),
            summary=lambda **_kwargs: summary,
            cohort_coverage=lambda _cohort_id: coverage,
        )

    month = operations._paper_cohort_public_payload(
        cohort_id="drawdown-test",
        paper_stores={
            "A": paper_store(drawdown="100", realized="-100"),
            "B": paper_store(drawdown="200", realized="-200"),
        },
        as_of=NOW,
    )

    assert month["max_drawdown"] == "300"
    assert month["max_drawdown_fraction"] == "0.03"
    assert month["drawdown_policy"] == "conservative_sum_of_lane_maxima"


def test_two_lane_market_holiday_is_atomically_covered(tmp_path: Path) -> None:
    config_path = tmp_path / "two-lane.yaml"
    paper_db = tmp_path / "intraday-paper.sqlite3"
    _write_automatic_intraday_config(config_path)
    _enable_one_day_simulation(config_path, paper_db, lanes=2)

    snapshot, state_db = _run(
        tmp_path,
        FakeTransport([_token(), _calendar(holiday=True)]),
        config_name="two-lane.yaml",
    )

    assert snapshot.blockers == ("intraday_market_holiday",)
    with SQLiteStateStore(state_db) as planner:
        cohort = planner.load_intraday_cohort(
            cohort_id="no-candidate-test", session_date=date(2026, 8, 28)
        )
        assert cohort is not None
        assert [cohort["lanes"][lane]["status"] for lane in ("A", "B")] == [
            "MARKET_CLOSED",
            "MARKET_CLOSED",
        ]
    config = load_config(config_path)
    paper_configs = dict(operations._intraday_paper_configs(config))
    with IntradayPaperStore(paper_db, paper_configs["A"]) as lane_a:
        assert lane_a.cohort_coverage("no-candidate-test")["covered"] == [
            "2026-08-28"
        ]
    with IntradayPaperStore(paper_db, paper_configs["B"]) as lane_b:
        assert lane_b.daily_summary(date(2026, 8, 28))["status"] == "MARKET_CLOSED"


def test_paper_status_sink_receives_only_public_month_and_latest_day(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary = {
        "run_id": "august-forward-test",
        "simulation_account_key": "private-simulation-account-key",
        "status": "WAITING",
        "start_date": "2026-08-01",
        "end_date_inclusive": "2026-08-31",
        "initial_cash_usd": "10000",
        "current_cash_usd": "10000",
        "realized_pnl_usd": "0",
        "trade_count": 0,
        "no_candidate_count": 1,
        "waiting_plan_count": 1,
        "coverage": {
            "expected_count": 21,
            "covered_count": 1,
            "missing_count": 20,
            "missing": ["2026-08-03"],
            "market_closed": [],
            "no_candidate": ["2026-08-29"],
            "covered": ["2026-08-29"],
        },
        "days": [
            {
                "run_id": "august-forward-test",
                "plan_id": "private-plan-id-old",
                "plan_hash": "a" * 64,
                "session_date": "2026-08-27",
                "symbol": "MSFT",
                "status": "NO_ENTRY",
            },
            {
                "run_id": "august-forward-test",
                "plan_id": "private-plan-id-latest",
                "plan_hash": "b" * 64,
                "session_date": "2026-08-28",
                "symbol": "AAPL",
                "status": "WAITING_ENTRY",
                "quantity": 1,
                "cash_before": "10000",
                "cash_after": None,
            },
        ],
    }
    closed: list[bool] = []

    class FakePaperStore:
        def __init__(self, _path: Path, _config: object) -> None:
            pass

        def summary(self, *, as_of: datetime) -> dict[str, Any]:
            assert as_of == NOW
            return summary

        def daily_summary(self, session_date: str) -> dict[str, Any]:
            assert session_date == "2026-08-29"
            return {
                "run_id": "august-forward-test",
                "session_date": session_date,
                "status": "NO_CANDIDATE",
                "recorded_at": NOW.isoformat(),
            }

        def close(self) -> None:
            closed.append(True)

    snapshot = operations.HealthSnapshot(
        mode="shadow",
        ready=False,
        blockers=("intraday_plan_window_not_started",),
        generated_at=NOW,
    )
    config = SimpleNamespace(
        strategy_kind="intraday",
        live_enabled=False,
        runtime=SimpleNamespace(mode="shadow"),
            intraday=SimpleNamespace(
                simulation_enabled=True,
                simulation_db_path=tmp_path / "intraday-paper.sqlite3",
                simulation_id="august-forward-test",
                simulation_lanes=1,
            ),
    )
    received: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def sink(month: dict[str, Any], **metadata: Any) -> None:
        received.append((month, metadata))

    monkeypatch.setattr(operations, "load_config", lambda _path: config)
    monkeypatch.setattr(
        operations,
        "_paper_service_iteration",
        lambda **_kwargs: snapshot,
    )
    monkeypatch.setattr(operations, "IntradayPaperStore", FakePaperStore)
    monkeypatch.setattr(operations, "_intraday_paper_config", lambda _config: object())

    result = run_paper_service(
        config_path=tmp_path / "simulation.yaml",
        state_db=tmp_path / "intraday.sqlite3",
        log_dir=tmp_path / "logs",
        once=True,
        paper_status_sink=sink,
    )

    assert result is snapshot
    assert closed == [True]
    assert len(received) == 1
    month, metadata = received[0]
    assert month == operations._paper_month_public_payload(summary)
    assert metadata == {
        "planner_ready": False,
        "blocker_codes": ("intraday_plan_window_not_started",),
        "latest_day": operations._paper_daily_public_payload(
            {
                "run_id": "august-forward-test",
                "session_date": "2026-08-29",
                "status": "NO_CANDIDATE",
            }
        ),
    }
    assert "simulation_account_key" not in month
    assert "days" not in month
    assert "plan_id" not in metadata["latest_day"]
    assert month["no_candidate_sessions"] == 1
    assert metadata["latest_day"]["symbol"] is None
    assert metadata["latest_day"]["data_gaps"] == 0
    serialized = json.dumps(received, sort_keys=True)
    assert "private-simulation-account-key" not in serialized
    assert "private-plan-id-old" not in serialized
    assert "private-plan-id-latest" not in serialized
    assert "a" * 64 not in serialized
    assert "b" * 64 not in serialized


def test_intraday_simulation_records_official_weekday_market_closure(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "simulation.yaml"
    paper_db = (tmp_path / "intraday-paper.sqlite3").resolve()
    _write_automatic_intraday_config(config_path)
    raw = config_path.read_text(encoding="utf-8").replace(
        "base_url: https://example.test",
        "base_url: https://openapi.tossinvest.com",
    ).replace(
        "    approval_envelope_path: null\n",
        (
            "    approval_envelope_path: null\n"
            "    simulation:\n"
            "      enabled: true\n"
            "      id: august-forward-test\n"
            "      start_date: 2026-08-01\n"
            "      end_date: 2026-08-31\n"
            "      initial_cash: 10000\n"
            "      slippage_fraction: 0.0005\n"
            f"      db_path: {json.dumps(str(paper_db))}\n"
        ),
    )
    config_path.write_text(raw, encoding="utf-8")

    snapshot = run_paper_service(
        config_path=config_path,
        state_db=tmp_path / "intraday.sqlite3",
        log_dir=tmp_path / "logs",
        once=True,
        expected_mode="shadow",
        env={"TOSS_CLIENT_ID": "id", "TOSS_CLIENT_SECRET": "secret"},
        transport=FakeTransport([_token(), _calendar(holiday=True)]),
        now=lambda: NOW,
    )

    assert snapshot.blockers == ("intraday_market_holiday",)
    config = load_config(config_path)
    with IntradayPaperStore(paper_db, operations._intraday_paper_config(config)) as paper:
        daily = paper.daily_summary(date(2026, 8, 28))
        assert daily["status"] == "MARKET_CLOSED"
        assert "2026-08-28" in paper.summary(as_of=NOW)["coverage"]["market_closed"]


def _paper_market_payload(
    plan: Mapping[str, Any],
    at: datetime,
    *,
    trade: Decimal,
    bid: Decimal,
    ask: Decimal,
) -> dict[str, Any]:
    quantity = int(plan["payload"]["quantity"])
    return {
        "schema_version": 1,
        "mode": "shadow",
        "live_order_submission": False,
        "ready_for_live_entry": False,
        "symbol": plan["symbol"],
        "session_date": plan["session_date"].isoformat(),
        "generation": 1,
        "shadow_usable": True,
        "valid_until": (at + timedelta(seconds=10)).isoformat(),
        "error_codes": [],
        "trade": {
            "price": str(trade),
            "volume": str(quantity),
            "currency": "USD",
            "broker_at": at.isoformat(),
            "received_at": at.isoformat(),
            "source": "websocket",
        },
        "orderbook": {
            "best_bid": str(bid),
            "best_bid_volume": str(quantity),
            "best_ask": str(ask),
            "best_ask_volume": str(quantity),
            "currency": "USD",
            "broker_at": at.isoformat(),
            "received_at": at.isoformat(),
            "timestamp_source": "broker",
            "source": "websocket",
        },
    }


def _close_paper_plan_at_target(
    paper: IntradayPaperStore,
    plan: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    payload = plan["payload"]
    entry_at = datetime.fromisoformat(payload["entry_start"]) + timedelta(seconds=1)
    entry_trigger = Decimal(payload["entry_trigger"])
    armed = paper.process_payload(
        plan["plan_id"],
        _paper_market_payload(
            plan,
            entry_at,
            trade=entry_trigger,
            bid=entry_trigger - Decimal("0.01"),
            ask=entry_trigger,
        ),
        event_kind="trade",
        now=entry_at,
    )
    filled_at = entry_at + timedelta(seconds=1)
    entered = paper.process_payload(
        plan["plan_id"],
        _paper_market_payload(
            plan,
            filled_at,
            trade=entry_trigger,
            bid=entry_trigger - Decimal("0.01"),
            ask=entry_trigger,
        ),
        event_kind="orderbook",
        now=filled_at,
    )
    target_at = filled_at + timedelta(minutes=1)
    target = Decimal(payload["target_trigger"])
    exit_bid = target + Decimal("1")
    exit_armed = paper.process_payload(
        plan["plan_id"],
        _paper_market_payload(
            plan,
            target_at,
            trade=target,
            bid=exit_bid,
            ask=exit_bid + Decimal("0.01"),
        ),
        event_kind="trade",
        now=target_at,
    )
    exited_at = target_at + timedelta(seconds=1)
    exited = paper.process_payload(
        plan["plan_id"],
        _paper_market_payload(
            plan,
            exited_at,
            trade=target,
            bid=exit_bid,
            ask=exit_bid + Decimal("0.01"),
        ),
        event_kind="orderbook",
        now=exited_at,
    )
    return armed, entered, exit_armed, exited


def test_intraday_simulation_forwards_fill_and_daily_reports_from_durable_outbox(
    tmp_path, monkeypatch
) -> None:
    config_path = tmp_path / "simulation.yaml"
    paper_db = tmp_path / "intraday-paper.sqlite3"
    _write_automatic_intraday_config(config_path)
    raw = config_path.read_text(encoding="utf-8").replace(
        "base_url: https://example.test",
        "base_url: https://openapi.tossinvest.com",
    )
    raw = raw.replace(
        "    approval_envelope_path: null\n",
        (
            "    approval_envelope_path: null\n"
            "    simulation:\n"
            "      enabled: true\n"
            "      id: august-forward-test\n"
            "      start_date: 2026-08-01\n"
            "      end_date: 2026-08-31\n"
            "      initial_cash: 10000\n"
            "      slippage_fraction: 0.0005\n"
            f"      db_path: {json.dumps(str(paper_db))}\n"
        ),
    )
    config_path.write_text(raw, encoding="utf-8")
    monkeypatch.setattr(operations.time, "sleep", lambda _seconds: None)
    responses = _automatic_successful_responses()
    transport = FakeTransport(
        [responses[0], responses[1], responses[6], *responses[7:15], *responses[19:22]]
    )
    state_db = tmp_path / "intraday.sqlite3"
    run_paper_service(
        config_path=config_path,
        state_db=state_db,
        log_dir=tmp_path / "logs",
        once=True,
        expected_mode="shadow",
        env={"TOSS_CLIENT_ID": "id", "TOSS_CLIENT_SECRET": "secret"},
        transport=transport,
        now=lambda: NOW,
    )
    config = load_config(config_path)

    with SQLiteStateStore(state_db) as state, IntradayPaperStore(
        paper_db, operations._intraday_paper_config(config)
    ) as paper:
        plan = state.list_intraday_plans()[0]
        payload = plan["payload"]
        armed, entered, exit_armed, exited = _close_paper_plan_at_target(
            paper,
            plan,
        )
        regular_close = datetime.fromisoformat(payload["regular_close"])
        operations._sync_intraday_paper_plan(
            paper_store=paper,
            store=state,
            record=plan,
            at=regular_close + timedelta(seconds=15),
            regular_close=regular_close,
        )
        outbox = state.list_notification_outbox()

        assert armed["action"] == "ENTRY_ARMED"
        assert entered["action"] == "ENTRY_FILLED"
        assert exit_armed["action"] == "TARGET_ARMED"
        assert exited["action"] == "TARGET_EXIT_FILLED"
        assert paper.daily_summary(plan["session_date"])["status"] == "CLOSED"
        assert {
            item["message"]
            for item in outbox
            if item["message"].startswith("intraday_paper_")
        } >= {
            "intraday_paper_entry_filled",
            "intraday_paper_exit_filled",
            "intraday_paper_daily_report",
        }


def test_restart_reconciles_prior_terminal_fill_alerts_and_daily_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "simulation.yaml"
    paper_db = tmp_path / "intraday-paper.sqlite3"
    _write_automatic_intraday_config(config_path)
    _enable_one_day_simulation(config_path, paper_db)
    operations._bootstrap_intraday_news_ledger(
        (tmp_path / "news.sqlite3").resolve()
    )
    monkeypatch.setattr(operations.time, "sleep", lambda _seconds: None)
    responses = _automatic_successful_responses()
    first_transport = FakeTransport(
        [responses[0], responses[1], responses[6], *responses[7:15], *responses[19:22]]
    )

    first, state_db = _run(
        tmp_path,
        first_transport,
        config_name="simulation.yaml",
    )
    assert first.ready is True
    config = load_config(config_path)
    with SQLiteStateStore(state_db) as state, IntradayPaperStore(
        paper_db,
        operations._intraday_paper_config(config),
    ) as paper:
        plan = state.list_intraday_plans()[0]
        _close_paper_plan_at_target(paper, plan)
        assert paper.daily_summary(plan["session_date"])["status"] == "CLOSED"
        assert {
            alert["event"] for alert in paper.list_alerts(pending_only=True)
        } >= {"entry_filled", "exit_filled"}
        assert not any(
            item["message"] == "intraday_paper_daily_report"
            for item in state.list_notification_outbox()
        )

    restart_at = datetime(2026, 8, 31, 12, 30, tzinfo=timezone.utc)
    restarted, _ = _run(
        tmp_path,
        FakeTransport([]),
        config_name="simulation.yaml",
        at=restart_at,
    )
    assert restarted.blockers == ("intraday_simulation_complete",)
    with SQLiteStateStore(state_db) as state, IntradayPaperStore(
        paper_db,
        operations._intraday_paper_config(config),
    ) as paper:
        assert paper.list_alerts(pending_only=True) == []
        messages = [item["message"] for item in state.list_notification_outbox()]
        assert messages.count("intraday_paper_entry_filled") == 1
        assert messages.count("intraday_paper_exit_filled") == 1
        assert messages.count("intraday_paper_daily_report") == 1

    repeated, _ = _run(
        tmp_path,
        FakeTransport([]),
        config_name="simulation.yaml",
        at=restart_at + timedelta(minutes=1),
    )
    assert repeated.blockers == ("intraday_simulation_complete",)
    with SQLiteStateStore(state_db) as state:
        messages = [item["message"] for item in state.list_notification_outbox()]
    assert messages.count("intraday_paper_entry_filled") == 1
    assert messages.count("intraday_paper_exit_filled") == 1
    assert messages.count("intraday_paper_daily_report") == 1


def test_intraday_automatic_restart_never_reselects(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "intraday.yaml"
    _write_automatic_intraday_config(config_path)
    monkeypatch.setattr(operations.time, "sleep", lambda _seconds: None)
    _, state_db = _run(
        tmp_path,
        FakeTransport(_automatic_successful_responses()),
    )
    with SQLiteStateStore(state_db) as store:
        original = store.list_intraday_plans()[0]
    transport = FakeTransport([_token(), _calendar()])

    snapshot, _ = _run(tmp_path, transport, at=NOW + timedelta(minutes=1))

    assert snapshot.ready is True
    assert len(transport.requests) == 2
    assert not any(
        request.url.endswith(("/rankings", "/stocks/all", "/prices", "/orderbook"))
        for request in transport.requests
    )
    with SQLiteStateStore(state_db) as store:
        plans = store.list_intraday_plans()
    assert len(plans) == 1
    assert plans[0]["plan_id"] == original["plan_id"]
    assert plans[0]["plan_hash"] == original["plan_hash"]
    assert plans[0]["payload"]["selection_snapshot"] == original["payload"][
        "selection_snapshot"
    ]


def test_intraday_automatic_stale_ranking_fails_closed(
    tmp_path, monkeypatch
) -> None:
    config_path = tmp_path / "intraday.yaml"
    _write_automatic_intraday_config(config_path)
    monkeypatch.setattr(operations.time, "sleep", lambda _seconds: None)
    transport = FakeTransport(
        _automatic_successful_responses(
            ranked_at="2026-08-28T12:00:00+00:00"
        )[:8]
    )

    snapshot, state_db = _run(tmp_path, transport)

    assert snapshot.blockers == ("intraday_ranking_stale",)
    assert transport.requests[-1].url.endswith("/api/v1/rankings")
    assert all(
        request.method == "GET" or request.url.endswith("/oauth2/token")
        for request in transport.requests
    )
    with SQLiteStateStore(state_db) as store:
        assert store.list_intraday_plans() == []


def test_intraday_automatic_rechecks_ranking_freshness_before_lock(
    tmp_path, monkeypatch
) -> None:
    config_path = tmp_path / "intraday.yaml"
    _write_automatic_intraday_config(config_path)
    monkeypatch.setattr(operations.time, "sleep", lambda _seconds: None)
    transport = FakeTransport(
        _automatic_successful_responses(
            price_timestamp="2026-08-28T12:32:05+00:00",
            book_timestamp="2026-08-28T12:32:06+00:00",
        )
    )

    def advancing_clock() -> datetime:
        if len(transport.requests) >= 19:
            return NOW + timedelta(minutes=2, seconds=10)
        return NOW

    snapshot, state_db = _run(tmp_path, transport, clock=advancing_clock)

    assert snapshot.blockers == ("intraday_ranking_stale",)
    assert transport.requests[-1].url.endswith("/api/v1/orderbook")
    assert all(
        request.method == "GET" or request.url.endswith("/oauth2/token")
        for request in transport.requests
    )
    with SQLiteStateStore(state_db) as store:
        assert store.list_intraday_plans() == []


def test_intraday_automatic_rechecks_freshness_at_database_lock(
    tmp_path, monkeypatch
) -> None:
    config_path = tmp_path / "intraday.yaml"
    _write_automatic_intraday_config(config_path)
    monkeypatch.setattr(operations.time, "sleep", lambda _seconds: None)
    transport = FakeTransport(_automatic_successful_responses())
    calls_after_final_account_check = 0

    def lock_boundary_clock() -> datetime:
        nonlocal calls_after_final_account_check
        if len(transport.requests) >= 26:
            calls_after_final_account_check += 1
            if calls_after_final_account_check >= 2:
                return NOW + timedelta(seconds=20)
        return NOW

    snapshot, state_db = _run(tmp_path, transport, clock=lock_boundary_clock)

    assert snapshot.blockers == ("intraday_price_stale",)
    assert transport.requests[-1].url.endswith("/api/v1/buying-power")
    assert calls_after_final_account_check >= 2
    with SQLiteStateStore(state_db) as store:
        assert store.list_intraday_plans() == []


def test_intraday_automatic_rechecks_final_price_change_before_lock(
    tmp_path, monkeypatch
) -> None:
    config_path = tmp_path / "intraday.yaml"
    _write_automatic_intraday_config(config_path)
    monkeypatch.setattr(operations.time, "sleep", lambda _seconds: None)
    transport = FakeTransport(
        _automatic_successful_responses(final_last_price="98.1")
    )

    snapshot, state_db = _run(tmp_path, transport)

    assert snapshot.blockers == ("intraday_no_eligible_candidate",)
    assert transport.requests[-1].url.endswith("/api/v1/orderbook")
    assert all(
        request.method == "GET" or request.url.endswith("/oauth2/token")
        for request in transport.requests
    )
    with SQLiteStateStore(state_db) as store:
        assert store.list_intraday_plans() == []


def test_intraday_automatic_partial_stock_details_are_malformed(
    tmp_path, monkeypatch
) -> None:
    config_path = tmp_path / "intraday.yaml"
    _write_automatic_intraday_config(config_path)
    monkeypatch.setattr(operations.time, "sleep", lambda _seconds: None)
    responses = _automatic_successful_responses()
    responses[7].payload["result"]["rankings"].append(
        {
            "rank": 2,
            "symbol": "MSFT",
            "currency": "USD",
            "price": {
                "lastPrice": "50",
                "basePrice": "49",
                "changeRate": "0.02",
            },
            "tradingVolume": "100000",
            "tradingAmount": "10000000",
        }
    )
    responses[8].payload["result"].append(
        {
            "symbol": "MSFT",
            "securityType": "STOCK",
            "isCommonShare": True,
            "isinCode": "US5949181045",
        }
    )
    transport = FakeTransport(responses[:12])

    snapshot, state_db = _run(tmp_path, transport)

    assert snapshot.blockers == ("intraday_stock_info_malformed",)
    assert transport.requests[-1].url.endswith("/api/v1/stocks")
    assert transport.requests[-1].query == {"symbols": "AAPL,MSFT"}
    with SQLiteStateStore(state_db) as store:
        assert store.list_intraday_plans() == []


def test_intraday_automatic_stock_market_must_match_universe(
    tmp_path, monkeypatch
) -> None:
    config_path = tmp_path / "intraday.yaml"
    _write_automatic_intraday_config(config_path)
    monkeypatch.setattr(operations.time, "sleep", lambda _seconds: None)
    responses = _automatic_successful_responses()
    responses[11].payload["result"][0]["market"] = "NYSE"
    transport = FakeTransport(responses[:12])

    snapshot, state_db = _run(tmp_path, transport)

    assert snapshot.blockers == ("intraday_stock_info_malformed",)
    assert transport.requests[-1].url.endswith("/api/v1/stocks")
    with SQLiteStateStore(state_db) as store:
        assert store.list_intraday_plans() == []


def test_intraday_automatic_warning_candidate_fails_closed(
    tmp_path, monkeypatch
) -> None:
    config_path = tmp_path / "intraday.yaml"
    _write_automatic_intraday_config(config_path)
    monkeypatch.setattr(operations.time, "sleep", lambda _seconds: None)
    transport = FakeTransport(
        _automatic_successful_responses(
            warnings=[{"warningType": "INVESTMENT_CAUTION"}]
        )
    )

    snapshot, state_db = _run(tmp_path, transport)

    assert snapshot.blockers == ("intraday_no_eligible_candidate",)
    assert transport.requests[-1].url.endswith("/api/v1/stocks/AAPL/warnings")
    assert all(
        request.method == "GET" or request.url.endswith("/oauth2/token")
        for request in transport.requests
    )
    with SQLiteStateStore(state_db) as store:
        assert store.list_intraday_plans() == []


def test_intraday_automatic_warning_change_before_lock_fails_closed(
    tmp_path, monkeypatch
) -> None:
    config_path = tmp_path / "intraday.yaml"
    _write_automatic_intraday_config(config_path)
    monkeypatch.setattr(operations.time, "sleep", lambda _seconds: None)
    transport = FakeTransport(
        _automatic_successful_responses(
            final_warnings=[{"warningType": "INVESTMENT_CAUTION"}]
        )
    )

    snapshot, state_db = _run(tmp_path, transport)

    assert snapshot.blockers == ("intraday_stock_warning_changed",)
    warning_requests = [
        request
        for request in transport.requests
        if request.url.endswith("/api/v1/stocks/AAPL/warnings")
    ]
    assert len(warning_requests) == 2
    assert all(request.method == "GET" for request in warning_requests)
    with SQLiteStateStore(state_db) as store:
        assert store.list_intraday_plans() == []


def test_simulation_records_and_reuses_no_candidate_as_covered_no_trade_day(
    tmp_path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "simulation.yaml"
    paper_db = tmp_path / "intraday-paper.sqlite3"
    _write_automatic_intraday_config(config_path)
    _enable_one_day_simulation(config_path, paper_db)
    monkeypatch.setattr(operations.time, "sleep", lambda _seconds: None)
    early_at = NOW - timedelta(minutes=1, seconds=1)
    early_responses = _automatic_successful_responses(
        ranked_at="2026-08-28T12:28:50+00:00"
    )
    early_responses[7].payload["result"]["rankings"] = []
    early_transport = FakeTransport(
        [
            early_responses[0],
            early_responses[1],
            early_responses[6],
            early_responses[7],
        ]
    )

    early, state_db = _run(
        tmp_path,
        early_transport,
        at=early_at,
        config_name="simulation.yaml",
    )

    config = load_config(config_path)
    paper_config = operations._intraday_paper_config(config)
    assert early.ready is False
    assert early.blockers == ("intraday_no_eligible_candidate",)
    with IntradayPaperStore(paper_db, paper_config) as paper:
        assert paper.daily_summary(date(2026, 8, 28))["status"] == "NO_PLAN"

    final_responses = _automatic_successful_responses()
    final_responses[7].payload["result"]["rankings"] = []
    final_transport = FakeTransport(
        [
            final_responses[0],
            final_responses[1],
            final_responses[6],
            final_responses[7],
        ]
    )
    final, _ = _run(
        tmp_path,
        final_transport,
        config_name="simulation.yaml",
    )

    assert final.ready is True
    assert final.blockers == ()
    with IntradayPaperStore(paper_db, paper_config) as paper:
        assert paper.daily_summary(date(2026, 8, 28))["status"] == "NO_CANDIDATE"
        summary = paper.summary(as_of=datetime(2026, 8, 29, tzinfo=timezone.utc))
        assert summary["status"] == "INCOMPLETE"
        assert summary["coverage"]["no_candidate"] == ["2026-08-28"]
        assert summary["coverage"]["missing"] == []

    second_transport = FakeTransport([_token(), _calendar()])
    second, repeated_state_db = _run(
        tmp_path,
        second_transport,
        config_name="simulation.yaml",
    )

    assert repeated_state_db == state_db
    assert second.ready is True
    assert second.blockers == ()
    assert all(
        not request.url.endswith("/api/v1/rankings")
        for request in second_transport.requests
    )


@pytest.mark.parametrize(
    ("deadline_offset_seconds", "ranked_at", "ready", "blocker", "day_status"),
    [
        (-61, "2026-08-28T12:28:50+00:00", False, "intraday_no_eligible_candidate", "NO_PLAN"),
        (-60, "2026-08-28T12:28:50+00:00", True, None, "NO_CANDIDATE"),
        (0, "2026-08-28T12:29:50+00:00", True, None, "NO_CANDIDATE"),
        (1, "2026-08-28T12:29:50+00:00", False, "intraday_plan_deadline_missed", "NO_PLAN"),
    ],
)
def test_no_candidate_final_attempt_deadline_boundaries(
    tmp_path,
    monkeypatch,
    deadline_offset_seconds: int,
    ranked_at: str,
    ready: bool,
    blocker: str | None,
    day_status: str,
) -> None:
    config_path = tmp_path / "simulation.yaml"
    paper_db = tmp_path / "intraday-paper.sqlite3"
    _write_automatic_intraday_config(config_path)
    _enable_one_day_simulation(config_path, paper_db)
    monkeypatch.setattr(operations.time, "sleep", lambda _seconds: None)
    responses = _automatic_successful_responses(ranked_at=ranked_at)
    responses[7].payload["result"]["rankings"] = []
    queued = (
        [responses[0], responses[1]]
        if deadline_offset_seconds > 0
        else [responses[0], responses[1], responses[6], responses[7]]
    )

    snapshot, _ = _run(
        tmp_path,
        FakeTransport(queued),
        at=NOW + timedelta(seconds=deadline_offset_seconds),
        config_name="simulation.yaml",
    )

    assert snapshot.ready is ready
    assert snapshot.blockers == (() if blocker is None else (blocker,))
    config = load_config(config_path)
    with IntradayPaperStore(
        paper_db,
        operations._intraday_paper_config(config),
    ) as paper:
        assert paper.daily_summary(date(2026, 8, 28))["status"] == day_status


def test_simulation_can_select_later_after_an_early_no_candidate(
    tmp_path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "simulation.yaml"
    paper_db = tmp_path / "intraday-paper.sqlite3"
    _write_automatic_intraday_config(config_path)
    _enable_one_day_simulation(config_path, paper_db)
    monkeypatch.setattr(operations.time, "sleep", lambda _seconds: None)
    early_responses = _automatic_successful_responses(
        ranked_at="2026-08-28T12:27:50+00:00"
    )
    early_responses[7].payload["result"]["rankings"] = []

    early, _ = _run(
        tmp_path,
        FakeTransport(
            [
                early_responses[0],
                early_responses[1],
                early_responses[6],
                early_responses[7],
            ]
        ),
        at=NOW - timedelta(minutes=2),
        config_name="simulation.yaml",
    )
    responses = _automatic_successful_responses()
    selected, state_db = _run(
        tmp_path,
        FakeTransport(
            [
                responses[0],
                responses[1],
                responses[6],
                *responses[7:15],
                *responses[19:22],
            ]
        ),
        config_name="simulation.yaml",
    )

    assert early.ready is False
    assert selected.ready is True
    with SQLiteStateStore(state_db) as store:
        assert [plan["symbol"] for plan in store.list_intraday_plans()] == ["AAPL"]
    config = load_config(config_path)
    with IntradayPaperStore(
        paper_db,
        operations._intraday_paper_config(config),
    ) as paper:
        assert paper.daily_summary(date(2026, 8, 28))["status"] == "WAITING_ENTRY"


def test_simulation_does_not_cover_no_candidate_when_market_data_is_stale(
    tmp_path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "simulation.yaml"
    paper_db = tmp_path / "intraday-paper.sqlite3"
    _write_automatic_intraday_config(config_path)
    _enable_one_day_simulation(config_path, paper_db)
    monkeypatch.setattr(operations.time, "sleep", lambda _seconds: None)
    responses = _automatic_successful_responses(
        price_timestamp="2026-08-28T12:00:00+00:00"
    )
    transport = FakeTransport(
        [
            responses[0],
            responses[1],
            responses[6],
            *responses[7:15],
            *responses[19:22],
        ]
    )

    snapshot, _ = _run(
        tmp_path,
        transport,
        config_name="simulation.yaml",
    )

    assert snapshot.ready is False
    assert snapshot.blockers == ("intraday_price_stale",)
    config = load_config(config_path)
    with IntradayPaperStore(
        paper_db,
        operations._intraday_paper_config(config),
    ) as paper:
        assert paper.daily_summary(date(2026, 8, 28))["status"] == "NO_PLAN"
        assert paper.summary(as_of=NOW)["coverage"]["missing"] == ["2026-08-28"]


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        ("daily_incomplete", "intraday_daily_candles_incomplete"),
        ("daily_stale", "intraday_daily_candles_stale"),
        ("daily_future", "intraday_daily_candles_future"),
        ("premarket_incomplete", "intraday_premarket_candles_incomplete"),
        ("premarket_stale", "intraday_premarket_candles_stale"),
        ("premarket_future", "intraday_premarket_candles_future"),
    ],
)
def test_simulation_candle_data_quality_failure_never_becomes_coverage(
    tmp_path,
    monkeypatch,
    failure: str,
    expected_code: str,
) -> None:
    config_path = tmp_path / "simulation.yaml"
    paper_db = tmp_path / "intraday-paper.sqlite3"
    _write_automatic_intraday_config(config_path)
    _enable_one_day_simulation(config_path, paper_db)
    monkeypatch.setattr(operations.time, "sleep", lambda _seconds: None)
    responses = _automatic_successful_responses()
    daily = responses[13].payload["result"]["candles"]
    premarket = responses[14].payload["result"]["candles"]
    if failure == "daily_incomplete":
        responses[13].payload["result"]["candles"] = daily[:-1]
    elif failure == "daily_stale":
        responses[13].payload["result"]["candles"] = [
            {
                **_candle(
                    (datetime(2026, 8, 20, 4, tzinfo=timezone.utc) - timedelta(days=index)).isoformat()
                ),
                "volume": "1000000",
            }
            for index in range(20)
        ]
    elif failure == "daily_future":
        daily[0]["timestamp"] = "2026-09-01T04:00:00+00:00"
    elif failure == "premarket_incomplete":
        responses[14].payload["result"]["candles"] = premarket[:2]
    elif failure == "premarket_stale":
        responses[14].payload["result"]["candles"] = [
            _candle(value)
            for value in (
                "2026-08-28T08:00:00+00:00",
                "2026-08-28T12:00:00+00:00",
                "2026-08-28T12:01:00+00:00",
                "2026-08-28T12:02:00+00:00",
            )
        ]
    else:
        premarket.append(_candle("2026-08-28T12:31:00+00:00"))

    snapshot, _ = _run(
        tmp_path,
        FakeTransport(
            [responses[0], responses[1], responses[6], *responses[7:15]]
        ),
        config_name="simulation.yaml",
    )

    assert snapshot.ready is False
    assert snapshot.blockers == (expected_code,)
    config = load_config(config_path)
    with IntradayPaperStore(
        paper_db,
        operations._intraday_paper_config(config),
    ) as paper:
        assert paper.daily_summary(date(2026, 8, 28))["status"] == "NO_PLAN"
        assert paper.summary(as_of=NOW)["coverage"]["missing"] == ["2026-08-28"]


def test_simulation_uses_effective_service_interval_for_final_attempt(
    tmp_path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "simulation.yaml"
    paper_db = tmp_path / "intraday-paper.sqlite3"
    _write_automatic_intraday_config(config_path)
    _enable_one_day_simulation(config_path, paper_db)
    monkeypatch.setattr(operations.time, "sleep", lambda _seconds: None)
    responses = _automatic_successful_responses(
        ranked_at="2026-08-28T12:28:20+00:00"
    )
    responses[7].payload["result"]["rankings"] = []

    snapshot, _ = _run(
        tmp_path,
        FakeTransport([responses[0], responses[1], responses[6], responses[7]]),
        at=NOW - timedelta(minutes=1, seconds=30),
        config_name="simulation.yaml",
        interval_seconds=120,
    )

    assert snapshot.ready is True
    config = load_config(config_path)
    assert config.runtime.interval_seconds == 60
    with IntradayPaperStore(
        paper_db,
        operations._intraday_paper_config(config),
    ) as paper:
        assert paper.daily_summary(date(2026, 8, 28))["status"] == "NO_CANDIDATE"


def test_automatic_selection_continues_after_first_candidate_data_error(
    tmp_path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "simulation.yaml"
    paper_db = tmp_path / "intraday-paper.sqlite3"
    _write_automatic_intraday_config(config_path)
    _enable_one_day_simulation(config_path, paper_db)
    monkeypatch.setattr(operations.time, "sleep", lambda _seconds: None)
    first = _automatic_successful_responses()
    second = _automatic_successful_responses()
    first[7].payload["result"]["rankings"].append(
        {
            **first[7].payload["result"]["rankings"][0],
            "rank": 2,
            "symbol": "MSFT",
        }
    )
    first[8].payload["result"].append(
        {
            "symbol": "MSFT",
            "securityType": "STOCK",
            "isCommonShare": True,
            "isinCode": "US5949181045",
        }
    )
    first[11].payload["result"].append(
        {
            **first[11].payload["result"][0],
            "symbol": "MSFT",
        }
    )
    first[13].payload["result"]["candles"] = first[13].payload["result"]["candles"][:-1]
    second[19].payload["result"][0]["symbol"] = "MSFT"

    snapshot, state_db = _run(
        tmp_path,
        FakeTransport(
            [
                first[0], first[1], first[6], *first[7:14],
                second[12], second[13], second[14],
                second[19], second[20], second[21],
            ]
        ),
        config_name="simulation.yaml",
    )

    assert snapshot.ready is True
    with SQLiteStateStore(state_db) as store:
        assert [plan["symbol"] for plan in store.list_intraday_plans()] == ["MSFT"]


def test_mixed_data_error_and_threshold_rejection_preserves_first_error(
    tmp_path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "simulation.yaml"
    paper_db = tmp_path / "intraday-paper.sqlite3"
    _write_automatic_intraday_config(config_path)
    _enable_one_day_simulation(config_path, paper_db)
    monkeypatch.setattr(operations.time, "sleep", lambda _seconds: None)
    first = _automatic_successful_responses()
    second = _automatic_successful_responses(
        warnings=[{"warningType": "INVESTMENT_CAUTION"}]
    )
    first[7].payload["result"]["rankings"].append(
        {
            **first[7].payload["result"]["rankings"][0],
            "rank": 2,
            "symbol": "MSFT",
        }
    )
    first[8].payload["result"].append(
        {
            "symbol": "MSFT",
            "securityType": "STOCK",
            "isCommonShare": True,
            "isinCode": "US5949181045",
        }
    )
    first[11].payload["result"].append(
        {
            **first[11].payload["result"][0],
            "symbol": "MSFT",
        }
    )
    first[13].payload["result"]["candles"] = first[13].payload["result"]["candles"][:-1]

    snapshot, _ = _run(
        tmp_path,
        FakeTransport(
            [first[0], first[1], first[6], *first[7:14], second[12]]
        ),
        config_name="simulation.yaml",
    )

    assert snapshot.ready is False
    assert snapshot.blockers == ("intraday_daily_candles_incomplete",)
    config = load_config(config_path)
    with IntradayPaperStore(
        paper_db,
        operations._intraday_paper_config(config),
    ) as paper:
        assert paper.daily_summary(date(2026, 8, 28))["status"] == "NO_PLAN"


@pytest.mark.parametrize(
    "threshold",
    ["daily_value", "daily_range", "premarket_volume", "premarket_range"],
)
def test_strategy_threshold_rejection_is_coverable_no_candidate(
    tmp_path,
    monkeypatch,
    threshold: str,
) -> None:
    config_path = tmp_path / "simulation.yaml"
    paper_db = tmp_path / "intraday-paper.sqlite3"
    _write_automatic_intraday_config(config_path)
    _enable_one_day_simulation(config_path, paper_db)
    monkeypatch.setattr(operations.time, "sleep", lambda _seconds: None)
    responses = _automatic_successful_responses()
    if threshold == "daily_value":
        for candle in responses[13].payload["result"]["candles"]:
            candle["volume"] = "1"
    elif threshold == "daily_range":
        for candle in responses[13].payload["result"]["candles"]:
            candle.update(highPrice="110", lowPrice="90")
    elif threshold == "premarket_volume":
        for candle in responses[14].payload["result"]["candles"]:
            candle["volume"] = "0"
    else:
        for candle in responses[14].payload["result"]["candles"]:
            candle.update(highPrice="110", lowPrice="90")

    snapshot, _ = _run(
        tmp_path,
        FakeTransport(
            [responses[0], responses[1], responses[6], *responses[7:15]]
        ),
        config_name="simulation.yaml",
    )

    assert snapshot.ready is True
    config = load_config(config_path)
    with IntradayPaperStore(
        paper_db,
        operations._intraday_paper_config(config),
    ) as paper:
        assert paper.daily_summary(date(2026, 8, 28))["status"] == "NO_CANDIDATE"


def test_intraday_exports_only_locked_symbol_as_atomic_news_context(tmp_path) -> None:
    context_path = tmp_path / "news" / "news-context.json"
    config_path = tmp_path / "intraday.yaml"
    _write_intraday_config(config_path, news_context_path=context_path)

    snapshot, _ = _run(tmp_path, FakeTransport(_successful_responses()))

    context = json.loads(context_path.read_text(encoding="utf-8"))
    assert snapshot.ready is True
    assert context == {
        "schema_version": 1,
        "generated_at": "2026-08-28T12:30:00+00:00",
        "market": "US",
        "session_date": "2026-08-28",
        "active_until": "2026-08-29T05:00:00+09:00",
        "symbol": "AAPL",
        "reason": "intraday_plan",
    }
    serialized = json.dumps(context)
    for forbidden in (
        "account",
        "cash",
        "quantity",
        "entry",
        "target",
        "stop",
        "price",
        "order",
    ):
        assert forbidden not in serialized.lower()


def test_intraday_exports_redacted_stable_approval_envelope(tmp_path) -> None:
    envelope_path = tmp_path / "approval" / "approval-envelope.json"
    config_path = tmp_path / "intraday.yaml"
    _write_intraday_config(
        config_path,
        approval_envelope_path=envelope_path,
    )

    snapshot, state_db = _run(tmp_path, FakeTransport(_successful_responses()))
    first_raw = envelope_path.read_text(encoding="utf-8")
    envelope = json.loads(first_raw)
    worker_envelope = load_envelope(envelope_path)
    with SQLiteStateStore(state_db) as store:
        plan = store.list_intraday_plans()[0]

    assert snapshot.ready is True
    assert set(envelope) == {
        "schema_version",
        "generated_at",
        "expires_at",
        "session_date",
        "plan_id",
        "plan_hash",
        "nonce",
        "account_alias",
        "mode",
        "live_order_submission",
        "symbol",
        "allocated_cash",
        "quantity",
        "entry_trigger",
        "entry_limit",
        "target_trigger",
        "stop_trigger",
        "stop_limit",
        "planned_risk",
        "reward_risk_ratio",
    }
    assert envelope["schema_version"] == 1
    assert envelope["expires_at"] == "2026-08-28T22:30:00+09:00"
    assert envelope["plan_id"] == plan["plan_id"]
    assert envelope["plan_hash"] == plan["plan_hash"]
    assert envelope["symbol"] == "AAPL"
    assert envelope["mode"] == "shadow"
    assert envelope["live_order_submission"] is False
    assert worker_envelope.plan_hash == plan["plan_hash"]
    assert worker_envelope.hash_suffix == plan["plan_hash"][-8:]
    assert len(envelope["nonce"]) >= 20
    assert "available_cash" not in envelope
    assert "risk_budget" not in envelope
    assert "account_key" not in envelope
    assert "order_id" not in envelope
    assert "raw-account-7" not in first_raw
    assert "TOSS_CLIENT_SECRET" not in first_raw

    inbox = envelope_path.parent / "approval-inbox"
    inbox.mkdir(mode=0o700)
    if os.name != "nt":
        inbox.chmod(0o700)
    approval_config = ApprovalConfig.from_env(
        {
            "DISCORD_APPROVAL_BOT_TOKEN": "test-token-with-more-than-forty-characters",
            "DISCORD_ALLOWED_GUILD_ID": "111111111111111111",
            "DISCORD_ALLOWED_CHANNEL_ID": "222222222222222222",
            "DISCORD_ALLOWED_USER_ID": "333333333333333333",
            "DISCORD_APPROVAL_ENVELOPE_PATH": str(envelope_path),
            "DISCORD_APPROVAL_INBOX_DIR": str(inbox),
        }
    )
    assert approval_config.envelope_path == envelope_path

    restarted, _ = _run(
        tmp_path,
        FakeTransport([_token(), _calendar()]),
        at=NOW + timedelta(minutes=1),
    )

    assert restarted.ready is True
    assert envelope_path.read_text(encoding="utf-8") == first_raw


def test_intraday_approval_envelope_does_not_overwrite_invalid_file(tmp_path) -> None:
    envelope_path = tmp_path / "approval-envelope.json"
    envelope_path.write_text('{"keep":true}', encoding="utf-8")
    config_path = tmp_path / "intraday.yaml"
    _write_intraday_config(
        config_path,
        approval_envelope_path=envelope_path,
    )

    snapshot, state_db = _run(tmp_path, FakeTransport(_successful_responses()))

    assert snapshot.ready is True
    assert envelope_path.read_text(encoding="utf-8") == '{"keep":true}'
    with SQLiteStateStore(state_db) as store:
        events = store.list_runtime_events(limit=100)
    failure = next(
        event
        for event in events
        if event["message"] == "intraday_approval_envelope_export_failed"
    )
    assert failure["payload"] == {
        "code": "approval_envelope_invalid_existing",
        "session_date": "2026-08-28",
        "symbol": "AAPL",
    }
    assert str(envelope_path) not in json.dumps(failure, default=str)


def test_intraday_approval_diagnostic_never_copies_untrusted_symbol(tmp_path) -> None:
    state_db = tmp_path / "state.sqlite3"
    with SQLiteStateStore(state_db) as store:
        _refresh_intraday_approval_envelope(
            config=SimpleNamespace(
                intraday=SimpleNamespace(
                    approval_envelope_path=str(tmp_path / "approval-envelope.json")
                )
            ),
            record={
                "payload": {},
                "session_date": date(2026, 8, 28),
                "plan_id": "",
                "plan_hash": "",
                "symbol": "AAPL\nprivate-canary" * 20,
            },
            store=store,
            account_alias="test-account",
            at=NOW,
            current_regular_open=NOW + timedelta(hours=1),
        )
        failure = next(
            event
            for event in store.list_runtime_events(limit=100)
            if event["message"] == "intraday_approval_envelope_export_failed"
        )

    assert failure["payload"]["symbol"] is None
    assert "private-canary" not in json.dumps(failure, default=str)


def test_intraday_approval_envelope_does_not_clobber_file_created_during_refresh(
    tmp_path, monkeypatch
) -> None:
    envelope_path = tmp_path / "approval-envelope.json"
    config_path = tmp_path / "intraday.yaml"
    _write_intraday_config(config_path, approval_envelope_path=envelope_path)
    original_reader = operations._read_existing_intraday_approval_envelope

    def create_after_absence_check(target):
        existing = original_reader(target)
        assert existing is None
        target.write_text('{"keep":true}', encoding="utf-8")
        if os.name != "nt":
            target.chmod(0o600)
        return None

    monkeypatch.setattr(
        operations,
        "_read_existing_intraday_approval_envelope",
        create_after_absence_check,
    )

    snapshot, state_db = _run(tmp_path, FakeTransport(_successful_responses()))

    assert snapshot.ready is True
    assert envelope_path.read_text(encoding="utf-8") == '{"keep":true}'
    with SQLiteStateStore(state_db) as store:
        failures = [
            event
            for event in store.list_runtime_events(limit=100)
            if event["message"] == "intraday_approval_envelope_export_failed"
        ]
    assert any(
        event["payload"]["code"] == "approval_envelope_write_failed"
        for event in failures
    )


def test_intraday_approval_envelope_refuses_changed_existing_file(
    tmp_path, monkeypatch
) -> None:
    envelope_path = tmp_path / "approval-envelope.json"
    config_path = tmp_path / "intraday.yaml"
    _write_intraday_config(config_path, approval_envelope_path=envelope_path)
    _, state_db = _run(tmp_path, FakeTransport(_successful_responses()))
    original = json.loads(envelope_path.read_text(encoding="utf-8"))
    original_reader = operations._read_existing_intraday_approval_envelope

    def change_after_read(target):
        existing = original_reader(target)
        changed = dict(original)
        changed["quantity"] = 777
        target.write_text(json.dumps(changed), encoding="utf-8")
        if os.name != "nt":
            target.chmod(0o600)
        return existing

    monkeypatch.setattr(
        operations,
        "_read_existing_intraday_approval_envelope",
        change_after_read,
    )

    with SQLiteStateStore(state_db) as store:
        record = store.list_intraday_plans()[0]
        _refresh_intraday_approval_envelope(
            config=SimpleNamespace(
                intraday=SimpleNamespace(approval_envelope_path=str(envelope_path))
            ),
            record=record,
            store=store,
            account_alias="test-account",
            at=NOW + timedelta(minutes=1),
            current_regular_open=datetime(
                2026,
                8,
                28,
                22,
                0,
                tzinfo=timezone(timedelta(hours=9)),
            ),
        )
        failures = [
            event
            for event in store.list_runtime_events(limit=100)
            if event["message"] == "intraday_approval_envelope_export_failed"
        ]

    assert json.loads(envelope_path.read_text(encoding="utf-8"))["quantity"] == 777
    assert any(
        event["payload"]["code"] == "approval_envelope_write_failed"
        for event in failures
    )


def test_intraday_approval_envelope_expiry_only_moves_earlier(tmp_path) -> None:
    envelope_path = tmp_path / "approval-envelope.json"
    config_path = tmp_path / "intraday.yaml"
    _write_intraday_config(
        config_path,
        approval_envelope_path=envelope_path,
    )
    _, state_db = _run(tmp_path, FakeTransport(_successful_responses()))
    original = json.loads(envelope_path.read_text(encoding="utf-8"))
    earlier_open = datetime(
        2026,
        8,
        28,
        22,
        0,
        tzinfo=timezone(timedelta(hours=9)),
    )
    config = SimpleNamespace(
        intraday=SimpleNamespace(approval_envelope_path=str(envelope_path))
    )

    with SQLiteStateStore(state_db) as store:
        record = store.list_intraday_plans()[0]
        _refresh_intraday_approval_envelope(
            config=config,
            record=record,
            store=store,
            account_alias="test-account",
            at=NOW + timedelta(minutes=1),
            current_regular_open=earlier_open,
        )
        shortened = json.loads(envelope_path.read_text(encoding="utf-8"))
        _refresh_intraday_approval_envelope(
            config=config,
            record=record,
            store=store,
            account_alias="test-account",
            at=NOW + timedelta(minutes=2),
            current_regular_open=datetime(
                2026,
                8,
                28,
                22,
                30,
                tzinfo=timezone(timedelta(hours=9)),
            ),
        )

    assert shortened["expires_at"] == earlier_open.isoformat()
    assert shortened["nonce"] == original["nonce"]
    assert json.loads(envelope_path.read_text(encoding="utf-8")) == shortened


def test_intraday_does_not_recreate_missing_approval_envelope_after_open(
    tmp_path,
) -> None:
    envelope_path = tmp_path / "approval" / "approval-envelope.json"
    config_path = tmp_path / "intraday.yaml"
    _write_intraday_config(config_path, approval_envelope_path=envelope_path)
    _, state_db = _run(tmp_path, FakeTransport(_successful_responses()))
    envelope_path.unlink()

    restarted, _ = _run(
        tmp_path,
        FakeTransport([_token(), _calendar()]),
        at=NOW + timedelta(hours=2),
    )

    assert restarted.ready is False
    assert restarted.blockers == ("intraday_execution_engine_not_enabled",)
    assert not envelope_path.exists()
    with SQLiteStateStore(state_db) as store:
        failures = [
            event
            for event in store.list_runtime_events(limit=100)
            if event["message"] == "intraday_approval_envelope_export_failed"
        ]
    assert any(
        event["payload"]["code"] == "approval_envelope_expired"
        for event in failures
    )


@pytest.mark.parametrize(
    ("field", "tampered"),
    [
        ("account_alias", "wrong-account"),
        ("allocated_cash", "999"),
        ("quantity", 999),
        ("entry_trigger", "999"),
        ("entry_limit", "999"),
        ("target_trigger", "999"),
        ("stop_trigger", "1"),
        ("stop_limit", "1"),
        ("planned_risk", "999"),
        ("reward_risk_ratio", "999"),
    ],
)
def test_intraday_repairs_tampered_public_approval_fields_with_new_nonce(
    tmp_path, field, tampered
) -> None:
    envelope_path = tmp_path / "approval" / "approval-envelope.json"
    config_path = tmp_path / "intraday.yaml"
    _write_intraday_config(config_path, approval_envelope_path=envelope_path)
    _run(tmp_path, FakeTransport(_successful_responses()))
    original = json.loads(envelope_path.read_text(encoding="utf-8"))
    changed = dict(original)
    changed[field] = tampered
    envelope_path.write_text(json.dumps(changed), encoding="utf-8")
    if os.name != "nt":
        envelope_path.chmod(0o600)

    restarted, _ = _run(
        tmp_path,
        FakeTransport([_token(), _calendar()]),
        at=NOW + timedelta(minutes=1),
    )

    repaired = json.loads(envelope_path.read_text(encoding="utf-8"))
    assert restarted.ready is True
    assert repaired[field] == original[field]
    assert repaired["nonce"] != original["nonce"]
    load_envelope(envelope_path)


def test_intraday_tamper_repair_never_reextends_shortened_expiry(tmp_path) -> None:
    envelope_path = tmp_path / "approval" / "approval-envelope.json"
    config_path = tmp_path / "intraday.yaml"
    _write_intraday_config(config_path, approval_envelope_path=envelope_path)
    _, state_db = _run(tmp_path, FakeTransport(_successful_responses()))
    earlier_open = datetime(
        2026,
        8,
        28,
        22,
        0,
        tzinfo=timezone(timedelta(hours=9)),
    )
    config = SimpleNamespace(
        intraday=SimpleNamespace(approval_envelope_path=str(envelope_path))
    )
    with SQLiteStateStore(state_db) as store:
        record = store.list_intraday_plans()[0]
        _refresh_intraday_approval_envelope(
            config=config,
            record=record,
            store=store,
            account_alias="test-account",
            at=NOW + timedelta(minutes=1),
            current_regular_open=earlier_open,
        )
    shortened = json.loads(envelope_path.read_text(encoding="utf-8"))
    tampered = dict(shortened)
    tampered["quantity"] = 999
    envelope_path.write_text(json.dumps(tampered), encoding="utf-8")
    if os.name != "nt":
        envelope_path.chmod(0o600)

    with SQLiteStateStore(state_db) as store:
        record = store.list_intraday_plans()[0]
        _refresh_intraday_approval_envelope(
            config=config,
            record=record,
            store=store,
            account_alias="test-account",
            at=NOW + timedelta(minutes=2),
            current_regular_open=datetime(
                2026,
                8,
                28,
                22,
                30,
                tzinfo=timezone(timedelta(hours=9)),
            ),
        )

    repaired = json.loads(envelope_path.read_text(encoding="utf-8"))
    assert repaired["quantity"] == shortened["quantity"]
    assert repaired["nonce"] != shortened["nonce"]
    assert datetime.fromisoformat(repaired["expires_at"]) == earlier_open
    load_envelope(envelope_path)


def test_intraday_news_context_failure_never_blocks_or_leaks_path(
    tmp_path, monkeypatch
) -> None:
    context_path = tmp_path / "private-canary" / "news-context.json"
    config_path = tmp_path / "intraday.yaml"
    _write_intraday_config(config_path, news_context_path=context_path)
    monkeypatch.setattr(
        "turtle_bot.operations.os.replace",
        lambda source, target: (_ for _ in ()).throw(OSError("private-canary")),
    )

    snapshot, state_db = _run(tmp_path, FakeTransport(_successful_responses()))

    with SQLiteStateStore(state_db) as store:
        events = store.list_runtime_events(limit=100)
    failure = next(
        event
        for event in events
        if event["message"] == "intraday_news_context_export_failed"
    )
    assert snapshot.ready is True
    assert failure["payload"] == {
        "code": "news_context_write_failed",
        "session_date": "2026-08-28",
        "symbol": "AAPL",
    }
    assert "private-canary" not in json.dumps(failure, default=str)


def test_intraday_news_context_does_not_overwrite_arbitrary_json(tmp_path) -> None:
    context_path = tmp_path / "operator-settings.json"
    context_path.write_text('{"keep":true}', encoding="utf-8")
    config_path = tmp_path / "intraday.yaml"
    _write_intraday_config(config_path, news_context_path=context_path)

    snapshot, _ = _run(tmp_path, FakeTransport(_successful_responses()))

    assert snapshot.ready is True
    assert context_path.read_text(encoding="utf-8") == '{"keep":true}'


def test_intraday_news_context_is_monotonic_across_delayed_iterations(tmp_path) -> None:
    context_path = tmp_path / "news-context.json"
    config_path = tmp_path / "intraday.yaml"
    _write_intraday_config(config_path, news_context_path=context_path)
    _run(tmp_path, FakeTransport(_successful_responses()), at=NOW)
    later = NOW + timedelta(minutes=1)
    early_close = _calendar(regular_close="2026-08-29T02:00:00+09:00")
    _run(tmp_path, FakeTransport([_token(), early_close]), at=later)
    expected = context_path.read_text(encoding="utf-8")

    _run(tmp_path, FakeTransport([_token(), _calendar()]), at=NOW)

    assert context_path.read_text(encoding="utf-8") == expected
    assert json.loads(expected)["active_until"] == "2026-08-29T02:00:00+09:00"


def test_intraday_news_context_is_not_rewritten_after_regular_close(tmp_path) -> None:
    context_path = tmp_path / "news-context.json"
    config_path = tmp_path / "intraday.yaml"
    _write_intraday_config(config_path, news_context_path=context_path)
    _run(tmp_path, FakeTransport(_successful_responses()), at=NOW)
    expected = context_path.read_bytes()

    _run(
        tmp_path,
        FakeTransport([_token(), _calendar()]),
        at=datetime(2026, 8, 28, 20, 1, tzinfo=timezone.utc),
    )

    assert context_path.read_bytes() == expected


def test_intraday_news_context_rejects_second_symbol_writer(tmp_path) -> None:
    class RecordingStore:
        def __init__(self) -> None:
            self.events: list[dict[str, Any]] = []

        def list_runtime_events(self, *, limit):
            return list(self.events)

        def record_runtime_event(self, level, message, payload):
            self.events.append({"message": message, "payload": dict(payload)})

    context_path = tmp_path / "news-context.json"
    config_path = tmp_path / "intraday.yaml"
    _write_intraday_config(config_path, news_context_path=context_path)
    _run(tmp_path, FakeTransport(_successful_responses()))
    expected = context_path.read_text(encoding="utf-8")
    store = RecordingStore()
    config = SimpleNamespace(
        intraday=SimpleNamespace(
            news_context_path=str(context_path),
            force_exit_minutes_before_close=15,
        )
    )

    _refresh_intraday_news_context(
        config=config,
        record={
            "session_date": date(2026, 8, 28),
            "symbol": "MSFT",
            "payload": {
                "symbol": "MSFT",
                "force_exit_at": "2026-08-29T04:45:00+09:00",
                "regular_close": "2026-08-29T05:00:00+09:00",
            },
        },
        store=store,
        at=NOW + timedelta(minutes=1),
        current_regular_close=datetime(
            2026, 8, 29, 5, 0, tzinfo=timezone(timedelta(hours=9))
        ),
    )

    assert context_path.read_text(encoding="utf-8") == expected
    assert store.events[0]["payload"]["code"] == "news_context_writer_collision"


@pytest.mark.parametrize("failure_point", ["list", "record"])
def test_intraday_news_context_diagnostic_failure_never_escapes(
    tmp_path, failure_point: str
) -> None:
    class ExplodingStore:
        def list_runtime_events(self, *, limit):
            if failure_point == "list":
                raise RuntimeError("diagnostic storage unavailable")
            return []

        def record_runtime_event(self, level, message, payload):
            raise RuntimeError("diagnostic storage unavailable")

    config = SimpleNamespace(
        intraday=SimpleNamespace(
            news_context_path=str(tmp_path / "wrong-name.json"),
            force_exit_minutes_before_close=15,
        )
    )
    record = {
        "session_date": date(2026, 8, 28),
        "symbol": "AAPL",
        "payload": {
            "symbol": "AAPL",
            "force_exit_at": "2026-08-29T04:45:00+09:00",
            "regular_close": "2026-08-29T05:00:00+09:00",
        },
    }

    _refresh_intraday_news_context(
        config=config,
        record=record,
        store=ExplodingStore(),
        at=NOW,
        current_regular_close=datetime(
            2026, 8, 29, 5, 0, tzinfo=timezone(timedelta(hours=9))
        ),
    )


def test_intraday_restart_reuses_daily_plan_without_duplicate_alert(tmp_path) -> None:
    first = FakeTransport(_successful_responses())
    _run(tmp_path, first)
    second = FakeTransport([_token(), _calendar()])

    snapshot, state_db = _run(tmp_path, second)

    assert snapshot.ready is True
    assert len(second.requests) == 2
    with SQLiteStateStore(state_db) as store:
        events = store.list_runtime_events(limit=50)
        assert len(store.list_intraday_plans()) == 1
    assert [event["message"] for event in events].count(
        "intraday_shadow_plan_created"
    ) == 1


def test_intraday_context_never_outlives_current_early_close(tmp_path) -> None:
    context_path = tmp_path / "news-context.json"
    config_path = tmp_path / "intraday.yaml"
    _write_intraday_config(config_path, news_context_path=context_path)
    _run(tmp_path, FakeTransport(_successful_responses()))
    early_close = _calendar(regular_close="2026-08-29T02:00:00+09:00")

    snapshot, _ = _run(tmp_path, FakeTransport([_token(), early_close]))

    context = json.loads(context_path.read_text(encoding="utf-8"))
    assert snapshot.ready is True
    assert context["active_until"] == "2026-08-29T02:00:00+09:00"


def test_intraday_notification_outbox_retries_after_restart(
    tmp_path, monkeypatch
) -> None:
    sent: list[dict[str, Any]] = []

    def flaky_send(url: str, body: bytes, timeout: float) -> None:
        sent.append(json.loads(body))
        if len(sent) == 1:
            raise RuntimeError("Authorization: Bearer SUPERSECRET")

    monkeypatch.setattr(
        DiscordTradeNotifier,
        "_post_json",
        staticmethod(flaky_send),
    )
    monkeypatch.setattr(
        DiscordTradeNotifier,
        "_fetch_webhook_channel_id",
        staticmethod(lambda _url, _timeout: "123456789012345678"),
    )
    webhook_env = {
        "DISCORD_TRADE_ALERT_WEBHOOK_URL": "https://discord.com/api/webhooks/123/token",
        "DISCORD_ALLOWED_CHANNEL_ID": "123456789012345678",
    }

    first_snapshot, state_db = _run(
        tmp_path,
        FakeTransport(_successful_responses()),
        env=webhook_env,
    )
    with SQLiteStateStore(state_db) as store:
        first_outbox = store.list_notification_outbox()
        stored_text = json.dumps(
            {
                "outbox": first_outbox,
                "events": store.list_runtime_events(limit=100),
            },
            default=str,
        )

    second_snapshot, _ = _run(
        tmp_path,
        FakeTransport([_token(), _calendar()]),
        env=webhook_env,
    )
    with SQLiteStateStore(state_db) as store:
        final_outbox = store.list_notification_outbox()

    assert first_snapshot.ready is True
    assert second_snapshot.ready is True
    assert first_outbox[0]["status"] == "PENDING"
    assert first_outbox[0]["last_error_code"] == "discord_send_failed"
    assert final_outbox[0]["status"] == "SENT"
    assert final_outbox[0]["attempt_count"] == 2
    assert len(sent) == 2
    assert "SUPERSECRET" not in stored_text


def test_intraday_generic_failure_never_exposes_exception_text(
    tmp_path, monkeypatch
) -> None:
    bodies: list[bytes] = []
    monkeypatch.setattr(
        DiscordTradeNotifier,
        "_post_json",
        staticmethod(lambda url, body, timeout: bodies.append(body)),
    )
    monkeypatch.setattr(
        DiscordTradeNotifier,
        "_fetch_webhook_channel_id",
        staticmethod(lambda _url, _timeout: "123456789012345678"),
    )
    canary = "Authorization: Bearer SUPERSECRET"

    snapshot, state_db = _run(
        tmp_path,
        FakeTransport([_token(), RuntimeError(canary)]),
        env={
            "DISCORD_TRADE_ALERT_WEBHOOK_URL": "https://discord.com/api/webhooks/123/token",
            "DISCORD_ALLOWED_CHANNEL_ID": "123456789012345678",
        },
    )
    with SQLiteStateStore(state_db) as store:
        stored_text = json.dumps(
            {
                "outbox": store.list_notification_outbox(),
                "events": store.list_runtime_events(limit=100),
            },
            default=str,
        )

    assert snapshot.blockers == ("intraday_read_or_integrity_failure",)
    assert bodies
    assert canary not in bodies[0].decode("utf-8")
    assert "SUPERSECRET" not in stored_text


def test_intraday_exception_diagnostic_allows_only_safe_error_code() -> None:
    safe = operations._intraday_exception_diagnostic(
        operations.TossApiError(
            401,
            code="invalid_client",
            message="Authorization: Bearer SUPERSECRET",
        )
    )
    unsafe = operations._intraday_exception_diagnostic(
        operations.TossApiError(
            401,
            code="invalid_client SUPERSECRET",
            message="Authorization: Bearer SUPERSECRET",
        )
    )

    assert safe == {
        "exception_type": "TossApiError",
        "http_status": 401,
        "error_code": "invalid_client",
    }
    assert unsafe == {"exception_type": "TossApiError", "http_status": 401}
    assert "SUPERSECRET" not in json.dumps([safe, unsafe])


def test_intraday_after_open_stays_blocked_from_execution_and_places_no_order(tmp_path) -> None:
    _run(tmp_path, FakeTransport(_successful_responses()))
    after_open = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)
    transport = FakeTransport([_token(), _calendar()])

    snapshot, _ = _run(tmp_path, transport, at=after_open)

    assert snapshot.ready is False
    assert snapshot.blockers == ("intraday_execution_engine_not_enabled",)
    assert len(transport.requests) == 2
    assert all(
        request.method == "GET" or request.url.endswith("/oauth2/token")
        for request in transport.requests
    )


def test_intraday_daily_plan_cannot_be_repriced_after_config_change(tmp_path) -> None:
    _run(tmp_path, FakeTransport(_successful_responses()))
    config_path = tmp_path / "intraday.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "take_profit_fraction: 0.02", "take_profit_fraction: 0.03"
        ),
        encoding="utf-8",
    )
    transport = FakeTransport([_token(), _calendar()])

    snapshot, state_db = _run(tmp_path, transport)

    assert snapshot.blockers == ("intraday_daily_plan_locked_config_changed",)
    with SQLiteStateStore(state_db) as store:
        plans = store.list_intraday_plans()
    assert len(plans) == 1
    assert plans[0]["payload"]["take_profit_fraction"] == "0.02"


def test_intraday_stale_quote_blocks_plan_and_deduplicates_blocker(tmp_path) -> None:
    stale = "2026-08-28T12:00:00+00:00"
    first = FakeTransport(_successful_responses(price_timestamp=stale))
    snapshot, state_db = _run(tmp_path, first)
    second = FakeTransport(_successful_responses(price_timestamp=stale))
    second_snapshot, _ = _run(tmp_path, second)

    assert snapshot.blockers == ("intraday_price_stale",)
    assert second_snapshot.blockers == ("intraday_price_stale",)
    with SQLiteStateStore(state_db) as store:
        assert store.list_intraday_plans() == []
        blocked = [
            event
            for event in store.list_runtime_events(limit=100)
            if event["message"] == "intraday_shadow_plan_blocked"
        ]
    assert len(blocked) == 1


def test_intraday_live_configuration_is_blocked_before_network(tmp_path) -> None:
    config_path = tmp_path / "live.yaml"
    _write_intraday_config(
        config_path,
        runtime_mode="live",
        live_enabled=True,
        live_execution_enabled=True,
    )
    transport = FakeTransport([])

    snapshot, state_db = _run(
        tmp_path,
        transport,
        config_name="live.yaml",
    )

    assert snapshot.ready is False
    assert any("intraday is shadow-only" in blocker for blocker in snapshot.blockers)
    assert transport.requests == []
    with SQLiteStateStore(state_db) as store:
        assert store.list_intraday_plans() == []


def test_intraday_waits_before_plan_window_without_account_reads(tmp_path) -> None:
    transport = FakeTransport([_token(), _calendar()])
    before_window = NOW - timedelta(hours=1)

    snapshot, state_db = _run(tmp_path, transport, at=before_window)

    assert snapshot.blockers == ("intraday_plan_window_not_started",)
    assert len(transport.requests) == 2
    with SQLiteStateStore(state_db) as store:
        assert store.list_intraday_plans() == []


def test_intraday_holiday_and_missed_deadline_are_terminal_for_the_day(tmp_path) -> None:
    holiday = FakeTransport([_token(), _calendar(holiday=True)])
    holiday_snapshot, _ = _run(tmp_path / "holiday", holiday)
    missed_at = datetime(2026, 8, 28, 13, 20, tzinfo=timezone.utc)
    missed = FakeTransport([_token(), _calendar()])
    missed_snapshot, _ = _run(tmp_path / "missed", missed, at=missed_at)

    assert holiday_snapshot.blockers == ("intraday_market_holiday",)
    assert missed_snapshot.blockers == ("intraday_plan_deadline_missed",)
    assert len(holiday.requests) == 2
    assert len(missed.requests) == 2


@pytest.mark.parametrize("missing", ["preMarket", "regularMarket"])
def test_intraday_calendar_requires_explicit_null_session_fields_for_holiday(
    missing: str,
) -> None:
    today = {
        "date": "2026-08-28",
        "preMarket": None,
        "regularMarket": None,
    }
    del today[missing]

    with pytest.raises(IntradayPlanBlocked) as exc:
        operations._strict_intraday_schedule(
            {"today": today}, expected_date=date(2026, 8, 28)
        )

    assert exc.value.code == "intraday_calendar_malformed"


def test_intraday_plan_window_deadline_is_inclusive(tmp_path) -> None:
    deadline = datetime(2026, 8, 28, 13, 15, tzinfo=timezone.utc)
    transport = FakeTransport(
        _successful_responses(
            price_timestamp="2026-08-28T13:14:55+00:00",
            book_timestamp="2026-08-28T13:14:56+00:00",
        )
    )

    snapshot, state_db = _run(tmp_path, transport, at=deadline)

    assert snapshot.ready is True
    with SQLiteStateStore(state_db) as store:
        assert len(store.list_intraday_plans()) == 1


def test_intraday_uses_official_early_close_for_force_exit(tmp_path) -> None:
    responses = _successful_responses()
    responses[1] = _calendar(regular_close="2026-08-29T02:00:00+09:00")

    snapshot, state_db = _run(tmp_path, FakeTransport(responses))

    assert snapshot.ready is True
    with SQLiteStateStore(state_db) as store:
        payload = store.list_intraday_plans()[0]["payload"]
    assert payload["regular_close"] == "2026-08-29T02:00:00+09:00"
    assert payload["force_exit_at"] == "2026-08-29T01:45:00+09:00"


def test_intraday_wide_or_crossed_orderbook_blocks_without_plan(tmp_path) -> None:
    wide = FakeTransport(
        _successful_responses(
            bids=[{"price": "99", "volume": "10"}],
            asks=[{"price": "101", "volume": "10"}],
        )
    )
    wide_snapshot, _ = _run(tmp_path / "wide", wide)
    crossed = FakeTransport(
        _successful_responses(
            bids=[{"price": "100.05", "volume": "10"}],
            asks=[{"price": "100.02", "volume": "10"}],
        )
    )
    crossed_snapshot, _ = _run(tmp_path / "crossed", crossed)

    assert wide_snapshot.blockers == ("intraday_spread_too_wide",)
    assert crossed_snapshot.blockers == ("intraday_orderbook_crossed",)


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("future_price", "intraday_price_from_future"),
        ("timestamp_skew", "intraday_quote_orderbook_skew"),
        ("empty_ask", "intraday_orderbook_empty"),
        ("zero_volume", "intraday_decimal_malformed"),
        ("wrong_price_currency", "intraday_quote_currency_mismatch"),
        ("wrong_book_currency", "intraday_orderbook_currency_mismatch"),
    ],
)
def test_intraday_quote_and_orderbook_fail_closed(
    tmp_path, case: str, expected: str
) -> None:
    responses = _successful_responses()
    if case == "future_price":
        responses[7].payload["result"][0]["timestamp"] = "2026-08-28T12:30:01+00:00"
    elif case == "timestamp_skew":
        responses[8].payload["result"]["timestamp"] = "2026-08-28T12:29:50+00:00"
    elif case == "empty_ask":
        responses[8].payload["result"]["asks"] = []
    elif case == "zero_volume":
        responses[8].payload["result"]["asks"][0]["volume"] = "0"
    elif case == "wrong_price_currency":
        responses[7].payload["result"][0]["currency"] = "KRW"
    elif case == "wrong_book_currency":
        responses[8].payload["result"]["currency"] = "KRW"

    snapshot, state_db = _run(tmp_path, FakeTransport(responses))

    assert snapshot.blockers == (expected,)
    with SQLiteStateStore(state_db) as store:
        assert store.list_intraday_plans() == []


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("holding", "intraday_account_not_flat"),
        ("open_order", "intraday_open_order_exists"),
        ("conditional", "intraday_conditional_order_exists"),
    ],
)
def test_intraday_existing_account_activity_blocks_plan(
    tmp_path, case: str, expected: str
) -> None:
    responses = _successful_responses()
    if case == "holding":
        responses[2].payload["result"]["items"] = [
            {"symbol": "SPY", "quantity": "1"}
        ]
        responses = responses[:3]
    elif case == "open_order":
        responses[3].payload["result"]["orders"] = [
            {"orderId": "order-1", "symbol": "SPY", "quantity": "1"}
        ]
        responses = responses[:4]
    elif case == "conditional":
        responses[4].payload["result"]["conditionalOrders"] = [
            {"conditionalOrderId": "conditional-1"}
        ]
        responses = responses[:5]

    snapshot, _ = _run(tmp_path, FakeTransport(responses))

    assert snapshot.blockers == (expected,)


@pytest.mark.parametrize(
    ("has_next", "expected"),
    [
        ("missing", "intraday_orders_malformed"),
        (None, "intraday_orders_malformed"),
        ("false", "intraday_orders_malformed"),
        (True, "intraday_open_order_exists"),
    ],
)
def test_intraday_open_order_pagination_fails_closed(
    tmp_path, has_next: object, expected: str
) -> None:
    responses = _successful_responses()
    result = responses[3].payload["result"]
    if has_next == "missing":
        result.pop("hasNext")
    else:
        result["hasNext"] = has_next

    snapshot, _ = _run(tmp_path, FakeTransport(responses[:4]))

    assert snapshot.blockers == (expected,)


def test_intraday_configured_cost_must_cover_both_commission_legs(tmp_path) -> None:
    responses = _successful_responses()
    responses[6].payload["result"][0]["commissionRate"] = "0.002"
    transport = FakeTransport(responses[:7])

    snapshot, state_db = _run(tmp_path, transport)

    assert snapshot.blockers == ("intraday_cost_buffer_below_commission",)
    with SQLiteStateStore(state_db) as store:
        assert store.list_intraday_plans() == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"currency": "KRW", "cashBuyingPower": "1000"},
        {"currency": "USD", "cashBuyingPower": "0"},
        {"currency": "USD", "cashBuyingPower": "NaN"},
        {"currency": "USD", "cashBuyingPower": "Infinity"},
    ],
)
def test_intraday_cash_parser_fails_closed(payload: Mapping[str, Any]) -> None:
    with pytest.raises(IntradayPlanBlocked):
        _strict_intraday_cash(payload)


def _maintenance_test_config(paper_db: Path) -> SimpleNamespace:
    return SimpleNamespace(
        intraday=SimpleNamespace(
            simulation_db_path=str(paper_db),
            news_context_path=None,
        )
    )


def _create_maintenance_test_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE sample(value TEXT NOT NULL)")
        connection.execute("INSERT INTO sample VALUES ('row')")


@pytest.mark.parametrize("crash_stage", ["after_enqueue", "after_mark"])
def test_paper_alert_forwarding_invalidates_before_cross_database_writes_and_rebacks_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_stage: str,
) -> None:
    state_db = tmp_path / "state.sqlite3"
    paper_db = tmp_path / "paper.sqlite3"
    logs = tmp_path / "logs"
    logs.mkdir()
    _create_maintenance_test_database(paper_db)
    session = date(2026, 8, 28)

    class CrashingPaper:
        mark_calls = 0
        forwarded = False

        def list_alerts(self, *, pending_only: bool):
            assert pending_only is True
            return [
                {
                    "alert_id": "alert-crash",
                    "plan_id": "plan-crash",
                    "event": "entry_filled",
                    "level": "info",
                    "payload": {
                        "session_date": session.isoformat(),
                        "price": "100",
                    },
                    "created_at": NOW.isoformat(),
                }
            ]

        def daily_summary(self, requested):
            assert str(requested) == session.isoformat()
            return {
                "run_id": "run-crash",
                "plan_id": "plan-crash",
                "session_date": session.isoformat(),
                "status": "OPEN",
            }

        def summary(self, *, as_of):
            assert as_of == NOW
            return {"days": []}

        def mark_alert_forwarded(self, alert_id: str, *, forwarded_at: datetime):
            assert alert_id == "alert-crash"
            assert forwarded_at == NOW
            self.mark_calls += 1
            self.forwarded = True
            if crash_stage == "after_mark":
                raise RuntimeError("synthetic crash after paper commit")
            return True

    paper = CrashingPaper()
    real_enqueue = SQLiteStateStore.enqueue_notification_once

    def enqueue_then_crash(self, **kwargs):
        result = real_enqueue(self, **kwargs)
        if (
            crash_stage == "after_enqueue"
            and kwargs.get("message") == "intraday_paper_entry_filled"
        ):
            raise RuntimeError("synthetic crash after planner commit")
        return result

    monkeypatch.setattr(
        SQLiteStateStore, "enqueue_notification_once", enqueue_then_crash
    )
    with SQLiteStateStore(state_db) as store:
        store.record_runtime_event(
            "INFO",
            "intraday_backup_completed",
            {
                "session_date": session.isoformat(),
                "databases": ["planner", "paper"],
            },
        )
        with pytest.raises(RuntimeError, match="synthetic crash"):
            operations._forward_intraday_paper_alerts(
                paper_store=paper,
                store=store,
                at=NOW,
            )
        events = store.list_runtime_events_for_messages(
            ("intraday_backup_completed", "intraday_backup_invalidated")
        )
        completion_id = max(
            event["id"]
            for event in events
            if event["message"] == "intraday_backup_completed"
        )
        invalidation_id = max(
            event["id"]
            for event in events
            if event["message"] == "intraday_backup_invalidated"
        )
        assert invalidation_id > completion_id
        assert any(
            item["message"] == "intraday_paper_entry_filled"
            for item in store.list_notification_outbox()
        )

        operations._run_intraday_post_close_maintenance(
            config=_maintenance_test_config(paper_db),
            store=store,
            state_db=state_db,
            log_dir=logs,
            session_date=session,
            at=NOW + timedelta(minutes=1),
        )
        recovered_events = store.list_runtime_events_for_messages(
            ("intraday_backup_completed", "intraday_backup_invalidated")
        )

    assert paper.mark_calls == (0 if crash_stage == "after_enqueue" else 1)
    assert paper.forwarded is (crash_stage == "after_mark")
    assert max(
        event["id"]
        for event in recovered_events
        if event["message"] == "intraday_backup_completed"
    ) > max(
        event["id"]
        for event in recovered_events
        if event["message"] == "intraday_backup_invalidated"
    )


def test_post_close_maintenance_phases_run_once_per_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_db = tmp_path / "state.sqlite3"
    paper_db = tmp_path / "paper.sqlite3"
    logs = tmp_path / "logs"
    logs.mkdir()
    _create_maintenance_test_database(paper_db)
    config = _maintenance_test_config(paper_db)
    backup_calls: list[str] = []
    real_backup = operations.backup_sqlite

    def tracked_backup(source, destination, **kwargs):
        backup_calls.append(Path(destination).name)
        return real_backup(source, destination, **kwargs)

    monkeypatch.setattr(operations, "backup_sqlite", tracked_backup)
    with SQLiteStateStore(state_db) as store:
        for _ in range(2):
            operations._run_intraday_post_close_maintenance(
                config=config,
                store=store,
                state_db=state_db,
                log_dir=logs,
                session_date=date(2026, 8, 28),
                at=NOW,
            )
        messages = [event["message"] for event in store.list_runtime_events()]

    assert sorted(backup_calls) == [
        "paper-2026-08-28.sqlite3",
        "planner-2026-08-28.sqlite3",
    ]
    for message in (
        "intraday_backup_completed",
        "intraday_backup_retention_completed",
        "intraday_log_rotation_completed",
        "intraday_disk_check_completed",
    ):
        assert messages.count(message) == 1


def test_completed_backup_missing_both_files_is_invalidated_and_rebuilt_only_for_current_session(
    tmp_path: Path,
) -> None:
    state_db = tmp_path / "state.sqlite3"
    paper_db = tmp_path / "paper.sqlite3"
    logs = tmp_path / "logs"
    logs.mkdir()
    _create_maintenance_test_database(paper_db)
    config = _maintenance_test_config(paper_db)
    session = date(2026, 8, 28)

    with SQLiteStateStore(state_db) as store:
        operations._run_intraday_post_close_maintenance(
            config=config,
            store=store,
            state_db=state_db,
            log_dir=logs,
            session_date=session,
            at=NOW,
        )
        destination = tmp_path / "backups" / "planner-2026-08-28.sqlite3"
        destination.unlink()
        operations.sha256_manifest_path(destination).unlink()

        operations._run_intraday_post_close_maintenance(
            config=config,
            store=store,
            state_db=state_db,
            log_dir=logs,
            session_date=session,
            at=NOW + timedelta(minutes=1),
        )
        events = store.list_runtime_events_for_messages(
            ("intraday_backup_completed", "intraday_backup_invalidated")
        )
        outbox = store.list_notification_outbox()

    assert destination.exists()
    assert operations.sha256_manifest_path(destination).exists()
    assert sum(
        event["message"] == "intraday_backup_completed" for event in events
    ) == 2
    assert any(
        event["message"] == "intraday_backup_invalidated"
        and event["payload"]["session_date"] == session.isoformat()
        and event["payload"]["database"] == "planner"
        for event in events
    )
    assert any(item["message"] == "intraday_backup_invalidated" for item in outbox)


def test_logical_paper_invalidation_replaces_the_whole_backup_generation(
    tmp_path: Path,
) -> None:
    state_db = tmp_path / "state.sqlite3"
    paper_db = tmp_path / "paper.sqlite3"
    logs = tmp_path / "logs"
    logs.mkdir()
    _create_maintenance_test_database(paper_db)
    config = _maintenance_test_config(paper_db)
    session = date(2026, 8, 28)
    backup_root = tmp_path / "backups"

    with SQLiteStateStore(state_db) as store:
        operations._run_intraday_post_close_maintenance(
            config=config,
            store=store,
            state_db=state_db,
            log_dir=logs,
            session_date=session,
            at=NOW,
        )
        first_completion = max(
            event["id"]
            for event in store.list_runtime_events_for_messages(
                ("intraday_backup_completed",)
            )
        )
        with sqlite3.connect(paper_db) as connection:
            connection.execute("INSERT INTO sample VALUES ('reconciled-row')")
            connection.commit()
        operations._invalidate_completed_paper_backup_if_any(
            store=store,
            session_date=session,
            at=NOW + timedelta(seconds=1),
        )

        operations._run_intraday_post_close_maintenance(
            config=config,
            store=store,
            state_db=state_db,
            log_dir=logs,
            session_date=session,
            at=NOW + timedelta(minutes=1),
        )
        second_completion = max(
            event["id"]
            for event in store.list_runtime_events_for_messages(
                ("intraday_backup_completed",)
            )
        )
        operations._invalidate_completed_paper_backup_if_any(
            store=store,
            session_date=session,
            at=NOW + timedelta(minutes=1, seconds=1),
        )
        invalidation_alerts = [
            item
            for item in store.list_notification_outbox()
            if item["message"] == "intraday_backup_invalidated"
            and item["payload"].get("database") == "paper"
        ]

    assert second_completion > first_completion
    with sqlite3.connect(
        backup_root / "paper-2026-08-28.sqlite3"
    ) as snapshot:
        assert snapshot.execute("SELECT value FROM sample ORDER BY rowid").fetchall() == [
            ("row",),
            ("reconciled-row",),
        ]
    for alias in ("planner", "paper"):
        destination = backup_root / f"{alias}-2026-08-28.sqlite3"
        assert len(list(backup_root.glob(f".{destination.name}.quarantine-*"))) == 1
        assert len(
            list(
                backup_root.glob(
                    f".{operations.sha256_manifest_path(destination).name}.quarantine-*"
                )
            )
        ) == 1
    assert {
        item["payload"]["backup_generation_id"]
        for item in invalidation_alerts
    } == {first_completion, second_completion}
    assert len({item["notification_key"] for item in invalidation_alerts}) == 2


def test_paper_backlog_invalidates_before_cross_database_materialization() -> None:
    session = date(2026, 8, 28)
    record = {
        "plan_id": "planner-plan",
        "account_key": "simulation-account",
        "session_date": session,
    }

    class Planner:
        def __init__(self) -> None:
            self.events = [
                {
                    "id": 41,
                    "message": "intraday_backup_completed",
                    "payload": {
                        "session_date": session.isoformat(),
                        "databases": ["planner", "paper"],
                    },
                }
            ]

        def list_intraday_plans(self):
            return [record]

        def list_runtime_events_for_messages(self, _messages):
            return list(self.events)

        def record_runtime_event(self, level, message, payload):
            self.events.append(
                {
                    "id": 42,
                    "level": level,
                    "message": message,
                    "payload": dict(payload),
                }
            )

        def enqueue_notification_once(self, **_kwargs):
            return True

    planner = Planner()

    class CrashingPaper:
        account_key = "simulation-account"

        def summary(self, *, as_of):
            assert as_of == NOW
            return {"days": []}

        def ensure_plan(self, _record, *, registered_at):
            assert registered_at is None
            assert planner.events[-1]["message"] == "intraday_backup_invalidated"
            raise RuntimeError("synthetic crash after write-ahead fence")

    with pytest.raises(RuntimeError, match="synthetic crash"):
        operations._reconcile_intraday_paper_backlog(
            paper_store=CrashingPaper(),
            store=planner,
            at=NOW,
        )

    assert planner.events[-1]["payload"] == {
        "session_date": session.isoformat(),
        "database": "paper",
        "code": "paper_ledger_reconciled",
        "backup_generation_id": 41,
    }


def test_untrusted_backup_root_temporarily_blocks_validation_then_recovers(
    tmp_path: Path,
) -> None:
    state_db = tmp_path / "state.sqlite3"
    paper_db = tmp_path / "paper.sqlite3"
    logs = tmp_path / "logs"
    logs.mkdir()
    _create_maintenance_test_database(paper_db)
    config = _maintenance_test_config(paper_db)
    session = date(2026, 8, 28)

    with SQLiteStateStore(state_db) as store:
        operations._run_intraday_post_close_maintenance(
            config=config,
            store=store,
            state_db=state_db,
            log_dir=logs,
            session_date=session,
            at=NOW,
        )
        backup_root = tmp_path / "backups"
        for artifact in tuple(backup_root.iterdir()):
            artifact.unlink()
        backup_root.rmdir()
        backup_root.write_text("not-a-directory", encoding="utf-8")

        operations._run_intraday_post_close_maintenance(
            config=config,
            store=store,
            state_db=state_db,
            log_dir=logs,
            session_date=session,
            at=NOW + timedelta(minutes=1),
        )
        events = store.list_runtime_events_for_messages(
            (
                "intraday_backup_invalidated",
                "intraday_backup_validation_failed",
                "intraday_backup_validation_completed",
                "intraday_maintenance_failed",
            )
        )
        assert not any(
            event["message"] == "intraday_backup_invalidated"
            for event in events
        )
        assert any(
            event["message"] == "intraday_backup_validation_failed"
            for event in events
        )
        assert any(
            event["message"] == "intraday_maintenance_failed"
            and event["payload"]["code"] == "backup_directory_invalid"
            for event in events
        )

        backup_root.unlink()
        backup_root.mkdir()
        operations._run_intraday_post_close_maintenance(
            config=config,
            store=store,
            state_db=state_db,
            log_dir=logs,
            session_date=session,
            at=NOW + timedelta(minutes=2),
        )
        recovered = store.list_runtime_events_for_messages(
            (
                "intraday_backup_invalidated",
                "intraday_backup_validation_completed",
            )
        )

    assert {
        event["payload"]["database"]
        for event in recovered
        if event["message"] == "intraday_backup_invalidated"
    } == {"planner", "paper"}
    assert any(
        event["message"] == "intraday_backup_validation_completed"
        for event in recovered
    )


def test_maintenance_event_ledger_failure_preserves_generation_and_blocks_catchup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_db = tmp_path / "state.sqlite3"
    paper_db = tmp_path / "paper.sqlite3"
    logs = tmp_path / "logs"
    logs.mkdir()
    _create_maintenance_test_database(paper_db)
    config = _maintenance_test_config(paper_db)
    session = date(2026, 8, 28)

    with SQLiteStateStore(state_db) as store:
        assert operations._run_intraday_post_close_maintenance(
            config=config,
            store=store,
            state_db=state_db,
            log_dir=logs,
            session_date=session,
            at=NOW,
        ) is True
        backup_root = tmp_path / "backups"
        before = {
            path.name: path.read_bytes()
            for path in backup_root.iterdir()
            if path.is_file()
        }
        real_list = SQLiteStateStore.list_runtime_events_for_messages
        reads = 0

        def fail_first_event_ledger_read(self, messages):
            nonlocal reads
            reads += 1
            if reads == 1:
                raise sqlite3.OperationalError("synthetic event ledger read failure")
            return real_list(self, messages)

        def forbidden_backup_call(*_args, **_kwargs):
            pytest.fail("backup artifacts cannot be touched without the event ledger")

        monkeypatch.setattr(
            SQLiteStateStore,
            "list_runtime_events_for_messages",
            fail_first_event_ledger_read,
        )
        for name in (
            "backup_sqlite",
            "finish_backup_retention_removal",
            "quarantine_invalid_sqlite_backup",
            "prune_backup_retention",
        ):
            monkeypatch.setattr(operations, name, forbidden_backup_call)

        class QuiescentPaper:
            def summary(self, *, as_of):
                assert as_of == NOW + timedelta(days=3)
                return {"coverage": {"covered": [session.isoformat()]}}

            def run_is_quiescent_for_backup(self):
                return True

        with pytest.raises(IntradayPlanBlocked) as blocked:
            operations._run_intraday_maintenance_catchup(
                config=config,
                paper_stores=(QuiescentPaper(),),
                store=store,
                state_db=state_db,
                log_dir=logs,
                before=date(2026, 8, 31),
                at=NOW + timedelta(days=3),
            )

        assert blocked.value.code == "intraday_backup_validation_incomplete"
        assert reads == 1
        assert {
            path.name: path.read_bytes()
            for path in backup_root.iterdir()
            if path.is_file()
        } == before
        events = real_list(
            store,
            (
                "intraday_backup_completed",
                "intraday_backup_invalidated",
                "intraday_backup_validation_failed",
            ),
        )

    assert sum(
        event["message"] == "intraday_backup_completed" for event in events
    ) == 1
    assert not any(
        event["message"] == "intraday_backup_invalidated" for event in events
    )
    assert any(
        event["message"] == "intraday_backup_validation_failed"
        and event["payload"]["code"] == "backup_event_ledger_read_failed"
        for event in events
    )


def test_post_close_backup_failure_does_not_skip_disk_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_db = tmp_path / "state.sqlite3"
    paper_db = tmp_path / "paper.sqlite3"
    logs = tmp_path / "logs"
    logs.mkdir()
    _create_maintenance_test_database(paper_db)
    disk_calls: list[Path] = []

    def fail_backup(_source, _destination, **_kwargs):
        raise operations.MaintenanceError("forced_backup_failure")

    def checked_disk(path):
        disk_calls.append(Path(path))
        return SimpleNamespace(
            level="ok",
            free_bytes=20 * 1024**3,
            free_fraction=0.5,
        )

    monkeypatch.setattr(operations, "backup_sqlite", fail_backup)
    monkeypatch.setattr(operations, "check_disk_space", checked_disk)
    with SQLiteStateStore(state_db) as store:
        operations._run_intraday_post_close_maintenance(
            config=_maintenance_test_config(paper_db),
            store=store,
            state_db=state_db,
            log_dir=logs,
            session_date=date(2026, 8, 28),
            at=NOW,
        )
        events = store.list_runtime_events()
        outbox = store.list_notification_outbox()

    assert disk_calls == [tmp_path]
    assert any(
        event["message"] == "intraday_maintenance_failed"
        and event["payload"]["code"] == "forced_backup_failure"
        for event in events
    )
    assert any(event["message"] == "intraday_disk_check_completed" for event in events)
    assert any(
        item["message"] == "intraday_maintenance_failure"
        and item["level"] == "error"
        and item["payload"] == {
            "session_date": "2026-08-28",
            "code": "forced_backup_failure",
        }
        for item in outbox
    )


def test_partial_backup_generation_never_runs_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_db = tmp_path / "state.sqlite3"
    paper_db = tmp_path / "paper.sqlite3"
    logs = tmp_path / "logs"
    logs.mkdir()
    _create_maintenance_test_database(paper_db)
    real_backup = operations.backup_sqlite
    calls: list[str] = []

    def fail_second_source(source, destination, **kwargs):
        alias = Path(destination).name.split("-", 1)[0]
        calls.append(alias)
        if alias == "paper":
            raise operations.MaintenanceError("forced_paper_backup_failure")
        return real_backup(source, destination, **kwargs)

    monkeypatch.setattr(operations, "backup_sqlite", fail_second_source)
    monkeypatch.setattr(
        operations,
        "prune_backup_retention",
        lambda *_args, **_kwargs: pytest.fail(
            "retention cannot run for a partial backup generation"
        ),
    )
    with SQLiteStateStore(state_db) as store:
        operations._run_intraday_post_close_maintenance(
            config=_maintenance_test_config(paper_db),
            store=store,
            state_db=state_db,
            log_dir=logs,
            session_date=date(2026, 8, 28),
            at=NOW,
        )
        messages = [event["message"] for event in store.list_runtime_events()]

    assert calls == ["planner", "paper"]
    assert "intraday_backup_completed" not in messages
    assert "intraday_backup_retention_completed" not in messages
    assert "intraday_log_rotation_completed" in messages
    assert "intraday_disk_check_completed" in messages


def test_backup_completion_must_commit_before_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_db = tmp_path / "state.sqlite3"
    paper_db = tmp_path / "paper.sqlite3"
    logs = tmp_path / "logs"
    logs.mkdir()
    _create_maintenance_test_database(paper_db)
    real_record = SQLiteStateStore.record_runtime_event

    def reject_completion(self, level, message, payload):
        if message == "intraday_backup_completed":
            raise sqlite3.OperationalError("synthetic completion failure")
        return real_record(self, level, message, payload)

    monkeypatch.setattr(
        SQLiteStateStore, "record_runtime_event", reject_completion
    )
    monkeypatch.setattr(
        operations,
        "prune_backup_retention",
        lambda *_args, **_kwargs: pytest.fail(
            "retention requires a durable backup completion"
        ),
    )
    with SQLiteStateStore(state_db) as store:
        operations._run_intraday_post_close_maintenance(
            config=_maintenance_test_config(paper_db),
            store=store,
            state_db=state_db,
            log_dir=logs,
            session_date=date(2026, 8, 28),
            at=NOW,
        )
        events = store.list_runtime_events()

    assert not any(
        event["message"] == "intraday_backup_completed" for event in events
    )
    assert any(
        event["message"] == "intraday_maintenance_failed"
        and event["payload"]["code"] == "backup_completion_record_failed"
        for event in events
    )


def test_repeated_maintenance_code_alerts_once_per_recovered_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_db = tmp_path / "state.sqlite3"
    paper_db = tmp_path / "paper.sqlite3"
    logs = tmp_path / "logs"
    logs.mkdir()
    _create_maintenance_test_database(paper_db)
    config = _maintenance_test_config(paper_db)
    session = date(2026, 8, 28)
    real_backup = operations.backup_sqlite
    should_fail = True

    def flaky_backup(source, destination, **kwargs):
        if should_fail:
            raise operations.MaintenanceError("repeatable_backup_failure")
        return real_backup(source, destination, **kwargs)

    monkeypatch.setattr(operations, "backup_sqlite", flaky_backup)
    with SQLiteStateStore(state_db) as store:
        for minute in (0, 1):
            operations._run_intraday_post_close_maintenance(
                config=config,
                store=store,
                state_db=state_db,
                log_dir=logs,
                session_date=session,
                at=NOW + timedelta(minutes=minute),
            )
        first_episode = [
            item
            for item in store.list_notification_outbox()
            if item["message"] == "intraday_maintenance_failure"
            and item["payload"].get("code") == "repeatable_backup_failure"
        ]
        assert len(first_episode) == 1

        should_fail = False
        operations._run_intraday_post_close_maintenance(
            config=config,
            store=store,
            state_db=state_db,
            log_dir=logs,
            session_date=session,
            at=NOW + timedelta(minutes=2),
        )
        operations._invalidate_completed_paper_backup_if_any(
            store=store,
            session_date=session,
            at=NOW + timedelta(minutes=2, seconds=1),
        )
        should_fail = True
        operations._run_intraday_post_close_maintenance(
            config=config,
            store=store,
            state_db=state_db,
            log_dir=logs,
            session_date=session,
            at=NOW + timedelta(minutes=3),
        )
        both_episodes = [
            item
            for item in store.list_notification_outbox()
            if item["message"] == "intraday_maintenance_failure"
            and item["payload"].get("code") == "repeatable_backup_failure"
        ]

    assert len(both_episodes) == 2
    assert len({item["notification_key"] for item in both_episodes}) == 2


def test_retention_candidates_resume_partly_retired_restore_sets_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_db = tmp_path / "state.sqlite3"
    paper_db = tmp_path / "paper.sqlite3"
    logs = tmp_path / "logs"
    logs.mkdir()
    _create_maintenance_test_database(paper_db)
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    for alias, dates in {
        "planner": ("2026-08-27", "2026-08-28"),
        "paper": (
            "2026-08-25",
            "2026-08-26",
            "2026-08-27",
            "2026-08-28",
        ),
    }.items():
        for artifact_date in dates:
            (backup_root / f"{alias}-{artifact_date}.sqlite3").write_bytes(b"x")
    (
        backup_root / "planner-2026-08-26.sqlite3.retention-delete"
    ).write_text("retention-delete-v1\n", encoding="ascii")
    monkeypatch.setattr(
        operations, "quarantine_invalid_sqlite_backup", lambda _path: False
    )
    observed: dict[str, set[str]] = {}

    def capture(items, **_kwargs):
        materialized = tuple(items)
        alias = materialized[0].path.name.split("-", 1)[0]
        observed[alias] = {
            item.path.name.removeprefix(f"{alias}-").removesuffix(".sqlite3")
            for item in materialized
        }

    monkeypatch.setattr(operations, "prune_backup_retention", capture)
    with SQLiteStateStore(state_db) as store:
        store.record_runtime_event(
            "INFO",
            "intraday_backup_completed",
            {
                "session_date": "2026-08-28",
                "databases": ["planner", "paper"],
            },
        )
        operations._run_intraday_post_close_maintenance(
            config=_maintenance_test_config(paper_db),
            store=store,
            state_db=state_db,
            log_dir=logs,
            session_date=date(2026, 8, 28),
            at=NOW,
        )

    assert observed == {
        "planner": {"2026-08-27", "2026-08-28"},
        "paper": {"2026-08-26", "2026-08-27", "2026-08-28"},
    }


def test_post_close_backup_waits_for_quiescent_paper_but_other_phases_run(
    tmp_path: Path,
) -> None:
    state_db = tmp_path / "state.sqlite3"
    paper_db = tmp_path / "paper.sqlite3"
    logs = tmp_path / "logs"
    logs.mkdir()
    _create_maintenance_test_database(paper_db)
    config = _maintenance_test_config(paper_db)

    with SQLiteStateStore(state_db) as store:
        operations._run_intraday_post_close_maintenance(
            config=config,
            store=store,
            state_db=state_db,
            log_dir=logs,
            session_date=date(2026, 8, 28),
            at=NOW,
            backup_ready=False,
        )
        first_messages = {
            event["message"] for event in store.list_runtime_events()
        }
        assert "intraday_backup_completed" not in first_messages
        assert "intraday_backup_retention_completed" not in first_messages
        assert "intraday_log_rotation_completed" in first_messages
        assert "intraday_disk_check_completed" in first_messages

        operations._run_intraday_post_close_maintenance(
            config=config,
            store=store,
            state_db=state_db,
            log_dir=logs,
            session_date=date(2026, 8, 28),
            at=NOW + timedelta(minutes=1),
            backup_ready=True,
        )
        final_messages = {
            event["message"] for event in store.list_runtime_events()
        }

    assert "intraday_backup_completed" in final_messages
    assert "intraday_backup_retention_completed" in final_messages


def test_iteration_marks_backup_not_ready_when_paper_is_not_quiescent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "simulation.yaml"
    paper_db = tmp_path / "intraday-paper.sqlite3"
    _write_intraday_config(
        config_path,
        news_context_path=tmp_path / "news-context.json",
    )
    _enable_one_day_simulation(config_path, paper_db)
    observed: list[bool] = []
    monkeypatch.setattr(
        operations.IntradayPaperStore,
        "run_is_quiescent_for_backup",
        lambda _self: False,
    )
    monkeypatch.setattr(
        operations,
        "_run_intraday_post_close_maintenance",
        lambda **kwargs: observed.append(kwargs["backup_ready"]),
    )

    _run(
        tmp_path,
        FakeTransport([_token(), _calendar()]),
        config_name="simulation.yaml",
        at=datetime(2026, 8, 28, 20, 0, 16, tzinfo=timezone.utc),
    )

    assert observed == [False]


def test_terminal_backlog_failure_blocks_backup_and_new_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "simulation.yaml"
    paper_db = tmp_path / "intraday-paper.sqlite3"
    _write_intraday_config(
        config_path,
        news_context_path=tmp_path / "news-context.json",
    )
    _enable_one_day_simulation(config_path, paper_db)
    observed: list[bool] = []

    def fail_reconciliation(**_kwargs) -> None:
        raise RuntimeError("forced terminal backlog failure")

    monkeypatch.setattr(
        operations,
        "_reconcile_intraday_paper_backlog",
        fail_reconciliation,
    )
    monkeypatch.setattr(
        operations,
        "_run_intraday_post_close_maintenance",
        lambda **kwargs: observed.append(kwargs["backup_ready"]),
    )

    snapshot, state_db = _run(
        tmp_path,
        FakeTransport([]),
        config_name="simulation.yaml",
        at=datetime(2026, 8, 28, 21, tzinfo=timezone.utc),
    )

    assert snapshot.blockers == ("intraday_read_or_integrity_failure",)
    assert observed == [False]
    with SQLiteStateStore(state_db) as state:
        assert state.list_intraday_plans() == []


def test_retention_failure_preserves_current_backups_and_other_phases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_db = tmp_path / "state.sqlite3"
    paper_db = tmp_path / "paper.sqlite3"
    logs = tmp_path / "logs"
    logs.mkdir()
    _create_maintenance_test_database(paper_db)

    def fail_retention(_items, **_kwargs):
        raise operations.MaintenanceError("forced_retention_failure")

    monkeypatch.setattr(operations, "prune_backup_retention", fail_retention)
    with SQLiteStateStore(state_db) as store:
        operations._run_intraday_post_close_maintenance(
            config=_maintenance_test_config(paper_db),
            store=store,
            state_db=state_db,
            log_dir=logs,
            session_date=date(2026, 8, 28),
            at=NOW,
        )
        messages = [event["message"] for event in store.list_runtime_events()]

    backup_root = tmp_path / "backups"
    for alias in ("planner", "paper"):
        destination = backup_root / f"{alias}-2026-08-28.sqlite3"
        assert destination.exists()
        assert operations.sha256_manifest_path(destination).exists()
    assert "intraday_backup_completed" in messages
    assert "intraday_backup_retention_completed" not in messages
    assert "intraday_log_rotation_completed" in messages
    assert "intraday_disk_check_completed" in messages


def test_preclose_does_not_run_maintenance(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "simulation.yaml"
    paper_db = tmp_path / "intraday-paper.sqlite3"
    _write_intraday_config(
        config_path,
        news_context_path=tmp_path / "news-context.json",
    )
    _enable_one_day_simulation(config_path, paper_db)
    calls: list[date] = []
    monkeypatch.setattr(
        operations,
        "_run_intraday_post_close_maintenance",
        lambda **kwargs: calls.append(kwargs["session_date"]),
    )

    snapshot, _ = _run(
        tmp_path,
        FakeTransport([_token(), _calendar()]),
        config_name="simulation.yaml",
        at=datetime(2026, 8, 28, 11, tzinfo=timezone.utc),
    )

    assert isinstance(snapshot, operations.HealthSnapshot)
    assert calls == []


def test_maintenance_exception_cannot_replace_snapshot_and_runs_after_close(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "simulation.yaml"
    paper_db = tmp_path / "intraday-paper.sqlite3"
    _write_intraday_config(
        config_path,
        news_context_path=tmp_path / "news-context.json",
    )
    _enable_one_day_simulation(config_path, paper_db)
    order: list[str] = []
    real_close = operations.IntradayPaperStore.close

    def tracked_close(self):
        order.append("close")
        return real_close(self)

    def fail_maintenance(**_kwargs):
        order.append("maintenance")
        raise RuntimeError("forced maintenance failure")

    monkeypatch.setattr(operations.IntradayPaperStore, "close", tracked_close)
    monkeypatch.setattr(
        operations,
        "_run_intraday_post_close_maintenance",
        fail_maintenance,
    )
    snapshot, state_db = _run(
        tmp_path,
        FakeTransport([_token(), _calendar()]),
        config_name="simulation.yaml",
        at=datetime(2026, 8, 28, 20, 0, 16, tzinfo=timezone.utc),
    )

    assert isinstance(snapshot, operations.HealthSnapshot)
    assert order == ["close", "maintenance"]
    with SQLiteStateStore(state_db) as store:
        assert any(
            event["message"] == "intraday_maintenance_failed"
            and event["payload"]["code"] == "maintenance_unhandled_error"
            for event in store.list_runtime_events()
        )


def test_persisted_plan_triggers_maintenance_when_calendar_fetch_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "simulation.yaml"
    paper_db = tmp_path / "intraday-paper.sqlite3"
    _write_intraday_config(
        config_path,
        news_context_path=tmp_path / "news-context.json",
    )
    _enable_one_day_simulation(config_path, paper_db)
    responses = _successful_responses()
    first, _ = _run(
        tmp_path,
        FakeTransport(
            [responses[0], responses[1], responses[6], responses[7], responses[8]]
        ),
        config_name="simulation.yaml",
    )
    assert first.ready is True
    order: list[str] = []
    real_close = operations.IntradayPaperStore.close

    def tracked_close(self):
        order.append("close")
        return real_close(self)

    monkeypatch.setattr(operations.IntradayPaperStore, "close", tracked_close)
    monkeypatch.setattr(
        operations,
        "_run_intraday_post_close_maintenance",
        lambda **_kwargs: order.append("maintenance"),
    )
    snapshot, _ = _run(
        tmp_path,
        FakeTransport([_token(), OSError("calendar unavailable")]),
        config_name="simulation.yaml",
        at=datetime(2026, 8, 28, 20, 0, 16, tzinfo=timezone.utc),
    )

    assert isinstance(snapshot, operations.HealthSnapshot)
    assert order == ["close", "maintenance"]


def test_two_lane_postclose_attempts_both_closes_before_maintenance(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "two-lane.yaml"
    paper_db = tmp_path / "intraday-paper.sqlite3"
    _write_automatic_intraday_config(config_path)
    _enable_one_day_simulation(config_path, paper_db, lanes=2)
    order: list[str] = []
    real_close = operations.IntradayPaperStore.close
    close_count = 0

    def tracked_close(self):
        nonlocal close_count
        close_count += 1
        order.append(f"close-{close_count}")
        if close_count == 1:
            raise RuntimeError("forced first close failure")
        return real_close(self)

    monkeypatch.setattr(operations.IntradayPaperStore, "close", tracked_close)
    monkeypatch.setattr(
        operations,
        "_run_intraday_post_close_maintenance",
        lambda **_kwargs: order.append("maintenance"),
    )
    snapshot, state_db = _run(
        tmp_path,
        FakeTransport([_token(), _calendar()]),
        config_name="two-lane.yaml",
        at=datetime(2026, 8, 28, 20, 0, 16, tzinfo=timezone.utc),
    )

    assert isinstance(snapshot, operations.HealthSnapshot)
    assert order == ["close-1", "close-2", "maintenance"]
    with SQLiteStateStore(state_db) as store:
        assert any(
            event["message"] == "intraday_maintenance_failed"
            and event["payload"]["code"] == "paper_store_close_failed"
            for event in store.list_runtime_events()
        )


def test_weekday_17_local_runs_maintenance_without_schedule_or_plan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "simulation.yaml"
    paper_db = tmp_path / "intraday-paper.sqlite3"
    _write_intraday_config(
        config_path,
        news_context_path=tmp_path / "news-context.json",
    )
    _enable_one_day_simulation(config_path, paper_db)
    calls: list[date] = []
    monkeypatch.setattr(
        operations,
        "_run_intraday_post_close_maintenance",
        lambda **kwargs: calls.append(kwargs["session_date"]),
    )

    snapshot, state_db = _run(
        tmp_path,
        FakeTransport([_token(), OSError("calendar unavailable")]),
        config_name="simulation.yaml",
        at=datetime(2026, 8, 28, 21, tzinfo=timezone.utc),
    )

    assert isinstance(snapshot, operations.HealthSnapshot)
    assert calls == [date(2026, 8, 28)]
    with SQLiteStateStore(state_db) as store:
        assert store.list_intraday_plans() == []
    config = load_config(config_path)
    with IntradayPaperStore(
        paper_db,
        operations._intraday_paper_config(config),
    ) as paper:
        assert paper.daily_summary(date(2026, 8, 28))["status"] == "NO_PLAN"


def test_weekday_fallback_without_durable_coverage_cannot_complete_a_backup(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "simulation.yaml"
    paper_db = tmp_path / "intraday-paper.sqlite3"
    _write_intraday_config(
        config_path,
        news_context_path=tmp_path / "news-context.json",
    )
    _enable_one_day_simulation(config_path, paper_db)

    _snapshot, state_db = _run(
        tmp_path,
        FakeTransport([_token(), OSError("calendar unavailable")]),
        config_name="simulation.yaml",
        at=datetime(2026, 8, 28, 21, tzinfo=timezone.utc),
    )

    with SQLiteStateStore(state_db) as store:
        messages = {event["message"] for event in store.list_runtime_events()}
    assert "intraday_backup_completed" not in messages
    assert "intraday_backup_retention_completed" not in messages
    assert "intraday_log_rotation_completed" in messages
    assert "intraday_disk_check_completed" in messages
    assert not (tmp_path / "backups").exists()


@pytest.mark.parametrize(
    "at",
    [
        datetime(2026, 8, 28, 20, 59, tzinfo=timezone.utc),
        datetime(2026, 8, 29, 21, tzinfo=timezone.utc),
        datetime(2026, 9, 1, 21, tzinfo=timezone.utc),
    ],
)
def test_local_maintenance_fallback_skips_preclose_weekend_and_outside_run(
    tmp_path: Path,
    monkeypatch,
    at: datetime,
) -> None:
    config_path = tmp_path / "simulation.yaml"
    paper_db = tmp_path / "intraday-paper.sqlite3"
    _write_intraday_config(
        config_path,
        news_context_path=tmp_path / "news-context.json",
    )
    _enable_one_day_simulation(config_path, paper_db)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "      end_date: 2026-08-28",
            "      end_date: 2026-08-31",
        ),
        encoding="utf-8",
    )
    calls: list[date] = []
    monkeypatch.setattr(
        operations,
        "_run_intraday_post_close_maintenance",
        lambda **kwargs: calls.append(kwargs["session_date"]),
    )

    _run(
        tmp_path,
        FakeTransport([_token(), OSError("calendar unavailable")]),
        config_name="simulation.yaml",
        at=at,
    )

    assert calls == []


def test_two_lane_weekday_17_local_fallback_runs_once_without_cohort_plan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "two-lane.yaml"
    paper_db = tmp_path / "intraday-paper.sqlite3"
    _write_automatic_intraday_config(config_path)
    _enable_one_day_simulation(config_path, paper_db, lanes=2)
    calls: list[date] = []
    monkeypatch.setattr(
        operations,
        "_run_intraday_post_close_maintenance",
        lambda **kwargs: calls.append(kwargs["session_date"]),
    )

    snapshot, state_db = _run(
        tmp_path,
        FakeTransport([_token(), OSError("calendar unavailable")]),
        config_name="two-lane.yaml",
        at=datetime(2026, 8, 28, 21, tzinfo=timezone.utc),
    )

    assert isinstance(snapshot, operations.HealthSnapshot)
    assert calls == [date(2026, 8, 28)]
    with SQLiteStateStore(state_db) as store:
        assert store.list_intraday_plans() == []


def test_weekday_holiday_still_runs_maintenance_after_17_local(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "simulation.yaml"
    paper_db = tmp_path / "intraday-paper.sqlite3"
    _write_intraday_config(
        config_path,
        news_context_path=tmp_path / "news-context.json",
    )
    _enable_one_day_simulation(config_path, paper_db)
    calls: list[date] = []
    monkeypatch.setattr(
        operations,
        "_run_intraday_post_close_maintenance",
        lambda **kwargs: calls.append(kwargs["session_date"]),
    )

    _run(
        tmp_path,
        FakeTransport([_token(), _calendar(holiday=True)]),
        config_name="simulation.yaml",
        at=datetime(2026, 8, 28, 21, tzinfo=timezone.utc),
    )

    assert calls == [date(2026, 8, 28)]


def test_no_candidate_coverage_is_unchanged_by_local_maintenance_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "simulation.yaml"
    paper_db = tmp_path / "intraday-paper.sqlite3"
    _write_automatic_intraday_config(config_path)
    _enable_one_day_simulation(config_path, paper_db)
    config = load_config(config_path)
    with IntradayPaperStore(
        paper_db,
        operations._intraday_paper_config(config),
    ) as paper:
        paper.record_no_candidate(date(2026, 8, 28), recorded_at=NOW)
    calls: list[date] = []
    monkeypatch.setattr(
        operations,
        "_run_intraday_post_close_maintenance",
        lambda **kwargs: calls.append(kwargs["session_date"]),
    )

    _run(
        tmp_path,
        FakeTransport([_token(), OSError("calendar unavailable")]),
        config_name="simulation.yaml",
        at=datetime(2026, 8, 28, 21, tzinfo=timezone.utc),
    )

    assert calls == [date(2026, 8, 28)]
    with IntradayPaperStore(
        paper_db,
        operations._intraday_paper_config(config),
    ) as paper:
        assert paper.daily_summary(date(2026, 8, 28))["status"] == "NO_CANDIDATE"
