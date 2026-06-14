from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Sequence

from turtle_bot.domain import Candle, PositionState, PositionStatus, TurtleSystem, UnitState
from turtle_bot.notifier import MemoryNotifier
from turtle_bot.paper_runtime import (
    PaperRuntimeConfig,
    PaperRuntimeScheduler,
    PaperTradingRuntime,
    export_paper_report_json,
)
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


def _position(
    symbol: str = "TEST",
    qty: str = "3",
    *,
    system: TurtleSystem = TurtleSystem.S1,
) -> PositionState:
    return PositionState(
        symbol=symbol,
        system=system,
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


def test_shadow_runtime_reconcile_warning_allows_signal_generation():
    market_data = FakeMarketData(
        candles={"TEST": _history("TEST")},
        prices={"TEST": Decimal("105")},
    )
    sync = FakePositionSync(
        ReconcileResult(
            issues=(
                ReconcileIssue(
                    code="broker_only_holding",
                    symbol="OTHER",
                    message="OTHER exists at broker but not in local state",
                ),
            ),
            holdings=(BrokerHolding(symbol="OTHER", quantity=Decimal("1")),),
            open_orders=(),
        )
    )
    notifier = MemoryNotifier()

    with SQLiteStateStore() as store:
        runtime = PaperTradingRuntime(
            config=PaperRuntimeConfig(
                symbols=("TEST",),
                mode="shadow",
                require_clean_reconcile=False,
            ),
            market_data=market_data,
            position_sync=sync,
            store=store,
            notifier=notifier,
        )
        result = runtime.run_once()
        events = store.list_runtime_events(limit=4)
        health = runtime.health_snapshot()

    assert result.ready is True
    assert len(result.intents) == 1
    assert result.intents[0].mode == "shadow"
    assert health.mode == "shadow"
    assert [event["message"] for event in events] == [
        "shadow_fill",
        "shadow_order_intent",
        "shadow_order_guard",
        "shadow_reconcile_warning",
    ]


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
        events = store.list_runtime_events(limit=3)
        health = runtime.health_snapshot()
        broker_order_guard = store.has_unresolved_client_order_id(result.intents[0].intent_id)
        saved_position = store.load_paper_position("TEST")
        live_position = store.load_position("TEST")

    assert result.ready is True
    assert len(result.intents) == 1
    intent = result.intents[0]
    assert intent.signal_kind == "ENTRY"
    assert intent.side == "BUY"
    assert intent.quantity == Decimal("2")
    assert intent.reason == "s1_breakout"
    assert intent.entry_n == Decimal("2")
    assert intent.stop_price == Decimal("101")
    assert [event["message"] for event in events] == [
        "paper_fill",
        "paper_order_intent",
        "paper_order_guard",
    ]
    assert notifier.snapshot()[0].message == "paper_order_intent"
    assert health.mode == "paper"
    assert health.open_orders[0]["mode"] == "paper"
    assert broker_order_guard is False
    assert saved_position is not None
    assert saved_position.status == PositionStatus.OPEN
    assert saved_position.total_qty == Decimal("2")
    assert live_position is None


def test_paper_runtime_stop_intent_uses_open_position_quantity():
    market_data = FakeMarketData(
        candles={"TEST": _history("TEST", days=60)},
        prices={"TEST": Decimal("96")},
    )
    notifier = MemoryNotifier()

    with SQLiteStateStore() as store:
        store.save_paper_position(_position("TEST", qty="3"))
        runtime = PaperTradingRuntime(
            config=PaperRuntimeConfig(symbols=("TEST",), unit_qty=Decimal("1")),
            market_data=market_data,
            position_sync=FakePositionSync(_clean_reconcile()),
            store=store,
            notifier=notifier,
        )
        result = runtime.run_once()
        saved_position = store.load_paper_position("TEST")

    assert len(result.intents) == 1
    intent = result.intents[0]
    assert intent.signal_kind == "STOP"
    assert intent.side == "SELL"
    assert intent.quantity == Decimal("3")
    assert intent.trigger_price == Decimal("97")
    assert saved_position is not None
    assert saved_position.status == PositionStatus.CLOSED
    assert saved_position.total_qty == Decimal("0")


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


def test_paper_report_export_preserves_intent_and_guard_payloads(tmp_path):
    market_data = FakeMarketData(
        candles={"TEST": _history("TEST")},
        prices={"TEST": Decimal("105")},
    )
    notifier = MemoryNotifier()
    report_path = tmp_path / "paper" / "report.json"

    with SQLiteStateStore() as store:
        runtime = PaperTradingRuntime(
            config=PaperRuntimeConfig(symbols=("TEST",), unit_qty=Decimal("2")),
            market_data=market_data,
            position_sync=FakePositionSync(_clean_reconcile()),
            store=store,
            notifier=notifier,
        )
        result = runtime.run_once()

    export_paper_report_json(result, report_path)

    import json

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["ready"] is True
    assert report["intents"][0]["quantity"] == "2"
    assert report["guard_results"][0]["passed"] is True


def test_paper_scheduler_runs_repeated_iterations_with_sleep():
    market_data = FakeMarketData(
        candles={"TEST": _history("TEST")},
        prices={"TEST": Decimal("100")},
    )
    notifier = MemoryNotifier()
    sleeps: list[float] = []

    with SQLiteStateStore() as store:
        runtime = PaperTradingRuntime(
            config=PaperRuntimeConfig(symbols=("TEST",), simulate_fills=False),
            market_data=market_data,
            position_sync=FakePositionSync(_clean_reconcile()),
            store=store,
            notifier=notifier,
        )
        scheduler = PaperRuntimeScheduler(runtime, sleep=sleeps.append)
        results = scheduler.run_iterations(2, interval_seconds=5)

    assert len(results) == 2
    assert sleeps == [5]


def test_paper_runtime_momentum_records_relative_strength_entry():
    def trend(symbol: str, step: str) -> tuple[Candle, ...]:
        return tuple(
            Candle(
                timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc)
                + timedelta(days=day),
                symbol=symbol,
                open=Decimal("100") + Decimal(step) * Decimal(day),
                high=Decimal("101") + Decimal(step) * Decimal(day),
                low=Decimal("99") + Decimal(step) * Decimal(day),
                close=Decimal("100") + Decimal(step) * Decimal(day),
                volume=Decimal("1000000"),
            )
            for day in range(12)
        )

    market_data = FakeMarketData(
        candles={
            "SPY": trend("SPY", "1"),
            "AAA": trend("AAA", "4"),
            "BBB": trend("BBB", "2"),
        },
        prices={"SPY": Decimal("112"), "AAA": Decimal("148"), "BBB": Decimal("124")},
    )
    notifier = MemoryNotifier()

    with SQLiteStateStore() as store:
        runtime = PaperTradingRuntime(
            config=PaperRuntimeConfig(
                symbols=("AAA", "BBB"),
                strategy_kind="momentum",
                momentum_lookback_days=5,
                momentum_skip_days=1,
                momentum_trend_ma_days=3,
                momentum_exit_ma_days=2,
                momentum_max_positions=1,
                momentum_accept_top_n=1,
                momentum_target_position_pct=Decimal("0.10"),
                momentum_min_price=Decimal("0"),
                momentum_min_average_daily_value=Decimal("0"),
                momentum_average_daily_value_days=2,
                initial_equity=Decimal("10000"),
            ),
            market_data=market_data,
            position_sync=FakePositionSync(_clean_reconcile()),
            store=store,
            notifier=notifier,
        )
        result = runtime.run_once()
        saved_position = store.load_paper_position("AAA")

    assert result.ready is True
    assert len(result.intents) == 1
    intent = result.intents[0]
    assert intent.system == "MOMENTUM"
    assert intent.symbol == "AAA"
    assert intent.side == "BUY"
    assert intent.quantity == Decimal("6")
    assert intent.reason == "relative_momentum"
    assert saved_position is not None
    assert saved_position.system == TurtleSystem.MOMENTUM


def test_paper_runtime_momentum_respects_max_exposure_budget():
    def trend(symbol: str, base: str, step: str) -> tuple[Candle, ...]:
        return tuple(
            Candle(
                timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=day),
                symbol=symbol,
                open=Decimal(base) + Decimal(step) * Decimal(day),
                high=Decimal(base) + Decimal(step) * Decimal(day) + Decimal("1"),
                low=Decimal(base) + Decimal(step) * Decimal(day) - Decimal("1"),
                close=Decimal(base) + Decimal(step) * Decimal(day),
                volume=Decimal("1000000"),
            )
            for day in range(12)
        )

    market_data = FakeMarketData(
        candles={
            "SPY": trend("SPY", "100", "1"),
            "AAA": trend("AAA", "100", "4"),
            "BBB": trend("BBB", "10", "1"),
        },
        prices={"SPY": Decimal("112"), "AAA": Decimal("148"), "BBB": Decimal("21")},
    )
    notifier = MemoryNotifier()

    with SQLiteStateStore() as store:
        store.save_paper_position(
            _position("AAA", qty="2", system=TurtleSystem.MOMENTUM)
        )
        runtime = PaperTradingRuntime(
            config=PaperRuntimeConfig(
                symbols=("AAA", "BBB"),
                strategy_kind="momentum",
                momentum_lookback_days=5,
                momentum_skip_days=1,
                momentum_trend_ma_days=3,
                momentum_exit_ma_days=2,
                momentum_max_positions=5,
                momentum_accept_top_n=2,
                momentum_max_exposure_pct=Decimal("0.50"),
                momentum_target_position_pct=Decimal("0.50"),
                momentum_min_price=Decimal("0"),
                momentum_min_average_daily_value=Decimal("0"),
                momentum_average_daily_value_days=2,
                initial_equity=Decimal("1000"),
            ),
            market_data=market_data,
            position_sync=FakePositionSync(_clean_reconcile()),
            store=store,
            notifier=notifier,
        )
        result = runtime.run_once()
        saved_position = store.load_paper_position("BBB")

    equity = Decimal("1000") + (Decimal("148") - Decimal("100")) * Decimal("2")
    max_exposure = equity * Decimal("0.50")
    current_exposure = Decimal("2") * Decimal("148")
    remaining_exposure = max_exposure - current_exposure
    expected_allocation = min(equity * Decimal("0.50"), remaining_exposure)
    expected_qty = (expected_allocation / Decimal("21")).to_integral_value()

    assert len(result.intents) == 1
    intent = result.intents[0]
    assert intent.system == "MOMENTUM"
    assert intent.symbol == "BBB"
    assert intent.quantity == expected_qty
    assert saved_position is not None
    assert saved_position.total_qty == expected_qty
    assert saved_position.system == TurtleSystem.MOMENTUM


def test_paper_runtime_momentum_exits_below_exit_ma_without_same_day_reentry():
    candles = tuple(
        Candle(
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=day),
            symbol="AAA",
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("1000000"),
        )
        for day in range(12)
    )
    market_data = FakeMarketData(
        candles={
            "SPY": candles,
            "AAA": candles,
        },
        prices={"SPY": Decimal("100"), "AAA": Decimal("90")},
    )
    notifier = MemoryNotifier()

    with SQLiteStateStore() as store:
        store.save_paper_position(
            _position("AAA", qty="3", system=TurtleSystem.MOMENTUM)
        )
        runtime = PaperTradingRuntime(
            config=PaperRuntimeConfig(
                symbols=("AAA",),
                strategy_kind="momentum",
                momentum_lookback_days=5,
                momentum_skip_days=1,
                momentum_trend_ma_days=3,
                momentum_exit_ma_days=2,
                momentum_min_price=Decimal("0"),
                momentum_min_average_daily_value=Decimal("0"),
                momentum_average_daily_value_days=2,
                momentum_use_market_filter=False,
            ),
            market_data=market_data,
            position_sync=FakePositionSync(_clean_reconcile()),
            store=store,
            notifier=notifier,
        )
        result = runtime.run_once()
        saved_position = store.load_paper_position("AAA")

    assert len(result.intents) == 1
    intent = result.intents[0]
    assert intent.system == "MOMENTUM"
    assert intent.side == "SELL"
    assert intent.quantity == Decimal("3")
    assert intent.reason == "momentum_exit_ma"
    assert saved_position is not None
    assert saved_position.status == PositionStatus.CLOSED
