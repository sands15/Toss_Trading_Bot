from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Protocol, Sequence

from .domain import Candle, as_decimal


WARNING_KEYS = (
    "warning",
    "warnings",
    "investmentWarning",
    "investmentCaution",
    "tradingHalt",
    "halted",
    "suspended",
    "management",
    "delisting",
)
ETF_KEYWORDS = ("ETF", "ETN")


class UniverseMarketDataProvider(Protocol):
    def get_completed_candles(self, symbol: str) -> Sequence[Candle]:
        ...


class ReadOnlyUniverseClient(Protocol):
    def get_stocks(self, symbols: list[str] | tuple[str, ...]) -> Mapping[str, Any]:
        ...

    def get_stock_warnings(self, symbol: str) -> Mapping[str, Any]:
        ...


@dataclass(frozen=True)
class UniversePolicy:
    candidate_symbols: tuple[str, ...] = ()
    markets: tuple[str, ...] = ("KR",)
    include_etfs: bool = False
    min_price: Decimal = Decimal("1000")
    min_average_daily_value: Decimal = Decimal("100000000")
    average_daily_value_days: int = 20
    min_completed_candles: int = 56
    require_warnings_clear: bool = True


@dataclass(frozen=True)
class UniverseDecision:
    symbol: str
    included: bool
    reasons: tuple[str, ...]
    stock: Mapping[str, Any]
    warnings: Mapping[str, Any]
    completed_candles: int
    average_daily_value: Decimal | None
    last_close: Decimal | None

    def as_payload(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "included": self.included,
            "reasons": list(self.reasons),
            "completed_candles": self.completed_candles,
            "average_daily_value": (
                str(self.average_daily_value)
                if self.average_daily_value is not None
                else None
            ),
            "last_close": str(self.last_close) if self.last_close is not None else None,
        }


@dataclass(frozen=True)
class Universe:
    generated_at: datetime
    policy: UniversePolicy
    decisions: tuple[UniverseDecision, ...]

    def symbols(self) -> tuple[str, ...]:
        return tuple(decision.symbol for decision in self.decisions if decision.included)

    def as_payload(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "symbols": list(self.symbols()),
            "count": len(self.symbols()),
            "decisions": [decision.as_payload() for decision in self.decisions],
            "policy": {
                "markets": list(self.policy.markets),
                "include_etfs": self.policy.include_etfs,
                "min_price": str(self.policy.min_price),
                "min_average_daily_value": str(self.policy.min_average_daily_value),
                "average_daily_value_days": self.policy.average_daily_value_days,
                "min_completed_candles": self.policy.min_completed_candles,
                "require_warnings_clear": self.policy.require_warnings_clear,
            },
        }


class UniverseBuilder:
    """Rule-based stock universe selection. It does not use AI."""

    def __init__(
        self,
        *,
        client: ReadOnlyUniverseClient,
        market_data: UniverseMarketDataProvider,
        policy: UniversePolicy,
        now=lambda: datetime.now(timezone.utc),
    ) -> None:
        if not policy.candidate_symbols:
            raise ValueError("candidate_symbols must not be empty")
        self.client = client
        self.market_data = market_data
        self.policy = policy
        self._now = now

    def build(self) -> Universe:
        stocks = normalize_stock_payload(
            self.client.get_stocks(self.policy.candidate_symbols)
        )
        decisions = []
        for symbol in self.policy.candidate_symbols:
            stock = stocks.get(symbol, {})
            warnings = self.client.get_stock_warnings(symbol)
            try:
                candles = tuple(self.market_data.get_completed_candles(symbol))
            except Exception as exc:
                decisions.append(
                    UniverseDecision(
                        symbol=symbol,
                        included=False,
                        reasons=(f"candles_unavailable:{exc}",),
                        stock=stock,
                        warnings=warnings,
                        completed_candles=0,
                        average_daily_value=None,
                        last_close=None,
                    )
                )
                continue
            decisions.append(self._decision(symbol, stock, warnings, candles))
        return Universe(
            generated_at=self._now(),
            policy=self.policy,
            decisions=tuple(decisions),
        )

    def _decision(
        self,
        symbol: str,
        stock: Mapping[str, Any],
        warnings: Mapping[str, Any],
        candles: Sequence[Candle],
    ) -> UniverseDecision:
        reasons: list[str] = []
        if not stock:
            reasons.append("stock_metadata_missing")

        market = _optional_upper(_first(stock, "market", "marketCode", "exchange"))
        if market is not None and market not in {item.upper() for item in self.policy.markets}:
            reasons.append(f"market_excluded:{market}")

        if not self.policy.include_etfs and _is_etf(stock):
            reasons.append("instrument_excluded:etf")

        warning_reasons = warning_blockers(warnings)
        if self.policy.require_warnings_clear and warning_reasons:
            reasons.extend(warning_reasons)

        completed_candles = len(candles)
        if completed_candles < self.policy.min_completed_candles:
            reasons.append(
                f"insufficient_candles:{completed_candles}<"
                f"{self.policy.min_completed_candles}"
            )

        last_close = candles[-1].close if candles else None
        if last_close is None:
            reasons.append("last_close_missing")
        elif last_close < self.policy.min_price:
            reasons.append(f"price_below_min:{last_close}<{self.policy.min_price}")

        average_daily_value = average_traded_value(
            candles,
            days=self.policy.average_daily_value_days,
        )
        if average_daily_value is None:
            reasons.append("average_daily_value_missing")
        elif average_daily_value < self.policy.min_average_daily_value:
            reasons.append(
                "average_daily_value_below_min:"
                f"{average_daily_value}<{self.policy.min_average_daily_value}"
            )

        return UniverseDecision(
            symbol=symbol,
            included=not reasons,
            reasons=tuple(reasons or ("included",)),
            stock=stock,
            warnings=warnings,
            completed_candles=completed_candles,
            average_daily_value=average_daily_value,
            last_close=last_close,
        )


def normalize_stock_payload(payload: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw_items = payload.get("stocks", payload.get("items", payload.get("data", ())))
    if isinstance(raw_items, Mapping):
        items = raw_items.values()
    elif isinstance(raw_items, list):
        items = raw_items
    else:
        items = ()
    result: dict[str, Mapping[str, Any]] = {}
    for item in items:
        if not isinstance(item, Mapping):
            continue
        symbol = str(item.get("symbol", "")).strip()
        if symbol:
            result[symbol] = dict(item)
    return result


def warning_blockers(payload: Mapping[str, Any]) -> tuple[str, ...]:
    blockers: list[str] = []
    for item in _warning_items(payload):
        for key in WARNING_KEYS:
            if _truthy_warning(item.get(key)):
                blockers.append(f"warning:{key}")
    return tuple(sorted(set(blockers)))


def average_traded_value(
    candles: Sequence[Candle],
    *,
    days: int,
) -> Decimal | None:
    if days <= 0 or not candles:
        return None
    recent = tuple(candles[-days:])
    if not recent:
        return None
    return sum(candle.close * candle.volume for candle in recent) / Decimal(len(recent))


def _warning_items(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    for key in ("warnings", "items", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return tuple(item for item in value if isinstance(item, Mapping))
        if isinstance(value, Mapping):
            return (value,)
    return (payload,)


def _truthy_warning(value: Any) -> bool:
    if value in (None, False, "", 0, "0", "N", "n", "false", "False"):
        return False
    return True


def _first(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _optional_upper(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value).upper()


def _is_etf(stock: Mapping[str, Any]) -> bool:
    stock_type = str(_first(stock, "type", "stockType", "instrumentType") or "").upper()
    name = str(_first(stock, "name", "stockName", "displayName") or "").upper()
    return any(keyword in stock_type or keyword in name for keyword in ETF_KEYWORDS)

