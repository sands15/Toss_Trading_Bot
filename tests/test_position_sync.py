from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from turtle_bot.domain import PositionState, PositionStatus, TurtleSystem, UnitState
from turtle_bot.position_sync import (
    BrokerHolding,
    BrokerOrder,
    TossPositionSync,
    normalize_holdings,
    normalize_orders,
    reconcile_positions,
)
from turtle_bot.state_store import SQLiteStateStore


def _position(symbol: str, qty: str = "1") -> PositionState:
    return PositionState(
        symbol=symbol,
        system=TurtleSystem.S1,
        status=PositionStatus.OPEN,
        total_qty=Decimal(qty),
        avg_entry_price=Decimal("100"),
        entry_n=Decimal("2"),
        current_stop_price=Decimal("96"),
        last_unit_entry_price=Decimal("100"),
        units=(
            UnitState(
                unit_no=1,
                qty=Decimal(qty),
                entry_price=Decimal("100"),
                n_at_entry=Decimal("2"),
                stop_price=Decimal("96"),
            ),
        ),
    )


class FakeReadOnlyClient:
    def __init__(
        self,
        *,
        holdings: Mapping[str, Any],
        orders: Mapping[str, Any],
    ) -> None:
        self.holdings = holdings
        self.orders = orders
        self.calls: list[str] = []

    def get_holdings(self, *, symbol: str | None = None) -> Mapping[str, Any]:
        self.calls.append("get_holdings")
        return self.holdings

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
        self.calls.append(f"get_orders:{status}")
        return self.orders


def test_normalize_holdings_and_orders_preserves_broker_fields():
    holdings = normalize_holdings(
        {
            "items": [
                {
                    "symbol": "005930",
                    "quantity": Decimal("2"),
                    "averagePurchasePrice": Decimal("71000"),
                },
                {
                    "symbol": "000000",
                    "quantity": Decimal("0"),
                },
            ]
        }
    )
    orders = normalize_orders(
        {
            "orders": [
                {
                    "orderId": "order-1",
                    "clientOrderId": "client-1",
                    "symbol": "005930",
                    "side": "BUY",
                    "status": "OPEN",
                    "quantity": Decimal("1"),
                }
            ]
        }
    )

    assert holdings == (
        BrokerHolding(
            symbol="005930",
            quantity=Decimal("2"),
            average_purchase_price=Decimal("71000"),
            raw={
                "symbol": "005930",
                "quantity": Decimal("2"),
                "averagePurchasePrice": Decimal("71000"),
            },
        ),
    )
    assert orders[0] == BrokerOrder(
        order_id="order-1",
        client_order_id="client-1",
        symbol="005930",
        side="BUY",
        status="OPEN",
        quantity=Decimal("1"),
        raw={
            "orderId": "order-1",
            "clientOrderId": "client-1",
            "symbol": "005930",
            "side": "BUY",
            "status": "OPEN",
            "quantity": Decimal("1"),
        },
    )


def test_reconcile_clean_when_local_and_broker_quantities_match():
    result = reconcile_positions(
        local_positions=[_position("005930", "2")],
        broker_holdings=[BrokerHolding(symbol="005930", quantity=Decimal("2"))],
        open_orders=[],
    )

    assert result.clean is True
    assert result.blockers == ()


def test_reconcile_blocks_local_only_broker_only_and_quantity_mismatch():
    result = reconcile_positions(
        local_positions=[_position("AAA", "2"), _position("BBB", "3")],
        broker_holdings=[
            BrokerHolding(symbol="BBB", quantity=Decimal("1")),
            BrokerHolding(symbol="CCC", quantity=Decimal("4")),
        ],
        open_orders=[],
    )

    assert result.clean is False
    assert [issue.code for issue in result.issues] == [
        "local_only_position",
        "quantity_mismatch",
        "broker_only_holding",
    ]


def test_reconcile_blocks_unresolved_and_unknown_broker_orders():
    result = reconcile_positions(
        local_positions=[],
        broker_holdings=[],
        open_orders=[
            BrokerOrder(order_id="1", symbol="AAA", side="BUY", status="OPEN"),
            BrokerOrder(order_id="2", symbol="BBB", side="BUY", status="FUTURE"),
        ],
    )

    assert result.clean is False
    assert [issue.code for issue in result.issues] == [
        "unresolved_open_order",
        "unknown_order_status",
    ]


def test_toss_position_sync_fetches_read_only_payloads_and_records_snapshots():
    client = FakeReadOnlyClient(
        holdings={
            "items": [
                {
                    "symbol": "005930",
                    "quantity": Decimal("1"),
                    "averagePurchasePrice": Decimal("71000"),
                }
            ]
        },
        orders={"orders": [], "nextCursor": None, "hasNext": False},
    )

    with SQLiteStateStore() as store:
        store.save_position(_position("005930", "1"))
        result = TossPositionSync(client=client, store=store).reconcile()

        assert result.clean is True
        assert client.calls == ["get_holdings", "get_orders:OPEN"]
        assert store.latest_broker_snapshot("holdings") == {
            "items": [
                {
                    "symbol": "005930",
                    "quantity": "1",
                    "averagePurchasePrice": "71000",
                }
            ]
        }
        assert store.latest_broker_snapshot("open_orders") == {
            "orders": [],
            "nextCursor": None,
            "hasNext": False,
        }
