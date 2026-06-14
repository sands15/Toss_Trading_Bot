from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from turtle_bot.cli import run
from turtle_bot.domain import Candle
from turtle_bot.pit_universe import PitUniverseCoverageError, load_pit_universe_csv
from turtle_bot.momentum_backtest import (
    MomentumBacktestConfig,
    run_momentum_backtest,
)


def _c(day: int, symbol: str, close: Decimal) -> Candle:
    return Candle(
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=day),
        symbol=symbol,
        open=close,
        high=close + Decimal("1"),
        low=close - Decimal("1"),
        close=close,
        volume=Decimal("1000000"),
    )


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


def test_momentum_backtest_enters_relative_winner_when_market_filter_passes():
    candles = []
    for day in range(30):
        candles.append(_c(day, "SPY", Decimal("100") + Decimal(day)))
        candles.append(_c(day, "AAA", Decimal("100") + Decimal(day * 2)))
        candles.append(_c(day, "BBB", Decimal("100") + Decimal(day)))

    result = run_momentum_backtest(
        candles,
        config=MomentumBacktestConfig(
            initial_equity=Decimal("1000"),
            momentum_lookback_days=10,
            momentum_skip_days=2,
            trend_ma_days=5,
            exit_ma_days=3,
            accept_top_n=1,
            max_positions=1,
            target_position_pct=Decimal("0.5"),
            min_price=Decimal("0"),
            min_average_daily_value=Decimal("0"),
        ),
    )

    entries = [event for event in result.backtest.audit_log if event.kind == "ENTRY"]
    assert entries
    assert entries[0].symbol == "AAA"


def test_momentum_backtest_market_filter_blocks_new_entries():
    candles = []
    for day in range(30):
        candles.append(_c(day, "SPY", Decimal("130") - Decimal(day)))
        candles.append(_c(day, "AAA", Decimal("100") + Decimal(day * 2)))

    result = run_momentum_backtest(
        candles,
        config=MomentumBacktestConfig(
            initial_equity=Decimal("1000"),
            momentum_lookback_days=10,
            momentum_skip_days=2,
            trend_ma_days=5,
            exit_ma_days=3,
            min_price=Decimal("0"),
            min_average_daily_value=Decimal("0"),
        ),
    )

    assert result.backtest.audit_log == ()


def test_momentum_backtest_pit_universe_filters_candidates(tmp_path):
    candles = []
    for day in range(40):
        candles.append(_c(day, "SPY", Decimal("100") + Decimal(day)))
        candles.append(_c(day, "AAA", Decimal("100") + Decimal(day * 2)))
        candles.append(_c(day, "BBB", Decimal("110") + Decimal(day * 3)))
    pit_path = tmp_path / "pit.csv"
    _write_pit_csv(pit_path, days=40, symbols={"AAA": False, "BBB": True})

    result = run_momentum_backtest(
        candles,
        config=MomentumBacktestConfig(
            initial_equity=Decimal("1000"),
            momentum_lookback_days=10,
            momentum_skip_days=2,
            trend_ma_days=5,
            exit_ma_days=3,
            accept_top_n=1,
            max_positions=1,
            target_position_pct=Decimal("0.5"),
            min_price=Decimal("0"),
            min_average_daily_value=Decimal("0"),
            pit_universe=load_pit_universe_csv(pit_path),
        ),
    )

    assert [event.symbol for event in result.backtest.audit_log] == ["BBB"]
    assert all(
        "AAA" not in {candidate.symbol for candidate in decision.recommended}
        for decision in result.decisions
        if decision.recommended
    )


def test_momentum_backtest_pit_universe_missing_date_blocks(tmp_path):
    candles = []
    for day in range(30):
        candles.append(_c(day, "SPY", Decimal("100") + Decimal(day)))
        candles.append(_c(day, "AAA", Decimal("100") + Decimal(day * 2)))
    pit_path = tmp_path / "pit.csv"
    _write_pit_csv(pit_path, days=1, symbols={"AAA": True, "SPY": True})

    with pytest.raises(PitUniverseCoverageError, match="2026-01-02"):
        run_momentum_backtest(
            candles,
            config=MomentumBacktestConfig(
                initial_equity=Decimal("1000"),
                momentum_lookback_days=10,
                momentum_skip_days=2,
                trend_ma_days=5,
                exit_ma_days=3,
                min_price=Decimal("0"),
                min_average_daily_value=Decimal("0"),
                pit_universe=load_pit_universe_csv(pit_path),
            ),
        )


def test_cli_momentum_backtest_writes_report(tmp_path, capsys):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for symbol, multiplier in (("SPY", 1), ("AAA", 2)):
        rows = ["timestamp,symbol,open,high,low,close,volume"]
        for day in range(30):
            close = Decimal("100") + Decimal(day * multiplier)
            rows.append(
                ",".join(
                    [
                        (
                            datetime(2026, 1, 1, tzinfo=timezone.utc)
                            + timedelta(days=day)
                        ).isoformat(),
                        symbol,
                        str(close),
                        str(close + Decimal("1")),
                        str(close - Decimal("1")),
                        str(close),
                        "1000000",
                    ]
                )
            )
        (data_dir / f"{symbol}.csv").write_text(
            "\n".join(rows) + "\n",
            encoding="utf-8",
        )
    report_path = tmp_path / "reports" / "momentum.json"

    result = run(
        [
            "--momentum-backtest",
            "--momentum-data-dir",
            str(data_dir),
            "--initial-equity",
            "1000",
            "--momentum-lookback-days",
            "10",
            "--momentum-skip-days",
            "2",
            "--momentum-trend-ma-days",
            "5",
            "--momentum-exit-ma-days",
            "3",
            "--momentum-min-price",
            "0",
            "--momentum-min-average-daily-value",
            "0",
            "--momentum-target-position-pct",
            "0.5",
            "--backtest-report",
            str(report_path),
        ]
    )

    printed = json.loads(capsys.readouterr().out)
    saved = json.loads(report_path.read_text(encoding="utf-8"))
    assert result == 0
    assert printed == saved
    assert saved["momentum"]["symbol_count"] == 1
    assert saved["momentum"]["accepted_days"] > 0


def test_cli_momentum_backtest_reads_pit_universe_csv(tmp_path, capsys):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for symbol, multiplier in (("SPY", 1), ("AAA", 2)):
        rows = ["timestamp,symbol,open,high,low,close,volume"]
        for day in range(40):
            close = Decimal("100") + Decimal(day * multiplier)
            rows.append(
                ",".join(
                    [
                        (
                            datetime(2026, 1, 1, tzinfo=timezone.utc)
                            + timedelta(days=day)
                        ).isoformat(),
                        symbol,
                        str(close),
                        str(close + Decimal("1")),
                        str(close - Decimal("1")),
                        str(close),
                        "1000000",
                    ]
                )
            )
        (data_dir / f"{symbol}.csv").write_text(
            "\n".join(rows) + "\n",
            encoding="utf-8",
        )
    pit_path = tmp_path / "pit.csv"
    _write_pit_csv(pit_path, days=40, symbols={"AAA": False, "SPY": True})
    report_path = tmp_path / "reports" / "momentum.json"

    result = run(
        [
            "--momentum-backtest",
            "--momentum-data-dir",
            str(data_dir),
            "--pit-universe-csv",
            str(pit_path),
            "--initial-equity",
            "1000",
            "--momentum-lookback-days",
            "10",
            "--momentum-skip-days",
            "2",
            "--momentum-trend-ma-days",
            "5",
            "--momentum-exit-ma-days",
            "3",
            "--momentum-min-price",
            "0",
            "--momentum-min-average-daily-value",
            "0",
            "--momentum-target-position-pct",
            "0.5",
            "--backtest-report",
            str(report_path),
        ]
    )

    printed = json.loads(capsys.readouterr().out)
    saved = json.loads(report_path.read_text(encoding="utf-8"))
    assert result == 0
    assert printed == saved
    assert printed["momentum"]["accepted_days"] == 0
    assert printed["audit_log"] == []
    assert printed["momentum"]["config"]["pit_universe_enabled"] is True
