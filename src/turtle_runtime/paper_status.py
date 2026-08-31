"""Owner-private, read-only paper-simulation status artifact."""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Mapping

from .heartbeat import HeartbeatError, _boot_id_hash


STATUS_NAME = "paper-status.json"
APPROVAL_ENVELOPE_NAME = "approval-envelope.json"
MAX_STATUS_BYTES = 16 * 1024
_FUTURE_TOLERANCE = timedelta(seconds=5)
_RELEASE_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_BLOCKER_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_GENERIC_BLOCKER = "planner_configuration_blocked"
_SYMBOL = re.compile(r"(?=.{1,16}\Z)[A-Z][A-Z0-9]*(?:[.\-][A-Z0-9]+)?\Z")
_MONTH_STATUSES = frozenset(
    {"UNRESOLVED", "OPEN", "WAITING", "INVALID", "BLOCKED", "ACTIVE", "INCOMPLETE", "COMPLETE"}
)
_DAY_STATUSES = frozenset(
    {"WAITING_ENTRY", "OPEN", "UNRESOLVED", "CLOSED", "INVALID", "NO_ENTRY", "NO_CANDIDATE", "MARKET_CLOSED", "NO_PLAN"}
)
_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "release_sha",
        "boot_id_hash",
        "mode",
        "live_order_submission",
        "updated_at",
        "planner_ready",
        "blocker_codes",
        "run_status",
        "start_date",
        "end_date",
        "initial_cash_usd",
        "current_cash_usd",
        "final_equity_usd",
        "realized_pnl_usd",
        "return_fraction",
        "trade_count",
        "wins",
        "losses",
        "win_rate",
        "total_fees_usd",
        "max_drawdown_usd",
        "max_drawdown_fraction",
        "no_entry_count",
        "no_candidate_count",
        "invalid_result_count",
        "unresolved_position_count",
        "waiting_plan_count",
        "coverage_expected_count",
        "coverage_covered_count",
        "coverage_missing_count",
        "latest_day",
    }
)
_LATEST_DAY_KEYS = frozenset(
    {
        "session_date",
        "symbol",
        "status",
        "net_pnl_usd",
        "fees_usd",
        "cash_start_usd",
        "cash_end_usd",
        "data_gap_count",
    }
)


class PaperStatusError(RuntimeError):
    """Fail-closed error carrying only a public code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def derive_paper_status_path(envelope_path: str | Path) -> Path:
    """Derive the one permitted status path beside an approval envelope."""

    path = Path(envelope_path)
    if not path.is_absolute() or path.name != APPROVAL_ENVELOPE_NAME:
        raise PaperStatusError("paper_status_configuration_invalid")
    return path.with_name(STATUS_NAME)


class PaperStatusWriter:
    """Publish an explicit safe subset of the paper database summary."""

    def __init__(
        self,
        path: str | Path,
        *,
        release_sha: str,
        boot_id_hash: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        target = Path(path)
        try:
            actual_boot_hash = boot_id_hash if boot_id_hash is not None else _boot_id_hash()
        except HeartbeatError as exc:
            raise PaperStatusError("paper_status_configuration_invalid") from exc
        if (
            not target.is_absolute()
            or target.name != STATUS_NAME
            or not _RELEASE_SHA.fullmatch(release_sha)
            or not _HEX_64.fullmatch(actual_boot_hash)
        ):
            raise PaperStatusError("paper_status_configuration_invalid")
        self.path = target
        self.release_sha = release_sha
        self.boot_id_hash = actual_boot_hash
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def write(
        self,
        month_summary: Mapping[str, object],
        *,
        planner_ready: bool,
        blocker_codes: object,
        latest_day: Mapping[str, object] | None = None,
    ) -> None:
        try:
            observed_at = _utc_clock(self.clock())
            blockers = _source_blocker_codes(blocker_codes)
            if type(planner_ready) is not bool or (planner_ready and blockers):
                raise ValueError
            coverage_expected = _count(month_summary.get("coverage_expected"))
            coverage_covered = _count(month_summary.get("coverage_covered"))
            coverage_missing = _count(month_summary.get("coverage_missing"))
            payload: dict[str, object] = {
                "schema_version": 2,
                "release_sha": self.release_sha,
                "boot_id_hash": self.boot_id_hash,
                "mode": "shadow",
                "live_order_submission": False,
                "updated_at": observed_at.isoformat(),
                "planner_ready": planner_ready,
                "blocker_codes": blockers,
                "run_status": _enum(month_summary.get("status"), _MONTH_STATUSES),
                "start_date": _date_string(month_summary.get("start_date")),
                "end_date": _date_string(month_summary.get("end_date")),
                "initial_cash_usd": _decimal_string(month_summary.get("initial_cash"), positive=True),
                "current_cash_usd": _decimal_string(month_summary.get("current_cash"), nonnegative=True),
                "final_equity_usd": _decimal_string(month_summary.get("final_equity"), nullable=True, nonnegative=True),
                "realized_pnl_usd": _decimal_string(month_summary.get("net_pnl")),
                "return_fraction": _decimal_string(month_summary.get("return_fraction"), nullable=True),
                "trade_count": _count(month_summary.get("trades")),
                "wins": _count(month_summary.get("wins")),
                "losses": _count(month_summary.get("losses")),
                "win_rate": _decimal_string(month_summary.get("win_rate"), nullable=True, fraction=True),
                "total_fees_usd": _decimal_string(month_summary.get("total_fees"), nonnegative=True),
                "max_drawdown_usd": _decimal_string(month_summary.get("max_drawdown"), nonnegative=True),
                "max_drawdown_fraction": _decimal_string(month_summary.get("max_drawdown_fraction"), fraction=True),
                "no_entry_count": _count(month_summary.get("no_entry_sessions")),
                "no_candidate_count": _count(month_summary.get("no_candidate_sessions")),
                "invalid_result_count": _count(month_summary.get("invalid_sessions")),
                "unresolved_position_count": _count(month_summary.get("unresolved_positions")),
                "waiting_plan_count": _count(month_summary.get("waiting_plans")),
                "coverage_expected_count": coverage_expected,
                "coverage_covered_count": coverage_covered,
                "coverage_missing_count": coverage_missing,
                "latest_day": _latest_day(latest_day),
            }
            _validate_payload(
                payload,
                expected_release_sha=self.release_sha,
                expected_boot_id_hash=self.boot_id_hash,
                now=observed_at,
                max_age_seconds=0,
            )
            raw = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("ascii")
            if len(raw) > MAX_STATUS_BYTES:
                raise ValueError
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise PaperStatusError("paper_status_value_invalid") from exc
        self._replace(raw)

    def _replace(self, payload: bytes) -> None:
        temporary: Path | None = None
        descriptor = -1
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _require_private_parent(self.path.parent)
            try:
                current = self.path.lstat()
            except FileNotFoundError:
                current = None
            if current is not None and (
                stat.S_ISLNK(current.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or current.st_nlink != 1
            ):
                raise PaperStatusError("paper_status_path_invalid")
            temporary = self.path.with_name(
                f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            )
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(temporary, flags, 0o600)
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("short paper status write")
                remaining = remaining[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, self.path)
            temporary = None
            _require_private_status_file(self.path)
        except PaperStatusError:
            raise
        except OSError as exc:
            raise PaperStatusError("paper_status_write_failed") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass


def read_paper_status(
    path: str | Path,
    *,
    expected_release_sha: str,
    expected_boot_id_hash: str | None = None,
    clock: Callable[[], datetime] | None = None,
    max_age_seconds: float = 130,
) -> Mapping[str, object]:
    """Read and strictly validate the status artifact without database access."""

    target = Path(path)
    try:
        boot_hash = expected_boot_id_hash if expected_boot_id_hash is not None else _boot_id_hash()
    except HeartbeatError as exc:
        raise PaperStatusError("paper_status_configuration_invalid") from exc
    if (
        not target.is_absolute()
        or target.name != STATUS_NAME
        or not _RELEASE_SHA.fullmatch(expected_release_sha)
        or not _HEX_64.fullmatch(boot_hash)
        or isinstance(max_age_seconds, bool)
        or not isinstance(max_age_seconds, (int, float))
        or max_age_seconds < 0
        or max_age_seconds > 3600
    ):
        raise PaperStatusError("paper_status_configuration_invalid")
    now = _utc_clock((clock or (lambda: datetime.now(timezone.utc)))())
    _require_private_parent(target.parent)
    raw = _read_private_status(target)
    try:
        parsed = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
        if not isinstance(parsed, dict):
            raise ValueError
        _validate_payload(
            parsed,
            expected_release_sha=expected_release_sha,
            expected_boot_id_hash=boot_hash,
            now=now,
            max_age_seconds=float(max_age_seconds),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise PaperStatusError("paper_status_invalid") from exc
    return parsed


def _latest_day(value: Mapping[str, object] | None) -> dict[str, object] | None:
    if value is None:
        return None
    status_code = _enum(value.get("status"), _DAY_STATUSES)
    symbol = value.get("symbol")
    if status_code in {"NO_CANDIDATE", "MARKET_CLOSED", "NO_PLAN"}:
        if symbol is not None:
            raise ValueError
        clean_symbol = None
    elif not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        raise ValueError
    else:
        clean_symbol = symbol
    return {
        "session_date": _date_string(value.get("session_date")),
        "symbol": clean_symbol,
        "status": status_code,
        "net_pnl_usd": _decimal_string(value.get("net_pnl")),
        "fees_usd": _decimal_string(value.get("fees"), nonnegative=True),
        "cash_start_usd": _decimal_string(value.get("cash_start"), nullable=True, nonnegative=True),
        "cash_end_usd": _decimal_string(value.get("cash_end"), nullable=True, nonnegative=True),
        "data_gap_count": _count(value.get("data_gaps")),
    }


def _validate_payload(
    payload: Mapping[str, object],
    *,
    expected_release_sha: str,
    expected_boot_id_hash: str,
    now: datetime,
    max_age_seconds: float,
) -> None:
    if set(payload) != _TOP_LEVEL_KEYS:
        raise ValueError
    if payload.get("schema_version") != 2 or type(payload.get("schema_version")) is not int:
        raise ValueError
    if payload.get("mode") != "shadow" or payload.get("live_order_submission") is not False:
        raise ValueError
    release_sha = payload.get("release_sha")
    boot_hash = payload.get("boot_id_hash")
    if not isinstance(release_sha, str) or not _RELEASE_SHA.fullmatch(release_sha):
        raise ValueError
    if not isinstance(boot_hash, str) or not _HEX_64.fullmatch(boot_hash):
        raise ValueError
    if release_sha != expected_release_sha:
        raise PaperStatusError("paper_status_release_mismatch")
    if boot_hash != expected_boot_id_hash:
        raise PaperStatusError("paper_status_boot_mismatch")
    updated_at = _utc_timestamp(payload.get("updated_at"))
    age = now - updated_at
    if age < -_FUTURE_TOLERANCE or age > timedelta(seconds=max_age_seconds):
        raise PaperStatusError("paper_status_stale")

    ready = payload.get("planner_ready")
    blockers = _canonical_blocker_codes(payload.get("blocker_codes"))
    if type(ready) is not bool or (ready and blockers):
        raise ValueError
    start = _date_string(payload.get("start_date"))
    end = _date_string(payload.get("end_date"))
    if start > end:
        raise ValueError
    _enum(payload.get("run_status"), _MONTH_STATUSES)
    _decimal_string(payload.get("initial_cash_usd"), positive=True)
    _decimal_string(payload.get("current_cash_usd"), nonnegative=True)
    _decimal_string(payload.get("final_equity_usd"), nullable=True, nonnegative=True)
    _decimal_string(payload.get("realized_pnl_usd"))
    _decimal_string(payload.get("return_fraction"), nullable=True)
    trades = _count(payload.get("trade_count"))
    wins = _count(payload.get("wins"))
    losses = _count(payload.get("losses"))
    if wins + losses > trades:
        raise ValueError
    _decimal_string(payload.get("win_rate"), nullable=True, fraction=True)
    _decimal_string(payload.get("total_fees_usd"), nonnegative=True)
    _decimal_string(payload.get("max_drawdown_usd"), nonnegative=True)
    _decimal_string(payload.get("max_drawdown_fraction"), fraction=True)
    _count(payload.get("no_entry_count"))
    no_candidates = _count(payload.get("no_candidate_count"))
    _count(payload.get("invalid_result_count"))
    _count(payload.get("unresolved_position_count"))
    _count(payload.get("waiting_plan_count"))
    expected = _count(payload.get("coverage_expected_count"))
    covered = _count(payload.get("coverage_covered_count"))
    missing = _count(payload.get("coverage_missing_count"))
    if covered > expected or missing != expected - covered:
        raise ValueError
    if no_candidates > covered:
        raise ValueError

    latest = payload.get("latest_day")
    if latest is not None:
        if not isinstance(latest, Mapping) or set(latest) != _LATEST_DAY_KEYS:
            raise ValueError
        latest_date = _date_string(latest.get("session_date"))
        if not start <= latest_date <= end:
            raise ValueError
        status_code = _enum(latest.get("status"), _DAY_STATUSES)
        if status_code == "NO_CANDIDATE" and no_candidates == 0:
            raise ValueError
        symbol = latest.get("symbol")
        if status_code in {"NO_CANDIDATE", "MARKET_CLOSED", "NO_PLAN"}:
            if symbol is not None:
                raise ValueError
        elif not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
            raise ValueError
        _decimal_string(latest.get("net_pnl_usd"))
        _decimal_string(latest.get("fees_usd"), nonnegative=True)
        _decimal_string(latest.get("cash_start_usd"), nullable=True, nonnegative=True)
        _decimal_string(latest.get("cash_end_usd"), nullable=True, nonnegative=True)
        _count(latest.get("data_gap_count"))


def _read_private_status(path: Path) -> bytes:
    try:
        path_info = _require_private_status_file(path)
    except FileNotFoundError as exc:
        raise PaperStatusError("paper_status_missing") from exc
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size < 1
            or info.st_size > MAX_STATUS_BYTES
            or (path_info.st_dev, path_info.st_ino) != (info.st_dev, info.st_ino)
        ):
            raise PaperStatusError("paper_status_invalid")
        _require_owner_mode(info)
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            raw = handle.read(MAX_STATUS_BYTES + 1)
        if len(raw) != info.st_size:
            raise PaperStatusError("paper_status_invalid")
        return raw
    except PaperStatusError:
        raise
    except FileNotFoundError as exc:
        raise PaperStatusError("paper_status_missing") from exc
    except OSError as exc:
        raise PaperStatusError("paper_status_invalid") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _require_private_parent(path: Path) -> None:
    if not path.is_absolute():
        raise PaperStatusError("paper_status_path_invalid")
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise PaperStatusError("paper_status_path_invalid") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise PaperStatusError("paper_status_path_invalid")
    if os.path.normcase(os.fspath(resolved)) != os.path.normcase(os.fspath(path)):
        raise PaperStatusError("paper_status_path_invalid")
    _require_owner_directory(info)


def _require_private_status_file(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise PaperStatusError("paper_status_invalid") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise PaperStatusError("paper_status_invalid")
    _require_owner_mode(info)
    return info


def _require_owner_directory(info: os.stat_result) -> None:
    if os.name != "nt":
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise PaperStatusError("paper_status_permissions_invalid")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise PaperStatusError("paper_status_permissions_invalid")


def _require_owner_mode(info: os.stat_result) -> None:
    if os.name != "nt":
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise PaperStatusError("paper_status_permissions_invalid")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise PaperStatusError("paper_status_permissions_invalid")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_json_constant(_: str) -> None:
    raise ValueError


def _utc_clock(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PaperStatusError("paper_status_clock_invalid")
    return value.astimezone(timezone.utc)


def _utc_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError from exc
    if parsed.tzinfo != timezone.utc or parsed.isoformat() != value:
        raise ValueError
    return parsed


def _date_string(value: object) -> str:
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError from exc
    if parsed.isoformat() != value:
        raise ValueError
    return value


def _enum(value: object, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError
    return value


def _count(value: object) -> int:
    if type(value) is not int or value < 0 or value > 10_000_000:
        raise ValueError
    return value


def _source_blocker_codes(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)) or len(value) > 64:
        return [_GENERIC_BLOCKER]
    result = {
        item if isinstance(item, str) and _BLOCKER_CODE.fullmatch(item) else _GENERIC_BLOCKER
        for item in value
    }
    return sorted(result) if len(result) <= 16 else [_GENERIC_BLOCKER]


def _canonical_blocker_codes(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > 16:
        raise ValueError
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not _BLOCKER_CODE.fullmatch(item) or item in result:
            raise ValueError
        result.append(item)
    if result != sorted(result):
        raise ValueError
    return result


def _decimal_string(
    value: object,
    *,
    nullable: bool = False,
    nonnegative: bool = False,
    positive: bool = False,
    fraction: bool = False,
) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError from exc
    if not parsed.is_finite():
        raise ValueError
    canonical = "0" if parsed == 0 else format(parsed.normalize(), "f")
    if value != canonical:
        raise ValueError
    if positive and parsed <= 0:
        raise ValueError
    if nonnegative and parsed < 0:
        raise ValueError
    if fraction and not Decimal("0") <= parsed <= Decimal("1"):
        raise ValueError
    return value
