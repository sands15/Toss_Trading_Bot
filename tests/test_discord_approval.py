from __future__ import annotations

import asyncio
import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from turtle_approval import worker
from turtle_approval.worker import (
    ApprovalConfig,
    ApprovalError,
    ApprovalService,
    extract_hash_suffix,
    load_envelope,
    make_approve_custom_id,
    make_confirm_custom_id,
)


NOW = datetime(2026, 8, 29, 0, 30, tzinfo=timezone.utc)
GUILD_ID = "111111111111111111"
CHANNEL_ID = "222222222222222222"
USER_ID = "333333333333333333"
OTHER_ID = "444444444444444444"
INTERACTION_ID = "555555555555555555"
NONCE = "nonce_abcdefghijklmnopqr"
PLAN_HASH = "a" * 56 + "deadbeef"


def _v2_envelope(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 2,
        "purpose": "INTRADAY_LIVE_ENTRY",
        "plan_id": "intraday-0123456789abcdef01234567",
        "plan_hash": PLAN_HASH,
        "account_alias": "primary-account",
        "session_date": "2026-08-29",
        "symbol": "AAPL",
        "quantity": "5",
        "entry_trigger": "200",
        "entry_limit": "200.2",
        "target_trigger": "204",
        "target_limit": "203.8",
        "stop_trigger": "198",
        "stop_limit": "197.8",
        "cash_reserved": "1001",
        "planned_risk": "11",
        "planned_reward": "19.8",
        "entry_start": (NOW + timedelta(minutes=5)).isoformat(),
        "entry_expiry": (NOW + timedelta(minutes=20)).isoformat(),
        "force_exit_at": (NOW + timedelta(hours=5)).isoformat(),
        "protection_slo_seconds": 10,
        "exit_fill_slo_seconds": 30,
        "emergency_exit": {
            "policy": "MARKET_ALL_REMAINING_OWNED",
            "regular_session_only": True,
            "price_not_guaranteed": True,
        },
        "boot_id_hash": "b" * 64,
        "writer_fence": 7,
        "approval_generation": 3,
        "nonce": "approval_nonce_abcdefghijkl",
        "issued_at": (NOW - timedelta(minutes=5)).isoformat(),
        "expires_at": (NOW + timedelta(minutes=4)).isoformat(),
    }
    value.update(changes)
    without_binding = dict(value)
    without_binding.pop("interaction_binding", None)
    value["interaction_binding"] = worker.hashlib.sha256(
        worker.canonical_json_bytes(without_binding)
    ).hexdigest()
    return value


def _runtime(tmp_path: Path) -> Path:
    path = tmp_path / "approval-runtime"
    path.mkdir(mode=0o700)
    inbox = path / worker.INBOX_NAME
    inbox.mkdir(mode=0o700)
    if os.name != "nt":
        path.chmod(0o700)
        inbox.chmod(0o700)
    return path


def _envelope(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "generated_at": (NOW - timedelta(minutes=10)).isoformat(),
        "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        "session_date": "2026-08-29",
        "plan_id": "intraday-0123456789abcdef01234567",
        "plan_hash": PLAN_HASH,
        "nonce": NONCE,
        "account_alias": "primary-account",
        "mode": "shadow",
        "live_order_submission": False,
        "symbol": "AAPL",
        "allocated_cash": "1000.00",
        "quantity": 5,
        "entry_trigger": "200.00",
        "entry_limit": "200.20",
        "target_trigger": "204.00",
        "stop_trigger": "198.00",
        "stop_limit": "197.80",
        "planned_risk": "11.00",
        "reward_risk_ratio": "1.80",
    }
    value.update(changes)
    return value


def _write_envelope(runtime: Path, **changes: object) -> Path:
    path = runtime / worker.ENVELOPE_NAME
    path.write_text(json.dumps(_envelope(**changes)), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    return path


def _config(runtime: Path) -> ApprovalConfig:
    return ApprovalConfig(
        bot_token="test-token-never-log",
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        allowed_user_id=USER_ID,
        envelope_path=runtime / worker.ENVELOPE_NAME,
        inbox_dir=runtime / worker.INBOX_NAME,
        poll_interval_seconds=0.01,
    )


def _service(tmp_path: Path, **changes: object) -> tuple[ApprovalService, Path]:
    runtime = _runtime(tmp_path)
    _write_envelope(runtime, **changes)
    return ApprovalService(_config(runtime), clock=lambda: NOW), runtime


def _assert_code(code: str, call) -> None:
    with pytest.raises(ApprovalError) as captured:
        call()
    assert captured.value.code == code


def test_config_uses_exact_allowlists_redacts_token_and_rejects_trading_env(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    values = {
        worker.TOKEN_ENV: "secret-bot-token",
        worker.GUILD_ENV: GUILD_ID,
        worker.CHANNEL_ENV: CHANNEL_ID,
        worker.USER_ENV: USER_ID,
        worker.ENVELOPE_PATH_ENV: str(runtime / worker.ENVELOPE_NAME),
        worker.INBOX_DIR_ENV: str(runtime / worker.INBOX_NAME),
    }
    config = ApprovalConfig.from_env(values)

    assert config.guild_id == GUILD_ID
    assert config.channel_id == CHANNEL_ID
    assert config.allowed_user_id == USER_ID
    assert "secret-bot-token" not in repr(config)

    for forbidden in (
        "TOSS_CLIENT_ID",
        "TOSS_ANY_FUTURE_SECRET",
        "TURTLE_STATE_DB",
        "TURTLE_AI_API_KEY",
        "FINNHUB_API_KEY",
        "DISCORD_NEWS_WEBHOOK_URL",
        "UNRELATED_WEBHOOK_URL",
        "LLM_API_KEY",
        "LOCAL_LLM_ENDPOINT",
        "OLLAMA_HOST",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
    ):
        poisoned = dict(values)
        poisoned[forbidden] = ""
        _assert_code(
            "trading_capability_in_approval_environment",
            lambda poisoned=poisoned: ApprovalConfig.from_env(poisoned),
        )

    invalid = dict(values)
    invalid[worker.CHANNEL_ENV] = "123"
    _assert_code(
        "discord_channel_id_invalid", lambda: ApprovalConfig.from_env(invalid)
    )


def test_config_rejects_runtime_symlink(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    link = tmp_path / "runtime-link"
    try:
        link.symlink_to(runtime, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    values = {
        worker.TOKEN_ENV: "secret-bot-token",
        worker.GUILD_ENV: GUILD_ID,
        worker.CHANNEL_ENV: CHANNEL_ID,
        worker.USER_ENV: USER_ID,
        worker.ENVELOPE_PATH_ENV: str(link.absolute() / worker.ENVELOPE_NAME),
        worker.INBOX_DIR_ENV: str(runtime / worker.INBOX_NAME),
    }
    _assert_code(
        "approval_envelope_directory_invalid", lambda: ApprovalConfig.from_env(values)
    )


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"unexpected": True}, "approval_envelope_schema_invalid"),
        ({"plan_hash": "A" * 64}, "approval_plan_hash_invalid"),
        ({"nonce": "too-short"}, "approval_nonce_invalid"),
        ({"expires_at": "2026-08-29T01:30:00"}, "approval_envelope_expiry_invalid"),
        ({"quantity": True}, "approval_quantity_invalid"),
        ({"allocated_cash": "Infinity"}, "approval_allocated_cash_invalid"),
        ({"live_order_submission": True}, "approval_envelope_schema_invalid"),
    ],
)
def test_envelope_has_a_strict_redacted_schema(
    tmp_path: Path, change: dict[str, object], code: str
) -> None:
    runtime = _runtime(tmp_path)
    path = _write_envelope(runtime, **change)
    _assert_code(code, lambda: load_envelope(path))


def test_envelope_rejects_duplicate_keys_oversize_and_symlink(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    path = runtime / worker.ENVELOPE_NAME
    path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    _assert_code("approval_envelope_invalid", lambda: load_envelope(path))

    path.write_bytes(b"{" + b" " * worker.MAX_ENVELOPE_BYTES + b"}")
    if os.name != "nt":
        path.chmod(0o600)
    _assert_code("approval_envelope_invalid", lambda: load_envelope(path))

    path.unlink()
    target = tmp_path / "outside-envelope.json"
    target.write_text(json.dumps(_envelope()), encoding="utf-8")
    if os.name != "nt":
        target.chmod(0o600)
    try:
        path.symlink_to(target)
    except OSError:
        return
    _assert_code("approval_envelope_invalid", lambda: load_envelope(path))


@pytest.mark.parametrize(
    ("context_change"),
    [
        {"user_id": OTHER_ID},
        {"guild_id": OTHER_ID},
        {"channel_id": OTHER_ID},
        {"user_id": None},
    ],
)
def test_button_requires_exact_user_guild_and_channel(
    tmp_path: Path, context_change: dict[str, object]
) -> None:
    service, runtime = _service(tmp_path)
    envelope = service.current_envelope()
    context: dict[str, object] = {
        "user_id": USER_ID,
        "guild_id": GUILD_ID,
        "channel_id": CHANNEL_ID,
    }
    context.update(context_change)
    _assert_code(
        "approval_context_denied",
        lambda: service.begin(
            custom_id=make_approve_custom_id(envelope), **context
        ),
    )
    assert list((runtime / worker.INBOX_NAME).iterdir()) == []


def test_button_binds_plan_hash_nonce_and_expiry_and_rechecks_expiry(
    tmp_path: Path,
) -> None:
    service, runtime = _service(tmp_path)
    original = service.current_envelope()
    custom_id = make_approve_custom_id(original)
    assert len(custom_id) <= 100
    assert original.nonce not in custom_id

    changed_hash = "b" * 56 + "deadbeef"
    _write_envelope(runtime, plan_hash=changed_hash)
    _assert_code(
        "approval_plan_binding_mismatch",
        lambda: service.begin(
            custom_id=custom_id,
            user_id=USER_ID,
            guild_id=GUILD_ID,
            channel_id=CHANNEL_ID,
        ),
    )

    _write_envelope(runtime, expires_at=NOW.isoformat())
    _assert_code(
        "approval_expired",
        lambda: service.begin(
            custom_id=make_approve_custom_id(load_envelope(runtime / worker.ENVELOPE_NAME)),
            user_id=USER_ID,
            guild_id=GUILD_ID,
            channel_id=CHANNEL_ID,
        ),
    )

    _write_envelope(runtime, nonce="n" * 80)
    longest_nonce = load_envelope(runtime / worker.ENVELOPE_NAME)
    assert len(make_approve_custom_id(longest_nonce)) <= 100


def test_future_generated_envelope_is_not_yet_valid(tmp_path: Path) -> None:
    service, runtime = _service(tmp_path)
    _write_envelope(
        runtime,
        generated_at=(NOW + timedelta(minutes=1)).isoformat(),
        expires_at=(NOW + timedelta(hours=1)).isoformat(),
    )

    _assert_code(
        "approval_not_yet_valid",
        service.current_envelope,
    )


def test_modal_requires_hash_suffix_and_writes_one_minimal_private_receipt(
    tmp_path: Path,
) -> None:
    service, _ = _service(tmp_path)
    envelope = service.current_envelope()
    values = {
        "custom_id": make_confirm_custom_id(envelope),
        "interaction_id": INTERACTION_ID,
        "user_id": USER_ID,
        "guild_id": GUILD_ID,
        "channel_id": CHANNEL_ID,
    }

    _assert_code(
        "approval_hash_suffix_mismatch",
        lambda: service.approve(hash_suffix="00000000", **values),
    )
    _assert_code(
        "approval_hash_suffix_mismatch",
        lambda: service.approve(hash_suffix="é" * 8, **values),
    )
    assert not service.receipt_exists(envelope)

    decision = service.approve(hash_suffix=envelope.hash_suffix, **values)
    receipt = json.loads(decision.receipt_path.read_text(encoding="utf-8"))
    assert receipt == {
        "schema_version": 1,
        "decision": "APPROVE",
        "plan_id": envelope.plan_id,
        "plan_hash": envelope.plan_hash,
        "interaction_binding": envelope.interaction_binding,
        "nonce_sha256": worker.hashlib.sha256(envelope.nonce.encode("ascii")).hexdigest(),
        "expires_at": envelope.expires_at.isoformat(),
        "decided_at": NOW.isoformat(),
        "discord_user_id": USER_ID,
        "guild_id": GUILD_ID,
        "channel_id": CHANNEL_ID,
        "interaction_id": INTERACTION_ID,
    }
    serialized = decision.receipt_path.read_text(encoding="utf-8")
    assert envelope.nonce not in serialized
    assert "test-token-never-log" not in serialized
    assert "entry_trigger" not in serialized
    if os.name != "nt":
        assert stat.S_IMODE(decision.receipt_path.stat().st_mode) == 0o600

    _assert_code(
        "decision_already_recorded",
        lambda: service.approve(hash_suffix=envelope.hash_suffix, **values),
    )


@pytest.mark.parametrize(
    "change",
    [
        {"plan_id": "intraday-fedcba9876543210fedcba98"},
        {"plan_hash": "b" * 64},
        {"nonce": "replacement_nonce_abcdefgh"},
        {"expires_at": (NOW + timedelta(hours=2)).isoformat()},
        {"session_date": "2026-08-30"},
        {"account_alias": "secondary-account"},
        {"symbol": "MSFT"},
        {"allocated_cash": "2000"},
        {"quantity": 6},
        {"entry_trigger": "201"},
        {"entry_limit": "202"},
        {"target_trigger": "205"},
        {"stop_trigger": "197"},
        {"stop_limit": "196"},
        {"planned_risk": "12"},
        {"reward_risk_ratio": "2"},
    ],
)
def test_modal_reloads_and_rejects_each_binding_changed_after_button(
    tmp_path: Path, change: dict[str, object]
) -> None:
    service, runtime = _service(tmp_path)
    original = service.current_envelope()
    service.begin(
        custom_id=make_approve_custom_id(original),
        user_id=USER_ID,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
    )
    _write_envelope(runtime, **change)

    _assert_code(
        "approval_plan_binding_mismatch",
        lambda: service.approve(
            custom_id=make_confirm_custom_id(original),
            hash_suffix=original.hash_suffix,
            interaction_id=INTERACTION_ID,
            user_id=USER_ID,
            guild_id=GUILD_ID,
            channel_id=CHANNEL_ID,
        ),
    )
    assert list(service.config.inbox_dir.iterdir()) == []


def test_exclusive_create_allows_only_one_concurrent_decision(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    envelope = service.current_envelope()

    def approve(interaction_id: str) -> str:
        try:
            service.approve(
                custom_id=make_confirm_custom_id(envelope),
                hash_suffix=envelope.hash_suffix,
                interaction_id=interaction_id,
                user_id=USER_ID,
                guild_id=GUILD_ID,
                channel_id=CHANNEL_ID,
            )
        except ApprovalError as exc:
            return exc.code
        return "recorded"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(approve, (INTERACTION_ID, "666666666666666666"))
        )
    assert sorted(results) == ["decision_already_recorded", "recorded"]


def test_receipt_is_published_only_after_complete_fsyncable_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _ = _service(tmp_path)
    envelope = service.current_envelope()
    final_path = service.receipt_path_for(envelope)
    original_link = worker.os.link
    observed: dict[str, object] = {}

    def checked_link(source, destination, **kwargs):
        assert Path(destination) == final_path
        assert not final_path.exists()
        content = Path(source).read_bytes()
        assert content.endswith(b"\n")
        assert json.loads(content)["plan_hash"] == envelope.plan_hash
        observed["complete_before_publish"] = True
        return original_link(source, destination, **kwargs)

    monkeypatch.setattr(worker.os, "link", checked_link)
    service.approve(
        custom_id=make_confirm_custom_id(envelope),
        hash_suffix=envelope.hash_suffix,
        interaction_id=INTERACTION_ID,
        user_id=USER_ID,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
    )

    assert observed == {"complete_before_publish": True}
    assert final_path.stat().st_size > 0
    assert list(final_path.parent.glob(f".{final_path.name}.*.tmp")) == []


def test_receipt_publish_failure_leaves_no_final_tombstone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _ = _service(tmp_path)
    envelope = service.current_envelope()
    final_path = service.receipt_path_for(envelope)

    def fail_link(*args, **kwargs):
        raise OSError("simulated hard-link failure")

    monkeypatch.setattr(worker.os, "link", fail_link)
    _assert_code(
        "approval_receipt_write_failed",
        lambda: service.approve(
            custom_id=make_confirm_custom_id(envelope),
            hash_suffix=envelope.hash_suffix,
            interaction_id=INTERACTION_ID,
            user_id=USER_ID,
            guild_id=GUILD_ID,
            channel_id=CHANNEL_ID,
        ),
    )
    assert not final_path.exists()
    assert list(final_path.parent.glob(f".{final_path.name}.*.tmp")) == []


def test_existing_receipt_symlink_is_never_followed_or_overwritten(
    tmp_path: Path,
) -> None:
    service, runtime = _service(tmp_path)
    envelope = service.current_envelope()
    inbox = runtime / worker.INBOX_NAME
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched", encoding="utf-8")
    try:
        service.receipt_path_for(envelope).symlink_to(victim)
    except OSError:
        pytest.skip("file symlinks are unavailable")

    _assert_code(
        "approval_receipt_invalid",
        lambda: service.approve(
            custom_id=make_confirm_custom_id(envelope),
            hash_suffix=envelope.hash_suffix,
            interaction_id=INTERACTION_ID,
            user_id=USER_ID,
            guild_id=GUILD_ID,
            channel_id=CHANNEL_ID,
        ),
    )
    assert victim.read_text(encoding="utf-8") == "untouched"


def test_invalid_receipt_file_is_reported_instead_of_suppressing_plan(
    tmp_path: Path,
) -> None:
    service, _ = _service(tmp_path)
    envelope = service.current_envelope()
    path = service.receipt_path_for(envelope)
    path.write_text('{"decision":"APPROVE"}', encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)

    _assert_code(
        "approval_receipt_invalid",
        lambda: service.receipt_exists(envelope),
    )


def test_modal_value_extraction_accepts_nested_components_but_not_duplicates() -> None:
    nested = {
        "components": [
            {
                "type": 1,
                "components": [
                    {
                        "custom_id": worker.HASH_INPUT_CUSTOM_ID,
                        "value": "deadbeef",
                    }
                ],
            }
        ]
    }
    assert extract_hash_suffix(nested) == "deadbeef"
    duplicate = {"components": [nested, nested]}
    _assert_code("approval_modal_invalid", lambda: extract_hash_suffix(duplicate))


class _FakeResponse:
    def __init__(self, events: list[str] | None = None) -> None:
        self.modal = None
        self.messages: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.edits: list[dict[str, object]] = []
        self._done = False
        self.events = events if events is not None else []

    def is_done(self) -> bool:
        return self._done

    async def send_modal(self, modal: object) -> None:
        self.events.append("modal")
        self.modal = modal
        self._done = True

    async def defer(self, **kwargs: object) -> None:
        assert kwargs == {"ephemeral": True, "thinking": True}
        self.events.append("defer")
        self._done = True

    async def send_message(self, *args: object, **kwargs: object) -> None:
        self.messages.append((args, kwargs))
        self._done = True

    async def edit_message(self, **kwargs: object) -> None:
        self.edits.append(kwargs)
        self._done = True


class _FakeFollowup:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.sent: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def send(self, *args: object, **kwargs: object) -> None:
        self.events.append("followup")
        self.sent.append((args, kwargs))


class _FakeView:
    def __init__(self, *, timeout: object) -> None:
        self.timeout = timeout
        self.children: list[object] = []

    def add_item(self, item: object) -> None:
        self.children.append(item)


class _FakeModal(_FakeView):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(timeout=kwargs.get("timeout"))
        self.custom_id = kwargs["custom_id"]
        self.title = kwargs["title"]


class _FakeButton:
    def __init__(self, **kwargs: object) -> None:
        self.__dict__.update(kwargs)


class _FakeTextInput(_FakeButton):
    pass


class _FakeClient:
    def __init__(self, **kwargs: object) -> None:
        self.client_kwargs = kwargs
        self.fetched: list[int] = []
        self.channel = None
        self._closed = False

    async def fetch_channel(self, channel_id: int):
        self.fetched.append(channel_id)
        return self.channel

    async def wait_until_ready(self) -> None:
        return None

    def is_closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        self._closed = True


class _FakeChannel:
    def __init__(self) -> None:
        self.id = int(CHANNEL_ID)
        self.guild = SimpleNamespace(id=int(GUILD_ID))
        self.sent: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.raise_after_send = False
        self.error_before_send: BaseException | None = None

    async def send(self, *args: object, **kwargs: object) -> None:
        if self.error_before_send is not None:
            raise self.error_before_send
        self.sent.append((args, kwargs))
        if self.raise_after_send:
            raise TimeoutError("ambiguous send")


class _FakeCommand:
    def __init__(self, callback, **kwargs: object) -> None:
        self.callback = callback
        self.name = kwargs["name"]
        self.description = kwargs["description"]
        self.guild = kwargs["guild"]


class _FakeCommandTree:
    def __init__(self, client: object) -> None:
        self.client = client
        self.commands: list[_FakeCommand] = []
        self.synced: list[object] = []

    def command(self, **kwargs: object):
        def register(callback):
            command = _FakeCommand(callback, **kwargs)
            self.commands.append(command)
            return command

        return register

    async def sync(self, *, guild: object) -> list[_FakeCommand]:
        self.synced.append(guild)
        return self.commands


def _fake_discord_module() -> object:
    allowed_mentions = object()
    return SimpleNamespace(
        Client=_FakeClient,
        Intents=SimpleNamespace(none=lambda: SimpleNamespace(value=0)),
        AllowedMentions=SimpleNamespace(none=lambda: allowed_mentions),
        Object=lambda *, id: SimpleNamespace(id=id),
        app_commands=SimpleNamespace(CommandTree=_FakeCommandTree),
        ButtonStyle=SimpleNamespace(success=3),
        ui=SimpleNamespace(
            View=_FakeView,
            Button=_FakeButton,
            Modal=_FakeModal,
            TextInput=_FakeTextInput,
        ),
    )


def _paper_status_snapshot() -> dict[str, object]:
    return {
        "schema_version": 2,
        "release_sha": "a" * 40,
        "boot_id_hash": "b" * 64,
        "mode": "shadow",
        "live_order_submission": False,
        "updated_at": "2026-08-31T03:00:00+00:00",
        "planner_ready": False,
        "blocker_codes": ["intraday_plan_window_not_started"],
        "run_status": "ACTIVE",
        "start_date": "2026-08-31",
        "end_date": "2026-09-30",
        "initial_cash_usd": "10000",
        "current_cash_usd": "10025.5",
        "final_equity_usd": None,
        "realized_pnl_usd": "25.5",
        "return_fraction": "0.00255",
        "trade_count": 2,
        "wins": 1,
        "losses": 1,
        "win_rate": "0.5",
        "total_fees_usd": "1.2",
        "max_drawdown_usd": "10",
        "max_drawdown_fraction": "0.001",
        "no_entry_count": 0,
        "no_candidate_count": 1,
        "invalid_result_count": 0,
        "unresolved_position_count": 0,
        "waiting_plan_count": 0,
        "coverage_expected_count": 23,
        "coverage_covered_count": 2,
        "coverage_missing_count": 21,
        "latest_day": {
            "session_date": "2026-08-31",
            "symbol": "AAPL",
            "status": "CLOSED",
            "net_pnl_usd": "25.5",
            "fees_usd": "1.2",
            "cash_start_usd": "10000",
            "cash_end_usd": "10025.5",
            "data_gap_count": 0,
        },
    }


def _status_interaction(
    *,
    user_id: str = USER_ID,
    guild_id: str = GUILD_ID,
    channel_id: str = CHANNEL_ID,
    bot: bool = False,
) -> object:
    events: list[str] = []
    return SimpleNamespace(
        user=SimpleNamespace(id=int(user_id), bot=bot),
        guild_id=int(guild_id),
        channel_id=int(channel_id),
        response=_FakeResponse(events),
        followup=_FakeFollowup(events),
    )


def test_status_command_is_guild_scoped_synced_and_uses_no_gateway_intents(
    tmp_path: Path,
) -> None:
    service, _ = _service(tmp_path)
    client = worker.create_discord_client(
        service.config,
        service=service,
        discord_module=_fake_discord_module(),
        expected_release_sha="a" * 40,
    )

    async def scenario() -> None:
        async def no_loop() -> None:
            return None

        client._approval_loop = no_loop
        await client.setup_hook()
        await asyncio.sleep(0)
        assert client.client_kwargs["intents"].value == 0
        assert len(client._command_tree.commands) == 1
        command = client._command_tree.commands[0]
        assert command.name == "현황"
        assert command.guild.id == int(GUILD_ID)
        assert [guild.id for guild in client._command_tree.synced] == [int(GUILD_ID)]
        await client.close()

    asyncio.run(scenario())


def test_status_command_allows_humans_only_in_exact_guild_and_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _ = _service(tmp_path)
    discord = _fake_discord_module()
    reads: list[tuple[Path, str]] = []

    def read(path: Path, *, expected_release_sha: str):
        reads.append((path, expected_release_sha))
        return _paper_status_snapshot()

    monkeypatch.setattr(worker, "read_paper_status", read)
    client = worker.create_discord_client(
        service.config,
        service=service,
        discord_module=discord,
        expected_release_sha="a" * 40,
    )

    async def scenario() -> None:
        for denied in (
            _status_interaction(guild_id=OTHER_ID),
            _status_interaction(channel_id=OTHER_ID),
            _status_interaction(bot=True),
        ):
            await client._handle_paper_status(denied)
            assert denied.response.messages == []
        assert reads == []

        allowed = _status_interaction(user_id=OTHER_ID)
        await client._handle_paper_status(allowed)
        assert len(allowed.response.messages) == 1
        args, kwargs = allowed.response.messages[0]
        message = str(args[0])
        assert "한 달 모의투자 현황" in message
        assert "$10,025.50" in message
        assert "+$25.50" in message
        assert "관망 1" in message
        assert "실주문: 꺼짐" in message
        assert kwargs["ephemeral"] is True
        assert kwargs["allowed_mentions"] is client.client_kwargs["allowed_mentions"]
        assert reads == [(service.config.paper_status_path, "a" * 40)]

    asyncio.run(scenario())


def test_status_renderer_labels_no_candidate_latest_day() -> None:
    snapshot = _paper_status_snapshot()
    snapshot["latest_day"] = {
        "session_date": "2026-08-31",
        "symbol": None,
        "status": "NO_CANDIDATE",
        "net_pnl_usd": "0",
        "fees_usd": "0",
        "cash_start_usd": None,
        "cash_end_usd": None,
        "data_gap_count": 0,
    }

    rendered = worker.render_paper_status(snapshot)

    assert "2026-08-31 · - · 조건 충족 종목 없음" in rendered
    assert "관망 1" in rendered


def test_status_renderer_shows_two_lane_results_and_distinct_sessions() -> None:
    snapshot = _paper_status_snapshot()
    snapshot.update(
        {
            "schema_version": 3,
            "simulation_lanes": 2,
            "distinct_trading_session_count": 1,
            "latest_day": None,
            "lanes": {
                "A": {
                    "status": "ACTIVE",
                    "current_cash_usd": "5000",
                    "realized_pnl_usd": "0",
                    "return_fraction": "0",
                    "trade_count": 1,
                    "no_candidate_count": 0,
                    "invalid_result_count": 0,
                    "unresolved_position_count": 0,
                    "coverage_covered_count": 1,
                    "coverage_missing_count": 22,
                    "latest_day": {
                        "session_date": "2026-08-31",
                        "symbol": "AAPL",
                        "status": "CLOSED",
                        "net_pnl_usd": "0",
                        "fees_usd": "0",
                        "cash_start_usd": "5000",
                        "cash_end_usd": "5000",
                        "data_gap_count": 0,
                    },
                },
                "B": {
                    "status": "ACTIVE",
                    "current_cash_usd": "5025.5",
                    "realized_pnl_usd": "25.5",
                    "return_fraction": "0.0051",
                    "trade_count": 1,
                    "no_candidate_count": 0,
                    "invalid_result_count": 0,
                    "unresolved_position_count": 0,
                    "coverage_covered_count": 1,
                    "coverage_missing_count": 22,
                    "latest_day": {
                        "session_date": "2026-08-31",
                        "symbol": "MSFT",
                        "status": "CLOSED",
                        "net_pnl_usd": "25.5",
                        "fees_usd": "1.2",
                        "cash_start_usd": "5000",
                        "cash_end_usd": "5025.5",
                        "data_gap_count": 0,
                    },
                },
            },
        }
    )

    rendered = worker.render_paper_status(snapshot)

    assert "2레인 한 달 모의투자 현황" in rendered
    assert "서로 다른 거래일: 1일" in rendered
    assert "레인 A:" in rendered and "최근 A:" in rendered
    assert "레인 B:" in rendered and "최근 B:" in rendered


def test_status_command_hides_reader_failures_in_allowed_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _ = _service(tmp_path)

    def fail(*args: object, **kwargs: object):
        raise worker.PaperStatusError("paper_status_stale")

    monkeypatch.setattr(worker, "read_paper_status", fail)
    client = worker.create_discord_client(
        service.config,
        service=service,
        discord_module=_fake_discord_module(),
        expected_release_sha="a" * 40,
    )
    interaction = _status_interaction()
    asyncio.run(client._handle_paper_status(interaction))

    assert interaction.response.messages[0][0] == (
        "현황을 확인할 수 없습니다. 잠시 후 다시 시도해 주세요.",
    )
    assert interaction.response.messages[0][1]["ephemeral"] is True


def test_gateway_loop_reports_sanitized_error_once(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    service, _ = _service(tmp_path)
    heartbeats: list[str] = []
    client = worker.create_discord_client(
        service.config,
        service=service,
        discord_module=_fake_discord_module(),
        heartbeat=heartbeats.append,
    )
    attempts = 0

    async def fail_publish() -> None:
        nonlocal attempts
        attempts += 1
        raise ApprovalError("discord_channel_fetch_failed")

    async def stop_after_two(_: float) -> None:
        if attempts >= 2:
            client._closed = True

    client._publish_current = fail_publish
    monkeypatch.setattr(worker.asyncio, "sleep", stop_after_two)

    asyncio.run(client._approval_loop())

    assert attempts == 2
    assert capsys.readouterr().err == (
        "approval_loop_error:discord_channel_fetch_failed\n"
    )
    assert heartbeats == ["DEGRADED", "DEGRADED"]


def _interaction(
    *,
    custom_id: str,
    kind: int,
    user_id: str = USER_ID,
    suffix: str | None = None,
    events: list[str] | None = None,
) -> object:
    interaction_events = events if events is not None else []
    data: dict[str, object] = {"custom_id": custom_id}
    if suffix is not None:
        data["components"] = [
            {
                "components": [
                    {
                        "custom_id": worker.HASH_INPUT_CUSTOM_ID,
                        "value": suffix,
                    }
                ]
            }
        ]
    return SimpleNamespace(
        id=int(INTERACTION_ID),
        user=SimpleNamespace(id=int(user_id)),
        guild_id=int(GUILD_ID),
        channel_id=int(CHANNEL_ID),
        type=SimpleNamespace(value=kind),
        data=data,
        response=_FakeResponse(interaction_events),
        followup=_FakeFollowup(interaction_events),
    )


def test_gateway_client_routes_dynamic_components_after_restart_and_stays_in_channel(
    tmp_path: Path,
) -> None:
    service, _ = _service(tmp_path)
    envelope = service.current_envelope()
    discord = _fake_discord_module()
    approval_events: list[str] = []
    original_approve = service.approve

    def recording_approve(**kwargs: object):
        approval_events.append("approve")
        return original_approve(**kwargs)

    service.approve = recording_approve  # type: ignore[method-assign]

    async def scenario() -> None:
        publishing_client = worker.create_discord_client(
            service.config, service=service, discord_module=discord
        )
        assert publishing_client.client_kwargs["intents"].value == 0
        channel = _FakeChannel()
        publishing_client.channel = channel
        await publishing_client._publish_current()
        assert publishing_client.fetched == [int(CHANNEL_ID)]
        assert len(channel.sent) == 1
        sent_view = channel.sent[0][1]["view"]
        button_custom_id = sent_view.children[0].custom_id

        # A new client has no registered view state, but routes the signed dynamic ID.
        restarted_client = worker.create_discord_client(
            service.config, service=service, discord_module=discord
        )
        denied = _interaction(
            custom_id=button_custom_id, kind=3, user_id=OTHER_ID
        )
        await restarted_client.on_interaction(denied)
        assert denied.response.modal is None
        assert denied.response.messages == []

        component = _interaction(custom_id=button_custom_id, kind=3)
        await restarted_client.on_interaction(component)
        assert component.response.modal is not None
        assert component.response.modal.custom_id == make_confirm_custom_id(envelope)

        rejected_modal = _interaction(
            custom_id=component.response.modal.custom_id,
            kind=5,
            suffix="00000000",
            events=approval_events,
        )
        await restarted_client.on_interaction(rejected_modal)
        assert approval_events == ["defer", "approve", "followup"]
        assert rejected_modal.followup.sent[0][1]["ephemeral"] is True
        assert not service.receipt_exists(envelope)
        approval_events.clear()

        modal = _interaction(
            custom_id=component.response.modal.custom_id,
            kind=5,
            suffix=envelope.hash_suffix,
            events=approval_events,
        )
        await restarted_client.on_interaction(modal)
        assert approval_events == ["defer", "approve", "followup"]
        assert modal.response.edits == []
        assert len(modal.followup.sent) == 1
        assert modal.followup.sent[0][1]["ephemeral"] is True
        assert service.receipt_exists(envelope)

    asyncio.run(scenario())


def test_ambiguous_discord_send_is_not_retried_in_the_same_process(
    tmp_path: Path,
) -> None:
    service, _ = _service(tmp_path)
    discord = _fake_discord_module()

    async def scenario() -> None:
        client = worker.create_discord_client(
            service.config, service=service, discord_module=discord
        )
        channel = _FakeChannel()
        channel.raise_after_send = True
        client.channel = channel
        with pytest.raises(ApprovalError) as captured:
            await client._publish_current()
        assert captured.value.code == "discord_send_failed"

        await client._publish_current()
        assert len(channel.sent) == 1
        assert client.fetched == [int(CHANNEL_ID)]

    asyncio.run(scenario())


def test_definitive_discord_4xx_is_retried_in_the_same_process(
    tmp_path: Path,
) -> None:
    service, _ = _service(tmp_path)
    discord = _fake_discord_module()

    class DefinitiveDiscordError(RuntimeError):
        status = 403

    async def scenario() -> None:
        client = worker.create_discord_client(
            service.config, service=service, discord_module=discord
        )
        channel = _FakeChannel()
        channel.error_before_send = DefinitiveDiscordError("forbidden")
        client.channel = channel
        with pytest.raises(ApprovalError):
            await client._publish_current()
        assert channel.sent == []

        channel.error_before_send = None
        await client._publish_current()
        assert len(channel.sent) == 1
        assert client.fetched == [int(CHANNEL_ID), int(CHANNEL_ID)]

    asyncio.run(scenario())


def test_changed_display_fields_publish_a_new_bound_message(
    tmp_path: Path,
) -> None:
    service, runtime = _service(tmp_path)
    discord = _fake_discord_module()

    async def scenario() -> None:
        client = worker.create_discord_client(
            service.config, service=service, discord_module=discord
        )
        channel = _FakeChannel()
        client.channel = channel
        await client._publish_current()
        first_custom_id = channel.sent[0][1]["view"].children[0].custom_id

        _write_envelope(runtime, quantity=6)
        await client._publish_current()
        second_custom_id = channel.sent[1][1]["view"].children[0].custom_id

        assert len(channel.sent) == 2
        assert first_custom_id != second_custom_id

    asyncio.run(scenario())


def test_discord_dependency_is_lazy_and_package_has_no_trading_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _ = _service(tmp_path)
    assert "discord" not in worker.__dict__
    assert "turtle_bot" not in worker.__dict__

    def missing(name: str) -> object:
        assert name == "discord"
        raise ImportError(name)

    monkeypatch.setattr(worker.importlib, "import_module", missing)
    _assert_code(
        "discord_dependency_missing",
        lambda: worker.create_discord_client(service.config),
    )


def test_v2_envelope_uses_one_canonical_hash_contract_and_complete_display() -> None:
    mapping = _v2_envelope()
    envelope = worker.ApprovalEnvelopeV2.from_mapping(mapping)

    assert envelope.to_mapping() == mapping
    assert envelope.expected_interaction_binding == mapping["interaction_binding"]
    assert envelope.envelope_sha256 == worker.hashlib.sha256(
        worker.canonical_json_bytes(mapping)
    ).hexdigest()
    assert worker.ApprovalEnvelopeV2.from_json_bytes(
        worker.canonical_json_bytes(mapping)
    ) == envelope
    assert not worker.canonical_json_bytes(mapping).endswith(b"\n")

    message = worker.render_approval_v2_message(envelope)
    assert worker._discord_plain_text(envelope.account_alias) in message
    for displayed in (
        envelope.symbol,
        envelope.quantity,
        envelope.entry_trigger,
        envelope.entry_limit,
        envelope.target_trigger,
        envelope.target_limit,
        envelope.stop_trigger,
        envelope.stop_limit,
        envelope.cash_reserved,
        envelope.planned_risk,
        envelope.planned_reward,
        envelope.entry_start,
        envelope.entry_expiry,
        envelope.force_exit_at,
        str(envelope.protection_slo_seconds),
        str(envelope.exit_fill_slo_seconds),
        envelope.plan_hash[-worker.HASH_SUFFIX_LENGTH :],
        "MARKET",
        "보장되지",
    ):
        assert displayed in message


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"unexpected": "extension"}, "approval_v2_envelope_schema_invalid"),
        ({"schema_version": True}, "approval_v2_envelope_schema_invalid"),
        ({"schema_version": 2.0}, "approval_v2_envelope_schema_invalid"),
        ({"purpose": "SHADOW"}, "approval_v2_envelope_schema_invalid"),
        ({"quantity": 5}, "approval_v2_quantity_invalid"),
        ({"quantity": "05"}, "approval_v2_quantity_invalid"),
        ({"entry_trigger": "2E2"}, "approval_v2_entry_trigger_invalid"),
        ({"entry_trigger": "200.0"}, "approval_v2_entry_trigger_invalid"),
        ({"entry_trigger": "NaN"}, "approval_v2_entry_trigger_invalid"),
        ({"protection_slo_seconds": True}, "approval_v2_protection_slo_invalid"),
        ({"writer_fence": "7"}, "approval_v2_writer_fence_invalid"),
        (
            {
                "emergency_exit": {
                    "policy": "MARKET_ALL_REMAINING_OWNED",
                    "regular_session_only": 1,
                    "price_not_guaranteed": True,
                }
            },
            "approval_v2_envelope_schema_invalid",
        ),
        ({"issued_at": "2026-08-29T00:25:00"}, "approval_v2_issued_at_invalid"),
    ],
)
def test_v2_envelope_rejects_extensions_wrong_types_and_noncanonical_numbers(
    change: dict[str, object], code: str
) -> None:
    _assert_code(
        code,
        lambda: worker.ApprovalEnvelopeV2.from_mapping(_v2_envelope(**change)),
    )


def test_v2_envelope_rejects_duplicate_keys_and_binding_tamper() -> None:
    duplicate = b'{"schema_version":2,"schema_version":2}'
    _assert_code(
        "approval_v2_envelope_invalid",
        lambda: worker.ApprovalEnvelopeV2.from_json_bytes(duplicate),
    )
    mapping = _v2_envelope()
    mapping["interaction_binding"] = "0" * 64
    _assert_code(
        "approval_v2_interaction_binding_mismatch",
        lambda: worker.ApprovalEnvelopeV2.from_mapping(mapping),
    )


def test_v2_receipt_is_minimal_canonical_and_bound_to_current_generation() -> None:
    envelope = worker.ApprovalEnvelopeV2.from_mapping(_v2_envelope())
    receipt = worker.ApprovalReceiptV2.create(
        envelope,
        discord_guild_id=GUILD_ID,
        discord_channel_id=CHANNEL_ID,
        discord_user_id=USER_ID,
        interaction_id=INTERACTION_ID,
        decided_at=NOW,
    )
    raw = receipt.canonical_bytes

    assert set(receipt.to_mapping()) == worker._V2_RECEIPT_KEYS
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert not raw.endswith(b"\n")
    assert envelope.nonce.encode("ascii") not in raw
    assert worker.ApprovalReceiptV2.from_json_bytes(raw) == receipt
    assert worker.verify_approval_receipt_v2(
        receipt,
        envelope,
        discord_guild_id=GUILD_ID,
        discord_channel_id=CHANNEL_ID,
        discord_user_id=USER_ID,
        current_boot_id_hash=envelope.boot_id_hash,
        writer_fence=envelope.writer_fence,
        approval_generation=envelope.approval_generation,
        now=NOW,
    ) == worker.hashlib.sha256(raw).hexdigest()


def test_v2_receipt_strict_schema_and_every_external_binding_fail_closed() -> None:
    envelope = worker.ApprovalEnvelopeV2.from_mapping(_v2_envelope())
    receipt = worker.ApprovalReceiptV2.create(
        envelope,
        discord_guild_id=GUILD_ID,
        discord_channel_id=CHANNEL_ID,
        discord_user_id=USER_ID,
        interaction_id=INTERACTION_ID,
        decided_at=NOW,
    )
    invalid = receipt.to_mapping()
    invalid["extra"] = True
    _assert_code(
        "approval_v2_receipt_schema_invalid",
        lambda: worker.ApprovalReceiptV2.from_mapping(invalid),
    )
    numeric_id = receipt.to_mapping()
    numeric_id["interaction_id"] = int(INTERACTION_ID)
    _assert_code(
        "approval_v2_receipt_invalid",
        lambda: worker.ApprovalReceiptV2.from_mapping(numeric_id),
    )
    duplicate = receipt.canonical_bytes[:-1] + b',"decision":"APPROVE"}'
    _assert_code(
        "approval_v2_receipt_invalid",
        lambda: worker.ApprovalReceiptV2.from_json_bytes(duplicate),
    )

    common = {
        "discord_guild_id": GUILD_ID,
        "discord_channel_id": CHANNEL_ID,
        "discord_user_id": USER_ID,
        "current_boot_id_hash": envelope.boot_id_hash,
        "writer_fence": envelope.writer_fence,
        "approval_generation": envelope.approval_generation,
        "now": NOW,
    }
    for change, code in (
        ({"discord_guild_id": OTHER_ID}, "approval_v2_receipt_binding_mismatch"),
        ({"discord_channel_id": OTHER_ID}, "approval_v2_receipt_binding_mismatch"),
        ({"discord_user_id": OTHER_ID}, "approval_v2_receipt_binding_mismatch"),
        ({"current_boot_id_hash": "0" * 64}, "approval_v2_receipt_binding_mismatch"),
        ({"writer_fence": envelope.writer_fence + 1}, "approval_v2_receipt_generation_mismatch"),
        ({"approval_generation": envelope.approval_generation + 1}, "approval_v2_receipt_generation_mismatch"),
        ({"now": NOW + timedelta(minutes=5)}, "approval_v2_expired"),
    ):
        arguments = dict(common)
        arguments.update(change)
        _assert_code(
            code,
            lambda arguments=arguments: worker.verify_approval_receipt_v2(
                receipt, envelope, **arguments
            ),
        )


def test_v2_boot_id_is_uuid_canonical_and_never_uses_a_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = "{A8098C1A-F86E-11DA-BD1A-00112444BE1E}"
    expected = worker.hashlib.sha256(
        b"macos-boot-v1\0a8098c1a-f86e-11da-bd1a-00112444be1e"
    ).hexdigest()
    assert worker.hash_macos_boot_session_uuid(f"  {raw}\n") == expected
    _assert_code(
        "approval_v2_boot_id_unavailable",
        lambda: worker.hash_macos_boot_session_uuid("not-a-uuid"),
    )
    monkeypatch.setattr(worker.sys, "platform", "linux")
    _assert_code(
        "approval_v2_boot_id_unavailable", worker.read_macos_boot_id_hash
    )


def _v2_mailbox_policy(
    *,
    writer_uid: int = 1001,
    reader_uid: int = 1002,
    file_gid: int = 2002,
    anchor_uid: int = 0,
    anchor_mode: int = 0o755,
    inbox_mode: int = 0o750,
) -> worker.CrossUidReceiptMailbox:
    root = PurePosixPath("/")
    secure = root / "secure"
    anchor = secure / "approval-v2"
    inbox = anchor / "approval-inbox"
    return worker.CrossUidReceiptMailbox(
        directories=(
            worker.DirectoryIdentity(root, 0, 0, 0o755),
            worker.DirectoryIdentity(secure, 0, 0, 0o755),
            worker.DirectoryIdentity(anchor, anchor_uid, 0, anchor_mode),
            worker.DirectoryIdentity(inbox, writer_uid, file_gid, inbox_mode),
        ),
        anchor_path=anchor,
        writer_uid=writer_uid,
        reader_uid=reader_uid,
        file_gid=file_gid,
    )


def test_v2_mailbox_policy_requires_distinct_uids_root_anchor_and_exact_modes() -> None:
    policy = _v2_mailbox_policy()
    assert policy.path.name == "approval-inbox"
    for changes in (
        {"reader_uid": 1001},
        {"anchor_uid": 1001},
        {"anchor_mode": 0o775},
        {"inbox_mode": 0o770},
    ):
        _assert_code(
            "approval_v2_mailbox_policy_invalid",
            lambda changes=changes: _v2_mailbox_policy(**changes),
        )


@pytest.mark.parametrize(
    "change",
    [
        {"st_mode": stat.S_IFLNK | 0o640},
        {"st_uid": 9000},
        {"st_gid": 9000},
        {"st_mode": stat.S_IFREG | 0o660},
        {"st_nlink": 3},
        {"st_size": worker.MAX_V2_RECEIPT_BYTES + 1},
    ],
)
def test_v2_receipt_metadata_rejects_symlink_owner_group_mode_and_hardlink(
    change: dict[str, int],
) -> None:
    policy = _v2_mailbox_policy()
    values = {
        "st_mode": stat.S_IFREG | 0o640,
        "st_uid": policy.writer_uid,
        "st_gid": policy.file_gid,
        "st_size": 100,
        "st_nlink": 1,
    }
    values.update(change)
    info = SimpleNamespace(**values)
    _assert_code(
        "approval_v2_receipt_file_invalid",
        lambda: worker._validate_v2_receipt_stat(
            info, policy, allowed_links=frozenset({1})
        ),
    )


def test_v2_non_apfs_or_remote_filesystem_is_never_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker.sys, "platform", "linux")
    _assert_code(
        "approval_v2_mailbox_filesystem_invalid",
        lambda: worker._require_local_apfs(-1),
    )


@pytest.mark.skipif(os.name != "posix", reason="fd-relative openat test")
def test_v2_after_link_crash_is_recovered_only_by_same_inode_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer_uid = os.getuid()
    policy = _v2_mailbox_policy(
        writer_uid=writer_uid,
        reader_uid=writer_uid + 1,
        file_gid=os.getgid(),
    )

    @worker.contextmanager
    def open_test_mailbox(self):
        descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            yield descriptor
        finally:
            os.close(descriptor)

    monkeypatch.setattr(
        worker.CrossUidReceiptMailbox, "open_for_writer", open_test_mailbox
    )
    envelope = worker.ApprovalEnvelopeV2.from_mapping(_v2_envelope())
    receipt = worker.ApprovalReceiptV2.create(
        envelope,
        discord_guild_id=GUILD_ID,
        discord_channel_id=CHANNEL_ID,
        discord_user_id=USER_ID,
        interaction_id=INTERACTION_ID,
        decided_at=NOW,
    )

    class SimulatedCrash(RuntimeError):
        pass

    with pytest.raises(SimulatedCrash):
        worker.publish_approval_receipt_v2(
            policy,
            receipt,
            after_link_before_temp_unlink=lambda: (_ for _ in ()).throw(
                SimulatedCrash()
            ),
        )
    final = tmp_path / f"{receipt.nonce_sha256}.json"
    temporary = next(tmp_path.glob(f".{final.name}.*.tmp"))
    assert final.stat().st_ino == temporary.stat().st_ino
    assert final.stat().st_nlink == 2

    assert worker.recover_approval_receipt_publish_v2(policy, final.name) is True
    assert final.stat().st_nlink == 1
    assert not temporary.exists()

    malicious = tmp_path / f".{final.name}.{'0' * 24}.tmp"
    malicious.write_bytes(receipt.canonical_bytes)
    malicious.chmod(0o640)
    _assert_code(
        "approval_v2_publish_recovery_invalid",
        lambda: worker.recover_approval_receipt_publish_v2(policy, final.name),
    )
    assert malicious.exists()
    malicious.unlink()

    second_envelope = worker.ApprovalEnvelopeV2.from_mapping(
        _v2_envelope(nonce="approval_nonce_second_abcdefghi")
    )
    second_receipt = worker.ApprovalReceiptV2.create(
        second_envelope,
        discord_guild_id=GUILD_ID,
        discord_channel_id=CHANNEL_ID,
        discord_user_id=USER_ID,
        interaction_id="666666666666666666",
        decided_at=NOW,
    )
    victim = tmp_path / "victim"
    victim.write_text("untouched", encoding="utf-8")
    symlink = tmp_path / f"{second_receipt.nonce_sha256}.json"
    symlink.symlink_to(victim)
    _assert_code(
        "approval_v2_receipt_file_invalid",
        lambda: worker.publish_approval_receipt_v2(policy, second_receipt),
    )
    assert victim.read_text(encoding="utf-8") == "untouched"


@pytest.mark.skipif(os.name != "posix", reason="fd-relative openat test")
def test_v2_consumer_bounds_nlink_two_pending_without_cleaning_peer_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer_uid = os.getuid()
    policy = _v2_mailbox_policy(
        writer_uid=writer_uid,
        reader_uid=writer_uid + 1,
        file_gid=os.getgid(),
    )

    @worker.contextmanager
    def open_test_mailbox(self):
        descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            yield descriptor
        finally:
            os.close(descriptor)

    monkeypatch.setattr(
        worker.CrossUidReceiptMailbox, "open_for_writer", open_test_mailbox
    )
    monkeypatch.setattr(
        worker.CrossUidReceiptMailbox, "open_for_reader", open_test_mailbox
    )
    envelope = worker.ApprovalEnvelopeV2.from_mapping(_v2_envelope())
    receipt = worker.ApprovalReceiptV2.create(
        envelope,
        discord_guild_id=GUILD_ID,
        discord_channel_id=CHANNEL_ID,
        discord_user_id=USER_ID,
        interaction_id=INTERACTION_ID,
        decided_at=NOW,
    )

    class SimulatedCrash(RuntimeError):
        pass

    with pytest.raises(SimulatedCrash):
        worker.publish_approval_receipt_v2(
            policy,
            receipt,
            after_link_before_temp_unlink=lambda: (_ for _ in ()).throw(
                SimulatedCrash()
            ),
        )
    final = tmp_path / f"{receipt.nonce_sha256}.json"
    temporary = next(tmp_path.glob(f".{final.name}.*.tmp"))
    current = [0.0]
    waits: list[float] = []

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        current[0] += seconds

    _assert_code(
        "APPROVAL_PUBLISH_PENDING",
        lambda: worker.read_approval_receipt_v2(
            policy,
            final.name,
            monotonic=lambda: current[0],
            sleeper=sleep,
        ),
    )
    assert waits == [15.0, 15.0]
    assert final.stat().st_nlink == 2
    assert temporary.exists()
