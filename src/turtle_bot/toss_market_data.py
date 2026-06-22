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

    def get_completed_candles(self, symbol: str) -> Sequence[Candle]:
        max_age = timedelta(seconds=self.config.candle_max_age_seconds)
        cached = self.cache.get_candles(
            symbol,
            self.config.candle_interval,
            max_age=max_age,
        )
        if cached.is_fresh:
            return cached.value

        stored = self._stored_candles(symbol, max_age=max_age)
        if stored is not None:
            return stored

        try:
            page = self.client.get_candles(
                symbol,
                interval=self.config.candle_interval,
                count=self.config.candle_count,
                adjusted=self.config.adjusted,
            )
        except TossApiError as exc:
            if exc.status == 429:
                fallback = self._stored_candles(symbol, max_age=None)
                if fallback is not None:
                    return fallback
            raise
        candles = self._completed_only(page.candles)
        captured_at = self._now()
        self.cache.set_candles(
            symbol,
            candles,
            self.config.candle_interval,
            updated_at=captured_at,
        )
        if self.store is not None:
            self.store.record_market_data_snapshot(
                "candles",
                symbol,
                {
                    "interval": self.config.candle_interval,
                    "count": len(candles),
                    "next_before": page.next_before,
                    "source": "toss",
                    "candles": tuple(_candle_payload(candle) for candle in candles),
                },
                captured_at=captured_at,
            )
        return candles

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
