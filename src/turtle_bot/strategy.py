from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Sequence

from .domain import (
    Candle,
    IndicatorSnapshot,
    PositionState,
    PositionStatus,
    Side,
    Signal,
    SignalKind,
    StrategyState,
    TurtleSystem,
    TradeOutcome,
)
from .indicators import compute_n_atr20, compute_n_turtle, donchian_channel


def build_indicator_snapshot(
    symbol: str,
    candles: Sequence[Candle],
    *,
    n_method: str = "turtle",
    exclude_current: bool = True,
) -> IndicatorSnapshot:
    if not candles:
        now = datetime.now(timezone.utc)
        return IndicatorSnapshot(
            symbol=symbol,
            as_of=now,
            n=None,
            n_method=n_method,
            entry_high_20=None,
            entry_low_20=None,
            entry_high_55=None,
            entry_low_55=None,
            exit_low_10=None,
            exit_low_20=None,
            ready=False,
        )

    history = candles[:-1] if exclude_current else candles
    as_of = candles[-1].timestamp if candles else datetime.now(timezone.utc)

    entry_high_20, entry_low_20 = donchian_channel(
        candles,
        20,
        exclude_current=exclude_current,
    )
    entry_high_55, entry_low_55 = donchian_channel(
        candles,
        55,
        exclude_current=exclude_current,
    )
    _, exit_low_10 = donchian_channel(
        candles,
        10,
        exclude_current=exclude_current,
    )
    _, exit_low_20 = donchian_channel(
        candles,
        20,
        exclude_current=exclude_current,
    )

    if n_method == "atr20":
        n = compute_n_atr20(history)
    else:
        n = compute_n_turtle(history)

    ready = len(history) >= 20
    return IndicatorSnapshot(
        symbol=symbol,
        as_of=as_of,
        n=n,
        n_method=n_method,
        entry_high_20=entry_high_20,
        entry_low_20=entry_low_20,
        entry_high_55=entry_high_55,
        entry_low_55=entry_low_55,
        exit_low_10=exit_low_10,
        exit_low_20=exit_low_20,
        ready=ready,
    )


def apply_trade_outcomes(
    state: StrategyState,
    outcomes: Sequence[TradeOutcome],
) -> StrategyState:
    """Apply completed trade outcomes for strategy state updates."""

    current = state
    for trade in outcomes:
        if trade.system == TurtleSystem.S1 and trade.realized_pnl > 0:
            current = current.with_s1_skip(trade.symbol)
    return current


def _should_take_pyramid(
    position: PositionState,
    current_price: Decimal,
    n: Decimal,
    max_units: int,
    pyramid_step_n: Decimal,
) -> bool:
    if len(position.units) >= max_units:
        return False
    if len(position.units) == 0:
        return False

    last_entry = position.last_unit_entry_price
    if current_price < last_entry + pyramid_step_n * n:
        return False
    return True



def evaluate_signals(
    *,
    symbol: str,
    completed_candles: Sequence[Candle],
    current_price: Decimal,
    state: StrategyState,
    position: PositionState | None = None,
    minimum_tick: Decimal = Decimal("0"),
    n_method: str = "turtle",
    max_units_per_symbol: int = 4,
    pyramid_step_n: Decimal = Decimal("0.5"),
) -> tuple[list[Signal], StrategyState]:
    """Return ordered decision signals for one symbol with exit/entry priority."""

    snapshot = build_indicator_snapshot(
        symbol=symbol,
        candles=completed_candles,
        n_method=n_method,
        exclude_current=False,
    )
    now = datetime.now(timezone.utc)
    next_state = state

    if position is not None and position.status == PositionStatus.OPEN:
        stop_price = position.current_stop_price
        if stop_price is not None and current_price <= stop_price:
            return [
                Signal.new(
                    symbol=symbol,
                    system=position.system,
                    kind=SignalKind.STOP,
                    side=Side.SELL,
                    trigger_price=stop_price,
                    observed_price=current_price,
                    triggered_at=now,
                    reason="risk_stop",
                )
            ], next_state

        exit_level = (
            snapshot.exit_low_10
            if position.system == TurtleSystem.S1
            else snapshot.exit_low_20
        )
        if exit_level is not None and current_price <= exit_level:
            return [
                Signal.new(
                    symbol=symbol,
                    system=position.system,
                    kind=SignalKind.EXIT,
                    side=Side.SELL,
                    trigger_price=exit_level,
                    observed_price=current_price,
                    triggered_at=now,
                    reason="channel_exit",
                )
            ], next_state

        if snapshot.n is not None and _should_take_pyramid(
            position=position,
            current_price=current_price,
            n=snapshot.n,
            max_units=max_units_per_symbol,
            pyramid_step_n=pyramid_step_n,
        ):
            return [
                Signal.new(
                    symbol=symbol,
                    system=position.system,
                    kind=SignalKind.PYRAMID,
                    side=Side.BUY,
                    trigger_price=position.last_unit_entry_price + (pyramid_step_n * snapshot.n),
                    observed_price=current_price,
                    triggered_at=now,
                    reason="pyramid_0.5N",
                )
            ], next_state

        return [], next_state

    if snapshot.n is None:
        return [], next_state

    if snapshot.entry_high_55 is not None and current_price >= snapshot.entry_high_55 + minimum_tick:
        return [
            Signal.new(
                symbol=symbol,
                system=TurtleSystem.S2,
                kind=SignalKind.ENTRY,
                side=Side.BUY,
                trigger_price=snapshot.entry_high_55 + minimum_tick,
                observed_price=current_price,
                triggered_at=now,
                reason="s2_breakout",
            )
        ], next_state

    if snapshot.entry_high_20 is not None and current_price >= snapshot.entry_high_20 + minimum_tick:
        if symbol in next_state.pending_s1_skip:
            next_state = next_state.clear_s1_skip(symbol)
            return [], next_state
        return [
            Signal.new(
                symbol=symbol,
                system=TurtleSystem.S1,
                kind=SignalKind.ENTRY,
                side=Side.BUY,
                trigger_price=snapshot.entry_high_20 + minimum_tick,
                observed_price=current_price,
                triggered_at=now,
                reason="s1_breakout",
            )
        ], next_state

    return [], next_state
