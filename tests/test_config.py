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
