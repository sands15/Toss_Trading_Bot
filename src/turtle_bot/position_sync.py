from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping, Protocol, Sequence

from .domain import (
    PositionDirection,
    PositionState,
    PositionStatus,
    TurtleSystem,
    UnitState,
    as_decimal,
)


UNRESOLVED_BROKER_STATUSES = frozenset(
    {
        "OPEN",
        "UNKNOWN",
        "PENDING",
        "PARTIAL_FILLED",
        "PENDING_CANCEL",
        "PENDING_REPLACE",
    }
)
CLOSED_BROKER_STATUSES = frozenset(
    {
        "CLOSED",
        "FILLED",
        "CANCELED",
        "REJECTED",
        "EXPIRED",
        "CANCEL_REJECTED",
        "REPLACE_REJECTED",
        "REPLACED",
    }
)


class PositionStore(Protocol):
    def load_position(self, symbol: str) -> PositionState | None:
        ...

    def list_positions(
        self,
        *,
        status: PositionStatus | str | None = None,
    ) -> list[PositionState]:
        ...

    def save_position(self, position: PositionState) -> None:
        ...

    def record_broker_snapshot(
        self,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        ...

    def record_runtime_event(
        self,
        level: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        ...


class ReadOnlyBrokerClient(Protocol):
    def get_holdings(self, *, symbol: str | None = None) -> Mapping[str, Any]:
        ...

    def get_orders(
        self,
        *,
        status: str = "OPEN",
        symbol: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> Mapping[str, Any]:
        ...


@dataclass(frozen=True)
class BrokerHolding:
    symbol: str
    quantity: Decimal
    average_purchase_price: Decimal | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BrokerOrder:
    order_id: str | None
    symbol: str
    side: str
    status: str
    quantity: Decimal | None = None
    client_order_id: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReconcileIssue:
    code: str
    symbol: str | None
    message: str
    severity: str = "BLOCK"


@dataclass(frozen=True)
class ReconcileResult:
    issues: tuple[ReconcileIssue, ...]
    holdings: tuple[BrokerHolding, ...]
    open_orders: tuple[BrokerOrder, ...]

    @property
    def clean(self) -> bool:
        return not self.issues

    @property
    def blockers(self) -> tuple[str, ...]:
        return tuple(issue.message for issue in self.issues if issue.severity == "BLOCK")

    def as_payload(self) -> dict[str, Any]:
        return {
            "clean": self.clean,
            "blockers": list(self.blockers),
            "issues": [
                {
                    "code": issue.code,
                    "symbol": issue.symbol,
                    "message": issue.message,
                    "severity": issue.severity,
                }
                for issue in self.issues
            ],
            "holdings": [
                {
                    "symbol": holding.symbol,
                    "quantity": str(holding.quantity),
                    "average_purchase_price": (
                        str(holding.average_purchase_price)
                        if holding.average_purchase_price is not None
                        else None
                    ),
                }
                for holding in self.holdings
            ],
            "open_orders": [
                {
                    "order_id": order.order_id,
                    "client_order_id": order.client_order_id,
                    "symbol": order.symbol,
                    "side": order.side,
                    "status": order.status,
                    "quantity": str(order.quantity) if order.quantity is not None else None,
                }
                for order in self.open_orders
            ],
        }


def normalize_holdings(payload: Mapping[str, Any]) -> tuple[BrokerHolding, ...]:
    raw_items = _items_from_payload(payload, "items", "holdings")
    holdings: list[BrokerHolding] = []
    for item in raw_items:
        symbol = str(item.get("symbol", "")).strip()
        if not symbol:
            continue
        quantity = as_decimal(item.get("quantity", "0"))
        if quantity <= Decimal("0"):
            continue
        avg_price = item.get("averagePurchasePrice")
        holdings.append(
            BrokerHolding(
                symbol=symbol,
                quantity=quantity,
                average_purchase_price=as_decimal(avg_price) if avg_price is not None else None,
                raw=dict(item),
            )
        )
    return tuple(sorted(holdings, key=lambda holding: holding.symbol))


def normalize_orders(payload: Mapping[str, Any]) -> tuple[BrokerOrder, ...]:
    raw_items = _items_from_payload(payload, "orders", "items")
    orders: list[BrokerOrder] = []
    for item in raw_items:
        symbol = str(item.get("symbol", "")).strip()
        status = str(item.get("status", "UNKNOWN")).upper()
        if not symbol:
            continue
        quantity = item.get("quantity")
        orders.append(
            BrokerOrder(
                order_id=_optional_str(item.get("orderId")),
                client_order_id=_optional_str(item.get("clientOrderId")),
                symbol=symbol,
                side=str(item.get("side", "UNKNOWN")).upper(),
                status=status,
                quantity=as_decimal(quantity) if quantity is not None else None,
                raw=dict(item),
            )
        )
    return tuple(sorted(orders, key=lambda order: (order.symbol, order.order_id or "")))


def reconcile_positions(
    *,
    local_positions: Sequence[PositionState],
    broker_holdings: Sequence[BrokerHolding],
    open_orders: Sequence[BrokerOrder],
    quantity_tolerance: Decimal = Decimal("0"),
) -> ReconcileResult:
    issues: list[ReconcileIssue] = []
    local_open = {
        position.symbol: position
        for position in local_positions
        if position.status == PositionStatus.OPEN and position.total_qty > Decimal("0")
    }
    broker_open = {
        holding.symbol: holding
        for holding in broker_holdings
        if holding.quantity > Decimal("0")
    }

    for symbol in sorted(set(local_open) | set(broker_open)):
        local = local_open.get(symbol)
        broker = broker_open.get(symbol)
        if local is None and broker is not None:
            issues.append(
                ReconcileIssue(
                    code="broker_only_holding",
                    symbol=symbol,
                    message=f"{symbol} exists at broker but not in local state",
                )
            )
            continue
        if broker is None and local is not None:
            issues.append(
                ReconcileIssue(
                    code="local_only_position",
                    symbol=symbol,
                    message=f"{symbol} exists locally but not at broker",
                )
            )
            continue
        if local is None or broker is None:
            continue
        if abs(local.total_qty - broker.quantity) > quantity_tolerance:
            issues.append(
                ReconcileIssue(
                    code="quantity_mismatch",
                    symbol=symbol,
                    message=(
                        f"{symbol} quantity mismatch: "
                        f"local={local.total_qty} broker={broker.quantity}"
                    ),
                )
            )

    unresolved = [
        order
        for order in open_orders
        if order.status.upper() in UNRESOLVED_BROKER_STATUSES
    ]
    for order in unresolved:
        issues.append(
            ReconcileIssue(
                code="unresolved_open_order",
                symbol=order.symbol,
                message=f"{order.symbol} has unresolved broker order status={order.status}",
            )
        )

    unknown_status = [
        order
        for order in open_orders
        if order.status.upper() not in UNRESOLVED_BROKER_STATUSES | CLOSED_BROKER_STATUSES
    ]
    for order in unknown_status:
        issues.append(
            ReconcileIssue(
                code="unknown_order_status",
                symbol=order.symbol,
                message=f"{order.symbol} has unknown broker order status={order.status}",
            )
        )

    return ReconcileResult(
        issues=tuple(issues),
        holdings=tuple(broker_holdings),
        open_orders=tuple(open_orders),
    )


class TossPositionSync:
    def __init__(
        self,
        *,
        client: ReadOnlyBrokerClient,
        store: PositionStore,
        quantity_tolerance: Decimal = Decimal("0"),
        sync_live_positions: bool = False,
        sync_closed_orders: bool = False,
        closed_order_limit: int = 20,
    ) -> None:
        self.client = client
        self.store = store
        self.quantity_tolerance = quantity_tolerance
        self.sync_live_positions = sync_live_positions
        self.sync_closed_orders = sync_closed_orders
        self.closed_order_limit = closed_order_limit

    def reconcile(self) -> ReconcileResult:
        holdings_payload = dict(self.client.get_holdings())
        orders_payload = dict(self.client.get_orders(status="OPEN"))
        self.store.record_broker_snapshot("holdings", holdings_payload)
        self.store.record_broker_snapshot("open_orders", orders_payload)
        closed_orders_payload = self._sync_closed_order_history()

        holdings = normalize_holdings(holdings_payload)
        open_orders = normalize_orders(orders_payload)
        if self.sync_live_positions:
            self._sync_live_positions(holdings)
        self.store.record_runtime_event(
            "INFO",
            "broker_account_synced",
            {
                "holdings_count": len(holdings),
                "open_orders_count": len(open_orders),
                "synced_live_positions": self.sync_live_positions,
                "closed_orders_count": _orders_count(closed_orders_payload),
                "synced_closed_orders": self.sync_closed_orders,
            },
        )
        local_positions = self.store.list_positions(status=PositionStatus.OPEN)
        return reconcile_positions(
            local_positions=local_positions,
            broker_holdings=holdings,
            open_orders=open_orders,
            quantity_tolerance=self.quantity_tolerance,
        )

    def _sync_closed_order_history(self) -> Mapping[str, Any] | None:
        if not self.sync_closed_orders:
            return None
        try:
            payload = dict(
                self.client.get_orders(
                    status="CLOSED",
                    limit=self.closed_order_limit,
                )
            )
        except Exception as exc:
            self.store.record_runtime_event(
                "WARN",
                "broker_order_history_sync_failed",
                {"error": str(exc)},
            )
            return None
        self.store.record_broker_snapshot("closed_orders", payload)
        self.store.record_runtime_event(
            "INFO",
            "broker_order_history_synced",
            {"closed_orders_count": _orders_count(payload)},
        )
        return payload

    def _sync_live_positions(self, holdings: Sequence[BrokerHolding]) -> None:
        broker_by_symbol = {holding.symbol: holding for holding in holdings}
        for holding in holdings:
            existing = self.store.load_position(holding.symbol)
            self.store.save_position(_position_from_broker_holding(holding, existing=existing))

        for position in self.store.list_positions(status=PositionStatus.OPEN):
            if position.symbol in broker_by_symbol:
                continue
            self.store.save_position(_closed_position_from_missing_broker_holding(position))


def _position_from_broker_holding(
    holding: BrokerHolding,
    *,
    existing: PositionState | None,
) -> PositionState:
    avg_price = holding.average_purchase_price or (
        existing.avg_entry_price if existing is not None else Decimal("0")
    )
    entry_n = existing.entry_n if existing is not None else Decimal("0")
    stop_price = existing.current_stop_price if existing is not None else avg_price
    system = existing.system if existing is not None else TurtleSystem.MOMENTUM
    direction = existing.direction if existing is not None else PositionDirection.LONG
    unit = UnitState(
        unit_no=1,
        qty=holding.quantity,
        entry_price=avg_price,
        n_at_entry=entry_n,
        stop_price=stop_price,
    )
    return PositionState(
        symbol=holding.symbol,
        system=system,
        status=PositionStatus.OPEN,
        total_qty=holding.quantity,
        avg_entry_price=avg_price,
        entry_n=entry_n,
        current_stop_price=stop_price,
        last_unit_entry_price=avg_price,
        units=(unit,),
        direction=direction,
    )


def _closed_position_from_missing_broker_holding(position: PositionState) -> PositionState:
    return PositionState(
        symbol=position.symbol,
        system=position.system,
        status=PositionStatus.CLOSED,
        total_qty=Decimal("0"),
        avg_entry_price=position.avg_entry_price,
        entry_n=position.entry_n,
        current_stop_price=position.current_stop_price,
        last_unit_entry_price=position.last_unit_entry_price,
        units=(),
        direction=position.direction,
    )


def _items_from_payload(
    payload: Mapping[str, Any],
    *keys: str,
) -> tuple[Mapping[str, Any], ...]:
    for key in keys:
        raw = payload.get(key)
        if raw is None:
            continue
        if isinstance(raw, list):
            return tuple(item for item in raw if isinstance(item, Mapping))
    return ()


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _orders_count(payload: Mapping[str, Any] | None) -> int:
    if not isinstance(payload, Mapping):
        return 0
    raw = payload.get("orders")
    if isinstance(raw, list):
        return len(raw)
    raw = payload.get("items")
    if isinstance(raw, list):
        return len(raw)
    return 0
