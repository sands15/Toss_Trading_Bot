from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from typing import Any, Iterator, Mapping


__all__ = [
    "IntradayPaperConfig",
    "IntradayPaperStore",
    "PaperSimulationBlocked",
    "PaperSimulationError",
    "simulation_account_key",
]


_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,80}")
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_SYMBOL_RE = re.compile(r"(?=.{1,16}\Z)[A-Z][A-Z0-9]*(?:[.-][A-Z0-9]+)?\Z")
_DECIMAL_RE = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?")
_ZERO = Decimal("0")
_ONE = Decimal("1")


class PaperSimulationError(RuntimeError):
    """Raised when paper-simulation input or persisted state is invalid."""


class PaperSimulationBlocked(PaperSimulationError):
    """Raised when unresolved state makes another simulated plan unsafe."""


@dataclass(frozen=True, slots=True)
class IntradayPaperConfig:
    """Immutable settings for one inclusive, forward-only simulation run."""

    run_id: str
    start_date: date
    end_date: date
    initial_cash_usd: Decimal
    slippage_fraction: Decimal = Decimal("0.0005")
    quote_max_age_seconds: int = 5
    future_tolerance_seconds: int = 1
    experiment_hash: str = "0" * 64

    def __post_init__(self) -> None:
        run_id = _identifier(self.run_id, "run_id")
        start = _date(self.start_date, "start_date")
        end = _date(self.end_date, "end_date")
        cash = _decimal(self.initial_cash_usd, "initial_cash_usd", positive=True)
        slippage = _decimal(
            self.slippage_fraction,
            "slippage_fraction",
            nonnegative=True,
        )
        if slippage >= Decimal("0.05"):
            raise ValueError("slippage_fraction must be less than 0.05")
        experiment_hash = str(self.experiment_hash or "").lower()
        if not _HASH_RE.fullmatch(experiment_hash):
            raise ValueError("experiment_hash must be a lowercase SHA-256 hash")
        if start > end:
            raise ValueError("start_date must not be after end_date")
        if (
            isinstance(self.quote_max_age_seconds, bool)
            or not isinstance(self.quote_max_age_seconds, int)
            or not 1 <= self.quote_max_age_seconds <= 300
        ):
            raise ValueError("quote_max_age_seconds must be an integer from 1 through 300")
        if (
            isinstance(self.future_tolerance_seconds, bool)
            or not isinstance(self.future_tolerance_seconds, int)
            or not 0 <= self.future_tolerance_seconds <= 30
        ):
            raise ValueError("future_tolerance_seconds must be an integer from 0 through 30")
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "start_date", start)
        object.__setattr__(self, "end_date", end)
        object.__setattr__(self, "initial_cash_usd", cash)
        object.__setattr__(self, "slippage_fraction", slippage)
        object.__setattr__(self, "experiment_hash", experiment_hash)


def simulation_account_key(config: IntradayPaperConfig) -> str:
    """Return the non-broker account key a planner must put in paper plans."""

    return f"simulation-{_config_hash(config)[:24]}"


class IntradayPaperStore:
    """SQLite-backed virtual USD account driven only by normalized market snapshots.

    This module deliberately has no broker, HTTP, websocket, or live-order imports.
    Callers provide immutable plans and ``ShadowStreamState.as_payload`` mappings.
    """

    MAX_PENDING_EVENTS = 128

    def __init__(self, path: str | Path, config: IntradayPaperConfig) -> None:
        self.path = Path(path).expanduser()
        if str(path) == ":memory:":
            raise ValueError("paper simulation requires a durable SQLite file")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.account_key = simulation_account_key(config)
        self._write_depth = 0
        self._pending_events: list[dict[str, Any]] = []
        self._conn = sqlite3.connect(
            str(self.path), timeout=30, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 30000")
        mode = str(self._conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
        if mode != "wal":
            self._conn.close()
            raise PaperSimulationError("paper simulation SQLite WAL mode is unavailable")
        self._conn.execute("PRAGMA synchronous = FULL")
        self._create_schema()
        self._ensure_run()

    def __enter__(self) -> IntradayPaperStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.flush_pending()
        finally:
            self._conn.close()

    @property
    def pending_event_count(self) -> int:
        return len(self._pending_events)

    def queue_payload(
        self,
        plan_id: str,
        payload: Mapping[str, Any],
        *,
        event_kind: str,
        now: datetime,
        commission_fraction: Decimal | str | None = None,
    ) -> list[dict[str, Any]]:
        """Queue one frame without disk I/O; auto-flush at a fixed bounded size.

        Stream integrations should also call :meth:`flush_pending` on a short
        timer. A crash can lose only the unflushed tail; callers must record a
        data gap after restart rather than claiming that tail as complete.
        """

        clean_plan_id = _identifier(plan_id, "plan_id")
        if event_kind not in {"trade", "orderbook"}:
            raise ValueError("event_kind must be trade or orderbook")
        observed_at = _utc_datetime(now, "now")
        if not isinstance(payload, Mapping):
            raise TypeError("stream payload must be an object")
        self._pending_events.append(
            {
                "plan_id": clean_plan_id,
                "payload": dict(payload),
                "event_kind": event_kind,
                "now": observed_at,
                "commission_fraction": commission_fraction,
            }
        )
        if len(self._pending_events) >= self.MAX_PENDING_EVENTS:
            return self.flush_pending()
        return []

    def flush_pending(self) -> list[dict[str, Any]]:
        """Durably process the pending frame tail with one SQLite FULL commit."""

        if not self._pending_events:
            return []
        pending = list(self._pending_events)
        with self._write():
            results = [self.process_payload(**event) for event in pending]
        del self._pending_events[: len(pending)]
        return results

    def current_cash(self) -> Decimal:
        row = self._run_row()
        return _stored_decimal(row["current_cash"], "stored current_cash")

    def assert_ready(self, session_date: date | str) -> None:
        session = _date(session_date, "session_date")
        if not self.config.start_date <= session <= self.config.end_date:
            raise PaperSimulationBlocked("session_date is outside the inclusive run window")
        if session.weekday() >= 5:
            raise PaperSimulationBlocked("simulation plans require a US weekday session")
        run = self._run_row()
        if run["blocked_reason"]:
            raise PaperSimulationBlocked(str(run["blocked_reason"]))
        nonterminal = self._conn.execute(
            """
            SELECT plan_id, status FROM paper_plans
            WHERE run_id = ? AND status IN ('WAITING_ENTRY', 'OPEN', 'UNRESOLVED')
            ORDER BY session_date LIMIT 1
            """,
            (self.config.run_id,),
        ).fetchone()
        if nonterminal is not None:
            raise PaperSimulationBlocked(
                "nonterminal_simulation_plan:"
                f"{nonterminal['plan_id']}:{nonterminal['status']}"
            )
        existing = self._conn.execute(
            "SELECT plan_id FROM paper_plans WHERE run_id = ? AND session_date = ?",
            (self.config.run_id, session.isoformat()),
        ).fetchone()
        if existing is not None:
            raise PaperSimulationBlocked("a simulation plan is already locked for this session")
        market_closed = self._conn.execute(
            """
            SELECT 1 FROM paper_market_closed_sessions
            WHERE run_id = ? AND session_date = ?
            """,
            (self.config.run_id, session.isoformat()),
        ).fetchone()
        if market_closed is not None:
            raise PaperSimulationBlocked("session is already recorded as MARKET_CLOSED")
        no_candidate = self._conn.execute(
            """
            SELECT 1 FROM paper_no_candidate_sessions
            WHERE run_id = ? AND session_date = ?
            """,
            (self.config.run_id, session.isoformat()),
        ).fetchone()
        if no_candidate is not None:
            raise PaperSimulationBlocked("session is already recorded as NO_CANDIDATE")
        latest = self._conn.execute(
            "SELECT MAX(session_date) FROM paper_plans WHERE run_id = ?",
            (self.config.run_id,),
        ).fetchone()[0]
        if latest is not None and session.isoformat() <= str(latest):
            raise PaperSimulationBlocked("simulation plans must be registered in date order")
        if self.current_cash() <= 0:
            raise PaperSimulationBlocked("virtual USD cash is exhausted")

    def ensure_plan(
        self,
        record: Mapping[str, Any],
        *,
        registered_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Validate and immutably register a ``load_intraday_plan``-shaped record."""

        plan = _validated_plan(record, expected_account_key=self.account_key)
        at = _utc_datetime(registered_at or datetime.now(timezone.utc), "registered_at")
        with self._write():
            existing = self._conn.execute(
                "SELECT * FROM paper_plans WHERE run_id = ? AND plan_id = ?",
                (self.config.run_id, plan["plan_id"]),
            ).fetchone()
            if existing is not None:
                if existing["plan_hash"] != plan["plan_hash"]:
                    raise PaperSimulationError("plan_id was already registered with different data")
                return self._plan_payload(existing)
            self.assert_ready(plan["session_date"])
            cash = self.current_cash()
            if plan["available_cash"] != cash:
                raise PaperSimulationError(
                    "plan available_cash must equal current virtual USD cash"
                )
            worst_entry_notional = plan["entry_limit"] * plan["quantity"]
            worst_entry_fee = (
                worst_entry_notional * plan["default_commission_fraction"]
                + plan["fixed_round_trip_cost"] / 2
            )
            if worst_entry_notional + worst_entry_fee > cash:
                raise PaperSimulationError("plan cannot be fully funded by virtual USD cash")
            self._conn.execute(
                """
                INSERT INTO paper_plans (
                    run_id, plan_id, plan_hash, account_key, session_date, symbol,
                    plan_json, status, quantity, entry_start, entry_expiry,
                    force_exit_at, regular_close, entry_trigger, entry_limit,
                    target_trigger, target_limit, stop_trigger, stop_limit,
                    round_trip_buffer_fraction, fixed_round_trip_cost,
                    default_commission_fraction, default_fee_source,
                    cash_before, cash_after, registered_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, 'WAITING_ENTRY', ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    self.config.run_id,
                    plan["plan_id"],
                    plan["plan_hash"],
                    self.account_key,
                    plan["session_date"].isoformat(),
                    plan["symbol"],
                    plan["plan_json"],
                    plan["quantity"],
                    _iso(plan["entry_start"]),
                    _iso(plan["entry_expiry"]),
                    _iso(plan["force_exit_at"]),
                    _iso(plan["regular_close"]),
                    _dstr(plan["entry_trigger"]),
                    _dstr(plan["entry_limit"]),
                    _dstr(plan["target_trigger"]),
                    _dstr(plan["target_limit"]),
                    _dstr(plan["stop_trigger"]),
                    _dstr(plan["stop_limit"]),
                    _dstr(plan["round_trip_buffer_fraction"]),
                    _dstr(plan["fixed_round_trip_cost"]),
                    _dstr(plan["default_commission_fraction"]),
                    plan["default_fee_source"],
                    _dstr(cash),
                    _dstr(cash),
                    _iso(at),
                ),
            )
            self._enqueue_alert(
                plan_id=plan["plan_id"],
                event="plan_registered",
                level="info",
                at=at,
                payload={
                    "status": "WAITING_ENTRY",
                    "session_date": plan["session_date"].isoformat(),
                    "symbol": plan["symbol"],
                    "quantity": plan["quantity"],
                    "cash_after": _dstr(cash),
                },
            )
            row = self._plan_row(plan["plan_id"])
            return self._plan_payload(row)

    def record_market_closed(
        self,
        session_date: date | str,
        *,
        recorded_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Idempotently account for one expected weekday with no market session."""

        session = _date(session_date, "session_date")
        if not self.config.start_date <= session <= self.config.end_date:
            raise PaperSimulationError("session_date is outside the inclusive run window")
        if session.weekday() >= 5:
            raise PaperSimulationError("MARKET_CLOSED applies only to expected weekdays")
        at = _utc_datetime(recorded_at or datetime.now(timezone.utc), "recorded_at")
        with self._write():
            plan = self._conn.execute(
                "SELECT 1 FROM paper_plans WHERE run_id = ? AND session_date = ?",
                (self.config.run_id, session.isoformat()),
            ).fetchone()
            if plan is not None:
                raise PaperSimulationError("session already has a simulation plan")
            no_candidate = self._conn.execute(
                """
                SELECT 1 FROM paper_no_candidate_sessions
                WHERE run_id = ? AND session_date = ?
                """,
                (self.config.run_id, session.isoformat()),
            ).fetchone()
            if no_candidate is not None:
                raise PaperSimulationError("session is already recorded as NO_CANDIDATE")
            self._conn.execute(
                """
                INSERT OR IGNORE INTO paper_market_closed_sessions (
                    run_id, session_date, recorded_at
                ) VALUES (?, ?, ?)
                """,
                (self.config.run_id, session.isoformat(), _iso(at)),
            )
            row = self._conn.execute(
                """
                SELECT recorded_at FROM paper_market_closed_sessions
                WHERE run_id = ? AND session_date = ?
                """,
                (self.config.run_id, session.isoformat()),
            ).fetchone()
        return {
            "run_id": self.config.run_id,
            "session_date": session.isoformat(),
            "status": "MARKET_CLOSED",
            "recorded_at": str(row["recorded_at"]),
        }

    def record_no_candidate(
        self,
        session_date: date | str,
        *,
        recorded_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Idempotently cover one evaluated session with no eligible candidate."""

        session = _date(session_date, "session_date")
        at = _utc_datetime(recorded_at or datetime.now(timezone.utc), "recorded_at")
        with self._write():
            existing = self._conn.execute(
                """
                SELECT recorded_at FROM paper_no_candidate_sessions
                WHERE run_id = ? AND session_date = ?
                """,
                (self.config.run_id, session.isoformat()),
            ).fetchone()
            if existing is None:
                self.assert_ready(session)
                self._conn.execute(
                    """
                    INSERT INTO paper_no_candidate_sessions (
                        run_id, session_date, recorded_at
                    ) VALUES (?, ?, ?)
                    """,
                    (self.config.run_id, session.isoformat(), _iso(at)),
                )
                existing = self._conn.execute(
                    """
                    SELECT recorded_at FROM paper_no_candidate_sessions
                    WHERE run_id = ? AND session_date = ?
                    """,
                    (self.config.run_id, session.isoformat()),
                ).fetchone()
        return {
            "run_id": self.config.run_id,
            "session_date": session.isoformat(),
            "status": "NO_CANDIDATE",
            "recorded_at": str(existing["recorded_at"]),
        }

    def load_plan(self, plan_id: str) -> dict[str, Any]:
        """Return the current durable paper-plan state for stream integration."""

        return self._plan_payload(self._plan_row(_identifier(plan_id, "plan_id")))

    def begin_stream_instance(
        self,
        plan_id: str,
        instance_id: str,
        *,
        started_at: datetime,
    ) -> dict[str, Any]:
        """Durably mark a stream process before it may buffer market frames."""

        clean_plan_id = _identifier(plan_id, "plan_id")
        clean_instance_id = _identifier(instance_id, "instance_id")
        at = _utc_datetime(started_at, "started_at")
        with self._write():
            self._plan_row(clean_plan_id)
            previous = self._conn.execute(
                """
                SELECT instance_id, started_at FROM paper_stream_instances
                WHERE run_id = ? AND plan_id = ? AND ended_at IS NULL
                ORDER BY started_at DESC LIMIT 1
                """,
                (self.config.run_id, clean_plan_id),
            ).fetchone()
            if previous is not None:
                self._conn.execute(
                    """
                    UPDATE paper_stream_instances
                    SET ended_at = ?, end_reason = 'superseded_by_stream_restart'
                    WHERE run_id = ? AND plan_id = ? AND ended_at IS NULL
                    """,
                    (_iso(at), self.config.run_id, clean_plan_id),
                )
            try:
                self._conn.execute(
                    """
                    INSERT INTO paper_stream_instances (
                        instance_id, run_id, plan_id, started_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        clean_instance_id,
                        self.config.run_id,
                        clean_plan_id,
                        _iso(at),
                        _iso(at),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise PaperSimulationError("stream instance id already exists") from exc
            return {
                "instance_id": clean_instance_id,
                "previous_unclosed": previous is not None,
                "previous_started_at": (
                    previous["started_at"] if previous is not None else None
                ),
            }

    def touch_stream_instance(
        self,
        instance_id: str,
        *,
        observed_at: datetime,
    ) -> bool:
        """Refresh one running stream marker so finalization can detect a crash."""

        clean_instance_id = _identifier(instance_id, "instance_id")
        at = _utc_datetime(observed_at, "observed_at")
        with self._write():
            updated = self._conn.execute(
                """
                UPDATE paper_stream_instances SET last_seen_at = ?
                WHERE run_id = ? AND instance_id = ? AND ended_at IS NULL
                """,
                (_iso(at), self.config.run_id, clean_instance_id),
            )
            return updated.rowcount == 1

    def end_stream_instance(
        self,
        instance_id: str,
        *,
        ended_at: datetime,
        reason: str,
    ) -> bool:
        """Mark an orderly stream end; an absent marker remains fail-closed."""

        clean_instance_id = _identifier(instance_id, "instance_id")
        clean_reason = _safe_reason(reason)
        at = _utc_datetime(ended_at, "ended_at")
        with self._write():
            updated = self._conn.execute(
                """
                UPDATE paper_stream_instances
                SET ended_at = ?, last_seen_at = ?, end_reason = ?
                WHERE run_id = ? AND instance_id = ? AND ended_at IS NULL
                """,
                (
                    _iso(at),
                    _iso(at),
                    clean_reason,
                    self.config.run_id,
                    clean_instance_id,
                ),
            )
            return updated.rowcount == 1

    def process_payload(
        self,
        plan_id: str,
        payload: Mapping[str, Any],
        *,
        event_kind: str,
        now: datetime | None = None,
        commission_fraction: Decimal | str | None = None,
    ) -> dict[str, Any]:
        """Journal one fresh stream snapshot and deterministically advance its plan.

        A trigger only arms an order. A different, subsequently accepted orderbook
        snapshot is required for a limit fill, preventing use of a pre-trigger quote.
        """

        clean_plan_id = _identifier(plan_id, "plan_id")
        if event_kind not in {"trade", "orderbook"}:
            raise ValueError("event_kind must be trade or orderbook")
        observed_now = _utc_datetime(now or datetime.now(timezone.utc), "now")
        commission = (
            None
            if commission_fraction is None
            else _fraction(commission_fraction, "commission_fraction")
        )
        with self._write():
            row = self._plan_row(clean_plan_id)
            self._validate_stream_identity(row, payload)
            frame, frame_inserted = self._journal_frame(
                row, payload, event_kind=event_kind, now=observed_now
            )
            if payload.get("shadow_usable") is not True:
                return {
                    "duplicate": not frame_inserted,
                    "action": "WARMING_UP",
                    "journaled_event_kind": event_kind,
                    "journaled_event_at": _iso(frame["event_at"]),
                    "plan": self._plan_payload(self._plan_row(clean_plan_id)),
                }
            normalized = self._normalize_stream_payload(row, payload, observed_now)
            observation_hash = _sha256(
                _canonical_json(
                    {
                        "run_id": self.config.run_id,
                        "plan_id": clean_plan_id,
                        "event_kind": event_kind,
                        "market": normalized,
                    }
                )
            )
            duplicate = self._conn.execute(
                """
                SELECT processed_action FROM market_observations
                WHERE run_id = ? AND observation_hash = ?
                """,
                (self.config.run_id, observation_hash),
            ).fetchone()
            if duplicate is not None:
                return {
                    "duplicate": True,
                    "action": duplicate["processed_action"],
                    "plan": self._plan_payload(self._plan_row(clean_plan_id)),
                }
            last = self._conn.execute(
                """
                SELECT event_at FROM market_observations
                WHERE run_id = ? AND plan_id = ? ORDER BY observation_id DESC LIMIT 1
                """,
                (self.config.run_id, clean_plan_id),
            ).fetchone()
            if last is not None and _parse_datetime(last["event_at"], "event_at") > normalized["event_at"]:
                raise PaperSimulationError("new market observation regressed in time")
            inserted = self._conn.execute(
                """
                INSERT INTO market_observations (
                    run_id, plan_id, observation_hash, event_at, trade_at, book_at,
                    book_hash, market_json, processed_action, accepted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'NONE', ?)
                """,
                (
                    self.config.run_id,
                    clean_plan_id,
                    observation_hash,
                    _iso(normalized["event_at"]),
                    _iso(normalized["trade_at"]),
                    _iso(normalized["book_at"]),
                    normalized["book_hash"],
                    _canonical_json(normalized),
                    _iso(observed_now),
                ),
            )
            observation_id = int(inserted.lastrowid)
            self._conn.execute(
                """
                UPDATE paper_plans SET
                    accepted_event_count = accepted_event_count + 1,
                    first_event_at = COALESCE(first_event_at, ?), last_event_at = ?
                WHERE run_id = ? AND plan_id = ?
                """,
                (
                    _iso(normalized["event_at"]),
                    _iso(normalized["event_at"]),
                    self.config.run_id,
                    clean_plan_id,
                ),
            )
            row = self._plan_row(clean_plan_id)
            action = self._advance(
                row,
                normalized,
                observation_id=observation_id,
                event_kind=event_kind,
                commission_fraction=commission,
            )
            self._conn.execute(
                "UPDATE market_observations SET processed_action = ? WHERE observation_id = ?",
                (action, observation_id),
            )
            return {
                "duplicate": False,
                "action": action,
                "journaled_event_kind": event_kind,
                "plan": self._plan_payload(self._plan_row(clean_plan_id)),
            }

    def record_data_gap(
        self,
        plan_id: str,
        reason: str,
        *,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        """Record stream unavailability; an open position exits on the next fresh bid."""

        clean_plan_id = _identifier(plan_id, "plan_id")
        clean_reason = _safe_reason(reason)
        observed_at = _utc_datetime(at or datetime.now(timezone.utc), "at")
        if self._pending_events:
            self.flush_pending()
        with self._write():
            return self._record_data_gap_locked(
                self._plan_row(clean_plan_id), reason=clean_reason, at=observed_at
            )

    def finalize_session(
        self,
        plan_id: str,
        *,
        now: datetime | None = None,
        commission_fraction: Decimal | str | None = None,
    ) -> dict[str, Any]:
        """Resolve expiry/force-exit using only already journaled fresh market data."""

        clean_plan_id = _identifier(plan_id, "plan_id")
        at = _utc_datetime(now or datetime.now(timezone.utc), "now")
        commission = (
            None
            if commission_fraction is None
            else _fraction(commission_fraction, "commission_fraction")
        )
        if self._pending_events:
            self.flush_pending()
        with self._write():
            row = self._plan_row(clean_plan_id)
            status = str(row["status"])
            finalization_due = (
                status == "WAITING_ENTRY"
                and at >= _parse_datetime(row["entry_expiry"], "entry_expiry")
                or status == "OPEN"
                and at >= _parse_datetime(row["force_exit_at"], "force_exit_at")
            )
            if finalization_due:
                boundary = _parse_datetime(
                    row[
                        "entry_expiry"
                        if status == "WAITING_ENTRY"
                        else "force_exit_at"
                    ],
                    "stream coverage boundary",
                )
                stream_rows = self._conn.execute(
                    """
                    SELECT started_at, last_seen_at, ended_at
                    FROM paper_stream_instances
                    WHERE run_id = ? AND plan_id = ?
                    ORDER BY started_at DESC
                    """,
                    (self.config.run_id, clean_plan_id),
                ).fetchall()
                coverage_complete = False
                active_stream = None
                active_liveness_failed = False
                for stream_row in stream_rows:
                    started = _parse_datetime(
                        stream_row["started_at"], "stream started_at"
                    )
                    last_seen = _parse_datetime(
                        stream_row["last_seen_at"] or stream_row["started_at"],
                        "stream last_seen_at",
                    )
                    ended = (
                        None
                        if stream_row["ended_at"] is None
                        else _parse_datetime(stream_row["ended_at"], "stream ended_at")
                    )
                    coverage_complete = coverage_complete or bool(
                        started <= boundary and (ended or last_seen) >= boundary
                    )
                    if active_stream is None and ended is None:
                        active_stream = (started, last_seen)
                if active_stream is not None:
                    started, last_seen = active_stream
                    age = (at - last_seen).total_seconds()
                    liveness_fresh = (
                        -self.config.future_tolerance_seconds
                        <= age
                        <= self.config.quote_max_age_seconds
                    )
                    if liveness_fresh and (coverage_complete or started <= boundary):
                        return self._plan_payload(row)
                    if not liveness_fresh:
                        active_liveness_failed = True
                        self._conn.execute(
                            """
                            UPDATE paper_stream_instances
                            SET ended_at = ?, end_reason = 'stream_liveness_expired'
                            WHERE run_id = ? AND plan_id = ? AND ended_at IS NULL
                            """,
                            (_iso(at), self.config.run_id, clean_plan_id),
                        )
                if active_liveness_failed:
                    self._record_data_gap_locked(
                        row,
                        reason=(
                            "stream_process_interrupted"
                            if coverage_complete
                            else "stream_coverage_incomplete"
                        ),
                        at=at,
                    )
                    row = self._plan_row(clean_plan_id)
                    status = str(row["status"])
                elif not coverage_complete:
                    self._record_data_gap_locked(
                        row,
                        reason="stream_coverage_incomplete",
                        at=at,
                    )
                    row = self._plan_row(clean_plan_id)
                    status = str(row["status"])
            if status == "WAITING_ENTRY" and at >= _parse_datetime(row["entry_expiry"], "entry_expiry"):
                self._finish_waiting(row, at=at)
            elif status == "OPEN" and at >= _parse_datetime(row["force_exit_at"], "force_exit_at"):
                latest = self._latest_observation(clean_plan_id)
                force_exit_at = _parse_datetime(row["force_exit_at"], "force_exit_at")
                latest_gap = self._conn.execute(
                    """
                    SELECT observed_at FROM paper_data_gaps
                    WHERE run_id = ? AND plan_id = ?
                    ORDER BY observed_at DESC LIMIT 1
                    """,
                    (self.config.run_id, clean_plan_id),
                ).fetchone()
                freshness_boundary = force_exit_at
                if latest_gap is not None:
                    freshness_boundary = max(
                        freshness_boundary,
                        _parse_datetime(latest_gap["observed_at"], "gap observed_at"),
                    )
                filled = False
                if latest is not None:
                    market = _decoded_market(json.loads(latest["market_json"]))
                    book_at = market["book_at"]
                    age = (at - book_at).total_seconds()
                    if (
                        book_at >= freshness_boundary
                        and 0 <= age <= self.config.quote_max_age_seconds
                    ):
                        filled = self._try_exit(
                            row,
                            market,
                            reason=(
                                "DATA_GAP"
                                if bool(row["data_quality_invalid"])
                                else "FORCE"
                            ),
                            at=at,
                            commission_fraction=commission,
                            ignore_limit=True,
                        )
                if not filled and at >= _parse_datetime(row["regular_close"], "regular_close"):
                    self._mark_unresolved(row, at=at, reason="force_exit_market_data_unresolved")
            return self._plan_payload(self._plan_row(clean_plan_id))

    def list_alerts(self, *, pending_only: bool = True) -> list[dict[str, Any]]:
        where = "AND forwarded_at IS NULL" if pending_only else ""
        rows = self._conn.execute(
            f"""
            SELECT * FROM paper_alert_outbox
            WHERE run_id = ? {where}
            ORDER BY created_at, alert_id
            """,
            (self.config.run_id,),
        ).fetchall()
        return [
            {
                "alert_id": row["alert_id"],
                "plan_id": row["plan_id"],
                "event": row["event"],
                "level": row["level"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "forwarded_at": row["forwarded_at"],
            }
            for row in rows
        ]

    def mark_alert_forwarded(
        self, alert_id: str, *, forwarded_at: datetime | None = None
    ) -> bool:
        clean_id = _identifier(alert_id, "alert_id")
        at = _utc_datetime(forwarded_at or datetime.now(timezone.utc), "forwarded_at")
        with self._write():
            updated = self._conn.execute(
                """
                UPDATE paper_alert_outbox SET forwarded_at = ?
                WHERE run_id = ? AND alert_id = ? AND forwarded_at IS NULL
                """,
                (_iso(at), self.config.run_id, clean_id),
            )
            if updated.rowcount == 1:
                return True
            exists = self._conn.execute(
                "SELECT 1 FROM paper_alert_outbox WHERE run_id = ? AND alert_id = ?",
                (self.config.run_id, clean_id),
            ).fetchone()
            if exists is None:
                raise PaperSimulationError("alert_id does not exist")
            return False

    def daily_summary(self, session_date: date | str) -> dict[str, Any]:
        session = _date(session_date, "session_date")
        row = self._conn.execute(
            "SELECT * FROM paper_plans WHERE run_id = ? AND session_date = ?",
            (self.config.run_id, session.isoformat()),
        ).fetchone()
        if row is None:
            no_candidate = self._conn.execute(
                """
                SELECT recorded_at FROM paper_no_candidate_sessions
                WHERE run_id = ? AND session_date = ?
                """,
                (self.config.run_id, session.isoformat()),
            ).fetchone()
            if no_candidate is not None:
                return {
                    "run_id": self.config.run_id,
                    "session_date": session.isoformat(),
                    "status": "NO_CANDIDATE",
                    "recorded_at": str(no_candidate["recorded_at"]),
                }
            closed = self._conn.execute(
                """
                SELECT recorded_at FROM paper_market_closed_sessions
                WHERE run_id = ? AND session_date = ?
                """,
                (self.config.run_id, session.isoformat()),
            ).fetchone()
            if closed is not None:
                return {
                    "run_id": self.config.run_id,
                    "session_date": session.isoformat(),
                    "status": "MARKET_CLOSED",
                    "recorded_at": str(closed["recorded_at"]),
                }
            return {
                "run_id": self.config.run_id,
                "session_date": session.isoformat(),
                "status": "NO_PLAN",
            }
        return self._daily_row(row)

    def month_summary(self, *, as_of: datetime | None = None) -> dict[str, Any]:
        at = _utc_datetime(as_of or datetime.now(timezone.utc), "as_of")
        rows = self._conn.execute(
            "SELECT * FROM paper_plans WHERE run_id = ? ORDER BY session_date",
            (self.config.run_id,),
        ).fetchall()
        clean = [row for row in rows if row["status"] == "CLOSED"]
        invalid = [row for row in rows if row["status"] == "INVALID"]
        unresolved = [row for row in rows if row["status"] == "UNRESOLVED"]
        open_rows = [row for row in rows if row["status"] == "OPEN"]
        waiting = [row for row in rows if row["status"] == "WAITING_ENTRY"]
        expected_dates = _expected_weekday_dates(
            self.config.start_date, self.config.end_date
        )
        planned_dates = {str(row["session_date"]) for row in rows}
        market_closed_dates = {
            str(row["session_date"])
            for row in self._conn.execute(
                """
                SELECT session_date FROM paper_market_closed_sessions
                WHERE run_id = ? ORDER BY session_date
                """,
                (self.config.run_id,),
            )
        }
        no_candidate_dates = {
            str(row["session_date"])
            for row in self._conn.execute(
                """
                SELECT session_date FROM paper_no_candidate_sessions
                WHERE run_id = ? ORDER BY session_date
                """,
                (self.config.run_id,),
            )
        }
        covered_dates = planned_dates | market_closed_dates | no_candidate_dates
        missing_dates = [value for value in expected_dates if value not in covered_dates]
        clean_pnl = sum(
            (_stored_decimal(row["realized_pnl"], "realized_pnl") for row in clean),
            _ZERO,
        )
        all_exits = [row for row in rows if row["realized_pnl"] is not None]
        realized = sum(
            (_stored_decimal(row["realized_pnl"], "realized_pnl") for row in all_exits),
            _ZERO,
        )
        fees = sum(
            (
                _stored_decimal(row["entry_fee"], "entry_fee")
                if row["entry_fee"] is not None
                else _ZERO
            )
            + (
                _stored_decimal(row["exit_fee"], "exit_fee")
                if row["exit_fee"] is not None
                else _ZERO
            )
            for row in rows
        )
        run = self._run_row()
        if unresolved:
            status = "UNRESOLVED"
        elif open_rows:
            status = "OPEN"
        elif waiting:
            status = "WAITING"
        elif invalid:
            status = "INVALID"
        elif run["blocked_reason"]:
            status = "BLOCKED"
        elif at.date() <= self.config.end_date:
            status = "ACTIVE"
        elif not rows or missing_dates:
            status = "INCOMPLETE"
        else:
            status = "COMPLETE"
        wins = sum(
            _stored_decimal(row["realized_pnl"], "realized_pnl") > 0 for row in clean
        )
        losses = sum(
            _stored_decimal(row["realized_pnl"], "realized_pnl") < 0 for row in clean
        )
        clean_values = [
            _stored_decimal(row["realized_pnl"], "realized_pnl") for row in clean
        ]
        win_values = [value for value in clean_values if value > 0]
        loss_values = [value for value in clean_values if value < 0]
        gross_profit = sum(win_values, _ZERO)
        gross_loss = -sum(loss_values, _ZERO)
        win_rate = Decimal(wins) / Decimal(len(clean)) if clean else None
        average_win = gross_profit / Decimal(len(win_values)) if win_values else None
        average_loss = sum(loss_values, _ZERO) / Decimal(len(loss_values)) if loss_values else None
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
        expectancy = clean_pnl / Decimal(len(clean)) if clean else None
        exit_reason_counts: dict[str, int] = {}
        for row in rows:
            if row["exit_at"] is not None and row["exit_reason"]:
                reason = str(row["exit_reason"])
                exit_reason_counts[reason] = exit_reason_counts.get(reason, 0) + 1
        cash_points = [self.config.initial_cash_usd] + [
            _stored_decimal(row["cash_after"], "cash_after")
            for row in self._conn.execute(
                """
                SELECT cash_after FROM paper_cash_ledger
                WHERE run_id = ? ORDER BY ledger_id
                """,
                (self.config.run_id,),
            )
        ]
        closed_equity_points = [self.config.initial_cash_usd] + [
            _stored_decimal(row["cash_after"], "cash_after")
            for row in rows
            if row["status"] in {"CLOSED", "INVALID", "NO_ENTRY"}
            and not (row["status"] == "INVALID" and row["entry_at"] is not None and row["exit_at"] is None)
        ]
        cash_drawdown_usd, cash_drawdown_fraction = _max_drawdown(cash_points)
        equity_drawdown_usd, equity_drawdown_fraction = _max_drawdown(closed_equity_points)
        current_cash = self.current_cash()
        final_equity = None if open_rows or unresolved else current_cash
        final_return = (
            None
            if final_equity is None
            else (final_equity - self.config.initial_cash_usd) / self.config.initial_cash_usd
        )
        return {
            "schema_version": 1,
            "run_id": self.config.run_id,
            "simulation_account_key": self.account_key,
            "status": status,
            "blocked_reason": run["blocked_reason"],
            "start_date": self.config.start_date.isoformat(),
            "end_date_inclusive": self.config.end_date.isoformat(),
            "as_of": _iso(at),
            "initial_cash_usd": _dstr(self.config.initial_cash_usd),
            "current_cash_usd": _dstr(current_cash),
            "final_equity_usd": _optional_dstr(final_equity),
            "final_return_fraction": _optional_dstr(final_return),
            "return_fraction": _optional_dstr(final_return),
            "realized_pnl_usd": _dstr(realized),
            "clean_realized_pnl_usd": _dstr(clean_pnl),
            "clean_return_fraction": _dstr(clean_pnl / self.config.initial_cash_usd),
            "max_cash_drawdown_usd": _dstr(cash_drawdown_usd),
            "max_cash_drawdown_fraction": _dstr(cash_drawdown_fraction),
            "max_closed_equity_drawdown_usd": _dstr(equity_drawdown_usd),
            "max_closed_equity_drawdown_fraction": _dstr(equity_drawdown_fraction),
            "max_drawdown_fraction": _dstr(equity_drawdown_fraction),
            "total_fees_usd": _dstr(fees),
            "plan_count": len(rows),
            "trade_count": len(clean),
            "clean_trade_count": len(clean),
            "invalid_result_count": len(invalid),
            "unresolved_position_count": len(unresolved) + len(open_rows),
            "waiting_plan_count": len(waiting),
            "wins": wins,
            "losses": losses,
            "win_rate": _optional_dstr(win_rate),
            "average_win_usd": _optional_dstr(average_win),
            "average_loss_usd": _optional_dstr(average_loss),
            "profit_factor": _optional_dstr(profit_factor),
            "expectancy_usd": _optional_dstr(expectancy),
            "exit_reason_counts": dict(sorted(exit_reason_counts.items())),
            "no_entry_count": sum(row["status"] == "NO_ENTRY" for row in rows),
            "no_candidate_count": len(no_candidate_dates),
            "accepted_event_count": sum(int(row["accepted_event_count"]) for row in rows),
            "journaled_frame_count": sum(int(row["journaled_frame_count"]) for row in rows),
            "data_gap_count": sum(int(row["data_gap_count"]) for row in rows),
            "coverage": {
                "expected": expected_dates,
                "covered": [value for value in expected_dates if value in covered_dates],
                "missing": missing_dates,
                "planned": sorted(planned_dates),
                "market_closed": sorted(market_closed_dates),
                "no_candidate": sorted(no_candidate_dates),
                "expected_count": len(expected_dates),
                "covered_count": len(expected_dates) - len(missing_dates),
                "missing_count": len(missing_dates),
            },
            "fee_model": {
                "variable": "active_broker_commission_per_leg_when_supplied; otherwise immutable plan commission snapshot; otherwise configured round-trip buffer split equally",
                "fixed": "configured fixed round-trip cost split equally across entry and exit",
                "planning_round_trip_buffer_is_not_an_extra_fee": True,
            },
            "journal_policy": {
                "sqlite_synchronous": "FULL",
                "wal": True,
                "max_unflushed_tail_events": self.MAX_PENDING_EVENTS - 1,
                "gap_free_claim": False,
            },
            "days": [self._daily_row(row) for row in rows],
        }

    def summary(self, *, as_of: datetime | None = None) -> dict[str, Any]:
        return self.month_summary(as_of=as_of)

    def _create_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS paper_runs (
                run_id TEXT PRIMARY KEY,
                config_hash TEXT NOT NULL,
                config_json TEXT NOT NULL,
                account_key TEXT NOT NULL UNIQUE,
                initial_cash TEXT NOT NULL,
                current_cash TEXT NOT NULL,
                blocked_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_plans (
                run_id TEXT NOT NULL,
                plan_id TEXT NOT NULL,
                plan_hash TEXT NOT NULL,
                account_key TEXT NOT NULL,
                session_date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                status TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                entry_start TEXT NOT NULL,
                entry_expiry TEXT NOT NULL,
                force_exit_at TEXT NOT NULL,
                regular_close TEXT NOT NULL,
                entry_trigger TEXT NOT NULL,
                entry_limit TEXT NOT NULL,
                target_trigger TEXT NOT NULL,
                target_limit TEXT NOT NULL,
                stop_trigger TEXT NOT NULL,
                stop_limit TEXT NOT NULL,
                round_trip_buffer_fraction TEXT NOT NULL,
                fixed_round_trip_cost TEXT NOT NULL,
                default_commission_fraction TEXT NOT NULL,
                default_fee_source TEXT NOT NULL,
                cash_before TEXT NOT NULL,
                cash_after TEXT NOT NULL,
                entry_armed_at TEXT,
                entry_armed_observation_id INTEGER,
                entry_armed_book_hash TEXT,
                exit_armed_reason TEXT,
                exit_armed_at TEXT,
                exit_armed_observation_id INTEGER,
                exit_armed_book_hash TEXT,
                entry_at TEXT,
                entry_price TEXT,
                entry_fee TEXT,
                entry_commission_fraction TEXT,
                entry_fee_source TEXT,
                exit_at TEXT,
                exit_price TEXT,
                exit_fee TEXT,
                exit_commission_fraction TEXT,
                exit_fee_source TEXT,
                exit_reason TEXT,
                realized_pnl TEXT,
                data_quality_invalid INTEGER NOT NULL DEFAULT 0,
                data_gap_count INTEGER NOT NULL DEFAULT 0,
                accepted_event_count INTEGER NOT NULL DEFAULT 0,
                journaled_frame_count INTEGER NOT NULL DEFAULT 0,
                first_event_at TEXT,
                last_event_at TEXT,
                registered_at TEXT NOT NULL,
                PRIMARY KEY (run_id, plan_id),
                UNIQUE (run_id, session_date),
                FOREIGN KEY (run_id) REFERENCES paper_runs(run_id)
            );
            CREATE TABLE IF NOT EXISTS market_observations (
                observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                plan_id TEXT NOT NULL,
                observation_hash TEXT NOT NULL,
                event_at TEXT NOT NULL,
                trade_at TEXT NOT NULL,
                book_at TEXT NOT NULL,
                book_hash TEXT NOT NULL,
                market_json TEXT NOT NULL,
                processed_action TEXT NOT NULL,
                accepted_at TEXT NOT NULL,
                UNIQUE (run_id, observation_hash),
                FOREIGN KEY (run_id, plan_id) REFERENCES paper_plans(run_id, plan_id)
            );
            CREATE TABLE IF NOT EXISTS market_frames (
                frame_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                plan_id TEXT NOT NULL,
                event_kind TEXT NOT NULL,
                event_hash TEXT NOT NULL,
                event_at TEXT NOT NULL,
                frame_json TEXT NOT NULL,
                accepted_at TEXT NOT NULL,
                UNIQUE (run_id, event_hash),
                FOREIGN KEY (run_id, plan_id) REFERENCES paper_plans(run_id, plan_id)
            );
            CREATE TABLE IF NOT EXISTS paper_cash_ledger (
                ledger_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                plan_id TEXT,
                event_key TEXT NOT NULL,
                event_type TEXT NOT NULL,
                amount TEXT NOT NULL,
                cash_after TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (run_id, event_key),
                FOREIGN KEY (run_id) REFERENCES paper_runs(run_id)
            );
            CREATE TABLE IF NOT EXISTS paper_data_gaps (
                gap_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                plan_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                FOREIGN KEY (run_id, plan_id) REFERENCES paper_plans(run_id, plan_id)
            );
            CREATE TABLE IF NOT EXISTS paper_market_closed_sessions (
                run_id TEXT NOT NULL,
                session_date TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                PRIMARY KEY (run_id, session_date),
                FOREIGN KEY (run_id) REFERENCES paper_runs(run_id)
            );
            CREATE TABLE IF NOT EXISTS paper_no_candidate_sessions (
                run_id TEXT NOT NULL,
                session_date TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                PRIMARY KEY (run_id, session_date),
                FOREIGN KEY (run_id) REFERENCES paper_runs(run_id)
            );
            CREATE TABLE IF NOT EXISTS paper_stream_instances (
                instance_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                plan_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                ended_at TEXT,
                end_reason TEXT,
                FOREIGN KEY (run_id, plan_id) REFERENCES paper_plans(run_id, plan_id)
            );
            CREATE TABLE IF NOT EXISTS paper_alert_outbox (
                alert_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                plan_id TEXT,
                event TEXT NOT NULL,
                level TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                forwarded_at TEXT,
                FOREIGN KEY (run_id) REFERENCES paper_runs(run_id)
            );
            CREATE INDEX IF NOT EXISTS idx_paper_observations_plan_time
                ON market_observations(run_id, plan_id, event_at);
            CREATE INDEX IF NOT EXISTS idx_paper_frames_plan_time
                ON market_frames(run_id, plan_id, event_at);
            CREATE INDEX IF NOT EXISTS idx_paper_stream_instances_open
                ON paper_stream_instances(run_id, plan_id, ended_at);
            """
        )
        stream_columns = {
            str(row["name"])
            for row in self._conn.execute(
                "PRAGMA table_info(paper_stream_instances)"
            ).fetchall()
        }
        if "last_seen_at" not in stream_columns:
            try:
                self._conn.execute(
                    "ALTER TABLE paper_stream_instances ADD COLUMN last_seen_at TEXT"
                )
            except sqlite3.OperationalError:
                if "last_seen_at" not in {
                    str(row["name"])
                    for row in self._conn.execute(
                        "PRAGMA table_info(paper_stream_instances)"
                    ).fetchall()
                }:
                    raise
            self._conn.execute(
                "UPDATE paper_stream_instances SET last_seen_at = started_at"
            )

    def _ensure_run(self) -> None:
        config_json = _config_json(self.config)
        config_hash = _sha256(config_json)
        now = _iso(datetime.now(timezone.utc))
        with self._write():
            row = self._conn.execute(
                "SELECT * FROM paper_runs WHERE run_id = ?", (self.config.run_id,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    """
                    INSERT INTO paper_runs (
                        run_id, config_hash, config_json, account_key, initial_cash,
                        current_cash, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self.config.run_id,
                        config_hash,
                        config_json,
                        self.account_key,
                        _dstr(self.config.initial_cash_usd),
                        _dstr(self.config.initial_cash_usd),
                        now,
                        now,
                    ),
                )
                self._conn.execute(
                    """
                    INSERT INTO paper_cash_ledger (
                        run_id, event_key, event_type, amount, cash_after, created_at
                    ) VALUES (?, 'initial', 'INITIAL', ?, ?, ?)
                    """,
                    (
                        self.config.run_id,
                        _dstr(self.config.initial_cash_usd),
                        _dstr(self.config.initial_cash_usd),
                        now,
                    ),
                )
            elif row["config_hash"] != config_hash or row["config_json"] != config_json:
                raise PaperSimulationError("run_id already exists with different immutable config")

    def _advance(
        self,
        row: sqlite3.Row,
        market: dict[str, Any],
        *,
        observation_id: int,
        event_kind: str,
        commission_fraction: Decimal | None,
    ) -> str:
        status = str(row["status"])
        event_at = market["event_at"]
        if status == "WAITING_ENTRY":
            start = _parse_datetime(row["entry_start"], "entry_start")
            expiry = _parse_datetime(row["entry_expiry"], "entry_expiry")
            if event_at < start:
                return "BEFORE_ENTRY_WINDOW"
            if event_at >= expiry:
                return self._finish_waiting(row, at=event_at)
            if row["entry_armed_observation_id"] is None:
                trade_at = market["trade_at"]
                if event_kind != "trade":
                    return "WAIT_ENTRY_TRIGGER"
                if trade_at < start:
                    return "BEFORE_ENTRY_WINDOW"
                trade = market["trade_price"]
                if trade < _stored_decimal(row["entry_trigger"], "entry_trigger"):
                    return "WAIT_ENTRY_TRIGGER"
                self._conn.execute(
                    """
                    UPDATE paper_plans SET entry_armed_at = ?,
                        entry_armed_observation_id = ?, entry_armed_book_hash = ?
                    WHERE run_id = ? AND plan_id = ?
                    """,
                    (
                        _iso(trade_at),
                        observation_id,
                        market["book_hash"],
                        self.config.run_id,
                        row["plan_id"],
                    ),
                )
                return "ENTRY_ARMED"
            if event_kind != "orderbook" or market["book_hash"] == row["entry_armed_book_hash"]:
                return "ENTRY_WAIT_NEW_BOOK"
            quantity = int(row["quantity"])
            if market["best_ask_volume"] < quantity:
                return "ENTRY_WAIT_DEPTH"
            fill_price = _buy_fill_price(
                market["best_ask"], self.config.slippage_fraction
            )
            if fill_price > _stored_decimal(row["entry_limit"], "entry_limit"):
                return "ENTRY_WAIT_LIMIT"
            self._enter(
                row,
                at=event_at,
                price=fill_price,
                commission_fraction=commission_fraction,
            )
            return "ENTRY_FILLED"
        if status != "OPEN":
            return "PLAN_FINAL"
        if bool(row["data_quality_invalid"]):
            if event_kind != "orderbook":
                return "DATA_GAP_EXIT_WAIT_NEW_BOOK"
            if self._try_exit(
                row,
                market,
                reason="DATA_GAP",
                at=event_at,
                commission_fraction=commission_fraction,
                ignore_limit=True,
            ):
                return "DATA_GAP_EXIT_FILLED"
            return "DATA_GAP_EXIT_WAIT_DEPTH"
        if event_at >= _parse_datetime(row["force_exit_at"], "force_exit_at"):
            if event_kind != "orderbook":
                return "FORCE_EXIT_WAIT_NEW_BOOK"
            if self._try_exit(
                row,
                market,
                reason="FORCE",
                at=event_at,
                commission_fraction=commission_fraction,
                ignore_limit=True,
            ):
                return "FORCE_EXIT_FILLED"
            return "FORCE_EXIT_WAIT_DEPTH"

        armed_reason = row["exit_armed_reason"]
        trade_at = market["trade_at"]
        entry_at = _parse_datetime(row["entry_at"], "entry_at")
        if event_kind == "trade" and trade_at >= entry_at:
            stop_hit = market["trade_price"] <= _stored_decimal(
                row["stop_trigger"], "stop_trigger"
            )
            target_hit = market["trade_price"] >= _stored_decimal(
                row["target_trigger"], "target_trigger"
            )
            if stop_hit and armed_reason != "STOP":
                self._arm_exit(
                    row, "STOP", trade_at, observation_id, market["book_hash"]
                )
                return "STOP_ARMED"
            if armed_reason is None and target_hit:
                self._arm_exit(
                    row, "TARGET", trade_at, observation_id, market["book_hash"]
                )
                return "TARGET_ARMED"
        row = self._plan_row(str(row["plan_id"]))
        armed_reason = row["exit_armed_reason"]
        if armed_reason is None:
            return "WAIT_EXIT_TRIGGER"
        if event_kind != "orderbook" or market["book_hash"] == row["exit_armed_book_hash"]:
            return f"{armed_reason}_WAIT_NEW_BOOK"
        limit = _stored_decimal(
            row["stop_limit"] if armed_reason == "STOP" else row["target_limit"],
            "exit_limit",
        )
        if self._try_exit(
            row,
            market,
            reason=str(armed_reason),
            at=event_at,
            commission_fraction=commission_fraction,
            ignore_limit=False,
            limit=limit,
        ):
            return f"{armed_reason}_EXIT_FILLED"
        return f"{armed_reason}_EXIT_WAIT_BOOK"

    def _enter(
        self,
        row: sqlite3.Row,
        *,
        at: datetime,
        price: Decimal,
        commission_fraction: Decimal | None,
    ) -> None:
        quantity = int(row["quantity"])
        fraction, source = self._fill_commission(row, commission_fraction)
        fee = price * quantity * fraction + _stored_decimal(
            row["fixed_round_trip_cost"], "fixed_round_trip_cost"
        ) / 2
        debit = price * quantity + fee
        cash = self.current_cash()
        if debit > cash:
            raise PaperSimulationError("virtual cash cannot fund the deterministic full fill")
        cash_after = cash - debit
        self._update_cash(cash_after, at=at)
        self._conn.execute(
            """
            INSERT INTO paper_cash_ledger (
                run_id, plan_id, event_key, event_type, amount, cash_after, created_at
            ) VALUES (?, ?, ?, 'ENTRY', ?, ?, ?)
            """,
            (
                self.config.run_id,
                row["plan_id"],
                f"{row['plan_id']}:entry",
                _dstr(-debit),
                _dstr(cash_after),
                _iso(at),
            ),
        )
        self._conn.execute(
            """
            UPDATE paper_plans SET status = 'OPEN', entry_at = ?, entry_price = ?,
                entry_fee = ?, entry_commission_fraction = ?, entry_fee_source = ?,
                cash_after = ? WHERE run_id = ? AND plan_id = ?
            """,
            (
                _iso(at),
                _dstr(price),
                _dstr(fee),
                _dstr(fraction),
                source,
                _dstr(cash_after),
                self.config.run_id,
                row["plan_id"],
            ),
        )
        self._enqueue_alert(
            plan_id=str(row["plan_id"]),
            event="entry_filled",
            level="info",
            at=at,
            payload={
                "symbol": row["symbol"],
                "session_date": row["session_date"],
                "quantity": quantity,
                "price": _dstr(price),
                "fee": _dstr(fee),
                "fee_source": source,
                "cash_after": _dstr(cash_after),
            },
        )

    def _try_exit(
        self,
        row: sqlite3.Row,
        market: Mapping[str, Any],
        *,
        reason: str,
        at: datetime,
        commission_fraction: Decimal | None,
        ignore_limit: bool,
        limit: Decimal | None = None,
    ) -> bool:
        quantity = int(row["quantity"])
        if market["best_bid_volume"] < quantity:
            return False
        price = _sell_fill_price(market["best_bid"], self.config.slippage_fraction)
        if not ignore_limit and (limit is None or price < limit):
            return False
        fraction, source = self._fill_commission(row, commission_fraction)
        fee = price * quantity * fraction + _stored_decimal(
            row["fixed_round_trip_cost"], "fixed_round_trip_cost"
        ) / 2
        proceeds = price * quantity - fee
        cash_after = self.current_cash() + proceeds
        entry_price = _stored_decimal(row["entry_price"], "entry_price")
        entry_fee = _stored_decimal(row["entry_fee"], "entry_fee")
        pnl = proceeds - (entry_price * quantity + entry_fee)
        invalid = bool(row["data_quality_invalid"]) or reason == "DATA_GAP"
        status = "INVALID" if invalid else "CLOSED"
        self._update_cash(cash_after, at=at)
        self._conn.execute(
            """
            INSERT INTO paper_cash_ledger (
                run_id, plan_id, event_key, event_type, amount, cash_after, created_at
            ) VALUES (?, ?, ?, 'EXIT', ?, ?, ?)
            """,
            (
                self.config.run_id,
                row["plan_id"],
                f"{row['plan_id']}:exit",
                _dstr(proceeds),
                _dstr(cash_after),
                _iso(at),
            ),
        )
        self._conn.execute(
            """
            UPDATE paper_plans SET status = ?, exit_at = ?, exit_price = ?,
                exit_fee = ?, exit_commission_fraction = ?, exit_fee_source = ?,
                exit_reason = ?, realized_pnl = ?, cash_after = ?
            WHERE run_id = ? AND plan_id = ?
            """,
            (
                status,
                _iso(at),
                _dstr(price),
                _dstr(fee),
                _dstr(fraction),
                source,
                reason,
                _dstr(pnl),
                _dstr(cash_after),
                self.config.run_id,
                row["plan_id"],
            ),
        )
        self._enqueue_alert(
            plan_id=str(row["plan_id"]),
            event="invalid_exit" if invalid else "exit_filled",
            level="warn" if invalid else "info",
            at=at,
            payload={
                "symbol": row["symbol"],
                "session_date": row["session_date"],
                "quantity": quantity,
                "reason": reason,
                "price": _dstr(price),
                "fee": _dstr(fee),
                "realized_pnl": _dstr(pnl),
                "cash_after": _dstr(cash_after),
                "included_in_clean_metrics": not invalid,
            },
        )
        return True

    def _finish_waiting(self, row: sqlite3.Row, *, at: datetime) -> str:
        expiry = _parse_datetime(row["entry_expiry"], "entry_expiry")
        last = self._conn.execute(
            """
            SELECT MAX(event_at) FROM market_observations
            WHERE run_id = ? AND plan_id = ? AND event_at >= ? AND event_at < ?
            """,
            (
                self.config.run_id,
                row["plan_id"],
                row["entry_start"],
                row["entry_expiry"],
            ),
        ).fetchone()[0]
        complete = last is not None and 0 <= (
            expiry - _parse_datetime(last, "last entry event")
        ).total_seconds() <= self.config.quote_max_age_seconds
        status = "NO_ENTRY" if complete and not bool(row["data_quality_invalid"]) else "INVALID"
        reason = "entry_window_expired" if status == "NO_ENTRY" else "entry_window_data_gap"
        self._conn.execute(
            """
            UPDATE paper_plans SET status = ?, exit_reason = ?, cash_after = ?
            WHERE run_id = ? AND plan_id = ?
            """,
            (status, reason, _dstr(self.current_cash()), self.config.run_id, row["plan_id"]),
        )
        self._enqueue_alert(
            plan_id=str(row["plan_id"]),
            event="no_entry" if status == "NO_ENTRY" else "invalid_no_entry",
            level="info" if status == "NO_ENTRY" else "warn",
            at=at,
            payload={
                "session_date": row["session_date"],
                "reason": reason,
                "cash_after": _dstr(self.current_cash()),
                "included_in_clean_metrics": status == "NO_ENTRY",
            },
        )
        return "ENTRY_EXPIRED" if status == "NO_ENTRY" else "ENTRY_DATA_GAP"

    def _record_data_gap_locked(
        self, row: sqlite3.Row, *, reason: str, at: datetime
    ) -> dict[str, Any]:
        clean_reason = _safe_reason(reason)
        gap_identity = (
            f"{self.config.run_id}:{row['plan_id']}:{clean_reason}:{_iso(at)}"
        )
        gap_id = f"gap-{_sha256(gap_identity)[:24]}"
        inserted = self._conn.execute(
            """
            INSERT OR IGNORE INTO paper_data_gaps (
                gap_id, run_id, plan_id, reason, observed_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (gap_id, self.config.run_id, row["plan_id"], clean_reason, _iso(at)),
        )
        if inserted.rowcount == 1:
            if row["status"] == "WAITING_ENTRY":
                self._conn.execute(
                    """
                    UPDATE paper_plans SET status = 'INVALID', data_quality_invalid = 1,
                        data_gap_count = data_gap_count + 1, exit_reason = ?, cash_after = ?
                    WHERE run_id = ? AND plan_id = ?
                    """,
                    (
                        clean_reason,
                        _dstr(self.current_cash()),
                        self.config.run_id,
                        row["plan_id"],
                    ),
                )
            elif row["status"] == "OPEN":
                self._conn.execute(
                    """
                    UPDATE paper_plans SET data_quality_invalid = 1,
                        data_gap_count = data_gap_count + 1
                    WHERE run_id = ? AND plan_id = ?
                    """,
                    (self.config.run_id, row["plan_id"]),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE paper_plans SET data_gap_count = data_gap_count + 1
                    WHERE run_id = ? AND plan_id = ?
                    """,
                    (self.config.run_id, row["plan_id"]),
                )
            self._enqueue_alert(
                plan_id=str(row["plan_id"]),
                event="market_data_gap",
                level="warn",
                at=at,
                payload={
                    "session_date": row["session_date"],
                    "reason": clean_reason,
                    "had_open_position": row["status"] == "OPEN",
                    "next_fresh_bid_action": (
                        "force_conservative_exit" if row["status"] == "OPEN" else "none"
                    ),
                },
            )
        return {
            "duplicate": inserted.rowcount == 0,
            "action": "DATA_GAP_RECORDED",
            "plan": self._plan_payload(self._plan_row(str(row["plan_id"]))),
        }

    def _mark_unresolved(self, row: sqlite3.Row, *, at: datetime, reason: str) -> None:
        self._conn.execute(
            """
            UPDATE paper_plans SET status = 'UNRESOLVED', data_quality_invalid = 1,
                exit_reason = ?, data_gap_count = data_gap_count + 1
            WHERE run_id = ? AND plan_id = ?
            """,
            (reason, self.config.run_id, row["plan_id"]),
        )
        blocker = f"unresolved_simulated_position:{row['plan_id']}"
        self._conn.execute(
            "UPDATE paper_runs SET blocked_reason = ?, updated_at = ? WHERE run_id = ?",
            (blocker, _iso(at), self.config.run_id),
        )
        self._enqueue_alert(
            plan_id=str(row["plan_id"]),
            event="unresolved_position",
            level="error",
            at=at,
            payload={
                "session_date": row["session_date"],
                "reason": reason,
                "cash_after": _dstr(self.current_cash()),
                "future_plans_blocked": True,
            },
        )

    def _arm_exit(
        self,
        row: sqlite3.Row,
        reason: str,
        at: datetime,
        observation_id: int,
        book_hash: str,
    ) -> None:
        self._conn.execute(
            """
            UPDATE paper_plans SET exit_armed_reason = ?, exit_armed_at = ?,
                exit_armed_observation_id = ?, exit_armed_book_hash = ?
            WHERE run_id = ? AND plan_id = ?
            """,
            (
                reason,
                _iso(at),
                observation_id,
                book_hash,
                self.config.run_id,
                row["plan_id"],
            ),
        )

    def _fill_commission(
        self, row: sqlite3.Row, supplied: Decimal | None
    ) -> tuple[Decimal, str]:
        if supplied is not None:
            return supplied, "active_broker_commission_supplied"
        return (
            _stored_decimal(row["default_commission_fraction"], "default commission"),
            str(row["default_fee_source"]),
        )

    def _journal_frame(
        self,
        row: sqlite3.Row,
        payload: Mapping[str, Any],
        *,
        event_kind: str,
        now: datetime,
    ) -> tuple[dict[str, Any], bool]:
        if payload.get("schema_version") != 1 or payload.get("mode") != "shadow":
            raise PaperSimulationError("stream payload schema or mode is invalid")
        if payload.get("live_order_submission") is not False:
            raise PaperSimulationError("paper stream payload must hard-disable live orders")
        source = payload.get("trade" if event_kind == "trade" else "orderbook")
        if not isinstance(source, Mapping):
            raise PaperSimulationError(f"stream {event_kind} event is missing")
        if source.get("currency") != "USD" or source.get("source") != "websocket":
            raise PaperSimulationError(f"stream {event_kind} source is invalid")
        event_at = _parse_datetime(source.get("broker_at"), f"{event_kind}.broker_at")
        received_at = _parse_datetime(
            source.get("received_at"), f"{event_kind}.received_at"
        )
        for name, value in (("broker_at", event_at), ("received_at", received_at)):
            age = (now - value).total_seconds()
            if (
                age < -self.config.future_tolerance_seconds
                or age > self.config.quote_max_age_seconds
            ):
                raise PaperSimulationError(f"{event_kind}.{name} is not fresh")
        if event_kind == "trade":
            normalized = {
                "event_kind": "trade",
                "price": _stream_decimal(source.get("price"), "trade.price"),
                "volume": _stream_decimal(source.get("volume"), "trade.volume"),
                "event_at": event_at,
                "received_at": received_at,
            }
        else:
            if source.get("timestamp_source") != "broker":
                raise PaperSimulationError("orderbook broker timestamp is required")
            bid = _stream_decimal(source.get("best_bid"), "orderbook.best_bid")
            ask = _stream_decimal(source.get("best_ask"), "orderbook.best_ask")
            if bid >= ask:
                raise PaperSimulationError("orderbook must have positive spread")
            normalized = {
                "event_kind": "orderbook",
                "best_bid": bid,
                "best_bid_volume": _stream_decimal(
                    source.get("best_bid_volume"), "orderbook.best_bid_volume"
                ),
                "best_ask": ask,
                "best_ask_volume": _stream_decimal(
                    source.get("best_ask_volume"), "orderbook.best_ask_volume"
                ),
                "event_at": event_at,
                "received_at": received_at,
            }
        frame_json = _canonical_json(normalized)
        previous = self._conn.execute(
            """
            SELECT event_at FROM market_frames
            WHERE run_id = ? AND plan_id = ? AND event_kind = ?
            ORDER BY frame_id DESC LIMIT 1
            """,
            (self.config.run_id, row["plan_id"], event_kind),
        ).fetchone()
        if previous is not None and _parse_datetime(
            previous["event_at"], "previous frame event_at"
        ) > event_at:
            raise PaperSimulationError(f"new {event_kind} frame regressed in time")
        event_hash = _sha256(
            _canonical_json(
                {
                    "run_id": self.config.run_id,
                    "plan_id": row["plan_id"],
                    "frame": normalized,
                }
            )
        )
        inserted = self._conn.execute(
            """
            INSERT OR IGNORE INTO market_frames (
                run_id, plan_id, event_kind, event_hash, event_at, frame_json, accepted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.config.run_id,
                row["plan_id"],
                event_kind,
                event_hash,
                _iso(event_at),
                frame_json,
                _iso(now),
            ),
        )
        if inserted.rowcount == 1:
            self._conn.execute(
                """
                UPDATE paper_plans
                SET journaled_frame_count = journaled_frame_count + 1
                WHERE run_id = ? AND plan_id = ?
                """,
                (self.config.run_id, row["plan_id"]),
            )
        return normalized, inserted.rowcount == 1

    def _normalize_stream_payload(
        self, row: sqlite3.Row, payload: Mapping[str, Any], now: datetime
    ) -> dict[str, Any]:
        if payload.get("schema_version") != 1 or payload.get("mode") != "shadow":
            raise PaperSimulationError("stream payload schema or mode is invalid")
        if payload.get("live_order_submission") is not False:
            raise PaperSimulationError("paper stream payload must hard-disable live orders")
        if payload.get("ready_for_live_entry") is not False:
            raise PaperSimulationError("paper stream payload must not be live-entry ready")
        errors = payload.get("error_codes")
        if not isinstance(errors, list) or errors:
            raise PaperSimulationError("usable stream payload must have no error codes")
        generation = payload.get("generation")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise PaperSimulationError("stream generation is invalid")
        valid_until = _parse_datetime(payload.get("valid_until"), "valid_until")
        if now > valid_until:
            raise PaperSimulationError("stream payload is outside valid_until")
        trade = payload.get("trade")
        book = payload.get("orderbook")
        if not isinstance(trade, Mapping) or not isinstance(book, Mapping):
            raise PaperSimulationError("stream trade and orderbook are required")
        if trade.get("currency") != "USD" or book.get("currency") != "USD":
            raise PaperSimulationError("stream currency must be USD")
        if trade.get("source") != "websocket" or book.get("source") != "websocket":
            raise PaperSimulationError("only normalized websocket events are accepted")
        if book.get("timestamp_source") != "broker":
            raise PaperSimulationError("orderbook broker timestamp is required")
        trade_at = _parse_datetime(trade.get("broker_at"), "trade.broker_at")
        book_at = _parse_datetime(book.get("broker_at"), "orderbook.broker_at")
        trade_received = _parse_datetime(trade.get("received_at"), "trade.received_at")
        book_received = _parse_datetime(book.get("received_at"), "orderbook.received_at")
        tolerance = self.config.future_tolerance_seconds
        for name, value in (
            ("trade.broker_at", trade_at),
            ("orderbook.broker_at", book_at),
            ("trade.received_at", trade_received),
            ("orderbook.received_at", book_received),
        ):
            age = (now - value).total_seconds()
            if age < -tolerance or age > self.config.quote_max_age_seconds:
                raise PaperSimulationError(f"{name} is not fresh")
        if abs((trade_at - book_at).total_seconds()) > self.config.quote_max_age_seconds:
            raise PaperSimulationError("trade/orderbook timestamp skew is too large")
        trade_price = _stream_decimal(trade.get("price"), "trade.price")
        trade_volume = _stream_decimal(trade.get("volume"), "trade.volume")
        bid = _stream_decimal(book.get("best_bid"), "orderbook.best_bid")
        ask = _stream_decimal(book.get("best_ask"), "orderbook.best_ask")
        bid_volume = _stream_decimal(
            book.get("best_bid_volume"), "orderbook.best_bid_volume"
        )
        ask_volume = _stream_decimal(
            book.get("best_ask_volume"), "orderbook.best_ask_volume"
        )
        if bid >= ask:
            raise PaperSimulationError("orderbook must have positive spread")
        book_data = {
            "best_bid": _dstr(bid),
            "best_bid_volume": _dstr(bid_volume),
            "best_ask": _dstr(ask),
            "best_ask_volume": _dstr(ask_volume),
            "book_at": _iso(book_at),
            "book_received_at": _iso(book_received),
        }
        return {
            "generation": generation,
            "symbol": row["symbol"],
            "trade_price": trade_price,
            "trade_volume": trade_volume,
            "best_bid": bid,
            "best_bid_volume": bid_volume,
            "best_ask": ask,
            "best_ask_volume": ask_volume,
            "trade_at": trade_at,
            "book_at": book_at,
            "event_at": max(trade_at, book_at),
            "book_hash": _sha256(_canonical_json(book_data)),
        }

    def _validate_stream_identity(
        self, row: sqlite3.Row, payload: Mapping[str, Any]
    ) -> None:
        if not isinstance(payload, Mapping):
            raise TypeError("stream payload must be an object")
        if str(payload.get("symbol") or "").upper() != row["symbol"]:
            raise PaperSimulationError("stream symbol does not match the locked plan")
        if str(payload.get("session_date") or "") != row["session_date"]:
            raise PaperSimulationError("stream session_date does not match the locked plan")

    def _latest_observation(self, plan_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            """
            SELECT * FROM market_observations
            WHERE run_id = ? AND plan_id = ? ORDER BY observation_id DESC LIMIT 1
            """,
            (self.config.run_id, plan_id),
        ).fetchone()

    def _update_cash(self, cash: Decimal, *, at: datetime) -> None:
        if cash < 0:
            raise PaperSimulationError("virtual cash cannot become negative")
        self._conn.execute(
            "UPDATE paper_runs SET current_cash = ?, updated_at = ? WHERE run_id = ?",
            (_dstr(cash), _iso(at), self.config.run_id),
        )

    def _enqueue_alert(
        self,
        *,
        plan_id: str | None,
        event: str,
        level: str,
        at: datetime,
        payload: Mapping[str, Any],
    ) -> None:
        ordinal = self._conn.execute(
            """
            SELECT COUNT(*) FROM paper_alert_outbox
            WHERE run_id = ? AND plan_id IS ? AND event = ?
            """,
            (self.config.run_id, plan_id, event),
        ).fetchone()[0]
        identity = f"{self.config.run_id}:{plan_id or 'run'}:{event}:{ordinal + 1}"
        alert_id = f"alert-{_sha256(identity)[:24]}"
        self._conn.execute(
            """
            INSERT INTO paper_alert_outbox (
                alert_id, run_id, plan_id, event, level, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                alert_id,
                self.config.run_id,
                plan_id,
                event,
                level,
                _canonical_json(dict(payload)),
                _iso(at),
            ),
        )

    def _daily_row(self, row: sqlite3.Row) -> dict[str, Any]:
        entry_price = (
            _stored_decimal(row["entry_price"], "entry_price")
            if row["entry_price"] is not None
            else None
        )
        exit_price = (
            _stored_decimal(row["exit_price"], "exit_price")
            if row["exit_price"] is not None
            else None
        )
        entry_fee = (
            _stored_decimal(row["entry_fee"], "entry_fee")
            if row["entry_fee"] is not None
            else _ZERO
        )
        exit_fee = (
            _stored_decimal(row["exit_fee"], "exit_fee")
            if row["exit_fee"] is not None
            else _ZERO
        )
        gross_pnl = (
            (exit_price - entry_price) * int(row["quantity"])
            if entry_price is not None and exit_price is not None
            else None
        )
        return {
            "run_id": self.config.run_id,
            "session_date": row["session_date"],
            "plan_id": row["plan_id"],
            "symbol": row["symbol"],
            "status": row["status"],
            "quantity": row["quantity"],
            "entry_at": row["entry_at"],
            "entry_price": row["entry_price"],
            "entry_fee": row["entry_fee"],
            "exit_at": row["exit_at"],
            "exit_price": row["exit_price"],
            "exit_fee": row["exit_fee"],
            "exit_reason": row["exit_reason"],
            "gross_pnl": _optional_dstr(gross_pnl),
            "total_fees": _dstr(entry_fee + exit_fee),
            "net_pnl": row["realized_pnl"],
            "realized_pnl": row["realized_pnl"],
            "cash_before": row["cash_before"],
            "cash_after": row["cash_after"],
            "included_in_clean_metrics": row["status"] in {"CLOSED", "NO_ENTRY"},
            "accepted_event_count": row["accepted_event_count"],
            "journaled_frame_count": row["journaled_frame_count"],
            "data_gap_count": row["data_gap_count"],
            "first_event_at": row["first_event_at"],
            "last_event_at": row["last_event_at"],
            "fee_sources": {
                "entry": row["entry_fee_source"],
                "exit": row["exit_fee_source"],
            },
        }

    def _plan_payload(self, row: sqlite3.Row) -> dict[str, Any]:
        return self._daily_row(row) | {
            "entry_start": row["entry_start"],
            "entry_expiry": row["entry_expiry"],
            "force_exit_at": row["force_exit_at"],
            "regular_close": row["regular_close"],
            "entry_armed_at": row["entry_armed_at"],
            "exit_armed_reason": row["exit_armed_reason"],
            "data_quality_invalid": bool(row["data_quality_invalid"]),
        }

    def _plan_row(self, plan_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM paper_plans WHERE run_id = ? AND plan_id = ?",
            (self.config.run_id, plan_id),
        ).fetchone()
        if row is None:
            raise PaperSimulationError("paper plan does not exist")
        return row

    def _run_row(self) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM paper_runs WHERE run_id = ?", (self.config.run_id,)
        ).fetchone()
        if row is None:  # pragma: no cover - initialized in __init__
            raise PaperSimulationError("paper run does not exist")
        return row

    @contextmanager
    def _write(self) -> Iterator[None]:
        if self._write_depth:
            self._write_depth += 1
            try:
                yield
            finally:
                self._write_depth -= 1
            return
        self._conn.execute("BEGIN IMMEDIATE")
        self._write_depth = 1
        try:
            yield
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()
        finally:
            self._write_depth = 0


def _validated_plan(
    record: Mapping[str, Any], *, expected_account_key: str
) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise TypeError("plan record must be an object")
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        raise PaperSimulationError("plan record payload is required")
    payload_object = dict(payload)
    plan_json = _canonical_json(payload_object)
    plan_hash = str(record.get("plan_hash") or "").lower()
    if not _HASH_RE.fullmatch(plan_hash) or _sha256(plan_json) != plan_hash:
        raise PaperSimulationError("plan hash failed integrity verification")
    plan_id = _identifier(record.get("plan_id"), "plan_id")
    account_key = _identifier(record.get("account_key"), "account_key")
    session = _date(record.get("session_date"), "session_date")
    symbol = str(record.get("symbol") or "").upper()
    if not _SYMBOL_RE.fullmatch(symbol):
        raise PaperSimulationError("plan symbol is invalid")
    if record.get("mode") != "shadow" or payload.get("mode") != "shadow":
        raise PaperSimulationError("paper plan mode must be shadow")
    if payload.get("live_order_submission") is not False:
        raise PaperSimulationError("paper plan must hard-disable live order submission")
    if (
        payload.get("plan_id") != plan_id
        or payload.get("account_id") != account_key
        or payload.get("session_date") != session.isoformat()
        or str(payload.get("symbol") or "").upper() != symbol
    ):
        raise PaperSimulationError("plan record metadata does not match its payload")
    if account_key != expected_account_key:
        raise PaperSimulationError("plan is not addressed to the simulation account")
    quantity = payload.get("quantity")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
        raise PaperSimulationError("plan quantity must be positive whole shares")
    entry_start = _parse_datetime(payload.get("entry_start"), "entry_start")
    entry_expiry = _parse_datetime(payload.get("entry_expiry"), "entry_expiry")
    force_exit = _parse_datetime(payload.get("force_exit_at"), "force_exit_at")
    regular_close = _parse_datetime(payload.get("regular_close"), "regular_close")
    if not entry_start < entry_expiry < force_exit < regular_close:
        raise PaperSimulationError("plan execution timestamps are misordered")
    prices = {
        name: _stream_decimal(payload.get(name), name)
        for name in (
            "entry_trigger",
            "entry_limit",
            "target_trigger",
            "target_limit",
            "stop_trigger",
            "stop_limit",
        )
    }
    if not (
        _ZERO < prices["stop_limit"] <= prices["stop_trigger"]
        < prices["entry_trigger"] <= prices["entry_limit"]
        < prices["target_limit"] <= prices["target_trigger"]
    ):
        raise PaperSimulationError("plan prices are misordered")
    available_cash = _stream_decimal(payload.get("available_cash"), "available_cash")
    round_trip = _fraction(
        payload.get("estimated_round_trip_cost_fraction"),
        "estimated_round_trip_cost_fraction",
    )
    fixed_cost = _decimal(
        payload.get("estimated_fixed_round_trip_cost"),
        "estimated_fixed_round_trip_cost",
        nonnegative=True,
    )
    commission_snapshot = payload.get("commission_snapshot")
    if isinstance(commission_snapshot, Mapping) and commission_snapshot.get(
        "broker_commission_fraction"
    ) is not None:
        default_commission = _fraction(
            commission_snapshot.get("broker_commission_fraction"),
            "commission_snapshot.broker_commission_fraction",
        )
        fee_source = "immutable_plan_broker_commission_snapshot"
    else:
        default_commission = round_trip / 2
        fee_source = "configured_round_trip_buffer_split"
    return {
        "plan_id": plan_id,
        "plan_hash": plan_hash,
        "plan_json": plan_json,
        "session_date": session,
        "symbol": symbol,
        "quantity": quantity,
        "entry_start": entry_start,
        "entry_expiry": entry_expiry,
        "force_exit_at": force_exit,
        "regular_close": regular_close,
        "available_cash": available_cash,
        "round_trip_buffer_fraction": round_trip,
        "fixed_round_trip_cost": fixed_cost,
        "default_commission_fraction": default_commission,
        "default_fee_source": fee_source,
        **prices,
    }


def _config_json(config: IntradayPaperConfig) -> str:
    values = asdict(config)
    values["start_date"] = config.start_date.isoformat()
    values["end_date"] = config.end_date.isoformat()
    values["initial_cash_usd"] = _dstr(config.initial_cash_usd)
    values["slippage_fraction"] = _dstr(config.slippage_fraction)
    return _canonical_json(values)


def _config_hash(config: IntradayPaperConfig) -> str:
    return _sha256(_config_json(config))


def _buy_fill_price(ask: Decimal, slippage: Decimal) -> Decimal:
    return _round_market_price(ask * (_ONE + slippage), ROUND_CEILING)


def _sell_fill_price(bid: Decimal, slippage: Decimal) -> Decimal:
    return _round_market_price(bid * (_ONE - slippage), ROUND_FLOOR)


def _decoded_market(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PaperSimulationError("stored market observation is invalid")
    decoded = dict(value)
    for key in (
        "trade_price",
        "trade_volume",
        "best_bid",
        "best_bid_volume",
        "best_ask",
        "best_ask_volume",
    ):
        decoded[key] = _stored_decimal(decoded.get(key), key)
    for key in ("trade_at", "book_at", "event_at"):
        decoded[key] = _parse_datetime(decoded.get(key), key)
    return decoded


def _round_market_price(price: Decimal, rounding: str) -> Decimal:
    if price <= 0:
        raise PaperSimulationError("slippage produced a nonpositive fill price")
    tick = Decimal("0.01") if price >= 1 else Decimal("0.0001")
    return (price / tick).to_integral_value(rounding=rounding) * tick


def _canonical_json(value: Any) -> str:
    def safe(item: Any) -> Any:
        if isinstance(item, Decimal):
            return _dstr(item)
        if isinstance(item, datetime):
            return _iso(item)
        if isinstance(item, date):
            return item.isoformat()
        if isinstance(item, Mapping):
            return {str(key): safe(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [safe(child) for child in item]
        return item

    try:
        return json.dumps(
            safe(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise PaperSimulationError("value must be canonical JSON data") from exc


def _identifier(value: Any, name: str) -> str:
    result = str(value or "").strip()
    if not _ID_RE.fullmatch(result):
        raise ValueError(f"{name} must be a short safe identifier")
    return result


def _safe_reason(value: Any) -> str:
    result = str(value or "").strip()
    if not _ID_RE.fullmatch(result):
        raise ValueError("data-gap reason must be a short safe identifier")
    return result


def _date(value: Any, name: str) -> date:
    if isinstance(value, datetime):
        raise ValueError(f"{name} must be a date")
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO date") from exc


def _expected_weekday_dates(start: date, end: date) -> list[str]:
    values: list[str] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            values.append(current.isoformat())
        current += timedelta(days=1)
    return values


def _decimal(
    value: Any,
    name: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a decimal")
    try:
        result = Decimal(value) if isinstance(value, (str, int, Decimal)) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    if positive and result <= 0:
        raise ValueError(f"{name} must be positive")
    if nonnegative and result < 0:
        raise ValueError(f"{name} must be nonnegative")
    return result


def _stream_decimal(value: Any, name: str) -> Decimal:
    if not isinstance(value, str) or not _DECIMAL_RE.fullmatch(value):
        raise PaperSimulationError(f"{name} must be a canonical positive decimal string")
    result = _decimal(value, name, positive=True)
    return result


def _stored_decimal(value: Any, name: str) -> Decimal:
    if value is None:
        raise PaperSimulationError(f"{name} is missing")
    return _decimal(str(value), name)


def _fraction(value: Any, name: str) -> Decimal:
    result = _decimal(value, name, nonnegative=True)
    if result >= 1:
        raise ValueError(f"{name} must be less than 1")
    return result


def _parse_datetime(value: Any, name: str) -> datetime:
    if isinstance(value, datetime):
        return _utc_datetime(value, name)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise PaperSimulationError(f"{name} must be an ISO datetime") from exc
    return _utc_datetime(parsed, name)


def _utc_datetime(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc_datetime(value, "datetime").isoformat()


def _dstr(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _optional_dstr(value: Decimal | None) -> str | None:
    return None if value is None else _dstr(value)


def _max_drawdown(values: list[Decimal]) -> tuple[Decimal, Decimal]:
    if not values:
        return _ZERO, _ZERO
    peak = values[0]
    maximum = _ZERO
    maximum_fraction = _ZERO
    for value in values:
        if value > peak:
            peak = value
        drawdown = peak - value
        fraction = drawdown / peak if peak > 0 else _ZERO
        maximum = max(maximum, drawdown)
        maximum_fraction = max(maximum_fraction, fraction)
    return maximum, maximum_fraction


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
