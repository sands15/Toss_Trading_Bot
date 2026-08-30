from __future__ import annotations

import json
import os
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
from turtle_bot.intraday_paper import IntradayPaperStore
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
        return operations.HealthSnapshot(mode="shadow")

    def swap_config(_seconds: float) -> None:
        text = config_path.read_text(encoding="utf-8")
        config_path.write_text(
            text.replace("live_enabled: false", "live_enabled: true"),
            encoding="utf-8",
        )

    monkeypatch.setattr(operations, "_paper_service_iteration", fake_iteration)
    transport = FakeTransport([])

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
    assert transport.requests == []
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


def _run(
    tmp_path: Path,
    transport: FakeTransport,
    *,
    at: datetime = NOW,
    config_name: str = "intraday.yaml",
    env: Mapping[str, str] | None = None,
    clock=None,
):
    config_path = tmp_path / config_name
    if not config_path.exists():
        _write_intraday_config(config_path)
    state_db = tmp_path / "state.sqlite"
    snapshot = run_paper_service(
        config_path=config_path,
        state_db=state_db,
        log_dir=tmp_path / "logs",
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
        "waiting_plan_count": 1,
        "coverage": {
            "expected_count": 21,
            "covered_count": 1,
            "missing_count": 20,
            "missing": ["2026-08-03"],
            "market_closed": [],
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
        "latest_day": operations._paper_daily_public_payload(summary["days"][-1]),
    }
    assert "simulation_account_key" not in month
    assert "days" not in month
    assert "plan_id" not in metadata["latest_day"]
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
        entry_at = datetime.fromisoformat(payload["entry_start"]) + timedelta(seconds=1)
        entry_trigger = Decimal(payload["entry_trigger"])
        entry_limit = Decimal(payload["entry_limit"])
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
