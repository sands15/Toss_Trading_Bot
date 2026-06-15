from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .live_order import BrokerOrderState, BrokerOrderTicket, ExecutionStatus, OrderIntent
from .live_safety import PreTradeDecision, PreTradeSafety, PreTradeSafetyContext


class LiveBrokerError(RuntimeError):
    def __init__(self, message: str, *, unknown_state: bool = False) -> None:
        super().__init__(message)
        self.unknown_state = unknown_state


class LiveBrokerDisabledError(LiveBrokerError):
    pass


class BrokerOrderAdapter(Protocol):
    def place_order(self, intent: OrderIntent) -> BrokerOrderTicket:
        ...

    def modify_order(self, ticket_id: str, request: dict[str, Any]) -> BrokerOrderTicket:
        ...

    def cancel_order(self, ticket_id: str) -> BrokerOrderTicket:
        ...

    def query_order(self, ticket_id: str) -> BrokerOrderState:
        ...


class ExecutionLedgerStore(Protocol):
    def record_order_intent(self, intent: OrderIntent) -> None:
        ...

    def record_execution_order(
        self,
        *,
        intent_id: str,
        idempotency_key: str,
        symbol: str,
        side: str,
        status: str,
        broker_order_id: str | None = None,
        raw: Any = None,
    ) -> None:
        ...

    def record_execution_event(
        self,
        *,
        intent_id: str,
        event_type: str,
        status: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        ...

    def has_unresolved_execution_key(self, idempotency_key: str) -> bool:
        ...


@dataclass(frozen=True)
class ExecutionResult:
    intent_id: str
    status: ExecutionStatus
    safety_decision: PreTradeDecision
    broker_order_id: str | None = None
    message: str = ""

    @property
    def accepted(self) -> bool:
        return self.status not in {
            ExecutionStatus.REJECTED,
            ExecutionStatus.FAILED,
            ExecutionStatus.UNKNOWN,
        }


class DisabledLiveBrokerAdapter:
    def place_order(self, intent: OrderIntent) -> BrokerOrderTicket:
        raise LiveBrokerDisabledError("live broker adapter is disabled")

    def modify_order(self, ticket_id: str, request: dict[str, Any]) -> BrokerOrderTicket:
        raise LiveBrokerDisabledError("live broker adapter is disabled")

    def cancel_order(self, ticket_id: str) -> BrokerOrderTicket:
        raise LiveBrokerDisabledError("live broker adapter is disabled")

    def query_order(self, ticket_id: str) -> BrokerOrderState:
        raise LiveBrokerDisabledError("live broker adapter is disabled")


class LiveOrderOrchestrator:
    def __init__(
        self,
        *,
        safety: PreTradeSafety,
        broker: BrokerOrderAdapter,
        store: ExecutionLedgerStore,
    ) -> None:
        self.safety = safety
        self.broker = broker
        self.store = store

    def submit(
        self,
        intent: OrderIntent,
        *,
        context: PreTradeSafetyContext,
    ) -> ExecutionResult:
        self.store.record_order_intent(intent)
        idempotency_key = intent.idempotency_key or intent.intent_id
        if self.store.has_unresolved_execution_key(idempotency_key):
            decision = PreTradeDecision(
                False,
                "DUPLICATE_IDEMPOTENCY_KEY",
                "unresolved execution already exists for idempotency key",
            )
            self._record_rejection(intent, decision)
            return ExecutionResult(
                intent_id=intent.intent_id,
                status=ExecutionStatus.REJECTED,
                safety_decision=decision,
                message=decision.message,
            )

        decision = self.safety.validate(intent, context)
        if not decision.passed:
            self._record_rejection(intent, decision)
            return ExecutionResult(
                intent_id=intent.intent_id,
                status=ExecutionStatus.REJECTED,
                safety_decision=decision,
                message=decision.message,
            )

        self.store.record_execution_order(
            intent_id=intent.intent_id,
            idempotency_key=idempotency_key,
            symbol=intent.symbol,
            side=intent.side.value,
            status=ExecutionStatus.PENDING.value,
        )
        self.store.record_execution_event(
            intent_id=intent.intent_id,
            event_type="submit_started",
            status=ExecutionStatus.SENT.value,
            payload=intent.as_payload(),
        )
        try:
            ticket = self.broker.place_order(intent)
        except LiveBrokerError as exc:
            status = ExecutionStatus.UNKNOWN if exc.unknown_state else ExecutionStatus.FAILED
            self.store.record_execution_order(
                intent_id=intent.intent_id,
                idempotency_key=idempotency_key,
                symbol=intent.symbol,
                side=intent.side.value,
                status=status.value,
                raw={"error": str(exc), "unknown_state": exc.unknown_state},
            )
            self.store.record_execution_event(
                intent_id=intent.intent_id,
                event_type="broker_error",
                status=status.value,
                payload={"error": str(exc), "unknown_state": exc.unknown_state},
            )
            return ExecutionResult(
                intent_id=intent.intent_id,
                status=status,
                safety_decision=decision,
                message=str(exc),
            )

        self.store.record_execution_order(
            intent_id=intent.intent_id,
            idempotency_key=idempotency_key,
            symbol=intent.symbol,
            side=intent.side.value,
            status=ticket.status.value,
            broker_order_id=ticket.broker_order_id,
            raw={
                **ticket.as_payload(),
                "request": {
                    "notional": str(intent.notional) if intent.notional is not None else None,
                    "quantity": str(intent.quantity),
                    "limit_price": str(intent.limit_price)
                    if intent.limit_price is not None
                    else None,
                },
            },
        )
        self.store.record_execution_event(
            intent_id=intent.intent_id,
            event_type="broker_ack",
            status=ticket.status.value,
            payload=ticket.as_payload(),
        )
        return ExecutionResult(
            intent_id=intent.intent_id,
            status=ticket.status,
            safety_decision=decision,
            broker_order_id=ticket.broker_order_id,
            message="broker order acknowledged",
        )

    def cancel_acknowledged(
        self,
        intent: OrderIntent,
        *,
        broker_order_id: str,
    ) -> ExecutionResult:
        idempotency_key = intent.idempotency_key or intent.intent_id
        decision = PreTradeDecision(True, "CANCEL_REQUESTED", "cancel requested after broker ack")
        self.store.record_execution_event(
            intent_id=intent.intent_id,
            event_type="cancel_started",
            status=ExecutionStatus.PENDING_CANCEL.value,
            payload={"broker_order_id": broker_order_id},
        )
        try:
            ticket = self.broker.cancel_order(broker_order_id)
        except LiveBrokerError as exc:
            status = ExecutionStatus.UNKNOWN if exc.unknown_state else ExecutionStatus.FAILED
            self.store.record_execution_order(
                intent_id=intent.intent_id,
                idempotency_key=idempotency_key,
                symbol=intent.symbol,
                side=intent.side.value,
                status=status.value,
                broker_order_id=broker_order_id,
                raw={"cancel_error": str(exc), "unknown_state": exc.unknown_state},
            )
            self.store.record_execution_event(
                intent_id=intent.intent_id,
                event_type="cancel_error",
                status=status.value,
                payload={"error": str(exc), "unknown_state": exc.unknown_state},
            )
            return ExecutionResult(
                intent_id=intent.intent_id,
                status=status,
                safety_decision=decision,
                broker_order_id=broker_order_id,
                message=str(exc),
            )

        self.store.record_execution_order(
            intent_id=intent.intent_id,
            idempotency_key=idempotency_key,
            symbol=intent.symbol,
            side=intent.side.value,
            status=ticket.status.value,
            broker_order_id=ticket.broker_order_id,
            raw={"cancel": ticket.as_payload()},
        )
        self.store.record_execution_event(
            intent_id=intent.intent_id,
            event_type="cancel_ack",
            status=ticket.status.value,
            payload=ticket.as_payload(),
        )
        return ExecutionResult(
            intent_id=intent.intent_id,
            status=ticket.status,
            safety_decision=decision,
            broker_order_id=ticket.broker_order_id,
            message="broker cancel acknowledged",
        )

    def _record_rejection(
        self,
        intent: OrderIntent,
        decision: PreTradeDecision,
    ) -> None:
        idempotency_key = intent.idempotency_key or intent.intent_id
        self.store.record_execution_order(
            intent_id=intent.intent_id,
            idempotency_key=idempotency_key,
            symbol=intent.symbol,
            side=intent.side.value,
            status=ExecutionStatus.REJECTED.value,
            raw=decision.as_payload(),
        )
        self.store.record_execution_event(
            intent_id=intent.intent_id,
            event_type="risk_block",
            status=ExecutionStatus.REJECTED.value,
            payload=decision.as_payload(),
        )
