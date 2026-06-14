from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .domain import (
    Candle,
    PositionDirection,
    PositionState,
    PositionStatus,
    Side,
    Signal,
    SignalKind,
    StrategyState,
    TradeOutcome,
    UnitState,
    as_decimal,
    parse_timestamp,
)
from .strategy import apply_trade_outcomes, build_indicator_snapshot, evaluate_signals


@dataclass(frozen=True)
class BacktestCosts:
    commission_rate: Decimal = Decimal("0")
    fixed_commission: Decimal = Decimal("0")
    tax_rate: Decimal = Decimal("0")
    slippage_rate: Decimal = Decimal("0")


@dataclass(frozen=True)
class BacktestConfig:
    initial_equity: Decimal = Decimal("10000000")
    unit_qty: Decimal = Decimal("1")
    risk_pct_per_unit: Decimal | None = None
    lot_size: Decimal = Decimal("1")
    minimum_tick: Decimal = Decimal("0")
    n_method: str = "turtle"
    stop_n: Decimal = Decimal("2")
    max_units_per_symbol: int = 4
    max_total_long_units: int | None = 12
    max_total_short_units: int | None = 12
    pyramid_step_n: Decimal = Decimal("0.5")
    allowed_directions: tuple[PositionDirection, ...] = (PositionDirection.LONG,)
    costs: BacktestCosts = field(default_factory=BacktestCosts)


@dataclass(frozen=True)
class AuditEvent:
    timestamp: datetime
    symbol: str
    kind: str
    action: str
    reason: str
    trigger_price: Decimal | None = None
    observed_price: Decimal | None = None
    fill_price: Decimal | None = None
    qty: Decimal | None = None
    cash_after: Decimal | None = None
    equity_after: Decimal | None = None


@dataclass(frozen=True)
class EquityPoint:
    timestamp: datetime
    cash: Decimal
    position_value: Decimal
    total_equity: Decimal


@dataclass(frozen=True)
class BacktestTrade:
    symbol: str
    system: str
    entry_at: datetime
    exit_at: datetime
    entry_price: Decimal
    exit_price: Decimal
    qty: Decimal
    realized_pnl: Decimal
    exit_reason: str
    units: int
    direction: str = PositionDirection.LONG.value


@dataclass(frozen=True)
class BacktestResult:
    final_equity: Decimal
    trades: tuple[BacktestTrade, ...]
    equity_curve: tuple[EquityPoint, ...]
    audit_log: tuple[AuditEvent, ...]
    strategy_state: StrategyState
    initial_equity: Decimal = Decimal("0")


@dataclass
class _OpenCost:
    entry_at: datetime
    total_cost: Decimal


def load_candles_csv(path: str | Path, *, default_symbol: str = "") -> list[Candle]:
    """Load daily candles from a CSV file using Decimal-safe parsing."""

    candles: list[Candle] = []
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            candles.append(_candle_from_csv_row(row, default_symbol=default_symbol))
    return sorted(candles, key=lambda candle: (candle.timestamp, candle.symbol))


def backtest_result_to_dict(result: BacktestResult) -> dict[str, Any]:
    return {
        "final_equity": _json_value(result.final_equity),
        "summary": _json_value(summarize_backtest_result(result)),
        "trades": [_dataclass_to_dict(trade) for trade in result.trades],
        "equity_curve": [_dataclass_to_dict(point) for point in result.equity_curve],
        "audit_log": [_dataclass_to_dict(event) for event in result.audit_log],
        "strategy_state": {
            "pending_s1_skip": sorted(result.strategy_state.pending_s1_skip),
        },
    }


def summarize_backtest_result(
    result: BacktestResult,
    *,
    initial_equity: Decimal | None = None,
) -> dict[str, Any]:
    """Calculate review metrics without changing the backtest trading rules."""

    starting_equity = initial_equity or (
        result.initial_equity
        if result.initial_equity != 0
        else result.equity_curve[0].total_equity
        if result.equity_curve
        else result.final_equity
    )
    net_pnl = result.final_equity - starting_equity
    realized_pnl = sum((trade.realized_pnl for trade in result.trades), Decimal("0"))
    winning_trades = sum(1 for trade in result.trades if trade.realized_pnl > 0)
    losing_trades = sum(1 for trade in result.trades if trade.realized_pnl < 0)
    trade_count = len(result.trades)
    max_drawdown, max_drawdown_pct = _max_drawdown(
        [point.total_equity for point in result.equity_curve],
        starting_equity=starting_equity,
    )
    min_equity = _min_equity(
        [point.total_equity for point in result.equity_curve],
        starting_equity=starting_equity,
        final_equity=result.final_equity,
    )

    return {
        "initial_equity": starting_equity,
        "final_equity": result.final_equity,
        "min_equity": min_equity,
        "net_pnl": net_pnl,
        "realized_pnl": realized_pnl,
        "return_pct": _pct(net_pnl, starting_equity),
        "min_return_pct": _pct(min_equity - starting_equity, starting_equity),
        "loss_pct": _pct(max(Decimal("0"), -net_pnl), starting_equity),
        "max_drawdown": max_drawdown,
        "max_drawdown_pct": max_drawdown_pct,
        "trade_count": trade_count,
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "win_rate_pct": _pct(Decimal(winning_trades), Decimal(trade_count))
        if trade_count
        else None,
    }


def _pct(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if denominator == 0:
        return None
    return (numerator / denominator) * Decimal("100")


def _min_equity(
    equity_values: Sequence[Decimal],
    *,
    starting_equity: Decimal,
    final_equity: Decimal,
) -> Decimal:
    values = [starting_equity, final_equity, *equity_values]
    return min(values)


def _max_drawdown(
    equity_values: Sequence[Decimal],
    *,
    starting_equity: Decimal,
) -> tuple[Decimal, Decimal | None]:
    peak = starting_equity
    max_drawdown = Decimal("0")
    max_drawdown_pct: Decimal | None = Decimal("0") if starting_equity != 0 else None
    for equity in equity_values:
        if equity > peak:
            peak = equity
        drawdown = peak - equity
        if drawdown > max_drawdown:
            max_drawdown = drawdown
            max_drawdown_pct = _pct(drawdown, peak)
    return max_drawdown, max_drawdown_pct


def export_backtest_report_json(result: BacktestResult, path: str | Path) -> None:
    """Write a reviewable JSON report with Decimal values preserved as strings."""

    report_path = Path(path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(backtest_result_to_dict(result), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _dataclass_to_dict(value: object) -> dict[str, Any]:
    if not is_dataclass(value):
        raise TypeError("expected dataclass instance")
    return {item.name: _json_value(getattr(value, item.name)) for item in fields(value)}


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return _dataclass_to_dict(value)
    if isinstance(value, tuple | list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def _first(row: dict[str, str], *names: str, default: str | None = None) -> str:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    if default is not None:
        return default
    raise ValueError(f"missing CSV column; expected one of {', '.join(names)}")


def _candle_from_csv_row(row: dict[str, str], *, default_symbol: str) -> Candle:
    return Candle(
        timestamp=parse_timestamp(_first(row, "timestamp", "date", "datetime")),
        symbol=_first(row, "symbol", "ticker", default=default_symbol),
        open=as_decimal(_first(row, "open", "openPrice")),
        high=as_decimal(_first(row, "high", "highPrice")),
        low=as_decimal(_first(row, "low", "lowPrice")),
        close=as_decimal(_first(row, "close", "closePrice")),
        volume=as_decimal(_first(row, "volume", default="0")),
        currency=_first(row, "currency", default="KRW"),
        adjusted=_first(row, "adjusted", default="true").lower() != "false",
        source=_first(row, "source", default="csv"),
    )


def _group_by_timestamp(candles: Sequence[Candle]) -> Iterable[tuple[datetime, list[Candle]]]:
    current_timestamp: datetime | None = None
    current_batch: list[Candle] = []
    for candle in candles:
        if current_timestamp is None:
            current_timestamp = candle.timestamp
        if candle.timestamp != current_timestamp:
            yield current_timestamp, current_batch
            current_timestamp = candle.timestamp
            current_batch = []
        current_batch.append(candle)
    if current_timestamp is not None:
        yield current_timestamp, current_batch


class BacktestEngine:
    """Daily-bar Turtle backtester with conservative same-bar ordering."""

    def __init__(self, config: BacktestConfig | None = None) -> None:
        self.config = config or BacktestConfig()

    def run(self, candles: Sequence[Candle]) -> BacktestResult:
        ordered = sorted(candles, key=lambda candle: candle.timestamp)
        if not ordered:
            return BacktestResult(
                final_equity=self.config.initial_equity,
                trades=(),
                equity_curve=(),
                audit_log=(),
                strategy_state=StrategyState(),
                initial_equity=self.config.initial_equity,
            )

        symbol = ordered[0].symbol
        if any(candle.symbol != symbol for candle in ordered):
            raise ValueError("BacktestEngine.run currently accepts one symbol at a time")

        cash = self.config.initial_equity
        position: PositionState | None = None
        open_cost: _OpenCost | None = None
        state = StrategyState()
        history: list[Candle] = []
        audit_log: list[AuditEvent] = []
        trades: list[BacktestTrade] = []
        equity_curve: list[EquityPoint] = []

        for candle in ordered:
            exited_this_bar = False

            if position is not None and position.status == PositionStatus.OPEN:
                exit_signals, state = evaluate_signals(
                    symbol=symbol,
                    completed_candles=history,
                    current_price=self._exit_observed_price(candle, position),
                    state=state,
                    position=position,
                    minimum_tick=self.config.minimum_tick,
                    n_method=self.config.n_method,
                    max_units_per_symbol=self.config.max_units_per_symbol,
                    pyramid_step_n=self.config.pyramid_step_n,
                )
                exit_signal = self._first_signal(
                    exit_signals,
                    {SignalKind.STOP, SignalKind.EXIT},
                )
                if exit_signal is not None:
                    fill_price = self._fill_price(candle, exit_signal)
                    cash, trade = self._exit_position(
                        position=position,
                        open_cost=open_cost,
                        signal=exit_signal,
                        fill_price=fill_price,
                        cash=cash,
                        at=candle.timestamp,
                    )
                    trades.append(trade)
                    state = apply_trade_outcomes(
                        state,
                        [
                            TradeOutcome(
                                symbol=trade.symbol,
                                system=position.system,
                                realized_pnl=trade.realized_pnl,
                                direction=position.direction,
                            )
                        ],
                    )
                    position = None
                    open_cost = None
                    exited_this_bar = True
                    audit_log.append(
                        self._audit(
                            candle=candle,
                            signal=exit_signal,
                            action=self._fill_action(exit_signal),
                            fill_price=fill_price,
                            qty=trade.qty,
                            cash_after=cash,
                            position=position,
                        )
                    )

            if not exited_this_bar:
                if position is not None and position.status == PositionStatus.OPEN:
                    pyramid_signals, state = evaluate_signals(
                        symbol=symbol,
                        completed_candles=history,
                        current_price=self._pyramid_observed_price(candle, position),
                        state=state,
                        position=position,
                        minimum_tick=self.config.minimum_tick,
                        n_method=self.config.n_method,
                        max_units_per_symbol=self.config.max_units_per_symbol,
                        pyramid_step_n=self.config.pyramid_step_n,
                    )
                    pyramid_signal = self._first_signal(pyramid_signals, {SignalKind.PYRAMID})
                    if pyramid_signal is not None:
                        fill_price = self._fill_price(candle, pyramid_signal)
                        qty = self._unit_qty(
                            self._equity(cash, position, candle.close),
                            position.entry_n,
                        )
                        if qty <= Decimal("0"):
                            audit_log.append(
                                AuditEvent(
                                    timestamp=candle.timestamp,
                                    symbol=symbol,
                                    kind="BLOCK",
                                    action="block_buy",
                                    reason="unit_qty_zero",
                                    trigger_price=pyramid_signal.trigger_price,
                                    observed_price=pyramid_signal.observed_price,
                                    cash_after=cash,
                                    equity_after=self._equity(cash, position, candle.close),
                                )
                            )
                        else:
                            next_position, cash_delta = self._add_unit(
                                position,
                                pyramid_signal,
                                fill_price,
                                qty,
                            )
                            if cash + cash_delta >= Decimal("0"):
                                cash += cash_delta
                                position = next_position
                                if open_cost is not None:
                                    open_cost.total_cost += self._open_cost_amount(
                                        position.direction,
                                        cash_delta,
                                    )
                                audit_log.append(
                                    self._audit(
                                        candle=candle,
                                        signal=pyramid_signal,
                                        action=self._fill_action(pyramid_signal),
                                        fill_price=fill_price,
                                        qty=qty,
                                        cash_after=cash,
                                        position=position,
                                    )
                                )
                            else:
                                audit_log.append(
                                    AuditEvent(
                                        timestamp=candle.timestamp,
                                        symbol=symbol,
                                        kind="BLOCK",
                                        action="block_buy",
                                        reason="insufficient_cash",
                                        trigger_price=pyramid_signal.trigger_price,
                                        observed_price=pyramid_signal.observed_price,
                                        cash_after=cash,
                                        equity_after=self._equity(cash, position, candle.close),
                                    )
                                )
                else:
                    entry_signals, state = self._entry_signals(
                        symbol=symbol,
                        completed_candles=history,
                        candle=candle,
                        state=state,
                    )
                    entry_signal = self._first_signal(entry_signals, {SignalKind.ENTRY})
                    if entry_signal is not None:
                        snapshot = build_indicator_snapshot(
                            symbol=symbol,
                            candles=history,
                            n_method=self.config.n_method,
                            exclude_current=False,
                        )
                        if snapshot.n is None:
                            audit_log.append(
                                AuditEvent(
                                    timestamp=candle.timestamp,
                                    symbol=symbol,
                                    kind="BLOCK",
                                    action="block_buy",
                                    reason="missing_n",
                                    trigger_price=entry_signal.trigger_price,
                                    observed_price=entry_signal.observed_price,
                                    cash_after=cash,
                                    equity_after=self._equity(cash, position, candle.close),
                                )
                            )
                        else:
                            fill_price = self._fill_price(candle, entry_signal)
                            qty = self._unit_qty(
                                self._equity(cash, position, candle.close),
                                snapshot.n,
                            )
                            if qty <= Decimal("0"):
                                audit_log.append(
                                    AuditEvent(
                                        timestamp=candle.timestamp,
                                        symbol=symbol,
                                        kind="BLOCK",
                                        action="block_buy",
                                        reason="unit_qty_zero",
                                        trigger_price=entry_signal.trigger_price,
                                        observed_price=entry_signal.observed_price,
                                        cash_after=cash,
                                        equity_after=self._equity(cash, position, candle.close),
                                    )
                                )
                            else:
                                next_position, cash_delta = self._open_position(
                                    entry_signal,
                                    fill_price,
                                    snapshot.n,
                                    qty,
                                )
                                if cash + cash_delta >= Decimal("0"):
                                    cash += cash_delta
                                    position = next_position
                                    open_cost = _OpenCost(
                                        entry_at=candle.timestamp,
                                        total_cost=self._open_cost_amount(
                                            position.direction,
                                            cash_delta,
                                        ),
                                    )
                                    audit_log.append(
                                        self._audit(
                                            candle=candle,
                                            signal=entry_signal,
                                            action=self._fill_action(entry_signal),
                                            fill_price=fill_price,
                                            qty=qty,
                                            cash_after=cash,
                                            position=position,
                                        )
                                    )
                                else:
                                    audit_log.append(
                                        AuditEvent(
                                            timestamp=candle.timestamp,
                                            symbol=symbol,
                                            kind="BLOCK",
                                            action="block_buy",
                                            reason="insufficient_cash",
                                            trigger_price=entry_signal.trigger_price,
                                            observed_price=entry_signal.observed_price,
                                            cash_after=cash,
                                            equity_after=self._equity(cash, position, candle.close),
                                        )
                                    )

            equity_curve.append(
                EquityPoint(
                    timestamp=candle.timestamp,
                    cash=cash,
                    position_value=self._position_value(position, candle.close),
                    total_equity=self._equity(cash, position, candle.close),
                )
            )
            history.append(candle)

        final_close = ordered[-1].close
        return BacktestResult(
            final_equity=self._equity(cash, position, final_close),
            trades=tuple(trades),
            equity_curve=tuple(equity_curve),
            audit_log=tuple(audit_log),
            strategy_state=state,
            initial_equity=self.config.initial_equity,
        )

    def run_portfolio(
        self,
        candles: Sequence[Candle],
        *,
        entry_filter: Callable[[datetime, str], bool] | None = None,
        entry_direction_filter: Callable[[datetime, str, PositionDirection], bool] | None = None,
    ) -> BacktestResult:
        ordered = sorted(candles, key=lambda candle: (candle.timestamp, candle.symbol))
        if not ordered:
            return BacktestResult(
                final_equity=self.config.initial_equity,
                trades=(),
                equity_curve=(),
                audit_log=(),
                strategy_state=StrategyState(),
                initial_equity=self.config.initial_equity,
            )

        cash = self.config.initial_equity
        positions: dict[str, PositionState] = {}
        open_costs: dict[str, _OpenCost] = {}
        histories: dict[str, list[Candle]] = {}
        last_closes: dict[str, Decimal] = {}
        state = StrategyState()
        audit_log: list[AuditEvent] = []
        trades: list[BacktestTrade] = []
        equity_curve: list[EquityPoint] = []

        for timestamp, batch in _group_by_timestamp(ordered):
            exited_symbols: set[str] = set()

            for candle in batch:
                symbol = candle.symbol
                histories.setdefault(symbol, [])
                last_closes[symbol] = candle.close
                position = positions.get(symbol)
                if position is None or position.status != PositionStatus.OPEN:
                    continue

                exit_signals, state = evaluate_signals(
                    symbol=symbol,
                    completed_candles=histories[symbol],
                    current_price=self._exit_observed_price(candle, position),
                    state=state,
                    position=position,
                    minimum_tick=self.config.minimum_tick,
                    n_method=self.config.n_method,
                    max_units_per_symbol=self.config.max_units_per_symbol,
                    pyramid_step_n=self.config.pyramid_step_n,
                )
                exit_signal = self._first_signal(
                    exit_signals,
                    {SignalKind.STOP, SignalKind.EXIT},
                )
                if exit_signal is None:
                    continue

                fill_price = self._fill_price(candle, exit_signal)
                cash, trade = self._exit_position(
                    position=position,
                    open_cost=open_costs.get(symbol),
                    signal=exit_signal,
                    fill_price=fill_price,
                    cash=cash,
                    at=candle.timestamp,
                )
                trades.append(trade)
                state = apply_trade_outcomes(
                    state,
                    [
                        TradeOutcome(
                            symbol=trade.symbol,
                            system=position.system,
                            realized_pnl=trade.realized_pnl,
                            direction=position.direction,
                        )
                    ],
                )
                positions.pop(symbol, None)
                open_costs.pop(symbol, None)
                exited_symbols.add(symbol)
                audit_log.append(
                    self._portfolio_audit(
                        candle=candle,
                        signal=exit_signal,
                        action=self._fill_action(exit_signal),
                        fill_price=fill_price,
                        qty=trade.qty,
                        cash_after=cash,
                        positions=positions,
                        last_closes=last_closes,
                    )
                )

            for candle in batch:
                symbol = candle.symbol
                if symbol in exited_symbols:
                    continue

                position = positions.get(symbol)
                equity_before = self._portfolio_equity(cash, positions, last_closes)
                if position is not None and position.status == PositionStatus.OPEN:
                    pyramid_signals, state = evaluate_signals(
                        symbol=symbol,
                        completed_candles=histories[symbol],
                        current_price=self._pyramid_observed_price(candle, position),
                        state=state,
                        position=position,
                        minimum_tick=self.config.minimum_tick,
                        n_method=self.config.n_method,
                        max_units_per_symbol=self.config.max_units_per_symbol,
                        pyramid_step_n=self.config.pyramid_step_n,
                    )
                    pyramid_signal = self._first_signal(pyramid_signals, {SignalKind.PYRAMID})
                    if pyramid_signal is None:
                        continue

                    fill_price = self._fill_price(candle, pyramid_signal)
                    if self._max_total_units_reached(positions, position.direction):
                        audit_log.append(
                            self._portfolio_block_buy_event(
                                candle=candle,
                                signal=pyramid_signal,
                                reason=self._max_total_units_reason(position.direction),
                                cash=cash,
                                positions=positions,
                                last_closes=last_closes,
                            )
                        )
                        continue
                    qty = self._unit_qty(equity_before, position.entry_n)
                    if qty <= Decimal("0"):
                        audit_log.append(
                            self._portfolio_block_buy_event(
                                candle=candle,
                                signal=pyramid_signal,
                                reason="unit_qty_zero",
                                cash=cash,
                                positions=positions,
                                last_closes=last_closes,
                            )
                        )
                        continue

                    next_position, cash_delta = self._add_unit(
                        position,
                        pyramid_signal,
                        fill_price,
                        qty,
                    )
                    if cash + cash_delta >= Decimal("0"):
                        cash += cash_delta
                        positions[symbol] = next_position
                        if symbol in open_costs:
                            open_costs[symbol].total_cost += self._open_cost_amount(
                                position.direction,
                                cash_delta,
                            )
                        audit_log.append(
                            self._portfolio_audit(
                                candle=candle,
                                signal=pyramid_signal,
                                action=self._fill_action(pyramid_signal),
                                fill_price=fill_price,
                                qty=qty,
                                cash_after=cash,
                                positions=positions,
                                last_closes=last_closes,
                            )
                        )
                    else:
                        audit_log.append(
                            self._portfolio_block_buy_event(
                                candle=candle,
                                signal=pyramid_signal,
                                reason="insufficient_cash",
                                cash=cash,
                                positions=positions,
                                last_closes=last_closes,
                            )
                        )
                else:
                    if entry_filter is not None and not entry_filter(timestamp, symbol):
                        continue
                    allowed_directions = tuple(
                        direction
                        for direction in self.config.allowed_directions
                        if entry_direction_filter is None
                        or entry_direction_filter(timestamp, symbol, direction)
                    )
                    if not allowed_directions:
                        continue
                    entry_signals, state = self._entry_signals(
                        symbol=symbol,
                        completed_candles=histories[symbol],
                        candle=candle,
                        state=state,
                        allowed_directions=allowed_directions,
                    )
                    entry_signal = self._first_signal(entry_signals, {SignalKind.ENTRY})
                    if entry_signal is None:
                        continue

                    snapshot = build_indicator_snapshot(
                        symbol=symbol,
                        candles=histories[symbol],
                        n_method=self.config.n_method,
                        exclude_current=False,
                    )
                    if snapshot.n is None:
                        audit_log.append(
                            self._portfolio_block_buy_event(
                                candle=candle,
                                signal=entry_signal,
                                reason="missing_n",
                                cash=cash,
                                positions=positions,
                                last_closes=last_closes,
                            )
                        )
                        continue

                    entry_direction = self._signal_direction(entry_signal)
                    if self._max_total_units_reached(positions, entry_direction):
                        audit_log.append(
                            self._portfolio_block_buy_event(
                                candle=candle,
                                signal=entry_signal,
                                reason=self._max_total_units_reason(entry_direction),
                                cash=cash,
                                positions=positions,
                                last_closes=last_closes,
                            )
                        )
                        continue

                    fill_price = self._fill_price(candle, entry_signal)
                    qty = self._unit_qty(equity_before, snapshot.n)
                    if qty <= Decimal("0"):
                        audit_log.append(
                            self._portfolio_block_buy_event(
                                candle=candle,
                                signal=entry_signal,
                                reason="unit_qty_zero",
                                cash=cash,
                                positions=positions,
                                last_closes=last_closes,
                            )
                        )
                        continue

                    next_position, cash_delta = self._open_position(
                        entry_signal,
                        fill_price,
                        snapshot.n,
                        qty,
                    )
                    if cash + cash_delta >= Decimal("0"):
                        cash += cash_delta
                        positions[symbol] = next_position
                        open_costs[symbol] = _OpenCost(
                            entry_at=candle.timestamp,
                            total_cost=self._open_cost_amount(
                                next_position.direction,
                                cash_delta,
                            ),
                        )
                        audit_log.append(
                            self._portfolio_audit(
                                candle=candle,
                                signal=entry_signal,
                                action=self._fill_action(entry_signal),
                                fill_price=fill_price,
                                qty=qty,
                                cash_after=cash,
                                positions=positions,
                                last_closes=last_closes,
                            )
                        )
                    else:
                        audit_log.append(
                            self._portfolio_block_buy_event(
                                candle=candle,
                                signal=entry_signal,
                                reason="insufficient_cash",
                                cash=cash,
                                positions=positions,
                                last_closes=last_closes,
                            )
                        )

            for candle in batch:
                histories[candle.symbol].append(candle)

            equity_curve.append(
                EquityPoint(
                    timestamp=timestamp,
                    cash=cash,
                    position_value=self._portfolio_position_value(positions, last_closes),
                    total_equity=self._portfolio_equity(cash, positions, last_closes),
                )
            )

        return BacktestResult(
            final_equity=self._portfolio_equity(cash, positions, last_closes),
            trades=tuple(trades),
            equity_curve=tuple(equity_curve),
            audit_log=tuple(audit_log),
            strategy_state=state,
            initial_equity=self.config.initial_equity,
        )

    @staticmethod
    def _first_signal(
        signals: Iterable[Signal],
        kinds: set[SignalKind],
    ) -> Signal | None:
        for signal in signals:
            if signal.kind in kinds:
                return signal
        return None

    def _buy_fill_price(self, candle: Candle, trigger_price: Decimal) -> Decimal:
        price = candle.open if candle.open >= trigger_price else trigger_price
        return price * (Decimal("1") + self.config.costs.slippage_rate)

    def _sell_fill_price(self, candle: Candle, trigger_price: Decimal) -> Decimal:
        price = candle.open if candle.open <= trigger_price else trigger_price
        return price * (Decimal("1") - self.config.costs.slippage_rate)

    def _fill_price(self, candle: Candle, signal: Signal) -> Decimal:
        if signal.side == Side.SELL:
            return self._sell_fill_price(candle, signal.trigger_price)
        return self._buy_fill_price(candle, signal.trigger_price)

    @staticmethod
    def _fill_action(signal: Signal) -> str:
        return "fill_sell" if signal.side == Side.SELL else "fill_buy"

    @staticmethod
    def _signal_direction(signal: Signal) -> PositionDirection:
        return PositionDirection.SHORT if signal.side == Side.SELL else PositionDirection.LONG

    @staticmethod
    def _exit_observed_price(candle: Candle, position: PositionState) -> Decimal:
        return candle.high if position.direction == PositionDirection.SHORT else candle.low

    @staticmethod
    def _pyramid_observed_price(candle: Candle, position: PositionState) -> Decimal:
        return candle.low if position.direction == PositionDirection.SHORT else candle.high

    def _entry_signals(
        self,
        *,
        symbol: str,
        completed_candles: Sequence[Candle],
        candle: Candle,
        state: StrategyState,
        allowed_directions: Sequence[PositionDirection] | None = None,
    ) -> tuple[list[Signal], StrategyState]:
        next_state = state
        for direction in allowed_directions or self.config.allowed_directions:
            current_price = candle.low if direction == PositionDirection.SHORT else candle.high
            signals, next_state = evaluate_signals(
                symbol=symbol,
                completed_candles=completed_candles,
                current_price=current_price,
                state=next_state,
                position=None,
                minimum_tick=self.config.minimum_tick,
                n_method=self.config.n_method,
                max_units_per_symbol=self.config.max_units_per_symbol,
                pyramid_step_n=self.config.pyramid_step_n,
                allowed_directions=(direction,),
            )
            if signals:
                return signals, next_state
        return [], next_state

    def _buy_cash_delta(self, fill_price: Decimal, qty: Decimal) -> Decimal:
        gross = fill_price * qty
        fee = gross * self.config.costs.commission_rate + self.config.costs.fixed_commission
        return -(gross + fee)

    def _sell_cash_delta(self, fill_price: Decimal, qty: Decimal) -> Decimal:
        gross = fill_price * qty
        fee = gross * self.config.costs.commission_rate + self.config.costs.fixed_commission
        tax = gross * self.config.costs.tax_rate
        return gross - fee - tax

    def _entry_cash_delta(
        self,
        direction: PositionDirection,
        fill_price: Decimal,
        qty: Decimal,
    ) -> Decimal:
        if direction == PositionDirection.SHORT:
            return self._sell_cash_delta(fill_price, qty)
        return self._buy_cash_delta(fill_price, qty)

    def _exit_cash_delta(
        self,
        direction: PositionDirection,
        fill_price: Decimal,
        qty: Decimal,
    ) -> Decimal:
        if direction == PositionDirection.SHORT:
            return self._buy_cash_delta(fill_price, qty)
        return self._sell_cash_delta(fill_price, qty)

    def _entry_stop_price(
        self,
        direction: PositionDirection,
        fill_price: Decimal,
        n: Decimal,
    ) -> Decimal:
        if direction == PositionDirection.SHORT:
            return fill_price + self.config.stop_n * n
        return fill_price - self.config.stop_n * n

    @staticmethod
    def _open_cost_amount(direction: PositionDirection, cash_delta: Decimal) -> Decimal:
        if direction == PositionDirection.SHORT:
            return cash_delta
        return -cash_delta

    def _open_position(
        self,
        signal: Signal,
        fill_price: Decimal,
        n: Decimal,
        qty: Decimal | None = None,
    ) -> tuple[PositionState, Decimal]:
        unit_qty = qty if qty is not None else self.config.unit_qty
        direction = self._signal_direction(signal)
        stop_price = self._entry_stop_price(direction, fill_price, n)
        unit = UnitState(
            unit_no=1,
            qty=unit_qty,
            entry_price=fill_price,
            n_at_entry=n,
            stop_price=stop_price,
            client_order_id=signal.signal_id,
        )
        position = PositionState(
            symbol=signal.symbol,
            system=signal.system,
            status=PositionStatus.OPEN,
            total_qty=unit_qty,
            avg_entry_price=fill_price,
            entry_n=n,
            current_stop_price=stop_price,
            last_unit_entry_price=fill_price,
            units=(unit,),
            direction=direction,
        )
        return position, self._entry_cash_delta(direction, fill_price, unit_qty)

    def _add_unit(
        self,
        position: PositionState,
        signal: Signal,
        fill_price: Decimal,
        qty: Decimal | None = None,
    ) -> tuple[PositionState, Decimal]:
        unit_qty = qty if qty is not None else self.config.unit_qty
        next_qty = position.total_qty + unit_qty
        avg_entry = (
            position.avg_entry_price * position.total_qty + fill_price * unit_qty
        ) / next_qty
        stop_price = self._entry_stop_price(position.direction, fill_price, position.entry_n)
        unit = UnitState(
            unit_no=len(position.units) + 1,
            qty=unit_qty,
            entry_price=fill_price,
            n_at_entry=position.entry_n,
            stop_price=stop_price,
            client_order_id=signal.signal_id,
        )
        return (
            PositionState(
                symbol=position.symbol,
                system=position.system,
                status=PositionStatus.OPEN,
                total_qty=next_qty,
                avg_entry_price=avg_entry,
                entry_n=position.entry_n,
                current_stop_price=stop_price,
                last_unit_entry_price=fill_price,
                units=position.units + (unit,),
                direction=position.direction,
            ),
            self._entry_cash_delta(position.direction, fill_price, unit_qty),
        )

    def _unit_qty(self, equity: Decimal, n: Decimal) -> Decimal:
        if self.config.risk_pct_per_unit is None:
            return self.config.unit_qty
        risk_per_share = self.config.stop_n * n
        if risk_per_share <= Decimal("0") or self.config.lot_size <= Decimal("0"):
            return Decimal("0")
        raw_qty = (equity * self.config.risk_pct_per_unit) / risk_per_share
        lots = (raw_qty / self.config.lot_size).to_integral_value(rounding=ROUND_FLOOR)
        return lots * self.config.lot_size

    def _exit_position(
        self,
        *,
        position: PositionState,
        open_cost: _OpenCost | None,
        signal: Signal,
        fill_price: Decimal,
        cash: Decimal,
        at: datetime,
    ) -> tuple[Decimal, BacktestTrade]:
        cash_delta = self._exit_cash_delta(position.direction, fill_price, position.total_qty)
        next_cash = cash + cash_delta
        entry_cash_basis = (
            open_cost.total_cost
            if open_cost is not None
            else position.avg_entry_price * position.total_qty
        )
        if position.direction == PositionDirection.SHORT:
            realized_pnl = entry_cash_basis + cash_delta
        else:
            realized_pnl = cash_delta - entry_cash_basis
        entry_at = open_cost.entry_at if open_cost is not None else at
        return (
            next_cash,
            BacktestTrade(
                symbol=position.symbol,
                system=position.system.value,
                entry_at=entry_at,
                exit_at=at,
                entry_price=position.units[0].entry_price,
                exit_price=fill_price,
                qty=position.total_qty,
                realized_pnl=realized_pnl,
                exit_reason=signal.reason,
                units=len(position.units),
                direction=position.direction.value,
            ),
        )

    def _audit(
        self,
        *,
        candle: Candle,
        signal: Signal,
        action: str,
        fill_price: Decimal,
        qty: Decimal,
        cash_after: Decimal,
        position: PositionState | None,
    ) -> AuditEvent:
        return AuditEvent(
            timestamp=candle.timestamp,
            symbol=candle.symbol,
            kind=signal.kind.value,
            action=action,
            reason=self._reason(candle, signal),
            trigger_price=signal.trigger_price,
            observed_price=signal.observed_price,
            fill_price=fill_price,
            qty=qty,
            cash_after=cash_after,
            equity_after=self._equity(cash_after, position, candle.close),
        )

    def _portfolio_audit(
        self,
        *,
        candle: Candle,
        signal: Signal,
        action: str,
        fill_price: Decimal,
        qty: Decimal,
        cash_after: Decimal,
        positions: dict[str, PositionState],
        last_closes: dict[str, Decimal],
    ) -> AuditEvent:
        return AuditEvent(
            timestamp=candle.timestamp,
            symbol=candle.symbol,
            kind=signal.kind.value,
            action=action,
            reason=self._reason(candle, signal),
            trigger_price=signal.trigger_price,
            observed_price=signal.observed_price,
            fill_price=fill_price,
            qty=qty,
            cash_after=cash_after,
            equity_after=self._portfolio_equity(cash_after, positions, last_closes),
        )

    def _portfolio_block_buy_event(
        self,
        *,
        candle: Candle,
        signal: Signal,
        reason: str,
        cash: Decimal,
        positions: dict[str, PositionState],
        last_closes: dict[str, Decimal],
    ) -> AuditEvent:
        return AuditEvent(
            timestamp=candle.timestamp,
            symbol=candle.symbol,
            kind="BLOCK",
            action="block_buy",
            reason=reason,
            trigger_price=signal.trigger_price,
            observed_price=signal.observed_price,
            cash_after=cash,
            equity_after=self._portfolio_equity(cash, positions, last_closes),
        )

    @staticmethod
    def _reason(candle: Candle, signal: Signal) -> str:
        if (
            signal.kind in {SignalKind.ENTRY, SignalKind.PYRAMID}
            and signal.side == Side.BUY
            and candle.open >= signal.trigger_price
        ):
            return f"gap_breakout:{signal.reason}"
        if (
            signal.kind in {SignalKind.ENTRY, SignalKind.PYRAMID}
            and signal.side == Side.SELL
            and candle.open <= signal.trigger_price
        ):
            return f"gap_breakout:{signal.reason}"
        if (
            signal.kind in {SignalKind.STOP, SignalKind.EXIT}
            and signal.side == Side.SELL
            and candle.open <= signal.trigger_price
        ):
            return f"gap_exit:{signal.reason}"
        if (
            signal.kind in {SignalKind.STOP, SignalKind.EXIT}
            and signal.side == Side.BUY
            and candle.open >= signal.trigger_price
        ):
            return f"gap_exit:{signal.reason}"
        return signal.reason

    @staticmethod
    def _position_value(position: PositionState | None, mark_price: Decimal) -> Decimal:
        if position is None:
            return Decimal("0")
        if position.direction == PositionDirection.SHORT:
            return -(position.total_qty * mark_price)
        return position.total_qty * mark_price

    def _equity(
        self,
        cash: Decimal,
        position: PositionState | None,
        mark_price: Decimal,
    ) -> Decimal:
        return cash + self._position_value(position, mark_price)

    def _portfolio_position_value(
        self,
        positions: dict[str, PositionState],
        last_closes: dict[str, Decimal],
    ) -> Decimal:
        total = Decimal("0")
        for symbol, position in positions.items():
            mark_price = last_closes.get(symbol, position.avg_entry_price)
            total += self._position_value(position, mark_price)
        return total

    def _portfolio_equity(
        self,
        cash: Decimal,
        positions: dict[str, PositionState],
        last_closes: dict[str, Decimal],
    ) -> Decimal:
        return cash + self._portfolio_position_value(positions, last_closes)

    def _total_units(
        self,
        positions: dict[str, PositionState],
        direction: PositionDirection,
    ) -> int:
        return sum(
            len(position.units)
            for position in positions.values()
            if position.direction == direction
        )

    def _max_total_units_reached(
        self,
        positions: dict[str, PositionState],
        direction: PositionDirection,
    ) -> bool:
        max_units = (
            self.config.max_total_short_units
            if direction == PositionDirection.SHORT
            else self.config.max_total_long_units
        )
        if max_units is None:
            return False
        return self._total_units(positions, direction) >= max_units

    @staticmethod
    def _max_total_units_reason(direction: PositionDirection) -> str:
        if direction == PositionDirection.SHORT:
            return "max_total_short_units"
        return "max_total_long_units"
