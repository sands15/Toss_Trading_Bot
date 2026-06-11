from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from turtle_bot.backtest import (
    BacktestConfig,
    BacktestCosts,
    BacktestEngine,
    export_backtest_report_json,
    load_candles_csv,
)
from turtle_bot.domain import Candle


def _c(
    day: int,
    open_: str,
    high: str,
    low: str,
    close: str,
    *,
    symbol: str = "TEST",
) -> Candle:
    return Candle(
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=day),
        symbol=symbol,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("100"),
    )


def _flat_history(days: int = 21) -> list[Candle]:
    return [_c(day, "100", "101", "99", "100") for day in range(days)]


def _flat_history_for(symbol: str, days: int = 21) -> list[Candle]:
    return [
        _c(day, "100", "101", "99", "100", symbol=symbol)
        for day in range(days)
    ]


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


def test_backtest_risk_based_unit_sizing_uses_equity_stop_and_n():
    candles = _flat_history()
    candles.append(_c(21, "100", "105", "99", "104"))
    config = BacktestConfig(
        initial_equity=Decimal("1000"),
        risk_pct_per_unit=Decimal("0.02"),
    )

    result = BacktestEngine(config).run(candles)

    assert result.audit_log[0].qty == Decimal("5")
    assert result.equity_curve[-1].cash == Decimal("495")
    assert result.equity_curve[-1].position_value == Decimal("520")


def test_portfolio_backtest_shares_cash_across_symbols():
    candles = []
    candles.extend(_flat_history_for("AAA"))
    candles.extend(_flat_history_for("BBB"))
    candles.append(_c(21, "100", "105", "99", "104", symbol="AAA"))
    candles.append(_c(21, "100", "105", "99", "104", symbol="BBB"))

    result = BacktestEngine(BacktestConfig(initial_equity=Decimal("1000"))).run_portfolio(candles)

    assert [event.symbol for event in result.audit_log] == ["AAA", "BBB"]
    assert [event.kind for event in result.audit_log] == ["ENTRY", "ENTRY"]
    assert result.equity_curve[-1].cash == Decimal("798")
    assert result.equity_curve[-1].position_value == Decimal("208")
    assert result.final_equity == Decimal("1006")


def test_portfolio_backtest_processes_exits_before_new_entries():
    candles = []
    candles.extend(_flat_history_for("AAA"))
    candles.extend(_flat_history_for("BBB"))
    candles.append(_c(21, "100", "105", "99", "104", symbol="AAA"))
    candles.append(_c(21, "100", "100", "99", "100", symbol="BBB"))
    candles.append(_c(22, "100", "110", "96", "100", symbol="AAA"))
    candles.append(_c(22, "100", "105", "99", "104", symbol="BBB"))

    result = BacktestEngine(BacktestConfig(initial_equity=Decimal("150"))).run_portfolio(candles)

    assert [(event.symbol, event.kind) for event in result.audit_log] == [
        ("AAA", "ENTRY"),
        ("AAA", "STOP"),
        ("BBB", "ENTRY"),
    ]
    assert result.equity_curve[-1].cash == Decimal("45")


def test_export_backtest_report_json_preserves_decimal_strings(tmp_path):
    candles = _flat_history()
    candles.append(_c(21, "100", "105", "99", "104"))
    result = BacktestEngine(BacktestConfig(initial_equity=Decimal("1000"))).run(candles)
    report_path = tmp_path / "reports" / "backtest.json"

    export_backtest_report_json(result, report_path)

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["final_equity"] == "1003"
    assert report["audit_log"][0]["fill_price"] == "101"
    assert report["strategy_state"]["pending_s1_skip"] == []
