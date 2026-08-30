from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from turtle_bot.intraday import build_intraday_plan


def _plan(**overrides):
    eastern = timezone(timedelta(hours=-4))
    values = {
        "account_id": "account-1",
        "session_date": date(2026, 8, 28),
        "symbol": "AAPL",
        "reference_at": datetime(2026, 8, 28, 8, 0, tzinfo=eastern),
        "created_at": datetime(2026, 8, 28, 8, 5, tzinfo=eastern),
        "regular_open": datetime(2026, 8, 28, 9, 30, tzinfo=eastern),
        "regular_close": datetime(2026, 8, 28, 16, 0, tzinfo=eastern),
        "entry_start_minutes_after_open": 5,
        "entry_expiry_minutes_after_open": 60,
        "force_exit_minutes_before_close": 15,
        "available_cash": Decimal("10000"),
        "reference_price": Decimal("100.001"),
        "cash_allocation_fraction": Decimal("0.50"),
        "risk_fraction": Decimal("0.01"),
        "take_profit_fraction": Decimal("0.02"),
        "stop_fraction": Decimal("0.015"),
        "stop_limit_buffer_fraction": Decimal("0.002"),
        "max_entry_slippage_fraction": Decimal("0.001"),
        "estimated_round_trip_cost_fraction": Decimal("0.001"),
        "estimated_fixed_round_trip_cost": Decimal("0.01"),
        "minimum_reward_risk_ratio": Decimal("1"),
        "max_quantity": 100,
        "max_notional": Decimal("3000"),
    }
    values.update(overrides)
    return build_intraday_plan(**values)


def test_builds_cash_and_risk_capped_plan_from_worst_case_entry() -> None:
    plan = _plan()

    assert plan.entry_trigger == Decimal("100.01")
    assert plan.entry_limit == Decimal("100.11")
    assert plan.target_trigger == plan.target_limit == Decimal("102.12")
    assert plan.stop_trigger == Decimal("98.60")
    assert plan.stop_limit == Decimal("98.40")
    assert plan.allocated_cash == Decimal("5000.00")
    assert plan.risk_budget == Decimal("100.00")
    assert plan.estimated_round_trip_cost_per_share == Decimal("0.10212")
    assert plan.cash_required_per_share == Decimal("100.21212")
    assert plan.risk_per_share == Decimal("1.81212")
    assert plan.reward_per_share == Decimal("1.90788")
    assert plan.estimated_round_trip_cost == Decimal("2.97148")
    assert plan.reward_risk_ratio == Decimal("55.31852") / Decimal("52.56148")
    assert plan.quantity == 29
    assert plan.entry_notional == Decimal("2903.19")
    assert plan.cash_reserved == Decimal("2906.16148")
    assert plan.planned_risk == Decimal("52.56148")
    assert plan.planned_reward == Decimal("55.31852")
    assert plan.entry_start.hour == 9 and plan.entry_start.minute == 35
    assert plan.entry_expiry.hour == 10 and plan.entry_expiry.minute == 30
    assert plan.force_exit_at.hour == 15 and plan.force_exit_at.minute == 45


def test_uses_sub_dollar_tick_without_float_math() -> None:
    plan = _plan(
        reference_price=Decimal("0.87654"),
        available_cash=Decimal("100"),
        max_notional=Decimal("100"),
        max_quantity=1,
        estimated_fixed_round_trip_cost=Decimal("0"),
    )

    assert plan.entry_trigger == Decimal("0.8766")
    assert plan.entry_limit == Decimal("0.8775")
    assert plan.target_limit == Decimal("0.8951")
    assert plan.stop_trigger == Decimal("0.8643")
    assert plan.stop_limit == Decimal("0.8625")
    assert all(
        isinstance(value, Decimal)
        for value in (
            plan.entry_trigger,
            plan.entry_limit,
            plan.target_limit,
            plan.stop_trigger,
            plan.stop_limit,
            plan.risk_per_share,
        )
    )


def test_switches_to_cent_tick_when_derived_price_reaches_one_dollar() -> None:
    plan = _plan(
        reference_price=Decimal("0.9999"),
        available_cash=Decimal("100"),
        max_notional=Decimal("100"),
        max_quantity=1,
        max_entry_slippage_fraction=Decimal("0.00001"),
        estimated_fixed_round_trip_cost=Decimal("0"),
    )

    assert plan.entry_trigger == Decimal("0.9999")
    assert plan.entry_limit == Decimal("1.0000")
    assert plan.target_limit == Decimal("1.02")


def test_rejects_when_cash_or_risk_budget_cannot_buy_one_share() -> None:
    with pytest.raises(ValueError, match="one whole share"):
        _plan(available_cash=Decimal("50"))


def test_rejects_misordered_levels_from_excess_entry_slippage() -> None:
    with pytest.raises(ValueError, match="misordered"):
        _plan(
            max_entry_slippage_fraction=Decimal("0.10"),
            stop_fraction=Decimal("0.01"),
        )


def test_rejects_nonpositive_reward_after_estimated_cost() -> None:
    with pytest.raises(ValueError, match="reward_per_share"):
        _plan(
            take_profit_fraction=Decimal("0.001"),
            estimated_round_trip_cost_fraction=Decimal("0.002"),
        )


def test_rejects_plan_below_minimum_reward_risk_after_rounding_and_costs() -> None:
    with pytest.raises(ValueError, match="reward/risk ratio"):
        _plan(minimum_reward_risk_ratio=Decimal("1.1"))


def test_cash_quantity_reserves_round_trip_cost() -> None:
    with pytest.raises(ValueError, match="one whole share"):
        _plan(
            available_cash=Decimal("100.20"),
            cash_allocation_fraction=Decimal("1"),
            max_notional=Decimal("1000"),
            max_quantity=1,
        )


def test_rejects_naive_or_reversed_audit_timestamps() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _plan(reference_at=datetime(2026, 8, 28, 8, 0))

    with pytest.raises(ValueError, match="reference_at"):
        _plan(
            reference_at=datetime(2026, 8, 28, 8, 10, tzinfo=timezone.utc),
            created_at=datetime(2026, 8, 28, 8, 5, tzinfo=timezone.utc),
        )


def test_rejects_plan_created_after_open_and_invalid_execution_window() -> None:
    eastern = timezone(timedelta(hours=-4))
    with pytest.raises(ValueError, match="created_at"):
        _plan(created_at=datetime(2026, 8, 28, 9, 30, tzinfo=eastern))

    with pytest.raises(ValueError, match="execution window"):
        _plan(entry_expiry_minutes_after_open=390)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("available_cash", Decimal("0")),
        ("reference_price", Decimal("NaN")),
        ("cash_allocation_fraction", Decimal("1.01")),
        ("risk_fraction", Decimal("0")),
        ("stop_fraction", Decimal("1")),
        ("stop_limit_buffer_fraction", Decimal("-0.01")),
        ("max_entry_slippage_fraction", Decimal("1")),
        ("estimated_round_trip_cost_fraction", Decimal("Infinity")),
        ("estimated_fixed_round_trip_cost", Decimal("-0.01")),
        ("minimum_reward_risk_ratio", Decimal("0")),
        ("max_notional", Decimal("-1")),
        ("max_quantity", 0),
        ("entry_start_minutes_after_open", -1),
        ("entry_expiry_minutes_after_open", 0),
        ("force_exit_minutes_before_close", 0),
    ],
)
def test_rejects_invalid_inputs(name: str, value: object) -> None:
    with pytest.raises(ValueError):
        _plan(**{name: value})


def test_ids_are_stable_for_equivalent_inputs_and_change_with_config() -> None:
    first = _plan(symbol=" aapl ", available_cash=Decimal("10000.00"))
    second = _plan(
        symbol="AAPL",
        available_cash=Decimal("1E+4"),
        session_date="2026-08-28",
        created_at=datetime(
            2026,
            8,
            28,
            8,
            30,
            tzinfo=timezone(timedelta(hours=-4)),
        ),
    )
    changed = _plan(reference_price=Decimal("100.02"))

    assert first.plan_id == second.plan_id
    assert first.entry_client_order_id == second.entry_client_order_id
    assert first.oco_client_order_id == second.oco_client_order_id
    assert first.entry_client_order_id != first.oco_client_order_id
    assert changed.plan_id != first.plan_id


def test_plan_is_immutable() -> None:
    plan = _plan()

    with pytest.raises(FrozenInstanceError):
        plan.quantity = 1  # type: ignore[misc]
