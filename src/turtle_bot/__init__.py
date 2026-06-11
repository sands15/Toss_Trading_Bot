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
    backtest_result_to_dict,
    export_backtest_report_json,
    load_candles_csv,
)
from .toss_client import (
    ACCOUNT_HEADER,
    CandlePage,
    TossApiError,
    TossClient,
    TossCredentials,
    TossHttpResponse,
    TossToken,
)
from .position_sync import (
    BrokerHolding,
    BrokerOrder,
    ReconcileIssue,
    ReconcileResult,
    TossPositionSync,
    normalize_holdings,
    normalize_orders,
    reconcile_positions,
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
    "backtest_result_to_dict",
    "export_backtest_report_json",
    "load_candles_csv",
    "ACCOUNT_HEADER",
    "CandlePage",
    "TossApiError",
    "TossClient",
    "TossCredentials",
    "TossHttpResponse",
    "TossToken",
    "BrokerHolding",
    "BrokerOrder",
    "ReconcileIssue",
    "ReconcileResult",
    "TossPositionSync",
    "normalize_holdings",
    "normalize_orders",
    "reconcile_positions",
]

Unit = UnitState

__version__ = "0.1.0"
