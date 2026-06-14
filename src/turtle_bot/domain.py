from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Mapping, Any
from uuid import uuid4


def as_decimal(value: Any) -> Decimal:
    """Convert string/int/float/Decimal into Decimal without using float math."""

    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def parse_timestamp(value: Any) -> datetime:
    """Parse ISO-8601 timestamps with timezone handling."""

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    return datetime.fromisoformat(str(value))


class TurtleSystem(str, Enum):
    S1 = "S1"
    S2 = "S2"
    MOMENTUM = "MOMENTUM"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class PositionDirection(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class SignalKind(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    STOP = "STOP"
    PYRAMID = "PYRAMID"


class PositionStatus(str, Enum):
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    symbol: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    currency: str = "KRW"
    adjusted: bool = True
    source: str = "raw"

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> "Candle":
        symbol = raw.get("symbol", "")
        return cls(
            timestamp=parse_timestamp(raw["timestamp"]),
            symbol=str(symbol),
            open=as_decimal(raw["openPrice"]),
            high=as_decimal(raw["highPrice"]),
            low=as_decimal(raw["lowPrice"]),
            close=as_decimal(raw["closePrice"]),
            volume=as_decimal(raw["volume"]),
            currency=str(raw.get("currency", "KRW")),
            adjusted=bool(raw.get("adjusted", True)),
            source=str(raw.get("source", "toss")),
        )


@dataclass(frozen=True)
class UnitState:
    unit_no: int
    qty: Decimal
    entry_price: Decimal
    n_at_entry: Decimal
    stop_price: Decimal
    broker_order_id: str | None = None
    client_order_id: str | None = None


@dataclass(frozen=True)
class PositionState:
    symbol: str
    system: TurtleSystem
    status: PositionStatus
    total_qty: Decimal
    avg_entry_price: Decimal
    entry_n: Decimal
    current_stop_price: Decimal
    last_unit_entry_price: Decimal
    units: tuple[UnitState, ...] = field(default_factory=tuple)
    direction: PositionDirection = PositionDirection.LONG


@dataclass(frozen=True)
class IndicatorSnapshot:
    symbol: str
    as_of: datetime
    n: Decimal | None
    n_method: str
    entry_high_20: Decimal | None
    entry_low_20: Decimal | None
    entry_high_55: Decimal | None
    entry_low_55: Decimal | None
    exit_high_10: Decimal | None
    exit_high_20: Decimal | None
    exit_low_10: Decimal | None
    exit_low_20: Decimal | None
    ready: bool


@dataclass(frozen=True)
class Signal:
    signal_id: str
    symbol: str
    system: TurtleSystem
    kind: SignalKind
    side: Side
    trigger_price: Decimal
    observed_price: Decimal
    triggered_at: datetime
    reason: str

    @classmethod
    def new(
        cls,
        symbol: str,
        system: TurtleSystem,
        kind: SignalKind,
        side: Side,
        trigger_price: Decimal,
        observed_price: Decimal,
        triggered_at: datetime,
        reason: str,
    ) -> "Signal":
        return cls(
            signal_id=f"sig-{uuid4()}",
            symbol=symbol,
            system=system,
            kind=kind,
            side=side,
            trigger_price=as_decimal(trigger_price),
            observed_price=as_decimal(observed_price),
            triggered_at=triggered_at,
            reason=reason,
        )


@dataclass(frozen=True)
class StrategyState:
    pending_s1_skip: frozenset[str] = field(default_factory=frozenset)

    def with_s1_skip(
        self,
        symbol: str,
        direction: PositionDirection = PositionDirection.LONG,
    ) -> "StrategyState":
        return replace(self, pending_s1_skip=self.pending_s1_skip | {_s1_skip_key(symbol, direction)})

    def clear_s1_skip(
        self,
        symbol: str,
        direction: PositionDirection = PositionDirection.LONG,
    ) -> "StrategyState":
        return replace(self, pending_s1_skip=self.pending_s1_skip - {_s1_skip_key(symbol, direction)})

    def should_skip_s1(
        self,
        symbol: str,
        direction: PositionDirection = PositionDirection.LONG,
    ) -> bool:
        return _s1_skip_key(symbol, direction) in self.pending_s1_skip


def _s1_skip_key(symbol: str, direction: PositionDirection) -> str:
    if direction == PositionDirection.LONG:
        return symbol
    return f"{symbol}:{direction.value}"


@dataclass(frozen=True)
class TradeOutcome:
    symbol: str
    system: TurtleSystem
    realized_pnl: Decimal
    direction: PositionDirection = PositionDirection.LONG
