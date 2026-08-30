"""Atomic, secret-free heartbeat writer for the macOS shadow jobs."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


_LABELS = {
    "planner": "com.sands15.toss-intraday-shadow",
    "stream": "com.sands15.toss-market-stream-shadow",
    "approval": "com.sands15.toss-discord-approval",
    "news": "com.sands15.toss-news-shadow",
}
_STATUS_CODES = frozenset({"OK", "IDLE", "STARTING", "DEGRADED", "ERROR", "STOPPED"})
_DB_CHECK_VALUES = frozenset({"ok", "fail", "not_applicable"})
_RELEASE_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")


class HeartbeatError(RuntimeError):
    """Fail-closed error carrying only a public code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _boot_id_hash() -> str:
    if sys.platform != "darwin":
        raise HeartbeatError("heartbeat_boot_id_unavailable")
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
        raise HeartbeatError("heartbeat_boot_id_unavailable")
    if size.value < 2 or size.value > 128:
        raise HeartbeatError("heartbeat_boot_id_unavailable")
    buffer = ctypes.create_string_buffer(size.value)
    if sysctlbyname(
        b"kern.bootsessionuuid", buffer, ctypes.byref(size), None, 0
    ) != 0:
        raise HeartbeatError("heartbeat_boot_id_unavailable")
    try:
        canonical = str(uuid.UUID(buffer.value.decode("ascii").strip())).lower()
    except (UnicodeDecodeError, ValueError) as exc:
        raise HeartbeatError("heartbeat_boot_id_unavailable") from exc
    return hashlib.sha256(b"macos-boot-v1\0" + canonical.encode("ascii")).hexdigest()


class RedactedHeartbeatWriter:
    """Publish the watchdog's exact allowlisted schema with no private fields."""

    def __init__(
        self,
        path: str | Path,
        *,
        release_sha: str,
        component: str,
        boot_id_hash: str | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        target = Path(path)
        expected_label = _LABELS.get(component)
        actual_boot_hash = boot_id_hash if boot_id_hash is not None else _boot_id_hash()
        if (
            not target.is_absolute()
            or target.name != "heartbeat.json"
            or target.parent.name != component
            or expected_label is None
            or not _RELEASE_SHA.fullmatch(release_sha)
            or not _HEX_64.fullmatch(actual_boot_hash)
        ):
            raise HeartbeatError("heartbeat_configuration_invalid")
        self.path = target
        self.release_sha = release_sha
        self.boot_id_hash = actual_boot_hash
        self.component = component
        self.launchd_label = expected_label
        self.clock = clock

    def write(
        self,
        status_code: str,
        *,
        stream_ack_ok: bool = False,
        baseline_fresh: bool = False,
        db_quick_check: str = "not_applicable",
    ) -> None:
        observed_at = self.clock()
        if (
            status_code not in _STATUS_CODES
            or type(stream_ack_ok) is not bool
            or type(baseline_fresh) is not bool
            or db_quick_check not in _DB_CHECK_VALUES
            or observed_at.tzinfo is None
            or observed_at.utcoffset() is None
        ):
            raise HeartbeatError("heartbeat_value_invalid")
        payload = {
            "schema_version": 1,
            "release_sha": self.release_sha,
            "boot_id_hash": self.boot_id_hash,
            "component": self.component,
            "launchd_label": self.launchd_label,
            "mode": "shadow",
            "live_order_submission": False,
            "updated_at": observed_at.astimezone(timezone.utc).isoformat(),
            "status_code": status_code,
            "stream_ack_ok": stream_ack_ok,
            "baseline_fresh": baseline_fresh,
            "db_quick_check": db_quick_check,
        }
        self._replace(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("ascii")
        )

    def _replace(self, payload: bytes) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            parent_stat = self.path.parent.lstat()
            if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
                raise HeartbeatError("heartbeat_path_invalid")
            try:
                current = self.path.lstat()
            except FileNotFoundError:
                current = None
            if current is not None and (
                stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode)
            ):
                raise HeartbeatError("heartbeat_path_invalid")
            temporary = self.path.with_name(
                f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            )
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(temporary, flags, 0o600)
            try:
                remaining = memoryview(payload)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written <= 0:
                        raise OSError("short heartbeat write")
                    remaining = remaining[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            try:
                os.replace(temporary, self.path)
            except BaseException:
                try:
                    temporary.unlink()
                except OSError:
                    pass
                raise
            if os.name != "nt" and stat.S_IMODE(self.path.stat().st_mode) != 0o600:
                raise HeartbeatError("heartbeat_mode_invalid")
        except HeartbeatError:
            raise
        except OSError as exc:
            raise HeartbeatError("heartbeat_write_failed") from exc
