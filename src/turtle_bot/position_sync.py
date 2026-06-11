from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping, Protocol, Sequence

from .domain import PositionState, PositionStatus, as_decimal


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
CLOSED_BROKER_STATUSES = frozenset({"CLOSED", "FILLED", "CANCELED", "REJECTED", "EXPIRED"})


class PositionStore(Protocol):
    def list_positions(
        self,
        *,
        status: PositionStatus | str | None = None,
    ) -> list[PositionState]:
        ...

    def record_broker_snapshot(
        self,
        kind: str,
        payload: dict[str, Any],
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
    ) -> None:
        self.client = client
        self.store = store
        self.quantity_tolerance = quantity_tolerance

    def reconcile(self) -> ReconcileResult:
        holdings_payload = dict(self.client.get_holdings())
        orders_payload = dict(self.client.get_orders(status="OPEN"))
        self.store.record_broker_snapshot("holdings", holdings_payload)
        self.store.record_broker_snapshot("open_orders", orders_payload)

        holdings = normalize_holdings(holdings_payload)
        open_orders = normalize_orders(orders_payload)
        local_positions = self.store.list_positions(status=PositionStatus.OPEN)
        return reconcile_positions(
            local_positions=local_positions,
            broker_holdings=holdings,
            open_orders=open_orders,
            quantity_tolerance=self.quantity_tolerance,
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
