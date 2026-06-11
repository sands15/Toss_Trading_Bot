from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from turtle_bot.market_cache import CacheLookup, MarketDataCache


def _fixed_now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_price_cache_marks_stale_and_decimal_and_missing() -> None:
    current = [datetime(2026, 1, 1, tzinfo=timezone.utc)]

    def now() -> datetime:
        return current[0]

    cache = MarketDataCache(now=now)
    cache.set_price("AAPL", "123.45", updated_at=current[0])
    current[0] = current[0] + timedelta(seconds=31)

    stale = cache.get_price("AAPL", max_age=timedelta(seconds=30))
    assert stale.found is True
    assert stale.stale is True
    assert stale.value == Decimal("123.45")

    latest = cache.get_price("AAPL")
    assert latest.found is True
    assert latest.stale is False

    missing = cache.get_price("MSFT")
    assert missing.found is False
    assert missing.stale is False


def test_orderbook_cached_values_use_decimal() -> None:
    cache = MarketDataCache(now=_fixed_now)
    cache.set_orderbook(
        "AAPL",
        {
            "bids": [{"price": "10.0", "size": "3"}, {"price": "9.5", "size": "5"}],
            "asks": [{"price": "10.5", "size": "4"}],
        },
    )
    lookup = cache.get_orderbook("AAPL")
    assert lookup.found is True
    assert lookup.value["bids"][0] == (Decimal("10.0"), Decimal("3"))
    assert lookup.value["asks"][0] == (Decimal("10.5"), Decimal("4"))


def test_candle_cache_keeps_decimal_fields_and_freshness() -> None:
    current = [datetime(2026, 1, 1, tzinfo=timezone.utc)]

    def now() -> datetime:
        return current[0]

    cache = MarketDataCache(now=now)
    cache.set_candles(
        "AAPL",
        [
            {
                "timestamp": "2026-01-01T00:00:00+00:00",
                "symbol": "AAPL",
                "openPrice": "1.1",
                "highPrice": "2.2",
                "lowPrice": "0.9",
                "closePrice": "2.0",
                "volume": "100",
            }
        ],
    )
    current[0] = current[0] + timedelta(seconds=2)

    lookup = cache.get_candles("AAPL", max_age=timedelta(seconds=1))
    assert lookup.found is True
    assert lookup.stale is True
    assert lookup.value[0].open == Decimal("1.1")
    assert lookup.value[0].close == Decimal("2.0")
