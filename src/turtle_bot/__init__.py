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
from .backtest import (
    AuditEvent,
    BacktestConfig,
    BacktestCosts,
    BacktestEngine,
    BacktestResult,
    BacktestTrade,
    EquityPoint,
    load_candles_csv,
)

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
    "AuditEvent",
    "BacktestConfig",
    "BacktestCosts",
    "BacktestEngine",
    "BacktestResult",
    "BacktestTrade",
    "EquityPoint",
    "load_candles_csv",
]

Unit = UnitState

__version__ = "0.1.0"
