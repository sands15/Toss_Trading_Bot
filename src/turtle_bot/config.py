from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class TossConfig:
    live_enabled: bool = False
    base_url: str | None = None
    account_seq: str | None = None
    client_id_env: str = "TOSS_CLIENT_ID"
    client_secret_env: str = "TOSS_CLIENT_SECRET"


@dataclass(frozen=True)
class RuntimeConfig:
    mode: str = "paper"
    market: str = "KR"
    timezone_name: str = "Asia/Seoul"
    use_market_calendar: bool = True
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


@dataclass(frozen=True)
class TradingConfig:
    toss: TossConfig = field(default_factory=TossConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    minimum_tick: Decimal = Decimal("1")
    risk_pct_per_unit: Decimal = Decimal("0.005")
    stop_n: Decimal = Decimal("2")
    pyramid_step_n: Decimal = Decimal("0.5")
    max_units_per_symbol: int = 4
    max_total_long_units: int = 12
    n_method: str = "turtle"

    @property
    def live_enabled(self) -> bool:
        return self.toss.live_enabled


def _to_decimal(value: Any, default: Decimal) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return default


def _to_symbols(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def load_config(path: str | Path | None = None) -> TradingConfig:
    if path is None:
        return TradingConfig()

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"config file not found: {p}")

    with p.open("r", encoding="utf-8") as file:
        raw = yaml.safe_load(file) or {}

    strategy = raw.get("strategy", {}) or {}
    risk = strategy.get("risk", {}) or {}
    toss = raw.get("toss", {}) or {}
    runtime = raw.get("runtime", {}) or {}
    return TradingConfig(
        toss=TossConfig(
            live_enabled=bool(toss.get("live_enabled", False)),
            base_url=toss.get("base_url"),
            account_seq=(
                str(toss["account_seq"])
                if toss.get("account_seq") is not None
                else None
            ),
            client_id_env=str(toss.get("client_id_env", "TOSS_CLIENT_ID")),
            client_secret_env=str(
                toss.get("client_secret_env", "TOSS_CLIENT_SECRET")
            ),
        ),
        runtime=RuntimeConfig(
            mode=str(runtime.get("mode", "paper")),
            market=str(runtime.get("market", "KR")),
            timezone_name=str(runtime.get("timezone", "Asia/Seoul")),
            use_market_calendar=bool(runtime.get("use_market_calendar", True)),
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
        ),
        minimum_tick=_to_decimal(strategy.get("minimum_tick"), Decimal("1")),
        risk_pct_per_unit=_to_decimal(risk.get("risk_pct_per_unit"), Decimal("0.005")),
        stop_n=_to_decimal(risk.get("stop_n"), Decimal("2")),
        pyramid_step_n=_to_decimal(risk.get("pyramid_step_n"), Decimal("0.5")),
        max_units_per_symbol=int(risk.get("max_units_per_symbol", 4)),
        max_total_long_units=int(risk.get("max_total_long_units", 12)),
        n_method=strategy.get("n_method", "turtle"),
    )
