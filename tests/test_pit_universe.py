from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from turtle_bot.pit_universe import (
    PitUniverseCoverageError,
    load_pit_universe_csv,
    pit_rows_from_universe,
    write_pit_universe_csv,
)
from turtle_bot.universe import Universe, UniverseDecision, UniversePolicy


def test_load_pit_universe_csv_filters_by_exact_as_of_date(tmp_path):
    path = tmp_path / "pit.csv"
    path.write_text(
        "\n".join(
            [
                "as_of,symbol,included,reasons,market,instrument_type",
                "2026-01-01,AAA,true,included,US,EQUITY",
                "2026-01-01,BBB,false,delisted,US,EQUITY",
                "2026-01-02,BBB,true,included,US,EQUITY",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    universe = load_pit_universe_csv(path)

    assert universe.eligible_symbols(date(2026, 1, 1)) == frozenset({"AAA"})
    assert universe.eligible_symbols(date(2026, 1, 2)) == frozenset({"BBB"})
    with pytest.raises(PitUniverseCoverageError, match="2026-01-03"):
        universe.eligible_symbols(date(2026, 1, 3))


def test_pit_rows_from_universe_can_be_written_and_reloaded(tmp_path):
    universe = Universe(
        generated_at=datetime(2026, 1, 2, 21, 0, tzinfo=timezone.utc),
        policy=UniversePolicy(candidate_symbols=("AAA", "BBB")),
        decisions=(
            UniverseDecision(
                symbol="AAA",
                included=True,
                reasons=("included",),
                stock={"market": "US", "type": "EQUITY"},
                warnings={},
                completed_candles=60,
                average_daily_value=Decimal("1000000"),
                last_close=Decimal("100"),
            ),
            UniverseDecision(
                symbol="BBB",
                included=False,
                reasons=("warning:halted",),
                stock={"market": "US", "type": "EQUITY"},
                warnings={"halted": True},
                completed_candles=60,
                average_daily_value=Decimal("1000000"),
                last_close=Decimal("100"),
            ),
        ),
    )
    path = tmp_path / "pit.csv"

    rows = pit_rows_from_universe(universe)
    write_pit_universe_csv(rows, path)
    loaded = load_pit_universe_csv(path)

    assert loaded.eligible_symbols(date(2026, 1, 2)) == frozenset({"AAA"})
    assert loaded.rows_for(date(2026, 1, 2))[1].reasons == ("warning:halted",)
