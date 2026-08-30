from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from turtle_approval import consumer, worker
from turtle_approval.consumer import consume_approval_v2
from turtle_bot.intraday import build_intraday_plan, intraday_plan_payload
from turtle_bot.state_store import SQLiteStateStore


NOW = datetime(2026, 8, 28, 14, 20, tzinfo=timezone.utc)
GUILD_ID = "111111111111111111"
CHANNEL_ID = "222222222222222222"
USER_ID = "333333333333333333"
INTERACTION_ID = "444444444444444444"
BOOT_HASH = "b" * 64
WRITER_ID = "writer-approval-consumer"


def _plan():
    return build_intraday_plan(
        account_id="acct-approval-consumer",
        session_date=date(2026, 8, 28),
        symbol="AAPL",
        reference_at=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
        created_at=datetime(2026, 8, 28, 12, 5, tzinfo=timezone.utc),
        regular_open=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
        regular_close=datetime(2026, 8, 28, 21, 0, tzinfo=timezone.utc),
        entry_start_minutes_after_open=5,
        entry_expiry_minutes_after_open=60,
        force_exit_minutes_before_close=15,
        available_cash=Decimal("10000"),
        reference_price=Decimal("100"),
        cash_allocation_fraction=Decimal("0.5"),
        risk_fraction=Decimal("0.01"),
        take_profit_fraction=Decimal("0.02"),
        stop_fraction=Decimal("0.015"),
        stop_limit_buffer_fraction=Decimal("0.002"),
        max_entry_slippage_fraction=Decimal("0.001"),
        estimated_round_trip_cost_fraction=Decimal("0.001"),
        estimated_fixed_round_trip_cost=Decimal("0"),
        minimum_reward_risk_ratio=Decimal("1"),
        max_quantity=5,
        max_notional=Decimal("1000"),
    )


def _seed(store: SQLiteStateStore):
    plan = _plan()
    payload = intraday_plan_payload(plan)
    payload.update({"mode": "shadow", "status": "SHADOW_PLANNED"})
    saved, inserted = store.save_intraday_plan_once(
        account_key=plan.account_id,
        session_date=plan.session_date,
        symbol=plan.symbol,
        payload=payload,
        created_at=plan.created_at,
    )
    assert inserted
    store.create_intraday_run(plan_id=plan.plan_id, created_at=plan.created_at)
    run = store.claim_intraday_writer(
        plan_id=plan.plan_id,
        writer_id=WRITER_ID,
        now=NOW,
        lease_seconds=600,
    )
    assert run is not None
    return plan, saved, int(run["writer_fence"])


def _envelope(plan, saved, fence: int, **changes: object):
    payload = saved["payload"]
    mapping: dict[str, object] = {
        "schema_version": 2,
        "purpose": "INTRADAY_LIVE_ENTRY",
        "plan_id": plan.plan_id,
        "plan_hash": saved["plan_hash"],
        "account_alias": "synthetic-account",
        "session_date": plan.session_date.isoformat(),
        "symbol": plan.symbol,
        "quantity": str(payload["quantity"]),
        "entry_trigger": payload["entry_trigger"],
        "entry_limit": payload["entry_limit"],
        "target_trigger": payload["target_trigger"],
        "target_limit": payload["target_limit"],
        "stop_trigger": payload["stop_trigger"],
        "stop_limit": payload["stop_limit"],
        "cash_reserved": payload["cash_reserved"],
        "planned_risk": payload["planned_risk"],
        "planned_reward": payload["planned_reward"],
        "entry_start": payload["entry_start"],
        "entry_expiry": payload["entry_expiry"],
        "force_exit_at": payload["force_exit_at"],
        "protection_slo_seconds": 10,
        "exit_fill_slo_seconds": 30,
        "emergency_exit": {
            "policy": "MARKET_ALL_REMAINING_OWNED",
            "regular_session_only": True,
            "price_not_guaranteed": True,
        },
        "boot_id_hash": BOOT_HASH,
        "writer_fence": fence,
        "approval_generation": 1,
        "nonce": "approval_nonce_consumer_abcdef",
        "issued_at": (NOW - timedelta(minutes=1)).isoformat(),
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
    }
    mapping.update(changes)
    mapping.pop("interaction_binding", None)
    mapping["interaction_binding"] = worker.hashlib.sha256(
        worker.canonical_json_bytes(mapping)
    ).hexdigest()
    return worker.ApprovalEnvelopeV2.from_mapping(mapping)


def _receipt(envelope, *, decided_at: datetime = NOW):
    return worker.ApprovalReceiptV2.create(
        envelope,
        discord_guild_id=GUILD_ID,
        discord_channel_id=CHANNEL_ID,
        discord_user_id=USER_ID,
        interaction_id=INTERACTION_ID,
        decided_at=decided_at,
    )


def _consume(
    store: SQLiteStateStore,
    plan,
    envelope,
    receipt,
    fence: int,
    **changes: object,
):
    arguments = {
        "account_key": plan.account_id,
        "writer_id": WRITER_ID,
        "writer_fence": fence,
        "discord_guild_id": GUILD_ID,
        "discord_channel_id": CHANNEL_ID,
        "discord_user_id": USER_ID,
        "current_boot_id_hash": BOOT_HASH,
        "approval_generation": 1,
        "now": NOW,
    }
    arguments.update(changes)
    return consume_approval_v2(store, envelope, receipt, **arguments)


def _assert_code(code: str, action) -> None:
    with pytest.raises(worker.ApprovalError) as captured:
        action()
    assert captured.value.code == code


def test_synthetic_v2_receipt_consumes_exact_locked_plan_once() -> None:
    with SQLiteStateStore() as store:
        plan, saved, fence = _seed(store)
        envelope = _envelope(plan, saved, fence)
        receipt = _receipt(envelope)

        approved = _consume(store, plan, envelope, receipt, fence)

        assert approved["state"] == "APPROVED"
        assert approved["version"] == 1
        assert approved["approval_generation"] == 1
        assert approved["approved_envelope_sha256"] == envelope.envelope_sha256
        assert approved["approval_receipt_sha256"] == receipt.receipt_sha256
        assert approved["approval_interaction_id"] == INTERACTION_ID
        assert approved["approved_writer_fence"] == fence
        events = store.list_execution_events(intent_id=f"run:{plan.plan_id}")
        assert [event["event_type"] for event in events].count("approval_consumed") == 1


@pytest.mark.parametrize(
    "change",
    [
        {"quantity": "4"},
        {"entry_limit": "999"},
        {"target_limit": "999"},
        {"entry_expiry": (datetime(2026, 8, 28, 15, 31, tzinfo=timezone.utc)).isoformat()},
    ],
)
def test_changed_quantity_price_or_plan_time_is_rejected(
    change: dict[str, object],
) -> None:
    with SQLiteStateStore() as store:
        plan, saved, fence = _seed(store)
        envelope = _envelope(plan, saved, fence, **change)
        receipt = _receipt(envelope)

        _assert_code(
            "approval_v2_plan_economics_mismatch",
            lambda: _consume(store, plan, envelope, receipt, fence),
        )
        assert store.load_intraday_run(plan.plan_id)["state"] == "PLANNED"


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (
            {"plan_id": "intraday-ffffffffffffffffffffffff"},
            "approval_v2_plan_binding_mismatch",
        ),
        ({"session_date": "2026-08-29"}, "approval_v2_plan_not_found"),
        ({"symbol": "MSFT"}, "approval_v2_plan_binding_mismatch"),
    ],
)
def test_changed_plan_identity_is_rejected(
    change: dict[str, object], code: str
) -> None:
    with SQLiteStateStore() as store:
        plan, saved, fence = _seed(store)
        envelope = _envelope(plan, saved, fence, **change)
        receipt = _receipt(envelope)

        _assert_code(
            code,
            lambda: _consume(store, plan, envelope, receipt, fence),
        )
        assert store.load_intraday_run(plan.plan_id)["version"] == 0


def test_changed_plan_hash_is_rejected_before_receipt_consumption() -> None:
    with SQLiteStateStore() as store:
        plan, saved, fence = _seed(store)
        envelope = _envelope(plan, saved, fence, plan_hash="f" * 64)
        receipt = _receipt(envelope)

        _assert_code(
            "approval_v2_plan_binding_mismatch",
            lambda: _consume(store, plan, envelope, receipt, fence),
        )
        assert store.load_intraday_run(plan.plan_id)["version"] == 0


def test_expected_generation_must_be_the_next_database_generation() -> None:
    with SQLiteStateStore() as store:
        plan, saved, fence = _seed(store)
        envelope = _envelope(plan, saved, fence)
        receipt = _receipt(envelope)

        _assert_code(
            "approval_v2_generation_conflict",
            lambda: _consume(
                store,
                plan,
                envelope,
                receipt,
                fence,
                approval_generation=2,
            ),
        )
        assert store.load_intraday_run(plan.plan_id)["version"] == 0


def test_expired_envelope_is_rejected_with_active_writer_lease() -> None:
    with SQLiteStateStore() as store:
        plan, saved, fence = _seed(store)
        envelope = _envelope(plan, saved, fence)
        receipt = _receipt(envelope)
        expired_at = datetime.fromisoformat(envelope.expires_at)

        _assert_code(
            "approval_v2_expired",
            lambda: _consume(
                store, plan, envelope, receipt, fence, now=expired_at
            ),
        )
        assert store.load_intraday_run(plan.plan_id)["state"] == "PLANNED"


@pytest.mark.parametrize(
    "identity",
    [
        {"discord_guild_id": "555555555555555555"},
        {"discord_channel_id": "555555555555555555"},
        {"discord_user_id": "555555555555555555"},
    ],
)
def test_wrong_discord_identity_is_rejected(identity: dict[str, object]) -> None:
    with SQLiteStateStore() as store:
        plan, saved, fence = _seed(store)
        envelope = _envelope(plan, saved, fence)
        receipt = _receipt(envelope)

        _assert_code(
            "approval_v2_receipt_binding_mismatch",
            lambda: _consume(store, plan, envelope, receipt, fence, **identity),
        )
        assert store.load_intraday_run(plan.plan_id)["version"] == 0


@pytest.mark.parametrize(
    ("column", "code"),
    [
        ("entry_disabled_at", "approval_v2_entry_disabled"),
        ("loss_fuse_at", "approval_v2_loss_fuse"),
    ],
)
def test_persisted_entry_latch_rejects_approval(column: str, code: str) -> None:
    with SQLiteStateStore() as store:
        plan, saved, fence = _seed(store)
        envelope = _envelope(plan, saved, fence)
        receipt = _receipt(envelope)
        with store._conn:
            store._conn.execute(
                f"UPDATE intraday_runs SET {column} = ? WHERE plan_id = ?",
                (NOW.isoformat(), plan.plan_id),
            )

        _assert_code(code, lambda: _consume(store, plan, envelope, receipt, fence))
        assert store.load_intraday_run(plan.plan_id)["state"] == "PLANNED"


def test_latch_set_after_precheck_is_rejected_by_atomic_consume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with SQLiteStateStore() as store:
        plan, saved, fence = _seed(store)
        envelope = _envelope(plan, saved, fence)
        receipt = _receipt(envelope)
        original_consume = store.consume_intraday_approval

        def latch_then_consume(**arguments):
            with store._conn:
                store._conn.execute(
                    "UPDATE intraday_runs SET loss_fuse_at = ? WHERE plan_id = ?",
                    (NOW.isoformat(), plan.plan_id),
                )
            return original_consume(**arguments)

        monkeypatch.setattr(store, "consume_intraday_approval", latch_then_consume)

        _assert_code(
            "approval_v2_consume_conflict",
            lambda: _consume(store, plan, envelope, receipt, fence),
        )
        run = store.load_intraday_run(plan.plan_id)
        events = store.list_execution_events(intent_id=f"run:{plan.plan_id}")
        assert run["state"] == "PLANNED" and run["loss_fuse_at"] == NOW
        assert run["approved_envelope_sha256"] is None
        assert [event["event_type"] for event in events].count("approval_consumed") == 0


def test_receipt_replay_does_not_add_a_second_approval_event() -> None:
    with SQLiteStateStore() as store:
        plan, saved, fence = _seed(store)
        envelope = _envelope(plan, saved, fence)
        receipt = _receipt(envelope)
        _consume(store, plan, envelope, receipt, fence)

        _assert_code(
            "approval_v2_consume_conflict",
            lambda: _consume(store, plan, envelope, receipt, fence),
        )
        run = store.load_intraday_run(plan.plan_id)
        events = store.list_execution_events(intent_id=f"run:{plan.plan_id}")
        assert run["version"] == 1
        assert [event["event_type"] for event in events].count("approval_consumed") == 1


def test_consumer_module_has_no_broker_network_filesystem_or_ssh_capability() -> None:
    forbidden = {"os", "pathlib", "socket", "subprocess", "urllib"}
    assert forbidden.isdisjoint(consumer.__dict__)
    assert "turtle_bot" not in consumer.__dict__
