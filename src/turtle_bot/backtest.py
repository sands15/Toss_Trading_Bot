from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from typing import Any, Iterable, Sequence

from .domain import (
    Candle,
    PositionState,
    PositionStatus,
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
    pyramid_step_n: Decimal = Decimal("0.5")
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


@dataclass(frozen=True)
class BacktestResult:
    final_equity: Decimal
    trades: tuple[BacktestTrade, ...]
    equity_curve: tuple[EquityPoint, ...]
    audit_log: tuple[AuditEvent, ...]
    strategy_state: StrategyState


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
        "trades": [_dataclass_to_dict(trade) for trade in result.trades],
        "equity_curve": [_dataclass_to_dict(point) for point in result.equity_curve],
        "audit_log": [_dataclass_to_dict(event) for event in result.audit_log],
        "strategy_state": {
            "pending_s1_skip": sorted(result.strategy_state.pending_s1_skip),
        },
    }


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
                    current_price=candle.low,
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
                    fill_price = self._sell_fill_price(candle, exit_signal.trigger_price)
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
                            action="fill_sell",
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
                        current_price=candle.high,
                        state=state,
                        position=position,
                        minimum_tick=self.config.minimum_tick,
                        n_method=self.config.n_method,
                        max_units_per_symbol=self.config.max_units_per_symbol,
                        pyramid_step_n=self.config.pyramid_step_n,
                    )
                    pyramid_signal = self._first_signal(pyramid_signals, {SignalKind.PYRAMID})
                    if pyramid_signal is not None:
                        fill_price = self._buy_fill_price(candle, pyramid_signal.trigger_price)
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
                                    open_cost.total_cost += -cash_delta
                                audit_log.append(
                                    self._audit(
                                        candle=candle,
                                        signal=pyramid_signal,
                                        action="fill_buy",
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
                    entry_signals, state = evaluate_signals(
                        symbol=symbol,
                        completed_candles=history,
                        current_price=candle.high,
                        state=state,
                        position=None,
                        minimum_tick=self.config.minimum_tick,
                        n_method=self.config.n_method,
                        max_units_per_symbol=self.config.max_units_per_symbol,
                        pyramid_step_n=self.config.pyramid_step_n,
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
                            fill_price = self._buy_fill_price(candle, entry_signal.trigger_price)
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
                                        total_cost=-cash_delta,
                                    )
                                    audit_log.append(
                                        self._audit(
                                            candle=candle,
                                            signal=entry_signal,
                                            action="fill_buy",
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
        )

    def run_portfolio(self, candles: Sequence[Candle]) -> BacktestResult:
        ordered = sorted(candles, key=lambda candle: (candle.timestamp, candle.symbol))
        if not ordered:
            return BacktestResult(
                final_equity=self.config.initial_equity,
                trades=(),
                equity_curve=(),
                audit_log=(),
                strategy_state=StrategyState(),
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
                    current_price=candle.low,
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

                fill_price = self._sell_fill_price(candle, exit_signal.trigger_price)
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
                        action="fill_sell",
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
                        current_price=candle.high,
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

                    fill_price = self._buy_fill_price(candle, pyramid_signal.trigger_price)
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
                            open_costs[symbol].total_cost += -cash_delta
                        audit_log.append(
                            self._portfolio_audit(
                                candle=candle,
                                signal=pyramid_signal,
                                action="fill_buy",
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
                    entry_signals, state = evaluate_signals(
                        symbol=symbol,
                        completed_candles=histories[symbol],
                        current_price=candle.high,
                        state=state,
                        position=None,
                        minimum_tick=self.config.minimum_tick,
                        n_method=self.config.n_method,
                        max_units_per_symbol=self.config.max_units_per_symbol,
                        pyramid_step_n=self.config.pyramid_step_n,
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

                    fill_price = self._buy_fill_price(candle, entry_signal.trigger_price)
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
                            total_cost=-cash_delta,
                        )
                        audit_log.append(
                            self._portfolio_audit(
                                candle=candle,
                                signal=entry_signal,
                                action="fill_buy",
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

    def _buy_cash_delta(self, fill_price: Decimal, qty: Decimal) -> Decimal:
        gross = fill_price * qty
        fee = gross * self.config.costs.commission_rate + self.config.costs.fixed_commission
        return -(gross + fee)

    def _sell_cash_delta(self, fill_price: Decimal, qty: Decimal) -> Decimal:
        gross = fill_price * qty
        fee = gross * self.config.costs.commission_rate + self.config.costs.fixed_commission
        tax = gross * self.config.costs.tax_rate
        return gross - fee - tax

    def _open_position(
        self,
        signal: Signal,
        fill_price: Decimal,
        n: Decimal,
        qty: Decimal | None = None,
    ) -> tuple[PositionState, Decimal]:
        unit_qty = qty if qty is not None else self.config.unit_qty
        stop_price = fill_price - self.config.stop_n * n
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
        )
        return position, self._buy_cash_delta(fill_price, unit_qty)

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
        stop_price = fill_price - self.config.stop_n * position.entry_n
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
            ),
            self._buy_cash_delta(fill_price, unit_qty),
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
        cash_delta = self._sell_cash_delta(fill_price, position.total_qty)
        next_cash = cash + cash_delta
        total_cost = (
            open_cost.total_cost
            if open_cost is not None
            else position.avg_entry_price * position.total_qty
        )
        realized_pnl = cash_delta - total_cost
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
            and candle.open >= signal.trigger_price
        ):
            return f"gap_breakout:{signal.reason}"
        if (
            signal.kind in {SignalKind.STOP, SignalKind.EXIT}
            and candle.open <= signal.trigger_price
        ):
            return f"gap_exit:{signal.reason}"
        return signal.reason

    @staticmethod
    def _position_value(position: PositionState | None, mark_price: Decimal) -> Decimal:
        if position is None:
            return Decimal("0")
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
