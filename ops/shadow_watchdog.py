#!/usr/bin/env python3
"""Trading-unprivileged shadow heartbeat watchdog.

This file is intentionally standalone and standard-library-only.  It reads the
redacted heartbeat contract and launchd state; it does not import the trading
package, open SQLite, or perform network I/O.
"""

from __future__ import annotations

import ctypes
import datetime as dt
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence
from zoneinfo import ZoneInfo


HEARTBEAT_KEYS = frozenset(
    {
        "schema_version",
        "release_sha",
        "boot_id_hash",
        "component",
        "launchd_label",
        "mode",
        "live_order_submission",
        "updated_at",
        "status_code",
        "stream_ack_ok",
        "baseline_fresh",
        "db_quick_check",
    }
)
ALLOWED_STATUS_CODES = frozenset(
    {"OK", "IDLE", "STARTING", "DEGRADED", "ERROR", "STOPPED"}
)
HEALTHY_STATUS_CODES = frozenset({"OK", "IDLE"})
DB_CHECK_VALUES = frozenset({"ok", "fail", "not_applicable"})
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_RELEASE_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_DOMAIN = re.compile(r"gui/(?:0|[1-9][0-9]*)\Z")
_UTC_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?"
    r"(?:Z|[+-][0-9]{2}:[0-9]{2})\Z"
)
_LAUNCHD_FIELD = re.compile(r"^\s*([a-z][a-z ]*[a-z]|[a-z])\s*=\s*(.*?)\s*$")
_SYMBOL = re.compile(r"(?=.{1,16}\Z)[A-Z][A-Z0-9]*(?:[.-][A-Z0-9]+)?\Z")
STREAM_CONTEXT_KEYS = frozenset(
    {
        "schema_version",
        "generated_at",
        "market",
        "session_date",
        "active_until",
        "symbol",
        "reason",
    }
)
STREAM_EXPECTATION_KEYS = frozenset(
    {
        "schema_version",
        "session_date",
        "expected_from",
        "expected_until",
        "reason",
    }
)


class WatchdogError(RuntimeError):
    """Fail-closed error carrying only a public, allowlisted code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ComponentSpec:
    component: str
    launchd_label: str
    max_age_seconds: int
    continuous: bool
    require_stream_ack: bool = False
    require_baseline_fresh: bool = False
    expected_db_quick_check: str = "not_applicable"


COMPONENTS: tuple[ComponentSpec, ...] = (
    ComponentSpec(
        "planner",
        "com.sands15.toss-intraday-shadow",
        130,
        True,
        require_baseline_fresh=True,
        expected_db_quick_check="ok",
    ),
    ComponentSpec(
        "stream",
        "com.sands15.toss-market-stream-shadow",
        30,
        True,
        require_stream_ack=True,
        require_baseline_fresh=True,
    ),
    ComponentSpec(
        "approval",
        "com.sands15.toss-discord-approval",
        130,
        True,
    ),
    ComponentSpec(
        "news",
        "com.sands15.toss-news-shadow",
        1_020,
        False,
    ),
)
ALLOWED_LAUNCHD_LABELS = frozenset(spec.launchd_label for spec in COMPONENTS)


@dataclass(frozen=True)
class Heartbeat:
    updated_at: dt.datetime
    status_code: str
    stream_ack_ok: bool
    baseline_fresh: bool
    db_quick_check: str


@dataclass(frozen=True)
class LaunchdStatus:
    state: str
    last_exit_code: int | None


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise WatchdogError("HEARTBEAT_JSON_INVALID")
        value[key] = item
    return value


def _read_small_regular_file(path: Path, *, limit: int, code: str) -> bytes:
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise WatchdogError(code)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
    except WatchdogError:
        raise
    except (FileNotFoundError, OSError) as exc:
        raise WatchdogError(code) from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > limit:
            raise WatchdogError(code)
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 4096))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > limit:
            raise WatchdogError(code)
        return payload
    finally:
        os.close(descriptor)


def _parse_timestamp(value: object) -> dt.datetime:
    if type(value) is not str or not _UTC_TIMESTAMP.fullmatch(value):
        raise WatchdogError("HEARTBEAT_TIMESTAMP_INVALID")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise WatchdogError("HEARTBEAT_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WatchdogError("HEARTBEAT_TIMESTAMP_INVALID")
    return parsed.astimezone(dt.timezone.utc)


def stream_context_state(path: Path, *, now: dt.datetime) -> str:
    """Return active or idle for the exact redacted selected-symbol context."""

    if not path.is_absolute() or path.name != "news-context.json":
        raise WatchdogError("STREAM_CONTEXT_INVALID")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return "idle"
    except OSError as exc:
        raise WatchdogError("STREAM_CONTEXT_INVALID") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise WatchdogError("STREAM_CONTEXT_INVALID")
    if os.name != "nt" and (
        metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise WatchdogError("STREAM_CONTEXT_INVALID")
    raw = _read_small_regular_file(
        path, limit=8192, code="STREAM_CONTEXT_INVALID"
    )
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except WatchdogError as exc:
        raise WatchdogError("STREAM_CONTEXT_INVALID") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WatchdogError("STREAM_CONTEXT_INVALID") from exc
    if (
        type(value) is not dict
        or frozenset(value) != STREAM_CONTEXT_KEYS
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["market"] != "US"
        or value["reason"] != "intraday_plan"
        or type(value["symbol"]) is not str
        or _SYMBOL.fullmatch(value["symbol"]) is None
        or type(value["session_date"]) is not str
    ):
        raise WatchdogError("STREAM_CONTEXT_INVALID")
    try:
        session_date = dt.date.fromisoformat(value["session_date"])
    except ValueError as exc:
        raise WatchdogError("STREAM_CONTEXT_INVALID") from exc
    if session_date.isoformat() != value["session_date"] or session_date.weekday() >= 5:
        raise WatchdogError("STREAM_CONTEXT_INVALID")
    try:
        generated_at = _parse_timestamp(value["generated_at"])
        active_until = _parse_timestamp(value["active_until"])
    except WatchdogError as exc:
        raise WatchdogError("STREAM_CONTEXT_INVALID") from exc
    if now.tzinfo is None or now.utcoffset() is None:
        raise WatchdogError("STREAM_CONTEXT_INVALID")
    current = now.astimezone(dt.timezone.utc)
    new_york = ZoneInfo("America/New_York")
    if active_until.astimezone(new_york).date() != session_date:
        raise WatchdogError("STREAM_CONTEXT_INVALID")
    if active_until <= current:
        return "idle"
    if (
        generated_at > active_until
        or generated_at.astimezone(new_york).date() != session_date
    ):
        raise WatchdogError("STREAM_CONTEXT_INVALID")
    age = (current - generated_at).total_seconds()
    if (
        session_date != current.astimezone(new_york).date()
        or age < -30
        or age > 300
    ):
        raise WatchdogError("STREAM_CONTEXT_INVALID")
    return "active"


def stream_expectation_state(path: Path, *, now: dt.datetime) -> str:
    """Return whether the planner durably requires its selected-symbol stream."""

    if not path.is_absolute() or path.name != "stream-expectation.json":
        raise WatchdogError("STREAM_EXPECTATION_INVALID")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return "idle"
    except OSError as exc:
        raise WatchdogError("STREAM_EXPECTATION_INVALID") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise WatchdogError("STREAM_EXPECTATION_INVALID")
    if os.name != "nt" and (
        metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise WatchdogError("STREAM_EXPECTATION_INVALID")
    raw = _read_small_regular_file(
        path, limit=4096, code="STREAM_EXPECTATION_INVALID"
    )
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except WatchdogError as exc:
        raise WatchdogError("STREAM_EXPECTATION_INVALID") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WatchdogError("STREAM_EXPECTATION_INVALID") from exc
    if (
        type(value) is not dict
        or frozenset(value) != STREAM_EXPECTATION_KEYS
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["reason"] != "intraday_paper_stream"
        or type(value["session_date"]) is not str
    ):
        raise WatchdogError("STREAM_EXPECTATION_INVALID")
    try:
        session_date = dt.date.fromisoformat(value["session_date"])
        expected_from = _parse_timestamp(value["expected_from"])
        expected_until = _parse_timestamp(value["expected_until"])
    except (ValueError, WatchdogError) as exc:
        raise WatchdogError("STREAM_EXPECTATION_INVALID") from exc
    if now.tzinfo is None or now.utcoffset() is None:
        raise WatchdogError("STREAM_EXPECTATION_INVALID")
    current = now.astimezone(dt.timezone.utc)
    new_york = ZoneInfo("America/New_York")
    if (
        session_date.isoformat() != value["session_date"]
        or session_date.weekday() >= 5
        or expected_from >= expected_until
        or expected_from.astimezone(new_york).date() != session_date
        or expected_until.astimezone(new_york).date() != session_date
    ):
        raise WatchdogError("STREAM_EXPECTATION_INVALID")
    if expected_until <= current:
        return "idle"
    if expected_from > current + dt.timedelta(seconds=30):
        raise WatchdogError("STREAM_EXPECTATION_INVALID")
    if current < expected_from:
        return "idle"
    if current.astimezone(new_york).date() != session_date:
        raise WatchdogError("STREAM_EXPECTATION_INVALID")
    return "active"


def _require_expected_hashes(release_sha: str, boot_id_hash: str) -> None:
    if not _RELEASE_SHA.fullmatch(release_sha):
        raise WatchdogError("EXPECTED_RELEASE_INVALID")
    if not _HEX_64.fullmatch(boot_id_hash):
        raise WatchdogError("EXPECTED_BOOT_INVALID")


def read_heartbeat(
    path: Path,
    spec: ComponentSpec,
    *,
    expected_release_sha: str,
    expected_boot_id_hash: str,
    now: dt.datetime,
    future_skew_seconds: int = 5,
) -> Heartbeat:
    """Read and validate one exact, redacted heartbeat."""

    _require_expected_hashes(expected_release_sha, expected_boot_id_hash)
    if now.tzinfo is None or now.utcoffset() is None:
        raise WatchdogError("WATCHDOG_CLOCK_INVALID")
    raw = _read_small_regular_file(path, limit=4096, code="HEARTBEAT_UNREADABLE")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except WatchdogError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WatchdogError("HEARTBEAT_JSON_INVALID") from exc
    if type(value) is not dict or frozenset(value) != HEARTBEAT_KEYS:
        raise WatchdogError("HEARTBEAT_SCHEMA_INVALID")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise WatchdogError("HEARTBEAT_SCHEMA_INVALID")
    if value["mode"] != "shadow" or type(value["mode"]) is not str:
        raise WatchdogError("HARD_OFF_INVARIANT_FAILED")
    if value["live_order_submission"] is not False:
        raise WatchdogError("HARD_OFF_INVARIANT_FAILED")
    if value["release_sha"] != expected_release_sha:
        raise WatchdogError("RELEASE_MISMATCH")
    if value["boot_id_hash"] != expected_boot_id_hash:
        raise WatchdogError("BOOT_MISMATCH")
    if value["component"] != spec.component:
        raise WatchdogError("COMPONENT_MISMATCH")
    if value["launchd_label"] != spec.launchd_label:
        raise WatchdogError("LAUNCHD_LABEL_MISMATCH")
    if type(value["stream_ack_ok"]) is not bool:
        raise WatchdogError("HEARTBEAT_SCHEMA_INVALID")
    if type(value["baseline_fresh"]) is not bool:
        raise WatchdogError("HEARTBEAT_SCHEMA_INVALID")
    if type(value["status_code"]) is not str or value["status_code"] not in ALLOWED_STATUS_CODES:
        raise WatchdogError("HEARTBEAT_STATUS_INVALID")
    if type(value["db_quick_check"]) is not str or value["db_quick_check"] not in DB_CHECK_VALUES:
        raise WatchdogError("HEARTBEAT_DB_CHECK_INVALID")

    updated_at = _parse_timestamp(value["updated_at"])
    age = (now.astimezone(dt.timezone.utc) - updated_at).total_seconds()
    if age < -future_skew_seconds:
        raise WatchdogError("HEARTBEAT_CLOCK_SKEW")
    if age > spec.max_age_seconds:
        raise WatchdogError("HEARTBEAT_STALE")
    return Heartbeat(
        updated_at=updated_at,
        status_code=value["status_code"],
        stream_ack_ok=value["stream_ack_ok"],
        baseline_fresh=value["baseline_fresh"],
        db_quick_check=value["db_quick_check"],
    )


def _launchd_fields(output: str) -> Mapping[str, str]:
    fields: dict[str, str] = {}
    for line in output.splitlines():
        matched = _LAUNCHD_FIELD.fullmatch(line)
        if matched is not None and matched.group(1) in {"state", "last exit code"}:
            fields[matched.group(1)] = matched.group(2)
    return fields


def probe_launchd(
    domain: str,
    spec: ComponentSpec,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> LaunchdStatus:
    """Query one compile-time-allowlisted launchd job without a shell."""

    if not _DOMAIN.fullmatch(domain):
        raise WatchdogError("LAUNCHD_DOMAIN_INVALID")
    if spec.launchd_label not in ALLOWED_LAUNCHD_LABELS:
        raise WatchdogError("LAUNCHD_LABEL_NOT_ALLOWED")
    try:
        result = runner(
            ["/bin/launchctl", "print", f"{domain}/{spec.launchd_label}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WatchdogError("LAUNCHD_QUERY_FAILED") from exc
    if result.returncode != 0:
        return LaunchdStatus("not_loaded", None)
    fields = _launchd_fields(result.stdout)
    state = fields.get("state")
    if state not in {"running", "not running", "waiting"}:
        raise WatchdogError("LAUNCHD_RESPONSE_INVALID")
    exit_text = fields.get("last exit code")
    try:
        last_exit_code = None if exit_text is None else int(exit_text, 10)
    except ValueError as exc:
        raise WatchdogError("LAUNCHD_RESPONSE_INVALID") from exc
    return LaunchdStatus(state.replace(" ", "_"), last_exit_code)


def component_health(
    heartbeat_root: Path,
    spec: ComponentSpec,
    *,
    expected_release_sha: str,
    expected_boot_id_hash: str,
    launchd_domain: str,
    now: dt.datetime,
    required: bool | None = None,
    launchctl_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    """Return one allowlisted public health code and no sensitive diagnostics."""

    if required is False:
        try:
            launchd = probe_launchd(launchd_domain, spec, runner=launchctl_runner)
        except WatchdogError as exc:
            return exc.code
        return "PROCESS_NOT_LOADED" if launchd.state == "not_loaded" else "OK"
    try:
        heartbeat = read_heartbeat(
            heartbeat_root / spec.component / "heartbeat.json",
            spec,
            expected_release_sha=expected_release_sha,
            expected_boot_id_hash=expected_boot_id_hash,
            now=now,
        )
        launchd = probe_launchd(launchd_domain, spec, runner=launchctl_runner)
    except WatchdogError as exc:
        return exc.code

    if launchd.state == "not_loaded":
        return "PROCESS_NOT_LOADED"
    if spec.continuous and launchd.state != "running":
        return "PROCESS_NOT_RUNNING"
    if not spec.continuous and launchd.state != "running" and launchd.last_exit_code != 0:
        return "PROCESS_LAST_EXIT_FAILED"
    if heartbeat.status_code not in HEALTHY_STATUS_CODES:
        return f"COMPONENT_{heartbeat.status_code}"
    if heartbeat.db_quick_check != spec.expected_db_quick_check:
        return "DB_QUICK_CHECK_FAILED"
    if spec.require_stream_ack and not heartbeat.stream_ack_ok:
        return "STREAM_ACK_MISSING"
    if spec.require_baseline_fresh and not heartbeat.baseline_fresh:
        return "BASELINE_STALE"
    return "OK"


def _load_previous_state(path: Path, components: Sequence[ComponentSpec]) -> dict[str, str] | None:
    try:
        raw = _read_small_regular_file(path, limit=16_384, code="STATE_UNREADABLE")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (WatchdogError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    names = {spec.component for spec in components}
    if (
        type(value) is not dict
        or frozenset(value) != {"schema_version", "components"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or type(value["components"]) is not dict
        or set(value["components"]) != names
        or any(type(code) is not str for code in value["components"].values())
    ):
        return None
    return dict(value["components"])


def _store_state(path: Path, state: Mapping[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = json.dumps(
        {"schema_version": 1, "components": dict(sorted(state.items()))},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def run_once(
    *,
    heartbeat_root: Path,
    state_path: Path,
    expected_release_sha: str,
    expected_boot_id_hash: str,
    launchd_domain: str,
    sender: Callable[[Mapping[str, object]], None],
    now: dt.datetime,
    stream_context_path: Path | None = None,
    stream_expectation_path: Path | None = None,
    components: Sequence[ComponentSpec] = COMPONENTS,
    launchctl_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> Mapping[str, str]:
    """Evaluate all components and emit only startup, changes, and recovery."""

    _require_expected_hashes(expected_release_sha, expected_boot_id_hash)
    if not heartbeat_root.is_absolute() or not state_path.is_absolute():
        raise WatchdogError("WATCHDOG_PATH_INVALID")
    names = [spec.component for spec in components]
    if len(names) != len(set(names)) or not names:
        raise WatchdogError("COMPONENT_SET_INVALID")
    stream_context = "active"
    stream_expectation: str | None = None
    if stream_expectation_path is not None:
        try:
            stream_expectation = stream_expectation_state(
                stream_expectation_path, now=now
            )
        except WatchdogError as exc:
            stream_expectation = exc.code
    if stream_context_path is not None:
        try:
            stream_context = stream_context_state(stream_context_path, now=now)
        except WatchdogError as exc:
            stream_context = exc.code
    if stream_expectation is None:
        stream_requirement = stream_context
    elif stream_expectation not in {"active", "idle"}:
        stream_requirement = stream_expectation
    elif stream_expectation == "active" and stream_context != "active":
        stream_requirement = "STREAM_CONTEXT_INVALID"
    elif stream_expectation == "idle" and stream_context == "active":
        stream_requirement = "STREAM_EXPECTATION_INVALID"
    elif stream_context not in {"active", "idle"}:
        stream_requirement = stream_context
    else:
        stream_requirement = stream_expectation
    current: dict[str, str] = {}
    for spec in components:
        if spec.component == "stream" and stream_requirement not in {"active", "idle"}:
            current[spec.component] = stream_requirement
            continue
        current[spec.component] = component_health(
            heartbeat_root,
            spec,
            expected_release_sha=expected_release_sha,
            expected_boot_id_hash=expected_boot_id_hash,
            launchd_domain=launchd_domain,
            now=now,
            required=(stream_requirement == "active") if spec.component == "stream" else None,
            launchctl_runner=launchctl_runner,
        )
    previous = _load_previous_state(state_path, components)
    alerts: list[dict[str, object]] = []
    if previous is None:
        alerts.append(
            {
                "schema_version": 1,
                "event": "WATCHDOG_STARTED",
                "component": "watchdog",
                "from": None,
                "to": "OK" if all(code == "OK" for code in current.values()) else "DEGRADED",
            }
        )
        for component, code in current.items():
            if code != "OK":
                alerts.append(
                    {
                        "schema_version": 1,
                        "event": "WATCHDOG_STATE_CHANGED",
                        "component": component,
                        "from": "UNKNOWN",
                        "to": code,
                    }
                )
    else:
        for component, code in current.items():
            old = previous[component]
            if old == code:
                continue
            alerts.append(
                {
                    "schema_version": 1,
                    "event": "WATCHDOG_RECOVERED" if code == "OK" else "WATCHDOG_STATE_CHANGED",
                    "component": component,
                    "from": old,
                    "to": code,
                }
            )
    for alert in alerts:
        sender(alert)
    _store_state(state_path, current)
    return current


def macos_boot_id_hash() -> str:
    """Return the documented hash of kern.bootsessionuuid via sysctlbyname."""

    if sys.platform != "darwin":
        raise WatchdogError("BOOT_ID_UNAVAILABLE")
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
    if sysctlbyname(b"kern.bootsessionuuid", None, ctypes.byref(size), None, 0) != 0:
        raise WatchdogError("BOOT_ID_UNAVAILABLE")
    if size.value < 2 or size.value > 128:
        raise WatchdogError("BOOT_ID_UNAVAILABLE")
    buffer = ctypes.create_string_buffer(size.value)
    if sysctlbyname(
        b"kern.bootsessionuuid", buffer, ctypes.byref(size), None, 0
    ) != 0:
        raise WatchdogError("BOOT_ID_UNAVAILABLE")
    try:
        canonical = str(uuid.UUID(buffer.value.decode("ascii").strip())).lower()
    except (UnicodeDecodeError, ValueError) as exc:
        raise WatchdogError("BOOT_ID_UNAVAILABLE") from exc
    return hashlib.sha256(b"macos-boot-v1\0" + canonical.encode("ascii")).hexdigest()


def _disable_core_dumps() -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if resource.getrlimit(resource.RLIMIT_CORE) != (0, 0):
            raise WatchdogError("CORE_LIMIT_INVALID")
    except ImportError:
        if sys.platform == "darwin":
            raise WatchdogError("CORE_LIMIT_INVALID")


def main() -> int:
    _disable_core_dumps()
    try:
        heartbeat_root = Path(os.environ.pop("TOSS_WATCHDOG_HEARTBEAT_ROOT"))
        stream_context_path = Path(os.environ.pop("TOSS_WATCHDOG_CONTEXT_PATH"))
        stream_expectation_path = Path(
            os.environ.pop("TOSS_WATCHDOG_EXPECTATION_PATH")
        )
        state_path = Path(os.environ.pop("TOSS_WATCHDOG_STATE_PATH"))
        release_sha = os.environ.pop("TOSS_WATCHDOG_RELEASE_SHA")
        launchd_domain = os.environ.pop("TOSS_WATCHDOG_LAUNCHD_DOMAIN")
    except KeyError:
        print("watchdog_configuration_invalid", file=sys.stderr)
        return 64
    try:
        run_once(
            heartbeat_root=heartbeat_root,
            stream_context_path=stream_context_path,
            stream_expectation_path=stream_expectation_path,
            state_path=state_path,
            expected_release_sha=release_sha,
            expected_boot_id_hash=macos_boot_id_hash(),
            launchd_domain=launchd_domain,
            sender=lambda alert: print(
                json.dumps(
                    alert,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                ),
                flush=True,
            ),
            now=dt.datetime.now(dt.timezone.utc),
        )
    except WatchdogError as exc:
        print(exc.code, file=sys.stderr)
        return 65
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
