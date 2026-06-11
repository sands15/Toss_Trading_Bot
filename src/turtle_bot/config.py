from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class TradingConfig:
    live_enabled: bool = False
    minimum_tick: Decimal = Decimal("1")
    risk_pct_per_unit: Decimal = Decimal("0.005")
    stop_n: Decimal = Decimal("2")
    pyramid_step_n: Decimal = Decimal("0.5")
    max_units_per_symbol: int = 4
    max_total_long_units: int = 12
    n_method: str = "turtle"


def _to_decimal(value: Any, default: Decimal) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return default


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
    return TradingConfig(
        live_enabled=bool(raw.get("toss", {}).get("live_enabled", False)),
        minimum_tick=_to_decimal(strategy.get("minimum_tick"), Decimal("1")),
        risk_pct_per_unit=_to_decimal(risk.get("risk_pct_per_unit"), Decimal("0.005")),
        stop_n=_to_decimal(risk.get("stop_n"), Decimal("2")),
        pyramid_step_n=_to_decimal(risk.get("pyramid_step_n"), Decimal("0.5")),
        max_units_per_symbol=int(risk.get("max_units_per_symbol", 4)),
        max_total_long_units=int(risk.get("max_total_long_units", 12)),
        n_method=strategy.get("n_method", "turtle"),
    )
