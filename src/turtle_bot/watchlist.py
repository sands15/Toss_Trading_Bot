from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Mapping, Sequence

from .domain import Candle
from .indicators import donchian_channel


@dataclass(frozen=True)
class WatchlistRow:
    symbol: str
    current_price: Decimal
    entry_high_20: Decimal | None
    entry_high_55: Decimal | None
    distance_to_20: Decimal | None
    distance_to_55: Decimal | None
    nearest_distance: Decimal
    is_new: bool = False


@dataclass(frozen=True)
class Watchlist:
    generated_at: datetime
    rows: tuple[WatchlistRow, ...]

    def symbols(self) -> tuple[str, ...]:
        return tuple(row.symbol for row in self.rows)


class WatchlistBuilder:
    """Build an operational watchlist from provided completed candles."""

    def __init__(self, *, top_n: int = 20, exclude_current: bool = True):
        if top_n <= 0:
            raise ValueError("top_n must be positive")
        self.top_n = top_n
        self.exclude_current = exclude_current

    def build(
        self,
        symbol_candles: Mapping[str, Sequence[Candle]],
        *,
        previous_watchlist: Sequence[str] | set[str] | None = None,
        generated_at: datetime | None = None,
    ) -> Watchlist:
        previous = set(previous_watchlist or ())
        rows: list[WatchlistRow] = []

        for symbol, candles in symbol_candles.items():
            if not candles:
                continue

            entry_high_20, _ = donchian_channel(
                candles,
                period=20,
                exclude_current=self.exclude_current,
            )
            entry_high_55, _ = donchian_channel(
                candles,
                period=55,
                exclude_current=self.exclude_current,
            )

            if entry_high_20 is None and entry_high_55 is None:
                continue

            current_price = candles[-1].close if not self.exclude_current else candles[-2].close
            distance_to_20 = (
                abs(current_price - entry_high_20)
                if entry_high_20 is not None
                else None
            )
            distance_to_55 = (
                abs(current_price - entry_high_55)
                if entry_high_55 is not None
                else None
            )

            distances = [d for d in (distance_to_20, distance_to_55) if d is not None]
            if not distances:
                continue

            nearest = min(distances)
            rows.append(
                WatchlistRow(
                    symbol=symbol,
                    current_price=current_price,
                    entry_high_20=entry_high_20,
                    entry_high_55=entry_high_55,
                    distance_to_20=distance_to_20,
                    distance_to_55=distance_to_55,
                    nearest_distance=nearest,
                    is_new=symbol not in previous,
                )
            )

        rows.sort(key=lambda row: (row.nearest_distance, row.symbol))
        return Watchlist(
            generated_at=generated_at or datetime.now(timezone.utc),
            rows=tuple(rows[: self.top_n]),
        )
