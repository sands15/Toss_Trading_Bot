from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from turtle_bot.indicators import donchian_channel, compute_n_turtle, true_range
from turtle_bot.domain import Candle


def _c(t: str, o: str, h: str, l: str, c: str) -> Candle:
    return Candle(
        timestamp=datetime.now(timezone.utc),
        symbol="TEST",
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(l),
        close=Decimal(c),
        volume=Decimal("100"),
    )


def test_donchian_excludes_current_bar():
    candles = [
        _c("1", "100", "110", "90", "100"),
        _c("2", "100", "120", "95", "110"),
        _c("3", "105", "150", "101", "140"),
    ]

    high, low = donchian_channel(candles, period=2, exclude_current=True)
    assert high == Decimal("120")
    assert low == Decimal("90")


def test_true_range_uses_prior_close():
    previous = _c("1", "100", "110", "90", "100")
    current = _c("2", "100", "105", "80", "82")
    assert true_range(current, previous) == Decimal("25")


def test_compute_turtle_n_requires_history():
    candles = [_c(str(i), "1", "1", "1", "1") for i in range(20)]
    assert compute_n_turtle(candles) is None
