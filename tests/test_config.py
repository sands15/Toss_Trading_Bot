from __future__ import annotations

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
    assert config.momentum_accept_top_n == 2
