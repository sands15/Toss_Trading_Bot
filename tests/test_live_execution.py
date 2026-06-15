from __future__ import annotations

from decimal import Decimal
from typing import Any

from turtle_bot.domain import Side
from turtle_bot.live_execution import LiveOrderOrchestrator
from turtle_bot.live_order import BrokerOrderState, BrokerOrderTicket, ExecutionStatus, OrderIntent, OrderType
from turtle_bot.live_safety import PreTradeSafety, PreTradeSafetyConfig, PreTradeSafetyContext
from turtle_bot.state_store import SQLiteStateStore


class FakeBrokerAdapter:
    def __init__(self) -> None:
        self.placed: list[OrderIntent] = []

    def place_order(self, intent: OrderIntent) -> BrokerOrderTicket:
        self.placed.append(intent)
        return BrokerOrderTicket(
            broker_order_id=f"broker-{len(self.placed)}",
            status=ExecutionStatus.ACKNOWLEDGED,
            raw={"accepted": True},
        )

    def modify_order(self, ticket_id: str, request: dict[str, Any]) -> BrokerOrderTicket:
        raise NotImplementedError

    def cancel_order(self, ticket_id: str) -> BrokerOrderTicket:
        raise NotImplementedError

    def query_order(self, ticket_id: str) -> BrokerOrderState:
        raise NotImplementedError


def _intent(
    *,
    intent_id: str = "intent-1",
    idempotency_key: str = "idem-1",
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        idempotency_key=idempotency_key,
        symbol="aapl",
        side=Side.BUY,
        quantity=Decimal("2"),
        order_type=OrderType.LIMIT,
        limit_price=Decimal("100"),
        source="test",
        reason="phase7_smoke",
    )


def _clean_context() -> PreTradeSafetyContext:
    return PreTradeSafetyContext(
        market_open=True,
        reconcile_clean=True,
        available_cash=Decimal("1000"),
        current_position_qty=Decimal("0"),
    )


def test_live_orchestrator_blocks_when_live_disabled() -> None:
    broker = FakeBrokerAdapter()
    with SQLiteStateStore() as store:
        orchestrator = LiveOrderOrchestrator(
            safety=PreTradeSafety(PreTradeSafetyConfig(live_enabled=False)),
            broker=broker,
            store=store,
        )

        result = orchestrator.submit(_intent(), context=_clean_context())
        order = store.load_execution_order("intent-1")
        events = store.list_execution_events(intent_id="intent-1")

    assert result.status == ExecutionStatus.REJECTED
    assert result.safety_decision.code == "LIVE_DISABLED"
    assert broker.placed == []
    assert order is not None
    assert order["status"] == "REJECTED"
    assert events[0]["event_type"] == "risk_block"
    assert events[0]["payload"]["code"] == "LIVE_DISABLED"


def test_live_orchestrator_records_acknowledged_order() -> None:
    broker = FakeBrokerAdapter()
    with SQLiteStateStore() as store:
        orchestrator = LiveOrderOrchestrator(
            safety=PreTradeSafety(
                PreTradeSafetyConfig(
                    live_enabled=True,
                    allowed_symbols=("AAPL",),
                    max_order_notional=Decimal("500"),
                )
            ),
            broker=broker,
            store=store,
        )

        result = orchestrator.submit(_intent(), context=_clean_context())
        order = store.load_execution_order("intent-1")
        events = store.list_execution_events(intent_id="intent-1")

    assert result.status == ExecutionStatus.ACKNOWLEDGED
    assert result.broker_order_id == "broker-1"
    assert len(broker.placed) == 1
    assert order is not None
    assert order["status"] == "ACKNOWLEDGED"
    assert order["broker_order_id"] == "broker-1"
    assert [event["event_type"] for event in events] == ["broker_ack", "submit_started"]


def test_live_orchestrator_blocks_duplicate_unresolved_idempotency_key() -> None:
    broker = FakeBrokerAdapter()
    with SQLiteStateStore() as store:
        orchestrator = LiveOrderOrchestrator(
            safety=PreTradeSafety(PreTradeSafetyConfig(live_enabled=True)),
            broker=broker,
            store=store,
        )

        first = orchestrator.submit(_intent(), context=_clean_context())
        second = orchestrator.submit(
            _intent(intent_id="intent-2", idempotency_key="idem-1"),
            context=_clean_context(),
        )
        second_order = store.load_execution_order("intent-2")
        second_events = store.list_execution_events(intent_id="intent-2")

    assert first.status == ExecutionStatus.ACKNOWLEDGED
    assert second.status == ExecutionStatus.REJECTED
    assert second.safety_decision.code == "DUPLICATE_IDEMPOTENCY_KEY"
    assert len(broker.placed) == 1
    assert second_order is not None
    assert second_order["status"] == "REJECTED"
    assert second_events[0]["payload"]["code"] == "DUPLICATE_IDEMPOTENCY_KEY"

