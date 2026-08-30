from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

import yaml

from .domain import PositionDirection


@dataclass(frozen=True)
class TossConfig:
    live_enabled: bool = False
    base_url: str | None = None
    account_seq: str | None = None
    client_id_env: str = "TOSS_CLIENT_ID"
    client_secret_env: str = "TOSS_CLIENT_SECRET"
    require_live_consent: bool = False
    allowed_live_consent_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RuntimeConfig:
    mode: str = "paper"
    market: str = "KR"
    timezone_name: str = "Asia/Seoul"
    use_market_calendar: bool = True
    market_calendar_open_sessions: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    state_db: str = "state/turtle.sqlite3"
    log_dir: str = "logs"
    interval_seconds: int = 60
    candle_interval: str = "1d"
    candle_count: int = 100
    exclude_current_session: bool = True
    watchlist_enabled: bool = True
    watchlist_top_n: int = 20
    watchlist_name: str = "premarket"
    universe_enabled: bool = False
    universe_candidate_symbols: tuple[str, ...] = ()
    universe_include_etfs: bool = False
    universe_min_price: Decimal = Decimal("1000")
    universe_min_average_daily_value: Decimal = Decimal("100000000")
    universe_min_completed_candles: int = 56
    pit_universe_csv: str | None = None


@dataclass(frozen=True)
class AiConfig:
    enabled: bool = False
    provider: str = "openai_compatible"
    model: str = "bRadu/gemma-4-E2B-it-textonly"
    base_url: str = "http://localhost:8000/v1"
    api_key_env: str = "TURTLE_AI_API_KEY"
    timeout_seconds: int = 30
    max_tokens: int = 700
    temperature: Decimal = Decimal("0.2")


@dataclass(frozen=True)
class LiveConfig:
    emergency_stop: bool = True
    allowed_symbols: tuple[str, ...] = ()
    max_order_quantity: Decimal | None = Decimal("1")
    max_order_notional: Decimal | None = None
    daily_order_count_limit: int | None = 1
    daily_notional_limit: Decimal | None = None
    require_market_open: bool = True
    require_clean_reconcile: bool = True
    block_unresolved_orders: bool = True
    confirm_high_value_order: bool = False
    cancel_after_ack: bool = False
    max_consecutive_order_failures: int | None = 3


@dataclass(frozen=True)
class IntradayConfig:
    """Explicit inputs for the read-only US premarket plan builder.

    Economic values intentionally have no trading defaults.  A plan is blocked
    until the operator supplies every value in configuration.
    """

    cash_allocation_fraction: Decimal | None = None
    risk_fraction: Decimal | None = None
    take_profit_fraction: Decimal | None = None
    stop_fraction: Decimal | None = None
    stop_limit_buffer_fraction: Decimal | None = None
    max_entry_slippage_fraction: Decimal | None = None
    estimated_round_trip_cost_fraction: Decimal | None = None
    estimated_fixed_round_trip_cost: Decimal | None = None
    minimum_reward_risk_ratio: Decimal | None = None
    max_spread_fraction: Decimal | None = None
    max_last_mid_deviation_fraction: Decimal | None = None
    max_notional: Decimal | None = None
    max_quantity: int = 1
    plan_lead_minutes: int | None = None
    minimum_plan_lead_minutes: int | None = None
    quote_max_age_seconds: int | None = None
    orderbook_max_age_seconds: int | None = None
    max_quote_skew_seconds: int | None = None
    entry_start_minutes_after_open: int | None = None
    entry_expiry_minutes_after_open: int | None = None
    force_exit_minutes_before_close: int | None = None
    regular_session_only: bool = True
    live_execution_enabled: bool = False
    selection_mode: str = "manual"
    selection_rank_max_age_seconds: int | None = None
    selection_min_price: Decimal | None = None
    selection_min_trading_amount: Decimal | None = None
    selection_min_change_fraction: Decimal | None = None
    selection_max_change_fraction: Decimal | None = None
    selection_min_average_daily_value: Decimal | None = None
    selection_max_average_daily_range_fraction: Decimal | None = None
    selection_max_premarket_range_fraction: Decimal | None = None
    news_context_path: str | None = None
    approval_envelope_path: str | None = None
    simulation_enabled: bool = False
    simulation_id: str | None = None
    simulation_start_date: date | None = None
    simulation_end_date: date | None = None
    simulation_initial_cash: Decimal | None = None
    simulation_slippage_fraction: Decimal | None = None
    simulation_db_path: str | None = None


@dataclass(frozen=True)
class TradingConfig:
    toss: TossConfig = field(default_factory=TossConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    ai: AiConfig = field(default_factory=AiConfig)
    live: LiveConfig = field(default_factory=LiveConfig)
    intraday: IntradayConfig = field(default_factory=IntradayConfig)
    strategy_kind: str = "turtle"
    minimum_tick: Decimal = Decimal("1")
    risk_pct_per_unit: Decimal = Decimal("0.005")
    stop_n: Decimal = Decimal("2")
    pyramid_step_n: Decimal = Decimal("0.5")
    max_units_per_symbol: int = 4
    max_total_long_units: int = 12
    max_total_short_units: int = 12
    backtest_allowed_directions: tuple[PositionDirection, ...] = (PositionDirection.LONG,)
    n_method: str = "turtle"
    momentum_market_symbol: str = "SPY"
    momentum_lookback_days: int = 126
    momentum_skip_days: int = 21
    momentum_trend_ma_days: int = 200
    momentum_exit_ma_days: int = 75
    momentum_max_positions: int = 5
    momentum_max_exposure_pct: Decimal = Decimal("0.50")
    momentum_accept_top_n: int = 2
    momentum_target_position_pct: Decimal = Decimal("0.10")
    momentum_min_price: Decimal = Decimal("5")
    momentum_min_average_daily_value: Decimal = Decimal("50000000")
    momentum_average_daily_value_days: int = 20
    momentum_use_market_filter: bool = True

    @property
    def live_enabled(self) -> bool:
        return self.toss.live_enabled

    @property
    def momentum_cash_reserve_pct(self) -> Decimal:
        return Decimal("1") - self.momentum_max_exposure_pct


_INTRADAY_EXPERIMENT_FIELDS = (
    "cash_allocation_fraction",
    "risk_fraction",
    "take_profit_fraction",
    "stop_fraction",
    "stop_limit_buffer_fraction",
    "max_entry_slippage_fraction",
    "estimated_round_trip_cost_fraction",
    "estimated_fixed_round_trip_cost",
    "minimum_reward_risk_ratio",
    "max_spread_fraction",
    "max_last_mid_deviation_fraction",
    "max_notional",
    "max_quantity",
    "plan_lead_minutes",
    "minimum_plan_lead_minutes",
    "quote_max_age_seconds",
    "orderbook_max_age_seconds",
    "max_quote_skew_seconds",
    "entry_start_minutes_after_open",
    "entry_expiry_minutes_after_open",
    "force_exit_minutes_before_close",
    "regular_session_only",
    "news_context_path",
    "live_execution_enabled",
    "selection_mode",
    "selection_rank_max_age_seconds",
    "selection_min_price",
    "selection_min_trading_amount",
    "selection_min_change_fraction",
    "selection_max_change_fraction",
    "selection_min_average_daily_value",
    "selection_max_average_daily_range_fraction",
    "selection_max_premarket_range_fraction",
    "simulation_id",
    "simulation_enabled",
    "simulation_start_date",
    "simulation_end_date",
    "simulation_initial_cash",
    "simulation_slippage_fraction",
)


def intraday_simulation_experiment_hash(config: TradingConfig) -> str:
    """Hash every economic/selection input that must stay fixed for one run."""

    def normalize(value: Any) -> Any:
        if isinstance(value, Decimal):
            return format(value, "f")
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, tuple):
            return [normalize(item) for item in value]
        return value

    payload = {
        "schema_version": 1,
        "strategy_kind": config.strategy_kind,
        "runtime": {
            "market": config.runtime.market,
            "timezone": config.runtime.timezone_name,
            "use_market_calendar": config.runtime.use_market_calendar,
            "symbols": normalize(config.runtime.symbols),
            "interval_seconds": config.runtime.interval_seconds,
        },
        "intraday": {
            name: normalize(getattr(config.intraday, name))
            for name in _INTRADAY_EXPERIMENT_FIELDS
        },
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _to_decimal(value: Any, default: Decimal) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return default


def _to_optional_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("boolean is not a valid decimal configuration value")
    try:
        parsed = Decimal(str(value))
    except Exception as exc:
        raise ValueError("invalid decimal configuration value") from exc
    if not parsed.is_finite():
        raise ValueError("decimal configuration value must be finite")
    return parsed


def _to_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("boolean is not a valid integer configuration value")
    return int(value)


def _to_optional_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        raise ValueError("datetime is not a valid date configuration value")
    if isinstance(value, date):
        return value
    if isinstance(value, bool):
        raise ValueError("boolean is not a valid date configuration value")
    text = str(value).strip()
    if len(text) != 10 or text[4:5] != "-" or text[7:8] != "-":
        raise ValueError("date configuration value must use YYYY-MM-DD")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("date configuration value must use YYYY-MM-DD") from exc


def _to_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError("boolean configuration value must be true or false")
    return value


def _to_int(value: Any, default: int) -> int:
    parsed = _to_optional_int(value)
    return default if parsed is None else parsed


def _momentum_max_exposure_pct(momentum: Mapping[str, Any]) -> Decimal:
    if "cash_reserve_pct" in momentum and momentum.get("cash_reserve_pct") is not None:
        cash_reserve_pct = _to_decimal(
            momentum.get("cash_reserve_pct"),
            Decimal("0.50"),
        )
        return Decimal("1") - cash_reserve_pct
    return _to_decimal(momentum.get("max_exposure_pct"), Decimal("0.50"))


def _to_symbols(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _to_clean_string(value: Any, *, allow_empty: bool = False) -> str | None:
    if value is None:
        return "" if allow_empty else None
    text = str(value).strip()
    if not text and not allow_empty:
        return None
    return text


def _to_optional_path_string(value: Any, *, base_dir: Path) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("path configuration value must be a string")
    text = value.strip()
    if not text:
        return None
    parsed = Path(text).expanduser()
    if not parsed.is_absolute():
        parsed = base_dir / parsed
    return str(parsed.resolve())


def _to_string_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        if "," in value:
            return tuple(
                part.strip()
                for part in value.split(",")
                if part is not None and str(part).strip()
            )
        text = value.strip()
        return (text,) if text else ()
    if isinstance(value, (list, tuple, set)):
        return tuple(
            str(item).strip()
            for item in value
            if str(item).strip()
        )
    if isinstance(value, Mapping):
        return tuple()
    text = str(value).strip()
    return (text,) if text else ()


def _to_directions(value: Any) -> tuple[PositionDirection, ...]:
    if value is None:
        return (PositionDirection.LONG,)
    raw_values: tuple[Any, ...]
    if isinstance(value, str):
        raw_values = tuple(part.strip() for part in value.split(",") if part.strip())
    elif isinstance(value, list):
        raw_values = tuple(value)
    else:
        raw_values = (value,)

    directions: list[PositionDirection] = []
    for item in raw_values:
        clean = str(item).strip().upper()
        if clean == "BOTH":
            directions.extend([PositionDirection.LONG, PositionDirection.SHORT])
            continue
        direction = PositionDirection(clean)
        if direction not in directions:
            directions.append(direction)
    return tuple(directions) or (PositionDirection.LONG,)


def load_config(path: str | Path | None = None) -> TradingConfig:
    if path is None:
        return TradingConfig()

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"config file not found: {p}")

    with p.open("r", encoding="utf-8") as file:
        raw = yaml.safe_load(file) or {}

    strategy = raw.get("strategy", {}) or {}
    momentum = strategy.get("momentum", {}) or {}
    intraday = strategy.get("intraday", {}) or {}
    selection = intraday.get("selection", {}) or {}
    simulation = intraday.get("simulation", {}) or {}
    if not isinstance(selection, Mapping):
        raise ValueError("strategy.intraday.selection must be an object")
    if not isinstance(simulation, Mapping):
        raise ValueError("strategy.intraday.simulation must be an object")
    risk = strategy.get("risk", {}) or {}
    toss = raw.get("toss", {}) or {}
    runtime = raw.get("runtime", {}) or {}
    ai = raw.get("ai", {}) or {}
    live = raw.get("live", {}) or {}
    config = TradingConfig(
        toss=TossConfig(
            live_enabled=bool(toss.get("live_enabled", False)),
            base_url=toss.get("base_url"),
            account_seq=_to_clean_string(toss.get("account_seq"), allow_empty=False),
            client_id_env=_to_clean_string(
                toss.get("client_id_env", "TOSS_CLIENT_ID"),
                allow_empty=False,
            )
            or "TOSS_CLIENT_ID",
            client_secret_env=_to_clean_string(
                toss.get("client_secret_env", "TOSS_CLIENT_SECRET"),
                allow_empty=False,
            )
            or "TOSS_CLIENT_SECRET",
            require_live_consent=bool(toss.get("require_live_consent", False)),
            allowed_live_consent_ids=_to_string_list(
                toss.get("allowed_live_consent_ids")
                or toss.get("consent_ids")
                or toss.get("allowed_consent_ids")
            ),
        ),
        runtime=RuntimeConfig(
            mode=str(runtime.get("mode", "paper")),
            market=str(runtime.get("market", "KR")),
            timezone_name=str(runtime.get("timezone", "Asia/Seoul")),
            use_market_calendar=bool(runtime.get("use_market_calendar", True)),
            market_calendar_open_sessions=_to_string_list(
                runtime.get("market_calendar_open_sessions")
                or runtime.get("open_sessions")
                or runtime.get("tradable_sessions")
            ),
            symbols=_to_symbols(runtime.get("symbols")),
            state_db=str(runtime.get("state_db", "state/turtle.sqlite3")),
            log_dir=str(runtime.get("log_dir", "logs")),
            interval_seconds=int(runtime.get("interval_seconds", 60)),
            candle_interval=str(runtime.get("candle_interval", "1d")),
            candle_count=int(runtime.get("candle_count", 100)),
            exclude_current_session=bool(
                runtime.get("exclude_current_session", True)
            ),
            watchlist_enabled=bool(runtime.get("watchlist_enabled", True)),
            watchlist_top_n=int(runtime.get("watchlist_top_n", 20)),
            watchlist_name=str(runtime.get("watchlist_name", "premarket")),
            universe_enabled=bool(runtime.get("universe_enabled", False)),
            universe_candidate_symbols=_to_symbols(
                runtime.get("universe_candidate_symbols")
            ),
            universe_include_etfs=bool(runtime.get("universe_include_etfs", False)),
            universe_min_price=_to_decimal(
                runtime.get("universe_min_price"),
                Decimal("1000"),
            ),
            universe_min_average_daily_value=_to_decimal(
                runtime.get("universe_min_average_daily_value"),
                Decimal("100000000"),
            ),
            universe_min_completed_candles=int(
                runtime.get("universe_min_completed_candles", 56)
            ),
            pit_universe_csv=_to_clean_string(
                runtime.get("pit_universe_csv"),
                allow_empty=False,
            ),
        ),
        ai=AiConfig(
            enabled=bool(ai.get("enabled", False)),
            provider=str(ai.get("provider", "openai_compatible")),
            model=str(ai.get("model", "bRadu/gemma-4-E2B-it-textonly")),
            base_url=str(ai.get("base_url", "http://localhost:8000/v1")).rstrip("/"),
            api_key_env=str(ai.get("api_key_env", "TURTLE_AI_API_KEY")),
            timeout_seconds=int(ai.get("timeout_seconds", 30)),
            max_tokens=int(ai.get("max_tokens", 700)),
            temperature=_to_decimal(ai.get("temperature"), Decimal("0.2")),
        ),
        live=LiveConfig(
            emergency_stop=bool(live.get("emergency_stop", True)),
            allowed_symbols=_to_symbols(live.get("allowed_symbols")),
            max_order_quantity=_to_optional_decimal(
                live.get("max_order_quantity", Decimal("1"))
            ),
            max_order_notional=_to_optional_decimal(live.get("max_order_notional")),
            daily_order_count_limit=_to_optional_int(
                live.get("daily_order_count_limit", 1)
            ),
            daily_notional_limit=_to_optional_decimal(live.get("daily_notional_limit")),
            require_market_open=bool(live.get("require_market_open", True)),
            require_clean_reconcile=bool(live.get("require_clean_reconcile", True)),
            block_unresolved_orders=bool(live.get("block_unresolved_orders", True)),
            confirm_high_value_order=bool(live.get("confirm_high_value_order", False)),
            cancel_after_ack=bool(live.get("cancel_after_ack", False)),
            max_consecutive_order_failures=_to_optional_int(
                live.get("max_consecutive_order_failures", 3)
            ),
        ),
        intraday=IntradayConfig(
            cash_allocation_fraction=_to_optional_decimal(
                intraday.get("cash_allocation_fraction")
            ),
            risk_fraction=_to_optional_decimal(intraday.get("risk_fraction")),
            take_profit_fraction=_to_optional_decimal(
                intraday.get("take_profit_fraction")
            ),
            stop_fraction=_to_optional_decimal(intraday.get("stop_fraction")),
            stop_limit_buffer_fraction=_to_optional_decimal(
                intraday.get("stop_limit_buffer_fraction")
            ),
            max_entry_slippage_fraction=_to_optional_decimal(
                intraday.get("max_entry_slippage_fraction")
            ),
            estimated_round_trip_cost_fraction=_to_optional_decimal(
                intraday.get("estimated_round_trip_cost_fraction")
            ),
            estimated_fixed_round_trip_cost=_to_optional_decimal(
                intraday.get("estimated_fixed_round_trip_cost")
            ),
            minimum_reward_risk_ratio=_to_optional_decimal(
                intraday.get("minimum_reward_risk_ratio")
            ),
            max_spread_fraction=_to_optional_decimal(
                intraday.get("max_spread_fraction")
            ),
            max_last_mid_deviation_fraction=_to_optional_decimal(
                intraday.get("max_last_mid_deviation_fraction")
            ),
            max_notional=_to_optional_decimal(intraday.get("max_notional")),
            max_quantity=_to_int(intraday.get("max_quantity", 1), 1),
            plan_lead_minutes=_to_optional_int(
                intraday.get("plan_lead_minutes")
            ),
            minimum_plan_lead_minutes=_to_optional_int(
                intraday.get("minimum_plan_lead_minutes")
            ),
            quote_max_age_seconds=_to_optional_int(
                intraday.get("quote_max_age_seconds")
            ),
            orderbook_max_age_seconds=_to_optional_int(
                intraday.get("orderbook_max_age_seconds")
            ),
            max_quote_skew_seconds=_to_optional_int(
                intraday.get("max_quote_skew_seconds")
            ),
            entry_start_minutes_after_open=_to_optional_int(
                intraday.get("entry_start_minutes_after_open")
            ),
            entry_expiry_minutes_after_open=_to_optional_int(
                intraday.get("entry_expiry_minutes_after_open")
            ),
            force_exit_minutes_before_close=_to_optional_int(
                intraday.get("force_exit_minutes_before_close")
            ),
            regular_session_only=bool(
                intraday.get("regular_session_only", True)
            ),
            live_execution_enabled=bool(
                intraday.get("live_execution_enabled", False)
            ),
            selection_mode=str(selection.get("mode", "manual")).strip().lower(),
            selection_rank_max_age_seconds=_to_optional_int(
                selection.get("rank_max_age_seconds")
            ),
            selection_min_price=_to_optional_decimal(selection.get("min_price")),
            selection_min_trading_amount=_to_optional_decimal(
                selection.get("min_trading_amount")
            ),
            selection_min_change_fraction=_to_optional_decimal(
                selection.get("min_change_fraction")
            ),
            selection_max_change_fraction=_to_optional_decimal(
                selection.get("max_change_fraction")
            ),
            selection_min_average_daily_value=_to_optional_decimal(
                selection.get("min_average_daily_value")
            ),
            selection_max_average_daily_range_fraction=_to_optional_decimal(
                selection.get("max_average_daily_range_fraction")
            ),
            selection_max_premarket_range_fraction=_to_optional_decimal(
                selection.get("max_premarket_range_fraction")
            ),
            news_context_path=_to_optional_path_string(
                intraday.get("news_context_path"),
                base_dir=p.parent.resolve(),
            ),
            approval_envelope_path=_to_optional_path_string(
                intraday.get("approval_envelope_path"),
                base_dir=p.parent.resolve(),
            ),
            simulation_enabled=_to_bool(simulation.get("enabled"), default=False),
            simulation_id=_to_clean_string(
                simulation.get("id"),
                allow_empty=False,
            ),
            simulation_start_date=_to_optional_date(
                simulation.get("start_date")
            ),
            simulation_end_date=_to_optional_date(
                simulation.get("end_date")
            ),
            simulation_initial_cash=_to_optional_decimal(
                simulation.get("initial_cash")
            ),
            simulation_slippage_fraction=_to_optional_decimal(
                simulation.get("slippage_fraction")
            ),
            simulation_db_path=_to_optional_path_string(
                simulation.get("db_path"),
                base_dir=p.parent.resolve(),
            ),
        ),
        minimum_tick=_to_decimal(strategy.get("minimum_tick"), Decimal("1")),
        strategy_kind=str(strategy.get("kind", "turtle")).strip().lower(),
        risk_pct_per_unit=_to_decimal(risk.get("risk_pct_per_unit"), Decimal("0.005")),
        stop_n=_to_decimal(risk.get("stop_n"), Decimal("2")),
        pyramid_step_n=_to_decimal(risk.get("pyramid_step_n"), Decimal("0.5")),
        max_units_per_symbol=int(risk.get("max_units_per_symbol", 4)),
        max_total_long_units=int(risk.get("max_total_long_units", 12)),
        max_total_short_units=int(risk.get("max_total_short_units", 12)),
        backtest_allowed_directions=_to_directions(
            strategy.get("backtest_allowed_directions")
        ),
        n_method=strategy.get("n_method", "turtle"),
        momentum_market_symbol=str(
            momentum.get("market_symbol", "SPY")
        ),
        momentum_lookback_days=int(
            momentum.get("lookback_days", 126)
        ),
        momentum_skip_days=int(
            momentum.get("skip_days", 21)
        ),
        momentum_trend_ma_days=int(
            momentum.get("trend_ma_days", 200)
        ),
        momentum_exit_ma_days=int(
            momentum.get("exit_ma_days", 75)
        ),
        momentum_max_positions=int(
            momentum.get("max_positions", 5)
        ),
        momentum_max_exposure_pct=_momentum_max_exposure_pct(momentum),
        momentum_accept_top_n=int(
            momentum.get("accept_top_n", 2)
        ),
        momentum_target_position_pct=_to_decimal(
            momentum.get("target_position_pct"),
            Decimal("0.10"),
        ),
        momentum_min_price=_to_decimal(
            momentum.get("min_price"),
            Decimal("5"),
        ),
        momentum_min_average_daily_value=_to_decimal(
            momentum.get("min_average_daily_value"),
            Decimal("50000000"),
        ),
        momentum_average_daily_value_days=int(
            momentum.get("average_daily_value_days", 20)
        ),
        momentum_use_market_filter=bool(
            momentum.get("use_market_filter", True)
        ),
    )
    if config.intraday.simulation_enabled:
        context_path = Path(str(config.intraday.news_context_path or ""))
        if not context_path.is_absolute() or context_path.name != "news-context.json":
            raise ValueError(
                "strategy.intraday.news_context_path must be an absolute news-context.json path when simulation is enabled"
            )
    return config
