from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from .domain import Candle, as_decimal


def true_range(current: Candle, previous: Candle | None = None) -> Decimal:
    if previous is None:
        return current.high - current.low
    return max(
        current.high - current.low,
        abs(current.high - previous.close),
        abs(current.low - previous.close),
    )


def donchian_channel(
    candles: Sequence[Candle],
    period: int,
    *,
    exclude_current: bool = True,
) -> tuple[Decimal | None, Decimal | None]:
    if period <= 0:
        raise ValueError("period must be positive")

    bars = candles[:-1] if exclude_current else candles
    if len(bars) < period:
        return None, None

    window = bars[-period:]
    highs = [c.high for c in window]
    lows = [c.low for c in window]
    return max(highs), min(lows)


def _true_ranges(candles: Sequence[Candle]) -> list[Decimal]:
    return [true_range(curr, prev) for prev, curr in zip(candles, candles[1:])]


def compute_n_turtle(candles: Sequence[Candle]) -> Decimal | None:
    """Compute Turtle N with 20-day smoothing over complete candles."""

    if len(candles) < 21:
        return None

    trs = _true_ranges(candles)
    if len(trs) < 20:
        return None

    n = sum(trs[:20]) / as_decimal(20)
    for tr in trs[20:]:
        n = (as_decimal(19) * n + tr) / as_decimal(20)
    return n


def compute_n_atr20(candles: Sequence[Candle]) -> Decimal | None:
    if len(candles) < 21:
        return None

    trs = _true_ranges(candles)
    if len(trs) < 20:
        return None
    return sum(trs[-20:]) / as_decimal(20)
