from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from turtle_bot.backtest import BacktestConfig, BacktestEngine
from turtle_bot.cli import run
from turtle_bot.domain import Candle, PositionDirection
from turtle_bot.pit_universe import PitUniverseCoverageError, load_pit_universe_csv
from turtle_bot.scan_backtest import (
    ScanBacktestConfig,
    run_scan_backtest,
)


def _c(
    day: int,
    open_: str,
    high: str,
    low: str,
    close: str,
    *,
    symbol: str,
) -> Candle:
    return Candle(
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=day),
        symbol=symbol,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("1000000"),
    )


def _history(symbol: str, close: str) -> list[Candle]:
    return [
        _c(day, "100", "101", "99", close, symbol=symbol)
        for day in range(56)
    ]


def _write_pit_csv(path, *, days: int, symbols: dict[str, bool]) -> None:
    rows = ["as_of,symbol,included,reasons"]
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for day in range(days):
        as_of = (start + timedelta(days=day)).date().isoformat()
        for symbol, included in symbols.items():
            rows.append(
                ",".join(
                    [
                        as_of,
                        symbol,
                        "true" if included else "false",
                        "included" if included else "excluded",
                    ]
                )
            )
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def test_scan_backtest_accepts_only_ranked_candidates():
    candles = []
    candles.extend(_history("AAA", "100"))
    candles.extend(_history("BBB", "90"))
    candles.append(_c(56, "100", "105", "99", "104", symbol="AAA"))
    candles.append(_c(56, "90", "105", "89", "104", symbol="BBB"))

    result = run_scan_backtest(
        candles,
        engine=BacktestEngine(BacktestConfig(initial_equity=Decimal("1000"))),
        config=ScanBacktestConfig(
            scan_top_n=2,
            accept_top_n=1,
            min_price=Decimal("0"),
            min_average_daily_value=Decimal("0"),
        ),
    )

    assert result.decisions[-1].accepted_symbols == ("AAA",)
    assert [event.symbol for event in result.backtest.audit_log] == ["AAA"]


def test_scan_backtest_short_recommendations_enter_short_direction():
    candles = []
    candles.extend(_history("AAA", "100"))
    candles.extend(_history("BBB", "100"))
    candles.append(_c(56, "100", "100", "99", "100", symbol="AAA"))
    candles.append(_c(56, "95", "100", "94", "95", symbol="BBB"))

    result = run_scan_backtest(
        candles,
        engine=BacktestEngine(
            BacktestConfig(
                initial_equity=Decimal("1000"),
                allowed_directions=(PositionDirection.SHORT,),
            )
        ),
        config=ScanBacktestConfig(
            scan_top_n=2,
            accept_top_n=1,
            min_price=Decimal("0"),
            min_average_daily_value=Decimal("0"),
            scan_directions=(PositionDirection.SHORT,),
        ),
    )

    assert result.decisions[-1].recommended[0].direction == PositionDirection.SHORT
    assert result.decisions[-1].accepted_entries == (("AAA", PositionDirection.SHORT),)
    assert result.backtest.audit_log[0].symbol == "AAA"
    assert result.backtest.audit_log[0].action == "fill_sell"


def test_scan_backtest_direction_acceptance_blocks_opposite_direction():
    candles = []
    candles.extend(_history("AAA", "100"))
    candles.append(_c(56, "95", "105", "94", "100", symbol="AAA"))

    result = run_scan_backtest(
        candles,
        engine=BacktestEngine(
            BacktestConfig(
                initial_equity=Decimal("1000"),
                allowed_directions=(PositionDirection.LONG, PositionDirection.SHORT),
            )
        ),
        config=ScanBacktestConfig(
            scan_top_n=1,
            accept_top_n=1,
            min_price=Decimal("0"),
            min_average_daily_value=Decimal("0"),
            scan_directions=(PositionDirection.SHORT,),
        ),
    )

    assert result.decisions[-1].accepted_entries == (("AAA", PositionDirection.SHORT),)
    assert result.backtest.audit_log[0].action == "fill_sell"


def test_scan_backtest_pit_universe_filters_recommendations(tmp_path):
    candles = []
    candles.extend(_history("AAA", "100"))
    candles.extend(_history("BBB", "90"))
    candles.append(_c(56, "100", "105", "99", "104", symbol="AAA"))
    candles.append(_c(56, "90", "105", "89", "104", symbol="BBB"))
    pit_path = tmp_path / "pit.csv"
    _write_pit_csv(pit_path, days=57, symbols={"AAA": True, "BBB": False})

    result = run_scan_backtest(
        candles,
        engine=BacktestEngine(BacktestConfig(initial_equity=Decimal("1000"))),
        config=ScanBacktestConfig(
            scan_top_n=2,
            accept_top_n=2,
            min_price=Decimal("0"),
            min_average_daily_value=Decimal("0"),
            pit_universe=load_pit_universe_csv(pit_path),
        ),
    )

    assert result.decisions[-1].accepted_symbols == ("AAA",)
    assert [candidate.symbol for candidate in result.decisions[-1].recommended] == ["AAA"]
    assert [event.symbol for event in result.backtest.audit_log] == ["AAA"]


def test_scan_backtest_pit_universe_missing_date_blocks(tmp_path):
    candles = []
    candles.extend(_history("AAA", "100"))
    candles.append(_c(56, "100", "105", "99", "104", symbol="AAA"))
    pit_path = tmp_path / "pit.csv"
    _write_pit_csv(pit_path, days=1, symbols={"AAA": True})

    with pytest.raises(PitUniverseCoverageError, match="2026-01-02"):
        run_scan_backtest(
            candles,
            engine=BacktestEngine(BacktestConfig(initial_equity=Decimal("1000"))),
            config=ScanBacktestConfig(
                scan_top_n=2,
                accept_top_n=2,
                min_price=Decimal("0"),
                min_average_daily_value=Decimal("0"),
                pit_universe=load_pit_universe_csv(pit_path),
            ),
        )


def test_cli_scan_backtest_writes_report(tmp_path, capsys):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    csv_path = data_dir / "AAA.csv"
    rows = ["timestamp,symbol,open,high,low,close,volume"]
    for candle in _history("AAA", "100"):
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
    rows.append("2026-02-26T00:00:00+00:00,AAA,100,105,99,104,1000000")
    csv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    report_path = tmp_path / "reports" / "scan.json"

    result = run(
        [
            "--scan-backtest",
            "--scan-data-dir",
            str(data_dir),
            "--scan-top-n",
            "5",
            "--accept-top-n",
            "1",
            "--scan-min-average-daily-value",
            "0",
            "--scan-min-price",
            "0",
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
    assert saved["scan"]["symbol_count"] == 1
    assert saved["summary"]["trade_count"] == 0
