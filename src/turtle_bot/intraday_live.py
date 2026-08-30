from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import re
from typing import Any, Callable, Mapping, Sequence

from .domain import Side
from .intraday import IntradayPlan, intraday_plan_payload
from .live_execution import LiveBrokerError
from .live_order import BrokerOrderTicket, ExecutionStatus, OrderIntent, OrderType
from .toss_conditional import ConditionalOrderUnknownStateError


_CLIENT_ORDER_ID = re.compile(r"[A-Za-z0-9_-]{1,36}\Z")
_GENERAL_ORDER_PATH = "/api/v1/orders"
_CONDITIONAL_ORDER_PATH = "/api/v1/conditional-orders"
_GENERAL_TERMINAL = frozenset({"FILLED", "CANCELLED", "REJECTED", "FAILED"})
_GENERAL_OPEN = frozenset(
    {"PENDING", "SENT", "ACKNOWLEDGED", "PARTIAL_FILLED", "PENDING_CANCEL"}
)
_CONDITIONAL_ACTIVE = frozenset({"WATCHING", "PAUSED", "ORDERING", "ORDERED"})
_CONDITIONAL_TERMINAL = frozenset({"COMPLETED", "EXPIRED"})
_CONDITIONAL_LEG_STATES = frozenset({"WATCHING", "HOLDING", "CANCELED"})
_ENTRY_STATES = frozenset(
    {
        "APPROVED",
        "RECONCILING",
        "READY_TO_ENTER",
        "ENTRY_SUBMITTING",
        "ENTRY_UNKNOWN",
        "ENTRY_WORKING",
        "ENTRY_CANCELING",
        "OPEN_UNPROTECTED",
        "PROTECTION_SUBMITTING",
        "PROTECTION_UNKNOWN",
        "PROTECTED",
        "EXIT_CANCELING_PROTECTION",
        "EXIT_SUBMITTING",
        "EXIT_UNKNOWN",
        "EXIT_WORKING",
    }
)
_TERMINAL_RUN_STATES = frozenset(
    {"CLOSED", "SKIPPED", "CANCELLED", "RECOVERY_REQUIRED"}
)
_EXIT_ROLES = frozenset({"TRIGGERED_EXIT", "FORCE_EXIT", "EMERGENCY_EXIT"})
_ALLOWED_ORDER_ROLES = frozenset({"ENTRY", *_EXIT_ROLES})


class IntradayRuntimeError(RuntimeError):
    """Fail-closed runtime error with a log-safe reason code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class BrokerOrderObservation:
    order_id: str
    plan_id: str
    role: str
    side: str
    status: str
    quantity: Decimal
    filled_quantity: Decimal
    client_order_id: str | None = None
    average_fill_price: Decimal | None = None

    def __post_init__(self) -> None:
        for name in ("order_id", "plan_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} is required")
        role = str(self.role).upper()
        side = str(self.side).upper()
        status = str(self.status).upper()
        if role not in _ALLOWED_ORDER_ROLES:
            raise ValueError("order role is not owned by the intraday runtime")
        if side not in {"BUY", "SELL"}:
            raise ValueError("order side is invalid")
        if (role == "ENTRY") != (side == "BUY"):
            raise ValueError("order role and side disagree")
        if status not in _GENERAL_OPEN | _GENERAL_TERMINAL | {"UNKNOWN"}:
            raise ValueError("order status is unknown")
        quantity = _finite_decimal(self.quantity, "quantity", positive=True)
        filled = _finite_decimal(self.filled_quantity, "filled_quantity")
        if filled > quantity:
            raise ValueError("filled quantity exceeds requested quantity")
        average = self.average_fill_price
        if filled > 0:
            if average is None:
                raise ValueError("average fill price is required after a fill")
            average = _finite_decimal(average, "average_fill_price", positive=True)
        elif average is not None:
            average = _finite_decimal(average, "average_fill_price", positive=True)
        if self.client_order_id is not None and not _CLIENT_ORDER_ID.fullmatch(
            self.client_order_id
        ):
            raise ValueError("client order ID is invalid")
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "filled_quantity", filled)
        object.__setattr__(self, "average_fill_price", average)


@dataclass(frozen=True, slots=True)
class ConditionalOrderObservation:
    conditional_order_id: str
    plan_id: str
    client_order_id: str
    symbol: str
    market: str
    conditional_type: str
    status: str
    quantity: Decimal
    order_type: str
    expire_date: str
    first: Mapping[str, object]
    second: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.conditional_order_id, str) or not self.conditional_order_id:
            raise ValueError("conditional order ID is required")
        if not isinstance(self.plan_id, str) or not self.plan_id:
            raise ValueError("conditional plan ID is required")
        if not _CLIENT_ORDER_ID.fullmatch(self.client_order_id):
            raise ValueError("conditional client order ID is invalid")
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ValueError("conditional symbol is required")
        if self.market != "US" or self.conditional_type != "OCO":
            raise ValueError("conditional market or type is invalid")
        status = str(self.status).upper()
        if status not in _CONDITIONAL_ACTIVE | _CONDITIONAL_TERMINAL:
            raise ValueError("conditional status is unknown")
        quantity = _finite_decimal(self.quantity, "conditional quantity", positive=True)
        if self.order_type != "LIMIT":
            raise ValueError("intraday OCO must be LIMIT")
        if not isinstance(self.expire_date, str) or not self.expire_date:
            raise ValueError("conditional expiry is required")
        first = _copy_leg(self.first)
        second = _copy_leg(self.second)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "first", first)
        object.__setattr__(self, "second", second)


@dataclass(frozen=True, slots=True)
class BrokerSnapshot:
    symbol: str
    holding_quantity: Decimal
    sellable_quantity: Decimal
    market_open: bool
    halt_state: str
    foreign_activity: bool
    captured_at: datetime
    orders: tuple[BrokerOrderObservation, ...] = ()
    conditional_orders: tuple[ConditionalOrderObservation, ...] = ()
    buying_power: Decimal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("snapshot symbol is required")
        if not isinstance(self.market_open, bool) or not isinstance(
            self.foreign_activity, bool
        ):
            raise TypeError("snapshot flags must be boolean")
        halt_state = str(self.halt_state).upper()
        if halt_state not in {"CLEAR", "HALTED", "UNKNOWN"}:
            raise ValueError("halt state is invalid")
        captured = _utc_datetime(self.captured_at, "captured_at")
        holding = _finite_decimal(self.holding_quantity, "holding_quantity")
        sellable = _finite_decimal(self.sellable_quantity, "sellable_quantity")
        if sellable > holding:
            raise ValueError("sellable quantity exceeds holding quantity")
        buying_power = self.buying_power
        if buying_power is not None:
            buying_power = _finite_decimal(buying_power, "buying_power")
        orders = tuple(self.orders)
        conditionals = tuple(self.conditional_orders)
        if any(not isinstance(item, BrokerOrderObservation) for item in orders):
            raise TypeError("orders must contain BrokerOrderObservation values")
        if any(
            not isinstance(item, ConditionalOrderObservation)
            for item in conditionals
        ):
            raise TypeError(
                "conditional_orders must contain ConditionalOrderObservation values"
            )
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        object.__setattr__(self, "halt_state", halt_state)
        object.__setattr__(self, "captured_at", captured)
        object.__setattr__(self, "holding_quantity", holding)
        object.__setattr__(self, "sellable_quantity", sellable)
        object.__setattr__(self, "buying_power", buying_power)
        object.__setattr__(self, "orders", orders)
        object.__setattr__(self, "conditional_orders", conditionals)


def canonical_order_request(body: Mapping[str, object]) -> tuple[bytes, str]:
    """Return deterministic UTF-8 JSON and its SHA-256 digest."""

    if not isinstance(body, Mapping):
        raise TypeError("order request body must be an object")
    normalized = _canonical_value(body)
    encoded = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return encoded, hashlib.sha256(encoded).hexdigest()


def remaining_owned_quantity(snapshot: BrokerSnapshot, plan_id: str) -> Decimal:
    """Compute owned shares from authoritative cumulative fills without clamping."""

    if not isinstance(snapshot, BrokerSnapshot):
        raise TypeError("snapshot must be a BrokerSnapshot")
    if not isinstance(plan_id, str) or not plan_id:
        raise ValueError("plan_id is required")

    by_order_id: dict[str, BrokerOrderObservation] = {}
    for order in snapshot.orders:
        if order.plan_id != plan_id:
            continue
        previous = by_order_id.get(order.order_id)
        if previous is None:
            by_order_id[order.order_id] = order
            continue
        identity = (
            order.plan_id,
            order.role,
            order.side,
            order.quantity,
            order.client_order_id,
        )
        prior_identity = (
            previous.plan_id,
            previous.role,
            previous.side,
            previous.quantity,
            previous.client_order_id,
        )
        if identity != prior_identity:
            raise ValueError("duplicate broker order changed immutable identity")
        if order.filled_quantity < previous.filled_quantity:
            raise ValueError("cumulative fill decreased")
        by_order_id[order.order_id] = order

    entry_orders = [item for item in by_order_id.values() if item.role == "ENTRY"]
    if len(entry_orders) > 1:
        raise ValueError("more than one logical ENTRY order exists")
    bought = sum((item.filled_quantity for item in entry_orders), Decimal("0"))
    sold = sum(
        (
            item.filled_quantity
            for item in by_order_id.values()
            if item.role in _EXIT_ROLES
        ),
        Decimal("0"),
    )
    remaining = bought - sold
    if remaining < 0:
        raise ValueError("confirmed SELL quantity exceeds confirmed BUY quantity")
    return remaining


class IntradayLiveRuntime:
    """Fenced intraday lifecycle used only with injected adapters in this release.

    The production CLI intentionally does not construct this class. Tests inject a
    snapshot reader, a fake clock and adapters backed by a fake transport.
    """

    def __init__(
        self,
        *,
        store: Any,
        plan: IntradayPlan,
        account_key: str,
        writer_id: str,
        current_boot_id_hash: str,
        snapshot_reader: Callable[[IntradayPlan], BrokerSnapshot],
        stream_barrier: Callable[[], object],
        order_adapter: Any,
        conditional_adapter: Any,
        personal_topic: str,
        clock: Callable[[], datetime] | None = None,
        max_stream_age_seconds: int = 2,
        max_spread_fraction: Decimal = Decimal("0.005"),
    ) -> None:
        if not isinstance(plan, IntradayPlan):
            raise TypeError("plan must be an IntradayPlan")
        for name, value in (("account_key", account_key), ("writer_id", writer_id)):
            if not isinstance(value, str) or not value or any(char.isspace() for char in value):
                raise ValueError(f"{name} is invalid")
        if not isinstance(current_boot_id_hash, str) or not re.fullmatch(
            r"[0-9a-f]{64}", current_boot_id_hash
        ):
            raise ValueError("current_boot_id_hash is invalid")
        if account_key != plan.account_id:
            raise ValueError("account_key does not match the immutable plan")
        if not callable(snapshot_reader):
            raise TypeError("snapshot_reader must be callable")
        if not callable(stream_barrier):
            raise TypeError("stream_barrier must be callable")
        if not isinstance(personal_topic, str) or not personal_topic.startswith(
            "personal:order:"
        ):
            raise ValueError("personal_topic is invalid")
        if (
            isinstance(max_stream_age_seconds, bool)
            or not isinstance(max_stream_age_seconds, int)
            or max_stream_age_seconds < 1
        ):
            raise ValueError("max_stream_age_seconds must be positive")
        spread = _finite_decimal(
            max_spread_fraction, "max_spread_fraction", positive=True
        )
        if spread >= 1:
            raise ValueError("max_spread_fraction must be below one")
        if getattr(order_adapter, "confirm_high_value_order", False) is not False:
            raise ValueError("intraday runtime requires confirmHighValueOrder=false")

        self.store = store
        self.plan = plan
        self.account_key = account_key
        self.writer_id = writer_id
        self.current_boot_id_hash = current_boot_id_hash
        self.snapshot_reader = snapshot_reader
        self.stream_barrier = stream_barrier
        self.order_adapter = order_adapter
        self.conditional_adapter = conditional_adapter
        self.personal_topic = personal_topic
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_stream_age = timedelta(seconds=max_stream_age_seconds)
        self.max_spread_fraction = spread

        self.writer_fence: int | None = None
        self._recovered = False
        self._dirty = True
        self._stream_ack = False
        self._last_trade: Decimal | None = None
        self._previous_trade: Decimal | None = None
        self._trade_at: datetime | None = None
        self._bid: Decimal | None = None
        self._ask: Decimal | None = None
        self._book_at: datetime | None = None
        self._last_frame_at: datetime | None = None
        self._crossing = False
        self._last_snapshot: BrokerSnapshot | None = None
        self._last_clock_value: datetime | None = None
        self._seen_stream_ack_ids: set[str] = set()

    def recover(self) -> None:
        """Acquire a writer fence and reconcile from a stable A/B snapshot."""

        now = self._now()
        self._verify_stored_plan()
        run = self.store.load_intraday_run(self.plan.plan_id)
        if run is None:
            raise IntradayRuntimeError("run_missing")
        if isinstance(run.get("updated_at"), datetime) and now < _utc_datetime(
            run["updated_at"], "updated_at"
        ):
            raise IntradayRuntimeError("clock_moved_backwards")
        if self.writer_fence is None:
            claimed = self.store.claim_intraday_writer(
                plan_id=self.plan.plan_id,
                writer_id=self.writer_id,
                now=now,
                lease_seconds=45,
            )
        else:
            claimed = self.store.renew_intraday_writer(
                plan_id=self.plan.plan_id,
                writer_id=self.writer_id,
                writer_fence=self.writer_fence,
                now=now,
                lease_seconds=45,
            )
            if claimed is None:
                claimed = self.store.claim_intraday_writer(
                    plan_id=self.plan.plan_id,
                    writer_id=self.writer_id,
                    now=now,
                    lease_seconds=45,
                )
        if claimed is None:
            raise IntradayRuntimeError("writer_lease_unavailable")
        self.writer_fence = int(claimed["writer_fence"])
        self._stream_ack = False
        run = claimed

        state = str(run["state"])
        if state in _TERMINAL_RUN_STATES:
            self._recovered = True
            return
        if state == "PLANNED":
            self._recovered = False
            return
        if state not in _ENTRY_STATES:
            raise IntradayRuntimeError("run_state_invalid")
        if state != "RECONCILING":
            run = self._cas(
                run,
                "RECONCILING",
                event_type="recovery_started",
                payload={"from_state": state},
            )

        try:
            snapshot = self._stable_snapshot()
            next_state, updates, reason = self._classify_snapshot(snapshot, run)
        except (IntradayRuntimeError, TypeError, ValueError):
            self._cas(
                run,
                "RECOVERY_REQUIRED",
                event_type="reconciliation_failed",
                reason_code="broker_snapshot_invalid",
            )
            self._recovered = True
            raise IntradayRuntimeError("broker_snapshot_invalid")

        synced = self.store.mark_intraday_broker_synced(
            plan_id=self.plan.plan_id,
            writer_id=self.writer_id,
            writer_fence=self._fence(),
            now=now,
            observed_at=snapshot.captured_at,
        )
        if synced is None:
            raise IntradayRuntimeError("writer_fenced")
        run = synced
        self._cas(
            run,
            next_state,
            event_type="reconciled",
            reason_code=reason,
            updates=updates,
            payload={"classification": next_state},
        )
        self._last_snapshot = snapshot
        self._dirty = False
        self._recovered = True

    def on_stream_frame(self, frame: object) -> None:
        """Validate a frame and update only cumulative local observations."""

        if not isinstance(frame, Mapping):
            self._invalidate_stream()
            raise IntradayRuntimeError("stream_frame_invalid")
        frame_type = frame.get("type")
        if frame_type == "ack":
            if set(frame) != {"type", "request_id", "subscribed", "rejected"}:
                self._invalidate_stream()
                raise IntradayRuntimeError("stream_ack_invalid")
            expected = {
                f"trade:us:{self.plan.symbol}",
                f"orderbook:us:{self.plan.symbol}",
                self.personal_topic,
            }
            subscribed = frame.get("subscribed")
            rejected = frame.get("rejected")
            if (
                not isinstance(frame.get("request_id"), str)
                or not frame["request_id"]
                or frame["request_id"] in self._seen_stream_ack_ids
                or not isinstance(subscribed, Sequence)
                or isinstance(subscribed, (str, bytes))
                or len(subscribed) != len(expected)
                or set(subscribed) != expected
                or rejected != []
            ):
                self._invalidate_stream()
                raise IntradayRuntimeError("stream_ack_invalid")
            self._seen_stream_ack_ids.add(frame["request_id"])
            self._stream_ack = True
            return

        if not self._recovered:
            self._invalidate_stream()
            raise IntradayRuntimeError("stream_before_recovery")

        if frame_type == "trade":
            expected_keys = {"type", "topic", "symbol", "price", "captured_at"}
            expected_topic = f"trade:us:{self.plan.symbol}"
        elif frame_type == "orderbook":
            expected_keys = {
                "type",
                "topic",
                "symbol",
                "bid",
                "ask",
                "captured_at",
            }
            expected_topic = f"orderbook:us:{self.plan.symbol}"
        elif frame_type == "personal":
            expected_keys = {"type", "topic", "captured_at", "payload"}
            expected_topic = self.personal_topic
        else:
            self._invalidate_stream()
            raise IntradayRuntimeError("stream_topic_invalid")

        if set(frame) != expected_keys or frame.get("topic") != expected_topic:
            self._invalidate_stream()
            raise IntradayRuntimeError("stream_frame_invalid")
        if frame_type != "personal" and frame.get("symbol") != self.plan.symbol:
            self._invalidate_stream()
            raise IntradayRuntimeError("stream_symbol_mismatch")
        captured = _frame_datetime(frame.get("captured_at"))
        now = self._now()
        if captured > now + timedelta(seconds=2) or (
            self._last_frame_at is not None and captured < self._last_frame_at
        ):
            self._invalidate_stream()
            raise IntradayRuntimeError("stream_time_invalid")
        self._last_frame_at = captured

        if frame_type == "trade":
            price = _finite_decimal(frame.get("price"), "trade price", positive=True)
            self._previous_trade = self._last_trade
            self._last_trade = price
            self._trade_at = captured
            self._crossing = (
                self._previous_trade is not None
                and self._previous_trade < self.plan.entry_trigger <= price
            )
        elif frame_type == "orderbook":
            bid = _finite_decimal(frame.get("bid"), "bid", positive=True)
            ask = _finite_decimal(frame.get("ask"), "ask", positive=True)
            if ask < bid:
                self._invalidate_stream()
                raise IntradayRuntimeError("stream_book_invalid")
            self._bid = bid
            self._ask = ask
            self._book_at = captured
        else:
            if not isinstance(frame.get("payload"), Mapping):
                self._invalidate_stream()
                raise IntradayRuntimeError("stream_personal_invalid")
            self._dirty = True

    def tick(self) -> None:
        """Perform at most one reserved broker mutation for the current state."""

        if not self._recovered:
            self.recover()
            return
        run = self.store.load_intraday_run(self.plan.plan_id)
        if run is None:
            raise IntradayRuntimeError("run_missing")
        state = str(run["state"])
        if state in _TERMINAL_RUN_STATES or state == "PLANNED":
            return
        self._renew_if_needed(run)

        if self._dirty or state in {
            "ENTRY_UNKNOWN",
            "ENTRY_SUBMITTING",
            "ENTRY_WORKING",
            "ENTRY_CANCELING",
            "PROTECTION_SUBMITTING",
            "PROTECTION_UNKNOWN",
            "PROTECTED",
            "EXIT_CANCELING_PROTECTION",
            "EXIT_SUBMITTING",
            "EXIT_UNKNOWN",
            "EXIT_WORKING",
        }:
            self._reconcile_tick(run)
            return

        if state == "READY_TO_ENTER":
            self._tick_ready(run)
        elif state == "OPEN_UNPROTECTED":
            self._tick_open_unprotected(run)

    def _reconcile_tick(self, run: Mapping[str, object]) -> None:
        state = str(run["state"])
        snapshot = self._stable_snapshot()
        next_state, updates, reason = self._classify_snapshot(snapshot, run)
        synced = self.store.mark_intraday_broker_synced(
            plan_id=self.plan.plan_id,
            writer_id=self.writer_id,
            writer_fence=self._fence(),
            now=self._now(),
            observed_at=snapshot.captured_at,
        )
        if synced is None:
            raise IntradayRuntimeError("writer_fenced")
        self._last_snapshot = snapshot
        self._dirty = False

        if self._persist_broker_observation_if_changed(
            synced,
            snapshot=snapshot,
            next_state=next_state,
            updates=updates,
            reason_code=reason,
        ):
            return

        if state == "ENTRY_CANCELING":
            entry = self._entry_order(snapshot)
            if entry is not None and entry.status in _GENERAL_OPEN | {"UNKNOWN"}:
                return

        if (
            state == "PROTECTED"
            and next_state == "PROTECTED"
            and self._now() >= self.plan.force_exit_at
        ):
            known = self._single_known_oco(snapshot, synced)
            if known is None or known.status != "WATCHING":
                self._cas(
                    synced,
                    "RECOVERY_REQUIRED",
                    event_type="force_exit_oco_ambiguous",
                    reason_code="oco_ambiguous",
                )
                return
            self._cancel_protection(synced, known)
            return
        if state == "EXIT_CANCELING_PROTECTION":
            triggered = self._triggered_order(snapshot)
            if triggered is not None:
                if next_state == "EXIT_WORKING":
                    self._cas(
                        synced,
                        "EXIT_WORKING",
                        event_type="triggered_exit_observed",
                        updates=updates,
                        payload={"order_id": triggered.order_id},
                    )
                else:
                    self._cas(
                        synced,
                        "RECOVERY_REQUIRED",
                        event_type="triggered_exit_ambiguous",
                        reason_code=reason or "triggered_exit_ambiguous",
                        updates=updates,
                    )
                return
            if snapshot.conditional_orders:
                known = self._single_known_oco(snapshot, synced)
                if (
                    known is not None
                    and known.status in {"WATCHING", "PAUSED"}
                    and not self._has_event("conditional_cancel_send_reserved")
                ):
                    self._cancel_protection(synced, known)
                elif known is None or known.status not in {"WATCHING", "PAUSED"}:
                    self._cas(
                        synced,
                        "RECOVERY_REQUIRED",
                        event_type="conditional_cancel_competition_ambiguous",
                        reason_code=reason or "conditional_cancel_competition_ambiguous",
                        updates=updates,
                    )
                return
            if not self._has_event("conditional_cancel_acknowledged"):
                self._cas(
                    synced,
                    "RECOVERY_REQUIRED",
                    event_type="conditional_cancel_unconfirmed",
                    reason_code="conditional_cancel_unknown",
                )
                return
            owned = remaining_owned_quantity(snapshot, self.plan.plan_id)
            if owned == 0:
                self._cas(
                    synced,
                    "CLOSED",
                    event_type="closed_after_protection_cancel",
                    updates={"owned_qty": "0", "protected_qty": "0"},
                )
                return
            if snapshot.holding_quantity != owned or snapshot.sellable_quantity != owned:
                self._cas(
                    synced,
                    "RECOVERY_REQUIRED",
                    event_type="exit_ownership_mismatch",
                    reason_code="sellable_mismatch",
                )
                return
            if not snapshot.market_open:
                return
            self._reserve_and_send_exit(synced, owned, role="FORCE_EXIT")
            return
        if state == "ENTRY_WORKING":
            entry = self._entry_order(snapshot)
            if entry is not None and (
                entry.filled_quantity > 0
                or self._now() >= self.plan.entry_expiry
            ) and entry.status in _GENERAL_OPEN:
                self._cancel_entry(synced, entry)
                return

        if (
            state in {"PROTECTED", "PROTECTION_SUBMITTING", "PROTECTION_UNKNOWN"}
            and next_state == "OPEN_UNPROTECTED"
            and reason == "conditional_expired"
        ):
            opened = self._cas(
                synced,
                "OPEN_UNPROTECTED",
                event_type="conditional_expired_observed",
                reason_code=reason,
                updates=updates,
            )
            self._tick_open_unprotected(opened)
            return

        if state in {"ENTRY_UNKNOWN", "EXIT_UNKNOWN"} and next_state == state:
            self._attempt_identity_recovery(synced, snapshot)
            return

        updates = _changed_run_updates(run, updates)
        if next_state != state or updates:
            self._cas(
                synced,
                next_state,
                event_type="broker_projection_updated",
                reason_code=reason,
                updates=updates,
                payload={"from_state": state, "to_state": next_state},
            )

    def _tick_ready(self, run: Mapping[str, object]) -> None:
        now = self._now()
        if not self._approval_valid(run):
            self._cas(
                run,
                "RECONCILING",
                event_type="approval_invalidated",
                reason_code="approval_invalid",
            )
            self._recovered = False
            return
        if now >= self.plan.entry_expiry:
            self._cas(
                run,
                "SKIPPED",
                event_type="entry_window_expired",
                reason_code="entry_window_expired",
                updates={"owned_qty": "0", "protected_qty": "0"},
            )
            return
        if run.get("entry_disabled_at") is not None or run.get("loss_fuse_at") is not None:
            self._cas(
                run,
                "SKIPPED",
                event_type="entry_latch_blocked",
                reason_code="entry_disabled",
                updates={"owned_qty": "0", "protected_qty": "0"},
            )
            return
        if now < self.plan.entry_start or not self._entry_stream_gate(now):
            return
        snapshot = self._stable_snapshot()
        synced = self.store.mark_intraday_broker_synced(
            plan_id=self.plan.plan_id,
            writer_id=self.writer_id,
            writer_fence=self._fence(),
            now=now,
            observed_at=snapshot.captured_at,
        )
        if synced is None:
            raise IntradayRuntimeError("writer_fenced")
        run = synced
        self._last_snapshot = snapshot
        if snapshot is None or not self._entry_snapshot_gate(snapshot):
            return
        body = self._entry_body()
        reservation = self.store.reserve_intraday_order_intent(
            plan_id=self.plan.plan_id,
            account_key=self.account_key,
            intent_id=f"{self.plan.plan_id}:entry",
            idempotency_key=self.plan.entry_client_order_id,
            order_role="ENTRY",
            method="POST",
            path=_GENERAL_ORDER_PATH,
            body=body,
            symbol=self.plan.symbol,
            side="BUY",
            quantity=str(self.plan.quantity),
            order_type="LIMIT",
            limit_price=str(self.plan.entry_limit),
            expected_state="READY_TO_ENTER",
            expected_version=int(run["version"]),
            next_state="ENTRY_SUBMITTING",
            writer_id=self.writer_id,
            writer_fence=self._fence(),
            send_by=min(self.plan.entry_expiry, now + timedelta(seconds=5)),
            now=now,
        )
        if not reservation["inserted"]:
            raise IntradayRuntimeError("entry_already_reserved")
        self._send_general(reservation, role="ENTRY")
        self._crossing = False

    def _tick_open_unprotected(self, run: Mapping[str, object]) -> None:
        snapshot = self._stable_snapshot()
        synced = self.store.mark_intraday_broker_synced(
            plan_id=self.plan.plan_id,
            writer_id=self.writer_id,
            writer_fence=self._fence(),
            now=self._now(),
            observed_at=snapshot.captured_at,
        )
        if synced is None:
            raise IntradayRuntimeError("writer_fenced")
        run = synced
        self._last_snapshot = snapshot
        identity_error = self._snapshot_order_identity_error(snapshot, run)
        if identity_error is not None:
            self._cas(
                run,
                "RECOVERY_REQUIRED",
                event_type="protection_order_identity_mismatch",
                reason_code=identity_error,
            )
            return
        owned = remaining_owned_quantity(snapshot, self.plan.plan_id)
        expired = self._single_known_oco(snapshot, run)
        if expired is not None and expired.status == "EXPIRED":
            if (
                not self._expired_oco_matches(expired, owned)
                or snapshot.symbol != self.plan.symbol
                or snapshot.foreign_activity
                or owned <= 0
                or snapshot.holding_quantity != owned
                or any(
                    order.side == "SELL"
                    and order.status in _GENERAL_OPEN | {"UNKNOWN"}
                    for order in snapshot.orders
                )
            ):
                self._cas(
                    run,
                    "RECOVERY_REQUIRED",
                    event_type="expired_protection_exit_blocked",
                    reason_code="conditional_expired_unprotected",
                )
                return
            if (
                snapshot.sellable_quantity != owned
                or not snapshot.market_open
                or snapshot.halt_state != "CLEAR"
            ):
                return
            self._reserve_and_send_exit(run, owned, role="EMERGENCY_EXIT")
            return
        if (
            owned <= 0
            or snapshot.holding_quantity != owned
            or snapshot.conditional_orders
            or any(order.side == "SELL" and order.status in _GENERAL_OPEN for order in snapshot.orders)
        ):
            self._cas(
                run,
                "RECOVERY_REQUIRED",
                event_type="protection_ownership_mismatch",
                reason_code="ownership_mismatch",
            )
            return
        if run.get("protection_intent_id") is not None:
            protection = self.store.load_execution_order(
                str(run["protection_intent_id"])
            )
            if protection is None or protection["status"] != "REJECTED":
                self._cas(
                    run,
                    "RECOVERY_REQUIRED",
                    event_type="protection_identity_ambiguous",
                    reason_code="protection_identity_ambiguous",
                )
                return
            if not snapshot.market_open or snapshot.sellable_quantity != owned:
                return
            self._reserve_and_send_exit(run, owned, role="EMERGENCY_EXIT")
            return
        if not snapshot.market_open:
            return
        body = self._protection_body(owned)
        now = self._now()
        reservation = self.store.reserve_intraday_order_intent(
            plan_id=self.plan.plan_id,
            account_key=self.account_key,
            intent_id=f"{self.plan.plan_id}:protection",
            idempotency_key=self.plan.oco_client_order_id,
            order_role="PROTECTION",
            method="POST",
            path=_CONDITIONAL_ORDER_PATH,
            body=body,
            symbol=self.plan.symbol,
            side="SELL",
            quantity=str(owned),
            order_type="LIMIT",
            limit_price=None,
            expected_state="OPEN_UNPROTECTED",
            expected_version=int(run["version"]),
            next_state="PROTECTION_SUBMITTING",
            writer_id=self.writer_id,
            writer_fence=self._fence(),
            send_by=now + timedelta(seconds=5),
            now=now,
        )
        if not reservation["inserted"]:
            raise IntradayRuntimeError("protection_already_reserved")
        self._send_conditional(reservation)

    def _send_general(self, reservation: Mapping[str, object], *, role: str) -> None:
        run = _reservation_run(reservation)
        intent_row = _reservation_intent(reservation)
        intent = self._general_intent(intent_row, role)
        if not self._reservation_sendable(run, intent_row):
            raise IntradayRuntimeError("reservation_not_sendable")
        serializer = getattr(self.order_adapter, "serialize_order", None)
        if callable(serializer) and serializer(intent) != self._verified_request_body(
            intent_row
        ):
            raise IntradayRuntimeError("adapter_request_projection_mismatch")
        try:
            ticket = self.order_adapter.place_order(intent)
            if not isinstance(ticket, BrokerOrderTicket) or not ticket.broker_order_id:
                raise LiveBrokerError("order response ID missing", unknown_state=True)
        except LiveBrokerError as exc:
            if exc.code == "idempotency-key-conflict":
                self._complete_order(
                    run,
                    intent_row,
                    next_state="RECOVERY_REQUIRED",
                    event_type="identity_conflict",
                    status="UNKNOWN",
                    reason_code="identity_conflict",
                )
                return
            if exc.unknown_state:
                self._complete_order(
                    run,
                    intent_row,
                    next_state="ENTRY_UNKNOWN" if role == "ENTRY" else "EXIT_UNKNOWN",
                    event_type="create_outcome_unknown",
                    status="UNKNOWN",
                )
                return
            self._complete_order(
                run,
                intent_row,
                next_state="CANCELLED" if role == "ENTRY" else "RECOVERY_REQUIRED",
                event_type="create_rejected",
                status="REJECTED",
                reason_code="broker_rejected",
                updates={"owned_qty": "0", "protected_qty": "0"}
                if role == "ENTRY"
                else None,
            )
            return
        self._complete_order(
            run,
            intent_row,
            next_state="ENTRY_WORKING" if role == "ENTRY" else "EXIT_WORKING",
            event_type="create_acknowledged",
            status=ticket.status.value,
            broker_order_id=ticket.broker_order_id,
        )

    def _attempt_identity_recovery(
        self, run: Mapping[str, object], snapshot: BrokerSnapshot
    ) -> None:
        state = str(run["state"])
        role = "ENTRY" if state == "ENTRY_UNKNOWN" else "FORCE_EXIT"
        pointer = "entry_intent_id" if state == "ENTRY_UNKNOWN" else "active_exit_intent_id"
        intent_id = run.get(pointer)
        if not isinstance(intent_id, str):
            self._cas(
                run,
                "RECOVERY_REQUIRED",
                event_type="identity_recovery_missing_intent",
                reason_code="identity_missing",
            )
            return
        intent_row = self.store.load_intraday_order_intent(intent_id)
        if intent_row is None:
            self._cas(
                run,
                "RECOVERY_REQUIRED",
                event_type="identity_recovery_missing_intent",
                reason_code="identity_missing",
            )
            return
        execution = self.store.load_execution_order(intent_id)
        if execution is None:
            self._cas(
                run,
                "RECOVERY_REQUIRED",
                event_type="identity_recovery_missing_execution",
                reason_code="identity_missing",
            )
            return
        role = str(intent_row["order_role"])
        deadline = intent_row.get("recovery_deadline_at")
        if not isinstance(deadline, datetime) or self._now() > _utc_datetime(
            deadline, "recovery_deadline_at"
        ):
            self._cas(
                run,
                "RECOVERY_REQUIRED",
                event_type="identity_recovery_expired",
                reason_code="identity_recovery_expired",
            )
            return
        if execution.get("broker_order_id") is not None:
            return
        if self._has_intent_event(intent_id, "identity_recovery_send_reserved"):
            self._cas(
                run,
                "RECOVERY_REQUIRED",
                event_type="identity_recovery_exhausted",
                reason_code="identity_recovery_exhausted",
            )
            return
        if role == "ENTRY":
            recovery_now = self._now()
            if recovery_now >= self.plan.entry_expiry:
                self._cas(
                    run,
                    "RECOVERY_REQUIRED",
                    event_type="identity_recovery_entry_window_expired",
                    reason_code="entry_window_expired",
                )
                return
            if not self._approval_valid(run) or not self._entry_recovery_gate(
                snapshot, recovery_now
            ):
                return
        elif role in {"FORCE_EXIT", "EMERGENCY_EXIT"}:
            owned = remaining_owned_quantity(snapshot, self.plan.plan_id)
            body = _request_body(intent_row)
            request_quantity = Decimal(str(body.get("quantity")))
            known_conditional = self._single_known_oco(snapshot, run)
            safe_expired_parent = (
                len(snapshot.conditional_orders) == 1
                and known_conditional is not None
                and known_conditional.status == "EXPIRED"
                and self._expired_oco_matches(known_conditional, owned)
            )
            if (
                snapshot.symbol != self.plan.symbol
                or snapshot.foreign_activity
                or not snapshot.market_open
                or snapshot.halt_state != "CLEAR"
                or (snapshot.conditional_orders and not safe_expired_parent)
                or owned <= 0
                or snapshot.holding_quantity != owned
                or snapshot.sellable_quantity != owned
                or request_quantity != owned
                or any(
                    order.side == "SELL"
                    and order.status in _GENERAL_OPEN | {"UNKNOWN"}
                    for order in snapshot.orders
                )
            ):
                return
        else:
            self._cas(
                run,
                "RECOVERY_REQUIRED",
                event_type="identity_recovery_role_invalid",
                reason_code="identity_role_invalid",
            )
            return

        reserved = self.store.reserve_intraday_action_event(
            plan_id=self.plan.plan_id,
            intent_id=intent_id,
            event_type="identity_recovery_send_reserved",
            expected_state=state,
            expected_version=int(run["version"]),
            next_state=state,
            writer_id=self.writer_id,
            writer_fence=self._fence(),
            payload={"request_hash": intent_row["request_hash"]},
            now=self._now(),
        )
        if reserved is None:
            raise IntradayRuntimeError("identity_recovery_not_reserved")
        recovery_intent = self._general_intent(intent_row, role)
        if not self.store.intraday_action_is_sendable(
            plan_id=self.plan.plan_id,
            intent_id=intent_id,
            event_type="identity_recovery_send_reserved",
            expected_state=state,
            expected_run_version=int(reserved["version"]),
            writer_id=self.writer_id,
            writer_fence=self._fence(),
            request_hash=str(intent_row["request_hash"]),
            now=self._now(),
            min_lease_seconds=30,
        ):
            raise IntradayRuntimeError("identity_recovery_not_sendable")
        self._send_general_recovery(
            reserved, intent_row, recovery_intent, role=role
        )

    def _send_general_recovery(
        self,
        run: Mapping[str, object],
        intent_row: Mapping[str, object],
        prepared_intent: OrderIntent,
        *,
        role: str,
    ) -> None:
        serializer = getattr(self.order_adapter, "serialize_order", None)
        if callable(serializer) and serializer(
            prepared_intent
        ) != self._verified_request_body(intent_row):
            raise IntradayRuntimeError("adapter_request_projection_mismatch")
        try:
            ticket = self.order_adapter.place_order(prepared_intent)
            if not isinstance(ticket, BrokerOrderTicket) or not ticket.broker_order_id:
                raise LiveBrokerError("order response ID missing", unknown_state=True)
        except LiveBrokerError as exc:
            if exc.code == "idempotency-key-conflict":
                self._complete_order(
                    run,
                    intent_row,
                    next_state="RECOVERY_REQUIRED",
                    event_type="identity_conflict",
                    status="UNKNOWN",
                    reason_code="identity_conflict",
                )
            elif exc.unknown_state:
                self._complete_order(
                    run,
                    intent_row,
                    next_state=str(run["state"]),
                    event_type="identity_recovery_unknown",
                    status="UNKNOWN",
                )
            else:
                self._complete_order(
                    run,
                    intent_row,
                    next_state="CANCELLED" if role == "ENTRY" else "RECOVERY_REQUIRED",
                    event_type="identity_recovery_rejected",
                    status="REJECTED",
                    reason_code="broker_rejected",
                    updates={"owned_qty": "0", "protected_qty": "0"}
                    if role == "ENTRY"
                    else None,
                )
            return
        self._complete_order(
            run,
            intent_row,
            next_state="ENTRY_WORKING" if role == "ENTRY" else "EXIT_WORKING",
            event_type="identity_recovery_acknowledged",
            status=ticket.status.value,
            broker_order_id=ticket.broker_order_id,
        )

    def _general_intent(
        self, intent_row: Mapping[str, object], role: str
    ) -> OrderIntent:
        body = self._verified_request_body(intent_row)
        expected_client_id = str(intent_row["idempotency_key"])
        if body.get("clientOrderId") != expected_client_id or not _CLIENT_ORDER_ID.fullmatch(
            expected_client_id
        ):
            raise IntradayRuntimeError("client_order_identity_invalid")
        expected = (
            self._entry_body()
            if role == "ENTRY"
            else self._exit_body(Decimal(str(intent_row["quantity"])), role)
        )
        if body != _canonical_value(expected):
            raise IntradayRuntimeError("reservation_request_projection_mismatch")
        return OrderIntent(
            intent_id=str(intent_row["intent_id"]),
            idempotency_key=expected_client_id,
            symbol=str(body["symbol"]),
            side=Side.BUY if role == "ENTRY" else Side.SELL,
            quantity=Decimal(str(body["quantity"])),
            order_type=OrderType(str(body["orderType"])),
            limit_price=(Decimal(str(body["price"])) if "price" in body else None),
            source="intraday_live",
            reason=role.lower(),
            created_at=self._now(),
        )

    def _send_conditional(self, reservation: Mapping[str, object]) -> None:
        run = _reservation_run(reservation)
        intent_row = _reservation_intent(reservation)
        body = self._verified_request_body(intent_row)
        if body != _canonical_value(
            self._protection_body(Decimal(str(intent_row["quantity"])))
        ):
            raise IntradayRuntimeError("reservation_request_projection_mismatch")
        if not self._reservation_sendable(run, intent_row):
            raise IntradayRuntimeError("reservation_not_sendable")
        try:
            result = self.conditional_adapter.create(
                body, current_price=self._last_trade or self.plan.entry_limit
            )
            conditional_id = result.get("conditionalOrderId") if isinstance(result, Mapping) else None
            if not isinstance(conditional_id, str) or not conditional_id:
                raise ConditionalOrderUnknownStateError("create", "ID missing")
        except ConditionalOrderUnknownStateError:
            self._complete_order(
                run,
                intent_row,
                next_state="PROTECTION_UNKNOWN",
                event_type="conditional_create_unknown",
                status="UNKNOWN",
            )
            return
        except Exception:
            self._complete_order(
                run,
                intent_row,
                next_state="RECONCILING",
                event_type="conditional_create_rejected",
                status="REJECTED",
                reason_code="protection_rejected",
            )
            self._recovered = False
            return
        self._complete_order(
            run,
            intent_row,
            next_state="PROTECTION_SUBMITTING",
            event_type="conditional_create_acknowledged",
            status="ACKNOWLEDGED",
            broker_order_id=conditional_id,
        )

    def _cancel_entry(
        self, run: Mapping[str, object], entry: BrokerOrderObservation
    ) -> None:
        action_hash = _action_request_hash("CANCEL", entry.order_id)
        reserved = self.store.reserve_intraday_action_event(
            plan_id=self.plan.plan_id,
            intent_id=f"{self.plan.plan_id}:entry",
            event_type="entry_cancel_send_reserved",
            expected_state=str(run["state"]),
            expected_version=int(run["version"]),
            next_state="ENTRY_CANCELING",
            writer_id=self.writer_id,
            writer_fence=self._fence(),
            payload={
                "operation": "CANCEL",
                "root_order_id": entry.order_id,
                "request_hash": action_hash,
            },
            now=self._now(),
        )
        if reserved is None:
            raise IntradayRuntimeError("entry_cancel_already_reserved")
        if not self.store.intraday_action_is_sendable(
            plan_id=self.plan.plan_id,
            intent_id=f"{self.plan.plan_id}:entry",
            event_type="entry_cancel_send_reserved",
            expected_state="ENTRY_CANCELING",
            expected_run_version=int(reserved["version"]),
            writer_id=self.writer_id,
            writer_fence=self._fence(),
            request_hash=action_hash,
            now=self._now(),
            min_lease_seconds=30,
        ):
            raise IntradayRuntimeError("entry_cancel_not_sendable")
        try:
            ticket = self.order_adapter.cancel_order(entry.order_id)
            if not isinstance(ticket, BrokerOrderTicket) or not ticket.broker_order_id:
                raise LiveBrokerError("cancel response ID missing", unknown_state=True)
        except LiveBrokerError as exc:
            if exc.unknown_state:
                return
            self._cas(
                reserved,
                "RECOVERY_REQUIRED",
                event_type="entry_cancel_rejected",
                reason_code="entry_cancel_rejected",
            )
            return
        except Exception:
            self._cas(
                reserved,
                "RECOVERY_REQUIRED",
                event_type="entry_cancel_rejected",
                reason_code="entry_cancel_rejected",
            )
            return
        recorded = self.store.append_intraday_observation_event(
            plan_id=self.plan.plan_id,
            intent_id=f"{self.plan.plan_id}:entry",
            event_type="entry_cancel_acknowledged",
            status=ticket.status.value,
            writer_id=self.writer_id,
            writer_fence=self._fence(),
            payload={
                "root_order_id": entry.order_id,
                "operation": "CANCEL",
                "operation_order_id": ticket.broker_order_id,
            },
            now=self._now(),
        )
        if not recorded:
            raise IntradayRuntimeError("stale_order_response")

    def _cancel_protection(
        self, run: Mapping[str, object], order: ConditionalOrderObservation
    ) -> None:
        action_hash = _action_request_hash("DELETE", order.conditional_order_id)
        reserved = self.store.reserve_intraday_action_event(
            plan_id=self.plan.plan_id,
            intent_id=f"{self.plan.plan_id}:protection",
            event_type="conditional_cancel_send_reserved",
            expected_state=str(run["state"]),
            expected_version=int(run["version"]),
            next_state="EXIT_CANCELING_PROTECTION",
            writer_id=self.writer_id,
            writer_fence=self._fence(),
            payload={
                "operation": "DELETE",
                "root_order_id": order.conditional_order_id,
                "request_hash": action_hash,
            },
            now=self._now(),
        )
        if reserved is None:
            raise IntradayRuntimeError("conditional_cancel_already_reserved")
        if not self.store.intraday_action_is_sendable(
            plan_id=self.plan.plan_id,
            intent_id=f"{self.plan.plan_id}:protection",
            event_type="conditional_cancel_send_reserved",
            expected_state="EXIT_CANCELING_PROTECTION",
            expected_run_version=int(reserved["version"]),
            writer_id=self.writer_id,
            writer_fence=self._fence(),
            request_hash=action_hash,
            now=self._now(),
            min_lease_seconds=30,
        ):
            raise IntradayRuntimeError("conditional_cancel_not_sendable")
        try:
            self.conditional_adapter.delete(order.conditional_order_id)
        except ConditionalOrderUnknownStateError:
            return
        except Exception:
            self._cas(
                reserved,
                "RECOVERY_REQUIRED",
                event_type="conditional_cancel_rejected",
                reason_code="conditional_cancel_rejected",
            )
            return
        recorded = self.store.append_intraday_observation_event(
            plan_id=self.plan.plan_id,
            intent_id=f"{self.plan.plan_id}:protection",
            event_type="conditional_cancel_acknowledged",
            status="ACKNOWLEDGED",
            writer_id=self.writer_id,
            writer_fence=self._fence(),
            payload={"root_order_id": order.conditional_order_id, "operation": "DELETE"},
            now=self._now(),
        )
        if not recorded:
            raise IntradayRuntimeError("stale_order_response")

    def _reserve_and_send_exit(
        self, run: Mapping[str, object], quantity: Decimal, *, role: str
    ) -> None:
        now = self._now()
        body = self._exit_body(quantity, role)
        client_order_id = _derived_client_order_id(self.plan.plan_id, role)
        reservation = self.store.reserve_intraday_order_intent(
            plan_id=self.plan.plan_id,
            account_key=self.account_key,
            intent_id=f"{self.plan.plan_id}:{role.lower()}",
            idempotency_key=client_order_id,
            order_role=role,
            method="POST",
            path=_GENERAL_ORDER_PATH,
            body=body,
            symbol=self.plan.symbol,
            side="SELL",
            quantity=str(quantity),
            order_type="MARKET",
            limit_price=None,
            expected_state=str(run["state"]),
            expected_version=int(run["version"]),
            next_state="EXIT_SUBMITTING",
            writer_id=self.writer_id,
            writer_fence=self._fence(),
            send_by=now + timedelta(seconds=5),
            now=now,
        )
        if not reservation["inserted"]:
            raise IntradayRuntimeError("exit_already_reserved")
        self._send_general(reservation, role=role)

    def _complete_order(
        self,
        run: Mapping[str, object],
        intent: Mapping[str, object],
        *,
        next_state: str,
        event_type: str,
        status: str,
        broker_order_id: str | None = None,
        filled_quantity: Decimal = Decimal("0"),
        remaining_quantity: Decimal | None = None,
        average_fill_price: Decimal | None = None,
        reason_code: str | None = None,
        updates: Mapping[str, object] | None = None,
    ) -> None:
        result = self.store.complete_intraday_order_action(
            plan_id=self.plan.plan_id,
            intent_id=str(intent["intent_id"]),
            expected_state=str(run["state"]),
            expected_version=int(run["version"]),
            next_state=next_state,
            writer_id=self.writer_id,
            writer_fence=self._fence(),
            event_type=event_type,
            status=status,
            broker_order_id=broker_order_id,
            filled_quantity=filled_quantity,
            remaining_quantity=remaining_quantity,
            average_fill_price=average_fill_price,
            reason_code=reason_code,
            run_updates=dict(updates or {}),
            now=self._now(),
        )
        if result is None:
            raise IntradayRuntimeError("stale_order_response")

    def _persist_broker_observation_if_changed(
        self,
        run: Mapping[str, object],
        *,
        snapshot: BrokerSnapshot,
        next_state: str,
        updates: Mapping[str, object],
        reason_code: str | None,
    ) -> bool:
        state = str(run["state"])
        if next_state == "RECOVERY_REQUIRED":
            return False
        observation: BrokerOrderObservation | None = None
        intent_id: object = None
        if state in {"ENTRY_WORKING", "ENTRY_CANCELING"}:
            observation = self._entry_order(snapshot)
            intent_id = run.get("entry_intent_id")
        elif state in {"EXIT_SUBMITTING", "EXIT_UNKNOWN", "EXIT_WORKING"}:
            local = [
                item
                for item in snapshot.orders
                if item.role in {"FORCE_EXIT", "EMERGENCY_EXIT"}
            ]
            if len({item.order_id for item in local}) > 1:
                raise IntradayRuntimeError("multiple_local_exit_orders")
            observation = local[-1] if local else None
            intent_id = run.get("active_exit_intent_id")
        if observation is None or not isinstance(intent_id, str):
            return False
        stored = self.store.load_execution_order(intent_id)
        if stored is None:
            raise IntradayRuntimeError("execution_projection_missing")
        remaining = observation.quantity - observation.filled_quantity
        changed = (
            stored.get("broker_order_id") != observation.order_id
            or stored.get("status") != observation.status
            or stored.get("filled_quantity") != observation.filled_quantity
            or stored.get("remaining_quantity") != remaining
            or stored.get("average_fill_price") != observation.average_fill_price
        )
        if not changed:
            return False
        intent_row = self.store.load_intraday_order_intent(intent_id)
        if intent_row is None:
            raise IntradayRuntimeError("intent_projection_missing")
        prepared_updates = dict(updates)
        if observation.role == "ENTRY" and observation.average_fill_price is not None:
            prepared_updates["average_entry_price"] = observation.average_fill_price
        self._complete_order(
            run,
            intent_row,
            next_state=next_state,
            event_type="broker_execution_observed",
            status=observation.status,
            broker_order_id=observation.order_id,
            filled_quantity=observation.filled_quantity,
            remaining_quantity=remaining,
            average_fill_price=observation.average_fill_price,
            reason_code=reason_code,
            updates=prepared_updates,
        )
        return True

    def _classify_snapshot(
        self, snapshot: BrokerSnapshot, run: Mapping[str, object]
    ) -> tuple[str, dict[str, object], str | None]:
        if snapshot.symbol != self.plan.symbol or snapshot.foreign_activity:
            return "RECOVERY_REQUIRED", {}, "foreign_account_activity"
        if any(order.plan_id != self.plan.plan_id for order in snapshot.orders) or any(
            order.plan_id != self.plan.plan_id for order in snapshot.conditional_orders
        ):
            return "RECOVERY_REQUIRED", {}, "foreign_account_activity"
        identity_error = self._snapshot_order_identity_error(snapshot, run)
        if identity_error is not None:
            return "RECOVERY_REQUIRED", {}, identity_error
        try:
            owned = remaining_owned_quantity(snapshot, self.plan.plan_id)
        except ValueError:
            return "RECOVERY_REQUIRED", {}, "owned_quantity_invalid"
        if snapshot.holding_quantity != owned:
            return "RECOVERY_REQUIRED", {}, "holding_mismatch"
        updates: dict[str, object] = {
            "owned_qty": _canonical_decimal(owned),
            "protected_qty": "0",
        }
        entry = self._entry_order(snapshot)
        exits = [item for item in snapshot.orders if item.role in _EXIT_ROLES]
        local_exits = [
            item for item in exits if item.role in {"FORCE_EXIT", "EMERGENCY_EXIT"}
        ]
        active_exits = [item for item in exits if item.status in _GENERAL_OPEN]
        unknown_exits = [item for item in exits if item.status == "UNKNOWN"]
        triggered = self._triggered_order(snapshot)
        if len(snapshot.conditional_orders) > 1:
            return "RECOVERY_REQUIRED", updates, "multiple_conditional_candidates"
        conditional = self._single_known_oco(snapshot, run)
        if snapshot.conditional_orders and conditional is None:
            return "RECOVERY_REQUIRED", updates, "conditional_identity_mismatch"
        if entry is not None and entry.status in _GENERAL_OPEN | {"UNKNOWN"} and (
            exits or conditional is not None
        ):
            return "RECOVERY_REQUIRED", updates, "competing_entry_exit_orders"

        triggered_id = (
            _conditional_triggered_id(conditional) if conditional is not None else None
        )
        if triggered is not None:
            if (
                conditional is None
                or triggered_id != triggered.order_id
                or not self._triggered_group_matches(conditional, triggered, run)
            ):
                return "RECOVERY_REQUIRED", updates, "triggered_order_identity_mismatch"
            updates["triggered_exit_order_id"] = triggered.order_id
        elif triggered_id is not None:
            return "RECOVERY_REQUIRED", updates, "triggered_order_missing"
        local_active_exits = [item for item in local_exits if item.status in _GENERAL_OPEN]
        if triggered is not None and local_exits:
            return "RECOVERY_REQUIRED", updates, "competing_exit_orders"
        if (
            local_exits
            and conditional is not None
            and conditional.status in _CONDITIONAL_ACTIVE
        ):
            return "RECOVERY_REQUIRED", updates, "conditional_not_cleared"

        if unknown_exits:
            if any(item.role == "TRIGGERED_EXIT" for item in unknown_exits):
                return "RECOVERY_REQUIRED", updates, "triggered_exit_unknown"
            if conditional is not None and conditional.status in _CONDITIONAL_ACTIVE:
                return "RECOVERY_REQUIRED", updates, "conditional_not_cleared"
            return "EXIT_UNKNOWN", updates, None
        if active_exits:
            return "EXIT_WORKING", updates, None
        if exits and all(item.status in _GENERAL_TERMINAL for item in exits):
            if owned == 0 and conditional is None:
                if entry is None and isinstance(run.get("entry_intent_id"), str):
                    projected_entry = self.store.load_execution_order(
                        str(run["entry_intent_id"])
                    )
                    if projected_entry is None:
                        return "RECOVERY_REQUIRED", updates, "entry_projection_missing"
                    if str(projected_entry["status"]) not in _GENERAL_TERMINAL:
                        return "RECOVERY_REQUIRED", updates, "entry_outcome_unresolved"
                return "CLOSED", updates, None
            if owned > 0:
                return "RECOVERY_REQUIRED", updates, "terminal_exit_with_position"
        if conditional is not None:
            if triggered_id is not None:
                matching = [item for item in snapshot.orders if item.order_id == triggered_id]
                if len(matching) != 1 or matching[0].role != "TRIGGERED_EXIT":
                    return "RECOVERY_REQUIRED", updates, "triggered_order_missing"
                triggered_order = matching[0]
                if triggered_order.status == "UNKNOWN":
                    return "RECOVERY_REQUIRED", updates, "triggered_exit_unknown"
                if triggered_order.status in _GENERAL_TERMINAL:
                    if owned == 0 and conditional.status in _CONDITIONAL_TERMINAL:
                        return "CLOSED", updates, None
                    return "RECOVERY_REQUIRED", updates, "terminal_exit_with_position"
                return "EXIT_WORKING", updates, None
            if conditional.status == "WATCHING" and self._oco_matches(conditional, owned):
                if self._has_event("conditional_cancel_send_reserved"):
                    return (
                        "EXIT_CANCELING_PROTECTION",
                        updates,
                        "conditional_cancel_reserved",
                    )
                updates["protected_qty"] = _canonical_decimal(owned)
                return "PROTECTED", updates, None
            if conditional.status == "WATCHING":
                return (
                    "EXIT_CANCELING_PROTECTION",
                    updates,
                    "conditional_economics_mismatch",
                )
            if conditional.status in {"ORDERING", "ORDERED"}:
                return "PROTECTION_UNKNOWN", updates, None
            if conditional.status == "PAUSED":
                return "EXIT_CANCELING_PROTECTION", updates, "conditional_paused"
            if conditional.status == "EXPIRED" and owned > 0:
                if not self._expired_oco_matches(conditional, owned):
                    return "RECOVERY_REQUIRED", updates, "conditional_expiry_ambiguous"
                if run.get("active_exit_intent_id") is not None:
                    local_exit = self.store.load_execution_order(
                        str(run["active_exit_intent_id"])
                    )
                    if local_exit is None:
                        return "RECOVERY_REQUIRED", updates, "exit_projection_missing"
                    if local_exit.get("broker_order_id") is not None:
                        return "EXIT_WORKING", updates, "exit_not_visible_yet"
                    if str(local_exit["status"]) in {"PENDING", "UNKNOWN"}:
                        return "EXIT_UNKNOWN", updates, None
                return "OPEN_UNPROTECTED", updates, "conditional_expired"
            return "RECOVERY_REQUIRED", updates, "conditional_state_invalid"

        if run.get("active_exit_intent_id") is not None:
            local_exit = self.store.load_execution_order(
                str(run["active_exit_intent_id"])
            )
            if local_exit is None:
                return "RECOVERY_REQUIRED", updates, "exit_projection_missing"
            if str(local_exit["status"]) in {"PENDING", "UNKNOWN"}:
                return "EXIT_UNKNOWN", updates, None
            if owned > 0 and not exits:
                return "RECOVERY_REQUIRED", updates, "exit_identity_missing"
        if run.get("protection_intent_id") is not None:
            protection = self.store.load_execution_order(
                str(run["protection_intent_id"])
            )
            if protection is None:
                return "RECOVERY_REQUIRED", updates, "protection_projection_missing"
            if str(protection["status"]) in {"PENDING", "ACKNOWLEDGED", "UNKNOWN"}:
                return "RECOVERY_REQUIRED", updates, "conditional_identity_missing"

        if entry is None:
            if run.get("entry_intent_id") is not None:
                projected_entry = self.store.load_execution_order(
                    str(run["entry_intent_id"])
                )
                if projected_entry is None:
                    return "RECOVERY_REQUIRED", updates, "entry_projection_missing"
                if str(projected_entry["status"]) in {"PENDING", "UNKNOWN"}:
                    return "ENTRY_UNKNOWN", updates, None
            if owned == 0 and int(run.get("entry_submit_count") or 0) == 0:
                if not self._approval_valid(run):
                    return "PLANNED", updates, "approval_invalid"
                return "READY_TO_ENTER", updates, None
            if owned > 0:
                updates["unprotected_since"] = run.get("unprotected_since") or self._now()
                return "OPEN_UNPROTECTED", updates, None
            return "RECOVERY_REQUIRED", updates, "entry_identity_missing"
        if entry.status == "UNKNOWN":
            if run.get("state") == "ENTRY_CANCELING" or self._has_intent_event(
                f"{self.plan.plan_id}:entry", "entry_cancel_send_reserved"
            ):
                return "ENTRY_CANCELING", updates, None
            return "ENTRY_UNKNOWN", updates, None
        if entry.status in _GENERAL_OPEN:
            if run.get("state") == "ENTRY_CANCELING" or self._has_intent_event(
                f"{self.plan.plan_id}:entry", "entry_cancel_send_reserved"
            ):
                return "ENTRY_CANCELING", updates, None
            return "ENTRY_WORKING", updates, None
        if entry.status in _GENERAL_TERMINAL:
            if entry.filled_quantity == 0:
                return "CANCELLED", updates, None
            if owned > 0:
                updates["unprotected_since"] = run.get("unprotected_since") or self._now()
                return "OPEN_UNPROTECTED", updates, None
        return "RECOVERY_REQUIRED", updates, "entry_state_invalid"

    def _stable_snapshot(self) -> BrokerSnapshot:
        self._dirty = False
        first = self.snapshot_reader(self.plan)
        if not isinstance(first, BrokerSnapshot):
            raise IntradayRuntimeError("snapshot_schema_invalid")
        first_fingerprint = _snapshot_fingerprint(first)
        self._stream_ack = False
        self.on_stream_frame(self.stream_barrier())
        second = self.snapshot_reader(self.plan)
        if not isinstance(second, BrokerSnapshot):
            raise IntradayRuntimeError("snapshot_schema_invalid")
        if self._dirty or first_fingerprint != _snapshot_fingerprint(second):
            raise IntradayRuntimeError("snapshot_unstable")
        now = self._now()
        if (
            second.captured_at > now + timedelta(seconds=2)
            or now - second.captured_at > self.max_stream_age
        ):
            raise IntradayRuntimeError("snapshot_time_invalid")
        return second

    def _entry_order(self, snapshot: BrokerSnapshot) -> BrokerOrderObservation | None:
        orders = [
            item
            for item in snapshot.orders
            if item.plan_id == self.plan.plan_id and item.role == "ENTRY"
        ]
        if len({item.order_id for item in orders}) > 1:
            raise IntradayRuntimeError("multiple_entry_orders")
        return orders[-1] if orders else None

    def _snapshot_order_identity_error(
        self,
        snapshot: BrokerSnapshot,
        run: Mapping[str, object],
    ) -> str | None:
        role_groups = (
            ("ENTRY", "multiple_entry_orders"),
            ("TRIGGERED_EXIT", "multiple_triggered_orders"),
        )
        for role, reason in role_groups:
            if len(
                {item.order_id for item in snapshot.orders if item.role == role}
            ) > 1:
                return reason
        if len(
            {
                item.order_id
                for item in snapshot.orders
                if item.role in {"FORCE_EXIT", "EMERGENCY_EXIT"}
            }
        ) > 1:
            return "multiple_local_exit_orders"
        for order in snapshot.orders:
            if order.role == "TRIGGERED_EXIT":
                continue
            if order.role == "ENTRY":
                intent_id = run.get("entry_intent_id")
            else:
                intent_id = run.get("active_exit_intent_id")
            if not isinstance(intent_id, str):
                return "order_identity_missing"
            intent = self.store.load_intraday_order_intent(intent_id)
            execution = self.store.load_execution_order(intent_id)
            if (
                intent is None
                or execution is None
                or intent.get("order_role") != order.role
                or intent.get("symbol") != self.plan.symbol
                or intent.get("side") != order.side
                or Decimal(str(intent.get("quantity"))) != order.quantity
            ):
                return "order_identity_mismatch"
            expected_client_id = intent.get("idempotency_key")
            if (
                order.client_order_id is not None
                and order.client_order_id != expected_client_id
            ):
                return "order_client_identity_mismatch"
            broker_order_id = execution.get("broker_order_id")
            if broker_order_id is not None:
                if order.order_id != broker_order_id:
                    return "order_broker_identity_mismatch"
            elif order.client_order_id != expected_client_id:
                return "order_client_identity_missing"
        return None

    def _triggered_order(
        self, snapshot: BrokerSnapshot
    ) -> BrokerOrderObservation | None:
        orders = [item for item in snapshot.orders if item.role == "TRIGGERED_EXIT"]
        if len({item.order_id for item in orders}) > 1:
            raise IntradayRuntimeError("multiple_triggered_orders")
        return orders[-1] if orders else None

    def _single_known_oco(
        self, snapshot: BrokerSnapshot, run: Mapping[str, object]
    ) -> ConditionalOrderObservation | None:
        matching = [
            item
            for item in snapshot.conditional_orders
            if item.plan_id == self.plan.plan_id
            and item.client_order_id == self.plan.oco_client_order_id
        ]
        if len(matching) > 1:
            raise IntradayRuntimeError("multiple_conditional_candidates")
        if not matching:
            return None
        intent_id = run.get("protection_intent_id")
        if not isinstance(intent_id, str):
            return None
        execution = self.store.load_execution_order(intent_id)
        broker_order_id = execution.get("broker_order_id") if execution else None
        if not isinstance(broker_order_id, str) or not broker_order_id:
            return None
        return matching[0] if matching[0].conditional_order_id == broker_order_id else None

    def _oco_matches(
        self, order: ConditionalOrderObservation, owned: Decimal
    ) -> bool:
        return (
            self._oco_economics_match(order, owned)
            and _leg_matches(
                order.first,
                trigger=self.plan.target_trigger,
                price=self.plan.target_limit,
            )
            and _leg_matches(
                order.second,
                trigger=self.plan.stop_trigger,
                price=self.plan.stop_limit,
            )
        )

    def _oco_economics_match(
        self, order: ConditionalOrderObservation, quantity: Decimal
    ) -> bool:
        return (
            quantity > 0
            and order.symbol == self.plan.symbol
            and order.quantity == quantity
            and order.order_type == "LIMIT"
            and order.expire_date == self.plan.session_date.isoformat()
            and _leg_economics_match(
                order.first,
                trigger=self.plan.target_trigger,
                price=self.plan.target_limit,
            )
            and _leg_economics_match(
                order.second,
                trigger=self.plan.stop_trigger,
                price=self.plan.stop_limit,
            )
        )

    def _expired_oco_matches(
        self, order: ConditionalOrderObservation, owned: Decimal
    ) -> bool:
        return (
            order.status == "EXPIRED"
            and self._oco_economics_match(order, owned)
            and order.first.get("status") == "CANCELED"
            and order.second.get("status") == "CANCELED"
            and _conditional_triggered_id(order) is None
        )

    def _triggered_group_matches(
        self,
        conditional: ConditionalOrderObservation,
        triggered: BrokerOrderObservation,
        run: Mapping[str, object],
    ) -> bool:
        intent_id = run.get("protection_intent_id")
        intent = (
            self.store.load_intraday_order_intent(str(intent_id))
            if isinstance(intent_id, str)
            else None
        )
        if intent is None:
            return False
        parent_quantity = Decimal(str(intent.get("quantity")))
        triggered_legs = [
            leg
            for leg in (conditional.first, conditional.second)
            if leg.get("triggered_order_id") is not None
        ]
        other_legs = [
            leg
            for leg in (conditional.first, conditional.second)
            if leg.get("triggered_order_id") is None
        ]
        return (
            conditional.status in _CONDITIONAL_TERMINAL
            and self._oco_economics_match(conditional, parent_quantity)
            and len(triggered_legs) == 1
            and len(other_legs) == 1
            and triggered_legs[0].get("status") == "HOLDING"
            and other_legs[0].get("status") == "CANCELED"
            and triggered.side == "SELL"
            and triggered.quantity == conditional.quantity
        )

    def _entry_stream_gate(self, now: datetime) -> bool:
        if (
            not self._stream_ack
            or not self._crossing
            or self._last_trade is None
            or self._trade_at is None
            or self._bid is None
            or self._ask is None
            or self._book_at is None
            or now - self._trade_at > self.max_stream_age
            or now - self._book_at > self.max_stream_age
            or self._last_trade > self.plan.entry_limit
        ):
            return False
        midpoint = (self._bid + self._ask) / 2
        return midpoint > 0 and (self._ask - self._bid) / midpoint <= self.max_spread_fraction

    def _entry_recovery_gate(self, snapshot: BrokerSnapshot, now: datetime) -> bool:
        if (
            now >= self.plan.entry_expiry
            or not self._stream_ack
            or self._last_trade is None
            or self._trade_at is None
            or self._bid is None
            or self._ask is None
            or self._book_at is None
            or now - self._trade_at > self.max_stream_age
            or now - self._book_at > self.max_stream_age
            or not self.plan.entry_trigger <= self._last_trade <= self.plan.entry_limit
        ):
            return False
        midpoint = (self._bid + self._ask) / 2
        return (
            midpoint > 0
            and (self._ask - self._bid) / midpoint <= self.max_spread_fraction
            and self._entry_snapshot_gate(snapshot)
        )

    def _entry_snapshot_gate(self, snapshot: BrokerSnapshot) -> bool:
        return (
            snapshot.market_open
            and snapshot.halt_state == "CLEAR"
            and not snapshot.foreign_activity
            and snapshot.holding_quantity == 0
            and snapshot.sellable_quantity == 0
            and not snapshot.orders
            and not snapshot.conditional_orders
            and snapshot.buying_power is not None
            and snapshot.buying_power >= self.plan.cash_reserved
        )

    def _entry_body(self) -> dict[str, object]:
        return {
            "clientOrderId": self.plan.entry_client_order_id,
            "symbol": self.plan.symbol,
            "side": "BUY",
            "orderType": "LIMIT",
            "quantity": str(self.plan.quantity),
            "confirmHighValueOrder": False,
            "price": _canonical_decimal(self.plan.entry_limit),
        }

    def _protection_body(self, quantity: Decimal) -> dict[str, object]:
        return {
            "symbol": self.plan.symbol,
            "type": "OCO",
            "quantity": _canonical_decimal(quantity),
            "orderType": "LIMIT",
            "expireDate": self.plan.session_date.isoformat(),
            "first": {
                "orderSide": "SELL",
                "triggerPrice": _canonical_decimal(self.plan.target_trigger),
                "orderPrice": _canonical_decimal(self.plan.target_limit),
            },
            "second": {
                "orderSide": "SELL",
                "triggerPrice": _canonical_decimal(self.plan.stop_trigger),
                "orderPrice": _canonical_decimal(self.plan.stop_limit),
            },
            "clientOrderId": self.plan.oco_client_order_id,
            "confirmHighValueOrder": False,
        }

    def _exit_body(self, quantity: Decimal, role: str) -> dict[str, object]:
        return {
            "clientOrderId": _derived_client_order_id(self.plan.plan_id, role),
            "symbol": self.plan.symbol,
            "side": "SELL",
            "orderType": "MARKET",
            "quantity": _canonical_decimal(quantity),
            "confirmHighValueOrder": False,
        }

    def _reservation_sendable(
        self, run: Mapping[str, object], intent: Mapping[str, object]
    ) -> bool:
        return bool(
            self.store.intraday_reservation_is_sendable(
                intent_id=str(intent["intent_id"]),
                plan_id=self.plan.plan_id,
                expected_state=str(run["state"]),
                expected_run_version=int(run["version"]),
                writer_id=self.writer_id,
                writer_fence=self._fence(),
                request_hash=str(intent["request_hash"]),
                now=self._now(),
                min_lease_seconds=30,
            )
        )

    def _verified_request_body(
        self, intent: Mapping[str, object]
    ) -> Mapping[str, object]:
        body = _request_body(intent)
        envelope = {
            "account_key": intent.get("account_key"),
            "plan_id": intent.get("plan_id"),
            "order_role": intent.get("order_role"),
            "method": intent.get("method"),
            "path": intent.get("path"),
            "body": body,
        }
        digest = canonical_order_request(envelope)[1]
        stored_hash = intent.get("request_hash")
        if (
            not isinstance(stored_hash, str)
            or not hmac.compare_digest(digest, stored_hash)
            or intent.get("account_key") != self.account_key
            or intent.get("plan_id") != self.plan.plan_id
            or intent.get("symbol") != self.plan.symbol
        ):
            raise IntradayRuntimeError("reservation_request_integrity_invalid")
        return body

    def _has_event(self, event_type: str) -> bool:
        return any(
            item["event_type"] == event_type
            and item.get("plan_id") == self.plan.plan_id
            and isinstance(item.get("writer_fence"), int)
            for item in self.store.list_execution_events(
                intent_id=f"{self.plan.plan_id}:protection"
            )
        )

    def _has_intent_event(self, intent_id: str, event_type: str) -> bool:
        return any(
            item["event_type"] == event_type
            and item.get("plan_id") == self.plan.plan_id
            and isinstance(item.get("writer_fence"), int)
            for item in self.store.list_execution_events(intent_id=intent_id)
        )

    def _renew_if_needed(self, run: Mapping[str, object]) -> None:
        lease_until = run.get("writer_lease_until")
        if isinstance(lease_until, str):
            lease_until = datetime.fromisoformat(lease_until)
        if isinstance(lease_until, datetime) and _utc_datetime(
            lease_until, "writer_lease_until"
        ) - self._now() >= timedelta(seconds=30):
            return
        renewed = self.store.renew_intraday_writer(
            plan_id=self.plan.plan_id,
            writer_id=self.writer_id,
            writer_fence=self._fence(),
            now=self._now(),
            lease_seconds=45,
        )
        if renewed is None:
            raise IntradayRuntimeError("writer_fenced")

    def _cas(
        self,
        run: Mapping[str, object],
        next_state: str,
        *,
        event_type: str,
        payload: Mapping[str, object] | None = None,
        reason_code: str | None = None,
        updates: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        result = self.store.cas_intraday_run(
            plan_id=self.plan.plan_id,
            expected_state=str(run["state"]),
            expected_version=int(run["version"]),
            next_state=next_state,
            writer_id=self.writer_id,
            writer_fence=self._fence(),
            event_type=event_type,
            event_status=next_state,
            intent_id=f"run:{self.plan.plan_id}",
            payload=dict(payload or {}),
            reason_code=reason_code,
            updates=dict(updates or {}),
            now=self._now(),
        )
        if result is None:
            raise IntradayRuntimeError("writer_fenced")
        return result

    def _fence(self) -> int:
        if self.writer_fence is None:
            raise IntradayRuntimeError("writer_not_acquired")
        return self.writer_fence

    def _approval_valid(self, run: Mapping[str, object]) -> bool:
        return (
            isinstance(run.get("approved_envelope_sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", str(run["approved_envelope_sha256"]))
            is not None
            and isinstance(run.get("approval_receipt_sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", str(run["approval_receipt_sha256"]))
            is not None
            and isinstance(run.get("approval_interaction_id"), str)
            and bool(run["approval_interaction_id"])
            and run.get("boot_id_hash") == self.current_boot_id_hash
            and run.get("approved_writer_fence") == self._fence()
            and int(run.get("approval_generation") or 0) > 0
            and isinstance(run.get("approved_at"), datetime)
            and isinstance(run.get("approval_expires_at"), datetime)
            and self._now()
            < _utc_datetime(run["approval_expires_at"], "approval_expires_at")
            and run.get("entry_disabled_at") is None
            and run.get("loss_fuse_at") is None
        )

    def _verify_stored_plan(self) -> None:
        try:
            stored = self.store.load_intraday_plan(
                account_key=self.account_key,
                session_date=self.plan.session_date,
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            raise IntradayRuntimeError("plan_integrity_invalid") from exc
        expected = intraday_plan_payload(self.plan)
        if (
            stored is None
            or stored.get("plan_id") != self.plan.plan_id
            or stored.get("account_key") != self.account_key
            or stored.get("session_date") != self.plan.session_date
            or stored.get("symbol") != self.plan.symbol
            or stored.get("mode") != "shadow"
            or not isinstance(stored.get("payload"), Mapping)
            or any(stored["payload"].get(key) != value for key, value in expected.items())
        ):
            raise IntradayRuntimeError("plan_mismatch")

    def _now(self) -> datetime:
        current = _utc_datetime(self.clock(), "clock")
        if self._last_clock_value is not None and current < self._last_clock_value:
            raise IntradayRuntimeError("clock_moved_backwards")
        self._last_clock_value = current
        return current

    def _invalidate_stream(self) -> None:
        self._stream_ack = False
        self._dirty = True
        self._recovered = False


def _finite_decimal(value: object, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal") from exc
    if not result.is_finite() or result < 0 or positive and result == 0:
        requirement = "positive" if positive else "nonnegative"
        raise ValueError(f"{name} must be finite and {requirement}")
    return result


def _utc_datetime(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _canonical_decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("non-finite decimal is not allowed")
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _canonical_value(value: object) -> object:
    if isinstance(value, Decimal):
        return _canonical_decimal(value)
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            result[key] = _canonical_value(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, float):
        raise TypeError("float values are not allowed in order requests")
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported order request value: {type(value).__name__}")


def _copy_leg(value: Mapping[str, object]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("conditional leg must be an object")
    required = {"type", "status", "trigger_price", "order_price", "triggered_order_id"}
    if set(value) != required:
        raise ValueError("conditional leg schema is invalid")
    leg_type = value["type"]
    status = value["status"]
    if leg_type != "STOP" or not isinstance(status, str):
        raise ValueError("conditional leg type or status is invalid")
    normalized_status = status.upper()
    if normalized_status not in _CONDITIONAL_LEG_STATES:
        raise ValueError("conditional leg status is unknown")
    trigger = _finite_decimal(value["trigger_price"], "trigger price", positive=True)
    order_price = _finite_decimal(value["order_price"], "order price", positive=True)
    triggered = value["triggered_order_id"]
    if triggered is not None and (not isinstance(triggered, str) or not triggered):
        raise ValueError("triggered order ID is invalid")
    return {
        "type": "STOP",
        "status": normalized_status,
        "trigger_price": trigger,
        "order_price": order_price,
        "triggered_order_id": triggered,
    }


def _frame_datetime(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise IntradayRuntimeError("stream_time_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IntradayRuntimeError("stream_time_invalid") from exc
    return _utc_datetime(parsed, "captured_at")


def _snapshot_fingerprint(snapshot: BrokerSnapshot) -> str:
    payload = {
        "symbol": snapshot.symbol,
        "holding_quantity": snapshot.holding_quantity,
        "sellable_quantity": snapshot.sellable_quantity,
        "buying_power": snapshot.buying_power,
        "market_open": snapshot.market_open,
        "halt_state": snapshot.halt_state,
        "foreign_activity": snapshot.foreign_activity,
        "orders": [
            {
                "order_id": item.order_id,
                "plan_id": item.plan_id,
                "role": item.role,
                "side": item.side,
                "status": item.status,
                "quantity": item.quantity,
                "filled_quantity": item.filled_quantity,
                "client_order_id": item.client_order_id,
                "average_fill_price": item.average_fill_price,
            }
            for item in sorted(snapshot.orders, key=lambda item: item.order_id)
        ],
        "conditional_orders": [
            {
                "conditional_order_id": item.conditional_order_id,
                "plan_id": item.plan_id,
                "client_order_id": item.client_order_id,
                "symbol": item.symbol,
                "market": item.market,
                "conditional_type": item.conditional_type,
                "status": item.status,
                "quantity": item.quantity,
                "order_type": item.order_type,
                "expire_date": item.expire_date,
                "first": item.first,
                "second": item.second,
            }
            for item in sorted(
                snapshot.conditional_orders,
                key=lambda item: item.conditional_order_id,
            )
        ],
    }
    return canonical_order_request(payload)[1]


def _conditional_triggered_id(order: ConditionalOrderObservation) -> str | None:
    values = [
        value
        for value in (
            order.first.get("triggered_order_id"),
            order.second.get("triggered_order_id"),
        )
        if value is not None
    ]
    if len(values) > 1:
        raise IntradayRuntimeError("multiple_triggered_orders")
    return str(values[0]) if values else None


def _leg_matches(
    leg: Mapping[str, object], *, trigger: Decimal, price: Decimal
) -> bool:
    return (
        _leg_economics_match(leg, trigger=trigger, price=price)
        and leg.get("status") == "WATCHING"
        and leg.get("triggered_order_id") is None
    )


def _leg_economics_match(
    leg: Mapping[str, object], *, trigger: Decimal, price: Decimal
) -> bool:
    return (
        leg.get("type") == "STOP"
        and leg.get("trigger_price") == trigger
        and leg.get("order_price") == price
    )


def _action_request_hash(operation: str, root_order_id: str) -> str:
    return canonical_order_request(
        {"operation": str(operation).upper(), "rootOrderId": root_order_id}
    )[1]


def _derived_client_order_id(plan_id: str, role: str) -> str:
    digest = hashlib.sha256(f"{plan_id}:{role}".encode("utf-8")).hexdigest()
    return f"itd-x-{digest[:20]}"


def _reservation_run(reservation: Mapping[str, object]) -> Mapping[str, object]:
    run = reservation.get("run")
    if not isinstance(run, Mapping):
        raise IntradayRuntimeError("reservation_run_invalid")
    return run


def _reservation_intent(reservation: Mapping[str, object]) -> Mapping[str, object]:
    intent = reservation.get("intent")
    if not isinstance(intent, Mapping):
        raise IntradayRuntimeError("reservation_intent_invalid")
    return intent


def _request_body(intent: Mapping[str, object]) -> Mapping[str, object]:
    raw = intent.get("request_json")
    if not isinstance(raw, str):
        raise IntradayRuntimeError("reservation_request_invalid")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise IntradayRuntimeError("reservation_request_invalid") from exc
    if not isinstance(value, Mapping):
        raise IntradayRuntimeError("reservation_request_invalid")
    encoded, _ = canonical_order_request(value)
    if encoded.decode("ascii") != raw:
        raise IntradayRuntimeError("reservation_request_noncanonical")
    return value


def _changed_run_updates(
    run: Mapping[str, object], updates: Mapping[str, object]
) -> dict[str, object]:
    changed: dict[str, object] = {}
    for key, value in updates.items():
        current = run.get(key)
        if isinstance(current, Decimal):
            try:
                same = current == Decimal(str(value))
            except InvalidOperation:
                same = False
        elif isinstance(current, datetime):
            try:
                candidate = (
                    value
                    if isinstance(value, datetime)
                    else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                )
                same = _utc_datetime(current, key) == _utc_datetime(candidate, key)
            except (TypeError, ValueError):
                same = False
        else:
            same = current == value
        if not same:
            changed[key] = value
    return changed
