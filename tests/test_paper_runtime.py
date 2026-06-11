from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Sequence

from turtle_bot.domain import Candle, PositionState, PositionStatus, TurtleSystem, UnitState
from turtle_bot.notifier import MemoryNotifier
from turtle_bot.paper_runtime import PaperRuntimeConfig, PaperTradingRuntime
from turtle_bot.position_sync import BrokerHolding, ReconcileIssue, ReconcileResult
from turtle_bot.state_store import SQLiteStateStore


def _c(day: int, symbol: str = "TEST") -> Candle:
    return Candle(
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=day),
        symbol=symbol,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("1000"),
    )


def _history(symbol: str = "TEST", days: int = 21) -> tuple[Candle, ...]:
    return tuple(_c(day, symbol=symbol) for day in range(days))


def _position(symbol: str = "TEST", qty: str = "3") -> PositionState:
    return PositionState(
        symbol=symbol,
        system=TurtleSystem.S1,
        status=PositionStatus.OPEN,
        total_qty=Decimal(qty),
        avg_entry_price=Decimal("100"),
        entry_n=Decimal("2"),
        current_stop_price=Decimal("97"),
        last_unit_entry_price=Decimal("100"),
        units=(
            UnitState(
                unit_no=1,
                qty=Decimal(qty),
                entry_price=Decimal("100"),
                n_at_entry=Decimal("2"),
                stop_price=Decimal("97"),
            ),
        ),
    )


class FakeMarketData:
    def __init__(
        self,
        *,
        candles: dict[str, Sequence[Candle]],
        prices: dict[str, Decimal],
    ) -> None:
        self.candles = candles
        self.prices = prices
        self.calls: list[str] = []

    def get_completed_candles(self, symbol: str) -> Sequence[Candle]:
        self.calls.append(f"candles:{symbol}")
        return self.candles[symbol]

    def get_current_price(self, symbol: str) -> Decimal:
        self.calls.append(f"price:{symbol}")
        return self.prices[symbol]


class FakePositionSync:
    def __init__(self, result: ReconcileResult) -> None:
        self.result = result
        self.calls = 0

    def reconcile(self) -> ReconcileResult:
        self.calls += 1
        return self.result


def _clean_reconcile() -> ReconcileResult:
    return ReconcileResult(
        issues=(),
        holdings=(),
        open_orders=(),
    )


def test_paper_runtime_reconcile_block_stops_before_market_data():
    market_data = FakeMarketData(candles={}, prices={})
    sync = FakePositionSync(
        ReconcileResult(
            issues=(
                ReconcileIssue(
                    code="quantity_mismatch",
                    symbol="TEST",
                    message="TEST mismatch",
                ),
            ),
            holdings=(BrokerHolding(symbol="TEST", quantity=Decimal("2")),),
            open_orders=(),
        )
    )
    notifier = MemoryNotifier()

    with SQLiteStateStore() as store:
        runtime = PaperTradingRuntime(
            config=PaperRuntimeConfig(symbols=("TEST",)),
            market_data=market_data,
            position_sync=sync,
            store=store,
            notifier=notifier,
        )
        result = runtime.run_once()
        events = store.list_runtime_events(limit=1)

    assert result.ready is False
    assert result.intents == ()
    assert market_data.calls == []
    assert notifier.snapshot()[0].message == "paper_reconcile_blocked"
    assert events[0]["message"] == "paper_reconcile_blocked"


def test_paper_runtime_records_entry_intent_without_submitting_order():
    market_data = FakeMarketData(
        candles={"TEST": _history("TEST")},
        prices={"TEST": Decimal("105")},
    )
    notifier = MemoryNotifier()

    with SQLiteStateStore() as store:
        runtime = PaperTradingRuntime(
            config=PaperRuntimeConfig(symbols=("TEST",), unit_qty=Decimal("2")),
            market_data=market_data,
            position_sync=FakePositionSync(_clean_reconcile()),
            store=store,
            notifier=notifier,
        )
        result = runtime.run_once()
        events = store.list_runtime_events(limit=1)
        health = runtime.health_snapshot()
        broker_order_guard = store.has_unresolved_client_order_id(result.intents[0].intent_id)

    assert result.ready is True
    assert len(result.intents) == 1
    intent = result.intents[0]
    assert intent.signal_kind == "ENTRY"
    assert intent.side == "BUY"
    assert intent.quantity == Decimal("2")
    assert intent.reason == "s1_breakout"
    assert events[0]["message"] == "paper_order_intent"
    assert notifier.snapshot()[0].message == "paper_order_intent"
    assert health.mode == "paper"
    assert health.open_orders[0]["mode"] == "paper"
    assert broker_order_guard is False


def test_paper_runtime_stop_intent_uses_open_position_quantity():
    market_data = FakeMarketData(
        candles={"TEST": _history("TEST", days=60)},
        prices={"TEST": Decimal("96")},
    )
    notifier = MemoryNotifier()

    with SQLiteStateStore() as store:
        store.save_position(_position("TEST", qty="3"))
        runtime = PaperTradingRuntime(
            config=PaperRuntimeConfig(symbols=("TEST",), unit_qty=Decimal("1")),
            market_data=market_data,
            position_sync=FakePositionSync(_clean_reconcile()),
            store=store,
            notifier=notifier,
        )
        result = runtime.run_once()

    assert len(result.intents) == 1
    intent = result.intents[0]
    assert intent.signal_kind == "STOP"
    assert intent.side == "SELL"
    assert intent.quantity == Decimal("3")
    assert intent.trigger_price == Decimal("97")


def test_paper_runtime_market_data_error_blocks_symbol():
    class BrokenMarketData(FakeMarketData):
        def get_completed_candles(self, symbol: str) -> Sequence[Candle]:
            raise RuntimeError("stale candles")

    notifier = MemoryNotifier()
    with SQLiteStateStore() as store:
        runtime = PaperTradingRuntime(
            config=PaperRuntimeConfig(symbols=("TEST",)),
            market_data=BrokenMarketData(candles={}, prices={}),
            position_sync=FakePositionSync(_clean_reconcile()),
            store=store,
            notifier=notifier,
        )
        result = runtime.run_once()

    assert result.ready is False
    assert result.intents == ()
    assert result.blockers == ("TEST market data unavailable: stale candles",)
    assert notifier.snapshot()[0].message == "paper_runtime_blocked"
