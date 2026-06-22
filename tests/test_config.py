from __future__ import annotations

from decimal import Decimal

from turtle_bot.config import load_config
from turtle_bot.domain import PositionDirection


def test_load_config_parses_backtest_short_and_pit_controls(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "runtime:",
                "  pit_universe_csv: data/pit.csv",
                "strategy:",
                "  backtest_allowed_directions: both",
                "  risk:",
                "    max_total_long_units: 8",
                "    max_total_short_units: 6",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.runtime.pit_universe_csv == "data/pit.csv"
    assert config.backtest_allowed_directions == (
        PositionDirection.LONG,
        PositionDirection.SHORT,
    )
    assert config.max_total_long_units == 8
    assert config.max_total_short_units == 6


def test_load_config_uses_selected_momentum_defaults(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("strategy:\n  kind: momentum\n", encoding="utf-8")

    config = load_config(config_path)

    assert config.momentum_lookback_days == 126
    assert config.momentum_skip_days == 21
    assert config.momentum_trend_ma_days == 200
    assert config.momentum_exit_ma_days == 75
    assert config.momentum_max_positions == 5
    assert config.momentum_max_exposure_pct == Decimal("0.50")
    assert config.momentum_cash_reserve_pct == Decimal("0.50")
    assert config.momentum_accept_top_n == 2


def test_load_config_accepts_momentum_cash_reserve_pct(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "strategy:",
                "  kind: momentum",
                "  momentum:",
                "    cash_reserve_pct: 0.30",
                "    max_exposure_pct: 0.50",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.momentum_cash_reserve_pct == Decimal("0.30")
    assert config.momentum_max_exposure_pct == Decimal("0.70")


def test_load_config_parses_live_safety_controls(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "toss:",
                "  live_enabled: true",
                "runtime:",
                "  mode: live",
                "live:",
                "  emergency_stop: false",
                "  allowed_symbols:",
                "    - AAPL",
                "  max_order_quantity: 2",
                "  max_order_notional: 300",
                "  daily_order_count_limit: 3",
                "  daily_notional_limit: 600",
                "  require_market_open: true",
                "  require_clean_reconcile: true",
                "  block_unresolved_orders: true",
                "  confirm_high_value_order: true",
                "  cancel_after_ack: true",
                "  max_consecutive_order_failures: 4",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.live.emergency_stop is False
    assert config.live.allowed_symbols == ("AAPL",)
    assert config.live.max_order_quantity == Decimal("2")
    assert config.live.max_order_notional == Decimal("300")
    assert config.live.daily_order_count_limit == 3
    assert config.live.daily_notional_limit == Decimal("600")
    assert config.live.confirm_high_value_order is True
    assert config.live.cancel_after_ack is True
    assert config.live.max_consecutive_order_failures == 4
