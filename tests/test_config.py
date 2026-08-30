from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from turtle_bot.config import intraday_simulation_experiment_hash, load_config
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


def test_load_config_parses_explicit_intraday_shadow_inputs(tmp_path):
    config_path = tmp_path / "intraday.yaml"
    config_path.write_text(
        """runtime:
  mode: shadow
  market: US
  timezone: America/New_York
  watchlist_enabled: false
  symbols: [AAPL]
strategy:
  kind: intraday
  intraday:
    cash_allocation_fraction: 0.25
    risk_fraction: 0.005
    take_profit_fraction: 0.02
    stop_fraction: 0.01
    stop_limit_buffer_fraction: 0.001
    max_entry_slippage_fraction: 0.001
    estimated_round_trip_cost_fraction: 0.0021
    estimated_fixed_round_trip_cost: 0.01
    minimum_reward_risk_ratio: 1.5
    max_spread_fraction: 0.003
    max_last_mid_deviation_fraction: 0.005
    max_notional: 1000
    max_quantity: 1
    plan_lead_minutes: 90
    minimum_plan_lead_minutes: 15
    quote_max_age_seconds: 15
    orderbook_max_age_seconds: 15
    max_quote_skew_seconds: 2
    entry_start_minutes_after_open: 5
    entry_expiry_minutes_after_open: 60
    force_exit_minutes_before_close: 15
    regular_session_only: true
    live_execution_enabled: false
    news_context_path: state/news-context.json
    approval_envelope_path: state/approval-envelope.json
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.strategy_kind == "intraday"
    assert config.intraday.cash_allocation_fraction == Decimal("0.25")
    assert config.intraday.estimated_round_trip_cost_fraction == Decimal("0.0021")
    assert config.intraday.estimated_fixed_round_trip_cost == Decimal("0.01")
    assert config.intraday.minimum_reward_risk_ratio == Decimal("1.5")
    assert config.intraday.max_notional == Decimal("1000")
    assert config.intraday.plan_lead_minutes == 90
    assert config.intraday.live_execution_enabled is False
    assert config.intraday.news_context_path == str(
        (tmp_path / "state" / "news-context.json").resolve()
    )
    assert config.intraday.approval_envelope_path == str(
        (tmp_path / "state" / "approval-envelope.json").resolve()
    )


def test_load_config_parses_automatic_intraday_selection_policy(tmp_path):
    config_path = tmp_path / "automatic.yaml"
    config_path.write_text(
        """strategy:
  kind: intraday
  intraday:
    selection:
      mode: automatic
      rank_max_age_seconds: 120
      min_price: 5
      min_trading_amount: 1000000
      min_change_fraction: 0.005
      max_change_fraction: 0.08
      min_average_daily_value: 50000000
      max_average_daily_range_fraction: 0.08
      max_premarket_range_fraction: 0.05
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.intraday.selection_mode == "automatic"
    assert config.intraday.selection_rank_max_age_seconds == 120
    assert config.intraday.selection_min_price == Decimal("5")
    assert config.intraday.selection_min_trading_amount == Decimal("1000000")
    assert config.intraday.selection_min_change_fraction == Decimal("0.005")
    assert config.intraday.selection_max_change_fraction == Decimal("0.08")
    assert config.intraday.selection_min_average_daily_value == Decimal("50000000")
    assert config.intraday.selection_max_average_daily_range_fraction == Decimal("0.08")
    assert config.intraday.selection_max_premarket_range_fraction == Decimal("0.05")


def test_load_config_parses_intraday_simulation_window_and_ledger(tmp_path):
    config_path = tmp_path / "simulation.yaml"
    config_path.write_text(
        """strategy:
  kind: intraday
  intraday:
    news_context_path: news-context.json
    simulation:
      enabled: true
      id: september-forward-test
      start_date: 2026-08-31
      end_date: 2026-09-30
      initial_cash: 10000
      slippage_fraction: 0.0005
      db_path: state/intraday-paper.sqlite3
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.intraday.simulation_enabled is True
    assert config.intraday.simulation_id == "september-forward-test"
    assert config.intraday.simulation_start_date == date(2026, 8, 31)
    assert config.intraday.simulation_end_date == date(2026, 9, 30)
    assert config.intraday.simulation_initial_cash == Decimal("10000")
    assert config.intraday.simulation_slippage_fraction == Decimal("0.0005")
    assert config.intraday.simulation_db_path == str(
        (tmp_path / "state" / "intraday-paper.sqlite3").resolve()
    )


@pytest.mark.parametrize("value", ["true", "2026-02-30", "20260831"])
def test_intraday_simulation_date_rejects_invalid_values(tmp_path, value):
    config_path = tmp_path / "invalid.yaml"
    config_path.write_text(
        "strategy:\n  kind: intraday\n  intraday:\n    simulation:\n"
        f"      start_date: {value}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_config(config_path)


@pytest.mark.parametrize("value", ["true", "123", "[news-context.json]"])
def test_intraday_news_context_path_rejects_non_string_values(tmp_path, value):
    config_path = tmp_path / "invalid.yaml"
    config_path.write_text(
        "strategy:\n  kind: intraday\n  intraday:\n"
        f"    news_context_path: {value}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="path configuration value"):
        load_config(config_path)


@pytest.mark.parametrize("value", ["true", "123", "[approval-envelope.json]"])
def test_intraday_approval_envelope_path_rejects_non_string_values(tmp_path, value):
    config_path = tmp_path / "invalid.yaml"
    config_path.write_text(
        "strategy:\n  kind: intraday\n  intraday:\n"
        f"    approval_envelope_path: {value}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="path configuration value"):
        load_config(config_path)


def test_intraday_integer_fields_reject_yaml_booleans(tmp_path):
    config_path = tmp_path / "invalid.yaml"
    config_path.write_text(
        "strategy:\n  kind: intraday\n  intraday:\n    max_quantity: true\n",
        encoding="utf-8",
    )

    try:
        load_config(config_path)
    except ValueError as exc:
        assert "boolean" in str(exc)
    else:
        raise AssertionError("boolean max_quantity was accepted as integer 1")


@pytest.mark.parametrize(
    "field",
    [
        "estimated_fixed_round_trip_cost",
        "stop_limit_buffer_fraction",
        "max_entry_slippage_fraction",
    ],
)
@pytest.mark.parametrize("value", ["typo", "true"])
def test_intraday_optional_decimals_reject_typos_and_booleans(tmp_path, field, value):
    config_path = tmp_path / "invalid.yaml"
    config_path.write_text(
        f"strategy:\n  kind: intraday\n  intraday:\n    {field}: {value}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_config(config_path)


def test_intraday_optional_decimal_rejects_nan(tmp_path):
    config_path = tmp_path / "invalid.yaml"
    config_path.write_text(
        "strategy:\n  kind: intraday\n  intraday:\n"
        "    max_entry_slippage_fraction: .nan\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="finite"):
        load_config(config_path)


def test_intraday_optional_decimals_keep_null_and_empty_as_none(tmp_path):
    config_path = tmp_path / "optional.yaml"
    config_path.write_text(
        "strategy:\n  kind: intraday\n  intraday:\n"
        "    estimated_fixed_round_trip_cost: null\n"
        '    stop_limit_buffer_fraction: ""\n',
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.intraday.estimated_fixed_round_trip_cost is None
    assert config.intraday.stop_limit_buffer_fraction is None


def test_intraday_simulation_experiment_hash_is_stable_and_covers_strategy(tmp_path):
    source = Path(__file__).resolve().parents[1] / "config" / "intraday-simulation.example.yaml"
    first_path = tmp_path / "first.yaml"
    changed_path = tmp_path / "changed.yaml"
    context_changed_path = tmp_path / "context-changed.yaml"
    text = source.read_text(encoding="utf-8")
    first_path.write_text(text, encoding="utf-8")
    changed_path.write_text(
        text.replace("risk_fraction: 0.00225", "risk_fraction: 0.00220"),
        encoding="utf-8",
    )
    context_changed_path.write_text(
        text.replace(
            "news_context_path: ../state/news-context.json",
            "news_context_path: ../other/news-context.json",
        ),
        encoding="utf-8",
    )

    first = load_config(first_path)
    same = load_config(first_path)
    changed = load_config(changed_path)
    context_changed = load_config(context_changed_path)

    assert intraday_simulation_experiment_hash(first) == intraday_simulation_experiment_hash(same)
    assert intraday_simulation_experiment_hash(first) != intraday_simulation_experiment_hash(changed)
    assert intraday_simulation_experiment_hash(first) != intraday_simulation_experiment_hash(
        context_changed
    )
