from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from typing import Any

from .domain import as_decimal


_CENT = Decimal("0.01")
_SUB_DOLLAR_TICK = Decimal("0.0001")
_ONE = Decimal("1")


@dataclass(frozen=True, slots=True)
class IntradayPlan:
    plan_id: str
    entry_client_order_id: str
    oco_client_order_id: str
    account_id: str
    session_date: date
    symbol: str
    reference_at: datetime
    created_at: datetime
    regular_open: datetime
    regular_close: datetime
    entry_start_minutes_after_open: int
    entry_expiry_minutes_after_open: int
    force_exit_minutes_before_close: int
    entry_start: datetime
    entry_expiry: datetime
    force_exit_at: datetime
    available_cash: Decimal
    reference_price: Decimal
    cash_allocation_fraction: Decimal
    risk_fraction: Decimal
    take_profit_fraction: Decimal
    stop_fraction: Decimal
    stop_limit_buffer_fraction: Decimal
    max_entry_slippage_fraction: Decimal
    estimated_round_trip_cost_fraction: Decimal
    estimated_fixed_round_trip_cost: Decimal
    minimum_reward_risk_ratio: Decimal
    max_quantity: int
    max_notional: Decimal
    allocated_cash: Decimal
    risk_budget: Decimal
    entry_trigger: Decimal
    entry_limit: Decimal
    target_trigger: Decimal
    target_limit: Decimal
    stop_trigger: Decimal
    stop_limit: Decimal
    estimated_round_trip_cost_per_share: Decimal
    estimated_round_trip_cost: Decimal
    cash_required_per_share: Decimal
    risk_per_share: Decimal
    reward_per_share: Decimal
    reward_risk_ratio: Decimal
    quantity: int
    entry_notional: Decimal
    cash_reserved: Decimal
    planned_risk: Decimal
    planned_reward: Decimal


def build_intraday_plan(
    *,
    account_id: str,
    session_date: date | str,
    symbol: str,
    reference_at: datetime,
    created_at: datetime,
    regular_open: datetime,
    regular_close: datetime,
    entry_start_minutes_after_open: int,
    entry_expiry_minutes_after_open: int,
    force_exit_minutes_before_close: int,
    available_cash: Decimal,
    reference_price: Decimal,
    cash_allocation_fraction: Decimal,
    risk_fraction: Decimal,
    take_profit_fraction: Decimal,
    stop_fraction: Decimal,
    stop_limit_buffer_fraction: Decimal,
    max_entry_slippage_fraction: Decimal,
    estimated_round_trip_cost_fraction: Decimal,
    estimated_fixed_round_trip_cost: Decimal,
    minimum_reward_risk_ratio: Decimal,
    max_quantity: int,
    max_notional: Decimal,
) -> IntradayPlan:
    """Build one immutable, cash-only US intraday plan without placing orders."""

    clean_account_id = _nonempty_text("account_id", account_id)
    clean_session_date = _date(session_date)
    clean_symbol = _nonempty_text("symbol", symbol).upper()
    clean_reference_at = _aware_datetime("reference_at", reference_at)
    clean_created_at = _aware_datetime("created_at", created_at)
    clean_regular_open = _aware_datetime("regular_open", regular_open)
    clean_regular_close = _aware_datetime("regular_close", regular_close)
    entry_start_offset = _nonnegative_int(
        "entry_start_minutes_after_open", entry_start_minutes_after_open
    )
    entry_expiry_offset = _positive_int(
        "entry_expiry_minutes_after_open", entry_expiry_minutes_after_open
    )
    force_exit_offset = _positive_int(
        "force_exit_minutes_before_close", force_exit_minutes_before_close
    )
    if clean_reference_at > clean_created_at:
        raise ValueError("reference_at must not be after created_at")
    if clean_created_at >= clean_regular_open:
        raise ValueError("created_at must be before regular_open")
    if clean_regular_open >= clean_regular_close:
        raise ValueError("regular_open must be before regular_close")
    if clean_regular_open.date() != clean_session_date:
        raise ValueError("session_date must match regular_open date")
    entry_start = clean_regular_open + timedelta(minutes=entry_start_offset)
    entry_expiry = clean_regular_open + timedelta(minutes=entry_expiry_offset)
    force_exit_at = clean_regular_close - timedelta(minutes=force_exit_offset)
    if not clean_regular_open <= entry_start < entry_expiry < force_exit_at < clean_regular_close:
        raise ValueError("execution window must satisfy open <= start < expiry < force exit < close")
    cash = _positive_decimal("available_cash", available_cash)
    reference = _positive_decimal("reference_price", reference_price)
    allocation = _fraction("cash_allocation_fraction", cash_allocation_fraction, zero=False)
    risk = _fraction("risk_fraction", risk_fraction, zero=False)
    take_profit = _positive_decimal("take_profit_fraction", take_profit_fraction)
    stop = _fraction("stop_fraction", stop_fraction, zero=False, upper_inclusive=False)
    stop_buffer = _fraction(
        "stop_limit_buffer_fraction",
        stop_limit_buffer_fraction,
        zero=True,
        upper_inclusive=False,
    )
    entry_slippage = _fraction(
        "max_entry_slippage_fraction",
        max_entry_slippage_fraction,
        zero=True,
        upper_inclusive=False,
    )
    round_trip_cost = _fraction(
        "estimated_round_trip_cost_fraction",
        estimated_round_trip_cost_fraction,
        zero=True,
        upper_inclusive=False,
    )
    fixed_round_trip_cost = _nonnegative_decimal(
        "estimated_fixed_round_trip_cost", estimated_fixed_round_trip_cost
    )
    minimum_reward_risk = _positive_decimal(
        "minimum_reward_risk_ratio", minimum_reward_risk_ratio
    )
    notional_cap = _positive_decimal("max_notional", max_notional)
    quantity_cap = _positive_int("max_quantity", max_quantity)

    entry_trigger = _round_us_price(reference, ROUND_CEILING)
    entry_limit = _round_us_price(reference * (_ONE + entry_slippage), ROUND_CEILING)
    target_trigger = _round_us_price(entry_limit * (_ONE + take_profit), ROUND_CEILING)
    target_limit = target_trigger
    stop_trigger = _round_us_price(entry_limit * (_ONE - stop), ROUND_FLOOR)
    stop_limit = _round_us_price(stop_trigger * (_ONE - stop_buffer), ROUND_FLOOR)

    if not (
        Decimal("0") < stop_limit <= stop_trigger < entry_trigger <= entry_limit
        < target_limit <= target_trigger
    ):
        raise ValueError("derived prices are nonpositive or misordered")

    # Use the larger planned exit notional for a conservative round-trip reserve.
    # This buffer is held in cash sizing as well as included in risk/reward.
    cost_per_share = target_limit * round_trip_cost
    cash_required_per_share = entry_limit + cost_per_share
    risk_per_share = entry_limit - stop_limit + cost_per_share
    reward_per_share = target_limit - entry_limit - cost_per_share
    if risk_per_share <= 0:
        raise ValueError("risk_per_share must be positive")
    if reward_per_share <= 0:
        raise ValueError("reward_per_share must be positive after estimated costs")
    allocated_cash = cash * allocation
    risk_budget = cash * risk
    quantity = min(
        quantity_cap,
        _whole_shares(allocated_cash - fixed_round_trip_cost, cash_required_per_share),
        _whole_shares(notional_cap, entry_limit),
        _whole_shares(risk_budget - fixed_round_trip_cost, risk_per_share),
    )
    if quantity < 1:
        raise ValueError("cash and risk limits do not permit one whole share")

    identity = _identity(
        account_id=clean_account_id,
        session_date=clean_session_date,
        symbol=clean_symbol,
        reference_at=clean_reference_at,
        regular_open=clean_regular_open,
        regular_close=clean_regular_close,
        entry_start_minutes_after_open=entry_start_offset,
        entry_expiry_minutes_after_open=entry_expiry_offset,
        force_exit_minutes_before_close=force_exit_offset,
        available_cash=cash,
        reference_price=reference,
        cash_allocation_fraction=allocation,
        risk_fraction=risk,
        take_profit_fraction=take_profit,
        stop_fraction=stop,
        stop_limit_buffer_fraction=stop_buffer,
        max_entry_slippage_fraction=entry_slippage,
        estimated_round_trip_cost_fraction=round_trip_cost,
        estimated_fixed_round_trip_cost=fixed_round_trip_cost,
        minimum_reward_risk_ratio=minimum_reward_risk,
        max_quantity=quantity_cap,
        max_notional=notional_cap,
    )
    plan_digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    entry_notional = entry_limit * quantity
    estimated_round_trip_cost = cost_per_share * quantity + fixed_round_trip_cost
    cash_reserved = entry_notional + estimated_round_trip_cost
    planned_risk = (entry_limit - stop_limit) * quantity + estimated_round_trip_cost
    planned_reward = (target_limit - entry_limit) * quantity - estimated_round_trip_cost
    if planned_reward <= 0:
        raise ValueError("planned_reward must be positive after estimated costs")
    reward_risk_ratio = planned_reward / planned_risk
    if reward_risk_ratio < minimum_reward_risk:
        raise ValueError("reward/risk ratio is below minimum_reward_risk_ratio")
    if entry_notional > notional_cap:
        raise AssertionError("entry notional exceeded max_notional")
    if cash_reserved > allocated_cash:
        raise AssertionError("cash reserve exceeded allocated cash")
    if planned_risk > risk_budget:
        raise AssertionError("planned risk exceeded risk budget")

    return IntradayPlan(
        plan_id=f"intraday-{plan_digest[:24]}",
        entry_client_order_id=_client_order_id(identity, "entry"),
        oco_client_order_id=_client_order_id(identity, "oco"),
        account_id=clean_account_id,
        session_date=clean_session_date,
        symbol=clean_symbol,
        reference_at=clean_reference_at,
        created_at=clean_created_at,
        regular_open=clean_regular_open,
        regular_close=clean_regular_close,
        entry_start_minutes_after_open=entry_start_offset,
        entry_expiry_minutes_after_open=entry_expiry_offset,
        force_exit_minutes_before_close=force_exit_offset,
        entry_start=entry_start,
        entry_expiry=entry_expiry,
        force_exit_at=force_exit_at,
        available_cash=cash,
        reference_price=reference,
        cash_allocation_fraction=allocation,
        risk_fraction=risk,
        take_profit_fraction=take_profit,
        stop_fraction=stop,
        stop_limit_buffer_fraction=stop_buffer,
        max_entry_slippage_fraction=entry_slippage,
        estimated_round_trip_cost_fraction=round_trip_cost,
        estimated_fixed_round_trip_cost=fixed_round_trip_cost,
        minimum_reward_risk_ratio=minimum_reward_risk,
        max_quantity=quantity_cap,
        max_notional=notional_cap,
        allocated_cash=allocated_cash,
        risk_budget=risk_budget,
        entry_trigger=entry_trigger,
        entry_limit=entry_limit,
        target_trigger=target_trigger,
        target_limit=target_limit,
        stop_trigger=stop_trigger,
        stop_limit=stop_limit,
        estimated_round_trip_cost_per_share=cost_per_share,
        estimated_round_trip_cost=estimated_round_trip_cost,
        cash_required_per_share=cash_required_per_share,
        risk_per_share=risk_per_share,
        reward_per_share=reward_per_share,
        reward_risk_ratio=reward_risk_ratio,
        quantity=quantity,
        entry_notional=entry_notional,
        cash_reserved=cash_reserved,
        planned_risk=planned_risk,
        planned_reward=planned_reward,
    )


def intraday_plan_payload(plan: IntradayPlan) -> dict[str, Any]:
    """Return a canonical JSON-safe representation of an immutable plan."""

    return _json_safe(asdict(plan))


def _round_us_price(price: Decimal, rounding: str) -> Decimal:
    if not price.is_finite() or price <= 0:
        raise ValueError("price must be finite and positive")
    tick = _CENT if price >= _ONE else _SUB_DOLLAR_TICK
    return (price / tick).to_integral_value(rounding=rounding) * tick


def _whole_shares(amount: Decimal, per_share: Decimal) -> int:
    return int((amount / per_share).to_integral_value(rounding=ROUND_FLOOR))


def _positive_decimal(name: str, value: Any) -> Decimal:
    try:
        result = as_decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal number") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return result


def _nonnegative_decimal(name: str, value: Any) -> Decimal:
    try:
        result = as_decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal number") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"{name} must be finite and nonnegative")
    return result


def _fraction(
    name: str,
    value: Any,
    *,
    zero: bool,
    upper_inclusive: bool = True,
) -> Decimal:
    try:
        result = as_decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal number") from exc
    lower_valid = result >= 0 if zero else result > 0
    upper_valid = result <= _ONE if upper_inclusive else result < _ONE
    if not result.is_finite() or not lower_valid or not upper_valid:
        lower = "0" if zero else "greater than 0"
        upper = "1" if upper_inclusive else "less than 1"
        raise ValueError(f"{name} must be {lower} through {upper}")
    return result


def _positive_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _nonempty_text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or any(ch.isspace() for ch in value.strip()):
        raise ValueError(f"{name} must be nonempty and contain no whitespace")
    return value.strip()


def _date(value: date | str) -> date:
    if isinstance(value, datetime):
        raise ValueError("session_date must be a date, not a datetime")
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("session_date must be an ISO date") from exc


def _aware_datetime(name: str, value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value


def _identity(**values: Any) -> str:
    serializable = {
        key: (
            _canonical_decimal(value)
            if isinstance(value, Decimal)
            else value.astimezone(timezone.utc).isoformat()
            if isinstance(value, datetime)
            else value.isoformat()
            if isinstance(value, date)
            else value
        )
        for key, value in values.items()
    }
    return json.dumps(serializable, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _canonical_decimal(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _client_order_id(identity: str, role: str) -> str:
    digest = hashlib.sha256(f"{identity}:{role}".encode("utf-8")).hexdigest()
    return f"itd-{role[0]}-{digest[:20]}"


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _canonical_decimal(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value
