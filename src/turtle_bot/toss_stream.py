from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import random
import re
import stat
import sys
import tempfile
import time
from typing import Any, Callable, Iterator, Mapping, Protocol
from uuid import uuid4

from turtle_news.worker import (
    NewsDigestError,
    SelectedContext,
    load_contexts,
)

from .toss_client import (
    TOSS_BASE_URL,
    SimulationReadOnlyTossTransport,
    TossApiError,
    TossClient,
    TossCredentials,
    TossToken,
)
from .config import TradingConfig, intraday_simulation_experiment_hash, load_config
from .intraday_paper import (
    IntradayPaperConfig,
    IntradayPaperStore,
    PaperSimulationError,
    PaperStreamInstanceInactive,
    assert_simulation_topology,
    simulation_account_key,
)
from .state_store import SQLiteStateStore


WS_URL = "wss://openapi-ws.tossinvest.com/ws/v1"
ASYNCAPI_VERSION = "1.2.2"
ASYNCAPI_SHA256 = "130251057fd9535a3e276099f9166b445f8c51f505f30540758e4b209231282e"
MAX_FRAME_BYTES = 65_536
_DECIMAL_RE = re.compile(r"\d+(?:\.\d+)?\Z")
_SYMBOL_RE = re.compile(r"[A-Z][A-Z0-9]*(?:[.-][A-Z0-9]+)?\Z")
_ORDERBOOK_UNAVAILABLE_CODES = frozenset(
    {
        "orderbook_crossed",
        "orderbook_empty",
        "orderbook_level_unavailable",
        "orderbook_sort_unverified",
    }
)


class StreamError(RuntimeError):
    """Expected fail-closed error whose code is safe to persist or print."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _StreamComplete(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class WebSocketConnection(Protocol):
    def send(self, message: str) -> None:
        ...

    def recv(self, timeout: float | None = None) -> str | bytes:
        ...

    def close(self) -> None:
        ...


@dataclass(frozen=True)
class StreamConfig:
    context_path: Path
    snapshot_path: Path
    expected_symbols: tuple[str, ...] | None = None
    once: bool = False
    context_max_age_seconds: int = 300
    context_check_interval_seconds: float = 1.0
    ack_timeout_seconds: float = 10.0
    receive_poll_seconds: float = 1.0
    ping_interval_seconds: float = 60.0
    pong_timeout_seconds: float = 15.0
    rest_resync_seconds: float = 30.0
    event_max_age_seconds: float = 30.0
    event_future_tolerance_seconds: float = 5.0
    snapshot_interval_seconds: float = 1.0
    max_reconnect_attempts: int = 8
    max_backoff_seconds: float = 30.0

    def validate(self) -> None:
        if not self.context_path.is_absolute() or self.context_path.name != "news-context.json":
            raise StreamError("context_path_invalid")
        if not self.snapshot_path.is_absolute() or self.snapshot_path.name != "market-stream.json":
            raise StreamError("snapshot_path_invalid")
        if not 1 <= self.context_max_age_seconds <= 3_600:
            raise StreamError("stream_config_invalid")
        positive = (
            self.ack_timeout_seconds,
            self.context_check_interval_seconds,
            self.receive_poll_seconds,
            self.ping_interval_seconds,
            self.pong_timeout_seconds,
            self.rest_resync_seconds,
            self.event_max_age_seconds,
            self.event_future_tolerance_seconds,
            self.snapshot_interval_seconds,
            self.max_backoff_seconds,
        )
        if any(value <= 0 for value in positive):
            raise StreamError("stream_config_invalid")
        if not 1 <= self.max_reconnect_attempts <= 100:
            raise StreamError("stream_config_invalid")
        if self.expected_symbols is not None:
            _validate_symbols(self.expected_symbols)


@dataclass
class ShadowStreamState:
    context: SelectedContext
    generation: int = 0
    connected: bool = False
    acknowledged: bool = False
    rest_resynced_at: datetime | None = None
    baseline: dict[str, Any] | None = None
    trade: dict[str, Any] | None = None
    orderbook: dict[str, Any] | None = None
    trade_generation: int | None = None
    orderbook_generation: int | None = None
    connection_error: str | None = None
    last_disconnect_error: str | None = None
    trade_error: str | None = None
    orderbook_error: str | None = None
    reconnect_count: int = 0
    consecutive_failures: int = 0
    healthy_once: bool = False

    @property
    def topics(self) -> tuple[str, str]:
        symbol = self.context.symbol
        return (f"trade:us:{symbol}", f"orderbook:us:{symbol}")

    def begin_connection(self) -> None:
        self.generation += 1
        self.connected = True
        self.acknowledged = False
        self.rest_resynced_at = None
        self.baseline = None
        self.trade = None
        self.orderbook = None
        self.trade_generation = None
        self.orderbook_generation = None
        self.connection_error = None
        self.trade_error = None
        self.orderbook_error = None
        self.healthy_once = False

    def disconnect(self, code: str) -> None:
        self.connected = False
        self.acknowledged = False
        self.trade_generation = None
        self.orderbook_generation = None
        self.connection_error = code
        self.last_disconnect_error = code
        self.healthy_once = False

    def as_payload(self, *, now: datetime, event_max_age_seconds: float) -> dict[str, Any]:
        now_utc = _utc(now)
        trade_fresh = _stream_value_fresh(self.trade, now_utc, event_max_age_seconds)
        book_fresh = _stream_value_fresh(self.orderbook, now_utc, event_max_age_seconds)
        baseline_verified = bool(
            isinstance(self.baseline, Mapping) and self.baseline.get("verified") is True
        )
        shadow_usable = bool(
            self.connected
            and self.acknowledged
            and self.rest_resynced_at is not None
            and baseline_verified
            and now_utc < self.context.active_until
            and self.trade_generation == self.generation
            and self.orderbook_generation == self.generation
            and self.trade_error is None
            and self.orderbook_error is None
            and trade_fresh
            and book_fresh
        )
        error_codes = sorted(
            {
                code
                for code in (self.connection_error, self.trade_error, self.orderbook_error)
                if code
            }
        )
        if isinstance(self.baseline, Mapping):
            baseline_errors = self.baseline.get("error_codes")
            if isinstance(baseline_errors, list):
                error_codes = sorted(
                    set(error_codes).union(
                        code for code in baseline_errors if isinstance(code, str)
                    )
                )
        valid_until = now_utc
        if shadow_usable:
            expiries = [self.context.active_until]
            for value in (self.trade, self.orderbook):
                for key in ("broker_at", "received_at"):
                    timestamp = _mapping_datetime(value, key)
                    if timestamp is not None:
                        expiries.append(timestamp + timedelta(seconds=event_max_age_seconds))
            valid_until = min(expiries)
        return {
            "schema_version": 1,
            "mode": "shadow",
            "live_order_submission": False,
            "ready_for_live_entry": False,
            "symbol": self.context.symbol,
            "session_date": self.context.session_date,
            "active_until": self.context.active_until.isoformat(),
            "asyncapi_version": ASYNCAPI_VERSION,
            "asyncapi_sha256": ASYNCAPI_SHA256,
            "generation": self.generation,
            "connected": self.connected,
            "subscription_acknowledged": self.acknowledged,
            "subscribed_topics": list(self.topics) if self.acknowledged else [],
            "rest_resynced_at": _iso_or_none(self.rest_resynced_at),
            "updated_at": now_utc.isoformat(),
            "valid_until": valid_until.isoformat(),
            "shadow_usable": shadow_usable,
            "reconnect_count": self.reconnect_count,
            "consecutive_failures": self.consecutive_failures,
            "error_codes": error_codes,
            "last_disconnect_error": self.last_disconnect_error,
            "baseline": self.baseline,
            "trade": self.trade,
            "orderbook": self.orderbook,
        }


@dataclass
class CohortStreamState:
    """One connection with isolated per-symbol market state."""

    states: dict[str, ShadowStreamState]
    generation: int = 0
    connected: bool = False
    acknowledged: bool = False
    connection_error: str | None = None
    last_disconnect_error: str | None = None
    reconnect_count: int = 0
    consecutive_failures: int = 0
    healthy_once: bool = False

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(self.states)

    @property
    def topics(self) -> tuple[str, ...]:
        return tuple(topic for state in self.states.values() for topic in state.topics)

    @property
    def active_until(self) -> datetime:
        return min(state.context.active_until for state in self.states.values())

    def begin_connection(self) -> None:
        self.generation += 1
        self.connected = True
        self.acknowledged = False
        self.connection_error = None
        self.healthy_once = False
        for state in self.states.values():
            state.begin_connection()
            state.generation = self.generation

    def disconnect(self, code: str) -> None:
        self.connected = False
        self.acknowledged = False
        self.connection_error = code
        self.last_disconnect_error = code
        self.healthy_once = False
        for state in self.states.values():
            state.disconnect(code)

    def as_payload(self, *, now: datetime, event_max_age_seconds: float) -> dict[str, Any]:
        streams = {
            symbol: state.as_payload(
                now=now,
                event_max_age_seconds=event_max_age_seconds,
            )
            for symbol, state in self.states.items()
        }
        usable = bool(streams) and all(
            value.get("shadow_usable") is True for value in streams.values()
        )
        error_codes = sorted(
            {
                code
                for value in streams.values()
                for code in value.get("error_codes", [])
                if isinstance(code, str)
            }
            | ({self.connection_error} if self.connection_error else set())
        )
        valid_until = min(
            _aware_datetime(value["valid_until"], "snapshot_time_invalid")
            for value in streams.values()
        )
        return {
            "schema_version": 2,
            "mode": "shadow",
            "live_order_submission": False,
            "ready_for_live_entry": False,
            "symbols": list(self.symbols),
            "asyncapi_version": ASYNCAPI_VERSION,
            "asyncapi_sha256": ASYNCAPI_SHA256,
            "generation": self.generation,
            "connected": self.connected,
            "subscription_acknowledged": self.acknowledged,
            "subscribed_topics": list(self.topics) if self.acknowledged else [],
            "updated_at": _utc(now).isoformat(),
            "valid_until": valid_until.isoformat(),
            "shadow_usable": usable,
            "reconnect_count": self.reconnect_count,
            "consecutive_failures": self.consecutive_failures,
            "error_codes": error_codes,
            "last_disconnect_error": self.last_disconnect_error,
            "streams": streams,
        }


@dataclass
class _SnapshotWriter:
    path: Path
    interval_seconds: float
    heartbeat_sink: Callable[[Any, datetime], None] | None = None
    last_written: float = float("-inf")

    def write(
        self,
        state: ShadowStreamState | CohortStreamState,
        *,
        now: datetime,
        monotonic_now: float,
        event_max_age_seconds: float,
        force: bool = False,
    ) -> None:
        if not force and monotonic_now - self.last_written < self.interval_seconds:
            return
        _write_private_json_atomic(
            self.path,
            state.as_payload(now=now, event_max_age_seconds=event_max_age_seconds),
        )
        if self.heartbeat_sink is not None:
            self.heartbeat_sink(state, now)
        self.last_written = monotonic_now


def _validate_symbols(symbols: tuple[str, ...]) -> None:
    if (
        type(symbols) is not tuple
        or not 1 <= len(symbols) <= 2
        or any(
            type(symbol) is not str
            or not 1 <= len(symbol) <= 16
            or not _SYMBOL_RE.fullmatch(symbol)
            for symbol in symbols
        )
        or len(set(symbols)) != len(symbols)
    ):
        raise StreamError("context_symbol_invalid")


def _validate_subscription_inputs(
    symbols: tuple[str, ...], request_id: str
) -> None:
    _validate_symbols(symbols)
    if (
        type(request_id) is not str
        or not request_id
        or len(request_id) > 80
        or any(ch.isspace() for ch in request_id)
    ):
        raise StreamError("subscription_request_id_invalid")


def subscription_declaration(symbol: str, request_id: str) -> list[dict[str, Any]]:
    return subscription_declaration_for_symbols((symbol,), request_id)


def subscription_declaration_for_symbols(
    symbols: tuple[str, ...], request_id: str
) -> list[dict[str, Any]]:
    _validate_subscription_inputs(symbols, request_id)
    return [
        {"id": request_id},
        {"type": "trade:us", "codes": list(symbols)},
        {"type": "orderbook:us", "codes": list(symbols)},
    ]


def validate_subscription_ack(
    value: object,
    *,
    symbol: str,
    request_id: str,
) -> None:
    validate_subscription_ack_for_symbols(
        value,
        symbols=(symbol,),
        request_id=request_id,
    )


def validate_subscription_ack_for_symbols(
    value: object,
    *,
    symbols: tuple[str, ...],
    request_id: str,
) -> None:
    _validate_subscription_inputs(symbols, request_id)
    if not isinstance(value, Mapping):
        raise StreamError("subscription_ack_malformed")
    if value.get("type") != "subscriptions" or value.get("id") != request_id:
        raise StreamError("subscription_ack_mismatch")
    subscribed = value.get("subscribed")
    rejected = value.get("rejected")
    expected = {
        f"{kind}:us:{symbol}"
        for symbol in symbols
        for kind in ("trade", "orderbook")
    }
    if (
        not isinstance(subscribed, list)
        or any(not isinstance(item, str) for item in subscribed)
        or len(subscribed) != len(set(subscribed))
        or set(subscribed) != expected
    ):
        raise StreamError("subscription_ack_incomplete")
    if not isinstance(rejected, list) or rejected:
        raise StreamError("subscription_rejected")


def run_shadow_stream(
    config: StreamConfig,
    *,
    client: TossClient,
    connector: Callable[[TossToken], WebSocketConnection] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[], float] = random.random,
    event_sink: Callable[[ShadowStreamState, str, datetime], None] | None = None,
    heartbeat_sink: Callable[[ShadowStreamState, datetime], None] | None = None,
) -> int:
    config.validate()
    _prepare_snapshot_directory(config.snapshot_path)

    with _exclusive_lock(config.snapshot_path.with_name(".market-stream.lock")):
        started_at = now()
        _write_private_json_atomic(
            config.snapshot_path,
            _startup_snapshot(started_at, "stream_starting"),
        )
        try:
            connection_factory = connector or _default_connector(config)
            contexts = _load_contexts(config, started_at)
        except StreamError as exc:
            _write_private_json_atomic(
                config.snapshot_path,
                _startup_snapshot(now(), exc.code),
            )
            raise
        if len(contexts) > 1:
            return _run_cohort_shadow_stream_locked(
                config,
                contexts=contexts,
                client=client,
                connector=connection_factory,
                now=now,
                monotonic=monotonic,
                sleep=sleep,
                jitter=jitter,
                event_sink=event_sink,
                heartbeat_sink=heartbeat_sink,
            )
        context = contexts[0]
        state = ShadowStreamState(context=context)
        writer = _SnapshotWriter(
            config.snapshot_path,
            config.snapshot_interval_seconds,
            heartbeat_sink=heartbeat_sink,
        )
        failures = 0
        writer.write(
            state,
            now=now(),
            monotonic_now=monotonic(),
            event_max_age_seconds=config.event_max_age_seconds,
            force=True,
        )
        if event_sink is not None:
            _emit_stream_event(event_sink, state, "start", now())

        while True:
            current = now()
            if _utc(current) >= state.context.active_until:
                state.disconnect("context_inactive")
                state.connection_error = None
                writer.write(
                    state,
                    now=current,
                    monotonic_now=monotonic(),
                    event_max_age_seconds=config.event_max_age_seconds,
                    force=True,
                )
                return 0
            try:
                healthy_connection = False
                _serve_connection(
                    config,
                    state=state,
                    writer=writer,
                    client=client,
                    connector=connection_factory,
                    now=now,
                    monotonic=monotonic,
                    event_sink=event_sink,
                )
            except _StreamComplete as complete:
                state.disconnect(complete.reason)
                state.connection_error = None
                writer.write(
                    state,
                    now=now(),
                    monotonic_now=monotonic(),
                    event_max_age_seconds=config.event_max_age_seconds,
                    force=True,
                )
                return 0
            except StreamError as exc:
                healthy_connection = state.healthy_once
                state.disconnect(exc.code)
            except Exception:
                healthy_connection = state.healthy_once
                state.disconnect("stream_internal_error")

            if event_sink is not None:
                try:
                    _emit_stream_event(event_sink, state, "disconnect", now())
                except StreamError as exc:
                    state.disconnect(exc.code)

            if healthy_connection:
                failures = 0
            failures += 1
            state.reconnect_count += 1
            state.consecutive_failures = failures
            writer.write(
                state,
                now=now(),
                monotonic_now=monotonic(),
                event_max_age_seconds=config.event_max_age_seconds,
                force=True,
            )
            if state.connection_error and state.connection_error.startswith("context_"):
                return 2
            if state.connection_error and state.connection_error.startswith("paper_"):
                return 2
            if state.connection_error in {
                "websocket_dependency_missing",
                "rest_baseline_unverified",
                "oauth_rejected",
                "ws_auth_rejected",
                "ws_ip_not_allowed",
                "subscription_ack_mismatch",
                "subscription_ack_incomplete",
                "subscription_rejected",
            }:
                return 2
            if failures >= config.max_reconnect_attempts:
                state.connection_error = "reconnect_limit_reached"
                writer.write(
                    state,
                    now=now(),
                    monotonic_now=monotonic(),
                    event_max_age_seconds=config.event_max_age_seconds,
                    force=True,
                )
                return 1
            delay = min(config.max_backoff_seconds, 2 ** (failures - 1))
            delay += delay * 0.2 * min(1.0, max(0.0, float(jitter())))
            sleep(delay)


def _run_cohort_shadow_stream_locked(
    config: StreamConfig,
    *,
    contexts: tuple[SelectedContext, ...],
    client: TossClient,
    connector: Callable[[TossToken], WebSocketConnection],
    now: Callable[[], datetime],
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
    jitter: Callable[[], float],
    event_sink: Callable[[ShadowStreamState, str, datetime], None] | None,
    heartbeat_sink: Callable[[Any, datetime], None] | None,
) -> int:
    if len(contexts) != 2:
        raise StreamError("context_symbol_invalid")
    cohort = CohortStreamState(
        {context.symbol: ShadowStreamState(context=context) for context in contexts}
    )
    writer = _SnapshotWriter(
        config.snapshot_path,
        config.snapshot_interval_seconds,
        heartbeat_sink=heartbeat_sink,
    )
    writer.write(
        cohort,
        now=now(),
        monotonic_now=monotonic(),
        event_max_age_seconds=config.event_max_age_seconds,
        force=True,
    )
    for state in cohort.states.values():
        _emit_stream_event(event_sink, state, "start", now())
    failures = 0
    fatal = {
        "websocket_dependency_missing",
        "rest_baseline_unverified",
        "oauth_rejected",
        "ws_auth_rejected",
        "ws_ip_not_allowed",
        "subscription_ack_mismatch",
        "subscription_ack_incomplete",
        "subscription_rejected",
    }
    while True:
        current = now()
        if _utc(current) >= cohort.active_until:
            cohort.disconnect("context_inactive")
            cohort.connection_error = None
            writer.write(
                cohort,
                now=current,
                monotonic_now=monotonic(),
                event_max_age_seconds=config.event_max_age_seconds,
                force=True,
            )
            return 0
        try:
            healthy_connection = False
            _serve_cohort_connection(
                config,
                cohort=cohort,
                writer=writer,
                client=client,
                connector=connector,
                now=now,
                monotonic=monotonic,
                event_sink=event_sink,
            )
        except _StreamComplete as complete:
            cohort.disconnect(complete.reason)
            cohort.connection_error = None
            writer.write(
                cohort,
                now=now(),
                monotonic_now=monotonic(),
                event_max_age_seconds=config.event_max_age_seconds,
                force=True,
            )
            return 0
        except StreamError as exc:
            healthy_connection = cohort.healthy_once
            cohort.disconnect(exc.code)
        except Exception:
            healthy_connection = cohort.healthy_once
            cohort.disconnect("stream_internal_error")

        fanout_error: StreamError | None = None
        for state in cohort.states.values():
            try:
                _emit_stream_event(event_sink, state, "disconnect", now())
            except StreamError as exc:
                fanout_error = fanout_error or exc
        if fanout_error is not None:
            cohort.disconnect(fanout_error.code)
        failures = 0 if healthy_connection else failures
        failures += 1
        cohort.reconnect_count += 1
        cohort.consecutive_failures = failures
        writer.write(
            cohort,
            now=now(),
            monotonic_now=monotonic(),
            event_max_age_seconds=config.event_max_age_seconds,
            force=True,
        )
        if cohort.connection_error and cohort.connection_error.startswith("context_"):
            return 2
        if cohort.connection_error in fatal or (
            cohort.connection_error is not None
            and cohort.connection_error.startswith("paper_")
        ):
            return 2
        if failures >= config.max_reconnect_attempts:
            cohort.connection_error = "reconnect_limit_reached"
            writer.write(
                cohort,
                now=now(),
                monotonic_now=monotonic(),
                event_max_age_seconds=config.event_max_age_seconds,
                force=True,
            )
            return 1
        delay = min(config.max_backoff_seconds, 2 ** (failures - 1))
        delay += delay * 0.2 * min(1.0, max(0.0, float(jitter())))
        sleep(delay)


def _stream_token(client: TossClient, at: datetime) -> TossToken:
    try:
        token = client.token
        if token is None or token.is_expiring(now=_utc(at)):
            token = client.issue_token()
        return token
    except StreamError:
        raise
    except TossApiError as exc:
        if exc.status in {400, 401, 403}:
            raise StreamError("oauth_rejected") from exc
        raise StreamError("oauth_failed") from exc
    except Exception as exc:
        raise StreamError("oauth_failed") from exc


def _serve_connection(
    config: StreamConfig,
    *,
    state: ShadowStreamState,
    writer: _SnapshotWriter,
    client: TossClient,
    connector: Callable[[TossToken], WebSocketConnection],
    now: Callable[[], datetime],
    monotonic: Callable[[], float],
    event_sink: Callable[[ShadowStreamState, str, datetime], None] | None,
) -> None:
    state.context = _refresh_context(config, state.context, now())
    token = _stream_token(client, now())
    try:
        connection = connector(token)
    except StreamError:
        raise
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 401:
            raise StreamError("ws_auth_rejected") from exc
        if status == 403:
            raise StreamError("ws_ip_not_allowed") from exc
        raise StreamError("ws_connect_failed") from exc

    state.begin_connection()
    request_id = f"shadow-{state.context.session_date}-{state.generation}"
    declaration = subscription_declaration(state.context.symbol, request_id)
    try:
        connection.send(json.dumps(declaration, separators=(",", ":")))
        ack = _recv_json(connection, config.ack_timeout_seconds)
        validate_subscription_ack(ack, symbol=state.context.symbol, request_id=request_id)
        state.acknowledged = True
        state.baseline = _rest_resync(
            client,
            state.context.symbol,
            now=now,
            max_age_seconds=config.event_max_age_seconds,
            future_tolerance_seconds=config.event_future_tolerance_seconds,
        )
        state.rest_resynced_at = _aware_datetime(
            state.baseline["captured_at"], "rest_resync_time_invalid"
        )
        if event_sink is not None:
            _emit_stream_event(event_sink, state, "baseline", now())
        post_resync_at = now()
        if _utc(post_resync_at) >= state.context.active_until:
            if config.once:
                raise StreamError("context_inactive")
            raise _StreamComplete("context_inactive")
        state.context = _refresh_context(config, state.context, post_resync_at)
        state.connection_error = None
        started = monotonic()
        writer.write(
            state,
            now=now(),
            monotonic_now=started,
            event_max_age_seconds=config.event_max_age_seconds,
            force=True,
        )
        if config.once and state.baseline.get("verified") is not True:
            raise StreamError("rest_baseline_unverified")
        if config.once:
            raise _StreamComplete("once_complete")

        last_ping = started
        last_resync = started
        last_context_check = started
        awaiting_pong_at: float | None = None
        while True:
            observed_at = now()
            if _utc(observed_at) >= state.context.active_until:
                raise _StreamComplete("context_inactive")
            loop_started = monotonic()
            if (
                loop_started - last_context_check
                >= config.context_check_interval_seconds
            ):
                state.context = _refresh_context(config, state.context, observed_at)
                last_context_check = loop_started

            try:
                raw = connection.recv(timeout=config.receive_poll_seconds)
            except TimeoutError:
                raw = None
            except Exception as exc:
                raise StreamError("ws_connection_lost") from exc

            tick = monotonic()
            if awaiting_pong_at is not None and tick - awaiting_pong_at > config.pong_timeout_seconds:
                raise StreamError("pong_timeout")
            if raw is not None:
                frame = _decode_frame(raw)
                kind = _handle_frame(
                    frame,
                    state=state,
                    received_at=now(),
                    max_age_seconds=config.event_max_age_seconds,
                    future_tolerance_seconds=config.event_future_tolerance_seconds,
                )
                if event_sink is not None and kind in {"trade", "orderbook"}:
                    _emit_stream_event(event_sink, state, kind, now())
                if kind == "pong":
                    awaiting_pong_at = None

            topic_checked_at = now()
            if (
                event_sink is not None
                and _utc(topic_checked_at) < state.context.active_until
            ):
                silent_topic = _silent_topic_error(
                    state,
                    now=topic_checked_at,
                    max_age_seconds=config.event_max_age_seconds,
                )
                if silent_topic is not None:
                    raise StreamError(silent_topic)

            if (
                awaiting_pong_at is None
                and tick - last_resync >= config.rest_resync_seconds
            ):
                state.baseline = _rest_resync(
                    client,
                    state.context.symbol,
                    now=now,
                    max_age_seconds=config.event_max_age_seconds,
                    future_tolerance_seconds=config.event_future_tolerance_seconds,
                )
                state.rest_resynced_at = _aware_datetime(
                    state.baseline["captured_at"], "rest_resync_time_invalid"
                )
                state.trade_generation = None
                state.orderbook_generation = None
                if event_sink is not None:
                    _emit_stream_event(event_sink, state, "baseline", now())
                last_resync = monotonic()
                tick = last_resync
            if awaiting_pong_at is None and tick - last_ping >= config.ping_interval_seconds:
                try:
                    connection.send("PING")
                except Exception as exc:
                    raise StreamError("ping_send_failed") from exc
                awaiting_pong_at = tick
                last_ping = tick
            if event_sink is not None:
                _emit_stream_event(event_sink, state, "tick", now())
            writer.write(
                state,
                now=now(),
                monotonic_now=tick,
                event_max_age_seconds=config.event_max_age_seconds,
            )
    except _StreamComplete:
        raise
    except StreamError:
        raise
    except Exception as exc:
        raise StreamError("ws_connection_lost") from exc
    finally:
        try:
            connection.close()
        except Exception:
            pass


def _serve_cohort_connection(
    config: StreamConfig,
    *,
    cohort: CohortStreamState,
    writer: _SnapshotWriter,
    client: TossClient,
    connector: Callable[[TossToken], WebSocketConnection],
    now: Callable[[], datetime],
    monotonic: Callable[[], float],
    event_sink: Callable[[ShadowStreamState, str, datetime], None] | None,
) -> None:
    refreshed = _refresh_contexts(
        config,
        tuple(state.context for state in cohort.states.values()),
        now(),
    )
    for context in refreshed:
        cohort.states[context.symbol].context = context
    token = _stream_token(client, now())
    try:
        connection = connector(token)
    except StreamError:
        raise
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 401:
            raise StreamError("ws_auth_rejected") from exc
        if status == 403:
            raise StreamError("ws_ip_not_allowed") from exc
        raise StreamError("ws_connect_failed") from exc

    cohort.begin_connection()
    session_date = next(iter(cohort.states.values())).context.session_date
    request_id = f"shadow-{session_date}-{cohort.generation}"
    declaration = subscription_declaration_for_symbols(cohort.symbols, request_id)
    topic_routes = {
        topic: state
        for state in cohort.states.values()
        for topic in state.topics
    }
    try:
        connection.send(json.dumps(declaration, separators=(",", ":")))
        ack = _recv_json(connection, config.ack_timeout_seconds)
        validate_subscription_ack_for_symbols(
            ack,
            symbols=cohort.symbols,
            request_id=request_id,
        )
        cohort.acknowledged = True
        for state in cohort.states.values():
            state.acknowledged = True
            try:
                state.baseline = _rest_resync(
                    client,
                    state.context.symbol,
                    now=now,
                    max_age_seconds=config.event_max_age_seconds,
                    future_tolerance_seconds=config.event_future_tolerance_seconds,
                )
            except StreamError as exc:
                _mark_lane_baseline_error(state, exc.code, now())
                _emit_stream_event(event_sink, state, "trade", now())
            else:
                state.rest_resynced_at = _aware_datetime(
                    state.baseline["captured_at"], "rest_resync_time_invalid"
                )
                _emit_stream_event(event_sink, state, "baseline", now())
        post_resync_at = now()
        if _utc(post_resync_at) >= cohort.active_until:
            if config.once:
                raise StreamError("context_inactive")
            raise _StreamComplete("context_inactive")
        refreshed = _refresh_contexts(
            config,
            tuple(state.context for state in cohort.states.values()),
            post_resync_at,
        )
        for context in refreshed:
            cohort.states[context.symbol].context = context
        cohort.connection_error = None
        started = monotonic()
        writer.write(
            cohort,
            now=now(),
            monotonic_now=started,
            event_max_age_seconds=config.event_max_age_seconds,
            force=True,
        )
        if config.once and any(
            state.baseline.get("verified") is not True
            for state in cohort.states.values()
        ):
            raise StreamError("rest_baseline_unverified")
        if config.once:
            raise _StreamComplete("once_complete")

        last_ping = started
        last_resync = started
        last_context_check = started
        awaiting_pong_at: float | None = None
        while True:
            observed_at = now()
            if _utc(observed_at) >= cohort.active_until:
                raise _StreamComplete("context_inactive")
            loop_started = monotonic()
            if loop_started - last_context_check >= config.context_check_interval_seconds:
                refreshed = _refresh_contexts(
                    config,
                    tuple(state.context for state in cohort.states.values()),
                    observed_at,
                )
                for context in refreshed:
                    cohort.states[context.symbol].context = context
                last_context_check = loop_started

            try:
                raw = connection.recv(timeout=config.receive_poll_seconds)
            except TimeoutError:
                raw = None
            except Exception as exc:
                raise StreamError("ws_connection_lost") from exc

            tick = monotonic()
            if awaiting_pong_at is not None and tick - awaiting_pong_at > config.pong_timeout_seconds:
                raise StreamError("pong_timeout")
            if raw is not None:
                frame = _decode_frame(raw)
                if isinstance(frame, Mapping) and frame.get("type") == "pong":
                    awaiting_pong_at = None
                else:
                    if (
                        isinstance(frame, Mapping)
                        and frame.get("type") in {"error", "subscriptions"}
                    ):
                        _handle_frame(
                            frame,
                            state=next(iter(cohort.states.values())),
                            received_at=now(),
                            max_age_seconds=config.event_max_age_seconds,
                            future_tolerance_seconds=(
                                config.event_future_tolerance_seconds
                            ),
                        )
                    topic = frame.get("topic") if isinstance(frame, Mapping) else None
                    state = topic_routes.get(topic) if isinstance(topic, str) else None
                    if state is None:
                        raise StreamError("ws_topic_unexpected")
                    try:
                        kind = _handle_frame(
                            frame,
                            state=state,
                            received_at=now(),
                            max_age_seconds=config.event_max_age_seconds,
                            future_tolerance_seconds=(
                                config.event_future_tolerance_seconds
                            ),
                        )
                    except StreamError as exc:
                        kind = _mark_lane_topic_error(state, topic, exc.code)
                    if kind in {"trade", "orderbook"}:
                        _emit_stream_event(event_sink, state, kind, now())

            topic_checked_at = now()
            if _utc(topic_checked_at) < cohort.active_until:
                for state in cohort.states.values():
                    silent = _silent_topic_error(
                        state,
                        now=topic_checked_at,
                        max_age_seconds=config.event_max_age_seconds,
                    )
                    if silent is None:
                        continue
                    topic = (
                        state.topics[0]
                        if silent.startswith("trade_")
                        else state.topics[1]
                    )
                    kind = _mark_lane_topic_error(state, topic, silent)
                    _emit_stream_event(event_sink, state, kind, topic_checked_at)

            if awaiting_pong_at is None and tick - last_resync >= config.rest_resync_seconds:
                for state in cohort.states.values():
                    try:
                        state.baseline = _rest_resync(
                            client,
                            state.context.symbol,
                            now=now,
                            max_age_seconds=config.event_max_age_seconds,
                            future_tolerance_seconds=(
                                config.event_future_tolerance_seconds
                            ),
                        )
                    except StreamError as exc:
                        _mark_lane_baseline_error(state, exc.code, now())
                        _emit_stream_event(event_sink, state, "trade", now())
                    else:
                        state.rest_resynced_at = _aware_datetime(
                            state.baseline["captured_at"],
                            "rest_resync_time_invalid",
                        )
                        state.trade_generation = None
                        state.orderbook_generation = None
                        _emit_stream_event(event_sink, state, "baseline", now())
                last_resync = monotonic()
                tick = last_resync
            if awaiting_pong_at is None and tick - last_ping >= config.ping_interval_seconds:
                try:
                    connection.send("PING")
                except Exception as exc:
                    raise StreamError("ping_send_failed") from exc
                awaiting_pong_at = tick
                last_ping = tick
            cohort.healthy_once = cohort.healthy_once or all(
                state.healthy_once for state in cohort.states.values()
            )
            for state in cohort.states.values():
                _emit_stream_event(event_sink, state, "tick", now())
            writer.write(
                cohort,
                now=now(),
                monotonic_now=tick,
                event_max_age_seconds=config.event_max_age_seconds,
            )
    except _StreamComplete:
        raise
    except StreamError:
        raise
    except Exception as exc:
        raise StreamError("ws_connection_lost") from exc
    finally:
        try:
            connection.close()
        except Exception:
            pass


def _emit_stream_event(
    sink: Callable[[ShadowStreamState, str, datetime], None] | None,
    state: ShadowStreamState,
    kind: str,
    observed_at: datetime,
) -> None:
    if sink is None:
        return
    try:
        sink(state, kind, _utc(observed_at))
    except StreamError:
        raise
    except Exception as exc:
        raise StreamError("paper_simulation_persistence_failed") from exc


def _mark_lane_topic_error(
    state: ShadowStreamState, topic: str, code: str
) -> str:
    if topic == state.topics[0]:
        state.trade_error = code
        state.trade_generation = None
        return "trade"
    if topic == state.topics[1]:
        state.orderbook_error = code
        state.orderbook_generation = None
        return "orderbook"
    raise StreamError("ws_topic_unexpected")


def _mark_lane_baseline_error(
    state: ShadowStreamState, code: str, observed_at: datetime
) -> None:
    """Degrade only the symbol whose REST baseline failed."""

    captured_at = _utc(observed_at)
    state.baseline = {
        "captured_at": captured_at.isoformat(),
        "verified": False,
        "error_codes": [code],
        "price": None,
        "orderbook": None,
    }
    state.rest_resynced_at = captured_at
    state.trade_error = code
    state.orderbook_error = code
    state.trade_generation = None
    state.orderbook_generation = None


def _handle_frame(
    frame: object,
    *,
    state: ShadowStreamState,
    received_at: datetime,
    max_age_seconds: float,
    future_tolerance_seconds: float,
) -> str:
    if not isinstance(frame, Mapping):
        raise StreamError("ws_frame_malformed")
    frame_type = frame.get("type")
    if frame_type == "pong":
        return "pong"
    if frame_type == "subscriptions":
        raise StreamError("subscription_ack_unexpected")
    if frame_type == "error":
        error = frame.get("error")
        code = error.get("code") if isinstance(error, Mapping) else None
        if code == "server-shutdown":
            raise StreamError("ws_server_shutdown")
        if code == "rate-limit-exceeded":
            raise StreamError("ws_rate_limited")
        raise StreamError("ws_server_error")
    if frame_type != "message" or not isinstance(frame.get("data"), Mapping):
        raise StreamError("ws_frame_malformed")

    topic = frame.get("topic")
    data = frame["data"]
    received_utc = _utc(received_at)
    if topic == state.topics[0]:
        trade = _parse_trade(data, received_at=received_utc)
        broker_at = _mapping_datetime(trade, "broker_at")
        if broker_at is None:
            raise StreamError("trade_time_invalid")
        quality = _event_time_error(
            broker_at,
            received_utc,
            max_age_seconds=max_age_seconds,
            future_tolerance_seconds=future_tolerance_seconds,
            stale_code="trade_stale",
            future_code="trade_from_future",
        )
        previous_at = _mapping_datetime(state.trade, "broker_at")
        baseline_at = _nested_mapping_datetime(state.baseline, "price", "broker_at")
        if quality is None and baseline_at is not None and broker_at < baseline_at:
            quality = "trade_before_rest_baseline"
        if quality is None and previous_at is not None and broker_at < previous_at:
            quality = "trade_time_regressed"
        state.trade_error = quality
        if quality is None:
            state.trade = trade
            state.trade_generation = state.generation
        else:
            state.trade_generation = None
        state.healthy_once = state.healthy_once or bool(
            isinstance(state.baseline, Mapping)
            and state.baseline.get("verified") is True
            and state.trade_generation == state.generation
            and state.orderbook_generation == state.generation
        )
        if state.healthy_once:
            state.consecutive_failures = 0
        return "trade"
    if topic == state.topics[1]:
        try:
            book = _parse_orderbook(
                data,
                received_at=received_utc,
                timestamp_optional=True,
                source="websocket",
            )
        except StreamError as exc:
            if exc.code not in _ORDERBOOK_UNAVAILABLE_CODES:
                raise
            state.orderbook_error = exc.code
            state.orderbook_generation = None
            return "orderbook"
        broker_at = _mapping_datetime(book, "broker_at")
        quality = "orderbook_timestamp_unverified" if broker_at is None else None
        if broker_at is not None:
            quality = _event_time_error(
                broker_at,
                received_utc,
                max_age_seconds=max_age_seconds,
                future_tolerance_seconds=future_tolerance_seconds,
                stale_code="orderbook_stale",
                future_code="orderbook_from_future",
            )
            previous_at = _mapping_datetime(state.orderbook, "broker_at")
            baseline_at = _nested_mapping_datetime(
                state.baseline, "orderbook", "broker_at"
            )
            if quality is None and baseline_at is not None and broker_at < baseline_at:
                quality = "orderbook_before_rest_baseline"
            if quality is None and previous_at is not None and broker_at < previous_at:
                quality = "orderbook_time_regressed"
        state.orderbook_error = quality
        if quality is None:
            state.orderbook = book
            state.orderbook_generation = state.generation
        else:
            if state.orderbook is None:
                state.orderbook = book
            state.orderbook_generation = None
        state.healthy_once = state.healthy_once or bool(
            isinstance(state.baseline, Mapping)
            and state.baseline.get("verified") is True
            and state.trade_generation == state.generation
            and state.orderbook_generation == state.generation
        )
        if state.healthy_once:
            state.consecutive_failures = 0
        return "orderbook"
    raise StreamError("ws_topic_unexpected")


def _rest_resync(
    client: TossClient,
    symbol: str,
    *,
    now: Callable[[], datetime],
    max_age_seconds: float,
    future_tolerance_seconds: float,
) -> dict[str, Any]:
    try:
        prices = client.get_prices((symbol,))
        price_checked_at = _utc(now())
        if not isinstance(prices, (list, tuple)) or len(prices) != 1:
            raise StreamError("rest_price_malformed")
        price = prices[0]
        if not isinstance(price, Mapping) or price.get("symbol") != symbol:
            raise StreamError("rest_price_symbol_mismatch")
        if price.get("currency") != "USD":
            raise StreamError("rest_price_currency_mismatch")
        price_at = (
            None
            if price.get("timestamp") is None
            else _aware_datetime(price.get("timestamp"), "rest_price_time_invalid")
        )
        errors: list[str] = []
        if price_at is None:
            errors.append("rest_price_timestamp_unverified")
        else:
            price_error = _event_time_error(
                price_at,
                price_checked_at,
                max_age_seconds=max_age_seconds,
                future_tolerance_seconds=future_tolerance_seconds,
                stale_code="rest_price_stale",
                future_code="rest_price_from_future",
            )
            if price_error:
                errors.append(price_error)
        last_price = _positive_decimal(
            price.get("lastPrice"), "rest_price_malformed", allow_decimal=True
        )

        orderbook = client.get_orderbook(symbol)
        book_checked_at = _utc(now())
        try:
            book = _parse_orderbook(
                orderbook,
                received_at=book_checked_at,
                timestamp_optional=True,
                source="rest",
            )
        except StreamError as exc:
            if exc.code not in _ORDERBOOK_UNAVAILABLE_CODES:
                raise
            book = None
            errors.append(exc.code)
        if book is not None:
            book_at = _mapping_datetime(book, "broker_at")
            if book_at is None:
                errors.append("rest_orderbook_timestamp_unverified")
            else:
                book_error = _event_time_error(
                    book_at,
                    book_checked_at,
                    max_age_seconds=max_age_seconds,
                    future_tolerance_seconds=future_tolerance_seconds,
                    stale_code="rest_orderbook_stale",
                    future_code="rest_orderbook_from_future",
                )
                if book_error:
                    errors.append(book_error)
        completed_at = _utc(now())
        return {
            "captured_at": completed_at.isoformat(),
            "verified": not errors,
            "error_codes": sorted(errors),
            "price": {
                "value": str(last_price),
                "currency": "USD",
                "broker_at": price_at.isoformat() if price_at is not None else None,
                "checked_at": price_checked_at.isoformat(),
            },
            "orderbook": book,
        }
    except StreamError:
        raise
    except Exception as exc:
        raise StreamError("rest_resync_failed") from exc


def _parse_trade(data: Mapping[str, Any], *, received_at: datetime) -> dict[str, Any]:
    if data.get("currency") != "USD":
        raise StreamError("trade_currency_mismatch")
    price = _positive_decimal(data.get("price"), "trade_price_invalid")
    volume = _positive_decimal(data.get("volume"), "trade_volume_invalid")
    broker_at = _aware_datetime(data.get("timestamp"), "trade_time_invalid")
    return {
        "price": str(price),
        "volume": str(volume),
        "currency": "USD",
        "broker_at": broker_at.isoformat(),
        "received_at": received_at.isoformat(),
        "source": "websocket",
    }


def _parse_orderbook(
    data: Mapping[str, Any],
    *,
    received_at: datetime,
    timestamp_optional: bool,
    source: str,
) -> dict[str, Any]:
    if data.get("currency") != "USD":
        raise StreamError("orderbook_currency_mismatch")
    raw_timestamp = data.get("timestamp")
    if raw_timestamp is None and timestamp_optional:
        broker_at = None
    else:
        broker_at = _aware_datetime(raw_timestamp, "orderbook_time_invalid")
    bids = _levels(data.get("bids"), descending=True)
    asks = _levels(data.get("asks"), descending=False)
    best_bid, bid_volume = bids[0]
    best_ask, ask_volume = asks[0]
    if best_bid >= best_ask:
        raise StreamError("orderbook_crossed")
    return {
        "best_bid": str(best_bid),
        "best_bid_volume": str(bid_volume),
        "best_ask": str(best_ask),
        "best_ask_volume": str(ask_volume),
        "currency": "USD",
        "broker_at": broker_at.isoformat() if broker_at is not None else None,
        "received_at": received_at.isoformat(),
        "timestamp_source": "broker" if broker_at is not None else "local_receive",
        "source": source,
    }


def _levels(value: object, *, descending: bool) -> list[tuple[Decimal, Decimal]]:
    if not isinstance(value, list) or len(value) > 100:
        raise StreamError("orderbook_levels_invalid")
    if not value:
        raise StreamError("orderbook_empty")
    levels: list[tuple[Decimal, Decimal]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise StreamError("orderbook_levels_invalid")
        price = _orderbook_decimal(item.get("price"))
        volume = _orderbook_decimal(item.get("volume"))
        levels.append((price, volume))
    prices = [item[0] for item in levels]
    if prices != sorted(prices, reverse=descending):
        raise StreamError("orderbook_sort_unverified")
    return levels


def _orderbook_decimal(value: object) -> Decimal:
    if isinstance(value, bool):
        raise StreamError("orderbook_levels_invalid")
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, str) and len(value) <= 30 and _DECIMAL_RE.fullmatch(value):
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise StreamError("orderbook_levels_invalid") from exc
    else:
        raise StreamError("orderbook_levels_invalid")
    if not parsed.is_finite():
        raise StreamError("orderbook_levels_invalid")
    if parsed <= 0:
        raise StreamError("orderbook_level_unavailable")
    return parsed


def _recv_json(connection: WebSocketConnection, timeout: float) -> object:
    try:
        raw = connection.recv(timeout=timeout)
    except TimeoutError as exc:
        raise StreamError("subscription_ack_timeout") from exc
    except Exception as exc:
        raise StreamError("ws_connection_lost") from exc
    return _decode_frame(raw)


def _decode_frame(raw: object) -> object:
    if not isinstance(raw, str):
        raise StreamError("ws_binary_frame_rejected")
    if len(raw.encode("utf-8")) > MAX_FRAME_BYTES:
        raise StreamError("ws_frame_too_large")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise StreamError("ws_json_duplicate_key")
            result[key] = value
        return result

    try:
        return json.loads(raw, object_pairs_hook=reject_duplicates)
    except StreamError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise StreamError("ws_json_invalid") from exc


def _load_context(config: StreamConfig, at: datetime) -> SelectedContext:
    contexts = _load_contexts(config, at)
    if len(contexts) != 1:
        raise StreamError("context_requires_multi_symbol_consumer")
    return contexts[0]


def _load_contexts(config: StreamConfig, at: datetime) -> tuple[SelectedContext, ...]:
    _require_private_regular_file(config.context_path, "context_file_invalid")
    try:
        contexts = load_contexts(
            config.context_path,
            now=_utc(at),
            max_age_seconds=config.context_max_age_seconds,
        )
    except NewsDigestError as exc:
        raise StreamError(exc.code) from exc
    symbols = tuple(context.symbol for context in contexts)
    if config.expected_symbols is not None and symbols != config.expected_symbols:
        raise StreamError("context_symbols_mismatch")
    return contexts


def _refresh_context(
    config: StreamConfig,
    previous: SelectedContext,
    at: datetime,
) -> SelectedContext:
    current = _load_context(config, at)
    if current.symbol != previous.symbol or current.session_date != previous.session_date:
        raise StreamError("context_changed")
    if current.generated_at < previous.generated_at:
        raise StreamError("context_reverted")
    if current.active_until > previous.active_until:
        raise StreamError("context_extended")
    return current


def _refresh_contexts(
    config: StreamConfig,
    previous: tuple[SelectedContext, ...],
    at: datetime,
) -> tuple[SelectedContext, ...]:
    current = _load_contexts(config, at)
    previous_identity = tuple((item.symbol, item.session_date) for item in previous)
    current_identity = tuple((item.symbol, item.session_date) for item in current)
    if current_identity != previous_identity:
        raise StreamError("context_changed")
    for old, new in zip(previous, current, strict=True):
        if new.generated_at < old.generated_at:
            raise StreamError("context_reverted")
        if new.active_until > old.active_until:
            raise StreamError("context_extended")
    return current


def _default_connector(
    config: StreamConfig,
) -> Callable[[TossToken], WebSocketConnection]:
    try:
        from websockets.sync.client import connect
    except ImportError as exc:
        raise StreamError("websocket_dependency_missing") from exc

    def connect_with_token(token: TossToken) -> WebSocketConnection:
        return connect(
            WS_URL,
            additional_headers={
                "Authorization": f"{token.token_type} {token.access_token}"
            },
            open_timeout=config.ack_timeout_seconds,
            close_timeout=5,
            max_size=MAX_FRAME_BYTES,
            max_queue=16,
            compression=None,
            ping_interval=None,
            proxy=None,
        )

    return connect_with_token


def _credentials_from_environment() -> TossCredentials:
    client_id = os.environ.pop("TOSS_CLIENT_ID", "")
    client_secret = os.environ.pop("TOSS_CLIENT_SECRET", "")
    if (
        not client_id
        or not client_secret
        or client_id.strip() != client_id
        or client_secret.strip() != client_secret
        or any(ch in client_id + client_secret for ch in "\r\n\0")
    ):
        raise StreamError("toss_credentials_missing_or_invalid")
    return TossCredentials(client_id=client_id, client_secret=client_secret)


def _positive_decimal(value: object, code: str, *, allow_decimal: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise StreamError(code)
    if isinstance(value, Decimal) and allow_decimal:
        parsed = value
    elif isinstance(value, str) and len(value) <= 30 and _DECIMAL_RE.fullmatch(value):
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise StreamError(code) from exc
    else:
        raise StreamError(code)
    if not parsed.is_finite() or parsed <= 0:
        raise StreamError(code)
    return parsed


def _aware_datetime(value: object, code: str) -> datetime:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise StreamError(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StreamError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StreamError(code)
    return parsed.astimezone(timezone.utc)


def _event_time_error(
    event_at: datetime,
    received_at: datetime,
    *,
    max_age_seconds: float,
    future_tolerance_seconds: float,
    stale_code: str,
    future_code: str,
) -> str | None:
    age = (_utc(received_at) - _utc(event_at)).total_seconds()
    if age < -future_tolerance_seconds:
        return future_code
    if age > max_age_seconds:
        return stale_code
    return None


def _stream_value_fresh(
    value: Mapping[str, Any] | None,
    now: datetime,
    maximum: float,
) -> bool:
    received_at = _mapping_datetime(value, "received_at")
    broker_at = _mapping_datetime(value, "broker_at")
    if received_at is None or broker_at is None:
        return False
    received_age = (_utc(now) - received_at).total_seconds()
    broker_age = (_utc(now) - broker_at).total_seconds()
    return 0 <= received_age <= maximum and 0 <= broker_age <= maximum


def _silent_topic_error(
    state: ShadowStreamState,
    *,
    now: datetime,
    max_age_seconds: float,
) -> str | None:
    """Return a fail-closed code when one acknowledged topic stops arriving."""

    if (
        not state.connected
        or not state.acknowledged
        or state.rest_resynced_at is None
        or not isinstance(state.baseline, Mapping)
        or state.baseline.get("verified") is not True
    ):
        return None
    observed_at = _utc(now)
    for name, value, generation, frame_error in (
        ("trade", state.trade, state.trade_generation, state.trade_error),
        (
            "orderbook",
            state.orderbook,
            state.orderbook_generation,
            state.orderbook_error,
        ),
    ):
        if frame_error is not None:
            continue
        if generation == state.generation and _stream_value_fresh(
            value, observed_at, max_age_seconds
        ):
            continue
        received_at = _mapping_datetime(value, "received_at")
        freshness_started = max(
            _utc(state.rest_resynced_at),
            received_at or _utc(state.rest_resynced_at),
        )
        if (observed_at - freshness_started).total_seconds() >= max_age_seconds:
            return f"{name}_topic_silent"
    return None


def _mapping_datetime(value: Mapping[str, Any] | None, key: str) -> datetime | None:
    if not isinstance(value, Mapping) or value.get(key) is None:
        return None
    try:
        return _aware_datetime(value[key], "snapshot_time_invalid")
    except StreamError:
        return None


def _nested_mapping_datetime(
    value: Mapping[str, Any] | None,
    nested_key: str,
    time_key: str,
) -> datetime | None:
    nested = value.get(nested_key) if isinstance(value, Mapping) else None
    return _mapping_datetime(nested if isinstance(nested, Mapping) else None, time_key)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise StreamError("clock_invalid")
    return value.astimezone(timezone.utc)


def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.fspath(left)) == os.path.normcase(os.fspath(right))


def _require_private_directory(path: Path, code: str) -> None:
    if not path.is_absolute():
        raise StreamError(code)
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise StreamError(code) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or not _same_path(resolved, path):
        raise StreamError(code)
    if os.name != "nt":
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise StreamError(code)
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise StreamError(code)


def _require_private_regular_file(path: Path, code: str) -> None:
    if not path.is_absolute():
        raise StreamError(code)
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise StreamError(code) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or not _same_path(resolved, path):
        raise StreamError(code)
    if os.name != "nt":
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise StreamError(code)
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise StreamError(code)


def _startup_snapshot(at: datetime, code: str) -> dict[str, Any]:
    timestamp = _utc(at).isoformat()
    return {
        "schema_version": 1,
        "mode": "shadow",
        "live_order_submission": False,
        "ready_for_live_entry": False,
        "symbol": None,
        "session_date": None,
        "active_until": None,
        "asyncapi_version": ASYNCAPI_VERSION,
        "asyncapi_sha256": ASYNCAPI_SHA256,
        "generation": 0,
        "connected": False,
        "subscription_acknowledged": False,
        "subscribed_topics": [],
        "rest_resynced_at": None,
        "updated_at": timestamp,
        "valid_until": timestamp,
        "shadow_usable": False,
        "reconnect_count": 0,
        "consecutive_failures": 0,
        "error_codes": [code],
        "last_disconnect_error": None,
        "baseline": None,
        "trade": None,
        "orderbook": None,
    }


def _prepare_snapshot_directory(target: Path) -> None:
    parent = target.parent
    if not parent.exists():
        parent.mkdir(mode=0o700, parents=True)
    _require_private_directory(parent, "snapshot_directory_invalid")
    try:
        info = target.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise StreamError("snapshot_file_invalid") from exc
    try:
        resolved = target.resolve(strict=True)
    except OSError as exc:
        raise StreamError("snapshot_file_invalid") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or not _same_path(resolved, target)
    ):
        raise StreamError("snapshot_file_invalid")
    if os.name != "nt":
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise StreamError("snapshot_file_invalid")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise StreamError("snapshot_file_invalid")


def _write_private_json_atomic(target: Path, payload: Mapping[str, Any]) -> None:
    _prepare_snapshot_directory(target)
    temporary_path: Path | None = None
    try:
        serialized = json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(serialized)
            temporary.flush()
            os.chmod(temporary_path, 0o600)
            os.fsync(temporary.fileno())
        os.replace(temporary_path, target)
        temporary_path = None
        os.chmod(target, 0o600)
        if os.name != "nt":
            descriptor = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                try:
                    os.fsync(descriptor)
                except OSError:
                    pass
            finally:
                os.close(descriptor)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise StreamError("stream_lock_invalid") from exc
    else:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise StreamError("stream_lock_invalid")
    handle = path.open("a+b")
    os.chmod(path, 0o600)
    try:
        try:
            if os.name == "nt":
                import msvcrt

                if path.stat().st_size == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (ImportError, OSError) as exc:
            raise StreamError("stream_worker_already_running") from exc
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        handle.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Toss one-symbol shadow market stream")
    parser.add_argument("--context", required=True, type=Path)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--simulation-config", type=Path)
    parser.add_argument("--plan-db", type=Path)
    parser.add_argument("--expected-simulation-id")
    parser.add_argument("--expected-simulation-start-date")
    parser.add_argument("--expected-simulation-end-date")
    parser.add_argument("--expected-simulation-db", type=Path)
    parser.add_argument("--expected-experiment-hash")
    parser.add_argument("--heartbeat", type=Path)
    parser.add_argument("--release-sha")
    return parser


def _stream_heartbeat_state(
    state: ShadowStreamState | CohortStreamState,
    *,
    at: datetime,
    rest_resync_seconds: float,
    future_tolerance_seconds: float,
) -> tuple[str, bool, bool]:
    if isinstance(state, CohortStreamState):
        lane_values = tuple(
            _stream_heartbeat_state(
                lane,
                at=at,
                rest_resync_seconds=rest_resync_seconds,
                future_tolerance_seconds=future_tolerance_seconds,
            )
            for lane in state.states.values()
        )
        acknowledged = bool(state.connected and state.acknowledged) and all(
            value[1] for value in lane_values
        )
        baseline_fresh = acknowledged and all(value[2] for value in lane_values)
        if acknowledged and baseline_fresh and all(
            value[0] == "OK" for value in lane_values
        ):
            return "OK", True, True
        if any(value[0] == "DEGRADED" for value in lane_values):
            return "DEGRADED", acknowledged, baseline_fresh
        if state.connection_error is None and (state.connected or state.generation == 0):
            return "STARTING", acknowledged, baseline_fresh
        return "DEGRADED", acknowledged, baseline_fresh
    current = _utc(at)
    acknowledged = bool(state.connected and state.acknowledged)
    baseline_age: float | None = None
    if state.rest_resynced_at is not None:
        baseline_age = (current - _utc(state.rest_resynced_at)).total_seconds()
    baseline_fresh = bool(
        acknowledged
        and isinstance(state.baseline, Mapping)
        and state.baseline.get("verified") is True
        and baseline_age is not None
        and -future_tolerance_seconds <= baseline_age
        <= rest_resync_seconds + future_tolerance_seconds
    )
    topic_failed = state.trade_error is not None or state.orderbook_error is not None
    if acknowledged and baseline_fresh and not topic_failed:
        status = "OK"
    elif topic_failed:
        status = "DEGRADED"
    elif state.connection_error is None and (state.connected or state.generation == 0):
        status = "STARTING"
    else:
        status = "DEGRADED"
    return status, acknowledged, baseline_fresh


class _PaperStreamSink:
    """Batch selected-symbol frames into the isolated virtual USD ledger."""

    def __init__(
        self,
        *,
        simulation_config: Path,
        plan_db: Path,
        event_max_age_seconds: float,
        context_path: Path | None = None,
        lane: str | None = None,
        locked_config: TradingConfig | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not simulation_config.is_absolute() or not plan_db.is_absolute():
            raise StreamError("paper_simulation_path_invalid")
        _require_private_regular_file(
            simulation_config, "paper_simulation_config_invalid"
        )
        _require_private_regular_file(plan_db, "paper_plan_db_invalid")
        config = locked_config or load_config(simulation_config)
        intraday = config.intraday
        if (
            config.strategy_kind != "intraday"
            or str(config.runtime.mode).strip().lower() != "shadow"
            or config.toss.live_enabled
            or config.toss.base_url != TOSS_BASE_URL
            or config.toss.account_seq is not None
            or intraday.live_execution_enabled
            or not intraday.simulation_enabled
            or not config.live.emergency_stop
            or config.live.allowed_symbols
            or str(config.runtime.market).strip().upper() != "US"
            or str(config.runtime.timezone_name).strip() != "America/New_York"
            or not config.runtime.use_market_calendar
        ):
            raise StreamError("paper_simulation_config_unsafe")
        configured_state_db = Path(str(config.runtime.state_db or "")).expanduser()
        configured_context = Path(
            str(intraday.news_context_path or "")
        ).expanduser()
        if (
            not configured_state_db.is_absolute()
            or not _same_path(configured_state_db, plan_db)
            or context_path is not None
            and (
                not configured_context.is_absolute()
                or not _same_path(configured_context, context_path)
            )
        ):
            raise StreamError("paper_simulation_runtime_path_mismatch")
        try:
            if intraday.simulation_lanes == 1 and lane is None:
                run_id = intraday.simulation_id
                initial_cash = intraday.simulation_initial_cash
            elif intraday.simulation_lanes == 2 and lane in {"A", "B"}:
                run_id = f"{intraday.simulation_id}-{lane.lower()}"
                initial_cash = intraday.simulation_initial_cash / Decimal("2")
            else:
                raise ValueError("paper simulation lane configuration is invalid")
            paper_path = Path(str(intraday.simulation_db_path or ""))
            if not paper_path.is_absolute() or paper_path.name != "intraday-paper.sqlite3":
                raise ValueError("paper database path is invalid")
            assert_simulation_topology(
                paper_path,
                simulation_id=intraday.simulation_id,
                lanes=intraday.simulation_lanes,
            )
            paper_config = IntradayPaperConfig(
                run_id=run_id,
                start_date=intraday.simulation_start_date,
                end_date=intraday.simulation_end_date,
                initial_cash_usd=initial_cash,
                slippage_fraction=intraday.simulation_slippage_fraction,
                quote_max_age_seconds=min(
                    intraday.quote_max_age_seconds,
                    intraday.orderbook_max_age_seconds,
                ),
                future_tolerance_seconds=min(5, intraday.max_quote_skew_seconds),
                experiment_hash=intraday_simulation_experiment_hash(config),
            )
            self.plan_store = SQLiteStateStore(plan_db)
            self.paper_store = IntradayPaperStore(paper_path, paper_config)
        except (TypeError, ValueError, OSError, PaperSimulationError) as exc:
            raise StreamError("paper_simulation_setup_failed") from exc
        self.account_key = simulation_account_key(paper_config)
        self.event_max_age_seconds = min(
            event_max_age_seconds, paper_config.quote_max_age_seconds
        )
        self.event_future_tolerance_seconds = paper_config.future_tolerance_seconds
        self.paper_quote_max_age_seconds = paper_config.quote_max_age_seconds
        self.monotonic = monotonic
        self.last_flush = monotonic()
        self.last_liveness_touch = float("-inf")
        self.active_plan_id: str | None = None
        lane_prefix = f"{lane.lower()}-" if lane is not None else ""
        self.stream_instance_id = f"stream-{lane_prefix}{uuid4().hex}"
        self.active_instance_started = False
        self.closed = False

    def locked_symbols(self, session_date: str) -> tuple[str, ...]:
        try:
            record = self.plan_store.load_intraday_plan(
                account_key=self.account_key,
                session_date=session_date,
            )
            if record is None:
                raise StreamError("paper_plan_missing_for_locked_context")
            symbols = (str(record.get("symbol") or "").strip().upper(),)
            _validate_symbols(symbols)
            return symbols
        except StreamError:
            raise
        except Exception as exc:
            raise StreamError("paper_plan_integrity_failed") from exc

    def __call__(
        self,
        state: ShadowStreamState,
        kind: str,
        at: datetime,
    ) -> None:
        if self.closed:
            return
        try:
            record = self.plan_store.load_intraday_plan(
                account_key=self.account_key,
                session_date=state.context.session_date,
            )
            if record is None:
                raise StreamError("paper_plan_missing_for_locked_context")
            plan_id = str(record["plan_id"])
            if self.active_plan_id != plan_id:
                self.paper_store.ensure_plan(
                    record,
                    registered_at=record.get("created_at"),
                )
                current = self.paper_store.load_plan(plan_id)
                instance = self.paper_store.begin_stream_instance(
                    plan_id,
                    self.stream_instance_id,
                    started_at=at,
                )
                self.active_instance_started = True
                self.last_liveness_touch = self.monotonic()
                entry_start = datetime.fromisoformat(
                    str(record["payload"]["entry_start"])
                )
                regular_close = datetime.fromisoformat(
                    str(record["payload"]["regular_close"])
                )
                status = str(current.get("status") or "")
                journaled = int(current.get("journaled_frame_count") or 0)
                in_sensitive_window = status == "OPEN" or (
                    status == "WAITING_ENTRY"
                    and entry_start <= at < regular_close
                )
                first_frame_late = bool(
                    journaled == 0
                    and status == "WAITING_ENTRY"
                    and at
                    > entry_start
                    + timedelta(seconds=self.paper_quote_max_age_seconds)
                )
                interrupted = bool(instance.get("previous_unclosed"))
                if in_sensitive_window and (
                    interrupted or journaled > 0 or first_frame_late
                ):
                    self.paper_store.record_data_gap(
                        plan_id,
                        (
                            "stream_process_interrupted"
                            if interrupted
                            else "stream_process_restarted"
                            if journaled > 0
                            else "stream_started_late"
                        ),
                        at=at,
                        stream_instance_id=self.stream_instance_id,
                    )
                self.active_plan_id = plan_id
            tick = self.monotonic()
            if tick - self.last_liveness_touch >= 1.0:
                if not self.paper_store.touch_stream_instance(
                    self.stream_instance_id,
                    observed_at=at,
                ):
                    raise StreamError("paper_stream_instance_missing")
                self.last_liveness_touch = tick
            if kind in {"start", "baseline"}:
                return
            if kind == "tick":
                if tick - self.last_flush >= 0.25:
                    self.paper_store.flush_pending(
                        stream_instance_id=self.stream_instance_id
                    )
                    self.last_flush = tick
                return
            if kind in {"trade", "orderbook"}:
                frame_error = (
                    state.trade_error if kind == "trade" else state.orderbook_error
                )
                if frame_error:
                    self.paper_store.flush_pending(
                        stream_instance_id=self.stream_instance_id
                    )
                    current = self.paper_store.load_plan(plan_id)
                    entry_start = datetime.fromisoformat(
                        str(record["payload"]["entry_start"])
                    )
                    regular_close = datetime.fromisoformat(
                        str(record["payload"]["regular_close"])
                    )
                    if current.get("status") == "OPEN" or (
                        current.get("status") == "WAITING_ENTRY"
                        and entry_start <= at < regular_close
                    ):
                        self.paper_store.record_data_gap(
                            plan_id,
                            str(frame_error),
                            at=at,
                            stream_instance_id=self.stream_instance_id,
                        )
                    return
                payload = state.as_payload(
                    now=at,
                    event_max_age_seconds=self.event_max_age_seconds,
                )
                self.paper_store.queue_payload(
                    plan_id,
                    payload,
                    event_kind=kind,
                    now=at,
                    stream_instance_id=self.stream_instance_id,
                )
                tick = self.monotonic()
                if tick - self.last_flush >= 0.25:
                    self.paper_store.flush_pending(
                        stream_instance_id=self.stream_instance_id
                    )
                    self.last_flush = tick
                return
            if kind == "disconnect":
                self.paper_store.flush_pending(
                    stream_instance_id=self.stream_instance_id
                )
                current = self.paper_store.load_plan(plan_id)
                entry_start = datetime.fromisoformat(str(record["payload"]["entry_start"]))
                regular_close = datetime.fromisoformat(
                    str(record["payload"]["regular_close"])
                )
                if current.get("status") == "OPEN" or (
                    current.get("status") == "WAITING_ENTRY"
                    and entry_start <= at < regular_close
                ):
                    self.paper_store.record_data_gap(
                        plan_id,
                        state.last_disconnect_error or "stream_disconnected",
                        at=at,
                        stream_instance_id=self.stream_instance_id,
                    )
        except (KeyError, TypeError, ValueError, PaperSimulationError) as exc:
            raise StreamError("paper_simulation_persistence_failed") from exc

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if self.active_instance_started:
                ended_at = datetime.now(timezone.utc)
                if self.paper_store.touch_stream_instance(
                    self.stream_instance_id,
                    observed_at=ended_at,
                ):
                    try:
                        self.paper_store.flush_pending(
                            stream_instance_id=self.stream_instance_id
                        )
                    except PaperStreamInstanceInactive:
                        self.paper_store.discard_pending()
                    self.paper_store.end_stream_instance(
                        self.stream_instance_id,
                        ended_at=ended_at,
                        reason="stream_process_closed",
                    )
                else:
                    self.paper_store.discard_pending()
            else:
                self.paper_store.discard_pending()
        finally:
            self.paper_store.discard_pending()
            try:
                self.paper_store.close()
            finally:
                self.plan_store.close()


class _PaperCohortStreamSink:
    """Route each selected symbol to exactly one isolated lane ledger."""

    def __init__(
        self,
        *,
        simulation_config: Path,
        plan_db: Path,
        event_max_age_seconds: float,
        context_path: Path | None = None,
        locked_config: TradingConfig | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        opened: list[_PaperStreamSink] = []
        try:
            for lane in ("A", "B"):
                opened.append(
                    _PaperStreamSink(
                        simulation_config=simulation_config,
                        plan_db=plan_db,
                        event_max_age_seconds=event_max_age_seconds,
                        context_path=context_path,
                        lane=lane,
                        locked_config=locked_config,
                        monotonic=monotonic,
                    )
                )
        except Exception:
            for sink in opened:
                sink.close()
            raise
        self.sinks = tuple(opened)
        self.routes: dict[tuple[str, str], _PaperStreamSink] = {}
        self.event_max_age_seconds = min(
            sink.event_max_age_seconds for sink in self.sinks
        )
        self.event_future_tolerance_seconds = min(
            sink.event_future_tolerance_seconds for sink in self.sinks
        )

    def locked_symbols(
        self, *, cohort_id: str, session_date: str
    ) -> tuple[str, ...]:
        try:
            cohort = self.sinks[0].plan_store.load_intraday_cohort(
                cohort_id=cohort_id,
                session_date=session_date,
            )
            if cohort is None:
                raise StreamError("paper_cohort_missing_for_locked_context")
            symbols = tuple(
                str(lane["plan"]["symbol"]).strip().upper()
                for label in ("A", "B")
                for lane in (cohort["lanes"][label],)
                if lane["status"] == "PLAN"
            )
            _validate_symbols(symbols)
            return symbols
        except StreamError:
            raise
        except Exception as exc:
            raise StreamError("paper_cohort_integrity_failed") from exc

    def __call__(self, state: ShadowStreamState, kind: str, at: datetime) -> None:
        key = (state.context.session_date, state.context.symbol)
        sink = self.routes.get(key)
        if sink is None:
            matches = []
            for candidate in self.sinks:
                record = candidate.plan_store.load_intraday_plan(
                    account_key=candidate.account_key,
                    session_date=state.context.session_date,
                )
                if (
                    record is not None
                    and str(record.get("symbol") or "").strip().upper()
                    == state.context.symbol
                ):
                    matches.append(candidate)
            if len(matches) != 1:
                raise StreamError("paper_cohort_route_invalid")
            sink = matches[0]
            self.routes[key] = sink
        sink(state, kind, at)

    def close(self) -> None:
        first_error: Exception | None = None
        for sink in self.sinks:
            try:
                sink.close()
            except Exception as exc:  # close every ledger before surfacing failure
                first_error = first_error or exc
        if first_error is not None:
            raise first_error


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if (args.simulation_config is None) != (args.plan_db is None):
        print(
            json.dumps({"status": "blocked", "code": "paper_simulation_args_incomplete"}),
            file=sys.stderr,
        )
        return 2
    expected_values = (
        args.expected_simulation_id,
        args.expected_simulation_start_date,
        args.expected_simulation_end_date,
        args.expected_simulation_db,
        args.expected_experiment_hash,
    )
    if (
        args.simulation_config is not None
        and any(value is None for value in expected_values)
        or args.simulation_config is None
        and any(value is not None for value in expected_values)
    ):
        print(
            json.dumps(
                {"status": "blocked", "code": "paper_simulation_lock_args_incomplete"}
            ),
            file=sys.stderr,
        )
        return 2
    if (args.heartbeat is None) != (args.release_sha is None):
        print(
            json.dumps({"status": "blocked", "code": "stream_heartbeat_args_incomplete"}),
            file=sys.stderr,
        )
        return 2
    config = StreamConfig(
        context_path=args.context.expanduser(),
        snapshot_path=args.snapshot.expanduser(),
        once=args.once,
    )
    paper_sink: _PaperStreamSink | _PaperCohortStreamSink | None = None
    heartbeat_writer = None
    try:
        if args.heartbeat is not None:
            from turtle_runtime.heartbeat import HeartbeatError, RedactedHeartbeatWriter

            try:
                heartbeat_writer = RedactedHeartbeatWriter(
                    args.heartbeat.expanduser(),
                    release_sha=args.release_sha,
                    component="stream",
                )
                heartbeat_writer.write("STARTING")
            except HeartbeatError as exc:
                raise StreamError("stream_heartbeat_setup_failed") from exc

        def publish_heartbeat(
            state: ShadowStreamState | CohortStreamState,
            observed_at: datetime,
        ) -> None:
            if heartbeat_writer is None:
                return
            status, stream_ack_ok, baseline_fresh = _stream_heartbeat_state(
                state,
                at=observed_at,
                rest_resync_seconds=config.rest_resync_seconds,
                future_tolerance_seconds=config.event_future_tolerance_seconds,
            )
            try:
                heartbeat_writer.write(
                    status,
                    stream_ack_ok=stream_ack_ok,
                    baseline_fresh=baseline_fresh,
                )
            except HeartbeatError as exc:
                raise StreamError("stream_heartbeat_write_failed") from exc

        if args.simulation_config is not None:
            try:
                from .operations import (
                    _normalize_expected_simulation,
                    _require_locked_simulation_config,
                    _require_shadow_service_config,
                )

                locked_config = load_config(args.simulation_config.expanduser())
                expected = _normalize_expected_simulation(
                    {
                        "run_id": args.expected_simulation_id,
                        "start_date": args.expected_simulation_start_date,
                        "end_date": args.expected_simulation_end_date,
                        "paper_db": str(args.expected_simulation_db.expanduser()),
                        "experiment_hash": args.expected_experiment_hash,
                    }
                )
                _require_shadow_service_config(locked_config)
                _require_locked_simulation_config(
                    locked_config,
                    expected=expected,
                    state_db=args.plan_db.expanduser(),
                )
                configured_context = Path(
                    str(locked_config.intraday.news_context_path or "")
                ).expanduser()
                if (
                    locked_config.toss.account_seq is not None
                    or not configured_context.is_absolute()
                    or not _same_path(configured_context, args.context.expanduser())
                ):
                    raise ValueError("stream manifest contains planner authority")
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                raise StreamError("paper_simulation_lock_mismatch") from exc
            sink_type = (
                _PaperCohortStreamSink
                if locked_config.intraday.simulation_lanes == 2
                else _PaperStreamSink
            )
            paper_sink = sink_type(
                simulation_config=args.simulation_config.expanduser(),
                plan_db=args.plan_db.expanduser(),
                event_max_age_seconds=config.event_max_age_seconds,
                context_path=args.context.expanduser(),
                locked_config=locked_config,
            )
            locked_contexts = _load_contexts(config, datetime.now(timezone.utc))
            session_date = locked_contexts[0].session_date
            expected_symbols = (
                paper_sink.locked_symbols(
                    cohort_id=str(locked_config.intraday.simulation_id),
                    session_date=session_date,
                )
                if isinstance(paper_sink, _PaperCohortStreamSink)
                else paper_sink.locked_symbols(session_date)
            )
            config = replace(
                config,
                expected_symbols=expected_symbols,
                event_max_age_seconds=paper_sink.event_max_age_seconds,
                event_future_tolerance_seconds=(
                    paper_sink.event_future_tolerance_seconds
                ),
            )
        credentials = _credentials_from_environment()
        client = TossClient(
            credentials=credentials,
            account_seq=None,
            transport=SimulationReadOnlyTossTransport(),
        )
        return run_shadow_stream(
            config,
            client=client,
            event_sink=paper_sink,
            heartbeat_sink=publish_heartbeat if heartbeat_writer is not None else None,
        )
    except StreamError as exc:
        if heartbeat_writer is not None:
            try:
                heartbeat_writer.write("ERROR")
            except Exception:
                pass
        print(json.dumps({"status": "blocked", "code": exc.code}), file=sys.stderr)
        return 2
    finally:
        if paper_sink is not None:
            paper_sink.close()


if __name__ == "__main__":
    raise SystemExit(main())
