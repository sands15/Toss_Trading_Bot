from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from turtle_bot.intraday_live import (
    BrokerSnapshot,
    IntradayLiveRuntime,
    IntradayRuntimeError,
)
from turtle_bot.live_order import BrokerOrderTicket
from turtle_bot.state_store import SQLiteStateStore

from test_intraday_live import (
    FakeClock,
    ScriptedBroker,
    _drive_to_open_unprotected,
    _empty_snapshot,
    _entry_observation,
    _plan,
    _consume_approval,
    _runtime,
    _save_run,
    _signaled_runtime,
    _watching_oco,
)


class BeforePlaceBroker(ScriptedBroker):
    def __init__(self, snapshot: BrokerSnapshot, before_place) -> None:
        super().__init__(snapshot)
        self.before_place = before_place

    def place_order(self, intent) -> BrokerOrderTicket:
        self.before_place(intent)
        return super().place_order(intent)


def test_entry_is_durably_reserved_before_the_broker_send(tmp_path) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    observed: dict[str, object] = {}

    with SQLiteStateStore(tmp_path / "reserve-before-send.sqlite") as store:

        def inspect_reservation(_intent) -> None:
            run = store.load_intraday_run(plan.plan_id)
            intent = store.load_intraday_order_intent(f"{plan.plan_id}:entry")
            order = store.load_execution_order(f"{plan.plan_id}:entry")
            events = store.list_execution_events(intent_id=f"{plan.plan_id}:entry")
            observed.update(
                state=run["state"],
                intent_id=intent["intent_id"],
                order_status=order["status"],
                event_types=[event["event_type"] for event in events],
            )

        broker = BeforePlaceBroker(_empty_snapshot(plan, clock()), inspect_reservation)
        runtime = _signaled_runtime(store, plan, clock, broker)
        runtime.tick()

    assert observed == {
        "state": "ENTRY_SUBMITTING",
        "intent_id": f"{plan.plan_id}:entry",
        "order_status": "PENDING",
        "event_types": ["create_send_reserved"],
    }
    assert [name for name, _ in broker.calls].count("place") == 1


def test_unknown_entry_response_allows_one_exact_retry_and_never_a_third(
    tmp_path,
) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    broker.general_outcomes = ["unknown", "unknown"]

    with SQLiteStateStore(tmp_path / "unknown-entry.sqlite") as store:
        runtime = _signaled_runtime(store, plan, clock, broker)
        runtime.tick()
        runtime.tick()
        runtime.tick()
        runtime.tick()
        run = store.load_intraday_run(plan.plan_id)

    placed = [value for name, value in broker.calls if name == "place"]
    assert run["state"] == "RECOVERY_REQUIRED"
    assert len(placed) == 2
    assert placed[0].idempotency_key == placed[1].idempotency_key
    assert placed[0].quantity == placed[1].quantity
    assert placed[0].limit_price == placed[1].limit_price


def test_stale_writer_cannot_project_an_ack_returned_after_takeover(tmp_path) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))

    with SQLiteStateStore(tmp_path / "stale-ack.sqlite") as store:

        def take_over_after_send(_intent) -> None:
            claimed = store.claim_intraday_writer(
                plan_id=plan.plan_id,
                writer_id="writer-2",
                now=clock() + timedelta(seconds=46),
            )
            assert claimed is not None and claimed["writer_fence"] == 2

        broker = BeforePlaceBroker(_empty_snapshot(plan, clock()), take_over_after_send)
        runtime = _signaled_runtime(store, plan, clock, broker)

        with pytest.raises(IntradayRuntimeError, match="stale_order_response"):
            runtime.tick()

        run = store.load_intraday_run(plan.plan_id)
        order = store.load_execution_order(f"{plan.plan_id}:entry")
        events = store.list_execution_events(intent_id=f"{plan.plan_id}:entry")

    assert run["writer_id"] == "writer-2" and run["writer_fence"] == 2
    assert run["state"] == "ENTRY_SUBMITTING"
    assert order["status"] == "PENDING" and order["broker_order_id"] is None
    assert "create_acknowledged" not in {event["event_type"] for event in events}


def test_approval_is_one_shot_and_wrong_boot_cannot_arm_entry(tmp_path) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))

    with SQLiteStateStore(tmp_path / "approval-replay.sqlite") as store:
        saved = _save_run(store, plan, clock())
        runtime = _runtime(
            store,
            plan,
            clock,
            broker,
            boot_id_hash="4" * 64,
        )
        runtime.recover()
        _consume_approval(
            store,
            plan,
            saved,
            runtime,
            clock(),
            boot_id_hash="3" * 64,
        )
        run = store.load_intraday_run(plan.plan_id)
        assert saved is not None and run is not None
        replay = store.consume_intraday_approval(
            plan_id=plan.plan_id,
            plan_hash=saved["plan_hash"],
            envelope_sha256="1" * 64,
            receipt_sha256="2" * 64,
            interaction_id="123456789012345678",
            boot_id_hash="3" * 64,
            approval_generation=1,
            approved_writer_fence=run["writer_fence"],
            writer_id="writer-1",
            writer_fence=run["writer_fence"],
            approved_at=clock(),
            approval_expires_at=clock() + timedelta(minutes=5),
            now=clock(),
        )
        assert replay is None

        runtime.recover()
        final = store.load_intraday_run(plan.plan_id)
        approval_events = store.list_execution_events(intent_id=f"run:{plan.plan_id}")

    assert final["state"] == "PLANNED"
    assert [event["event_type"] for event in approval_events].count(
        "approval_consumed"
    ) == 1
    assert [name for name, _ in broker.calls].count("place") == 0


def test_unknown_oco_create_is_never_replayed(tmp_path) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))
    broker.conditional_create_unknown = True

    with SQLiteStateStore(tmp_path / "unknown-oco-create.sqlite") as store:
        runtime = _drive_to_open_unprotected(store, plan, clock, broker)
        runtime.tick()
        runtime.tick()
        runtime.tick()
        runtime.tick()
        run = store.load_intraday_run(plan.plan_id)

    assert run["state"] == "RECOVERY_REQUIRED"
    assert [name for name, _ in broker.calls].count("oco-create") == 1


def test_unknown_oco_delete_never_retries_or_falls_through_to_sell(tmp_path) -> None:
    plan = _plan()
    clock = FakeClock(plan.entry_start + timedelta(minutes=1))
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))

    with SQLiteStateStore(tmp_path / "unknown-oco-delete.sqlite") as store:
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
        runtime.tick()
        run = store.load_intraday_run(plan.plan_id)

    mutations = [name for name, _ in broker.calls if name not in {"snapshot", "stream-ack"}]
    assert run["state"] == "RECOVERY_REQUIRED"
    assert mutations == ["place", "oco-create", "oco-delete"]


def test_clock_rollback_blocks_recovery_before_any_broker_call(tmp_path) -> None:
    plan = _plan()
    approved_at = plan.entry_start
    clock = FakeClock(approved_at)
    broker = ScriptedBroker(_empty_snapshot(plan, clock()))

    with SQLiteStateStore(tmp_path / "clock-rollback.sqlite") as store:
        saved = _save_run(store, plan, approved_at)
        runtime = _runtime(store, plan, clock, broker)
        runtime.recover()
        _consume_approval(store, plan, saved, runtime, approved_at)
        clock.value = approved_at - timedelta(microseconds=1)

        with pytest.raises(IntradayRuntimeError, match="clock_moved_backwards"):
            runtime.recover()

        run = store.load_intraday_run(plan.plan_id)

    assert run["state"] == "APPROVED"
    assert broker.calls == []
