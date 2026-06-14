from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from typing import Any, Mapping, Sequence

from .backtest import (
    AuditEvent,
    BacktestResult,
    BacktestTrade,
    EquityPoint,
    backtest_result_to_dict,
    load_candles_csv,
)
from .domain import Candle, StrategyState
from .universe import average_traded_value


@dataclass(frozen=True)
class MomentumBacktestConfig:
    initial_equity: Decimal = Decimal("100000")
    market_symbol: str = "SPY"
    momentum_lookback_days: int = 252
    momentum_skip_days: int = 21
    trend_ma_days: int = 200
    exit_ma_days: int = 100
    max_positions: int = 10
    accept_top_n: int = 3
    target_position_pct: Decimal = Decimal("0.10")
    min_price: Decimal = Decimal("5")
    min_average_daily_value: Decimal = Decimal("50000000")
    average_daily_value_days: int = 20
    use_market_filter: bool = True
    buy_commission_rate: Decimal = Decimal("0.001")
    sell_commission_rate: Decimal = Decimal("0.001")
    sell_sec_fee_rate: Decimal = Decimal("0.0000206")
    min_sec_fee: Decimal = Decimal("0.01")


@dataclass(frozen=True)
class MomentumCandidate:
    symbol: str
    score: Decimal
    current_price: Decimal
    trend_ma: Decimal

    def as_payload(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "score": str(self.score),
            "current_price": str(self.current_price),
            "trend_ma": str(self.trend_ma),
        }


@dataclass(frozen=True)
class MomentumDecision:
    timestamp: datetime
    market_filter_passed: bool
    recommended: tuple[MomentumCandidate, ...]
    accepted_symbols: tuple[str, ...]

    def as_payload(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "market_filter_passed": self.market_filter_passed,
            "recommended": [candidate.as_payload() for candidate in self.recommended],
            "accepted_symbols": list(self.accepted_symbols),
        }


@dataclass(frozen=True)
class MomentumBacktestResult:
    backtest: BacktestResult
    decisions: tuple[MomentumDecision, ...]
    config: MomentumBacktestConfig
    symbols: tuple[str, ...]

    def as_payload(self) -> dict[str, Any]:
        return {
            **backtest_result_to_dict(self.backtest),
            "momentum": {
                "symbols": list(self.symbols),
                "symbol_count": len(self.symbols),
                "decision_days": len(self.decisions),
                "accepted_days": sum(1 for item in self.decisions if item.accepted_symbols),
                "config": {
                    "market_symbol": self.config.market_symbol,
                    "momentum_lookback_days": self.config.momentum_lookback_days,
                    "momentum_skip_days": self.config.momentum_skip_days,
                    "trend_ma_days": self.config.trend_ma_days,
                    "exit_ma_days": self.config.exit_ma_days,
                    "max_positions": self.config.max_positions,
                    "accept_top_n": self.config.accept_top_n,
                    "target_position_pct": str(self.config.target_position_pct),
                    "min_price": str(self.config.min_price),
                    "min_average_daily_value": str(self.config.min_average_daily_value),
                    "use_market_filter": self.config.use_market_filter,
                    "buy_commission_rate": str(self.config.buy_commission_rate),
                    "sell_commission_rate": str(self.config.sell_commission_rate),
                    "sell_sec_fee_rate": str(self.config.sell_sec_fee_rate),
                    "min_sec_fee": str(self.config.min_sec_fee),
                },
                "decisions": [decision.as_payload() for decision in self.decisions],
            },
        }


@dataclass
class _MomentumPosition:
    symbol: str
    entry_at: datetime
    entry_price: Decimal
    qty: Decimal
    entry_cost: Decimal


def load_momentum_backtest_candles(data_dir: str | Path) -> tuple[Candle, ...]:
    candles: list[Candle] = []
    for path in sorted(Path(data_dir).glob("*.csv")):
        candles.extend(load_candles_csv(path))
    return tuple(sorted(candles, key=lambda candle: (candle.timestamp, candle.symbol)))


def run_momentum_backtest(
    candles: Sequence[Candle],
    *,
    config: MomentumBacktestConfig | None = None,
) -> MomentumBacktestResult:
    cfg = config or MomentumBacktestConfig()
    ordered = tuple(sorted(candles, key=lambda candle: (candle.timestamp, candle.symbol)))
    histories: dict[str, list[Candle]] = {}
    last_closes: dict[str, Decimal] = {}
    positions: dict[str, _MomentumPosition] = {}
    cash = cfg.initial_equity
    trades: list[BacktestTrade] = []
    audit: list[AuditEvent] = []
    equity_curve: list[EquityPoint] = []
    decisions: list[MomentumDecision] = []

    for timestamp, batch in _group_by_timestamp(ordered):
        batch_by_symbol = {candle.symbol: candle for candle in batch}
        exited_symbols: set[str] = set()
        for symbol, candle in batch_by_symbol.items():
            last_closes[symbol] = candle.close

        for symbol, position in tuple(positions.items()):
            candle = batch_by_symbol.get(symbol)
            history = histories.get(symbol, [])
            exit_ma = _sma(history, cfg.exit_ma_days)
            if candle is None or exit_ma is None or candle.close >= exit_ma:
                continue
            cash_delta = _sell_cash_delta(candle.close, position.qty, cfg)
            cash += cash_delta
            pnl = cash_delta - position.entry_cost
            trades.append(
                BacktestTrade(
                    symbol=symbol,
                    system="MOMENTUM",
                    entry_at=position.entry_at,
                    exit_at=timestamp,
                    entry_price=position.entry_price,
                    exit_price=candle.close,
                    qty=position.qty,
                    realized_pnl=pnl,
                    exit_reason="exit_ma",
                    units=1,
                )
            )
            positions.pop(symbol, None)
            exited_symbols.add(symbol)
            audit.append(
                AuditEvent(
                    timestamp=timestamp,
                    symbol=symbol,
                    kind="EXIT",
                    action="fill_sell",
                    reason="exit_ma",
                    fill_price=candle.close,
                    qty=position.qty,
                    cash_after=cash,
                    equity_after=_equity(cash, positions, last_closes, cfg),
                )
            )

        market_ok = _market_filter_passes(histories, cfg)
        candidates = (
            _rank_candidates(histories, cfg)
            if market_ok and len(positions) < cfg.max_positions
            else []
        )
        accepted: list[str] = []
        for candidate in candidates:
            if len(accepted) >= cfg.accept_top_n or len(positions) >= cfg.max_positions:
                break
            if candidate.symbol in positions or candidate.symbol in exited_symbols:
                continue
            candle = batch_by_symbol.get(candidate.symbol)
            if candle is None:
                continue
            equity = _equity(cash, positions, last_closes, cfg)
            allocation = equity * cfg.target_position_pct
            qty = (allocation / candle.close).to_integral_value(rounding=ROUND_FLOOR)
            if qty <= 0:
                continue
            cost = qty * candle.close
            cash_delta = _buy_cash_delta(candle.close, qty, cfg)
            if -cash_delta > cash:
                continue
            cash += cash_delta
            positions[candidate.symbol] = _MomentumPosition(
                symbol=candidate.symbol,
                entry_at=timestamp,
                entry_price=candle.close,
                qty=qty,
                entry_cost=-cash_delta,
            )
            accepted.append(candidate.symbol)
            audit.append(
                AuditEvent(
                    timestamp=timestamp,
                    symbol=candidate.symbol,
                    kind="ENTRY",
                    action="fill_buy",
                    reason="relative_momentum",
                    observed_price=candidate.current_price,
                    fill_price=candle.close,
                    qty=qty,
                    cash_after=cash,
                    equity_after=_equity(cash, positions, last_closes, cfg),
                )
            )

        decisions.append(
            MomentumDecision(
                timestamp=timestamp,
                market_filter_passed=market_ok,
                recommended=tuple(candidates[: cfg.accept_top_n]),
                accepted_symbols=tuple(accepted),
            )
        )

        for candle in batch:
            histories.setdefault(candle.symbol, []).append(candle)

        equity_curve.append(
            EquityPoint(
                timestamp=timestamp,
                cash=cash,
                position_value=_position_value(positions, last_closes, cfg),
                total_equity=_equity(cash, positions, last_closes, cfg),
            )
        )

    result = BacktestResult(
        final_equity=_equity(cash, positions, last_closes, cfg),
        trades=tuple(trades),
        equity_curve=tuple(equity_curve),
        audit_log=tuple(audit),
        strategy_state=StrategyState(),
        initial_equity=cfg.initial_equity,
    )
    symbols = tuple(sorted({candle.symbol for candle in ordered if candle.symbol != cfg.market_symbol}))
    return MomentumBacktestResult(
        backtest=result,
        decisions=tuple(decisions),
        config=cfg,
        symbols=symbols,
    )


def export_momentum_backtest_report_json(
    result: MomentumBacktestResult,
    path: str | Path,
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(result.as_payload(), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _group_by_timestamp(candles: Sequence[Candle]):
    current_timestamp: datetime | None = None
    batch: list[Candle] = []
    for candle in candles:
        if current_timestamp is None:
            current_timestamp = candle.timestamp
        if candle.timestamp != current_timestamp:
            yield current_timestamp, batch
            current_timestamp = candle.timestamp
            batch = []
        batch.append(candle)
    if current_timestamp is not None:
        yield current_timestamp, batch


def _rank_candidates(
    histories: Mapping[str, Sequence[Candle]],
    config: MomentumBacktestConfig,
) -> list[MomentumCandidate]:
    candidates: list[MomentumCandidate] = []
    for symbol, history in histories.items():
        if symbol == config.market_symbol:
            continue
        candidate = _candidate(symbol, history, config)
        if candidate is not None:
            candidates.append(candidate)
    candidates.sort(key=lambda item: (-item.score, item.symbol))
    return candidates


def _candidate(
    symbol: str,
    history: Sequence[Candle],
    config: MomentumBacktestConfig,
) -> MomentumCandidate | None:
    required = max(
        config.momentum_lookback_days,
        config.trend_ma_days,
        config.average_daily_value_days,
    ) + config.momentum_skip_days
    if len(history) <= required:
        return None
    current = history[-1].close
    if current < config.min_price:
        return None
    average_value = average_traded_value(
        history,
        days=config.average_daily_value_days,
    )
    if average_value is None or average_value < config.min_average_daily_value:
        return None
    trend_ma = _sma(history, config.trend_ma_days)
    if trend_ma is None or current <= trend_ma:
        return None
    exit_ma = _sma(history, config.exit_ma_days)
    if exit_ma is None or current <= exit_ma:
        return None
    recent = history[-config.momentum_skip_days - 1].close
    past = history[-config.momentum_lookback_days - config.momentum_skip_days - 1].close
    if past <= 0:
        return None
    score = (recent - past) / past
    if score <= 0:
        return None
    return MomentumCandidate(
        symbol=symbol,
        score=score,
        current_price=current,
        trend_ma=trend_ma,
    )


def _market_filter_passes(
    histories: Mapping[str, Sequence[Candle]],
    config: MomentumBacktestConfig,
) -> bool:
    if not config.use_market_filter:
        return True
    history = histories.get(config.market_symbol, ())
    market_ma = _sma(history, config.trend_ma_days)
    if not history or market_ma is None:
        return False
    return history[-1].close > market_ma


def _sma(history: Sequence[Candle], days: int) -> Decimal | None:
    if days <= 0 or len(history) < days:
        return None
    return sum((candle.close for candle in history[-days:]), Decimal("0")) / Decimal(days)


def _position_value(
    positions: Mapping[str, _MomentumPosition],
    last_closes: Mapping[str, Decimal],
    config: MomentumBacktestConfig,
) -> Decimal:
    total = Decimal("0")
    for symbol, position in positions.items():
        total += _sell_cash_delta(
            last_closes.get(symbol, position.entry_price),
            position.qty,
            config,
        )
    return total


def _equity(
    cash: Decimal,
    positions: Mapping[str, _MomentumPosition],
    last_closes: Mapping[str, Decimal],
    config: MomentumBacktestConfig | None = None,
) -> Decimal:
    cfg = config or MomentumBacktestConfig()
    return cash + _position_value(positions, last_closes, cfg)


def _buy_cash_delta(
    price: Decimal,
    qty: Decimal,
    config: MomentumBacktestConfig,
) -> Decimal:
    gross = price * qty
    commission = gross * config.buy_commission_rate
    return -(gross + commission)


def _sell_cash_delta(
    price: Decimal,
    qty: Decimal,
    config: MomentumBacktestConfig,
) -> Decimal:
    gross = price * qty
    commission = gross * config.sell_commission_rate
    sec_fee = max(gross * config.sell_sec_fee_rate, config.min_sec_fee)
    return gross - commission - sec_fee
