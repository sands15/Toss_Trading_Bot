from __future__ import annotations

from datetime import datetime, timezone
import hmac
from typing import TYPE_CHECKING, Mapping

from .worker import (
    ApprovalEnvelopeV2,
    ApprovalError,
    ApprovalReceiptV2,
    verify_approval_receipt_v2,
)

if TYPE_CHECKING:
    from turtle_bot.state_store import SQLiteStateStore


_PLAN_DECIMAL_FIELDS = (
    "entry_trigger",
    "entry_limit",
    "target_trigger",
    "target_limit",
    "stop_trigger",
    "stop_limit",
    "cash_reserved",
    "planned_risk",
    "planned_reward",
)
_PLAN_TIME_FIELDS = ("entry_start", "entry_expiry", "force_exit_at")


def consume_approval_v2(
    store: SQLiteStateStore,
    envelope: ApprovalEnvelopeV2,
    receipt: ApprovalReceiptV2,
    *,
    account_key: str,
    writer_id: str,
    writer_fence: int,
    discord_guild_id: str,
    discord_channel_id: str,
    discord_user_id: str,
    current_boot_id_hash: str,
    approval_generation: int,
    now: datetime,
) -> Mapping[str, object]:
    """Verify and consume one parsed synthetic v2 approval without I/O."""

    if not isinstance(envelope, ApprovalEnvelopeV2) or not isinstance(
        receipt, ApprovalReceiptV2
    ):
        raise TypeError("envelope and receipt must be parsed approval v2 values")
    current = _aware_utc(now)
    try:
        stored = store.load_intraday_plan(
            account_key=account_key,
            session_date=envelope.session_date,
        )
    except (RuntimeError, TypeError, ValueError) as exc:
        raise ApprovalError("approval_v2_plan_integrity_invalid") from exc
    if stored is None:
        raise ApprovalError("approval_v2_plan_not_found")
    _verify_stored_plan(stored, envelope, account_key=account_key)

    run = store.load_intraday_run(envelope.plan_id)
    if run is None:
        raise ApprovalError("approval_v2_run_not_found")
    if run.get("entry_disabled_at") is not None:
        raise ApprovalError("approval_v2_entry_disabled")
    if run.get("loss_fuse_at") is not None:
        raise ApprovalError("approval_v2_loss_fuse")
    if run.get("state") != "PLANNED":
        raise ApprovalError("approval_v2_consume_conflict")
    lease_until = run.get("writer_lease_until")
    if (
        run.get("writer_id") != writer_id
        or run.get("writer_fence") != writer_fence
        or not isinstance(lease_until, datetime)
        or _aware_utc(lease_until) <= current
    ):
        raise ApprovalError("approval_v2_writer_fence_invalid")
    if run.get("approval_generation") != approval_generation - 1:
        raise ApprovalError("approval_v2_generation_conflict")

    receipt_sha256 = verify_approval_receipt_v2(
        receipt,
        envelope,
        discord_guild_id=discord_guild_id,
        discord_channel_id=discord_channel_id,
        discord_user_id=discord_user_id,
        current_boot_id_hash=current_boot_id_hash,
        writer_fence=writer_fence,
        approval_generation=approval_generation,
        now=current,
    )
    approved_at = _aware_utc(datetime.fromisoformat(receipt.decided_at))
    if approved_at > current:
        raise ApprovalError("approval_v2_receipt_invalid")
    consumed = store.consume_intraday_approval(
        plan_id=envelope.plan_id,
        plan_hash=envelope.plan_hash,
        envelope_sha256=envelope.envelope_sha256,
        receipt_sha256=receipt_sha256,
        interaction_id=receipt.interaction_id,
        boot_id_hash=current_boot_id_hash,
        approval_generation=approval_generation,
        approved_writer_fence=writer_fence,
        writer_id=writer_id,
        writer_fence=writer_fence,
        approved_at=approved_at,
        approval_expires_at=_aware_utc(datetime.fromisoformat(envelope.expires_at)),
        now=current,
    )
    if consumed is None:
        raise ApprovalError("approval_v2_consume_conflict")
    return consumed


def _verify_stored_plan(
    stored: Mapping[str, object],
    envelope: ApprovalEnvelopeV2,
    *,
    account_key: str,
) -> None:
    payload = stored.get("payload")
    session_date = stored.get("session_date")
    if (
        not isinstance(payload, Mapping)
        or stored.get("account_key") != account_key
        or stored.get("plan_id") != envelope.plan_id
        or stored.get("symbol") != envelope.symbol
        or stored.get("mode") != "shadow"
        or not hasattr(session_date, "isoformat")
        or session_date.isoformat() != envelope.session_date
        or not isinstance(stored.get("plan_hash"), str)
        or not hmac.compare_digest(str(stored["plan_hash"]), envelope.plan_hash)
    ):
        raise ApprovalError("approval_v2_plan_binding_mismatch")

    expected = {
        "quantity": envelope.quantity,
        **{name: getattr(envelope, name) for name in _PLAN_DECIMAL_FIELDS},
        **{name: getattr(envelope, name) for name in _PLAN_TIME_FIELDS},
    }
    for name, approved in expected.items():
        actual = payload.get(name)
        if name == "quantity":
            if isinstance(actual, bool) or not isinstance(actual, int) or actual < 1:
                raise ApprovalError("approval_v2_plan_economics_mismatch")
            actual = str(actual)
        if not isinstance(actual, str) or not hmac.compare_digest(actual, approved):
            raise ApprovalError("approval_v2_plan_economics_mismatch")


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ApprovalError("approval_v2_clock_invalid")
    return value.astimezone(timezone.utc)
