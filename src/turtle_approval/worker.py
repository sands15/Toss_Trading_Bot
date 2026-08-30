from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib
import json
import os
import re
import secrets
import stat
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from turtle_runtime.paper_status import (
    PaperStatusError,
    derive_paper_status_path,
    read_paper_status,
)


TOKEN_ENV = "DISCORD_APPROVAL_BOT_TOKEN"
GUILD_ENV = "DISCORD_ALLOWED_GUILD_ID"
CHANNEL_ENV = "DISCORD_ALLOWED_CHANNEL_ID"
USER_ENV = "DISCORD_ALLOWED_USER_ID"
ENVELOPE_PATH_ENV = "DISCORD_APPROVAL_ENVELOPE_PATH"
INBOX_DIR_ENV = "DISCORD_APPROVAL_INBOX_DIR"

FORBIDDEN_ENV_KEYS = frozenset(
    {
        "DISCORD_TRADE_ALERT_WEBHOOK_URL",
        "TURTLE_STATE_DB",
    }
)
_FORBIDDEN_ENV_PREFIXES = (
    "TOSS_",
    "TURTLE_",
    "FINNHUB_",
    "LLM_",
    "LOCAL_LLM_",
    "OLLAMA_",
    "OPENAI_",
    "ANTHROPIC_",
    "GEMINI_",
)
ENVELOPE_NAME = "approval-envelope.json"
INBOX_NAME = "approval-inbox"
MAX_ENVELOPE_BYTES = 16_384
MAX_RECEIPT_BYTES = 16_384
MAX_V2_ENVELOPE_BYTES = 32_768
MAX_V2_RECEIPT_BYTES = 32_768
APPROVAL_PUBLISH_PENDING_SECONDS = 30
APPROVAL_PUBLISH_PENDING_ATTEMPTS = 3
HASH_SUFFIX_LENGTH = 8
HASH_INPUT_CUSTOM_ID = "ta:hash-suffix"

_SNOWFLAKE_RE = re.compile(r"[1-9][0-9]{16,19}\Z")
_PLAN_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,80}\Z")
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_NONCE_RE = re.compile(r"[A-Za-z0-9_-]{20,80}\Z")
_SYMBOL_RE = re.compile(r"[A-Z][A-Z0-9]*(?:[.-][A-Z0-9]+)?\Z")
_CUSTOM_ID_RE = re.compile(r"ta:(?P<action>[ac]):(?P<binding>[0-9a-f]{64})\Z")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_HASH_SUFFIX_RE = re.compile(rf"[0-9a-fA-F]{{{HASH_SUFFIX_LENGTH}}}\Z")
_CANONICAL_POSITIVE_INTEGER_RE = re.compile(r"[1-9][0-9]*\Z")
_CANONICAL_POSITIVE_DECIMAL_RE = re.compile(
    r"(?:[1-9][0-9]*(?:\.[0-9]*[1-9])?|0\.[0-9]*[1-9])\Z"
)
_V2_RECEIPT_NAME_RE = re.compile(r"[0-9a-f]{64}\.json\Z")
_V2_TEMP_NAME_RE = re.compile(
    r"\.(?P<final>[0-9a-f]{64}\.json)\.(?P<token>[0-9a-f]{24})\.tmp\Z"
)

_ENVELOPE_KEYS = frozenset(
    {
        "schema_version",
        "generated_at",
        "expires_at",
        "session_date",
        "plan_id",
        "plan_hash",
        "nonce",
        "account_alias",
        "mode",
        "live_order_submission",
        "symbol",
        "allocated_cash",
        "quantity",
        "entry_trigger",
        "entry_limit",
        "target_trigger",
        "stop_trigger",
        "stop_limit",
        "planned_risk",
        "reward_risk_ratio",
    }
)
_DECIMAL_KEYS = (
    "allocated_cash",
    "entry_trigger",
    "entry_limit",
    "target_trigger",
    "stop_trigger",
    "stop_limit",
    "planned_risk",
    "reward_risk_ratio",
)
_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "decision",
        "plan_id",
        "plan_hash",
        "interaction_binding",
        "nonce_sha256",
        "expires_at",
        "decided_at",
        "discord_user_id",
        "guild_id",
        "channel_id",
        "interaction_id",
    }
)

_V2_ENVELOPE_KEYS = frozenset(
    {
        "schema_version",
        "purpose",
        "plan_id",
        "plan_hash",
        "account_alias",
        "session_date",
        "symbol",
        "quantity",
        "entry_trigger",
        "entry_limit",
        "target_trigger",
        "target_limit",
        "stop_trigger",
        "stop_limit",
        "cash_reserved",
        "planned_risk",
        "planned_reward",
        "entry_start",
        "entry_expiry",
        "force_exit_at",
        "protection_slo_seconds",
        "exit_fill_slo_seconds",
        "emergency_exit",
        "boot_id_hash",
        "writer_fence",
        "approval_generation",
        "nonce",
        "issued_at",
        "expires_at",
        "interaction_binding",
    }
)
_V2_ECONOMIC_KEYS = (
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
_V2_EMERGENCY_EXIT = {
    "policy": "MARKET_ALL_REMAINING_OWNED",
    "regular_session_only": True,
    "price_not_guaranteed": True,
}
_V2_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "purpose",
        "decision",
        "plan_id",
        "plan_hash",
        "interaction_binding",
        "approval_generation",
        "writer_fence",
        "boot_id_hash",
        "nonce_sha256",
        "discord_guild_id",
        "discord_channel_id",
        "discord_user_id",
        "interaction_id",
        "decided_at",
        "expires_at",
        "envelope_sha256",
    }
)


class ApprovalError(RuntimeError):
    """Expected fail-closed error whose code is safe to display or log."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _is_forbidden_capability_env(name: object) -> bool:
    if not isinstance(name, str):
        return True
    normalized = name.upper()
    return (
        normalized in FORBIDDEN_ENV_KEYS
        or normalized.endswith("_WEBHOOK_URL")
        or normalized.startswith(_FORBIDDEN_ENV_PREFIXES)
    )


def _required_env(env: Mapping[str, str], name: str, code: str) -> str:
    value = env.get(name)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 2_000
        or value != value.strip()
        or any(char.isspace() for char in value)
        or _CONTROL_RE.search(value)
    ):
        raise ApprovalError(code)
    return value


def _required_path_env(env: Mapping[str, str], name: str, code: str) -> str:
    value = env.get(name)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4_096
        or value != value.strip()
        or _CONTROL_RE.search(value)
    ):
        raise ApprovalError(code)
    return value


def _snowflake(value: object, code: str) -> str:
    clean = str(value) if isinstance(value, int) and not isinstance(value, bool) else value
    if not isinstance(clean, str) or not _SNOWFLAKE_RE.fullmatch(clean):
        raise ApprovalError(code)
    return clean


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.fspath(left)) == os.path.normcase(os.fspath(right))


def require_private_directory(path: Path, code: str) -> Path:
    if not path.is_absolute():
        raise ApprovalError(code)
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ApprovalError(code) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ApprovalError(code)
    if not _same_path(resolved, path):
        raise ApprovalError(code)
    if os.name != "nt":
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise ApprovalError(code)
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise ApprovalError(code)
    return path


@dataclass(frozen=True)
class ApprovalConfig:
    bot_token: str = field(repr=False)
    guild_id: str
    channel_id: str
    allowed_user_id: str
    envelope_path: Path
    inbox_dir: Path
    poll_interval_seconds: float = 5.0

    @property
    def paper_status_path(self) -> Path:
        try:
            return derive_paper_status_path(self.envelope_path)
        except PaperStatusError as exc:
            raise ApprovalError("paper_status_path_invalid") from exc

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ApprovalConfig:
        values = os.environ if env is None else env
        if any(_is_forbidden_capability_env(name) for name in values):
            raise ApprovalError("trading_capability_in_approval_environment")

        token = _required_env(values, TOKEN_ENV, "discord_bot_token_missing")
        guild_id = _snowflake(
            _required_env(values, GUILD_ENV, "discord_guild_id_missing"),
            "discord_guild_id_invalid",
        )
        channel_id = _snowflake(
            _required_env(values, CHANNEL_ENV, "discord_channel_id_missing"),
            "discord_channel_id_invalid",
        )
        allowed_user_id = _snowflake(
            _required_env(values, USER_ENV, "discord_user_id_missing"),
            "discord_user_id_invalid",
        )

        envelope_path = Path(
            _required_path_env(
                values, ENVELOPE_PATH_ENV, "approval_envelope_path_missing"
            )
        ).expanduser()
        if not envelope_path.is_absolute() or envelope_path.name != ENVELOPE_NAME:
            raise ApprovalError("approval_envelope_path_invalid")
        require_private_directory(
            envelope_path.parent, "approval_envelope_directory_invalid"
        )
        try:
            envelope_info = envelope_path.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ApprovalError("approval_envelope_path_invalid") from exc
        else:
            if stat.S_ISLNK(envelope_info.st_mode) or not stat.S_ISREG(
                envelope_info.st_mode
            ):
                raise ApprovalError("approval_envelope_path_invalid")

        inbox_dir = Path(
            _required_path_env(values, INBOX_DIR_ENV, "approval_inbox_dir_missing")
        ).expanduser()
        inbox_dir = require_private_directory(
            inbox_dir, "approval_inbox_dir_invalid"
        )
        return cls(
            bot_token=token,
            guild_id=guild_id,
            channel_id=channel_id,
            allowed_user_id=allowed_user_id,
            envelope_path=envelope_path,
            inbox_dir=inbox_dir,
        )


def _aware_datetime(value: object, code: str) -> datetime:
    if not isinstance(value, str) or len(value) > 64 or _CONTROL_RE.search(value):
        raise ApprovalError(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApprovalError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ApprovalError(code)
    return parsed.astimezone(timezone.utc)


def _positive_decimal(value: object, code: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ApprovalError(code)
    raw = str(value)
    if not raw or len(raw) > 64 or _CONTROL_RE.search(raw):
        raise ApprovalError(code)
    try:
        number = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise ApprovalError(code) from exc
    if not number.is_finite() or number <= 0:
        raise ApprovalError(code)
    if number.adjusted() > 18 or number.adjusted() < -12:
        raise ApprovalError(code)
    return number


def _clean_text(value: object, *, maximum: int, code: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or _CONTROL_RE.search(value)
    ):
        raise ApprovalError(code)
    return value


@dataclass(frozen=True)
class ApprovalEnvelope:
    generated_at: datetime
    expires_at: datetime
    session_date: date
    plan_id: str
    plan_hash: str
    nonce: str
    account_alias: str
    symbol: str
    allocated_cash: Decimal
    quantity: int
    entry_trigger: Decimal
    entry_limit: Decimal
    target_trigger: Decimal
    stop_trigger: Decimal
    stop_limit: Decimal
    planned_risk: Decimal
    reward_risk_ratio: Decimal

    @classmethod
    def from_mapping(cls, value: object) -> ApprovalEnvelope:
        if not isinstance(value, Mapping) or set(value) != _ENVELOPE_KEYS:
            raise ApprovalError("approval_envelope_schema_invalid")
        if (
            value.get("schema_version") != 1
            or value.get("mode") != "shadow"
            or value.get("live_order_submission") is not False
        ):
            raise ApprovalError("approval_envelope_schema_invalid")

        generated_at = _aware_datetime(
            value.get("generated_at"), "approval_envelope_generated_at_invalid"
        )
        expires_at = _aware_datetime(
            value.get("expires_at"), "approval_envelope_expiry_invalid"
        )
        if generated_at >= expires_at:
            raise ApprovalError("approval_envelope_expiry_invalid")
        raw_session_date = value.get("session_date")
        if not isinstance(raw_session_date, str) or len(raw_session_date) != 10:
            raise ApprovalError("approval_envelope_session_date_invalid")
        try:
            session_date = date.fromisoformat(raw_session_date)
        except ValueError as exc:
            raise ApprovalError("approval_envelope_session_date_invalid") from exc

        plan_id = _clean_text(
            value.get("plan_id"), maximum=80, code="approval_plan_id_invalid"
        )
        if not _PLAN_ID_RE.fullmatch(plan_id):
            raise ApprovalError("approval_plan_id_invalid")
        plan_hash = _clean_text(
            value.get("plan_hash"), maximum=64, code="approval_plan_hash_invalid"
        )
        if not _HASH_RE.fullmatch(plan_hash):
            raise ApprovalError("approval_plan_hash_invalid")
        nonce = _clean_text(
            value.get("nonce"), maximum=80, code="approval_nonce_invalid"
        )
        if not _NONCE_RE.fullmatch(nonce):
            raise ApprovalError("approval_nonce_invalid")
        account_alias = _clean_text(
            value.get("account_alias"),
            maximum=80,
            code="approval_account_alias_invalid",
        )
        symbol = _clean_text(
            value.get("symbol"), maximum=24, code="approval_symbol_invalid"
        )
        if not _SYMBOL_RE.fullmatch(symbol):
            raise ApprovalError("approval_symbol_invalid")

        quantity = value.get("quantity")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 1_000_000_000:
            raise ApprovalError("approval_quantity_invalid")
        decimals = {
            key: _positive_decimal(
                value.get(key), f"approval_{key}_invalid"
            )
            for key in _DECIMAL_KEYS
        }
        return cls(
            generated_at=generated_at,
            expires_at=expires_at,
            session_date=session_date,
            plan_id=plan_id,
            plan_hash=plan_hash,
            nonce=nonce,
            account_alias=account_alias,
            symbol=symbol,
            quantity=quantity,
            **decimals,
        )

    @property
    def hash_suffix(self) -> str:
        return self.plan_hash[-HASH_SUFFIX_LENGTH:]

    @property
    def interaction_binding(self) -> str:
        encoded = json.dumps(
            [
                self.plan_id,
                self.plan_hash,
                self.nonce,
                self.expires_at.isoformat(),
                self.session_date.isoformat(),
                self.account_alias,
                self.symbol,
                _format_decimal(self.allocated_cash),
                self.quantity,
                _format_decimal(self.entry_trigger),
                _format_decimal(self.entry_limit),
                _format_decimal(self.target_trigger),
                _format_decimal(self.stop_trigger),
                _format_decimal(self.stop_limit),
                _format_decimal(self.planned_risk),
                _format_decimal(self.reward_risk_ratio),
            ],
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def key(self) -> str:
        return self.interaction_binding


def _reject_json_constant(_: str) -> None:
    raise ValueError("non-finite JSON number")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def canonical_json_bytes(value: object) -> bytes:
    """Return the one byte representation used by every approval-v2 hash."""

    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError) as exc:
        raise ApprovalError("approval_v2_canonical_json_invalid") from exc


def _sha256_canonical(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _strict_json_mapping(raw: bytes, *, maximum: int, code: str) -> dict[str, Any]:
    if not raw or len(raw) > maximum:
        raise ApprovalError(code)
    try:
        parsed = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (RecursionError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ApprovalError(code) from exc
    if not isinstance(parsed, dict):
        raise ApprovalError(code)
    return parsed


def _v2_text(value: object, *, maximum: int, code: str) -> str:
    return _clean_text(value, maximum=maximum, code=code)


def _v2_hash(value: object, code: str) -> str:
    text = _v2_text(value, maximum=64, code=code)
    if not _HASH_RE.fullmatch(text):
        raise ApprovalError(code)
    return text


def _v2_snowflake(value: object, code: str) -> str:
    if not isinstance(value, str):
        raise ApprovalError(code)
    return _snowflake(value, code)


def _v2_positive_decimal(value: object, code: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 64
        or not _CANONICAL_POSITIVE_DECIMAL_RE.fullmatch(value)
    ):
        raise ApprovalError(code)
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ApprovalError(code) from exc
    if not number.is_finite() or number <= 0 or format(number, "f") != value:
        raise ApprovalError(code)
    return value


def _v2_int(value: object, *, positive: bool, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApprovalError(code)
    if (positive and value <= 0) or (not positive and value < 0):
        raise ApprovalError(code)
    return value


def _v2_timestamp(value: object, code: str) -> tuple[str, datetime]:
    text = _v2_text(value, maximum=64, code=code)
    return text, _aware_datetime(text, code)


@dataclass(frozen=True)
class ApprovalEnvelopeV2:
    plan_id: str
    plan_hash: str
    account_alias: str
    session_date: str
    symbol: str
    quantity: str
    entry_trigger: str
    entry_limit: str
    target_trigger: str
    target_limit: str
    stop_trigger: str
    stop_limit: str
    cash_reserved: str
    planned_risk: str
    planned_reward: str
    entry_start: str
    entry_expiry: str
    force_exit_at: str
    protection_slo_seconds: int
    exit_fill_slo_seconds: int
    boot_id_hash: str
    writer_fence: int
    approval_generation: int
    nonce: str
    issued_at: str
    expires_at: str
    interaction_binding: str

    @classmethod
    def from_mapping(cls, value: object) -> ApprovalEnvelopeV2:
        if not isinstance(value, Mapping) or set(value) != _V2_ENVELOPE_KEYS:
            raise ApprovalError("approval_v2_envelope_schema_invalid")
        if (
            not isinstance(value.get("schema_version"), int)
            or isinstance(value.get("schema_version"), bool)
            or value.get("schema_version") != 2
            or value.get("purpose") != "INTRADAY_LIVE_ENTRY"
            or value.get("emergency_exit") != _V2_EMERGENCY_EXIT
            or not isinstance(value.get("emergency_exit"), Mapping)
            or any(
                not isinstance(value["emergency_exit"].get(key), bool)
                for key in ("regular_session_only", "price_not_guaranteed")
            )
        ):
            raise ApprovalError("approval_v2_envelope_schema_invalid")

        plan_id = _v2_text(
            value.get("plan_id"), maximum=80, code="approval_v2_plan_id_invalid"
        )
        if not _PLAN_ID_RE.fullmatch(plan_id):
            raise ApprovalError("approval_v2_plan_id_invalid")
        plan_hash = _v2_hash(
            value.get("plan_hash"), "approval_v2_plan_hash_invalid"
        )
        account_alias = _v2_text(
            value.get("account_alias"),
            maximum=80,
            code="approval_v2_account_alias_invalid",
        )
        raw_session_date = _v2_text(
            value.get("session_date"),
            maximum=10,
            code="approval_v2_session_date_invalid",
        )
        try:
            if date.fromisoformat(raw_session_date).isoformat() != raw_session_date:
                raise ValueError(raw_session_date)
        except ValueError as exc:
            raise ApprovalError("approval_v2_session_date_invalid") from exc
        symbol = _v2_text(
            value.get("symbol"), maximum=24, code="approval_v2_symbol_invalid"
        )
        if not _SYMBOL_RE.fullmatch(symbol):
            raise ApprovalError("approval_v2_symbol_invalid")
        quantity = value.get("quantity")
        if (
            not isinstance(quantity, str)
            or len(quantity) > 24
            or not _CANONICAL_POSITIVE_INTEGER_RE.fullmatch(quantity)
        ):
            raise ApprovalError("approval_v2_quantity_invalid")

        economics = {
            key: _v2_positive_decimal(
                value.get(key), f"approval_v2_{key}_invalid"
            )
            for key in _V2_ECONOMIC_KEYS
        }
        entry_start, entry_start_dt = _v2_timestamp(
            value.get("entry_start"), "approval_v2_entry_start_invalid"
        )
        entry_expiry, entry_expiry_dt = _v2_timestamp(
            value.get("entry_expiry"), "approval_v2_entry_expiry_invalid"
        )
        force_exit_at, force_exit_dt = _v2_timestamp(
            value.get("force_exit_at"), "approval_v2_force_exit_invalid"
        )
        issued_at, issued_at_dt = _v2_timestamp(
            value.get("issued_at"), "approval_v2_issued_at_invalid"
        )
        expires_at, expires_at_dt = _v2_timestamp(
            value.get("expires_at"), "approval_v2_expires_at_invalid"
        )
        if (
            entry_start_dt >= entry_expiry_dt
            or entry_expiry_dt > force_exit_dt
            or issued_at_dt >= expires_at_dt
        ):
            raise ApprovalError("approval_v2_time_order_invalid")

        nonce = _v2_text(
            value.get("nonce"), maximum=80, code="approval_v2_nonce_invalid"
        )
        if not _NONCE_RE.fullmatch(nonce):
            raise ApprovalError("approval_v2_nonce_invalid")
        envelope = cls(
            plan_id=plan_id,
            plan_hash=plan_hash,
            account_alias=account_alias,
            session_date=raw_session_date,
            symbol=symbol,
            quantity=quantity,
            entry_start=entry_start,
            entry_expiry=entry_expiry,
            force_exit_at=force_exit_at,
            protection_slo_seconds=_v2_int(
                value.get("protection_slo_seconds"),
                positive=True,
                code="approval_v2_protection_slo_invalid",
            ),
            exit_fill_slo_seconds=_v2_int(
                value.get("exit_fill_slo_seconds"),
                positive=True,
                code="approval_v2_exit_fill_slo_invalid",
            ),
            boot_id_hash=_v2_hash(
                value.get("boot_id_hash"), "approval_v2_boot_id_hash_invalid"
            ),
            writer_fence=_v2_int(
                value.get("writer_fence"),
                positive=False,
                code="approval_v2_writer_fence_invalid",
            ),
            approval_generation=_v2_int(
                value.get("approval_generation"),
                positive=True,
                code="approval_v2_generation_invalid",
            ),
            nonce=nonce,
            issued_at=issued_at,
            expires_at=expires_at,
            interaction_binding=_v2_hash(
                value.get("interaction_binding"),
                "approval_v2_interaction_binding_invalid",
            ),
            **economics,
        )
        if not hmac.compare_digest(
            envelope.interaction_binding, envelope.expected_interaction_binding
        ):
            raise ApprovalError("approval_v2_interaction_binding_mismatch")
        return envelope

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> ApprovalEnvelopeV2:
        return cls.from_mapping(
            _strict_json_mapping(
                raw,
                maximum=MAX_V2_ENVELOPE_BYTES,
                code="approval_v2_envelope_invalid",
            )
        )

    def to_mapping(self, *, include_binding: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": 2,
            "purpose": "INTRADAY_LIVE_ENTRY",
            "plan_id": self.plan_id,
            "plan_hash": self.plan_hash,
            "account_alias": self.account_alias,
            "session_date": self.session_date,
            "symbol": self.symbol,
            "quantity": self.quantity,
            "entry_trigger": self.entry_trigger,
            "entry_limit": self.entry_limit,
            "target_trigger": self.target_trigger,
            "target_limit": self.target_limit,
            "stop_trigger": self.stop_trigger,
            "stop_limit": self.stop_limit,
            "cash_reserved": self.cash_reserved,
            "planned_risk": self.planned_risk,
            "planned_reward": self.planned_reward,
            "entry_start": self.entry_start,
            "entry_expiry": self.entry_expiry,
            "force_exit_at": self.force_exit_at,
            "protection_slo_seconds": self.protection_slo_seconds,
            "exit_fill_slo_seconds": self.exit_fill_slo_seconds,
            "emergency_exit": dict(_V2_EMERGENCY_EXIT),
            "boot_id_hash": self.boot_id_hash,
            "writer_fence": self.writer_fence,
            "approval_generation": self.approval_generation,
            "nonce": self.nonce,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }
        if include_binding:
            value["interaction_binding"] = self.interaction_binding
        return value

    @property
    def expected_interaction_binding(self) -> str:
        return _sha256_canonical(self.to_mapping(include_binding=False))

    @property
    def envelope_sha256(self) -> str:
        return _sha256_canonical(self.to_mapping())

    @property
    def nonce_sha256(self) -> str:
        return hashlib.sha256(self.nonce.encode("ascii")).hexdigest()

    @property
    def receipt_name(self) -> str:
        return f"{self.nonce_sha256}.json"


@dataclass(frozen=True)
class ApprovalReceiptV2:
    plan_id: str
    plan_hash: str
    interaction_binding: str
    approval_generation: int
    writer_fence: int
    boot_id_hash: str
    nonce_sha256: str
    discord_guild_id: str
    discord_channel_id: str
    discord_user_id: str
    interaction_id: str
    decided_at: str
    expires_at: str
    envelope_sha256: str

    @classmethod
    def create(
        cls,
        envelope: ApprovalEnvelopeV2,
        *,
        discord_guild_id: object,
        discord_channel_id: object,
        discord_user_id: object,
        interaction_id: object,
        decided_at: datetime,
    ) -> ApprovalReceiptV2:
        if decided_at.tzinfo is None or decided_at.utcoffset() is None:
            raise ApprovalError("approval_v2_decided_at_invalid")
        decided = decided_at.astimezone(timezone.utc)
        issued = _aware_datetime(envelope.issued_at, "approval_v2_issued_at_invalid")
        expiry = _aware_datetime(envelope.expires_at, "approval_v2_expires_at_invalid")
        if decided < issued or decided >= expiry:
            raise ApprovalError("approval_v2_expired")
        return cls(
            plan_id=envelope.plan_id,
            plan_hash=envelope.plan_hash,
            interaction_binding=envelope.interaction_binding,
            approval_generation=envelope.approval_generation,
            writer_fence=envelope.writer_fence,
            boot_id_hash=envelope.boot_id_hash,
            nonce_sha256=envelope.nonce_sha256,
            discord_guild_id=_snowflake(
                discord_guild_id, "approval_v2_discord_guild_invalid"
            ),
            discord_channel_id=_snowflake(
                discord_channel_id, "approval_v2_discord_channel_invalid"
            ),
            discord_user_id=_snowflake(
                discord_user_id, "approval_v2_discord_user_invalid"
            ),
            interaction_id=_snowflake(
                interaction_id, "approval_v2_interaction_id_invalid"
            ),
            decided_at=decided.isoformat(timespec="microseconds"),
            expires_at=envelope.expires_at,
            envelope_sha256=envelope.envelope_sha256,
        )

    @classmethod
    def from_mapping(cls, value: object) -> ApprovalReceiptV2:
        if not isinstance(value, Mapping) or set(value) != _V2_RECEIPT_KEYS:
            raise ApprovalError("approval_v2_receipt_schema_invalid")
        if (
            not isinstance(value.get("schema_version"), int)
            or isinstance(value.get("schema_version"), bool)
            or value.get("schema_version") != 2
            or value.get("purpose") != "INTRADAY_LIVE_ENTRY"
            or value.get("decision") != "APPROVE"
        ):
            raise ApprovalError("approval_v2_receipt_schema_invalid")
        plan_id = _v2_text(
            value.get("plan_id"), maximum=80, code="approval_v2_receipt_invalid"
        )
        if not _PLAN_ID_RE.fullmatch(plan_id):
            raise ApprovalError("approval_v2_receipt_invalid")
        ids = {
            key: _v2_snowflake(value.get(key), "approval_v2_receipt_invalid")
            for key in (
                "discord_guild_id",
                "discord_channel_id",
                "discord_user_id",
                "interaction_id",
            )
        }
        decided_at, _ = _v2_timestamp(
            value.get("decided_at"), "approval_v2_receipt_invalid"
        )
        expires_at, _ = _v2_timestamp(
            value.get("expires_at"), "approval_v2_receipt_invalid"
        )
        return cls(
            plan_id=plan_id,
            plan_hash=_v2_hash(value.get("plan_hash"), "approval_v2_receipt_invalid"),
            interaction_binding=_v2_hash(
                value.get("interaction_binding"), "approval_v2_receipt_invalid"
            ),
            approval_generation=_v2_int(
                value.get("approval_generation"),
                positive=True,
                code="approval_v2_receipt_invalid",
            ),
            writer_fence=_v2_int(
                value.get("writer_fence"),
                positive=False,
                code="approval_v2_receipt_invalid",
            ),
            boot_id_hash=_v2_hash(
                value.get("boot_id_hash"), "approval_v2_receipt_invalid"
            ),
            nonce_sha256=_v2_hash(
                value.get("nonce_sha256"), "approval_v2_receipt_invalid"
            ),
            decided_at=decided_at,
            expires_at=expires_at,
            envelope_sha256=_v2_hash(
                value.get("envelope_sha256"), "approval_v2_receipt_invalid"
            ),
            **ids,
        )

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> ApprovalReceiptV2:
        return cls.from_mapping(
            _strict_json_mapping(
                raw,
                maximum=MAX_V2_RECEIPT_BYTES,
                code="approval_v2_receipt_invalid",
            )
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": 2,
            "purpose": "INTRADAY_LIVE_ENTRY",
            "decision": "APPROVE",
            "plan_id": self.plan_id,
            "plan_hash": self.plan_hash,
            "interaction_binding": self.interaction_binding,
            "approval_generation": self.approval_generation,
            "writer_fence": self.writer_fence,
            "boot_id_hash": self.boot_id_hash,
            "nonce_sha256": self.nonce_sha256,
            "discord_guild_id": self.discord_guild_id,
            "discord_channel_id": self.discord_channel_id,
            "discord_user_id": self.discord_user_id,
            "interaction_id": self.interaction_id,
            "decided_at": self.decided_at,
            "expires_at": self.expires_at,
            "envelope_sha256": self.envelope_sha256,
        }

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    @property
    def receipt_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()


def verify_approval_receipt_v2(
    receipt: ApprovalReceiptV2,
    envelope: ApprovalEnvelopeV2,
    *,
    discord_guild_id: str,
    discord_channel_id: str,
    discord_user_id: str,
    current_boot_id_hash: str,
    writer_fence: int,
    approval_generation: int,
    now: datetime,
) -> str:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ApprovalError("approval_v2_clock_invalid")
    expected_guild = _v2_snowflake(
        discord_guild_id, "approval_v2_discord_guild_invalid"
    )
    expected_channel = _v2_snowflake(
        discord_channel_id, "approval_v2_discord_channel_invalid"
    )
    expected_user = _v2_snowflake(
        discord_user_id, "approval_v2_discord_user_invalid"
    )
    expected_boot = _v2_hash(
        current_boot_id_hash, "approval_v2_boot_id_hash_invalid"
    )
    expected_fence = _v2_int(
        writer_fence, positive=False, code="approval_v2_writer_fence_invalid"
    )
    expected_generation = _v2_int(
        approval_generation,
        positive=True,
        code="approval_v2_generation_invalid",
    )
    exact_strings = (
        (receipt.plan_id, envelope.plan_id),
        (receipt.plan_hash, envelope.plan_hash),
        (receipt.interaction_binding, envelope.interaction_binding),
        (receipt.nonce_sha256, envelope.nonce_sha256),
        (receipt.boot_id_hash, expected_boot),
        (receipt.boot_id_hash, envelope.boot_id_hash),
        (receipt.discord_guild_id, expected_guild),
        (receipt.discord_channel_id, expected_channel),
        (receipt.discord_user_id, expected_user),
        (receipt.expires_at, envelope.expires_at),
        (receipt.envelope_sha256, envelope.envelope_sha256),
    )
    if any(not hmac.compare_digest(left, right) for left, right in exact_strings):
        raise ApprovalError("approval_v2_receipt_binding_mismatch")
    if (
        receipt.writer_fence != expected_fence
        or receipt.writer_fence != envelope.writer_fence
        or receipt.approval_generation != expected_generation
        or receipt.approval_generation != envelope.approval_generation
    ):
        raise ApprovalError("approval_v2_receipt_generation_mismatch")
    decided = _aware_datetime(receipt.decided_at, "approval_v2_receipt_invalid")
    issued = _aware_datetime(envelope.issued_at, "approval_v2_receipt_invalid")
    expiry = _aware_datetime(envelope.expires_at, "approval_v2_receipt_invalid")
    entry_expiry = _aware_datetime(
        envelope.entry_expiry, "approval_v2_receipt_invalid"
    )
    current = now.astimezone(timezone.utc)
    if decided < issued or decided >= expiry or current >= min(expiry, entry_expiry):
        raise ApprovalError("approval_v2_expired")
    return receipt.receipt_sha256


def render_approval_v2_message(envelope: ApprovalEnvelopeV2) -> str:
    """Render every value whose approval is bound by the v2 envelope."""

    return "\n".join(
        (
            "장전 단타 LIVE 진입 승인 요청",
            f"계정: {_discord_plain_text(envelope.account_alias)}",
            f"거래일 / 종목 / 수량: {envelope.session_date} / {envelope.symbol} / {envelope.quantity}",
            f"진입 트리거 / 한도: ${envelope.entry_trigger} / ${envelope.entry_limit}",
            f"목표 트리거 / 한도: ${envelope.target_trigger} / ${envelope.target_limit}",
            f"손절 트리거 / 한도: ${envelope.stop_trigger} / ${envelope.stop_limit}",
            f"예약 현금 / 계획 위험 / 계획 보상: ${envelope.cash_reserved} / ${envelope.planned_risk} / ${envelope.planned_reward}",
            f"진입 시작 / 만료 / 강제청산: {envelope.entry_start} / {envelope.entry_expiry} / {envelope.force_exit_at}",
            f"보호 SLO / 청산 체결 SLO: {envelope.protection_slo_seconds}초 / {envelope.exit_fill_slo_seconds}초",
            "비상정책: 정규장에 당일 봇 소유 잔여 전량 MARKET 청산",
            "경고: MARKET 가격과 체결은 보장되지 않습니다.",
            f"계획 해시 끝 {HASH_SUFFIX_LENGTH}자리: {envelope.plan_hash[-HASH_SUFFIX_LENGTH:]}",
        )
    )


def hash_macos_boot_session_uuid(value: str) -> str:
    if not isinstance(value, str):
        raise ApprovalError("approval_v2_boot_id_unavailable")
    try:
        canonical = str(uuid.UUID(value.strip()))
    except (ValueError, AttributeError) as exc:
        raise ApprovalError("approval_v2_boot_id_unavailable") from exc
    return hashlib.sha256(
        b"macos-boot-v1\0" + canonical.encode("ascii")
    ).hexdigest()


def read_macos_boot_id_hash() -> str:
    """Read kern.bootsessionuuid without spawning a command."""

    if sys.platform != "darwin":
        raise ApprovalError("approval_v2_boot_id_unavailable")
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    sysctlbyname = libc.sysctlbyname
    sysctlbyname.argtypes = [
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_void_p,
        ctypes.c_size_t,
    ]
    sysctlbyname.restype = ctypes.c_int
    size = ctypes.c_size_t()
    name = b"kern.bootsessionuuid"
    if sysctlbyname(name, None, ctypes.byref(size), None, 0) != 0:
        raise ApprovalError("approval_v2_boot_id_unavailable")
    if not 1 <= size.value <= 128:
        raise ApprovalError("approval_v2_boot_id_unavailable")
    buffer = ctypes.create_string_buffer(size.value)
    if sysctlbyname(name, buffer, ctypes.byref(size), None, 0) != 0:
        raise ApprovalError("approval_v2_boot_id_unavailable")
    try:
        raw = buffer.raw[: size.value].rstrip(b"\0").decode("ascii")
    except UnicodeDecodeError as exc:
        raise ApprovalError("approval_v2_boot_id_unavailable") from exc
    return hash_macos_boot_session_uuid(raw)


@dataclass(frozen=True)
class DirectoryIdentity:
    path: Path
    uid: int
    gid: int
    mode: int


@dataclass(frozen=True)
class CrossUidReceiptMailbox:
    """Exact on-disk identities for the root anchor and approval inbox."""

    directories: tuple[DirectoryIdentity, ...]
    anchor_path: Path
    writer_uid: int
    reader_uid: int
    file_gid: int

    def __post_init__(self) -> None:
        if self.writer_uid < 0 or self.reader_uid < 0 or self.file_gid < 0:
            raise ApprovalError("approval_v2_mailbox_policy_invalid")
        if self.writer_uid == self.reader_uid or len(self.directories) < 2:
            raise ApprovalError("approval_v2_mailbox_policy_invalid")
        paths = tuple(item.path for item in self.directories)
        if any(not path.is_absolute() for path in paths):
            raise ApprovalError("approval_v2_mailbox_policy_invalid")
        if paths[0] != paths[0].__class__(paths[0].anchor):
            raise ApprovalError("approval_v2_mailbox_policy_invalid")
        if any(child.parent != parent for parent, child in zip(paths, paths[1:])):
            raise ApprovalError("approval_v2_mailbox_policy_invalid")
        if paths[-2] != self.anchor_path or paths[-1].name != "approval-inbox":
            raise ApprovalError("approval_v2_mailbox_policy_invalid")
        anchor = self.directories[-2]
        inbox = self.directories[-1]
        if anchor.uid != 0 or anchor.gid != 0 or anchor.mode != 0o755:
            raise ApprovalError("approval_v2_mailbox_policy_invalid")
        if (
            inbox.uid != self.writer_uid
            or inbox.gid != self.file_gid
            or inbox.mode != 0o750
        ):
            raise ApprovalError("approval_v2_mailbox_policy_invalid")
        if any(
            item.uid != 0 or item.mode & 0o022
            for item in self.directories[:-1]
        ):
            raise ApprovalError("approval_v2_mailbox_policy_invalid")
        if any(
            item.uid < 0
            or item.gid < 0
            or item.mode < 0
            or item.mode > 0o7777
            for item in self.directories
        ):
            raise ApprovalError("approval_v2_mailbox_policy_invalid")

    @property
    def path(self) -> Path:
        return self.directories[-1].path

    @contextmanager
    def open_for_writer(self) -> Iterator[int]:
        with self._open(expected_uid=self.writer_uid) as descriptor:
            yield descriptor

    @contextmanager
    def open_for_reader(self) -> Iterator[int]:
        with self._open(expected_uid=self.reader_uid) as descriptor:
            yield descriptor

    @contextmanager
    def _open(self, *, expected_uid: int) -> Iterator[int]:
        _require_fd_relative_platform()
        if os.geteuid() != expected_uid:
            raise ApprovalError("approval_v2_mailbox_identity_mismatch")
        flags = (
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )
        descriptor = -1
        try:
            descriptor = os.open(os.fspath(self.directories[0].path), flags)
            _verify_directory_identity(descriptor, self.directories[0])
            _require_local_apfs(descriptor)
            for expected in self.directories[1:]:
                next_descriptor = os.open(
                    expected.path.name, flags, dir_fd=descriptor
                )
                os.close(descriptor)
                descriptor = next_descriptor
                _verify_directory_identity(descriptor, expected)
                _require_local_apfs(descriptor)
            yield descriptor
        except ApprovalError:
            raise
        except OSError as exc:
            raise ApprovalError("approval_v2_mailbox_invalid") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)


def _require_fd_relative_platform() -> None:
    required = (os.open, os.link, os.stat, os.unlink)
    if (
        os.name != "posix"
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "geteuid")
        or not hasattr(os, "fchown")
        or any(operation not in os.supports_dir_fd for operation in required)
    ):
        raise ApprovalError("approval_v2_mailbox_platform_unsupported")


def _verify_directory_identity(descriptor: int, expected: DirectoryIdentity) -> None:
    info = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != expected.uid
        or info.st_gid != expected.gid
        or stat.S_IMODE(info.st_mode) != expected.mode
    ):
        raise ApprovalError("approval_v2_mailbox_identity_mismatch")


def _require_local_apfs(descriptor: int) -> None:
    if sys.platform != "darwin":
        raise ApprovalError("approval_v2_mailbox_filesystem_invalid")
    import ctypes

    class _Fsid(ctypes.Structure):
        _fields_ = [("value", ctypes.c_int32 * 2)]

    class _StatFs(ctypes.Structure):
        _fields_ = [
            ("f_bsize", ctypes.c_uint32),
            ("f_iosize", ctypes.c_int32),
            ("f_blocks", ctypes.c_uint64),
            ("f_bfree", ctypes.c_uint64),
            ("f_bavail", ctypes.c_uint64),
            ("f_files", ctypes.c_uint64),
            ("f_ffree", ctypes.c_uint64),
            ("f_fsid", _Fsid),
            ("f_owner", ctypes.c_uint32),
            ("f_type", ctypes.c_uint32),
            ("f_flags", ctypes.c_uint32),
            ("f_fssubtype", ctypes.c_uint32),
            ("f_fstypename", ctypes.c_char * 16),
            ("f_mntonname", ctypes.c_char * 1024),
            ("f_mntfromname", ctypes.c_char * 1024),
            ("f_reserved", ctypes.c_uint32 * 8),
        ]

    details = _StatFs()
    libc = ctypes.CDLL(None, use_errno=True)
    fstatfs = libc.fstatfs
    fstatfs.argtypes = [ctypes.c_int, ctypes.POINTER(_StatFs)]
    fstatfs.restype = ctypes.c_int
    if fstatfs(descriptor, ctypes.byref(details)) != 0:
        raise ApprovalError("approval_v2_mailbox_filesystem_invalid")
    filesystem = bytes(details.f_fstypename).split(b"\0", 1)[0]
    mount_local = 0x00001000
    mount_ignore_ownership = 0x00200000
    if (
        filesystem != b"apfs"
        or not details.f_flags & mount_local
        or details.f_flags & mount_ignore_ownership
    ):
        raise ApprovalError("approval_v2_mailbox_filesystem_invalid")


def _validate_v2_receipt_stat(
    info: os.stat_result,
    mailbox: CrossUidReceiptMailbox,
    *,
    allowed_links: frozenset[int],
) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != mailbox.writer_uid
        or info.st_gid != mailbox.file_gid
        or stat.S_IMODE(info.st_mode) != 0o640
        or not 1 <= info.st_size <= MAX_V2_RECEIPT_BYTES
        or info.st_nlink not in allowed_links
    ):
        raise ApprovalError("approval_v2_receipt_file_invalid")


def _read_fd_bytes(descriptor: int, maximum: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(8192, maximum + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > maximum:
            raise ApprovalError("approval_v2_receipt_file_invalid")
    return b"".join(chunks)


def _open_v2_receipt_at(
    directory_fd: int,
    mailbox: CrossUidReceiptMailbox,
    name: str,
    *,
    allowed_links: frozenset[int],
) -> tuple[int, os.stat_result]:
    if not _V2_RECEIPT_NAME_RE.fullmatch(name):
        raise ApprovalError("approval_v2_receipt_name_invalid")
    flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise ApprovalError("approval_v2_receipt_file_invalid") from exc
    try:
        info = os.fstat(descriptor)
        _validate_v2_receipt_stat(info, mailbox, allowed_links=allowed_links)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, info


def publish_approval_receipt_v2(
    mailbox: CrossUidReceiptMailbox,
    receipt: ApprovalReceiptV2,
    *,
    after_link_before_temp_unlink: Callable[[], None] | None = None,
) -> str:
    """Durably publish a v2 receipt without replacing an existing decision."""

    name = f"{receipt.nonce_sha256}.json"
    if not _V2_RECEIPT_NAME_RE.fullmatch(name):
        raise ApprovalError("approval_v2_receipt_name_invalid")
    encoded = receipt.canonical_bytes
    if not encoded or len(encoded) > MAX_V2_RECEIPT_BYTES:
        raise ApprovalError("approval_v2_receipt_invalid")
    temporary_name = f".{name}.{secrets.token_hex(12)}.tmp"
    created = False
    linked = False
    with mailbox.open_for_writer() as directory_fd:
        descriptor = -1
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_BINARY", 0)
        )
        try:
            descriptor = os.open(
                temporary_name, flags, 0o640, dir_fd=directory_fd
            )
            created = True
            os.fchown(descriptor, mailbox.writer_uid, mailbox.file_gid)
            os.fchmod(descriptor, 0o640)
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short receipt write")
                view = view[written:]
            os.fsync(descriptor)
            info = os.fstat(descriptor)
            _validate_v2_receipt_stat(
                info, mailbox, allowed_links=frozenset({1})
            )
            os.close(descriptor)
            descriptor = -1
            try:
                os.link(
                    temporary_name,
                    name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                existing_fd, existing_info = _open_v2_receipt_at(
                    directory_fd,
                    mailbox,
                    name,
                    allowed_links=frozenset({1, 2}),
                )
                try:
                    if existing_info.st_nlink == 2:
                        raise ApprovalError("APPROVAL_PUBLISH_PENDING") from exc
                    existing = ApprovalReceiptV2.from_json_bytes(
                        _read_fd_bytes(existing_fd, MAX_V2_RECEIPT_BYTES)
                    )
                finally:
                    os.close(existing_fd)
                if not hmac.compare_digest(
                    existing.receipt_sha256, receipt.receipt_sha256
                ):
                    raise ApprovalError("approval_v2_receipt_file_invalid") from exc
                raise ApprovalError("decision_already_recorded") from exc
            linked = True
            os.fsync(directory_fd)
            if after_link_before_temp_unlink is not None:
                after_link_before_temp_unlink()
            os.unlink(temporary_name, dir_fd=directory_fd)
            created = False
            os.fsync(directory_fd)
            final_fd, _ = _open_v2_receipt_at(
                directory_fd,
                mailbox,
                name,
                allowed_links=frozenset({1}),
            )
            os.close(final_fd)
        except ApprovalError:
            raise
        except OSError as exc:
            raise ApprovalError("approval_v2_receipt_write_failed") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if created and not linked:
                try:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                    os.fsync(directory_fd)
                except OSError:
                    pass
    return name


def recover_approval_receipt_publish_v2(
    mailbox: CrossUidReceiptMailbox, name: str
) -> bool:
    """Let only the approver finish its own known nlink=2 publish."""

    if not _V2_RECEIPT_NAME_RE.fullmatch(name):
        raise ApprovalError("approval_v2_receipt_name_invalid")
    with mailbox.open_for_writer() as directory_fd:
        try:
            entries = os.listdir(directory_fd)
        except OSError as exc:
            raise ApprovalError("approval_v2_publish_recovery_invalid") from exc
        matching_temps = [
            entry
            for entry in entries
            if (match := _V2_TEMP_NAME_RE.fullmatch(entry)) is not None
            and match.group("final") == name
        ]
        final_fd, final_info = _open_v2_receipt_at(
            directory_fd,
            mailbox,
            name,
            allowed_links=frozenset({1, 2}),
        )
        try:
            final_raw = _read_fd_bytes(final_fd, MAX_V2_RECEIPT_BYTES)
        finally:
            os.close(final_fd)
        if final_info.st_nlink == 1:
            if matching_temps:
                raise ApprovalError("approval_v2_publish_recovery_invalid")
            return False
        if len(matching_temps) != 1:
            raise ApprovalError("approval_v2_publish_recovery_invalid")
        temporary_name = matching_temps[0]
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            temporary_fd = os.open(temporary_name, flags, dir_fd=directory_fd)
        except OSError as exc:
            raise ApprovalError("approval_v2_publish_recovery_invalid") from exc
        try:
            temporary_info = os.fstat(temporary_fd)
            _validate_v2_receipt_stat(
                temporary_info, mailbox, allowed_links=frozenset({2})
            )
            temporary_raw = _read_fd_bytes(temporary_fd, MAX_V2_RECEIPT_BYTES)
        finally:
            os.close(temporary_fd)
        if (
            final_info.st_dev != temporary_info.st_dev
            or final_info.st_ino != temporary_info.st_ino
            or final_info.st_size != temporary_info.st_size
            or not hmac.compare_digest(
                hashlib.sha256(final_raw).digest(),
                hashlib.sha256(temporary_raw).digest(),
            )
        ):
            raise ApprovalError("approval_v2_publish_recovery_invalid")
        # This makes the final directory entry durable before removing the temp link.
        try:
            os.fsync(directory_fd)
            os.unlink(temporary_name, dir_fd=directory_fd)
            os.fsync(directory_fd)
        except OSError as exc:
            raise ApprovalError("approval_v2_publish_recovery_invalid") from exc
        final_fd, _ = _open_v2_receipt_at(
            directory_fd,
            mailbox,
            name,
            allowed_links=frozenset({1}),
        )
        os.close(final_fd)
        return True


def read_approval_receipt_v2(
    mailbox: CrossUidReceiptMailbox,
    name: str,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> ApprovalReceiptV2:
    """Read as trader; nlink=2 remains bounded pending and is never cleaned here."""

    if not _V2_RECEIPT_NAME_RE.fullmatch(name):
        raise ApprovalError("approval_v2_receipt_name_invalid")
    deadline = monotonic() + APPROVAL_PUBLISH_PENDING_SECONDS
    for attempt in range(APPROVAL_PUBLISH_PENDING_ATTEMPTS):
        with mailbox.open_for_reader() as directory_fd:
            descriptor, info = _open_v2_receipt_at(
                directory_fd,
                mailbox,
                name,
                allowed_links=frozenset({1, 2}),
            )
            try:
                if info.st_nlink == 1:
                    return ApprovalReceiptV2.from_json_bytes(
                        _read_fd_bytes(descriptor, MAX_V2_RECEIPT_BYTES)
                    )
            finally:
                os.close(descriptor)
        if attempt + 1 == APPROVAL_PUBLISH_PENDING_ATTEMPTS:
            break
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        sleeper(
            min(
                remaining,
                APPROVAL_PUBLISH_PENDING_SECONDS
                / (APPROVAL_PUBLISH_PENDING_ATTEMPTS - 1),
            )
        )
    raise ApprovalError("APPROVAL_PUBLISH_PENDING")


def load_envelope(path: Path) -> ApprovalEnvelope:
    try:
        path_info = path.lstat()
    except FileNotFoundError as exc:
        raise ApprovalError("approval_envelope_missing") from exc
    except OSError as exc:
        raise ApprovalError("approval_envelope_invalid") from exc
    if stat.S_ISLNK(path_info.st_mode) or not stat.S_ISREG(path_info.st_mode):
        raise ApprovalError("approval_envelope_invalid")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise ApprovalError("approval_envelope_missing") from exc
    except OSError as exc:
        raise ApprovalError("approval_envelope_invalid") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ENVELOPE_BYTES:
            raise ApprovalError("approval_envelope_invalid")
        if os.name != "nt":
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                raise ApprovalError("approval_envelope_invalid")
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise ApprovalError("approval_envelope_permissions_invalid")
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            raw = handle.read(MAX_ENVELOPE_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not raw or len(raw) > MAX_ENVELOPE_BYTES:
        raise ApprovalError("approval_envelope_invalid")
    try:
        parsed = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ApprovalError("approval_envelope_invalid") from exc
    return ApprovalEnvelope.from_mapping(parsed)


def _custom_id(action: str, envelope: ApprovalEnvelope) -> str:
    return f"ta:{action}:{envelope.interaction_binding}"


def make_approve_custom_id(envelope: ApprovalEnvelope) -> str:
    return _custom_id("a", envelope)


def make_confirm_custom_id(envelope: ApprovalEnvelope) -> str:
    return _custom_id("c", envelope)


def _validate_custom_id(
    custom_id: object, *, action: str, envelope: ApprovalEnvelope
) -> None:
    if not isinstance(custom_id, str):
        raise ApprovalError("approval_interaction_invalid")
    match = _CUSTOM_ID_RE.fullmatch(custom_id)
    if match is None or match.group("action") != action:
        raise ApprovalError("approval_interaction_invalid")
    if not hmac.compare_digest(match.group("binding"), envelope.interaction_binding):
        raise ApprovalError("approval_plan_binding_mismatch")


def _format_decimal(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _discord_plain_text(value: str) -> str:
    return re.sub(r"([\\`*_{}\[\]()<>#+\-.!|~@])", r"\\\1", value)


def render_approval_message(envelope: ApprovalEnvelope) -> str:
    return "\n".join(
        (
            "장전 단타 계획 승인 요청",
            f"계정: {_discord_plain_text(envelope.account_alias)}",
            f"거래일 / 종목: {envelope.session_date.isoformat()} / {envelope.symbol}",
            f"배정 현금 / 수량: ${_format_decimal(envelope.allocated_cash)} / {envelope.quantity}",
            f"진입 트리거 / 한도: ${_format_decimal(envelope.entry_trigger)} / ${_format_decimal(envelope.entry_limit)}",
            f"목표 / 손절 트리거 / 손절 한도: ${_format_decimal(envelope.target_trigger)} / ${_format_decimal(envelope.stop_trigger)} / ${_format_decimal(envelope.stop_limit)}",
            f"계획 위험 / 손익비: ${_format_decimal(envelope.planned_risk)} / {_format_decimal(envelope.reward_risk_ratio)}",
            f"만료: {envelope.expires_at.isoformat()}",
            f"확인 코드(계획 해시 끝 {HASH_SUFFIX_LENGTH}자리): {envelope.hash_suffix}",
            "이 워커는 승인 영수증만 기록하며 주문을 제출하지 않습니다.",
        )
    )


def extract_hash_suffix(data: object) -> str:
    values: list[str] = []
    visited = 0

    def walk(value: object, depth: int) -> None:
        nonlocal visited
        visited += 1
        if depth > 8 or visited > 64:
            raise ApprovalError("approval_modal_invalid")
        if isinstance(value, Mapping):
            if value.get("custom_id") == HASH_INPUT_CUSTOM_ID:
                candidate = value.get("value")
                if isinstance(candidate, str):
                    values.append(candidate)
            for nested in value.values():
                walk(nested, depth + 1)
        elif isinstance(value, list):
            for nested in value:
                walk(nested, depth + 1)

    walk(data, 0)
    if len(values) != 1:
        raise ApprovalError("approval_modal_invalid")
    return values[0]


@dataclass(frozen=True)
class ApprovalDecision:
    envelope: ApprovalEnvelope
    receipt_path: Path
    decided_at: datetime


class ApprovalService:
    def __init__(
        self,
        config: ApprovalConfig,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ApprovalError("approval_clock_invalid")
        return value.astimezone(timezone.utc)

    def assert_context(
        self, *, user_id: object, guild_id: object, channel_id: object
    ) -> None:
        try:
            user = _snowflake(user_id, "approval_context_denied")
            guild = _snowflake(guild_id, "approval_context_denied")
            channel = _snowflake(channel_id, "approval_context_denied")
        except ApprovalError as exc:
            raise ApprovalError("approval_context_denied") from exc
        if (
            user != self.config.allowed_user_id
            or guild != self.config.guild_id
            or channel != self.config.channel_id
        ):
            raise ApprovalError("approval_context_denied")

    def current_envelope(self, *, now: datetime | None = None) -> ApprovalEnvelope:
        envelope = load_envelope(self.config.envelope_path)
        current = self._now() if now is None else now
        if current.tzinfo is None or current.utcoffset() is None:
            raise ApprovalError("approval_clock_invalid")
        if current.astimezone(timezone.utc) < envelope.generated_at:
            raise ApprovalError("approval_not_yet_valid")
        if current.astimezone(timezone.utc) >= envelope.expires_at:
            raise ApprovalError("approval_expired")
        return envelope

    def begin(
        self,
        *,
        custom_id: object,
        user_id: object,
        guild_id: object,
        channel_id: object,
    ) -> ApprovalEnvelope:
        self.assert_context(
            user_id=user_id, guild_id=guild_id, channel_id=channel_id
        )
        envelope = self.current_envelope()
        _validate_custom_id(custom_id, action="a", envelope=envelope)
        return envelope

    def receipt_path_for(self, envelope: ApprovalEnvelope) -> Path:
        nonce_digest = hashlib.sha256(envelope.nonce.encode("ascii")).hexdigest()
        return self.config.inbox_dir / f"{nonce_digest}.json"

    def receipt_exists(self, envelope: ApprovalEnvelope) -> bool:
        path = self.receipt_path_for(envelope)
        try:
            path_info = path.lstat()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise ApprovalError("approval_receipt_invalid") from exc
        if stat.S_ISLNK(path_info.st_mode) or not stat.S_ISREG(path_info.st_mode):
            raise ApprovalError("approval_receipt_invalid")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = -1
        try:
            descriptor = os.open(path, flags)
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_size < 1
                or info.st_size > MAX_RECEIPT_BYTES
            ):
                raise ApprovalError("approval_receipt_invalid")
            if os.name != "nt":
                if hasattr(os, "getuid") and info.st_uid != os.getuid():
                    raise ApprovalError("approval_receipt_invalid")
                if stat.S_IMODE(info.st_mode) & 0o077:
                    raise ApprovalError("approval_receipt_permissions_invalid")
            with os.fdopen(descriptor, "rb", closefd=True) as handle:
                descriptor = -1
                raw = handle.read(MAX_RECEIPT_BYTES + 1)
        except ApprovalError:
            raise
        except OSError as exc:
            raise ApprovalError("approval_receipt_invalid") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if not raw or len(raw) > MAX_RECEIPT_BYTES:
            raise ApprovalError("approval_receipt_invalid")
        try:
            parsed = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ApprovalError("approval_receipt_invalid") from exc
        if not isinstance(parsed, Mapping) or set(parsed) != _RECEIPT_KEYS:
            raise ApprovalError("approval_receipt_invalid")
        decided_at = _aware_datetime(
            parsed.get("decided_at"), "approval_receipt_invalid"
        )
        expires_at = _aware_datetime(
            parsed.get("expires_at"), "approval_receipt_invalid"
        )
        expected = {
            "schema_version": 1,
            "decision": "APPROVE",
            "plan_id": envelope.plan_id,
            "plan_hash": envelope.plan_hash,
            "interaction_binding": envelope.interaction_binding,
            "nonce_sha256": hashlib.sha256(
                envelope.nonce.encode("ascii")
            ).hexdigest(),
            "discord_user_id": self.config.allowed_user_id,
            "guild_id": self.config.guild_id,
            "channel_id": self.config.channel_id,
        }
        if any(parsed.get(key) != value for key, value in expected.items()):
            raise ApprovalError("approval_receipt_invalid")
        try:
            _snowflake(parsed.get("interaction_id"), "approval_receipt_invalid")
        except ApprovalError as exc:
            raise ApprovalError("approval_receipt_invalid") from exc
        if (
            expires_at != envelope.expires_at
            or decided_at < envelope.generated_at
            or decided_at >= envelope.expires_at
        ):
            raise ApprovalError("approval_receipt_invalid")
        return True

    def approve(
        self,
        *,
        custom_id: object,
        hash_suffix: object,
        interaction_id: object,
        user_id: object,
        guild_id: object,
        channel_id: object,
    ) -> ApprovalDecision:
        self.assert_context(
            user_id=user_id, guild_id=guild_id, channel_id=channel_id
        )
        decided_at = self._now()
        envelope = self.current_envelope(now=decided_at)
        _validate_custom_id(custom_id, action="c", envelope=envelope)
        if (
            not isinstance(hash_suffix, str)
            or not _HASH_SUFFIX_RE.fullmatch(hash_suffix)
            or not hmac.compare_digest(hash_suffix.lower(), envelope.hash_suffix)
        ):
            raise ApprovalError("approval_hash_suffix_mismatch")
        interaction = _snowflake(interaction_id, "approval_interaction_id_invalid")
        if self.receipt_exists(envelope):
            raise ApprovalError("decision_already_recorded")
        receipt_path = self._write_receipt(
            envelope=envelope,
            decided_at=decided_at,
            interaction_id=interaction,
            user_id=_snowflake(user_id, "approval_context_denied"),
        )
        return ApprovalDecision(
            envelope=envelope,
            receipt_path=receipt_path,
            decided_at=decided_at,
        )

    def _write_receipt(
        self,
        *,
        envelope: ApprovalEnvelope,
        decided_at: datetime,
        interaction_id: str,
        user_id: str,
    ) -> Path:
        inbox = self.config.inbox_dir
        require_private_directory(inbox, "approval_receipt_directory_invalid")

        path = self.receipt_path_for(envelope)
        payload = {
            "schema_version": 1,
            "decision": "APPROVE",
            "plan_id": envelope.plan_id,
            "plan_hash": envelope.plan_hash,
            "interaction_binding": envelope.interaction_binding,
            "nonce_sha256": hashlib.sha256(
                envelope.nonce.encode("ascii")
            ).hexdigest(),
            "expires_at": envelope.expires_at.isoformat(),
            "decided_at": decided_at.isoformat(),
            "discord_user_id": user_id,
            "guild_id": self.config.guild_id,
            "channel_id": self.config.channel_id,
            "interaction_id": interaction_id,
        }
        encoded = (
            json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        temporary_path = inbox / (
            f".{path.name}.{secrets.token_hex(12)}.tmp"
        )
        descriptor = -1
        try:
            try:
                descriptor = os.open(temporary_path, flags, 0o600)
            except FileExistsError as exc:
                raise ApprovalError("approval_receipt_write_failed") from exc
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                descriptor = -1
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                os.chmod(temporary_path, 0o600, follow_symlinks=False)
            try:
                os.link(temporary_path, path, follow_symlinks=False)
            except FileExistsError as exc:
                raise ApprovalError("decision_already_recorded") from exc
            _fsync_directory(inbox)
        except ApprovalError:
            raise
        except OSError as exc:
            raise ApprovalError("approval_receipt_write_failed") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        _fsync_directory(inbox)
        return path


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        directory_fd = os.open(path, directory_flags)
    except OSError:
        return
    try:
        try:
            os.fsync(directory_fd)
        except OSError:
            # Some otherwise safe filesystems do not support directory fsync.
            pass
    finally:
        os.close(directory_fd)


def _interaction_ids(interaction: object) -> tuple[object, object, object]:
    user = getattr(interaction, "user", None)
    return (
        getattr(user, "id", None),
        getattr(interaction, "guild_id", None),
        getattr(interaction, "channel_id", None),
    )


def _is_allowed_context(service: ApprovalService, interaction: object) -> bool:
    user_id, guild_id, channel_id = _interaction_ids(interaction)
    try:
        service.assert_context(
            user_id=user_id, guild_id=guild_id, channel_id=channel_id
        )
    except ApprovalError:
        return False
    return True


def _safe_error_message(code: str) -> str:
    return {
        "approval_expired": "이 승인 요청은 만료되었습니다.",
        "decision_already_recorded": "이 계획의 결정은 이미 기록되었습니다.",
        "approval_hash_suffix_mismatch": "확인 코드가 계획 해시와 일치하지 않습니다.",
    }.get(code, "승인 요청을 처리하지 못했습니다.")


_PAPER_RUN_LABELS = {
    "ACTIVE": "진행 중",
    "WAITING": "계획 대기",
    "OPEN": "포지션 보유 중",
    "UNRESOLVED": "미해결 포지션",
    "INVALID": "데이터 무효",
    "BLOCKED": "차단됨",
    "INCOMPLETE": "미완료",
    "COMPLETE": "완료",
}
_PAPER_DAY_LABELS = {
    "WAITING_ENTRY": "진입 대기",
    "OPEN": "포지션 보유 중",
    "CLOSED": "청산 완료",
    "NO_ENTRY": "진입 없음",
    "INVALID": "데이터 무효",
    "UNRESOLVED": "미해결",
    "MARKET_CLOSED": "휴장",
    "NO_PLAN": "계획 없음",
}
_PAPER_BLOCKER_LABELS = {
    "intraday_simulation_not_started": "모의투자 시작일 전",
    "intraday_simulation_complete": "모의투자 완료",
    "intraday_simulation_incomplete": "모의투자 기간 종료 후 미완료 항목 확인 필요",
    "intraday_market_holiday": "미국장 휴장",
    "intraday_plan_window_not_started": "장전 계획 시간 대기",
    "intraday_plan_deadline_missed": "장전 계획 마감 지남",
    "intraday_not_in_premarket": "프리마켓 시간 밖",
    "intraday_no_eligible_candidate": "조건을 충족한 종목 없음",
    "intraday_simulation_blocked": "모의투자 안전 조건 차단",
    "intraday_simulation_integrity_failure": "모의투자 데이터 무결성 확인 필요",
    "intraday_read_or_integrity_failure": "조회 또는 데이터 무결성 확인 필요",
    "planner_configuration_blocked": "플래너 설정 확인 필요",
}


def _paper_usd(value: object, *, signed: bool = False) -> str:
    amount = Decimal(str(value))
    prefix = "+" if signed and amount > 0 else "-" if amount < 0 else ""
    return f"{prefix}${abs(amount):,.2f}"


def _paper_percent(value: object, *, signed: bool = False) -> str:
    percent = Decimal(str(value)) * Decimal("100")
    prefix = "+" if signed and percent > 0 else "-" if percent < 0 else ""
    return f"{prefix}{abs(percent):,.2f}%"


def render_paper_status(snapshot: Mapping[str, Any]) -> str:
    """Render only the strict, redacted status schema accepted by the reader."""

    run_status = str(snapshot["run_status"])
    ready = snapshot["planner_ready"] is True
    blockers = snapshot["blocker_codes"]
    planner_label = "정상"
    if not ready:
        first = str(blockers[0]) if blockers else "planner_configuration_blocked"
        planner_label = _PAPER_BLOCKER_LABELS.get(first, "안전 조건 확인 대기")
    win_rate = snapshot["win_rate"]
    return_fraction = snapshot["return_fraction"]
    max_drawdown_fraction = snapshot["max_drawdown_fraction"]
    lines = [
        "📊 한 달 모의투자 현황",
        f"상태: {_PAPER_RUN_LABELS.get(run_status, run_status)} · 플래너: {planner_label}",
        f"기간: {snapshot['start_date']} ~ {snapshot['end_date']}",
        (
            f"가상 현금: {_paper_usd(snapshot['current_cash_usd'])}"
            f" / 시작 {_paper_usd(snapshot['initial_cash_usd'])}"
        ),
        (
            f"확정 손익: {_paper_usd(snapshot['realized_pnl_usd'], signed=True)}"
            + (
                f" ({_paper_percent(return_fraction, signed=True)})"
                if return_fraction is not None
                else ""
            )
        ),
        (
            f"거래: {snapshot['trade_count']}회 · 승 {snapshot['wins']} / 패 {snapshot['losses']}"
            + (
                f" · 승률 {_paper_percent(win_rate)}"
                if win_rate is not None
                else ""
            )
        ),
        f"수수료: {_paper_usd(snapshot['total_fees_usd'])}",
        (
            f"최대 낙폭: {_paper_usd(snapshot['max_drawdown_usd'])}"
            f" ({_paper_percent(max_drawdown_fraction)})"
        ),
        (
            f"기간 기록: {snapshot['coverage_covered_count']}"
            f"/{snapshot['coverage_expected_count']}일"
            f" · 미기록 {snapshot['coverage_missing_count']}일"
        ),
        (
            f"미진입 {snapshot['no_entry_count']} · 대기 {snapshot['waiting_plan_count']}"
            f" · 무효 {snapshot['invalid_result_count']}"
            f" · 미해결 {snapshot['unresolved_position_count']}"
        ),
    ]
    final_equity = snapshot["final_equity_usd"]
    if final_equity is not None:
        equity_label = "최종 평가액" if run_status == "COMPLETE" else "현재 평가액"
        lines.append(f"{equity_label}: {_paper_usd(final_equity)}")
    latest = snapshot["latest_day"]
    if isinstance(latest, Mapping):
        symbol = latest["symbol"] or "-"
        lines.append(
            f"최근: {latest['session_date']} · {symbol} · "
            f"{_PAPER_DAY_LABELS.get(str(latest['status']), str(latest['status']))} · "
            f"손익 {_paper_usd(latest['net_pnl_usd'], signed=True)}"
        )
    updated_at = datetime.fromisoformat(
        str(snapshot["updated_at"]).replace("Z", "+00:00")
    ).astimezone(timezone(timedelta(hours=9)))
    lines.extend(
        (
            "실주문: 꺼짐 (모의투자 전용)",
            f"업데이트: {updated_at:%Y-%m-%d %H:%M:%S} KST",
        )
    )
    return "\n".join(lines)


def _discord_send_failure_is_definitive(error: BaseException) -> bool:
    status = getattr(error, "status", None)
    return (
        isinstance(status, int)
        and not isinstance(status, bool)
        and 400 <= status < 500
        and status != 408
    )


def create_discord_client(
    config: ApprovalConfig,
    *,
    service: ApprovalService | None = None,
    discord_module: Any | None = None,
    heartbeat: Callable[[str], None] | None = None,
    expected_release_sha: str | None = None,
) -> Any:
    """Build the Gateway client; discord.py is imported only at this boundary."""

    if discord_module is None:
        try:
            discord_module = importlib.import_module("discord")
        except ImportError as exc:
            raise ApprovalError("discord_dependency_missing") from exc
    discord = discord_module
    approval_service = service or ApprovalService(config)
    allowed_mentions = discord.AllowedMentions.none()

    def view_for(envelope: ApprovalEnvelope, *, disabled: bool = False) -> Any:
        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label="승인 기록" if not disabled else "승인 기록 완료",
                style=discord.ButtonStyle.success,
                custom_id=make_approve_custom_id(envelope),
                disabled=disabled,
            )
        )
        return view

    class HashConfirmationModal(discord.ui.Modal):
        def __init__(self, envelope: ApprovalEnvelope) -> None:
            super().__init__(
                title="계획 해시 확인",
                custom_id=make_confirm_custom_id(envelope),
                timeout=300,
            )
            self.add_item(
                discord.ui.TextInput(
                    label=f"계획 해시 끝 {HASH_SUFFIX_LENGTH}자리 입력",
                    custom_id=HASH_INPUT_CUSTOM_ID,
                    min_length=HASH_SUFFIX_LENGTH,
                    max_length=HASH_SUFFIX_LENGTH,
                    required=True,
                )
            )

    class ApprovalDiscordClient(discord.Client):
        def __init__(self) -> None:
            super().__init__(
                intents=discord.Intents.none(),
                allowed_mentions=allowed_mentions,
            )
            self._approval_task: asyncio.Task[None] | None = None
            self._posted_key: str | None = None
            self._last_loop_error: str | None = None
            self._command_guild = discord.Object(id=int(config.guild_id))
            self._command_tree = discord.app_commands.CommandTree(self)

            async def paper_status(interaction):
                await self._handle_paper_status(interaction)

            self._paper_status_command = self._command_tree.command(
                name="현황",
                description="한 달 모의투자 현황을 확인합니다.",
                guild=self._command_guild,
            )(paper_status)

        async def setup_hook(self) -> None:
            await self._command_tree.sync(guild=self._command_guild)
            self._approval_task = asyncio.create_task(self._approval_loop())

        async def close(self) -> None:
            if self._approval_task is not None:
                self._approval_task.cancel()
            await super().close()

        async def _approval_loop(self) -> None:
            await self.wait_until_ready()
            while not self.is_closed():
                heartbeat_status = "OK"
                try:
                    await self._publish_current()
                except ApprovalError as exc:
                    if exc.code in {
                        "approval_envelope_missing",
                        "approval_not_yet_valid",
                        "approval_expired",
                    }:
                        self._last_loop_error = None
                        heartbeat_status = "IDLE"
                    else:
                        self._report_loop_error(exc.code)
                        heartbeat_status = "DEGRADED"
                except OSError:
                    self._report_loop_error("approval_worker_io_failed")
                    heartbeat_status = "DEGRADED"
                except Exception:
                    self._report_loop_error("approval_worker_failed")
                    heartbeat_status = "DEGRADED"
                else:
                    self._last_loop_error = None
                if heartbeat is not None:
                    try:
                        heartbeat(heartbeat_status)
                    except Exception:
                        self._report_loop_error("approval_heartbeat_write_failed")
                        await super().close()
                        return
                await asyncio.sleep(config.poll_interval_seconds)

        async def _handle_paper_status(self, interaction: object) -> None:
            if not _is_allowed_context(approval_service, interaction):
                return
            try:
                if expected_release_sha is None:
                    raise PaperStatusError("paper_status_release_missing")
                snapshot = read_paper_status(
                    config.paper_status_path,
                    expected_release_sha=expected_release_sha,
                )
                message = render_paper_status(snapshot)
            except Exception:
                message = "현황을 확인할 수 없습니다. 잠시 후 다시 시도해 주세요."
            await interaction.response.send_message(
                message,
                ephemeral=True,
                allowed_mentions=allowed_mentions,
            )

        def _report_loop_error(self, code: str) -> None:
            if code == self._last_loop_error:
                return
            self._last_loop_error = code
            print(f"approval_loop_error:{code}", file=sys.stderr, flush=True)

        async def _publish_current(self) -> None:
            envelope = approval_service.current_envelope()
            if approval_service.receipt_exists(envelope):
                return
            if self._posted_key == envelope.key:
                return
            try:
                channel = await self.fetch_channel(int(config.channel_id))
            except Exception as exc:
                raise ApprovalError("discord_channel_fetch_failed") from exc
            channel_guild = getattr(channel, "guild", None)
            if (
                str(getattr(channel, "id", "")) != config.channel_id
                or str(getattr(channel_guild, "id", "")) != config.guild_id
                or not callable(getattr(channel, "send", None))
            ):
                raise ApprovalError("discord_channel_mismatch")
            # An accepted request followed by an ambiguous network error must not spam.
            self._posted_key = envelope.key
            try:
                await channel.send(
                    render_approval_message(envelope),
                    view=view_for(envelope),
                    allowed_mentions=allowed_mentions,
                )
            except Exception as exc:
                if _discord_send_failure_is_definitive(exc):
                    self._posted_key = None
                raise ApprovalError("discord_send_failed") from exc

        async def _respond_error(self, interaction: object, code: str) -> None:
            response = getattr(interaction, "response", None)
            if response is None:
                return
            try:
                if not response.is_done():
                    await response.send_message(
                        _safe_error_message(code),
                        ephemeral=True,
                        allowed_mentions=allowed_mentions,
                    )
                else:
                    followup = getattr(interaction, "followup", None)
                    if followup is not None:
                        await followup.send(
                            _safe_error_message(code),
                            ephemeral=True,
                            allowed_mentions=allowed_mentions,
                        )
            except Exception:
                pass

        async def on_interaction(self, interaction: object) -> None:
            data = getattr(interaction, "data", None)
            if not isinstance(data, Mapping):
                return
            custom_id = data.get("custom_id")
            if not isinstance(custom_id, str) or not custom_id.startswith("ta:"):
                return
            if not _is_allowed_context(approval_service, interaction):
                return
            user_id, guild_id, channel_id = _interaction_ids(interaction)
            interaction_type = getattr(getattr(interaction, "type", None), "value", None)
            try:
                if custom_id.startswith("ta:a:"):
                    if interaction_type != 3:
                        raise ApprovalError("approval_interaction_invalid")
                    envelope = approval_service.begin(
                        custom_id=custom_id,
                        user_id=user_id,
                        guild_id=guild_id,
                        channel_id=channel_id,
                    )
                    await interaction.response.send_modal(
                        HashConfirmationModal(envelope)
                    )
                    return
                if custom_id.startswith("ta:c:"):
                    if interaction_type != 5:
                        raise ApprovalError("approval_interaction_invalid")
                    await interaction.response.defer(ephemeral=True, thinking=True)
                    await asyncio.to_thread(
                        approval_service.approve,
                        custom_id=custom_id,
                        hash_suffix=extract_hash_suffix(data),
                        interaction_id=getattr(interaction, "id", None),
                        user_id=user_id,
                        guild_id=guild_id,
                        channel_id=channel_id,
                    )
                    await interaction.followup.send(
                        "승인 영수증이 기록되었습니다. 이 워커는 주문을 제출하지 않습니다.",
                        ephemeral=True,
                        allowed_mentions=allowed_mentions,
                    )
            except ApprovalError as exc:
                await self._respond_error(interaction, exc.code)
            except Exception:
                await self._respond_error(interaction, "approval_worker_failed")

    return ApprovalDiscordClient()


def main(
    env: Mapping[str, str] | None = None,
    *,
    heartbeat: Callable[[str], None] | None = None,
    expected_release_sha: str | None = None,
) -> int:
    try:
        config = ApprovalConfig.from_env(env)
        if env is None:
            os.environ.pop(TOKEN_ENV, None)
        client = create_discord_client(
            config,
            heartbeat=heartbeat,
            expected_release_sha=expected_release_sha,
        )
        client.run(config.bot_token, reconnect=True, log_handler=None)
    except KeyboardInterrupt:
        return 130
    except ApprovalError as exc:
        print(exc.code, file=sys.stderr)
        return 2
    except Exception:
        print("approval_worker_failed", file=sys.stderr)
        return 1
    return 0
