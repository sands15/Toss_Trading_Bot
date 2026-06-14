from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

from turtle_bot.domain import Candle
from turtle_bot.universe import (
    UniverseBuilder,
    UniversePolicy,
    average_traded_value,
    normalize_stock_payload,
    warning_blockers,
)


def _candles(symbol: str, *, days: int = 60, close: str = "50000", volume: str = "10000") -> tuple[Candle, ...]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return tuple(
        Candle(
            timestamp=start + timedelta(days=idx),
            symbol=symbol,
            open=Decimal(close),
            high=Decimal(close) + Decimal("1"),
            low=Decimal(close) - Decimal("1"),
            close=Decimal(close),
            volume=Decimal(volume),
        )
        for idx in range(days)
    )


@dataclass
class FakeUniverseClient:
    stocks: Mapping[str, Any]
    warnings: Mapping[str, Mapping[str, Any]]

    def get_stocks(self, symbols: list[str] | tuple[str, ...]) -> Mapping[str, Any]:
        return {"stocks": [self.stocks[symbol] for symbol in symbols if symbol in self.stocks]}

    def get_stock_warnings(self, symbol: str) -> Mapping[str, Any]:
        return self.warnings.get(symbol, {"warnings": []})


@dataclass
class FakeMarketData:
    candles: Mapping[str, Sequence[Candle]]

    def get_completed_candles(self, symbol: str) -> Sequence[Candle]:
        return self.candles[symbol]


def test_normalize_stock_payload_accepts_common_shapes() -> None:
    payload = {"stocks": [{"symbol": "AAA"}, {"symbol": "BBB"}]}

    assert sorted(normalize_stock_payload(payload)) == ["AAA", "BBB"]


def test_normalize_stock_payload_accepts_official_result_array() -> None:
    payload = [{"symbol": "AAA", "market": "KR"}]

    assert normalize_stock_payload(payload)["AAA"]["market"] == "KR"


def test_warning_blockers_detects_truthy_warning_fields() -> None:
    blockers = warning_blockers({"warnings": [{"investmentWarning": True, "halted": "N"}]})

    assert blockers == ("warning:investmentWarning",)


def test_warning_blockers_accepts_official_result_array() -> None:
    assert warning_blockers([{"warningType": "INVESTMENT_WARNING"}]) == ()


def test_average_traded_value_uses_recent_completed_candles() -> None:
    assert average_traded_value(_candles("AAA", days=2, close="10", volume="3"), days=2) == Decimal("30")


def test_universe_builder_includes_only_rule_eligible_symbols() -> None:
    client = FakeUniverseClient(
        stocks={
            "AAA": {"symbol": "AAA", "market": "KR", "name": "Alpha"},
            "ETF1": {"symbol": "ETF1", "market": "KR", "name": "Index ETF"},
            "LOW": {"symbol": "LOW", "market": "KR", "name": "Low Price"},
            "WARN": {"symbol": "WARN", "market": "KR", "name": "Warning"},
            "SHORT": {"symbol": "SHORT", "market": "KR", "name": "Short History"},
        },
        warnings={"WARN": {"warnings": [{"investmentWarning": True}]}},
    )
    market_data = FakeMarketData(
        candles={
            "AAA": _candles("AAA"),
            "ETF1": _candles("ETF1"),
            "LOW": _candles("LOW", close="500"),
            "WARN": _candles("WARN"),
            "SHORT": _candles("SHORT", days=10),
        }
    )
    universe = UniverseBuilder(
        client=client,
        market_data=market_data,
        policy=UniversePolicy(
            candidate_symbols=("AAA", "ETF1", "LOW", "WARN", "SHORT"),
            min_price=Decimal("1000"),
            min_average_daily_value=Decimal("100000000"),
            min_completed_candles=56,
        ),
        now=lambda: datetime(2026, 2, 1, tzinfo=timezone.utc),
    ).build()

    by_symbol = {decision.symbol: decision for decision in universe.decisions}
    assert universe.symbols() == ("AAA",)
    assert by_symbol["AAA"].reasons == ("included",)
    assert "instrument_excluded:etf" in by_symbol["ETF1"].reasons
    assert any(reason.startswith("price_below_min") for reason in by_symbol["LOW"].reasons)
    assert "warning:investmentWarning" in by_symbol["WARN"].reasons
    assert any(reason.startswith("insufficient_candles") for reason in by_symbol["SHORT"].reasons)
    assert universe.as_payload()["count"] == 1
