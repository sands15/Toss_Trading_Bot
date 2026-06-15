from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import uuid4

from .domain import Side, TurtleSystem, as_decimal


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class TimeInForce(str, Enum):
    DAY = "DAY"
    CLS = "CLS"


class ExecutionStatus(str, Enum):
    PENDING = "PENDING"
    SENT = "SENT"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PENDING_CANCEL = "PENDING_CANCEL"
    PENDING_REPLACE = "PENDING_REPLACE"
    PARTIAL_FILLED = "PARTIAL_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    CANCEL_REJECTED = "CANCEL_REJECTED"
    REPLACE_REJECTED = "REPLACE_REJECTED"
    REPLACED = "REPLACED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: Side
    quantity: Decimal
    order_type: OrderType
    source: str
    reason: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    intent_id: str = field(default_factory=lambda: f"intent-{uuid4()}")
    idempotency_key: str | None = None
    system: TurtleSystem | None = None
    limit_price: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.DAY
    signal_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        object.__setattr__(self, "quantity", as_decimal(self.quantity))
        if self.limit_price is not None:
            object.__setattr__(self, "limit_price", as_decimal(self.limit_price))
        if self.created_at.tzinfo is None:
            object.__setattr__(self, "created_at", self.created_at.replace(tzinfo=timezone.utc))
        if self.idempotency_key is None:
            object.__setattr__(self, "idempotency_key", self.intent_id)

    @property
    def notional(self) -> Decimal | None:
        if self.limit_price is None:
            return None
        return self.quantity * self.limit_price

    def as_payload(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "idempotency_key": self.idempotency_key,
            "symbol": self.symbol,
            "side": self.side.value,
            "quantity": str(self.quantity),
            "order_type": self.order_type.value,
            "limit_price": str(self.limit_price) if self.limit_price is not None else None,
            "time_in_force": self.time_in_force.value,
            "source": self.source,
            "reason": self.reason,
            "system": self.system.value if self.system is not None else None,
            "signal_id": self.signal_id,
            "created_at": self.created_at.isoformat(),
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class BrokerOrderTicket:
    broker_order_id: str
    status: ExecutionStatus
    raw: dict[str, Any] = field(default_factory=dict)

    def as_payload(self) -> dict[str, Any]:
        return {
            "broker_order_id": self.broker_order_id,
            "status": self.status.value,
            "raw": self.raw,
        }


@dataclass(frozen=True)
class BrokerOrderState:
    broker_order_id: str
    status: ExecutionStatus
    filled_quantity: Decimal = Decimal("0")
    remaining_quantity: Decimal | None = None
    average_fill_price: Decimal | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "filled_quantity", as_decimal(self.filled_quantity))
        if self.remaining_quantity is not None:
            object.__setattr__(self, "remaining_quantity", as_decimal(self.remaining_quantity))
        if self.average_fill_price is not None:
            object.__setattr__(self, "average_fill_price", as_decimal(self.average_fill_price))

    def as_payload(self) -> dict[str, Any]:
        return {
            "broker_order_id": self.broker_order_id,
            "status": self.status.value,
            "filled_quantity": str(self.filled_quantity),
            "remaining_quantity": (
                str(self.remaining_quantity) if self.remaining_quantity is not None else None
            ),
            "average_fill_price": (
                str(self.average_fill_price) if self.average_fill_price is not None else None
            ),
            "raw": self.raw,
        }
