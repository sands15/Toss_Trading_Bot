from __future__ import annotations

import json
import importlib.util
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from turtle_runtime.heartbeat import HeartbeatError, RedactedHeartbeatWriter


NOW = datetime(2026, 8, 30, 9, 1, 2, 345678, tzinfo=timezone.utc)
RELEASE_SHA = "a" * 40
BOOT_HASH = "b" * 64
WATCHDOG_PATH = Path(__file__).resolve().parents[1] / "ops" / "shadow_watchdog.py"


def _writer(tmp_path: Path, component: str = "planner") -> RedactedHeartbeatWriter:
    return RedactedHeartbeatWriter(
        (tmp_path / component / "heartbeat.json").resolve(),
        release_sha=RELEASE_SHA,
        component=component,
        boot_id_hash=BOOT_HASH,
        clock=lambda: NOW,
    )


def test_writer_publishes_exact_schema_atomically_with_mode_0600(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    writer.write("OK", baseline_fresh=True, db_quick_check="ok")

    payload = json.loads(writer.path.read_text(encoding="ascii"))
    assert set(payload) == {
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
    assert payload == {
        "schema_version": 1,
        "release_sha": RELEASE_SHA,
        "boot_id_hash": BOOT_HASH,
        "component": "planner",
        "launchd_label": "com.sands15.toss-intraday-shadow",
        "mode": "shadow",
        "live_order_submission": False,
        "updated_at": NOW.isoformat(),
        "status_code": "OK",
        "stream_ack_ok": False,
        "baseline_fresh": True,
        "db_quick_check": "ok",
    }
    if os.name != "nt":
        assert stat.S_IMODE(writer.path.stat().st_mode) == 0o600
    assert list(writer.path.parent.glob(".*.tmp")) == []


def test_writer_output_is_accepted_by_the_standalone_watchdog(tmp_path: Path) -> None:
    writer = _writer(tmp_path, "stream")
    writer.write("OK", stream_ack_ok=True, baseline_fresh=True)
    spec = importlib.util.spec_from_file_location("heartbeat_contract_watchdog", WATCHDOG_PATH)
    assert spec is not None and spec.loader is not None
    watchdog = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = watchdog
    spec.loader.exec_module(watchdog)

    heartbeat = watchdog.read_heartbeat(
        writer.path,
        next(item for item in watchdog.COMPONENTS if item.component == "stream"),
        expected_release_sha=RELEASE_SHA,
        expected_boot_id_hash=BOOT_HASH,
        now=NOW,
    )

    assert heartbeat.status_code == "OK"
    assert heartbeat.stream_ack_ok is True
    assert heartbeat.baseline_fresh is True


def test_writer_replaces_existing_file_and_rejects_symlink(tmp_path: Path) -> None:
    writer = _writer(tmp_path, "stream")
    writer.write("STARTING")
    writer.write("OK", stream_ack_ok=True, baseline_fresh=True)
    assert json.loads(writer.path.read_text(encoding="ascii"))["status_code"] == "OK"

    writer.path.unlink()
    target = tmp_path / "elsewhere.json"
    target.write_text("untouched", encoding="utf-8")
    try:
        writer.path.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(HeartbeatError, match="heartbeat_path_invalid"):
        writer.write("OK", stream_ack_ok=True, baseline_fresh=True)
    assert target.read_text(encoding="utf-8") == "untouched"


@pytest.mark.parametrize(
    "path,component,release,boot",
    [
        (Path("relative/planner/heartbeat.json"), "planner", RELEASE_SHA, BOOT_HASH),
        (Path("/tmp/wrong/heartbeat.json"), "planner", RELEASE_SHA, BOOT_HASH),
        (Path("/tmp/planner/status.json"), "planner", RELEASE_SHA, BOOT_HASH),
        (Path("/tmp/planner/heartbeat.json"), "unknown", RELEASE_SHA, BOOT_HASH),
        (Path("/tmp/planner/heartbeat.json"), "planner", "not-a-sha", BOOT_HASH),
        (Path("/tmp/planner/heartbeat.json"), "planner", RELEASE_SHA, "bad"),
    ],
)
def test_writer_rejects_unbound_identity_or_path(
    path: Path, component: str, release: str, boot: str
) -> None:
    with pytest.raises(HeartbeatError, match="heartbeat_configuration_invalid"):
        RedactedHeartbeatWriter(
            path,
            release_sha=release,
            component=component,
            boot_id_hash=boot,
        )


def test_writer_rejects_non_boolean_and_naive_clock(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    with pytest.raises(HeartbeatError, match="heartbeat_value_invalid"):
        writer.write("OK", stream_ack_ok=1)  # type: ignore[arg-type]

    naive = RedactedHeartbeatWriter(
        (tmp_path / "news" / "heartbeat.json").resolve(),
        release_sha=RELEASE_SHA,
        component="news",
        boot_id_hash=BOOT_HASH,
        clock=lambda: datetime(2026, 8, 30),
    )
    with pytest.raises(HeartbeatError, match="heartbeat_value_invalid"):
        naive.write("OK")
