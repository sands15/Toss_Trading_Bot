from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo

from .domain import Candle, as_decimal
from .market_cache import MarketDataCache
from .toss_client import CandlePage, TossApiError


PRICE_KEYS = (
    "lastPrice",
    "price",
    "currentPrice",
    "tradePrice",
    "closePrice",
)
MIN_CANDLE_COUNT = 1
MAX_CANDLE_COUNT = 200


class ReadOnlyMarketDataClient(Protocol):
    def get_candles(
        self,
        symbol: str,
        *,
        interval: str = "1d",
        count: int = 100,
        before: str | datetime | None = None,
        adjusted: bool = True,
    ) -> CandlePage:
        ...

    def get_prices(self, symbols: list[str] | tuple[str, ...]) -> Any:
        ...


class MarketDataSnapshotStore(Protocol):
    def record_market_data_snapshot(
        self,
        kind: str,
        symbol: str,
        payload: dict[str, Any],
        *,
        captured_at: datetime | None = None,
    ) -> None:
        ...

    def latest_candles_snapshot(
        self,
        symbol: str,
        *,
        interval: str = "1d",
    ) -> tuple[tuple[Candle, ...], datetime | None] | None:
        ...


@dataclass(frozen=True)
class TossMarketDataConfig:
    candle_interval: str = "1d"
    candle_count: int = 100
    adjusted: bool = True
    exclude_current_session: bool = True
    local_timezone: str = "Asia/Seoul"
    price_max_age_seconds: int = 15
    candle_max_age_seconds: int = 43200


class TossReadOnlyMarketDataProvider:
    """Paper-runtime market data provider backed by read-only Toss endpoints."""

    def __init__(
        self,
        *,
        client: ReadOnlyMarketDataClient,
        config: TossMarketDataConfig | None = None,
        cache: MarketDataCache | None = None,
        store: MarketDataSnapshotStore | None = None,
        now=lambda: datetime.now(timezone.utc),
    ) -> None:
        self.client = client
        self.config = config or TossMarketDataConfig()
        self.cache = cache or MarketDataCache(now=now)
        self.store = store
        self._now = now
        self._exhausted_candle_pages: set[tuple[str, str]] = set()

    def get_completed_candles(self, symbol: str) -> Sequence[Candle]:
        target_count = _target_candle_count(self.config.candle_count)
        cache_key = (symbol, self.config.candle_interval)
        max_age = timedelta(seconds=self.config.candle_max_age_seconds)
        cached = self.cache.get_candles(
            symbol,
            self.config.candle_interval,
            max_age=max_age,
        )
        cached_candles = tuple(cached.value or ()) if cached.found else ()
        if (
            cached.is_fresh
            and (len(cached_candles) >= target_count or cache_key in self._exhausted_candle_pages)
        ):
            return _latest_candles(cached_candles, target_count)

        stored = self._stored_candles(symbol, max_age=max_age)
        if stored is not None and len(stored) >= target_count:
            return _latest_candles(stored, target_count)
        if stored is not None and self._stored_candle_pages_exhausted(symbol):
            self._exhausted_candle_pages.add(cache_key)
            return _latest_candles(stored, target_count)
        seed = _merge_candles(cached_candles, stored or ())

        try:
            candles, next_before = self._fetch_completed_candle_pages(
                symbol,
                seed=seed,
                target_count=target_count,
            )
        except TossApiError as exc:
            if exc.status == 429:
                fallback = self._stored_candles(symbol, max_age=None)
                if fallback is not None:
                    return _latest_candles(fallback, target_count)
            raise
        captured_at = self._now()
        self.cache.set_candles(
            symbol,
            candles,
            self.config.candle_interval,
            updated_at=captured_at,
        )
        if next_before:
            self._exhausted_candle_pages.discard(cache_key)
        else:
            self._exhausted_candle_pages.add(cache_key)
        if self.store is not None:
            self.store.record_market_data_snapshot(
                "candles",
                symbol,
                {
                    "interval": self.config.candle_interval,
                    "count": len(candles),
                    "next_before": next_before,
                    "source": "toss",
                    "candles": tuple(_candle_payload(candle) for candle in candles),
                },
                captured_at=captured_at,
            )
        return candles

    def _fetch_completed_candle_pages(
        self,
        symbol: str,
        *,
        seed: Sequence[Candle],
        target_count: int,
    ) -> tuple[tuple[Candle, ...], str | None]:
        candles = _latest_candles(seed, target_count)
        before: str | datetime | None = None
        seen_cursors: set[str] = set()
        next_before: str | None = None

        for _ in range(10):
            if len(candles) >= target_count:
                break
            page = self.client.get_candles(
                symbol,
                interval=self.config.candle_interval,
                count=MAX_CANDLE_COUNT,
                before=before,
                adjusted=self.config.adjusted,
            )
            next_before = page.next_before
            completed = self._completed_only(page.candles)
            candles = _latest_candles(
                _merge_candles(candles, completed),
                target_count,
            )
            if not next_before or next_before in seen_cursors:
                break
            seen_cursors.add(next_before)
            before = next_before

        return candles, next_before

    def _stored_candle_pages_exhausted(self, symbol: str) -> bool:
        if self.store is None or not hasattr(self.store, "latest_market_data_snapshot"):
            return False
        payload = self.store.latest_market_data_snapshot("candles", symbol)
        if not isinstance(payload, Mapping):
            return False
        if str(payload.get("interval") or "1d") != self.config.candle_interval:
            return False
        return not payload.get("next_before")

    def _stored_candles(
        self,
        symbol: str,
        *,
        max_age: timedelta | None,
    ) -> tuple[Candle, ...] | None:
        if self.store is None or not hasattr(self.store, "latest_candles_snapshot"):
            return None
        snapshot = self.store.latest_candles_snapshot(
            symbol,
            interval=self.config.candle_interval,
        )
        if snapshot is None:
            return None
        candles, captured_at = snapshot
        if captured_at is None:
            return None
        if captured_at.tzinfo is None:
            captured_at = captured_at.replace(tzinfo=timezone.utc)
        if max_age is not None and self._now() - captured_at > max_age:
            return None
        self.cache.set_candles(
            symbol,
            candles,
            self.config.candle_interval,
            updated_at=captured_at,
        )
        return candles

    def get_current_price(self, symbol: str) -> Decimal:
        max_age = timedelta(seconds=self.config.price_max_age_seconds)
        cached = self.cache.get_price(symbol, max_age=max_age)
        if cached.is_fresh:
            return cached.value

        payload = self.client.get_prices((symbol,))
        price = extract_price(payload, symbol)
        captured_at = self._now()
        self.cache.set_price(symbol, price, updated_at=captured_at)
        if self.store is not None:
            self.store.record_market_data_snapshot(
                "price",
                symbol,
                {"price": str(price), "source": "toss"},
                captured_at=captured_at,
            )
        return price

    def _completed_only(self, candles: Sequence[Candle]) -> tuple[Candle, ...]:
        ordered = tuple(sorted(candles, key=lambda candle: candle.timestamp))
        if not self.config.exclude_current_session:
            return ordered

        local_tz = ZoneInfo(self.config.local_timezone)
        today = self._now().astimezone(local_tz).date()
        return tuple(
            candle
            for candle in ordered
            if candle.timestamp.astimezone(local_tz).date() < today
        )


def extract_price(payload: Any, symbol: str) -> Decimal:
    for candidate in _price_candidates(payload, symbol):
        if isinstance(candidate, Mapping):
            price = _price_from_mapping(candidate)
            if price is not None:
                return price
        elif candidate is not None:
            return as_decimal(candidate)
    raise ValueError(f"price not found for {symbol}")


def _toss_candle_count(count: int) -> int:
    return min(max(int(count), MIN_CANDLE_COUNT), MAX_CANDLE_COUNT)


def _target_candle_count(count: int) -> int:
    return max(int(count), MIN_CANDLE_COUNT)


def _merge_candles(*groups: Sequence[Candle]) -> tuple[Candle, ...]:
    merged: dict[tuple[str, datetime], Candle] = {}
    for group in groups:
        for candle in group:
            merged[(candle.symbol, candle.timestamp)] = candle
    return tuple(sorted(merged.values(), key=lambda candle: candle.timestamp))


def _latest_candles(candles: Sequence[Candle], target_count: int) -> tuple[Candle, ...]:
    ordered = _merge_candles(candles)
    if len(ordered) <= target_count:
        return ordered
    return ordered[-target_count:]


def _price_candidates(payload: Any, symbol: str) -> tuple[Any, ...]:
    candidates: list[Any] = []
    if isinstance(payload, list):
        candidates.extend(
            item
            for item in payload
            if isinstance(item, Mapping)
            and str(item.get("symbol", "")).strip() == symbol
        )
        return tuple(candidates)
    if not isinstance(payload, Mapping):
        return (payload,)
    if symbol in payload:
        candidates.append(payload[symbol])
    for key in ("prices", "items", "data"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            if symbol in value:
                candidates.append(value[symbol])
            candidates.append(value)
        elif isinstance(value, list):
            candidates.extend(
                item
                for item in value
                if isinstance(item, Mapping)
                and str(item.get("symbol", "")).strip() == symbol
            )
    candidates.append(payload)
    return tuple(candidates)


def _price_from_mapping(payload: Mapping[str, Any]) -> Decimal | None:
    for key in PRICE_KEYS:
        value = payload.get(key)
        if value is not None:
            return as_decimal(value)
    return None


def _candle_payload(candle: Candle) -> dict[str, Any]:
    return {
        "timestamp": candle.timestamp.isoformat(),
        "symbol": candle.symbol,
        "openPrice": str(candle.open),
        "highPrice": str(candle.high),
        "lowPrice": str(candle.low),
        "closePrice": str(candle.close),
        "volume": str(candle.volume),
        "currency": candle.currency,
        "adjusted": candle.adjusted,
        "source": candle.source,
    }
