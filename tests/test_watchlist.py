from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from turtle_bot.domain import Candle
from turtle_bot.watchlist import WatchlistBuilder


def _candle(symbol: str, time: datetime, *, high: str, close: str) -> Candle:
    return Candle(
        timestamp=time,
        symbol=symbol,
        open=Decimal(high),
        high=Decimal(high),
        low=Decimal("90"),
        close=Decimal(close),
        volume=Decimal("1"),
    )


def _base_sequence(symbol: str) -> list[Candle]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        Candle(
            timestamp=start + timedelta(days=idx),
            symbol=symbol,
            open=Decimal("100"),
            high=Decimal("100"),
            low=Decimal("90"),
            close=Decimal("95"),
            volume=Decimal("1"),
        )
        for idx in range(56)
    ]


def test_builder_excludes_current_candle_for_levels_by_default() -> None:
    candles = _base_sequence("AAA")
    candles.append(
        _candle(
            "AAA",
            datetime(2026, 3, 1, tzinfo=timezone.utc),
            high="300",
            close="300",
        )
    )

    watchlist = WatchlistBuilder(top_n=5).build({"AAA": candles})
    row = watchlist.rows[0]
    assert row.entry_high_20 == Decimal("100")
    assert row.entry_high_55 == Decimal("100")
    assert row.current_price == Decimal("95")
    assert row.nearest_distance == Decimal("5")
    assert "20일 돌파선" in row.reason
    assert "현재가 95" in row.reason


def test_watchlist_marks_new_symbols() -> None:
    symbols = {
        "AAA": _base_sequence("AAA")
        + [_candle("AAA", datetime(2026, 3, 1, tzinfo=timezone.utc), high="100", close="95")],
        "BBB": _base_sequence("BBB")
        + [_candle("BBB", datetime(2026, 3, 1, tzinfo=timezone.utc), high="101", close="95")],
    }
    rows = WatchlistBuilder(top_n=10).build(symbols, previous_watchlist=("AAA",)).rows

    by_symbol = {row.symbol: row.is_new for row in rows}
    by_reason = {row.symbol: row.reason for row in rows}
    assert by_symbol["AAA"] is False
    assert by_symbol["BBB"] is True
    assert by_reason["BBB"].startswith("새 후보.")


def test_watchlist_ranks_by_distance_to_breakouts() -> None:
    candles_a = _base_sequence("A")
    candles_a.append(
        _candle("A", datetime(2026, 3, 1, tzinfo=timezone.utc), high="100", close="95")
    )

    candles_b = _base_sequence("B")
    candles_b[-1] = _candle(
        "B",
        candles_b[-1].timestamp,
        high="100",
        close="97",
    )
    candles_b.append(
        _candle("B", datetime(2026, 3, 1, tzinfo=timezone.utc), high="100", close="101")
    )

    symbols = {
        "A": candles_a,
        "B": candles_b,
    }

    watchlist = WatchlistBuilder(top_n=2).build(symbols)
    assert watchlist.rows[0].symbol == "B"
