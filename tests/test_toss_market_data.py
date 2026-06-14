from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping

from turtle_bot.domain import Candle
from turtle_bot.toss_client import CandlePage
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

    def record_market_data_snapshot(
        self,
        kind: str,
        symbol: str,
        payload: dict[str, Any],
        *,
        captured_at: datetime | None = None,
    ) -> None:
        self.items.append((kind, symbol, payload))


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
