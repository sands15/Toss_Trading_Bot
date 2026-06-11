from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .domain import Candle, as_decimal


def _to_decimal(value: Any) -> Decimal:
    return as_decimal(value)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class CacheEntry:
    value: Any
    updated_at: datetime


@dataclass(frozen=True)
class CacheLookup:
    value: Any
    found: bool
    stale: bool
    updated_at: datetime | None = None

    @property
    def is_fresh(self) -> bool:
        return self.found and not self.stale


class MarketDataCache:
    """Keep in-memory snapshots of market data with optional freshness checks."""

    def __init__(self, *, now=_now_utc):
        self._now = now
        self._prices: dict[str, CacheEntry] = {}
        self._orderbooks: dict[str, CacheEntry] = {}
        self._candles: dict[tuple[str, str], CacheEntry] = {}

    def _to_decimal_pair(self, price: Any, qty: Any) -> tuple[Decimal, Decimal]:
        return _to_decimal(price), _to_decimal(qty)

    def _normalize_orderbook(
        self,
        orderbook: Mapping[str, Any] | Sequence[tuple[Any, Any]] | Sequence[Mapping[str, Any]],
    ) -> tuple[tuple[tuple[Decimal, Decimal], ...], tuple[tuple[Decimal, Decimal], ...]]:
        if isinstance(orderbook, Mapping):
            raw_bids = orderbook.get("bids", ())
            raw_asks = orderbook.get("asks", ())
        else:
            raw_bids = orderbook
            raw_asks = ()

        def _row(row: Any) -> tuple[Decimal, Decimal]:
            if isinstance(row, Mapping):
                def _pick(*keys: str) -> Any:
                    for key in keys:
                        if key in row:
                            return row[key]
                    return None

                price = _pick("price", "px", "0")
                size = _pick("size", "qty", "quantity", "1")
                return self._to_decimal_pair(price, size)

            if isinstance(row, (tuple, list)) and len(row) >= 2:
                return self._to_decimal_pair(row[0], row[1])

            raise TypeError(f"Unsupported orderbook row: {row!r}")

        bids = tuple(_row(row) for row in raw_bids)
        asks = tuple(_row(row) for row in raw_asks)
        return bids, asks

    def _normalize_candles(
        self,
        candles: Sequence[Mapping[str, Any]] | Sequence[Candle] | Sequence[Any],
    ) -> tuple[Candle, ...]:
        normalized: list[Candle] = []
        for candle in candles:
            if isinstance(candle, Candle):
                normalized.append(candle)
            elif isinstance(candle, Mapping):
                normalized.append(Candle.from_api(candle))
            else:
                raise TypeError(f"Unsupported candle type: {type(candle)!r}")
        return tuple(normalized)

    def set_price(self, symbol: str, value: Any, *, updated_at: datetime | None = None) -> None:
        self._prices[symbol] = CacheEntry(_to_decimal(value), updated_at or self._now())

    def get_price(self, symbol: str, *, max_age: timedelta | None = None) -> CacheLookup:
        entry = self._prices.get(symbol)
        if entry is None:
            return CacheLookup(value=None, found=False, stale=False)
        stale = self._is_stale(entry.updated_at, max_age=max_age)
        return CacheLookup(
            value=entry.value,
            found=True,
            stale=stale,
            updated_at=entry.updated_at,
        )

    def set_orderbook(
        self,
        symbol: str,
        orderbook: Mapping[str, Any] | Sequence[tuple[Any, Any]] | Sequence[Mapping[str, Any]],
        *,
        updated_at: datetime | None = None,
    ) -> None:
        bids, asks = self._normalize_orderbook(orderbook)
        self._orderbooks[symbol] = CacheEntry({"bids": bids, "asks": asks}, updated_at or self._now())

    def get_orderbook(
        self,
        symbol: str,
        *,
        max_age: timedelta | None = None,
    ) -> CacheLookup:
        entry = self._orderbooks.get(symbol)
        if entry is None:
            return CacheLookup(value=None, found=False, stale=False)
        stale = self._is_stale(entry.updated_at, max_age=max_age)
        return CacheLookup(
            value=entry.value,
            found=True,
            stale=stale,
            updated_at=entry.updated_at,
        )

    def set_candles(
        self,
        symbol: str,
        candles: Sequence[Mapping[str, Any]] | Sequence[Candle] | Sequence[Any],
        interval: str = "1d",
        *,
        updated_at: datetime | None = None,
    ) -> None:
        self._candles[(symbol, interval)] = CacheEntry(
            self._normalize_candles(candles),
            updated_at or self._now(),
        )

    def get_candles(
        self,
        symbol: str,
        interval: str = "1d",
        *,
        max_age: timedelta | None = None,
    ) -> CacheLookup:
        entry = self._candles.get((symbol, interval))
        if entry is None:
            return CacheLookup(value=None, found=False, stale=False)
        stale = self._is_stale(entry.updated_at, max_age=max_age)
        return CacheLookup(
            value=entry.value,
            found=True,
            stale=stale,
            updated_at=entry.updated_at,
        )

    def _is_stale(self, updated_at: datetime, max_age: timedelta | None) -> bool:
        if max_age is None:
            return False
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        return (self._now() - updated_at) > max_age
