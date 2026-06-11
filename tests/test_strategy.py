from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from dataclasses import replace

from turtle_bot.domain import (
    Candle,
    PositionState,
    PositionStatus,
    SignalKind,
    StrategyState,
    TurtleSystem,
    UnitState,
)
from turtle_bot.strategy import evaluate_signals, apply_trade_outcomes
from turtle_bot.domain import TradeOutcome


def _c(timestamp: str, open_: str, high: str, low: str, close: str) -> Candle:
    return Candle(
        timestamp=datetime.fromisoformat(timestamp),
        symbol="TEST",
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("100"),
    )


def _trend_candles(days: int, base: int = 100) -> list[Candle]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        _c(
            (start + timedelta(days=day)).isoformat(),
            str(base),
            str(base + 1),
            str(base - 1),
            str(base),
        )
        for day in range(days)
    ]


def test_not_ready_before_history():
    candles = _trend_candles(15)
    signals, _ = evaluate_signals(
        symbol="TEST",
        completed_candles=candles,
        current_price=Decimal("1000"),
        state=StrategyState(),
    )
    assert signals == []


def test_evaluate_uses_latest_completed_candle_for_breakout_level():
    candles = _trend_candles(20)
    latest_completed = _c(
        (datetime(2026, 1, 30, tzinfo=timezone.utc)).isoformat(),
        "100",
        "300",
        "99",
        "120",
    )
    candles.append(latest_completed)

    signals, _ = evaluate_signals(
        symbol="TEST",
        completed_candles=candles,
        current_price=Decimal("200"),
        state=StrategyState(),
        minimum_tick=Decimal("0"),
    )
    assert signals == []

    signals, _ = evaluate_signals(
        symbol="TEST",
        completed_candles=candles,
        current_price=Decimal("300"),
        state=StrategyState(),
        minimum_tick=Decimal("0"),
    )
    assert len(signals) == 1
    assert signals[0].system == TurtleSystem.S1


def test_s2_not_skipped_when_s1_skip_applies():
    candles = _trend_candles(60)
    signals, next_state = evaluate_signals(
        symbol="TEST",
        completed_candles=candles,
        current_price=Decimal("200"),
        state=StrategyState(pending_s1_skip=frozenset({"TEST"})),
        minimum_tick=Decimal("0"),
    )
    assert len(signals) == 1
    assert signals[0].system == TurtleSystem.S2
    assert signals[0].kind.name == SignalKind.ENTRY.name
    assert "TEST" in next_state.pending_s1_skip


def test_stop_priority_over_pyramid_and_entry():
    candles = _trend_candles(60)
    position = PositionState(
        symbol="TEST",
        system=TurtleSystem.S1,
        status=PositionStatus.OPEN,
        total_qty=Decimal("1"),
        avg_entry_price=Decimal("100"),
        entry_n=Decimal("2"),
        current_stop_price=Decimal("101.10"),
        last_unit_entry_price=Decimal("100"),
        units=(
            UnitState(
                unit_no=1,
                qty=Decimal("1"),
                entry_price=Decimal("100"),
                n_at_entry=Decimal("2"),
                stop_price=Decimal("101.10"),
            ),
        ),
    )

    signals, _ = evaluate_signals(
        symbol="TEST",
        completed_candles=candles,
        current_price=Decimal("101.05"),
        state=StrategyState(),
        position=position,
        pyramid_step_n=Decimal("0.5"),
    )

    assert len(signals) == 1
    assert signals[0].kind == SignalKind.STOP


def test_pyramid_only_after_favorable_half_n_and_cap_applies():
    candles = _trend_candles(60)

    capped_position = PositionState(
        symbol="TEST",
        system=TurtleSystem.S1,
        status=PositionStatus.OPEN,
        total_qty=Decimal("4"),
        avg_entry_price=Decimal("100"),
        entry_n=Decimal("2"),
        current_stop_price=Decimal("90"),
        last_unit_entry_price=Decimal("100"),
        units=(
            UnitState(
                unit_no=1,
                qty=Decimal("1"),
                entry_price=Decimal("100"),
                n_at_entry=Decimal("2"),
                stop_price=Decimal("96"),
            ),
            UnitState(
                unit_no=2,
                qty=Decimal("1"),
                entry_price=Decimal("101"),
                n_at_entry=Decimal("2"),
                stop_price=Decimal("97"),
            ),
            UnitState(
                unit_no=3,
                qty=Decimal("1"),
                entry_price=Decimal("102"),
                n_at_entry=Decimal("2"),
                stop_price=Decimal("98"),
            ),
            UnitState(
                unit_no=4,
                qty=Decimal("1"),
                entry_price=Decimal("103"),
                n_at_entry=Decimal("2"),
                stop_price=Decimal("99"),
            ),
        ),
    )
    signals, _ = evaluate_signals(
        symbol="TEST",
        completed_candles=candles,
        current_price=Decimal("1000"),
        state=StrategyState(),
        position=capped_position,
    )
    assert signals == []

    position_for_add = replace(
        capped_position,
        total_qty=Decimal("1"),
        units=(capped_position.units[0],),
    )
    signals, _ = evaluate_signals(
        symbol="TEST",
        completed_candles=candles,
        current_price=Decimal("100.20"),
        state=StrategyState(),
        position=position_for_add,
        pyramid_step_n=Decimal("0.5"),
        max_units_per_symbol=4,
    )
    assert signals == []

    signals, _ = evaluate_signals(
        symbol="TEST",
        completed_candles=candles,
        current_price=Decimal("101.10"),
        state=StrategyState(),
        position=position_for_add,
        pyramid_step_n=Decimal("0.5"),
        max_units_per_symbol=4,
    )
    assert len(signals) == 1
    assert signals[0].kind == SignalKind.PYRAMID


def test_decimal_parsing_for_candle_values():
    raw = {
        "timestamp": "2026-01-01T00:00:00+00:00",
        "openPrice": "10.25",
        "highPrice": "12.50",
        "lowPrice": "9.80",
        "closePrice": "11.10",
        "volume": "10000",
        "symbol": "TEST",
    }
    candle = Candle.from_api(raw)

    assert all(isinstance(v, Decimal) for v in [candle.open, candle.high, candle.low, candle.close, candle.volume])


def test_apply_trade_outcome_sets_s1_skip():
    state = apply_trade_outcomes(
        StrategyState(),
        [TradeOutcome(symbol="TEST", system=TurtleSystem.S1, realized_pnl=Decimal("12.34"))],
    )
    assert "TEST" in state.pending_s1_skip
