from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .domain import Side, as_decimal
from .live_order import OrderIntent, OrderType


@dataclass(frozen=True)
class PreTradeSafetyConfig:
    live_enabled: bool = False
    emergency_stop: bool = False
    allowed_symbols: tuple[str, ...] = ()
    max_order_quantity: Decimal | None = None
    max_order_notional: Decimal | None = None
    daily_order_count_limit: int | None = None
    daily_notional_limit: Decimal | None = None
    require_market_open: bool = True
    require_clean_reconcile: bool = True
    block_unresolved_orders: bool = True
    max_consecutive_order_failures: int | None = None

    def __post_init__(self) -> None:
        if self.max_order_quantity is not None:
            object.__setattr__(self, "max_order_quantity", as_decimal(self.max_order_quantity))
        if self.max_order_notional is not None:
            object.__setattr__(self, "max_order_notional", as_decimal(self.max_order_notional))
        if self.daily_notional_limit is not None:
            object.__setattr__(self, "daily_notional_limit", as_decimal(self.daily_notional_limit))
        object.__setattr__(
            self,
            "allowed_symbols",
            tuple(symbol.strip().upper() for symbol in self.allowed_symbols if symbol.strip()),
        )


@dataclass(frozen=True)
class PreTradeSafetyContext:
    market_open: bool = False
    reconcile_clean: bool = False
    unresolved_order_exists: bool = False
    available_cash: Decimal | None = None
    current_position_qty: Decimal = Decimal("0")
    daily_order_count: int = 0
    daily_notional: Decimal = Decimal("0")
    consecutive_order_failures: int = 0
    unresolved_execution_count: int = 0

    def __post_init__(self) -> None:
        if self.available_cash is not None:
            object.__setattr__(self, "available_cash", as_decimal(self.available_cash))
        object.__setattr__(self, "current_position_qty", as_decimal(self.current_position_qty))
        object.__setattr__(self, "daily_notional", as_decimal(self.daily_notional))


@dataclass(frozen=True)
class PreTradeDecision:
    passed: bool
    code: str
    message: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "code": self.code,
            "message": self.message,
        }


class PreTradeSafety:
    def __init__(self, config: PreTradeSafetyConfig) -> None:
        self.config = config

    def validate(
        self,
        intent: OrderIntent,
        context: PreTradeSafetyContext,
    ) -> PreTradeDecision:
        config = self.config
        if not config.live_enabled:
            return self._block("LIVE_DISABLED", "live trading is disabled")
        if config.emergency_stop:
            return self._block("EMERGENCY_STOP", "emergency stop is active")
        if config.allowed_symbols and intent.symbol not in config.allowed_symbols:
            return self._block("SYMBOL_NOT_ALLOWED", f"{intent.symbol} is not allowlisted")
        if config.require_market_open and not context.market_open:
            return self._block("MARKET_CLOSED", "market is not open")
        if config.require_clean_reconcile and not context.reconcile_clean:
            return self._block("RECONCILE_DIRTY", "reconciliation is not clean")
        if config.block_unresolved_orders and context.unresolved_order_exists:
            return self._block("UNRESOLVED_ORDER", "unresolved broker order exists")
        if config.block_unresolved_orders and context.unresolved_execution_count > 0:
            return self._block(
                "UNRESOLVED_EXECUTION",
                "unresolved live execution exists",
            )
        if (
            config.max_consecutive_order_failures is not None
            and context.consecutive_order_failures >= config.max_consecutive_order_failures
        ):
            return self._block(
                "CONSECUTIVE_ORDER_FAILURES",
                "consecutive live order failure limit reached",
            )
        if intent.quantity <= Decimal("0"):
            return self._block("BAD_QUANTITY", "order quantity must be positive")
        if config.max_order_quantity is not None and intent.quantity > config.max_order_quantity:
            return self._block("QUANTITY_LIMIT", "order quantity exceeds limit")
        notional = intent.notional
        if intent.order_type == OrderType.LIMIT and notional is None:
            return self._block("MISSING_LIMIT_PRICE", "limit order requires limit price")
        if config.max_order_notional is not None and notional is not None and notional > config.max_order_notional:
            return self._block("ORDER_NOTIONAL_LIMIT", "order notional exceeds limit")
        if context.available_cash is not None and intent.side == Side.BUY and notional is not None and notional > context.available_cash:
            return self._block("INSUFFICIENT_CASH", "available cash is insufficient")
        if intent.side == Side.SELL and intent.quantity > context.current_position_qty:
            return self._block("INSUFFICIENT_POSITION", "sell quantity exceeds current position")
        if config.daily_order_count_limit is not None and context.daily_order_count >= config.daily_order_count_limit:
            return self._block("DAILY_ORDER_LIMIT", "daily order count limit reached")
        if (
            config.daily_notional_limit is not None
            and notional is not None
            and context.daily_notional + notional > config.daily_notional_limit
        ):
            return self._block("DAILY_NOTIONAL_LIMIT", "daily notional limit would be exceeded")
        return PreTradeDecision(True, "PASSED", "pre-trade safety passed")

    @staticmethod
    def _block(code: str, message: str) -> PreTradeDecision:
        return PreTradeDecision(False, code, message)
