"""Turtle bot core package."""

from .domain import (
    Candle,
    IndicatorSnapshot,
    Signal,
    SignalKind,
    Side,
    StrategyState,
    TradeOutcome,
    TurtleSystem,
    UnitState,
    PositionState,
)

__all__ = [
    "Candle",
    "IndicatorSnapshot",
    "Signal",
    "SignalKind",
    "Side",
    "StrategyState",
    "TradeOutcome",
    "TurtleSystem",
    "UnitState",
    "Unit",
    "PositionState",
]

Unit = UnitState

__version__ = "0.1.0"
