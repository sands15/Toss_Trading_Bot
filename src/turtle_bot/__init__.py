"""Turtle bot core package."""

from .domain import (
    Candle,
    IndicatorSnapshot,
    Signal,
    SignalKind,
    Side,
    PositionStatus,
    StrategyState,
    TradeOutcome,
    TurtleSystem,
    UnitState,
    PositionState,
)
from .state_store import SQLiteStateStore

__all__ = [
    "Candle",
    "IndicatorSnapshot",
    "Signal",
    "SignalKind",
    "Side",
    "PositionStatus",
    "StrategyState",
    "TradeOutcome",
    "TurtleSystem",
    "UnitState",
    "Unit",
    "SQLiteStateStore",
    "PositionState",
]

Unit = UnitState

__version__ = "0.1.0"
