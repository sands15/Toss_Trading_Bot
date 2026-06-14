from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from .backtest import (
    BacktestConfig,
    BacktestResult,
    backtest_result_to_dict,
    load_candles_csv,
)
from .domain import Candle, PositionDirection
from .indicators import donchian_channel
from .pit_universe import PitUniverse
from .universe import average_traded_value


@dataclass(frozen=True)
class ScanBacktestConfig:
    scan_top_n: int = 20
    accept_top_n: int = 5
    accept_hold_days: int = 1
    min_price: Decimal = Decimal("1000")
    min_average_daily_value: Decimal = Decimal("100000000")
    average_daily_value_days: int = 20
    min_completed_candles: int = 56
    max_breakout_distance_pct: Decimal | None = None
    pit_universe: PitUniverse | None = None
    scan_directions: tuple[PositionDirection, ...] = (PositionDirection.LONG,)


@dataclass(frozen=True)
class ScanCandidate:
    symbol: str
    direction: PositionDirection
    current_price: Decimal
    entry_high_20: Decimal | None
    entry_high_55: Decimal | None
    entry_low_20: Decimal | None
    entry_low_55: Decimal | None
    distance_pct: Decimal
    average_daily_value: Decimal

    def as_payload(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction.value,
            "current_price": str(self.current_price),
            "entry_high_20": (
                str(self.entry_high_20) if self.entry_high_20 is not None else None
            ),
            "entry_high_55": (
                str(self.entry_high_55) if self.entry_high_55 is not None else None
            ),
            "entry_low_20": (
                str(self.entry_low_20) if self.entry_low_20 is not None else None
            ),
            "entry_low_55": (
                str(self.entry_low_55) if self.entry_low_55 is not None else None
            ),
            "distance_pct": str(self.distance_pct),
            "average_daily_value": str(self.average_daily_value),
        }


@dataclass(frozen=True)
class ScanDecision:
    timestamp: datetime
    recommended: tuple[ScanCandidate, ...]
    accepted_symbols: tuple[str, ...]
    accepted_entries: tuple[tuple[str, PositionDirection], ...] = ()

    def as_payload(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "recommended": [candidate.as_payload() for candidate in self.recommended],
            "accepted_symbols": list(self.accepted_symbols),
            "accepted_entries": [
                {"symbol": symbol, "direction": direction.value}
                for symbol, direction in self.accepted_entries
            ],
        }


@dataclass(frozen=True)
class ScanBacktestResult:
    backtest: BacktestResult
    decisions: tuple[ScanDecision, ...]
    config: ScanBacktestConfig
    symbols: tuple[str, ...]

    def as_payload(self) -> dict[str, Any]:
        accepted_days = sum(1 for decision in self.decisions if decision.accepted_symbols)
        return {
            **backtest_result_to_dict(self.backtest),
            "scan": {
                "symbols": list(self.symbols),
                "symbol_count": len(self.symbols),
                "decision_days": len(self.decisions),
                "accepted_days": accepted_days,
                "config": {
                    "scan_top_n": self.config.scan_top_n,
                    "accept_top_n": self.config.accept_top_n,
                    "accept_hold_days": self.config.accept_hold_days,
                    "min_price": str(self.config.min_price),
                    "min_average_daily_value": str(
                        self.config.min_average_daily_value
                    ),
                    "average_daily_value_days": self.config.average_daily_value_days,
                    "min_completed_candles": self.config.min_completed_candles,
                    "max_breakout_distance_pct": (
                        str(self.config.max_breakout_distance_pct)
                        if self.config.max_breakout_distance_pct is not None
                        else None
                    ),
                    "pit_universe_enabled": self.config.pit_universe is not None,
                    "scan_directions": [
                        direction.value for direction in self.config.scan_directions
                    ],
                },
                "decisions": [decision.as_payload() for decision in self.decisions],
            },
        }


def load_scan_backtest_candles(data_dir: str | Path) -> tuple[Candle, ...]:
    candles: list[Candle] = []
    for path in sorted(Path(data_dir).glob("*.csv")):
        candles.extend(load_candles_csv(path))
    return tuple(sorted(candles, key=lambda candle: (candle.timestamp, candle.symbol)))


def run_scan_backtest(
    candles: Sequence[Candle],
    *,
    engine,
    config: ScanBacktestConfig | None = None,
) -> ScanBacktestResult:
    scan_config = config or ScanBacktestConfig()
    ordered = tuple(sorted(candles, key=lambda candle: (candle.timestamp, candle.symbol)))
    histories: dict[str, list[Candle]] = {}
    accepted_by_timestamp: dict[datetime, set[tuple[str, PositionDirection]]] = {}
    active_acceptances: dict[tuple[str, PositionDirection], int] = {}
    decisions: list[ScanDecision] = []

    current_timestamp: datetime | None = None
    current_batch: list[Candle] = []
    for candle in ordered:
        if current_timestamp is None:
            current_timestamp = candle.timestamp
        if candle.timestamp != current_timestamp:
            _scan_one_day(
                current_timestamp,
                current_batch,
                histories,
                accepted_by_timestamp,
                active_acceptances,
                decisions,
                scan_config,
            )
            current_timestamp = candle.timestamp
            current_batch = []
        current_batch.append(candle)
    if current_timestamp is not None:
        _scan_one_day(
            current_timestamp,
            current_batch,
            histories,
            accepted_by_timestamp,
            active_acceptances,
            decisions,
            scan_config,
        )

    backtest = engine.run_portfolio(
        ordered,
        entry_filter=lambda timestamp, symbol: symbol
        in {item[0] for item in accepted_by_timestamp.get(timestamp, set())}
        and _pit_allows(scan_config.pit_universe, timestamp, symbol),
        entry_direction_filter=lambda timestamp, symbol, direction: (
            symbol,
            direction,
        )
        in accepted_by_timestamp.get(timestamp, set()),
    )
    symbols = tuple(sorted({candle.symbol for candle in ordered}))
    return ScanBacktestResult(
        backtest=backtest,
        decisions=tuple(decisions),
        config=scan_config,
        symbols=symbols,
    )


def export_scan_backtest_report_json(
    result: ScanBacktestResult,
    path: str | Path,
) -> None:
    report_path = Path(path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(result.as_payload(), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _scan_one_day(
    timestamp: datetime,
    batch: Sequence[Candle],
    histories: dict[str, list[Candle]],
    accepted_by_timestamp: dict[datetime, set[tuple[str, PositionDirection]]],
    active_acceptances: dict[tuple[str, PositionDirection], int],
    decisions: list[ScanDecision],
    config: ScanBacktestConfig,
) -> None:
    eligible_symbols = (
        config.pit_universe.eligible_symbols(timestamp)
        if config.pit_universe is not None
        else None
    )
    candidates = _recommend_from_histories(
        histories,
        config=config,
        eligible_symbols=eligible_symbols,
    )
    recommended = tuple(candidates[: config.scan_top_n])
    newly_accepted = tuple(
        (candidate.symbol, candidate.direction)
        for candidate in recommended[: config.accept_top_n]
    )
    for entry in newly_accepted:
        active_acceptances[entry] = max(config.accept_hold_days, 1)
    accepted_entries = tuple(sorted(active_acceptances))
    accepted_symbols = tuple(sorted({symbol for symbol, _direction in accepted_entries}))
    accepted_by_timestamp[timestamp] = set(accepted_entries)
    decisions.append(
        ScanDecision(
            timestamp=timestamp,
            recommended=recommended,
            accepted_symbols=accepted_symbols,
            accepted_entries=accepted_entries,
        )
    )
    for candle in batch:
        histories.setdefault(candle.symbol, []).append(candle)
    for entry in tuple(active_acceptances):
        active_acceptances[entry] -= 1
        if active_acceptances[entry] <= 0:
            active_acceptances.pop(entry, None)


def _recommend_from_histories(
    histories: Mapping[str, Sequence[Candle]],
    *,
    config: ScanBacktestConfig,
    eligible_symbols: frozenset[str] | None = None,
) -> list[ScanCandidate]:
    candidates: list[ScanCandidate] = []
    for symbol, history in histories.items():
        if eligible_symbols is not None and symbol not in eligible_symbols:
            continue
        candidates.extend(_candidates(symbol, history, config=config))
    candidates.sort(key=lambda item: (item.distance_pct, item.symbol))
    return candidates


def _candidates(
    symbol: str,
    history: Sequence[Candle],
    *,
    config: ScanBacktestConfig,
) -> tuple[ScanCandidate, ...]:
    if len(history) < config.min_completed_candles:
        return ()
    current_price = history[-1].close
    if current_price < config.min_price:
        return ()
    average_value = average_traded_value(
        history,
        days=config.average_daily_value_days,
    )
    if average_value is None or average_value < config.min_average_daily_value:
        return ()
    entry_high_20, entry_low_20 = donchian_channel(
        history,
        period=20,
        exclude_current=False,
    )
    entry_high_55, entry_low_55 = donchian_channel(
        history,
        period=55,
        exclude_current=False,
    )
    candidates: list[ScanCandidate] = []
    if PositionDirection.LONG in config.scan_directions:
        distance_pct = _nearest_distance(
            _long_breakout_distance_pct(current_price, entry_high)
            for entry_high in (entry_high_20, entry_high_55)
        )
        if _distance_allowed(distance_pct, config.max_breakout_distance_pct):
            candidates.append(
                ScanCandidate(
                    symbol=symbol,
                    direction=PositionDirection.LONG,
                    current_price=current_price,
                    entry_high_20=entry_high_20,
                    entry_high_55=entry_high_55,
                    entry_low_20=entry_low_20,
                    entry_low_55=entry_low_55,
                    distance_pct=distance_pct,
                    average_daily_value=average_value,
                )
            )
    if PositionDirection.SHORT in config.scan_directions:
        distance_pct = _nearest_distance(
            _short_breakout_distance_pct(current_price, entry_low)
            for entry_low in (entry_low_20, entry_low_55)
        )
        if _distance_allowed(distance_pct, config.max_breakout_distance_pct):
            candidates.append(
                ScanCandidate(
                    symbol=symbol,
                    direction=PositionDirection.SHORT,
                    current_price=current_price,
                    entry_high_20=entry_high_20,
                    entry_high_55=entry_high_55,
                    entry_low_20=entry_low_20,
                    entry_low_55=entry_low_55,
                    distance_pct=distance_pct,
                    average_daily_value=average_value,
                )
            )
    return tuple(candidates)


def _nearest_distance(distances: Any) -> Decimal:
    clean = [item for item in distances if item is not None and item >= 0]
    if not clean:
        return Decimal("Infinity")
    return min(clean)


def _distance_allowed(
    distance_pct: Decimal,
    max_breakout_distance_pct: Decimal | None,
) -> bool:
    if distance_pct == Decimal("Infinity"):
        return False
    if max_breakout_distance_pct is None:
        return True
    return distance_pct <= max_breakout_distance_pct


def _long_breakout_distance_pct(
    current_price: Decimal,
    entry_high: Decimal | None,
) -> Decimal | None:
    if entry_high is None or current_price <= 0:
        return None
    return ((entry_high - current_price) / current_price) * Decimal("100")


def _short_breakout_distance_pct(
    current_price: Decimal,
    entry_low: Decimal | None,
) -> Decimal | None:
    if entry_low is None or current_price <= 0:
        return None
    return ((current_price - entry_low) / current_price) * Decimal("100")


def _pit_allows(
    pit_universe: PitUniverse | None,
    timestamp: datetime,
    symbol: str,
) -> bool:
    if pit_universe is None:
        return True
    return pit_universe.is_eligible(timestamp, symbol)
