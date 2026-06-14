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
    summarize_backtest_result,
)
from turtle_bot.cli import run
from turtle_bot.domain import Candle, PositionDirection


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


def _write_pit_rows(path, *, days: int, symbols: dict[str, bool]) -> None:
    rows = ["as_of,symbol,included,reasons"]
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for day in range(days):
        as_of = (start + timedelta(days=day)).date().isoformat()
        for symbol, included in symbols.items():
            rows.append(
                f"{as_of},{symbol},{'true' if included else 'false'},"
                f"{'included' if included else 'excluded'}"
            )
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


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


def test_backtest_short_entry_marks_liability_when_enabled():
    candles = _flat_history()
    candles.append(_c(21, "95", "100", "94", "95"))

    result = BacktestEngine(
        BacktestConfig(
            initial_equity=Decimal("1000"),
            allowed_directions=(PositionDirection.SHORT,),
        )
    ).run(candles)

    assert len(result.audit_log) == 1
    assert result.audit_log[0].action == "fill_sell"
    assert result.audit_log[0].reason == "gap_breakout:s1_short_breakout"
    assert result.equity_curve[-1].cash == Decimal("1095")
    assert result.equity_curve[-1].position_value == Decimal("-95")
    assert result.final_equity == Decimal("1000")


def test_backtest_short_stop_buy_to_cover_realized_loss():
    candles = _flat_history()
    candles.append(_c(21, "95", "100", "94", "95"))
    candles.append(_c(22, "100", "105", "94", "100"))

    result = BacktestEngine(
        BacktestConfig(
            initial_equity=Decimal("1000"),
            allowed_directions=(PositionDirection.SHORT,),
        )
    ).run(candles)

    assert [event.action for event in result.audit_log] == ["fill_sell", "fill_buy"]
    assert len(result.trades) == 1
    assert result.trades[0].direction == "SHORT"
    assert result.trades[0].exit_price == Decimal("100")
    assert result.trades[0].realized_pnl == Decimal("-5")
    assert result.final_equity == Decimal("995")


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


def test_portfolio_backtest_mixed_long_short_marks_signed_position_value():
    candles = []
    candles.extend(_flat_history_for("AAA"))
    candles.extend(_flat_history_for("BBB"))
    candles.append(_c(21, "105", "106", "100", "104", symbol="AAA"))
    candles.append(_c(21, "95", "100", "94", "94", symbol="BBB"))

    result = BacktestEngine(
        BacktestConfig(
            initial_equity=Decimal("1000"),
            allowed_directions=(PositionDirection.LONG, PositionDirection.SHORT),
        )
    ).run_portfolio(candles)

    assert [(event.symbol, event.action) for event in result.audit_log] == [
        ("AAA", "fill_buy"),
        ("BBB", "fill_sell"),
    ]
    assert result.equity_curve[-1].cash == Decimal("990")
    assert result.equity_curve[-1].position_value == Decimal("10")
    assert result.final_equity == Decimal("1000")


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


def test_portfolio_entry_filter_blocks_unaccepted_new_entries():
    candles = []
    candles.extend(_flat_history_for("AAA"))
    candles.extend(_flat_history_for("BBB"))
    candles.append(_c(21, "100", "105", "99", "104", symbol="AAA"))
    candles.append(_c(21, "100", "105", "99", "104", symbol="BBB"))

    result = BacktestEngine(BacktestConfig(initial_equity=Decimal("1000"))).run_portfolio(
        candles,
        entry_filter=lambda _timestamp, symbol: symbol == "AAA",
    )

    assert [event.symbol for event in result.audit_log] == ["AAA"]


def test_portfolio_backtest_blocks_entries_after_total_unit_cap():
    candles = []
    candles.extend(_flat_history_for("AAA"))
    candles.extend(_flat_history_for("BBB"))
    candles.append(_c(21, "100", "105", "99", "104", symbol="AAA"))
    candles.append(_c(21, "100", "105", "99", "104", symbol="BBB"))

    result = BacktestEngine(
        BacktestConfig(
            initial_equity=Decimal("1000"),
            max_total_long_units=1,
        )
    ).run_portfolio(candles)

    assert [(event.symbol, event.kind, event.reason) for event in result.audit_log] == [
        ("AAA", "ENTRY", "s1_breakout"),
        ("BBB", "BLOCK", "max_total_long_units"),
    ]


def test_portfolio_backtest_blocks_short_entries_after_short_unit_cap():
    candles = []
    candles.extend(_flat_history_for("AAA"))
    candles.extend(_flat_history_for("BBB"))
    candles.append(_c(21, "95", "100", "94", "95", symbol="AAA"))
    candles.append(_c(21, "95", "100", "94", "95", symbol="BBB"))

    result = BacktestEngine(
        BacktestConfig(
            initial_equity=Decimal("1000"),
            allowed_directions=(PositionDirection.SHORT,),
            max_total_short_units=1,
        )
    ).run_portfolio(candles)

    assert [(event.symbol, event.kind, event.reason) for event in result.audit_log] == [
        ("AAA", "ENTRY", "gap_breakout:s1_short_breakout"),
        ("BBB", "BLOCK", "max_total_short_units"),
    ]


def test_cli_portfolio_backtest_uses_configured_pit_universe_csv(tmp_path, capsys):
    data_path = tmp_path / "candles.csv"
    pit_path = tmp_path / "data" / "pit.csv"
    pit_path.parent.mkdir()
    config_path = tmp_path / "config.yaml"

    candles = []
    candles.extend(_flat_history_for("AAA"))
    candles.extend(_flat_history_for("BBB"))
    candles.append(_c(21, "100", "105", "99", "104", symbol="AAA"))
    candles.append(_c(21, "100", "105", "99", "104", symbol="BBB"))
    rows = ["timestamp,symbol,open,high,low,close,volume"]
    for candle in candles:
        rows.append(
            ",".join(
                [
                    candle.timestamp.isoformat(),
                    candle.symbol,
                    str(candle.open),
                    str(candle.high),
                    str(candle.low),
                    str(candle.close),
                    str(candle.volume),
                ]
            )
        )
    data_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    _write_pit_rows(pit_path, days=22, symbols={"AAA": True, "BBB": False})
    config_path.write_text(
        "\n".join(
            [
                "runtime:",
                "  pit_universe_csv: data/pit.csv",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = run(
        [
            "--config",
            str(config_path),
            "--backtest-csv",
            str(data_path),
            "--backtest-portfolio",
            "--initial-equity",
            "1000",
        ]
    )

    printed = json.loads(capsys.readouterr().out)
    assert result == 0
    assert [event["symbol"] for event in printed["audit_log"]] == ["AAA"]


def test_export_backtest_report_json_preserves_decimal_strings(tmp_path):
    candles = _flat_history()
    candles.append(_c(21, "100", "105", "99", "104"))
    result = BacktestEngine(BacktestConfig(initial_equity=Decimal("1000"))).run(candles)
    report_path = tmp_path / "reports" / "backtest.json"

    export_backtest_report_json(result, report_path)

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["final_equity"] == "1003"
    assert report["summary"]["initial_equity"] == "1000"
    assert report["summary"]["return_pct"] == "0.300"
    assert report["audit_log"][0]["fill_price"] == "101"
    assert report["strategy_state"]["pending_s1_skip"] == []


def test_backtest_summary_reports_loss_and_mdd():
    candles = _flat_history()
    candles.append(_c(21, "100", "105", "99", "104"))
    candles.append(_c(22, "100", "110", "96", "100"))

    result = BacktestEngine(BacktestConfig(initial_equity=Decimal("1000"))).run(candles)
    summary = summarize_backtest_result(result)

    assert summary["initial_equity"] == Decimal("1000")
    assert summary["final_equity"] == Decimal("996")
    assert summary["min_equity"] == Decimal("996")
    assert summary["min_return_pct"] == Decimal("-0.400")
    assert summary["loss_pct"] == Decimal("0.400")
    assert summary["max_drawdown"] == Decimal("7")
    assert summary["max_drawdown_pct"] == Decimal("0.6979062811565304087736789631")
    assert summary["trade_count"] == 1
    assert summary["losing_trades"] == 1


def test_cli_backtest_csv_writes_report_with_summary(tmp_path, capsys):
    csv_path = tmp_path / "candles.csv"
    report_path = tmp_path / "reports" / "backtest.json"
    rows = ["timestamp,symbol,open,high,low,close,volume"]
    for candle in _flat_history():
        rows.append(
            ",".join(
                [
                    candle.timestamp.isoformat(),
                    candle.symbol,
                    str(candle.open),
                    str(candle.high),
                    str(candle.low),
                    str(candle.close),
                    str(candle.volume),
                ]
            )
        )
    rows.append("2026-01-22T00:00:00+00:00,TEST,100,105,99,104,100")
    csv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    result = run(
        [
            "--backtest-csv",
            str(csv_path),
            "--initial-equity",
            "1000",
            "--backtest-report",
            str(report_path),
        ]
    )

    printed = json.loads(capsys.readouterr().out)
    saved = json.loads(report_path.read_text(encoding="utf-8"))
    assert result == 0
    assert printed == saved
    assert saved["summary"]["final_equity"] == "1003"
    assert saved["summary"]["max_drawdown_pct"] == "0"


def test_cli_backtest_direction_short_enables_short_signals(tmp_path, capsys):
    csv_path = tmp_path / "candles.csv"
    rows = ["timestamp,symbol,open,high,low,close,volume"]
    for candle in _flat_history():
        rows.append(
            ",".join(
                [
                    candle.timestamp.isoformat(),
                    candle.symbol,
                    str(candle.open),
                    str(candle.high),
                    str(candle.low),
                    str(candle.close),
                    str(candle.volume),
                ]
            )
        )
    rows.append("2026-01-22T00:00:00+00:00,TEST,95,100,94,95,100")
    csv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    result = run(
        [
            "--backtest-csv",
            str(csv_path),
            "--initial-equity",
            "1000",
            "--backtest-direction",
            "short",
        ]
    )

    printed = json.loads(capsys.readouterr().out)
    assert result == 0
    assert printed["audit_log"][0]["action"] == "fill_sell"
    assert printed["audit_log"][0]["reason"] == "gap_breakout:s1_short_breakout"
