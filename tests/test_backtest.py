from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from turtle_bot.backtest import (
    BacktestConfig,
    BacktestCosts,
    BacktestEngine,
    load_candles_csv,
)
from turtle_bot.domain import Candle


def _c(day: int, open_: str, high: str, low: str, close: str) -> Candle:
    return Candle(
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=day),
        symbol="TEST",
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("100"),
    )


def _flat_history(days: int = 21) -> list[Candle]:
    return [_c(day, "100", "101", "99", "100") for day in range(days)]


def test_load_candles_csv_decimal_safe(tmp_path):
    csv_path = tmp_path / "candles.csv"
    csv_path.write_text(
        "timestamp,symbol,open,high,low,close,volume\n"
        "2026-01-01T00:00:00+00:00,TEST,10.1,11.2,9.3,10.4,1000\n",
        encoding="utf-8",
    )

    candles = load_candles_csv(csv_path)

    assert len(candles) == 1
    assert candles[0].symbol == "TEST"
    assert candles[0].open == Decimal("10.1")
    assert isinstance(candles[0].close, Decimal)


def test_backtest_gap_breakout_fills_at_open_not_trigger():
    candles = _flat_history()
    candles.append(_c(21, "105", "106", "100", "104"))

    result = BacktestEngine(BacktestConfig(initial_equity=Decimal("1000"))).run(candles)

    assert len(result.audit_log) == 1
    event = result.audit_log[0]
    assert event.kind == "ENTRY"
    assert event.trigger_price == Decimal("101")
    assert event.fill_price == Decimal("105")
    assert event.reason == "gap_breakout:s1_breakout"


def test_backtest_same_bar_stop_beats_pyramid():
    candles = _flat_history()
    candles.append(_c(21, "100", "105", "99", "104"))
    candles.append(_c(22, "100", "110", "96", "100"))

    result = BacktestEngine(BacktestConfig(initial_equity=Decimal("1000"))).run(candles)

    assert [event.kind for event in result.audit_log] == ["ENTRY", "STOP"]
    assert len(result.trades) == 1
    assert result.trades[0].exit_price == Decimal("97")
    assert result.trades[0].realized_pnl == Decimal("-4")
    assert result.final_equity == Decimal("996")


def test_backtest_equity_curve_marks_open_position_to_close():
    candles = _flat_history()
    candles.append(_c(21, "100", "105", "99", "104"))

    result = BacktestEngine(BacktestConfig(initial_equity=Decimal("1000"))).run(candles)

    assert result.final_equity == Decimal("1003")
    assert result.equity_curve[-1].cash == Decimal("899")
    assert result.equity_curve[-1].position_value == Decimal("104")


def test_backtest_cost_hooks_apply_to_fills_and_cash():
    candles = _flat_history()
    candles.append(_c(21, "100", "105", "99", "104"))
    config = BacktestConfig(
        initial_equity=Decimal("1000"),
        costs=BacktestCosts(
            commission_rate=Decimal("0.01"),
            fixed_commission=Decimal("1"),
            slippage_rate=Decimal("0.01"),
        ),
    )

    result = BacktestEngine(config).run(candles)

    assert result.audit_log[0].fill_price == Decimal("102.01")
    assert result.equity_curve[-1].cash == Decimal("895.9699")
