from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import plistlib
import sqlite3
import stat
from typing import Any, Mapping

import pytest
import turtle_bot.toss_stream as toss_stream_module

from turtle_bot.config import intraday_simulation_experiment_hash, load_config
from turtle_bot.toss_client import (
    TossClient,
    TossCredentials,
    TossHttpResponse,
    TossToken,
)
from turtle_bot.intraday_paper import (
    IntradayPaperConfig,
    IntradayPaperStore,
    simulation_account_key,
)
from turtle_bot.state_store import SQLiteStateStore
from turtle_bot.toss_stream import (
    MAX_FRAME_BYTES,
    ShadowStreamState,
    StreamConfig,
    StreamError,
    _credentials_from_environment,
    _decode_frame,
    _handle_frame,
    _refresh_context,
    _rest_resync,
    run_shadow_stream,
    subscription_declaration,
    validate_subscription_ack,
)
from turtle_news.worker import SelectedContext


NOW = datetime(2026, 8, 28, 13, 30, tzinfo=timezone.utc)
SYMBOL = "AAPL"
SESSION_DATE = "2026-08-28"


class FakeClock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value
        self.monotonic_value = 0.0
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.value

    def monotonic(self) -> float:
        return self.monotonic_value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)
        self.monotonic_value += seconds

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)


@dataclass(frozen=True)
class Incoming:
    after: float
    value: object


@dataclass(frozen=True)
class Silence:
    after: float


class FakeConnection:
    def __init__(self, clock: FakeClock, events: list[object]) -> None:
        self.clock = clock
        self.events = list(events)
        self.sent: list[str] = []
        self.closed = False

    def send(self, message: str) -> None:
        self.sent.append(message)

    def recv(self, timeout: float | None = None) -> str | bytes:
        if not self.events:
            raise ConnectionError("fake socket closed")
        event = self.events[0]
        delay = event.after if isinstance(event, (Incoming, Silence)) else 0
        if timeout is not None and delay > timeout:
            self.clock.advance(timeout)
            remaining = delay - timeout
            self.events[0] = (
                Incoming(remaining, event.value)
                if isinstance(event, Incoming)
                else Silence(remaining)
            )
            raise TimeoutError
        event = self.events.pop(0)
        if isinstance(event, Silence):
            self.clock.advance(event.after)
            raise TimeoutError
        if isinstance(event, BaseException):
            raise event
        assert isinstance(event, Incoming)
        self.clock.advance(event.after)
        value = event.value() if callable(event.value) else event.value
        if isinstance(value, (str, bytes)):
            return value
        return json.dumps(value, separators=(",", ":"))

    def close(self) -> None:
        self.closed = True


class FakeConnector:
    def __init__(self, connections: list[FakeConnection]) -> None:
        self.connections = list(connections)
        self.tokens: list[TossToken] = []
        self.returned: list[FakeConnection] = []

    def __call__(self, token: TossToken) -> FakeConnection:
        if self.returned:
            assert self.returned[-1].closed is True
        self.tokens.append(token)
        connection = self.connections.pop(0)
        self.returned.append(connection)
        return connection


class FakeReadOnlyClient:
    def __init__(self, clock: FakeClock, *, fail_orderbook_calls: set[int] | None = None) -> None:
        self.clock = clock
        self.calls: list[object] = []
        self.token_count = 0
        self.orderbook_count = 0
        self.fail_orderbook_calls = fail_orderbook_calls or set()

    def issue_token(self) -> TossToken:
        self.calls.append("issue_token")
        self.token_count += 1
        return TossToken(
            access_token=f"TOKEN-CANARY-{self.token_count}",
            token_type="Bearer",
            expires_at=self.clock.now() + timedelta(hours=1),
        )

    def get_prices(self, symbols: tuple[str, ...]) -> list[dict[str, str]]:
        self.calls.append(("get_prices", symbols))
        return [
            {
                "symbol": symbols[0],
                "timestamp": self.clock.now().isoformat(),
                "lastPrice": "100.01",
                "currency": "USD",
            }
        ]

    def get_orderbook(self, symbol: str) -> dict[str, object]:
        self.calls.append(("get_orderbook", symbol))
        self.orderbook_count += 1
        if self.orderbook_count in self.fail_orderbook_calls:
            raise RuntimeError("SECRET-EXCEPTION-CANARY")
        return {
            "timestamp": self.clock.now().isoformat(),
            "currency": "USD",
            "bids": [{"price": "100.00", "volume": "10"}],
            "asks": [{"price": "100.02", "volume": "11"}],
        }


def _ack(generation: int = 1, *, subscribed: list[str] | None = None) -> dict[str, object]:
    return {
        "type": "subscriptions",
        "id": f"shadow-{SESSION_DATE}-{generation}",
        "subscribed": subscribed
        if subscribed is not None
        else [f"trade:us:{SYMBOL}", f"orderbook:us:{SYMBOL}"],
        "rejected": [],
    }


def _write_context(
    path: Path,
    clock: FakeClock,
    *,
    active_seconds: float = 3_600,
    symbol: str = SYMBOL,
) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": clock.now().isoformat(),
                "market": "US",
                "session_date": SESSION_DATE,
                "active_until": (clock.now() + timedelta(seconds=active_seconds)).isoformat(),
                "symbol": symbol,
                "reason": "intraday_plan",
            }
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def _config(tmp_path: Path, clock: FakeClock, **changes: object) -> StreamConfig:
    root = (tmp_path / "private").resolve()
    context = root / "news-context.json"
    _write_context(context, clock, active_seconds=float(changes.pop("active_seconds", 3_600)))
    values: dict[str, object] = {
        "context_path": context,
        "snapshot_path": root / "market-stream.json",
        "context_max_age_seconds": 3_600,
        "max_reconnect_attempts": 1,
    }
    values.update(changes)
    return StreamConfig(**values)  # type: ignore[arg-type]


def _state(clock: FakeClock) -> ShadowStreamState:
    context = SelectedContext(
        symbol=SYMBOL,
        generated_at=clock.now(),
        active_until=clock.now() + timedelta(hours=1),
        session_date=SESSION_DATE,
    )
    state = ShadowStreamState(context)
    state.begin_connection()
    state.acknowledged = True
    state.rest_resynced_at = clock.now()
    state.baseline = {
        "captured_at": clock.now().isoformat(),
        "verified": True,
        "error_codes": [],
    }
    return state


def _trade(clock: FakeClock, *, timestamp: datetime | None = None) -> dict[str, object]:
    return {
        "type": "message",
        "topic": f"trade:us:{SYMBOL}",
        "data": {
            "price": "100.01",
            "volume": "2",
            "timestamp": (timestamp or clock.now()).isoformat(),
            "currency": "USD",
        },
    }


def _book(clock: FakeClock, *, timestamp: datetime | None = None) -> dict[str, object]:
    return {
        "type": "message",
        "topic": f"orderbook:us:{SYMBOL}",
        "data": {
            "timestamp": (timestamp or clock.now()).isoformat(),
            "currency": "USD",
            "bids": [{"price": "100.00", "volume": "10"}],
            "asks": [{"price": "100.02", "volume": "11"}],
        },
    }


def test_declaration_subscribes_one_symbol_market_data_only() -> None:
    declaration = subscription_declaration(SYMBOL, "req-1")

    assert declaration == [
        {"id": "req-1"},
        {"type": "trade:us", "codes": [SYMBOL]},
        {"type": "orderbook:us", "codes": [SYMBOL]},
    ]
    assert "personal:order" not in json.dumps(declaration)


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"id": "wrong"}, "subscription_ack_mismatch"),
        ({"subscribed": [f"trade:us:{SYMBOL}"]}, "subscription_ack_incomplete"),
        (
            {
                "subscribed": [
                    f"trade:us:{SYMBOL}",
                    f"orderbook:us:{SYMBOL}",
                    "personal:order:7",
                ]
            },
            "subscription_ack_incomplete",
        ),
        ({"rejected": [{"target": "x"}]}, "subscription_rejected"),
    ],
)
def test_ack_requires_exact_id_topics_and_no_rejection(
    changes: Mapping[str, object], code: str
) -> None:
    value = _ack()
    value.update(changes)

    with pytest.raises(StreamError, match=code):
        validate_subscription_ack(
            value,
            symbol=SYMBOL,
            request_id=f"shadow-{SESSION_DATE}-1",
        )


@pytest.mark.parametrize(
    "case,code",
    [
        ("binary", "ws_binary_frame_rejected"),
        ("oversize", "ws_frame_too_large"),
        ("duplicate", "ws_json_duplicate_key"),
        ("malformed", "ws_json_invalid"),
    ],
)
def test_frame_decoder_rejects_binary_oversize_duplicate_and_malformed(
    case: str, code: str
) -> None:
    raw: object = {
        "binary": b"{}",
        "oversize": "{" + "x" * MAX_FRAME_BYTES + "}",
        "duplicate": '{"type":"pong","type":"message"}',
        "malformed": "{",
    }[case]
    with pytest.raises(StreamError, match=code):
        _decode_frame(raw)


@pytest.mark.parametrize(
    "topic",
    ["trade:us:MSFT", "orderbook:us:MSFT", "trade:kr:AAPL", "personal:order:7"],
)
def test_wrong_topic_never_updates_selected_symbol_state(topic: str) -> None:
    clock = FakeClock()
    state = _state(clock)
    frame = _trade(clock)
    frame["topic"] = topic

    with pytest.raises(StreamError, match="ws_topic_unexpected"):
        _handle_frame(
            frame,
            state=state,
            received_at=clock.now(),
            max_age_seconds=30,
            future_tolerance_seconds=5,
        )

    assert state.trade is None
    assert state.orderbook is None


def test_nullable_orderbook_timestamp_is_accepted_but_not_usable() -> None:
    clock = FakeClock()
    state = _state(clock)
    _handle_frame(
        _trade(clock),
        state=state,
        received_at=clock.now(),
        max_age_seconds=30,
        future_tolerance_seconds=5,
    )
    book = _book(clock)
    book["data"]["timestamp"] = None  # type: ignore[index]

    assert (
        _handle_frame(
            book,
            state=state,
            received_at=clock.now(),
            max_age_seconds=30,
            future_tolerance_seconds=5,
        )
        == "orderbook"
    )
    payload = state.as_payload(now=clock.now(), event_max_age_seconds=30)
    assert payload["shadow_usable"] is False
    assert payload["orderbook"]["timestamp_source"] == "local_receive"
    assert payload["error_codes"] == ["orderbook_timestamp_unverified"]


def test_fresh_trade_and_book_are_usable_and_regression_fails_closed() -> None:
    clock = FakeClock()
    state = _state(clock)
    for frame in (_trade(clock), _book(clock)):
        _handle_frame(
            frame,
            state=state,
            received_at=clock.now(),
            max_age_seconds=30,
            future_tolerance_seconds=5,
        )

    assert state.as_payload(now=clock.now(), event_max_age_seconds=30)["shadow_usable"] is True
    same_time = _trade(clock)
    same_time["data"]["price"] = "100.02"  # type: ignore[index]
    _handle_frame(
        same_time,
        state=state,
        received_at=clock.now(),
        max_age_seconds=30,
        future_tolerance_seconds=5,
    )
    assert state.trade_error is None
    assert state.trade["price"] == "100.02"
    original = state.trade
    _handle_frame(
        _trade(clock, timestamp=clock.now() - timedelta(seconds=1)),
        state=state,
        received_at=clock.now(),
        max_age_seconds=30,
        future_tolerance_seconds=5,
    )

    assert state.trade == original
    assert state.trade_error == "trade_time_regressed"
    assert state.as_payload(now=clock.now(), event_max_age_seconds=30)["shadow_usable"] is False


def test_frames_older_than_rest_baseline_never_become_usable() -> None:
    clock = FakeClock()
    state = _state(clock)
    state.baseline = {
        "verified": True,
        "error_codes": [],
        "price": {"broker_at": clock.now().isoformat()},
        "orderbook": {"broker_at": clock.now().isoformat()},
    }
    old = clock.now() - timedelta(seconds=1)

    _handle_frame(
        _trade(clock, timestamp=old),
        state=state,
        received_at=clock.now(),
        max_age_seconds=30,
        future_tolerance_seconds=5,
    )
    _handle_frame(
        _book(clock, timestamp=old),
        state=state,
        received_at=clock.now(),
        max_age_seconds=30,
        future_tolerance_seconds=5,
    )

    payload = state.as_payload(now=clock.now(), event_max_age_seconds=30)
    assert payload["shadow_usable"] is False
    assert payload["error_codes"] == [
        "orderbook_before_rest_baseline",
        "trade_before_rest_baseline",
    ]


def test_shadow_freshness_uses_broker_time_not_only_receive_time() -> None:
    clock = FakeClock()
    state = _state(clock)
    broker_at = clock.now() - timedelta(seconds=29)
    for frame in (
        _trade(clock, timestamp=broker_at),
        _book(clock, timestamp=broker_at),
    ):
        _handle_frame(
            frame,
            state=state,
            received_at=clock.now(),
            max_age_seconds=30,
            future_tolerance_seconds=5,
        )

    assert state.as_payload(now=clock.now(), event_max_age_seconds=30)["shadow_usable"] is True
    clock.advance(2)
    assert state.as_payload(now=clock.now(), event_max_age_seconds=30)["shadow_usable"] is False


def test_active_until_exact_boundary_is_never_usable() -> None:
    clock = FakeClock()
    state = _state(clock)
    for frame in (_trade(clock), _book(clock)):
        _handle_frame(
            frame,
            state=state,
            received_at=clock.now(),
            max_age_seconds=30,
            future_tolerance_seconds=5,
        )
    state.context = SelectedContext(
        symbol=SYMBOL,
        generated_at=clock.now(),
        active_until=clock.now(),
        session_date=SESSION_DATE,
    )

    payload = state.as_payload(now=clock.now(), event_max_age_seconds=30)
    assert payload["shadow_usable"] is False
    assert payload["valid_until"] == payload["updated_at"]


def test_optional_rest_timestamps_keep_unverified_baseline_without_schema_failure() -> None:
    clock = FakeClock()

    class NullTimestampClient(FakeReadOnlyClient):
        def get_prices(self, symbols: tuple[str, ...]) -> list[dict[str, Any]]:
            result = super().get_prices(symbols)
            result[0]["timestamp"] = None
            return result

        def get_orderbook(self, symbol: str) -> dict[str, object]:
            result = super().get_orderbook(symbol)
            result["timestamp"] = None
            return result

    baseline = _rest_resync(
        NullTimestampClient(clock),  # type: ignore[arg-type]
        SYMBOL,
        now=clock.now,
        max_age_seconds=30,
        future_tolerance_seconds=5,
    )

    assert baseline["verified"] is False
    assert baseline["error_codes"] == [
        "rest_orderbook_timestamp_unverified",
        "rest_price_timestamp_unverified",
    ]
    assert baseline["price"]["broker_at"] is None
    assert baseline["orderbook"]["broker_at"] is None


@pytest.mark.parametrize("case", ["empty", "zero"])
def test_schema_valid_unavailable_book_waits_for_next_update_without_disconnect(
    case: str,
) -> None:
    clock = FakeClock()
    state = _state(clock)
    frame = _book(clock)
    if case == "empty":
        frame["data"]["asks"] = []  # type: ignore[index]
        expected = "orderbook_empty"
    else:
        frame["data"]["asks"][0]["volume"] = "0"  # type: ignore[index]
        expected = "orderbook_level_unavailable"

    assert (
        _handle_frame(
            frame,
            state=state,
            received_at=clock.now(),
            max_age_seconds=30,
            future_tolerance_seconds=5,
        )
        == "orderbook"
    )
    payload = state.as_payload(now=clock.now(), event_max_age_seconds=30)
    assert payload["connected"] is True
    assert payload["shadow_usable"] is False
    assert payload["error_codes"] == [expected]


def test_empty_rest_book_is_unverified_baseline_not_transport_failure() -> None:
    clock = FakeClock()

    class EmptyBookClient(FakeReadOnlyClient):
        def get_orderbook(self, symbol: str) -> dict[str, object]:
            result = super().get_orderbook(symbol)
            result["asks"] = []
            return result

    baseline = _rest_resync(
        EmptyBookClient(clock),  # type: ignore[arg-type]
        SYMBOL,
        now=clock.now,
        max_age_seconds=30,
        future_tolerance_seconds=5,
    )

    assert baseline["verified"] is False
    assert baseline["error_codes"] == ["orderbook_empty"]
    assert baseline["orderbook"] is None


def test_once_handshake_rest_snapshot_is_closed_private_and_redacted(tmp_path) -> None:
    clock = FakeClock()
    config = _config(tmp_path, clock, once=True)
    connection = FakeConnection(clock, [Incoming(0, _ack())])
    connector = FakeConnector([connection])
    client = FakeReadOnlyClient(clock)

    result = run_shadow_stream(
        config,
        client=client,  # type: ignore[arg-type]
        connector=connector,
        now=clock.now,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        jitter=lambda: 0,
    )

    assert result == 0
    assert connection.closed is True
    assert json.loads(connection.sent[0]) == subscription_declaration(
        SYMBOL, f"shadow-{SESSION_DATE}-1"
    )
    assert client.calls == [
        "issue_token",
        ("get_prices", (SYMBOL,)),
        ("get_orderbook", SYMBOL),
    ]
    payload = json.loads(config.snapshot_path.read_text(encoding="utf-8"))
    assert payload["connected"] is False
    assert payload["shadow_usable"] is False
    assert payload["live_order_submission"] is False
    assert payload["ready_for_live_entry"] is False
    assert payload["last_disconnect_error"] == "once_complete"
    assert payload["error_codes"] == []
    raw = config.snapshot_path.read_text(encoding="utf-8").lower()
    assert "token-canary" not in raw
    assert "client_secret" not in raw
    assert "authorization" not in raw
    assert "accountseq" not in raw
    if os.name != "nt":
        assert stat.S_IMODE(config.snapshot_path.stat().st_mode) == 0o600


def test_validated_baseline_is_forwarded_to_optional_paper_sink(tmp_path) -> None:
    clock = FakeClock()
    config = _config(tmp_path, clock, once=True)
    connection = FakeConnection(clock, [Incoming(0, _ack())])
    observed: list[tuple[str, str, bool]] = []

    result = run_shadow_stream(
        config,
        client=FakeReadOnlyClient(clock),  # type: ignore[arg-type]
        connector=FakeConnector([connection]),
        now=clock.now,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        jitter=lambda: 0,
        event_sink=lambda state, kind, at: observed.append(
            (kind, at.isoformat(), bool(state.baseline and state.baseline["verified"]))
        ),
    )

    assert result == 0
    assert observed == [
        ("start", NOW.isoformat(), False),
        ("baseline", NOW.isoformat(), True),
    ]


def test_heartbeat_reports_ack_only_after_a_fresh_verified_baseline(tmp_path) -> None:
    clock = FakeClock()
    config = _config(tmp_path, clock, once=True)
    connection = FakeConnection(clock, [Incoming(0, _ack())])
    observed: list[tuple[str, bool, bool]] = []

    def heartbeat(state, at) -> None:  # type: ignore[no-untyped-def]
        observed.append(
            toss_stream_module._stream_heartbeat_state(
                state,
                at=at,
                rest_resync_seconds=config.rest_resync_seconds,
                future_tolerance_seconds=config.event_future_tolerance_seconds,
            )
        )

    result = run_shadow_stream(
        config,
        client=FakeReadOnlyClient(clock),  # type: ignore[arg-type]
        connector=FakeConnector([connection]),
        now=clock.now,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        jitter=lambda: 0,
        heartbeat_sink=heartbeat,
    )

    assert result == 0
    assert observed[0] == ("STARTING", False, False)
    assert ("OK", True, True) in observed
    assert observed[-1] == ("DEGRADED", False, False)


def test_ack_loss_reconnects_with_fresh_token_and_closes_old_socket(tmp_path) -> None:
    clock = FakeClock()
    config = _config(tmp_path, clock, once=True, max_reconnect_attempts=2)
    first = FakeConnection(clock, [ConnectionError("drop before ack")])
    second = FakeConnection(clock, [Incoming(0, _ack(2))])
    connector = FakeConnector([first, second])
    client = FakeReadOnlyClient(clock)

    result = run_shadow_stream(
        config,
        client=client,  # type: ignore[arg-type]
        connector=connector,
        now=clock.now,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        jitter=lambda: 0,
    )

    assert result == 0
    assert first.closed is True
    assert second.closed is True
    assert len(connector.tokens) == 2
    assert client.calls.count("issue_token") == 2
    assert client.calls.count(("get_prices", (SYMBOL,))) == 1
    assert client.calls.count(("get_orderbook", SYMBOL)) == 1
    assert clock.sleeps == [1.0]


def test_rest_failure_reconnects_and_resyncs_again(tmp_path) -> None:
    clock = FakeClock()
    config = _config(tmp_path, clock, once=True, max_reconnect_attempts=2)
    first = FakeConnection(clock, [Incoming(0, _ack(1))])
    second = FakeConnection(clock, [Incoming(0, _ack(2))])
    client = FakeReadOnlyClient(clock, fail_orderbook_calls={1})

    result = run_shadow_stream(
        config,
        client=client,  # type: ignore[arg-type]
        connector=FakeConnector([first, second]),
        now=clock.now,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        jitter=lambda: 0,
    )

    assert result == 0
    assert client.calls.count(("get_prices", (SYMBOL,))) == 2
    assert client.calls.count(("get_orderbook", SYMBOL)) == 2
    assert first.closed is True
    assert second.closed is True


def test_old_snapshot_is_invalidated_before_any_network_call(tmp_path) -> None:
    clock = FakeClock()
    config = _config(tmp_path, clock, once=True)
    config.snapshot_path.write_text(
        json.dumps({"connected": True, "shadow_usable": True}), encoding="utf-8"
    )
    os.chmod(config.snapshot_path, 0o600)
    connection = FakeConnection(clock, [Incoming(0, _ack())])

    def connector(_token: TossToken) -> FakeConnection:
        published = json.loads(config.snapshot_path.read_text(encoding="utf-8"))
        assert published["connected"] is False
        assert published["shadow_usable"] is False
        return connection

    result = run_shadow_stream(
        config,
        client=FakeReadOnlyClient(clock),  # type: ignore[arg-type]
        connector=connector,
        now=clock.now,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        jitter=lambda: 0,
    )

    assert result == 0


def test_delayed_ack_expires_at_bounded_timeout(tmp_path) -> None:
    clock = FakeClock()
    config = _config(tmp_path, clock, once=True, ack_timeout_seconds=10)
    connection = FakeConnection(clock, [Incoming(11, _ack())])

    result = run_shadow_stream(
        config,
        client=FakeReadOnlyClient(clock),  # type: ignore[arg-type]
        connector=FakeConnector([connection]),
        now=clock.now,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        jitter=lambda: 0,
    )

    payload = json.loads(config.snapshot_path.read_text(encoding="utf-8"))
    assert result == 1
    assert clock.monotonic() == 10
    assert payload["last_disconnect_error"] == "subscription_ack_timeout"
    assert payload["shadow_usable"] is False


def test_healthy_connection_resets_consecutive_failure_backoff(tmp_path) -> None:
    clock = FakeClock()
    config = _config(
        tmp_path,
        clock,
        active_seconds=10,
        max_reconnect_attempts=2,
        rest_resync_seconds=600,
    )
    first = FakeConnection(clock, [ConnectionError("first setup failure")])
    second = FakeConnection(
        clock,
        [
            Incoming(0, _ack(2)),
            Incoming(0, lambda: _trade(clock)),
            Incoming(0, lambda: _book(clock)),
            Incoming(
                0,
                lambda: _trade(
                    clock, timestamp=clock.now() - timedelta(seconds=31)
                ),
            ),
            ConnectionError("drop after healthy data"),
        ],
    )
    third = FakeConnection(clock, [Incoming(0, _ack(3)), Silence(8)])

    result = run_shadow_stream(
        config,
        client=FakeReadOnlyClient(clock),  # type: ignore[arg-type]
        connector=FakeConnector([first, second, third]),
        now=clock.now,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        jitter=lambda: 0,
    )

    payload = json.loads(config.snapshot_path.read_text(encoding="utf-8"))
    assert result == 0
    assert clock.sleeps == [1.0, 1.0]
    assert payload["reconnect_count"] == 2
    assert payload["last_disconnect_error"] == "context_inactive"
    assert payload["error_codes"] == []


def test_market_data_does_not_suppress_text_ping(tmp_path) -> None:
    clock = FakeClock()
    config = _config(
        tmp_path,
        clock,
        active_seconds=62,
        ping_interval_seconds=60,
        pong_timeout_seconds=15,
        rest_resync_seconds=600,
    )
    connection = FakeConnection(
        clock,
        [
            Incoming(0, _ack()),
            Incoming(20, lambda: _trade(clock)),
            Incoming(20, lambda: _book(clock)),
            Incoming(20, lambda: _trade(clock)),
            Incoming(1, {"type": "pong"}),
            Silence(2),
        ],
    )

    result = run_shadow_stream(
        config,
        client=FakeReadOnlyClient(clock),  # type: ignore[arg-type]
        connector=FakeConnector([connection]),
        now=clock.now,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        jitter=lambda: 0,
    )

    assert result == 0
    assert connection.sent[1:] == ["PING"]
    payload = json.loads(config.snapshot_path.read_text(encoding="utf-8"))
    assert payload["last_disconnect_error"] == "context_inactive"
    assert payload["error_codes"] == []


def test_missing_pong_records_safe_failure_and_never_stays_usable(tmp_path) -> None:
    clock = FakeClock()
    config = _config(
        tmp_path,
        clock,
        ping_interval_seconds=60,
        pong_timeout_seconds=15,
        rest_resync_seconds=600,
    )
    connection = FakeConnection(
        clock,
        [Incoming(0, _ack()), Incoming(60, lambda: _trade(clock)), Silence(16)],
    )

    result = run_shadow_stream(
        config,
        client=FakeReadOnlyClient(clock),  # type: ignore[arg-type]
        connector=FakeConnector([connection]),
        now=clock.now,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        jitter=lambda: 0,
    )

    payload = json.loads(config.snapshot_path.read_text(encoding="utf-8"))
    assert result == 1
    assert connection.sent[1:] == ["PING"]
    assert payload["connected"] is False
    assert payload["shadow_usable"] is False
    assert payload["error_codes"] == ["reconnect_limit_reached"]
    assert payload["last_disconnect_error"] == "pong_timeout"


def test_late_pong_does_not_clear_expired_deadline(tmp_path) -> None:
    clock = FakeClock()
    config = _config(
        tmp_path,
        clock,
        ping_interval_seconds=60,
        pong_timeout_seconds=15,
        rest_resync_seconds=600,
    )
    connection = FakeConnection(
        clock,
        [
            Incoming(0, _ack()),
            Incoming(60, lambda: _trade(clock)),
            Incoming(16, {"type": "pong"}),
        ],
    )

    result = run_shadow_stream(
        config,
        client=FakeReadOnlyClient(clock),  # type: ignore[arg-type]
        connector=FakeConnector([connection]),
        now=clock.now,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        jitter=lambda: 0,
    )

    payload = json.loads(config.snapshot_path.read_text(encoding="utf-8"))
    assert result == 1
    assert payload["last_disconnect_error"] == "pong_timeout"
    assert payload["shadow_usable"] is False


def test_slow_periodic_rest_finishes_before_ping_deadline_starts(tmp_path) -> None:
    clock = FakeClock()
    config = _config(
        tmp_path,
        clock,
        active_seconds=90,
        ping_interval_seconds=60,
        pong_timeout_seconds=15,
        rest_resync_seconds=30,
    )

    class SlowThirdOrderbookClient(FakeReadOnlyClient):
        def get_orderbook(self, symbol: str) -> dict[str, object]:
            result = super().get_orderbook(symbol)
            if self.orderbook_count == 3:
                self.clock.advance(16)
            return result

    connection = FakeConnection(
        clock,
        [
            Incoming(0, _ack()),
            Silence(60),
            Incoming(1, {"type": "pong"}),
            Silence(13),
        ],
    )

    result = run_shadow_stream(
        config,
        client=SlowThirdOrderbookClient(clock),  # type: ignore[arg-type]
        connector=FakeConnector([connection]),
        now=clock.now,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        jitter=lambda: 0,
    )

    payload = json.loads(config.snapshot_path.read_text(encoding="utf-8"))
    assert result == 0
    assert connection.sent[1:] == ["PING"]
    assert payload["last_disconnect_error"] == "context_inactive"


def test_burst_frames_do_not_poll_context_file_per_tick(tmp_path, monkeypatch) -> None:
    clock = FakeClock()
    config = _config(
        tmp_path,
        clock,
        active_seconds=2,
        context_check_interval_seconds=1,
        rest_resync_seconds=600,
    )
    refresh_calls = 0
    real_refresh = toss_stream_module._refresh_context

    def counted_refresh(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal refresh_calls
        refresh_calls += 1
        return real_refresh(*args, **kwargs)

    monkeypatch.setattr(toss_stream_module, "_refresh_context", counted_refresh)
    burst = [
        Incoming(0, (lambda: _trade(clock)) if index % 2 == 0 else (lambda: _book(clock)))
        for index in range(100)
    ]
    connection = FakeConnection(
        clock,
        [Incoming(0, _ack()), *burst, Silence(2)],
    )

    result = run_shadow_stream(
        config,
        client=FakeReadOnlyClient(clock),  # type: ignore[arg-type]
        connector=FakeConnector([connection]),
        now=clock.now,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        jitter=lambda: 0,
    )

    assert result == 0
    assert refresh_calls <= 3


def test_invalid_context_replaces_old_usable_snapshot_with_tombstone(tmp_path) -> None:
    clock = FakeClock()
    config = _config(tmp_path, clock)
    config.snapshot_path.write_text(
        json.dumps({"connected": True, "shadow_usable": True}), encoding="utf-8"
    )
    os.chmod(config.snapshot_path, 0o600)
    config.context_path.unlink()

    with pytest.raises(StreamError, match="context_file_invalid"):
        run_shadow_stream(
            config,
            client=FakeReadOnlyClient(clock),  # type: ignore[arg-type]
            connector=FakeConnector([]),
            now=clock.now,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            jitter=lambda: 0,
        )

    payload = json.loads(config.snapshot_path.read_text(encoding="utf-8"))
    assert payload["connected"] is False
    assert payload["shadow_usable"] is False
    assert payload["valid_until"] == payload["updated_at"]
    assert payload["error_codes"] == ["context_file_invalid"]


class RecordingTransport:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls: list[dict[str, Any]] = []

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
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "query": dict(query or {}),
                "json_body": json_body,
                "form_body": form_body,
            }
        )
        if url.endswith("/oauth2/token"):
            return TossHttpResponse(
                200,
                {},
                {"access_token": "REAL-CLIENT-TOKEN-CANARY", "token_type": "Bearer", "expires_in": 3600},
            )
        if url.endswith("/api/v1/prices"):
            return TossHttpResponse(
                200,
                {},
                {
                    "result": [
                        {
                            "symbol": SYMBOL,
                            "timestamp": self.clock.now().isoformat(),
                            "lastPrice": "100.01",
                            "currency": "USD",
                        }
                    ]
                },
            )
        if url.endswith("/api/v1/orderbook"):
            return TossHttpResponse(
                200,
                {},
                {
                    "result": {
                        "timestamp": self.clock.now().isoformat(),
                        "currency": "USD",
                        "bids": [{"price": "100.00", "volume": "10"}],
                        "asks": [{"price": "100.02", "volume": "11"}],
                    }
                },
            )
        raise AssertionError(url)


def test_real_read_only_client_uses_only_oauth_prices_and_orderbook(tmp_path) -> None:
    clock = FakeClock()
    config = _config(tmp_path, clock, once=True)
    transport = RecordingTransport(clock)
    client = TossClient(
        credentials=TossCredentials("CLIENT-ID-CANARY", "CLIENT-SECRET-CANARY"),
        account_seq=None,
        transport=transport,
        now=clock.now,
    )
    connection = FakeConnection(clock, [Incoming(0, _ack())])

    result = run_shadow_stream(
        config,
        client=client,
        connector=FakeConnector([connection]),
        now=clock.now,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        jitter=lambda: 0,
    )

    assert result == 0
    assert [(call["method"], call["url"].rsplit("/", 1)[-1]) for call in transport.calls] == [
        ("POST", "token"),
        ("GET", "prices"),
        ("GET", "orderbook"),
    ]
    assert all("X-Tossinvest-Account" not in call["headers"] for call in transport.calls)
    assert all(call["json_body"] is None for call in transport.calls)


def test_credentials_are_removed_from_process_environment(monkeypatch) -> None:
    monkeypatch.setenv("TOSS_CLIENT_ID", "client-id")
    monkeypatch.setenv("TOSS_CLIENT_SECRET", "client-secret")

    credentials = _credentials_from_environment()

    assert credentials.client_id == "client-id"
    assert credentials.client_secret == "client-secret"
    assert "TOSS_CLIENT_ID" not in os.environ
    assert "TOSS_CLIENT_SECRET" not in os.environ


@pytest.mark.parametrize(
    "case,code",
    [
        ("symbol", "context_changed"),
        ("generated", "context_reverted"),
        ("expiry", "context_extended"),
    ],
)
def test_locked_context_cannot_change_revert_or_extend(
    tmp_path, case: str, code: str
) -> None:
    clock = FakeClock()
    config = _config(tmp_path, clock)
    previous = SelectedContext(
        symbol=SYMBOL,
        generated_at=clock.now(),
        active_until=clock.now() + timedelta(hours=1),
        session_date=SESSION_DATE,
    )
    payload = json.loads(config.context_path.read_text(encoding="utf-8"))
    if case == "symbol":
        payload["symbol"] = "MSFT"
    elif case == "generated":
        payload["generated_at"] = (clock.now() - timedelta(seconds=1)).isoformat()
    else:
        payload["active_until"] = (clock.now() + timedelta(hours=2)).isoformat()
    config.context_path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(config.context_path, 0o600)

    with pytest.raises(StreamError, match=code):
        _refresh_context(config, previous, clock.now())


def test_macos_wrapper_uses_clean_keychain_market_data_boundary() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "ops" / "run-toss-stream.command").read_text(encoding="utf-8")

    assert "/usr/bin/env -i" in source
    assert "turtle_bot.toss_stream" in source
    assert "toss-trading-bot" in source
    assert "toss_client_id" in source
    assert "toss_client_secret" in source
    assert "TOSS_ACCOUNT" not in source
    assert "DISCORD_" not in source
    assert "--simulation-config" in source
    assert "--plan-db" in source
    assert "--context" in source
    assert "--snapshot" in source


def test_shadow_launchagent_contains_no_secret_or_account_setting() -> None:
    root = Path(__file__).resolve().parents[1]
    path = root / "ops" / "launchd" / "com.sands15.toss-market-stream-shadow.plist.example"
    with path.open("rb") as handle:
        payload = plistlib.load(handle)

    assert payload["Label"] == "com.sands15.toss-market-stream-shadow"
    assert payload["LimitLoadToSessionType"] == "Aqua"
    assert len(payload["ProgramArguments"]) == 1
    assert set(payload["EnvironmentVariables"]) == {
        "TOSS_STREAM_CONTEXT_PATH",
        "TOSS_STREAM_EXPERIMENT_HASH",
        "TOSS_STREAM_HEARTBEAT_PATH",
        "TOSS_STREAM_KEYCHAIN_SLUG",
        "TOSS_STREAM_PLAN_DB",
        "TOSS_STREAM_SIMULATION_DB",
        "TOSS_STREAM_SIMULATION_END_DATE",
        "TOSS_STREAM_SIMULATION_ID",
        "TOSS_STREAM_SIMULATION_START_DATE",
        "TOSS_STREAM_SIMULATION_CONFIG_PATH",
        "TOSS_STREAM_SNAPSHOT_PATH",
    }
    raw = path.read_text(encoding="utf-8").lower()
    assert "client_secret" not in raw
    assert "access_token" not in raw
    assert "account_seq" not in raw


def _paper_stream_fixture(
    tmp_path: Path,
) -> tuple[Path, Path, IntradayPaperConfig, str]:
    paper_path = (tmp_path / "private" / "intraday-paper.sqlite3").resolve()
    plan_db = (tmp_path / "private" / "intraday.sqlite3").resolve()
    context_path = (tmp_path / "private" / "news-context.json").resolve()
    paper_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(paper_path.parent, 0o700)
    config_path = (tmp_path / "private" / "simulation.yaml").resolve()
    template = (
        Path(__file__).resolve().parents[1]
        / "config"
        / "intraday-simulation.example.yaml"
    ).read_text(encoding="utf-8")
    template = template.replace("start_date: 2026-08-31", "start_date: 2026-08-28")
    template = template.replace("end_date: 2026-09-30", "end_date: 2026-09-27")
    template = template.replace(
        "db_path: ../state/intraday-paper.sqlite3",
        f"db_path: {json.dumps(str(paper_path))}",
    )
    template = template.replace(
        "state_db: state/intraday.sqlite3",
        f"state_db: {json.dumps(str(plan_db))}",
    )
    template = template.replace(
        "news_context_path: ../state/news-context.json",
        f"news_context_path: {json.dumps(str(context_path))}",
    )
    config_path.write_text(template, encoding="utf-8")
    os.chmod(config_path, 0o600)

    parsed_config = load_config(config_path)
    paper_config = IntradayPaperConfig(
        run_id="2026-09-forward-test",
        start_date=date(2026, 8, 28),
        end_date=date(2026, 9, 27),
        initial_cash_usd=Decimal("10000"),
        slippage_fraction=Decimal("0.0005"),
        quote_max_age_seconds=15,
        future_tolerance_seconds=2,
        experiment_hash=intraday_simulation_experiment_hash(parsed_config),
    )
    account_key = simulation_account_key(paper_config)
    plan_id = "intraday-20260828"
    payload = {
        "plan_id": plan_id,
        "account_id": account_key,
        "session_date": SESSION_DATE,
        "mode": "shadow",
        "status": "PAPER_PLANNED",
        "live_order_submission": False,
        "symbol": SYMBOL,
        "quantity": 1,
        "available_cash": "10000",
        "entry_start": (NOW + timedelta(minutes=2)).isoformat(),
        "entry_expiry": (NOW + timedelta(minutes=30)).isoformat(),
        "force_exit_at": (NOW + timedelta(hours=6, minutes=15)).isoformat(),
        "regular_close": (NOW + timedelta(hours=6, minutes=30)).isoformat(),
        "entry_trigger": "100",
        "entry_limit": "101",
        "target_trigger": "102",
        "target_limit": "102",
        "stop_trigger": "98",
        "stop_limit": "97.5",
        "estimated_round_trip_cost_fraction": "0.0021",
        "estimated_fixed_round_trip_cost": "0.01",
        "commission_snapshot": {"broker_commission_fraction": "0.0005"},
    }
    with SQLiteStateStore(plan_db) as store:
        store.save_intraday_plan_once(
            account_key=account_key,
            session_date=SESSION_DATE,
            symbol=SYMBOL,
            payload=payload,
            created_at=NOW - timedelta(minutes=30),
        )
    os.chmod(plan_db, 0o600)
    return config_path, plan_db, paper_config, plan_id


def test_paper_stream_sink_preserves_causal_fill_and_marks_disconnect_gap(
    tmp_path: Path,
) -> None:
    config_path, plan_db, paper_config, plan_id = _paper_stream_fixture(tmp_path)
    clock = FakeClock()
    clock.advance(120)
    state = _state(clock)
    sink = toss_stream_module._PaperStreamSink(
        simulation_config=config_path,
        plan_db=plan_db,
        event_max_age_seconds=15,
        monotonic=clock.monotonic,
    )
    try:
        trade_kind = _handle_frame(
            _trade(clock),
            state=state,
            received_at=clock.now(),
            max_age_seconds=15,
            future_tolerance_seconds=2,
        )
        sink(state, trade_kind, clock.now())
        book_kind = _handle_frame(
            _book(clock),
            state=state,
            received_at=clock.now(),
            max_age_seconds=15,
            future_tolerance_seconds=2,
        )
        sink(state, book_kind, clock.now())

        assert sink.paper_store.load_plan(plan_id)["status"] == "WAITING_ENTRY"

        clock.advance(1)
        trade_kind = _handle_frame(
            _trade(clock),
            state=state,
            received_at=clock.now(),
            max_age_seconds=15,
            future_tolerance_seconds=2,
        )
        sink(state, trade_kind, clock.now())
        clock.advance(1)
        book_kind = _handle_frame(
            _book(clock),
            state=state,
            received_at=clock.now(),
            max_age_seconds=15,
            future_tolerance_seconds=2,
        )
        sink(state, book_kind, clock.now())
        assert sink.paper_store.load_plan(plan_id)["status"] == "OPEN"

        state.disconnect("ws_connection_lost")
        sink(state, "disconnect", clock.now())
        disconnected = sink.paper_store.load_plan(plan_id)
        assert disconnected["status"] == "OPEN"
        assert disconnected["data_quality_invalid"] is True
        assert disconnected["data_gap_count"] == 1
        assert disconnected["journaled_frame_count"] == 4
    finally:
        sink.close()

    with IntradayPaperStore(
        tmp_path / "private" / "intraday-paper.sqlite3", paper_config
    ) as reopened:
        assert reopened.load_plan(plan_id)["data_gap_count"] == 1


def test_paper_stream_main_rejects_partial_simulation_arguments(capsys) -> None:
    result = toss_stream_module.main(
        [
            "--context",
            str(Path.cwd() / "news-context.json"),
            "--snapshot",
            str(Path.cwd() / "market-stream.json"),
            "--simulation-config",
            str(Path.cwd() / "intraday-simulation.yaml"),
        ]
    )

    assert result == 2
    assert "paper_simulation_args_incomplete" in capsys.readouterr().err


def test_paper_stream_main_requires_immutable_deployment_lock(capsys) -> None:
    result = toss_stream_module.main(
        [
            "--context",
            str(Path.cwd() / "news-context.json"),
            "--snapshot",
            str(Path.cwd() / "market-stream.json"),
            "--simulation-config",
            str(Path.cwd() / "intraday-simulation.yaml"),
            "--plan-db",
            str(Path.cwd() / "intraday.sqlite3"),
        ]
    )

    assert result == 2
    assert "paper_simulation_lock_args_incomplete" in capsys.readouterr().err


def test_stream_main_wraps_rest_client_in_simulation_read_only_transport(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sentinel_transport = object()
    captured: dict[str, Any] = {}
    monkeypatch.setenv("TOSS_CLIENT_ID", "id")
    monkeypatch.setenv("TOSS_CLIENT_SECRET", "secret")
    monkeypatch.setattr(
        toss_stream_module,
        "SimulationReadOnlyTossTransport",
        lambda: sentinel_transport,
    )

    def fake_client(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(toss_stream_module, "TossClient", fake_client)
    monkeypatch.setattr(
        toss_stream_module,
        "run_shadow_stream",
        lambda _config, **kwargs: captured.update(kwargs) or 0,
    )

    result = toss_stream_module.main(
        [
            "--context",
            str((tmp_path / "news-context.json").resolve()),
            "--snapshot",
            str((tmp_path / "market-stream.json").resolve()),
            "--once",
        ]
    )

    assert result == 0
    assert captured["transport"] is sentinel_transport
    assert captured["account_seq"] is None


def test_paper_stream_restart_in_entry_window_is_a_durable_data_gap(
    tmp_path: Path,
) -> None:
    config_path, plan_db, _paper_config, plan_id = _paper_stream_fixture(tmp_path)
    clock = FakeClock()
    first_state = _state(clock)
    first = toss_stream_module._PaperStreamSink(
        simulation_config=config_path,
        plan_db=plan_db,
        event_max_age_seconds=15,
        monotonic=clock.monotonic,
    )
    try:
        for frame in (_trade(clock), _book(clock)):
            kind = _handle_frame(
                frame,
                state=first_state,
                received_at=clock.now(),
                max_age_seconds=15,
                future_tolerance_seconds=2,
            )
            first(first_state, kind, clock.now())
    finally:
        first.close()

    clock.advance(120)
    restarted_state = _state(clock)
    restarted = toss_stream_module._PaperStreamSink(
        simulation_config=config_path,
        plan_db=plan_db,
        event_max_age_seconds=15,
        monotonic=clock.monotonic,
    )
    try:
        kind = _handle_frame(
            _trade(clock),
            state=restarted_state,
            received_at=clock.now(),
            max_age_seconds=15,
            future_tolerance_seconds=2,
        )
        restarted(restarted_state, kind, clock.now())
        plan = restarted.paper_store.load_plan(plan_id)
        assert plan["status"] == "INVALID"
        assert plan["data_gap_count"] == 1
        assert plan["exit_reason"] == "stream_process_restarted"
    finally:
        restarted.close()


def test_paper_stream_hard_crash_before_flush_is_detected_on_restart(
    tmp_path: Path,
) -> None:
    config_path, plan_db, _paper_config, plan_id = _paper_stream_fixture(tmp_path)
    clock = FakeClock()
    clock.advance(120)
    state = _state(clock)
    crashed = toss_stream_module._PaperStreamSink(
        simulation_config=config_path,
        plan_db=plan_db,
        event_max_age_seconds=30,
        monotonic=clock.monotonic,
    )
    kind = _handle_frame(
        _trade(clock),
        state=state,
        received_at=clock.now(),
        max_age_seconds=15,
        future_tolerance_seconds=2,
    )
    crashed(state, kind, clock.now())
    assert crashed.paper_store.pending_event_count == 1
    assert crashed.paper_store.load_plan(plan_id)["journaled_frame_count"] == 0

    # Model abrupt process death: no flush and no clean instance end marker.
    crashed.paper_store._conn.close()
    crashed.plan_store.close()
    crashed.closed = True

    restarted = toss_stream_module._PaperStreamSink(
        simulation_config=config_path,
        plan_db=plan_db,
        event_max_age_seconds=30,
        monotonic=clock.monotonic,
    )
    try:
        restarted(state, kind, clock.now())
        plan = restarted.paper_store.load_plan(plan_id)
        assert plan["status"] == "INVALID"
        assert plan["exit_reason"] == "stream_process_interrupted"
        assert plan["data_gap_count"] == 1
    finally:
        restarted.close()


def test_stale_crash_marker_invalidates_before_planner_can_finalize_cleanly(
    tmp_path: Path,
) -> None:
    config_path, plan_db, paper_config, plan_id = _paper_stream_fixture(tmp_path)
    clock = FakeClock()
    state = _state(clock)
    crashed = toss_stream_module._PaperStreamSink(
        simulation_config=config_path,
        plan_db=plan_db,
        event_max_age_seconds=15,
        monotonic=clock.monotonic,
    )
    crashed(state, "start", clock.now())
    clock.advance(30 * 60 - 2)
    crashed(state, "tick", clock.now())
    book_kind = _handle_frame(
        _book(clock),
        state=state,
        received_at=clock.now(),
        max_age_seconds=15,
        future_tolerance_seconds=2,
    )
    crashed(state, book_kind, clock.now())
    clock.advance(1)
    trade = _trade(clock)
    assert isinstance(trade["data"], dict)
    trade["data"]["price"] = "99.00"
    trade_kind = _handle_frame(
        trade,
        state=state,
        received_at=clock.now(),
        max_age_seconds=15,
        future_tolerance_seconds=2,
    )
    crashed(state, trade_kind, clock.now())
    crashed.paper_store.flush_pending()
    assert crashed.paper_store.load_plan(plan_id)["status"] == "WAITING_ENTRY"

    # Abrupt death leaves the durable liveness marker open.
    crashed.paper_store._conn.close()
    crashed.plan_store.close()
    crashed.closed = True
    clock.advance(14)

    with IntradayPaperStore(
        tmp_path / "private" / "intraday-paper.sqlite3", paper_config
    ) as reopened:
        deferred = reopened.finalize_session(plan_id, now=clock.now())
        assert deferred["status"] == "WAITING_ENTRY"
        clock.advance(2)
        finalized = reopened.finalize_session(plan_id, now=clock.now())
        assert finalized["status"] == "INVALID"
        assert finalized["exit_reason"] == "stream_coverage_incomplete"
        assert finalized["data_gap_count"] == 1


def test_one_silent_topic_reconnects_and_durably_invalidates_sensitive_window(
    tmp_path: Path,
) -> None:
    config_path, plan_db, _paper_config, plan_id = _paper_stream_fixture(tmp_path)
    clock = FakeClock()
    clock.advance(120)
    config = _config(
        tmp_path,
        clock,
        active_seconds=60,
        event_max_age_seconds=15,
        rest_resync_seconds=600,
    )
    connection = FakeConnection(
        clock,
        [Incoming(0, _ack()), Incoming(0, lambda: _trade(clock)), Silence(16)],
    )
    sink = toss_stream_module._PaperStreamSink(
        simulation_config=config_path,
        plan_db=plan_db,
        event_max_age_seconds=15,
        monotonic=clock.monotonic,
    )
    try:
        result = run_shadow_stream(
            config,
            client=FakeReadOnlyClient(clock),  # type: ignore[arg-type]
            connector=FakeConnector([connection]),
            now=clock.now,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            jitter=lambda: 0,
            event_sink=sink,
        )

        plan = sink.paper_store.load_plan(plan_id)
        snapshot = json.loads(config.snapshot_path.read_text(encoding="utf-8"))
        assert result == 1
        assert plan["status"] == "INVALID"
        assert plan["exit_reason"] == "orderbook_topic_silent"
        assert plan["data_gap_count"] == 1
        assert snapshot["last_disconnect_error"] == "orderbook_topic_silent"
    finally:
        sink.close()


def test_paper_stream_tick_flushes_a_single_tail_and_uses_manifest_tolerances(
    tmp_path: Path,
) -> None:
    config_path, plan_db, _paper_config, plan_id = _paper_stream_fixture(tmp_path)
    clock = FakeClock()
    state = _state(clock)
    sink = toss_stream_module._PaperStreamSink(
        simulation_config=config_path,
        plan_db=plan_db,
        event_max_age_seconds=30,
        monotonic=clock.monotonic,
    )
    try:
        assert sink.event_max_age_seconds == 15
        assert sink.event_future_tolerance_seconds == 2
        kind = _handle_frame(
            _trade(clock),
            state=state,
            received_at=clock.now(),
            max_age_seconds=15,
            future_tolerance_seconds=2,
        )
        sink(state, kind, clock.now())
        assert sink.paper_store.pending_event_count == 1
        clock.advance(1)
        sink(state, "tick", clock.now())
        assert sink.paper_store.pending_event_count == 0
        assert sink.paper_store.load_plan(plan_id)["journaled_frame_count"] == 1
    finally:
        sink.close()


def test_paper_stream_missing_locked_plan_and_path_mismatch_fail_closed(
    tmp_path: Path,
) -> None:
    config_path, plan_db, _paper_config, _plan_id = _paper_stream_fixture(tmp_path)
    with pytest.raises(StreamError, match="paper_simulation_runtime_path_mismatch"):
        toss_stream_module._PaperStreamSink(
            simulation_config=config_path,
            plan_db=plan_db,
            context_path=(tmp_path / "wrong-news-context.json").resolve(),
            event_max_age_seconds=15,
        )

    with sqlite3.connect(plan_db) as database:
        database.execute("DELETE FROM intraday_plans")
    clock = FakeClock()
    state = _state(clock)
    sink = toss_stream_module._PaperStreamSink(
        simulation_config=config_path,
        plan_db=plan_db,
        event_max_age_seconds=15,
        monotonic=clock.monotonic,
    )
    try:
        with pytest.raises(StreamError, match="paper_plan_missing_for_locked_context"):
            sink(state, "tick", clock.now())
    finally:
        sink.close()
