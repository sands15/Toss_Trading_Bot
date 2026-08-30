from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from turtle_bot.intraday_live import (
    BrokerOrderObservation,
    BrokerSnapshot,
    ConditionalOrderObservation,
    IntradayLiveRuntime,
    canonical_order_request,
    remaining_owned_quantity,
)
from turtle_bot.intraday import build_intraday_plan, intraday_plan_payload
from turtle_bot.live_execution import LiveBrokerError
from turtle_bot.live_order import BrokerOrderTicket, ExecutionStatus
from turtle_bot.state_store import SQLiteStateStore
from turtle_bot.toss_conditional import ConditionalOrderUnknownStateError
from turtle_bot.toss_live_adapter import TossLiveBrokerAdapter


AT = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)


def _order(
    order_id: str,
    *,
    role: str,
    side: str,
    filled: str,
    quantity: str = "5",
) -> BrokerOrderObservation:
    return BrokerOrderObservation(
        order_id=order_id,
        plan_id="plan-1",
        role=role,
        side=side,
        status="FILLED" if Decimal(filled) == Decimal(quantity) else "PARTIAL_FILLED",
        quantity=Decimal(quantity),
        filled_quantity=Decimal(filled),
        client_order_id=f"client-{order_id}",
        average_fill_price=Decimal("100") if Decimal(filled) else None,
    )


def _snapshot(*orders: BrokerOrderObservation) -> BrokerSnapshot:
    remaining = Decimal("3")
    return BrokerSnapshot(
        symbol="AAPL",
        holding_quantity=remaining,
        sellable_quantity=remaining,
        market_open=True,
        halt_state="CLEAR",
        foreign_activity=False,
        orders=orders,
        captured_at=AT,
    )


def test_canonical_order_request_normalizes_decimal_without_float_or_nan() -> None:
    first, first_hash = canonical_order_request(
        {"quantity": Decimal("1.00"), "nested": {"price": Decimal("1E+2")}}
    )
    second, second_hash = canonical_order_request(
        {"nested": {"price": Decimal("100")}, "quantity": Decimal("1")}
    )

    assert first == second == b'{"nested":{"price":"100"},"quantity":"1"}'
    assert first_hash == second_hash
    with pytest.raises(TypeError, match="float"):
        canonical_order_request({"price": 1.5})
    with pytest.raises(ValueError, match="non-finite"):
        canonical_order_request({"price": Decimal("NaN")})


def test_remaining_owned_quantity_uses_cumulative_fill_not_event_sum() -> None:
    earlier = _order("entry", role="ENTRY", side="BUY", filled="3")
    latest = _order("entry", role="ENTRY", side="BUY", filled="5")
    exit_order = _order(
        "exit", role="EMERGENCY_EXIT", side="SELL", filled="2", quantity="5"
    )

    assert remaining_owned_quantity(
        _snapshot(earlier, latest, exit_order), "plan-1"
    ) == Decimal("3")


def test_remaining_owned_quantity_rejects_decreasing_or_changed_duplicate() -> None:
    latest = _order("entry", role="ENTRY", side="BUY", filled="5")
    earlier = _order("entry", role="ENTRY", side="BUY", filled="3")
    with pytest.raises(ValueError, match="decreased"):
        remaining_owned_quantity(_snapshot(latest, earlier), "plan-1")

    changed = BrokerOrderObservation(
        order_id="entry",
        plan_id="plan-1",
        role="ENTRY",
        side="BUY",
        status="FILLED",
        quantity=Decimal("6"),
        filled_quantity=Decimal("6"),
        client_order_id="client-entry",
        average_fill_price=Decimal("100"),
    )
    with pytest.raises(ValueError, match="identity"):
        remaining_owned_quantity(_snapshot(latest, changed), "plan-1")


def test_remaining_owned_quantity_rejects_oversell_without_clamping() -> None:
    entry = _order("entry", role="ENTRY", side="BUY", filled="3")
    exit_order = _order(
        "exit", role="FORCE_EXIT", side="SELL", filled="4", quantity="5"
    )

    with pytest.raises(ValueError, match="exceeds"):
        remaining_owned_quantity(_snapshot(entry, exit_order), "plan-1")


@pytest.mark.parametrize(
    ("quantity", "filled", "average", "message"),
    [
        ("NaN", "0", None, "finite"),
        ("1", "2", "100", "exceeds"),
        ("1", "1", None, "average fill"),
        ("1", "-1", None, "nonnegative"),
    ],
)
def test_broker_order_observation_rejects_malformed_execution(
    quantity: str, filled: str, average: str | None, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        BrokerOrderObservation(
            order_id="entry",
            plan_id="plan-1",
            role="ENTRY",
            side="BUY",
            status="FILLED",
            quantity=Decimal(quantity),
            filled_quantity=Decimal(filled),
            average_fill_price=Decimal(average) if average is not None else None,
        )


def test_broker_snapshot_requires_explicit_market_safety_observations() -> None:
    with pytest.raises(TypeError, match="required keyword-only argument|required positional argument"):
        BrokerSnapshot(
            symbol="AAPL",
            holding_quantity=Decimal("0"),
            sellable_quantity=Decimal("0"),
        )


def test_conditional_observation_rejects_unknown_leg_status() -> None:
    plan = _plan()
    watching = _watching_oco(plan)
    first = dict(watching.first)
    first["status"] = "NEW_UNDOCUMENTED_STATE"

    with pytest.raises(ValueError, match="leg status is unknown"):
        ConditionalOrderObservation(
            conditional_order_id=watching.conditional_order_id,
            plan_id=watching.plan_id,
            client_order_id=watching.client_order_id,
            symbol=watching.symbol,
            market=watching.market,
            conditional_type=watching.conditional_type,
            status=watching.status,
            quantity=watching.quantity,
            order_type=watching.order_type,
            expire_date=watching.expire_date,
            first=first,
            second=watching.second,
        )


def test_runtime_rejects_market_data_before_recovery() -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start)
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    with SQLiteStateStore() as store:
        runtime = IntradayLiveRuntime(
            store=store,
            plan=plan,
            account_key=plan.account_id,
            writer_id="writer-1",
            current_boot_id_hash="3" * 64,
            snapshot_reader=broker.read,
            stream_barrier=broker.stream_ack,
            order_adapter=broker,
            conditional_adapter=broker,
            personal_topic="personal:order:account-fixture",
            clock=clock,
        )
        with pytest.raises(RuntimeError, match="stream_before_recovery"):
            runtime.on_stream_frame(
                {
                    "type": "trade",
                    "topic": f"trade:us:{plan.symbol}",
                    "symbol": plan.symbol,
                    "price": str(plan.entry_trigger),
                    "captured_at": clock().isoformat(),
                }
            )


def test_runtime_invalidates_approval_from_another_boot(tmp_path) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start)
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    with SQLiteStateStore(tmp_path / "wrong-boot.sqlite") as store:
        saved = _save_run(store, plan, clock())
        runtime = _runtime(store, plan, clock, broker, boot_id_hash="4" * 64)
        runtime.recover()
        _consume_approval(
            store,
            plan,
            saved,
            runtime,
            clock(),
            boot_id_hash="3" * 64,
        )
        runtime.recover()
        state = store.load_intraday_run(plan.plan_id)["state"]

    assert state == "PLANNED"
    assert [name for name, _ in broker.calls].count("place") == 0


def test_runtime_rejects_injected_plan_economics_that_do_not_match_db(tmp_path) -> None:
    original = _plan()
    changed = replace(original, quantity=original.quantity * 10)
    clock = FakeClock(original.entry_start)
    broker = ScriptedBroker(_empty_snapshot(original, clock()))
    with SQLiteStateStore(tmp_path / "plan-mismatch.sqlite") as store:
        _save_run(store, original, clock())
        runtime = _runtime(store, changed, clock, broker)

        with pytest.raises(RuntimeError, match="plan_mismatch"):
            runtime.recover()

        run = store.load_intraday_run(original.plan_id)

    assert run["state"] == "PLANNED"
    assert run["writer_id"] is None
    assert broker.calls == []


class FakeClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class ScriptedBroker:
    def __init__(self, snapshot: BrokerSnapshot) -> None:
        self.origin = "https://broker.invalid"
        self.snapshot = snapshot
        self.calls: list[tuple[str, object]] = []
        self._general_counter = 0
        self._barrier_counter = 0
        self.general_outcomes: list[str] = []
        self.cancel_unknown = False
        self.conditional_create_unknown = False
        self.conditional_create_reject = False
        self.conditional_delete_unknown = False

    def read(self, _plan) -> BrokerSnapshot:
        self.calls.append(("snapshot", self.snapshot))
        return self.snapshot

    def stream_ack(self) -> object:
        self._barrier_counter += 1
        self.calls.append(("stream-ack", None))
        return {
            "type": "ack",
            "request_id": f"barrier-{self._barrier_counter}",
            "subscribed": [
                f"trade:us:{self.snapshot.symbol}",
                f"orderbook:us:{self.snapshot.symbol}",
                "personal:order:account-fixture",
            ],
            "rejected": [],
        }

    def place_order(self, intent) -> BrokerOrderTicket:
        self._general_counter += 1
        self.calls.append(("place", intent))
        outcome = self.general_outcomes.pop(0) if self.general_outcomes else "ack"
        if outcome == "unknown":
            raise LiveBrokerError("scripted transport timeout", unknown_state=True)
        if outcome == "reject":
            raise LiveBrokerError("scripted validation rejection", unknown_state=False)
        return BrokerOrderTicket(
            broker_order_id=f"order-{self._general_counter}",
            status=ExecutionStatus.ACKNOWLEDGED,
        )

    def cancel_order(self, order_id: str) -> BrokerOrderTicket:
        self.calls.append(("cancel", order_id))
        if self.cancel_unknown:
            raise LiveBrokerError("scripted cancel timeout", unknown_state=True)
        return BrokerOrderTicket(
            broker_order_id=f"cancel-{order_id}",
            status=ExecutionStatus.PENDING_CANCEL,
        )

    def create(self, body, *, current_price):
        self.calls.append(("oco-create", dict(body)))
        if self.conditional_create_unknown:
            raise ConditionalOrderUnknownStateError(
                "create", "accepted response was lost"
            )
        if self.conditional_create_reject:
            raise ValueError("scripted deterministic validation rejection")
        return {"conditionalOrderId": "oco-1"}

    def delete(self, order_id: str) -> None:
        self.calls.append(("oco-delete", order_id))
        if self.conditional_delete_unknown:
            raise ConditionalOrderUnknownStateError(
                "delete", "accepted response was lost"
            )


def _plan():
    return build_intraday_plan(
        account_id="acct-1",
        session_date=date(2026, 8, 28),
        symbol="AAPL",
        reference_at=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
        created_at=datetime(2026, 8, 28, 12, 5, tzinfo=timezone.utc),
        regular_open=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
        regular_close=datetime(2026, 8, 28, 21, 0, tzinfo=timezone.utc),
        entry_start_minutes_after_open=5,
        entry_expiry_minutes_after_open=60,
        force_exit_minutes_before_close=15,
        available_cash=Decimal("10000"),
        reference_price=Decimal("100"),
        cash_allocation_fraction=Decimal("0.5"),
        risk_fraction=Decimal("0.01"),
        take_profit_fraction=Decimal("0.02"),
        stop_fraction=Decimal("0.015"),
        stop_limit_buffer_fraction=Decimal("0.002"),
        max_entry_slippage_fraction=Decimal("0.001"),
        estimated_round_trip_cost_fraction=Decimal("0.001"),
        estimated_fixed_round_trip_cost=Decimal("0"),
        minimum_reward_risk_ratio=Decimal("1"),
        max_quantity=5,
        max_notional=Decimal("1000"),
    )


def _empty_snapshot(plan, at: datetime) -> BrokerSnapshot:
    return BrokerSnapshot(
        symbol=plan.symbol,
        holding_quantity=Decimal("0"),
        sellable_quantity=Decimal("0"),
        market_open=True,
        halt_state="CLEAR",
        foreign_activity=False,
        buying_power=Decimal("10000"),
        captured_at=at,
    )


def _entry_observation(
    plan, *, status: str = "FILLED", filled: Decimal | None = None
) -> BrokerOrderObservation:
    if filled is None:
        filled = Decimal(plan.quantity) if status == "FILLED" else Decimal("0")
    return BrokerOrderObservation(
        order_id="order-1",
        plan_id=plan.plan_id,
        role="ENTRY",
        side="BUY",
        status=status,
        quantity=Decimal(plan.quantity),
        filled_quantity=filled,
        client_order_id=plan.entry_client_order_id,
        average_fill_price=plan.entry_limit if filled else None,
    )


def _watching_oco(plan) -> ConditionalOrderObservation:
    return ConditionalOrderObservation(
        conditional_order_id="oco-1",
        plan_id=plan.plan_id,
        client_order_id=plan.oco_client_order_id,
        symbol=plan.symbol,
        market="US",
        conditional_type="OCO",
        status="WATCHING",
        quantity=Decimal(plan.quantity),
        order_type="LIMIT",
        expire_date=plan.session_date.isoformat(),
        first={
            "type": "STOP",
            "status": "WATCHING",
            "trigger_price": plan.target_trigger,
            "order_price": plan.target_limit,
            "triggered_order_id": None,
        },
        second={
            "type": "STOP",
            "status": "WATCHING",
            "trigger_price": plan.stop_trigger,
            "order_price": plan.stop_limit,
            "triggered_order_id": None,
        },
    )


def _completed_oco(plan, triggered_order_id: str) -> ConditionalOrderObservation:
    watching = _watching_oco(plan)
    first = dict(watching.first)
    first.update(status="HOLDING", triggered_order_id=triggered_order_id)
    second = dict(watching.second)
    second["status"] = "CANCELED"
    return ConditionalOrderObservation(
        conditional_order_id=watching.conditional_order_id,
        plan_id=watching.plan_id,
        client_order_id=watching.client_order_id,
        symbol=watching.symbol,
        market=watching.market,
        conditional_type=watching.conditional_type,
        status="COMPLETED",
        quantity=watching.quantity,
        order_type=watching.order_type,
        expire_date=watching.expire_date,
        first=first,
        second=second,
    )


def _paused_oco(plan) -> ConditionalOrderObservation:
    watching = _watching_oco(plan)
    return ConditionalOrderObservation(
        conditional_order_id=watching.conditional_order_id,
        plan_id=watching.plan_id,
        client_order_id=watching.client_order_id,
        symbol=watching.symbol,
        market=watching.market,
        conditional_type=watching.conditional_type,
        status="PAUSED",
        quantity=watching.quantity,
        order_type=watching.order_type,
        expire_date=watching.expire_date,
        first=watching.first,
        second=watching.second,
    )


def _expired_oco(plan) -> ConditionalOrderObservation:
    watching = _watching_oco(plan)
    first = dict(watching.first)
    first["status"] = "CANCELED"
    second = dict(watching.second)
    second["status"] = "CANCELED"
    return replace(
        watching,
        status="EXPIRED",
        first=first,
        second=second,
    )


def _save_run(store: SQLiteStateStore, plan, at: datetime) -> dict[str, object]:
    payload = intraday_plan_payload(plan)
    payload.update({"mode": "shadow", "status": "SHADOW_PLANNED"})
    saved, _ = store.save_intraday_plan_once(
        account_key=plan.account_id,
        session_date=plan.session_date,
        symbol=plan.symbol,
        payload=payload,
        created_at=at - timedelta(hours=1),
    )
    store.create_intraday_run(plan_id=plan.plan_id, created_at=at)
    return saved


def _consume_approval(
    store: SQLiteStateStore,
    plan,
    saved: dict[str, object],
    runtime: IntradayLiveRuntime,
    at: datetime,
    *,
    boot_id_hash: str = "3" * 64,
) -> None:
    run = store.load_intraday_run(plan.plan_id)
    assert run is not None and runtime.writer_fence == run["writer_fence"]
    approved = store.consume_intraday_approval(
        plan_id=plan.plan_id,
        plan_hash=saved["plan_hash"],
        envelope_sha256="1" * 64,
        receipt_sha256="2" * 64,
        interaction_id="123456789012345678",
        boot_id_hash=boot_id_hash,
        approval_generation=1,
        approved_writer_fence=run["writer_fence"],
        writer_id="writer-1",
        writer_fence=run["writer_fence"],
        approved_at=at,
        approval_expires_at=at + timedelta(minutes=5),
        now=at,
    )
    assert approved is not None


def _runtime(
    store: SQLiteStateStore,
    plan,
    clock: FakeClock,
    broker: ScriptedBroker,
    *,
    boot_id_hash: str = "3" * 64,
    order_adapter=None,
) -> IntradayLiveRuntime:
    return IntradayLiveRuntime(
        store=store,
        plan=plan,
        account_key=plan.account_id,
        writer_id="writer-1",
        current_boot_id_hash=boot_id_hash,
        snapshot_reader=broker.read,
        stream_barrier=broker.stream_ack,
        order_adapter=broker if order_adapter is None else order_adapter,
        conditional_adapter=broker,
        personal_topic="personal:order:account-fixture",
        clock=clock,
    )


def _approved_runtime(
    store: SQLiteStateStore,
    plan,
    clock: FakeClock,
    broker: ScriptedBroker,
    *,
    order_adapter=None,
) -> IntradayLiveRuntime:
    saved = _save_run(store, plan, clock())
    runtime = _runtime(store, plan, clock, broker, order_adapter=order_adapter)
    runtime.recover()
    _consume_approval(store, plan, saved, runtime, clock())
    runtime.recover()
    return runtime


def test_recovery_rejects_wall_clock_rollback_before_broker_read(tmp_path) -> None:
    plan = _plan()
    approved_at = plan.entry_start
    clock = FakeClock(approved_at)
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    with SQLiteStateStore(tmp_path / "clock-rollback.sqlite") as store:
        saved = _save_run(store, plan, approved_at)
        runtime = _runtime(store, plan, clock, broker)
        runtime.recover()
        _consume_approval(store, plan, saved, runtime, approved_at)
        clock.value = approved_at - timedelta(seconds=1)

        with pytest.raises(RuntimeError, match="clock_moved_backwards"):
            runtime.recover()

        assert store.load_intraday_run(plan.plan_id)["state"] == "APPROVED"
        assert broker.calls == []


def test_recovery_rejects_stale_broker_snapshot_without_mutation(tmp_path) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start)
    broker = ScriptedBroker(
        _empty_snapshot(plan, clock() - timedelta(seconds=30, microseconds=1))
    )
    with SQLiteStateStore(tmp_path / "stale-snapshot.sqlite") as store:
        saved = _save_run(store, plan, clock())
        runtime = _runtime(store, plan, clock, broker)
        runtime.recover()
        _consume_approval(store, plan, saved, runtime, clock())

        with pytest.raises(RuntimeError, match="broker_snapshot_invalid"):
            runtime.recover()

        assert store.load_intraday_run(plan.plan_id)["state"] == "RECOVERY_REQUIRED"
        assert [name for name, _ in broker.calls if name not in {"snapshot", "stream-ack"}] == []


def test_every_stable_snapshot_pair_requires_a_fresh_stream_barrier(tmp_path) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start)
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    with SQLiteStateStore(tmp_path / "barrier.sqlite") as store:
        runtime = _approved_runtime(store, plan, clock, broker)
        runtime.recover()

    assert [name for name, _ in broker.calls] == [
        "snapshot",
        "stream-ack",
        "snapshot",
        "snapshot",
        "stream-ack",
        "snapshot",
    ]


def _signaled_runtime(
    store: SQLiteStateStore,
    plan,
    clock: FakeClock,
    broker: ScriptedBroker,
    *,
    order_adapter=None,
) -> IntradayLiveRuntime:
    runtime = _approved_runtime(
        store, plan, clock, broker, order_adapter=order_adapter
    )
    runtime.on_stream_frame(
        {
            "type": "ack",
            "request_id": "fixture-1",
            "subscribed": [
                f"trade:us:{plan.symbol}",
                f"orderbook:us:{plan.symbol}",
                "personal:order:account-fixture",
            ],
            "rejected": [],
        }
    )
    runtime.on_stream_frame(
        {
            "type": "trade",
            "topic": f"trade:us:{plan.symbol}",
            "symbol": plan.symbol,
            "price": str(plan.entry_trigger - Decimal("0.01")),
            "captured_at": clock().isoformat(),
        }
    )
    runtime.on_stream_frame(
        {
            "type": "orderbook",
            "topic": f"orderbook:us:{plan.symbol}",
            "symbol": plan.symbol,
            "bid": str(plan.entry_trigger - Decimal("0.01")),
            "ask": str(plan.entry_trigger),
            "captured_at": clock().isoformat(),
        }
    )
    runtime.on_stream_frame(
        {
            "type": "trade",
            "topic": f"trade:us:{plan.symbol}",
            "symbol": plan.symbol,
            "price": str(plan.entry_trigger),
            "captured_at": clock().isoformat(),
        }
    )
    return runtime


def _mark_runtime_dirty(runtime: IntradayLiveRuntime, clock: FakeClock) -> None:
    runtime.on_stream_frame(
        {
            "type": "personal",
            "topic": "personal:order:account-fixture",
            "captured_at": clock().isoformat(),
            "payload": {"fixture": "dirty-reconcile"},
        }
    )


def _drive_to_open_unprotected(
    store: SQLiteStateStore,
    plan,
    clock: FakeClock,
    broker: ScriptedBroker,
) -> IntradayLiveRuntime:
    runtime = _signaled_runtime(store, plan, clock, broker)
    runtime.tick()
    broker.snapshot = BrokerSnapshot(
        symbol=plan.symbol,
        holding_quantity=Decimal(plan.quantity),
        sellable_quantity=Decimal(plan.quantity),
        market_open=True,
        halt_state="CLEAR",
        foreign_activity=False,
        orders=(_entry_observation(plan),),
        buying_power=Decimal("9000"),
        captured_at=clock(),
    )
    runtime.tick()
    assert store.load_intraday_run(plan.plan_id)["state"] == "OPEN_UNPROTECTED"
    return runtime


def _drive_to_protected(
    store: SQLiteStateStore,
    plan,
    clock: FakeClock,
    broker: ScriptedBroker,
) -> IntradayLiveRuntime:
    runtime = _drive_to_open_unprotected(store, plan, clock, broker)
    runtime.tick()
    broker.snapshot = BrokerSnapshot(
        symbol=plan.symbol,
        holding_quantity=Decimal(plan.quantity),
        sellable_quantity=Decimal("0"),
        market_open=True,
        halt_state="CLEAR",
        foreign_activity=False,
        orders=(_entry_observation(plan),),
        conditional_orders=(_watching_oco(plan),),
        captured_at=clock(),
    )
    runtime.tick()
    assert store.load_intraday_run(plan.plan_id)["state"] == "PROTECTED"
    return runtime


def _drive_to_known_protection_unknown(
    store: SQLiteStateStore,
    plan,
    clock: FakeClock,
    broker: ScriptedBroker,
) -> tuple[IntradayLiveRuntime, ConditionalOrderObservation]:
    runtime = _drive_to_open_unprotected(store, plan, clock, broker)
    runtime.tick()
    ordering = replace(_watching_oco(plan), status="ORDERING")
    broker.snapshot = BrokerSnapshot(
        symbol=plan.symbol,
        holding_quantity=Decimal(plan.quantity),
        sellable_quantity=Decimal("0"),
        market_open=True,
        halt_state="CLEAR",
        foreign_activity=False,
        orders=(_entry_observation(plan),),
        conditional_orders=(ordering,),
        captured_at=clock(),
    )
    runtime.tick()
    assert store.load_intraday_run(plan.plan_id)["state"] == "PROTECTION_UNKNOWN"
    return runtime, ordering


def _drive_partial_entry_to_protected(
    store: SQLiteStateStore,
    plan,
    clock: FakeClock,
    broker: ScriptedBroker,
    *,
    owned: Decimal = Decimal("2"),
) -> tuple[IntradayLiveRuntime, BrokerOrderObservation, ConditionalOrderObservation]:
    runtime = _signaled_runtime(store, plan, clock, broker)
    runtime.tick()
    partial = _entry_observation(plan, status="PARTIAL_FILLED", filled=owned)
    broker.snapshot = BrokerSnapshot(
        symbol=plan.symbol,
        holding_quantity=owned,
        sellable_quantity=owned,
        market_open=True,
        halt_state="CLEAR",
        foreign_activity=False,
        orders=(partial,),
        captured_at=clock(),
    )
    runtime.tick()
    runtime.tick()
    cancelled = replace(partial, status="CANCELLED")
    broker.snapshot = replace(broker.snapshot, orders=(cancelled,))
    runtime.tick()
    assert store.load_intraday_run(plan.plan_id)["state"] == "OPEN_UNPROTECTED"

    runtime.tick()
    oco = replace(_watching_oco(plan), quantity=owned)
    broker.snapshot = replace(
        broker.snapshot,
        sellable_quantity=Decimal("0"),
        conditional_orders=(oco,),
    )
    runtime.tick()
    assert store.load_intraday_run(plan.plan_id)["state"] == "PROTECTED"
    return runtime, cancelled, oco


def test_runtime_fake_lifecycle_enters_protects_and_exits_all_owned(tmp_path) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    with SQLiteStateStore(tmp_path / "runtime.sqlite") as store:
        runtime = _approved_runtime(store, plan, clock, broker)
        assert store.load_intraday_run(plan.plan_id)["state"] == "READY_TO_ENTER"
        assert [name for name, _ in broker.calls[:3]] == [
            "snapshot",
            "stream-ack",
            "snapshot",
        ]
        runtime.on_stream_frame(
            {
                "type": "ack",
                "request_id": "fixture-1",
                "subscribed": [
                    f"trade:us:{plan.symbol}",
                    f"orderbook:us:{plan.symbol}",
                    "personal:order:account-fixture",
                ],
                "rejected": [],
            }
        )
        runtime.on_stream_frame(
            {
                "type": "trade",
                "topic": f"trade:us:{plan.symbol}",
                "symbol": plan.symbol,
                "price": str(plan.entry_trigger - Decimal("0.01")),
                "captured_at": clock().isoformat(),
            }
        )
        runtime.on_stream_frame(
            {
                "type": "orderbook",
                "topic": f"orderbook:us:{plan.symbol}",
                "symbol": plan.symbol,
                "bid": str(plan.entry_trigger - Decimal("0.01")),
                "ask": str(plan.entry_trigger),
                "captured_at": clock().isoformat(),
            }
        )
        runtime.on_stream_frame(
            {
                "type": "trade",
                "topic": f"trade:us:{plan.symbol}",
                "symbol": plan.symbol,
                "price": str(plan.entry_trigger),
                "captured_at": clock().isoformat(),
            }
        )

        runtime.tick()
        assert store.load_intraday_run(plan.plan_id)["state"] == "ENTRY_WORKING"
        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=Decimal(plan.quantity),
            sellable_quantity=Decimal(plan.quantity),
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=False,
            orders=(_entry_observation(plan),),
            buying_power=Decimal("9000"),
            captured_at=clock(),
        )
        runtime.tick()
        assert store.load_intraday_run(plan.plan_id)["state"] == "OPEN_UNPROTECTED"

        runtime.tick()
        assert store.load_intraday_run(plan.plan_id)["state"] == "PROTECTION_SUBMITTING"
        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=Decimal(plan.quantity),
            sellable_quantity=Decimal("0"),
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=False,
            orders=(_entry_observation(plan),),
            conditional_orders=(_watching_oco(plan),),
            captured_at=clock(),
        )
        runtime.tick()
        assert store.load_intraday_run(plan.plan_id)["state"] == "PROTECTED"

        clock.value = plan.force_exit_at
        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=Decimal(plan.quantity),
            sellable_quantity=Decimal(plan.quantity),
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=False,
            orders=(_entry_observation(plan),),
            conditional_orders=(_watching_oco(plan),),
            captured_at=clock(),
        )
        runtime.recover()
        assert store.load_intraday_run(plan.plan_id)["state"] == "PROTECTED"
        runtime.tick()
        assert store.load_intraday_run(plan.plan_id)["state"] == "EXIT_CANCELING_PROTECTION"

        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=Decimal(plan.quantity),
            sellable_quantity=Decimal(plan.quantity),
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=False,
            orders=(_entry_observation(plan),),
            captured_at=clock(),
        )
        runtime.tick()
        assert store.load_intraday_run(plan.plan_id)["state"] == "EXIT_WORKING"

        exit_order = BrokerOrderObservation(
            order_id="order-2",
            plan_id=plan.plan_id,
            role="FORCE_EXIT",
            side="SELL",
            status="FILLED",
            quantity=Decimal(plan.quantity),
            filled_quantity=Decimal(plan.quantity),
            client_order_id=None,
            average_fill_price=plan.entry_limit,
        )
        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=Decimal("0"),
            sellable_quantity=Decimal("0"),
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=False,
            orders=(_entry_observation(plan), exit_order),
            captured_at=clock(),
        )
        runtime.tick()
        final = store.load_intraday_run(plan.plan_id)

    assert final["state"] == "CLOSED"
    assert final["owned_qty"] == Decimal("0")
    assert [name for name, _ in broker.calls if name not in {"snapshot", "stream-ack"}] == [
        "place",
        "oco-create",
        "oco-delete",
        "place",
    ]


def test_terminal_oco_fill_reconciles_to_closed_instead_of_sticking_exit_working(
    tmp_path,
) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    with SQLiteStateStore(tmp_path / "terminal-oco.sqlite") as store:
        runtime = _drive_to_open_unprotected(store, plan, clock, broker)
        runtime.tick()
        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=Decimal(plan.quantity),
            sellable_quantity=Decimal("0"),
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=False,
            orders=(_entry_observation(plan),),
            conditional_orders=(_watching_oco(plan),),
            captured_at=clock(),
        )
        runtime.tick()
        assert store.load_intraday_run(plan.plan_id)["state"] == "PROTECTED"

        triggered = BrokerOrderObservation(
            order_id="triggered-exit-1",
            plan_id=plan.plan_id,
            role="TRIGGERED_EXIT",
            side="SELL",
            status="FILLED",
            quantity=Decimal(plan.quantity),
            filled_quantity=Decimal(plan.quantity),
            average_fill_price=plan.target_limit,
        )
        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=Decimal("0"),
            sellable_quantity=Decimal("0"),
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=False,
            orders=(_entry_observation(plan), triggered),
            conditional_orders=(_completed_oco(plan, triggered.order_id),),
            captured_at=clock(),
        )
        runtime.tick()
        final = store.load_intraday_run(plan.plan_id)

    assert final["state"] == "CLOSED"
    assert final["owned_qty"] == Decimal("0")
    assert final["protected_qty"] == Decimal("0")


def test_triggered_exit_competing_with_terminal_local_exit_fails_closed(
    tmp_path,
) -> None:
    plan = _plan()
    quantity = Decimal(plan.quantity)
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    with SQLiteStateStore(tmp_path / "triggered-local-competition.sqlite") as store:
        runtime = _drive_to_protected(store, plan, clock, broker)
        clock.value = plan.force_exit_at
        broker.snapshot = replace(
            broker.snapshot,
            sellable_quantity=quantity,
            captured_at=clock(),
        )
        runtime.recover()
        runtime.tick()
        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=quantity,
            sellable_quantity=quantity,
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=False,
            orders=(_entry_observation(plan),),
            captured_at=clock(),
        )
        runtime.tick()
        assert store.load_intraday_run(plan.plan_id)["state"] == "EXIT_WORKING"

        triggered = BrokerOrderObservation(
            order_id="triggered-exit-1",
            plan_id=plan.plan_id,
            role="TRIGGERED_EXIT",
            side="SELL",
            status="PENDING",
            quantity=quantity,
            filled_quantity=Decimal("0"),
        )
        local = BrokerOrderObservation(
            order_id="order-2",
            plan_id=plan.plan_id,
            role="FORCE_EXIT",
            side="SELL",
            status="FILLED",
            quantity=quantity,
            filled_quantity=quantity,
            average_fill_price=plan.entry_limit,
        )
        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=Decimal("0"),
            sellable_quantity=Decimal("0"),
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=False,
            orders=(_entry_observation(plan), triggered, local),
            conditional_orders=(_completed_oco(plan, triggered.order_id),),
            captured_at=clock(),
        )
        mutations_before = len(
            [name for name, _ in broker.calls if name not in {"snapshot", "stream-ack"}]
        )
        runtime.tick()
        final = store.load_intraday_run(plan.plan_id)
        mutations_after = len(
            [name for name, _ in broker.calls if name not in {"snapshot", "stream-ack"}]
        )

    assert final["state"] == "RECOVERY_REQUIRED"
    assert final["reason_code"] == "competing_exit_orders"
    assert mutations_after == mutations_before


@pytest.mark.parametrize("conditional_status", ["WATCHING", "ORDERING"])
def test_terminal_local_exit_with_active_oco_fails_closed_without_delete(
    tmp_path, conditional_status: str
) -> None:
    plan = _plan()
    quantity = Decimal(plan.quantity)
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    with SQLiteStateStore(
        tmp_path / f"terminal-local-active-oco-{conditional_status}.sqlite"
    ) as store:
        runtime = _drive_to_protected(store, plan, clock, broker)
        clock.value = plan.force_exit_at
        broker.snapshot = replace(
            broker.snapshot,
            sellable_quantity=quantity,
            captured_at=clock(),
        )
        runtime.recover()
        runtime.tick()
        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=quantity,
            sellable_quantity=quantity,
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=False,
            orders=(_entry_observation(plan),),
            captured_at=clock(),
        )
        runtime.tick()
        assert store.load_intraday_run(plan.plan_id)["state"] == "EXIT_WORKING"

        terminal_local = BrokerOrderObservation(
            order_id="order-2",
            plan_id=plan.plan_id,
            role="FORCE_EXIT",
            side="SELL",
            status="FILLED",
            quantity=quantity,
            filled_quantity=quantity,
            average_fill_price=plan.entry_limit,
        )
        active_oco = replace(_watching_oco(plan), status=conditional_status)
        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=Decimal("0"),
            sellable_quantity=Decimal("0"),
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=False,
            orders=(_entry_observation(plan), terminal_local),
            conditional_orders=(active_oco,),
            captured_at=clock(),
        )
        mutations_before = len(
            [name for name, _ in broker.calls if name not in {"snapshot", "stream-ack"}]
        )
        runtime.tick()
        final = store.load_intraday_run(plan.plan_id)
        mutations_after = len(
            [name for name, _ in broker.calls if name not in {"snapshot", "stream-ack"}]
        )

    assert final["state"] == "RECOVERY_REQUIRED"
    assert final["reason_code"] == "conditional_not_cleared"
    assert mutations_after == mutations_before
    assert [name for name, _ in broker.calls].count("oco-delete") == 1


def test_paused_oco_is_deleted_once_instead_of_stalling_cancel_state(tmp_path) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    with SQLiteStateStore(tmp_path / "paused-oco.sqlite") as store:
        runtime = _drive_to_open_unprotected(store, plan, clock, broker)
        runtime.tick()
        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=Decimal(plan.quantity),
            sellable_quantity=Decimal("0"),
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=False,
            orders=(_entry_observation(plan),),
            conditional_orders=(_watching_oco(plan),),
            captured_at=clock(),
        )
        runtime.tick()
        assert store.load_intraday_run(plan.plan_id)["state"] == "PROTECTED"

        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=Decimal(plan.quantity),
            sellable_quantity=Decimal("0"),
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=False,
            orders=(_entry_observation(plan),),
            conditional_orders=(_paused_oco(plan),),
            captured_at=clock(),
        )
        runtime.tick()
        runtime.tick()
        runtime.tick()
        final = store.load_intraday_run(plan.plan_id)

    assert final["state"] == "EXIT_CANCELING_PROTECTION"
    assert [name for name, _ in broker.calls].count("oco-delete") == 1


def test_conditional_create_unknown_is_never_automatically_posted_twice(
    tmp_path,
) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    broker.conditional_create_unknown = True
    with SQLiteStateStore(tmp_path / "conditional-unknown.sqlite") as store:
        runtime = _drive_to_open_unprotected(store, plan, clock, broker)

        runtime.tick()
        assert store.load_intraday_run(plan.plan_id)["state"] == "PROTECTION_UNKNOWN"
        runtime.tick()
        runtime.tick()
        state = store.load_intraday_run(plan.plan_id)["state"]

    assert state == "RECOVERY_REQUIRED"
    assert [name for name, _ in broker.calls].count("oco-create") == 1


def test_rejected_protection_exits_only_after_exact_regular_session_ownership(
    tmp_path,
) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    broker.conditional_create_reject = True
    with SQLiteStateStore(tmp_path / "protection-reject.sqlite") as store:
        runtime = _drive_to_open_unprotected(store, plan, clock, broker)
        runtime.tick()
        assert store.load_intraday_run(plan.plan_id)["state"] == "RECONCILING"
        runtime.tick()
        assert store.load_intraday_run(plan.plan_id)["state"] == "OPEN_UNPROTECTED"

        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=Decimal(plan.quantity),
            sellable_quantity=Decimal("0"),
            orders=(_entry_observation(plan),),
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=False,
            captured_at=clock(),
        )
        runtime.tick()
        assert store.load_intraday_run(plan.plan_id)["state"] == "OPEN_UNPROTECTED"
        assert [name for name, _ in broker.calls].count("place") == 1

        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=Decimal(plan.quantity),
            sellable_quantity=Decimal(plan.quantity),
            orders=(_entry_observation(plan),),
            market_open=False,
            halt_state="CLEAR",
            foreign_activity=False,
            captured_at=clock(),
        )
        runtime.tick()
        assert [name for name, _ in broker.calls].count("place") == 1

        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=Decimal(plan.quantity),
            sellable_quantity=Decimal(plan.quantity),
            orders=(_entry_observation(plan),),
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=False,
            captured_at=clock(),
        )
        runtime.tick()
        state = store.load_intraday_run(plan.plan_id)["state"]

    placed = [value for name, value in broker.calls if name == "place"]
    assert state == "EXIT_WORKING"
    assert len(placed) == 2
    assert placed[-1].side.value == "SELL"
    assert placed[-1].quantity == Decimal(plan.quantity)


def test_unknown_conditional_delete_never_falls_through_to_separate_sell(
    tmp_path,
) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    with SQLiteStateStore(tmp_path / "delete-unknown.sqlite") as store:
        runtime = _drive_to_open_unprotected(store, plan, clock, broker)
        runtime.tick()
        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=Decimal(plan.quantity),
            sellable_quantity=Decimal("0"),
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=False,
            orders=(_entry_observation(plan),),
            conditional_orders=(_watching_oco(plan),),
            captured_at=clock(),
        )
        runtime.tick()
        assert store.load_intraday_run(plan.plan_id)["state"] == "PROTECTED"

        clock.value = plan.force_exit_at
        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=Decimal(plan.quantity),
            sellable_quantity=Decimal(plan.quantity),
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=False,
            orders=(_entry_observation(plan),),
            conditional_orders=(_watching_oco(plan),),
            captured_at=clock(),
        )
        runtime.recover()
        broker.conditional_delete_unknown = True
        runtime.tick()
        assert store.load_intraday_run(plan.plan_id)["state"] == "EXIT_CANCELING_PROTECTION"

        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=Decimal(plan.quantity),
            sellable_quantity=Decimal(plan.quantity),
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=False,
            orders=(_entry_observation(plan),),
            captured_at=clock(),
        )
        runtime.tick()
        state = store.load_intraday_run(plan.plan_id)["state"]

    mutation_names = [
        name for name, _ in broker.calls if name not in {"snapshot", "stream-ack"}
    ]
    assert state == "RECOVERY_REQUIRED"
    assert mutation_names == ["place", "oco-create", "oco-delete"]


def test_general_create_unknown_uses_one_exact_identity_recovery(tmp_path) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    broker.general_outcomes = ["unknown", "ack"]
    with SQLiteStateStore(tmp_path / "identity-recovery.sqlite") as store:
        runtime = _signaled_runtime(store, plan, clock, broker)

        runtime.tick()
        assert store.load_intraday_run(plan.plan_id)["state"] == "ENTRY_UNKNOWN"
        runtime.tick()
        state = store.load_intraday_run(plan.plan_id)["state"]

    placed = [value for name, value in broker.calls if name == "place"]
    assert state == "ENTRY_WORKING"
    assert len(placed) == 2
    assert placed[0].idempotency_key == placed[1].idempotency_key
    assert placed[0].symbol == placed[1].symbol
    assert placed[0].quantity == placed[1].quantity
    assert placed[0].limit_price == placed[1].limit_price


def test_entry_identity_recovery_rechecks_real_adapter_projection(
    tmp_path, monkeypatch
) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    adapter = TossLiveBrokerAdapter(object())
    external_posts = []

    def fake_place(intent):
        external_posts.append(intent)
        if len(external_posts) == 1:
            raise LiveBrokerError("scripted response loss", unknown_state=True)
        return BrokerOrderTicket(
            broker_order_id="unexpected-second-post",
            status=ExecutionStatus.ACKNOWLEDGED,
        )

    monkeypatch.setattr(adapter, "place_order", fake_place)
    with SQLiteStateStore(tmp_path / "entry-recovery-projection.sqlite") as store:
        runtime = _signaled_runtime(
            store,
            plan,
            clock,
            broker,
            order_adapter=adapter,
        )
        runtime.tick()
        assert store.load_intraday_run(plan.plan_id)["state"] == "ENTRY_UNKNOWN"
        assert len(external_posts) == 1

        adapter.confirm_high_value_order = True
        with pytest.raises(RuntimeError, match="adapter_request_projection_mismatch"):
            runtime.tick()
        assert len(external_posts) == 1

        runtime.tick()
        runtime.tick()
        final = store.load_intraday_run(plan.plan_id)

    assert final["state"] == "RECOVERY_REQUIRED"
    assert len(external_posts) == 1


def test_general_create_unknown_after_local_deadline_never_posts_again(
    tmp_path,
) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    broker.general_outcomes = ["unknown"]
    with SQLiteStateStore(tmp_path / "identity-expired.sqlite") as store:
        runtime = _signaled_runtime(store, plan, clock, broker)
        runtime.tick()
        assert store.load_intraday_run(plan.plan_id)["state"] == "ENTRY_UNKNOWN"

        clock.value += timedelta(minutes=8, microseconds=1)
        broker.snapshot = _empty_snapshot(plan, clock())
        runtime.recover()
        runtime.tick()
        state = store.load_intraday_run(plan.plan_id)["state"]

    assert state == "RECOVERY_REQUIRED"
    assert [name for name, _ in broker.calls].count("place") == 1


def test_partial_fill_is_persisted_before_one_shot_cancel_and_late_fill(
    tmp_path,
) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    with SQLiteStateStore(tmp_path / "partial-fill.sqlite") as store:
        runtime = _signaled_runtime(store, plan, clock, broker)
        runtime.tick()
        partial = _entry_observation(
            plan, status="PARTIAL_FILLED", filled=Decimal("2")
        )
        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=Decimal("2"),
            sellable_quantity=Decimal("2"),
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=False,
            orders=(partial,),
            captured_at=clock(),
        )

        runtime.tick()
        run = store.load_intraday_run(plan.plan_id)
        order = store.load_execution_order(run["entry_intent_id"])
        assert run["state"] == "ENTRY_WORKING"
        assert run["owned_qty"] == Decimal("2")
        assert order["filled_quantity"] == Decimal("2")
        assert [name for name, _ in broker.calls].count("cancel") == 0

        runtime.tick()
        assert store.load_intraday_run(plan.plan_id)["state"] == "ENTRY_CANCELING"
        pending_cancel_fill = _entry_observation(
            plan, status="PENDING_CANCEL", filled=Decimal("3")
        )
        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=Decimal("3"),
            sellable_quantity=Decimal("3"),
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=False,
            orders=(pending_cancel_fill,),
            captured_at=clock(),
        )
        runtime.tick()
        pending_run = store.load_intraday_run(plan.plan_id)
        pending_order = store.load_execution_order(pending_run["entry_intent_id"])
        assert pending_run["state"] == "ENTRY_CANCELING"
        assert pending_run["owned_qty"] == Decimal("3")
        assert pending_order["filled_quantity"] == Decimal("3")

        late_fill = _entry_observation(
            plan, status="CANCELLED", filled=Decimal("3")
        )
        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=Decimal("3"),
            sellable_quantity=Decimal("3"),
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=False,
            orders=(late_fill,),
            captured_at=clock(),
        )
        runtime.tick()
        final = store.load_intraday_run(plan.plan_id)
        order = store.load_execution_order(final["entry_intent_id"])

    assert final["state"] == "OPEN_UNPROTECTED"
    assert final["owned_qty"] == Decimal("3")
    assert order["filled_quantity"] == Decimal("3")
    assert [name for name, _ in broker.calls].count("cancel") == 1


def test_unknown_entry_cancel_is_never_called_twice(tmp_path) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    with SQLiteStateStore(tmp_path / "cancel-unknown.sqlite") as store:
        runtime = _signaled_runtime(store, plan, clock, broker)
        runtime.tick()
        partial = _entry_observation(
            plan, status="PARTIAL_FILLED", filled=Decimal("2")
        )
        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=Decimal("2"),
            sellable_quantity=Decimal("2"),
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=False,
            orders=(partial,),
            captured_at=clock(),
        )
        runtime.tick()
        broker.cancel_unknown = True
        runtime.tick()
        assert store.load_intraday_run(plan.plan_id)["state"] == "ENTRY_CANCELING"

        runtime.tick()
        runtime.tick()
        state = store.load_intraday_run(plan.plan_id)["state"]

    assert state == "ENTRY_CANCELING"
    assert [name for name, _ in broker.calls].count("cancel") == 1


@pytest.mark.parametrize("latch", ["entry_disabled_at", "loss_fuse_at"])
def test_persisted_entry_latches_block_buy_even_with_valid_approval(
    tmp_path,
    latch: str,
) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    with SQLiteStateStore(tmp_path / f"{latch}.sqlite") as store:
        runtime = _approved_runtime(store, plan, clock, broker)
        run = store.load_intraday_run(plan.plan_id)
        updates: dict[str, object] = {latch: clock()}
        if latch == "entry_disabled_at":
            updates["entry_disabled_reason"] = "operator_latch"
        latched = store.cas_intraday_run(
            plan_id=plan.plan_id,
            expected_state="READY_TO_ENTER",
            expected_version=run["version"],
            next_state="RECONCILING",
            writer_id="writer-1",
            writer_fence=runtime.writer_fence,
            event_type="entry_latch_set",
            updates=updates,
            now=clock(),
        )
        assert latched is not None

        runtime.recover()
        final = store.load_intraday_run(plan.plan_id)

    assert final["state"] == "PLANNED"
    assert [name for name, _ in broker.calls].count("place") == 0


def test_unknown_entry_is_not_reposted_at_or_after_entry_expiry(tmp_path) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_expiry - timedelta(seconds=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    broker.general_outcomes = ["unknown"]
    with SQLiteStateStore(tmp_path / "entry-expiry-recovery.sqlite") as store:
        runtime = _signaled_runtime(store, plan, clock, broker)
        runtime.tick()
        assert store.load_intraday_run(plan.plan_id)["state"] == "ENTRY_UNKNOWN"

        clock.value = plan.entry_expiry
        broker.snapshot = _empty_snapshot(plan, clock())
        runtime.tick()
        final = store.load_intraday_run(plan.plan_id)

    assert final["state"] == "RECOVERY_REQUIRED"
    assert [name for name, _ in broker.calls].count("place") == 1


def test_unknown_entry_does_not_adopt_order_without_exact_client_identity(
    tmp_path,
) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    broker.general_outcomes = ["unknown"]
    with SQLiteStateStore(tmp_path / "foreign-entry.sqlite") as store:
        runtime = _signaled_runtime(store, plan, clock, broker)
        runtime.tick()
        foreign = BrokerOrderObservation(
            order_id="foreign-entry",
            plan_id=plan.plan_id,
            role="ENTRY",
            side="BUY",
            status="FILLED",
            quantity=Decimal(plan.quantity),
            filled_quantity=Decimal(plan.quantity),
            client_order_id=None,
            average_fill_price=plan.entry_limit,
        )
        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=Decimal(plan.quantity),
            sellable_quantity=Decimal(plan.quantity),
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=False,
            orders=(foreign,),
            captured_at=clock(),
        )

        runtime.tick()
        final = store.load_intraday_run(plan.plan_id)
        execution = store.load_execution_order(final["entry_intent_id"])

    assert final["state"] == "RECOVERY_REQUIRED"
    assert execution["broker_order_id"] is None
    assert [name for name, _ in broker.calls].count("place") == 1


def test_expired_approval_blocks_ready_entry_send(tmp_path) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    with SQLiteStateStore(tmp_path / "expired-approval.sqlite") as store:
        runtime = _signaled_runtime(store, plan, clock, broker)
        assert store.load_intraday_run(plan.plan_id)["state"] == "READY_TO_ENTER"

        approved_at = clock()
        for seconds in range(30, 300, 30):
            assert store.renew_intraday_writer(
                plan_id=plan.plan_id,
                writer_id="writer-1",
                writer_fence=runtime.writer_fence,
                now=approved_at + timedelta(seconds=seconds),
            ) is not None
        clock.value = approved_at + timedelta(minutes=5)
        runtime.tick()
        final = store.load_intraday_run(plan.plan_id)

    assert final["state"] == "RECONCILING"
    assert [name for name, _ in broker.calls].count("place") == 0


def test_runtime_rejects_adapter_that_enables_high_value_confirmation() -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start)
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    broker.confirm_high_value_order = True

    with SQLiteStateStore() as store, pytest.raises(
        ValueError, match="confirmHighValueOrder=false"
    ):
        _runtime(store, plan, clock, broker)


def test_entry_cancel_reserved_then_fenced_never_calls_broker(
    tmp_path, monkeypatch
) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    with SQLiteStateStore(tmp_path / "cancel-fenced.sqlite") as store:
        runtime = _signaled_runtime(store, plan, clock, broker)
        runtime.tick()
        partial = _entry_observation(
            plan, status="PARTIAL_FILLED", filled=Decimal("2")
        )
        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=Decimal("2"),
            sellable_quantity=Decimal("2"),
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=False,
            orders=(partial,),
            captured_at=clock(),
        )
        runtime.tick()
        reserve = store.reserve_intraday_action_event

        def reserve_then_take_over(**kwargs):
            reserved = reserve(**kwargs)
            assert reserved is not None
            clock.value += timedelta(seconds=46)
            assert store.claim_intraday_writer(
                plan_id=plan.plan_id,
                writer_id="writer-race",
                now=clock(),
            ) is not None
            return reserved

        monkeypatch.setattr(
            store, "reserve_intraday_action_event", reserve_then_take_over
        )
        with pytest.raises(RuntimeError, match="entry_cancel_not_sendable"):
            runtime.tick()

    assert [name for name, _ in broker.calls].count("cancel") == 0


def test_conditional_delete_reserved_then_fenced_never_calls_broker(
    tmp_path, monkeypatch
) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    with SQLiteStateStore(tmp_path / "conditional-delete-fenced.sqlite") as store:
        runtime = _drive_to_protected(store, plan, clock, broker)
        clock.value = plan.force_exit_at
        broker.snapshot = replace(
            broker.snapshot,
            sellable_quantity=Decimal(plan.quantity),
            captured_at=clock(),
        )
        runtime.recover()
        reserve = store.reserve_intraday_action_event

        def reserve_then_take_over(**kwargs):
            reserved = reserve(**kwargs)
            assert reserved is not None
            clock.value += timedelta(seconds=46)
            assert store.claim_intraday_writer(
                plan_id=plan.plan_id,
                writer_id="writer-race",
                now=clock(),
            ) is not None
            return reserved

        monkeypatch.setattr(
            store, "reserve_intraday_action_event", reserve_then_take_over
        )
        with pytest.raises(RuntimeError, match="conditional_cancel_not_sendable"):
            runtime.tick()

    assert [name for name, _ in broker.calls].count("oco-delete") == 0


def test_restart_does_not_resend_latched_entry_cancel(tmp_path) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    with SQLiteStateStore(tmp_path / "entry-cancel-restart.sqlite") as store:
        runtime = _signaled_runtime(store, plan, clock, broker)
        runtime.tick()
        partial = _entry_observation(
            plan, status="PARTIAL_FILLED", filled=Decimal("2")
        )
        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=Decimal("2"),
            sellable_quantity=Decimal("2"),
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=False,
            orders=(partial,),
            captured_at=clock(),
        )
        runtime.tick()
        broker.cancel_unknown = True
        runtime.tick()
        assert [name for name, _ in broker.calls].count("cancel") == 1

        clock.value += timedelta(seconds=46)
        broker.snapshot = replace(broker.snapshot, captured_at=clock())
        restarted = _runtime(store, plan, clock, broker)
        restarted.recover()
        restarted.tick()
        restarted.tick()
        final = store.load_intraday_run(plan.plan_id)

    assert final["state"] == "ENTRY_CANCELING"
    assert [name for name, _ in broker.calls].count("cancel") == 1


def test_restart_does_not_resend_latched_conditional_delete(tmp_path) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    with SQLiteStateStore(tmp_path / "conditional-delete-restart.sqlite") as store:
        runtime = _drive_to_protected(store, plan, clock, broker)
        clock.value = plan.force_exit_at
        broker.snapshot = replace(
            broker.snapshot,
            sellable_quantity=Decimal(plan.quantity),
            captured_at=clock(),
        )
        runtime.recover()
        broker.conditional_delete_unknown = True
        runtime.tick()
        assert [name for name, _ in broker.calls].count("oco-delete") == 1

        clock.value += timedelta(seconds=46)
        broker.snapshot = replace(broker.snapshot, captured_at=clock())
        restarted = _runtime(store, plan, clock, broker)
        restarted.recover()
        restarted.tick()
        restarted.tick()
        final = store.load_intraday_run(plan.plan_id)

    assert final["state"] == "EXIT_CANCELING_PROTECTION"
    assert [name for name, _ in broker.calls].count("oco-delete") == 1


def test_restart_reconciling_restores_known_protection_unknown(tmp_path) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    with SQLiteStateStore(tmp_path / "restart-protection-unknown.sqlite") as store:
        _drive_to_known_protection_unknown(store, plan, clock, broker)
        mutations_before = len(
            [name for name, _ in broker.calls if name not in {"snapshot", "stream-ack"}]
        )
        clock.value += timedelta(seconds=46)
        broker.snapshot = replace(broker.snapshot, captured_at=clock())

        restarted = _runtime(store, plan, clock, broker)
        restarted.recover()
        final = store.load_intraday_run(plan.plan_id)
        mutations_after = len(
            [name for name, _ in broker.calls if name not in {"snapshot", "stream-ack"}]
        )

    assert final["state"] == "PROTECTION_UNKNOWN"
    assert mutations_after == mutations_before


def test_restart_reconciling_restores_exit_unknown_without_repost(tmp_path) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    broker.conditional_create_reject = True
    with SQLiteStateStore(tmp_path / "restart-exit-unknown.sqlite") as store:
        runtime = _drive_to_open_unprotected(store, plan, clock, broker)
        runtime.tick()
        runtime.tick()
        broker.general_outcomes = ["unknown"]
        runtime.tick()
        assert store.load_intraday_run(plan.plan_id)["state"] == "EXIT_UNKNOWN"
        mutations_before = len(
            [name for name, _ in broker.calls if name not in {"snapshot", "stream-ack"}]
        )
        clock.value += timedelta(seconds=46)
        broker.snapshot = replace(broker.snapshot, captured_at=clock())

        restarted = _runtime(store, plan, clock, broker)
        restarted.recover()
        final = store.load_intraday_run(plan.plan_id)
        mutations_after = len(
            [name for name, _ in broker.calls if name not in {"snapshot", "stream-ack"}]
        )

    assert final["state"] == "EXIT_UNKNOWN"
    assert mutations_after == mutations_before


def test_exit_unknown_with_known_broker_id_is_never_reposted(tmp_path) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    broker.conditional_create_reject = True
    with SQLiteStateStore(tmp_path / "known-exit-id.sqlite") as store:
        runtime = _drive_to_open_unprotected(store, plan, clock, broker)
        runtime.tick()
        runtime.tick()
        broker.general_outcomes = ["unknown"]
        runtime.tick()
        unknown = store.load_intraday_run(plan.plan_id)
        assert unknown["state"] == "EXIT_UNKNOWN"
        assert [name for name, _ in broker.calls].count("place") == 2

        with store._conn:
            store._conn.execute(
                "UPDATE execution_orders SET broker_order_id = ? WHERE intent_id = ?",
                ("known-exit-order", unknown["active_exit_intent_id"]),
            )
        runtime.tick()
        runtime.tick()
        final = store.load_intraday_run(plan.plan_id)

    assert final["state"] == "EXIT_UNKNOWN"
    assert [name for name, _ in broker.calls].count("place") == 2


def test_exit_working_known_order_unknown_projects_without_repost(tmp_path) -> None:
    plan = _plan()
    quantity = Decimal(plan.quantity)
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    with SQLiteStateStore(tmp_path / "known-exit-regressed-unknown.sqlite") as store:
        runtime = _drive_to_protected(store, plan, clock, broker)
        clock.value = plan.force_exit_at
        broker.snapshot = replace(
            broker.snapshot,
            sellable_quantity=quantity,
            captured_at=clock(),
        )
        runtime.recover()
        runtime.tick()
        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=quantity,
            sellable_quantity=quantity,
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=False,
            orders=(_entry_observation(plan),),
            captured_at=clock(),
        )
        runtime.tick()
        working = store.load_intraday_run(plan.plan_id)
        execution = store.load_execution_order(working["active_exit_intent_id"])
        intent = store.load_intraday_order_intent(working["active_exit_intent_id"])
        assert working["state"] == "EXIT_WORKING"
        assert execution["broker_order_id"] == "order-2"

        unknown_order = BrokerOrderObservation(
            order_id="order-2",
            plan_id=plan.plan_id,
            role="FORCE_EXIT",
            side="SELL",
            status="UNKNOWN",
            quantity=quantity,
            filled_quantity=Decimal("0"),
            client_order_id=intent["idempotency_key"],
        )
        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=quantity,
            sellable_quantity=Decimal("0"),
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=False,
            orders=(_entry_observation(plan), unknown_order),
            captured_at=clock(),
        )
        places_before = [value for name, value in broker.calls if name == "place"]
        runtime.tick()
        projected = store.load_intraday_run(plan.plan_id)
        projected_execution = store.load_execution_order(
            projected["active_exit_intent_id"]
        )
        assert projected["state"] == "EXIT_UNKNOWN"
        assert projected_execution["status"] == "UNKNOWN"
        assert projected_execution["broker_order_id"] == "order-2"

        runtime.tick()
        runtime.tick()
        final = store.load_intraday_run(plan.plan_id)
        places_after = [value for name, value in broker.calls if name == "place"]

    assert final["state"] == "EXIT_UNKNOWN"
    assert len(places_after) == len(places_before) == 2


@pytest.mark.parametrize(
    "local_roles",
    [
        ("FORCE_EXIT", "FORCE_EXIT"),
        ("FORCE_EXIT", "EMERGENCY_EXIT"),
    ],
    ids=["same-role", "mixed-role"],
)
def test_multiple_active_local_exit_ids_fail_to_durable_recovery(
    tmp_path, local_roles: tuple[str, str]
) -> None:
    plan = _plan()
    quantity = Decimal(plan.quantity)
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    with SQLiteStateStore(tmp_path / f"multiple-local-{'-'.join(local_roles)}.sqlite") as store:
        runtime = _drive_to_protected(store, plan, clock, broker)
        clock.value = plan.force_exit_at
        broker.snapshot = replace(
            broker.snapshot,
            sellable_quantity=quantity,
            captured_at=clock(),
        )
        runtime.recover()
        runtime.tick()

        broker.general_outcomes = ["unknown"]
        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=quantity,
            sellable_quantity=quantity,
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=False,
            orders=(_entry_observation(plan),),
            captured_at=clock(),
        )
        runtime.tick()
        unknown = store.load_intraday_run(plan.plan_id)
        execution = store.load_execution_order(unknown["active_exit_intent_id"])
        intent = store.load_intraday_order_intent(unknown["active_exit_intent_id"])
        assert unknown["state"] == "EXIT_UNKNOWN"
        assert execution["broker_order_id"] is None
        assert intent["order_role"] == "FORCE_EXIT"

        competing = tuple(
            BrokerOrderObservation(
                order_id=f"competing-local-{index}",
                plan_id=plan.plan_id,
                role=role,
                side="SELL",
                status="PENDING",
                quantity=quantity,
                filled_quantity=Decimal("0"),
                client_order_id=intent["idempotency_key"],
            )
            for index, role in enumerate(local_roles, start=1)
        )
        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=quantity,
            sellable_quantity=Decimal("0"),
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=False,
            orders=(_entry_observation(plan), *competing),
            captured_at=clock(),
        )
        mutations_before = len(
            [name for name, _ in broker.calls if name not in {"snapshot", "stream-ack"}]
        )
        runtime.tick()
        final = store.load_intraday_run(plan.plan_id)
        mutations_after = len(
            [name for name, _ in broker.calls if name not in {"snapshot", "stream-ack"}]
        )

    assert final["state"] == "RECOVERY_REQUIRED"
    assert final["reason_code"] == "multiple_local_exit_orders"
    assert mutations_after == mutations_before


def test_candidate_only_oco_is_not_adopted_without_durable_broker_id(tmp_path) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    with SQLiteStateStore(tmp_path / "candidate-only-oco.sqlite") as store:
        runtime = _drive_to_open_unprotected(store, plan, clock, broker)
        broker.snapshot = replace(
            broker.snapshot,
            sellable_quantity=Decimal("0"),
            conditional_orders=(_watching_oco(plan),),
        )
        mutations_before = len(
            [name for name, _ in broker.calls if name not in {"snapshot", "stream-ack"}]
        )
        runtime.tick()
        final = store.load_intraday_run(plan.plan_id)

    mutations_after = len(
        [name for name, _ in broker.calls if name not in {"snapshot", "stream-ack"}]
    )
    assert final["state"] == "RECOVERY_REQUIRED"
    assert mutations_after == mutations_before


def test_oco_broker_id_mismatch_fails_closed_without_delete(tmp_path) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    with SQLiteStateStore(tmp_path / "oco-id-mismatch.sqlite") as store:
        runtime = _drive_to_protected(store, plan, clock, broker)
        broker.snapshot = replace(
            broker.snapshot,
            conditional_orders=(
                replace(_watching_oco(plan), conditional_order_id="other-oco-id"),
            ),
        )
        runtime.tick()
        final = store.load_intraday_run(plan.plan_id)

    assert final["state"] == "RECOVERY_REQUIRED"
    assert [name for name, _ in broker.calls].count("oco-delete") == 0


@pytest.mark.parametrize("mismatch", ["oversized_trigger", "parent_quantity"])
def test_triggered_oco_requires_exact_trigger_and_parent_quantity(
    tmp_path, mismatch: str
) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    with SQLiteStateStore(tmp_path / f"trigger-{mismatch}.sqlite") as store:
        runtime = _drive_to_protected(store, plan, clock, broker)
        conditional = _completed_oco(plan, "triggered-exit-1")
        triggered_quantity = Decimal(plan.quantity)
        if mismatch == "oversized_trigger":
            triggered_quantity += Decimal("1")
        else:
            triggered_quantity -= Decimal("1")
            conditional = replace(conditional, quantity=triggered_quantity)
        triggered = BrokerOrderObservation(
            order_id="triggered-exit-1",
            plan_id=plan.plan_id,
            role="TRIGGERED_EXIT",
            side="SELL",
            status="PENDING",
            quantity=triggered_quantity,
            filled_quantity=Decimal("0"),
        )
        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=Decimal(plan.quantity),
            sellable_quantity=Decimal("0"),
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=False,
            orders=(_entry_observation(plan), triggered),
            conditional_orders=(conditional,),
            captured_at=clock(),
        )
        runtime.tick()
        final = store.load_intraday_run(plan.plan_id)

    assert final["state"] == "RECOVERY_REQUIRED"
    assert [name for name, _ in broker.calls].count("place") == 1


def test_expired_exact_oco_submits_one_full_emergency_exit(tmp_path) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    with SQLiteStateStore(tmp_path / "expired-oco.sqlite") as store:
        runtime = _drive_to_protected(store, plan, clock, broker)
        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=Decimal(plan.quantity),
            sellable_quantity=Decimal(plan.quantity),
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=False,
            orders=(_entry_observation(plan),),
            conditional_orders=(_expired_oco(plan),),
            captured_at=clock(),
        )
        runtime.tick()
        final = store.load_intraday_run(plan.plan_id)

    placed = [value for name, value in broker.calls if name == "place"]
    assert final["state"] == "EXIT_WORKING"
    assert len(placed) == 2
    assert placed[-1].side.value == "SELL"
    assert placed[-1].quantity == Decimal(plan.quantity)
    assert placed[-1].reason == "emergency_exit"
    assert [name for name, _ in broker.calls].count("oco-delete") == 0


def test_protection_unknown_exact_expired_oco_exits_in_same_tick(tmp_path) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    with SQLiteStateStore(tmp_path / "protection-unknown-expired.sqlite") as store:
        runtime, _ = _drive_to_known_protection_unknown(store, plan, clock, broker)
        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=Decimal(plan.quantity),
            sellable_quantity=Decimal(plan.quantity),
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=False,
            orders=(_entry_observation(plan),),
            conditional_orders=(_expired_oco(plan),),
            captured_at=clock(),
        )
        runtime.tick()
        final = store.load_intraday_run(plan.plan_id)

    placed = [value for name, value in broker.calls if name == "place"]
    assert final["state"] == "EXIT_WORKING"
    assert len(placed) == 2
    assert placed[-1].side.value == "SELL"
    assert placed[-1].quantity == Decimal(plan.quantity)
    assert placed[-1].reason == "emergency_exit"
    assert [name for name, _ in broker.calls].count("oco-delete") == 0


def test_expired_oco_exit_unknown_gets_one_bounded_identity_recovery(tmp_path) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    with SQLiteStateStore(tmp_path / "expired-oco-exit-recovery.sqlite") as store:
        runtime = _drive_to_protected(store, plan, clock, broker)
        broker.general_outcomes = ["unknown", "ack"]
        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=Decimal(plan.quantity),
            sellable_quantity=Decimal(plan.quantity),
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=False,
            orders=(_entry_observation(plan),),
            conditional_orders=(_expired_oco(plan),),
            captured_at=clock(),
        )

        runtime.tick()
        assert store.load_intraday_run(plan.plan_id)["state"] == "EXIT_UNKNOWN"
        runtime.tick()
        assert store.load_intraday_run(plan.plan_id)["state"] == "EXIT_WORKING"
        runtime.tick()
        runtime.tick()
        final = store.load_intraday_run(plan.plan_id)
        recovery_events = [
            event
            for event in store.list_execution_events(
                intent_id=str(final["active_exit_intent_id"])
            )
            if event["event_type"] == "identity_recovery_send_reserved"
        ]

    placed = [value for name, value in broker.calls if name == "place"]
    assert final["state"] == "EXIT_WORKING"
    assert len(placed) == 3
    assert placed[1].idempotency_key == placed[2].idempotency_key
    assert placed[1].quantity == placed[2].quantity == Decimal(plan.quantity)
    assert placed[1].reason == placed[2].reason == "emergency_exit"
    assert len(recovery_events) == 1


def test_nonterminal_entry_competing_with_terminal_exit_cannot_close(tmp_path) -> None:
    plan = _plan()
    owned = Decimal("2")
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    with SQLiteStateStore(tmp_path / "entry-exit-competition.sqlite") as store:
        runtime, cancelled_entry, oco = _drive_partial_entry_to_protected(
            store, plan, clock, broker, owned=owned
        )
        clock.value = plan.force_exit_at
        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=owned,
            sellable_quantity=owned,
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=False,
            orders=(cancelled_entry,),
            conditional_orders=(oco,),
            captured_at=clock(),
        )
        runtime.recover()
        runtime.tick()
        broker.snapshot = replace(broker.snapshot, conditional_orders=())
        runtime.tick()
        assert store.load_intraday_run(plan.plan_id)["state"] == "EXIT_WORKING"

        pending_cancel = replace(cancelled_entry, status="PENDING_CANCEL")
        terminal_exit = BrokerOrderObservation(
            order_id="order-2",
            plan_id=plan.plan_id,
            role="FORCE_EXIT",
            side="SELL",
            status="FILLED",
            quantity=owned,
            filled_quantity=owned,
            average_fill_price=plan.entry_limit,
        )
        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=Decimal("0"),
            sellable_quantity=Decimal("0"),
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=False,
            orders=(pending_cancel, terminal_exit),
            captured_at=clock(),
        )
        runtime.tick()
        final = store.load_intraday_run(plan.plan_id)

    assert final["state"] == "RECOVERY_REQUIRED"
    assert final["state"] != "CLOSED"


@pytest.mark.parametrize("entry_status", ["PENDING_CANCEL", "UNKNOWN"])
def test_nonterminal_entry_competing_with_exact_active_oco_cannot_protect(
    tmp_path, entry_status: str
) -> None:
    plan = _plan()
    owned = Decimal("2")
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    with SQLiteStateStore(tmp_path / f"entry-oco-{entry_status}.sqlite") as store:
        runtime, cancelled_entry, oco = _drive_partial_entry_to_protected(
            store, plan, clock, broker, owned=owned
        )
        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=owned,
            sellable_quantity=Decimal("0"),
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=False,
            orders=(replace(cancelled_entry, status=entry_status),),
            conditional_orders=(oco,),
            captured_at=clock(),
        )
        runtime.tick()
        final = store.load_intraday_run(plan.plan_id)

    assert final["state"] == "RECOVERY_REQUIRED"
    assert final["state"] != "PROTECTED"


def test_dirty_entry_working_foreign_activity_is_durably_quarantined(
    tmp_path,
) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    with SQLiteStateStore(tmp_path / "dirty-entry-foreign.sqlite") as store:
        runtime = _signaled_runtime(store, plan, clock, broker)
        runtime.tick()
        assert store.load_intraday_run(plan.plan_id)["state"] == "ENTRY_WORKING"
        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=Decimal("0"),
            sellable_quantity=Decimal("0"),
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=True,
            orders=(_entry_observation(plan, status="PENDING"),),
            captured_at=clock(),
        )
        _mark_runtime_dirty(runtime, clock)
        runtime.tick()
        final = store.load_intraday_run(plan.plan_id)

    assert final["state"] == "RECOVERY_REQUIRED"
    assert final["reason_code"] == "foreign_account_activity"


@pytest.mark.parametrize(
    ("variant", "expected_reason"),
    [
        ("foreign", "foreign_account_activity"),
        ("identity", "conditional_identity_mismatch"),
        ("cardinality", "multiple_conditional_candidates"),
    ],
)
def test_dirty_protection_submitting_anomaly_is_durably_quarantined(
    tmp_path, variant: str, expected_reason: str
) -> None:
    plan = _plan()
    quantity = Decimal(plan.quantity)
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    with SQLiteStateStore(tmp_path / f"dirty-protection-{variant}.sqlite") as store:
        runtime = _drive_to_open_unprotected(store, plan, clock, broker)
        runtime.tick()
        assert store.load_intraday_run(plan.plan_id)["state"] == "PROTECTION_SUBMITTING"

        conditional_orders: tuple[ConditionalOrderObservation, ...] = ()
        foreign_activity = variant == "foreign"
        if variant == "identity":
            conditional_orders = (
                replace(_watching_oco(plan), conditional_order_id="wrong-oco-id"),
            )
        elif variant == "cardinality":
            conditional_orders = (
                _watching_oco(plan),
                replace(_watching_oco(plan), conditional_order_id="second-oco-id"),
            )
        broker.snapshot = BrokerSnapshot(
            symbol=plan.symbol,
            holding_quantity=quantity,
            sellable_quantity=Decimal("0"),
            market_open=True,
            halt_state="CLEAR",
            foreign_activity=foreign_activity,
            orders=(_entry_observation(plan),),
            conditional_orders=conditional_orders,
            captured_at=clock(),
        )
        _mark_runtime_dirty(runtime, clock)
        runtime.tick()
        final = store.load_intraday_run(plan.plan_id)

    assert final["state"] == "RECOVERY_REQUIRED"
    assert final["reason_code"] == expected_reason


@pytest.mark.parametrize(
    ("variant", "expected_state", "expected_reason"),
    [
        ("foreign", "RECOVERY_REQUIRED", "foreign_account_activity"),
        ("expired_approval", "PLANNED", "approval_invalid"),
    ],
)
def test_dirty_ready_state_reconciles_to_durable_safe_state(
    tmp_path, variant: str, expected_state: str, expected_reason: str
) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    with SQLiteStateStore(tmp_path / f"dirty-ready-{variant}.sqlite") as store:
        runtime = _approved_runtime(store, plan, clock, broker)
        assert store.load_intraday_run(plan.plan_id)["state"] == "READY_TO_ENTER"
        if variant == "foreign":
            broker.snapshot = replace(broker.snapshot, foreign_activity=True)
        else:
            with store._conn:
                store._conn.execute(
                    "UPDATE intraday_runs SET approval_expires_at = ? WHERE plan_id = ?",
                    (
                        (clock() - timedelta(seconds=1)).isoformat(),
                        plan.plan_id,
                    ),
                )
        _mark_runtime_dirty(runtime, clock)
        runtime.tick()
        final = store.load_intraday_run(plan.plan_id)

    assert final["state"] == expected_state
    assert final["reason_code"] == expected_reason
