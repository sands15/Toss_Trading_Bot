from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping

from turtle_bot.domain import Candle
from turtle_bot.toss_client import CandlePage, TossApiError
from turtle_bot.toss_market_data import (
    TossMarketDataConfig,
    TossReadOnlyMarketDataProvider,
    extract_price,
)


def _c(day: int, symbol: str = "TEST") -> Candle:
    return Candle(
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=day),
        symbol=symbol,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("1000"),
    )


@dataclass
class FakeReadOnlyMarketClient:
    candles_payload: tuple[Candle, ...]
    prices_payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        self.candle_calls = 0
        self.price_calls = 0
        self.last_candle_count: int | None = None
        self.candle_requests: list[dict[str, Any]] = []

    def get_candles(
        self,
        symbol: str,
        *,
        interval: str = "1d",
        count: int = 100,
        before: str | datetime | None = None,
        adjusted: bool = True,
    ) -> CandlePage:
        self.candle_calls += 1
        self.last_candle_count = count
        self.candle_requests.append({"count": count, "before": before})
        return CandlePage(
            candles=self.candles_payload,
            next_before=None,
            raw={"symbol": symbol, "interval": interval, "count": count},
        )

    def get_prices(self, symbols: list[str] | tuple[str, ...]) -> Mapping[str, Any]:
        self.price_calls += 1
        return self.prices_payload


class SnapshotStore:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, dict[str, Any]]] = []
        self.latest: tuple[tuple[Candle, ...], datetime | None] | None = None
        self.latest_payload: dict[str, Any] | None = None

    def record_market_data_snapshot(
        self,
        kind: str,
        symbol: str,
        payload: dict[str, Any],
        *,
        captured_at: datetime | None = None,
    ) -> None:
        self.items.append((kind, symbol, payload))

    def latest_candles_snapshot(
        self,
        symbol: str,
        *,
        interval: str = "1d",
    ) -> tuple[tuple[Candle, ...], datetime | None] | None:
        return self.latest

    def latest_market_data_snapshot(self, kind: str, symbol: str) -> dict[str, Any] | None:
        if self.latest_payload is not None:
            return self.latest_payload
        for item_kind, item_symbol, payload in reversed(self.items):
            if item_kind == kind and item_symbol == symbol:
                return payload
        return None


class RateLimitedClient(FakeReadOnlyMarketClient):
    def get_candles(
        self,
        symbol: str,
        *,
        interval: str = "1d",
        count: int = 100,
        before: str | datetime | None = None,
        adjusted: bool = True,
    ) -> CandlePage:
        self.candle_calls += 1
        raise TossApiError(429, code="rate-limit-paused", message="paused")


class PagedReadOnlyMarketClient(FakeReadOnlyMarketClient):
    def __init__(
        self,
        pages: tuple[tuple[tuple[Candle, ...], str | None], ...],
        *,
        prices_payload: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(candles_payload=(), prices_payload=prices_payload or {})
        self.pages = pages

    def get_candles(
        self,
        symbol: str,
        *,
        interval: str = "1d",
        count: int = 100,
        before: str | datetime | None = None,
        adjusted: bool = True,
    ) -> CandlePage:
        self.candle_calls += 1
        self.last_candle_count = count
        self.candle_requests.append({"count": count, "before": before})
        index = min(self.candle_calls - 1, len(self.pages) - 1)
        candles, next_before = self.pages[index]
        return CandlePage(
            candles=candles,
            next_before=next_before,
            raw={"symbol": symbol, "interval": interval, "count": count},
        )


def test_extract_price_accepts_common_toss_payload_shapes() -> None:
    assert extract_price({"prices": [{"symbol": "AAA", "lastPrice": "10.5"}]}, "AAA") == Decimal("10.5")
    assert extract_price({"AAA": {"price": "11"}}, "AAA") == Decimal("11")
    assert extract_price({"items": [{"symbol": "AAA", "closePrice": "12"}]}, "AAA") == Decimal("12")
    assert extract_price([{"symbol": "AAA", "lastPrice": "13"}], "AAA") == Decimal("13")


def test_toss_market_data_provider_caches_and_records_snapshots() -> None:
    now = datetime(2026, 1, 25, tzinfo=timezone.utc)
    store = SnapshotStore()
    client = FakeReadOnlyMarketClient(
        candles_payload=tuple(_c(day) for day in range(5)),
        prices_payload={"prices": [{"symbol": "TEST", "lastPrice": "105"}]},
    )
    provider = TossReadOnlyMarketDataProvider(
        client=client,
        config=TossMarketDataConfig(candle_count=5),
        store=store,
        now=lambda: now,
    )

    assert len(provider.get_completed_candles("TEST")) == 5
    assert provider.get_current_price("TEST") == Decimal("105")
    assert len(provider.get_completed_candles("TEST")) == 5
    assert provider.get_current_price("TEST") == Decimal("105")

    assert client.candle_calls == 1
    assert client.price_calls == 1
    assert [item[0] for item in store.items] == ["candles", "price"]
    assert "candles" in store.items[0][2]


def test_toss_market_data_provider_uses_persistent_candle_cache() -> None:
    now = datetime(2026, 1, 25, tzinfo=timezone.utc)
    store = SnapshotStore()
    store.latest = (tuple(_c(day) for day in range(3)), now - timedelta(minutes=1))
    client = FakeReadOnlyMarketClient(
        candles_payload=tuple(_c(day) for day in range(5)),
        prices_payload={},
    )
    provider = TossReadOnlyMarketDataProvider(
        client=client,
        config=TossMarketDataConfig(candle_count=3),
        store=store,
        now=lambda: now,
    )

    candles = provider.get_completed_candles("TEST")

    assert candles == tuple(_c(day) for day in range(3))
    assert client.candle_calls == 0


def test_toss_market_data_provider_clamps_candle_count_to_toss_limit() -> None:
    now = datetime(2026, 1, 25, tzinfo=timezone.utc)
    client = FakeReadOnlyMarketClient(
        candles_payload=tuple(_c(day) for day in range(5)),
        prices_payload={},
    )
    provider = TossReadOnlyMarketDataProvider(
        client=client,
        config=TossMarketDataConfig(candle_count=320),
        now=lambda: now,
    )

    provider.get_completed_candles("TEST")

    assert client.last_candle_count == 200


def test_toss_market_data_provider_paginates_to_reach_completed_target() -> None:
    now = datetime(2026, 10, 28, tzinfo=timezone.utc)
    current_session = Candle(
        timestamp=now,
        symbol="TEST",
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("90"),
        close=Decimal("105"),
        volume=Decimal("1000"),
    )
    client = PagedReadOnlyMarketClient(
        pages=(
            (tuple(_c(day) for day in range(199)) + (current_session,), "cursor-1"),
            (tuple(_c(day) for day in range(120, 250)), None),
        ),
    )
    provider = TossReadOnlyMarketDataProvider(
        client=client,
        config=TossMarketDataConfig(candle_count=250),
        now=lambda: now,
    )

    candles = provider.get_completed_candles("TEST")

    assert len(candles) == 250
    assert candles[-1] == _c(249)
    assert client.candle_calls == 2
    assert client.candle_requests == [
        {"count": 200, "before": None},
        {"count": 200, "before": "cursor-1"},
    ]


def test_toss_market_data_provider_merges_insufficient_fresh_snapshot_with_pages() -> None:
    now = datetime(2027, 1, 1, tzinfo=timezone.utc)
    store = SnapshotStore()
    store.latest = (tuple(_c(day) for day in range(150)), now - timedelta(minutes=1))
    store.latest_payload = {
        "interval": "1d",
        "next_before": "cursor-stored",
        "candles": [],
    }
    client = PagedReadOnlyMarketClient(
        pages=((tuple(_c(day) for day in range(150, 350)), None),),
    )
    provider = TossReadOnlyMarketDataProvider(
        client=client,
        config=TossMarketDataConfig(candle_count=250),
        store=store,
        now=lambda: now,
    )

    candles = provider.get_completed_candles("TEST")

    assert len(candles) == 250
    assert candles[0] == _c(100)
    assert candles[-1] == _c(349)
    assert client.candle_calls == 1


def test_toss_market_data_provider_falls_back_to_stale_cache_on_rate_limit() -> None:
    now = datetime(2026, 1, 25, tzinfo=timezone.utc)
    store = SnapshotStore()
    store.latest = (tuple(_c(day) for day in range(3)), now - timedelta(days=3))
    client = RateLimitedClient(
        candles_payload=(),
        prices_payload={},
    )
    provider = TossReadOnlyMarketDataProvider(
        client=client,
        config=TossMarketDataConfig(candle_count=5, candle_max_age_seconds=1),
        store=store,
        now=lambda: now,
    )

    candles = provider.get_completed_candles("TEST")

    assert candles == tuple(_c(day) for day in range(3))
    assert client.candle_calls == 1


def test_toss_market_data_provider_excludes_current_session_candle() -> None:
    now = datetime(2026, 1, 10, 3, tzinfo=timezone.utc)
    current_session = Candle(
        timestamp=datetime(2026, 1, 10, 0, tzinfo=timezone.utc),
        symbol="TEST",
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("90"),
        close=Decimal("105"),
        volume=Decimal("1000"),
    )
    client = FakeReadOnlyMarketClient(
        candles_payload=(_c(0), current_session),
        prices_payload={"TEST": {"lastPrice": "105"}},
    )
    provider = TossReadOnlyMarketDataProvider(
        client=client,
        config=TossMarketDataConfig(candle_count=2),
        now=lambda: now,
    )

    candles = provider.get_completed_candles("TEST")

    assert candles == (_c(0),)
