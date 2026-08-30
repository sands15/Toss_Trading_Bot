from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import importlib.util
import json
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "ops" / "shadow_watchdog.py"
RELEASE_SHA = "a" * 40
BOOT_ID_HASH = "b" * 64
NOW = dt.datetime(2026, 8, 30, 8, 0, tzinfo=dt.timezone.utc)
STREAM_NOW = dt.datetime(2026, 8, 31, 14, 0, tzinfo=dt.timezone.utc)


def _load_module():
    spec = importlib.util.spec_from_file_location("shadow_watchdog", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


watchdog = _load_module()


def _spec(component: str):
    return next(item for item in watchdog.COMPONENTS if item.component == component)


def _heartbeat(spec, **changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "release_sha": RELEASE_SHA,
        "boot_id_hash": BOOT_ID_HASH,
        "component": spec.component,
        "launchd_label": spec.launchd_label,
        "mode": "shadow",
        "live_order_submission": False,
        "updated_at": (NOW - dt.timedelta(seconds=2)).isoformat(),
        "status_code": "OK",
        "stream_ack_ok": spec.require_stream_ack,
        "baseline_fresh": spec.require_baseline_fresh,
        "db_quick_check": spec.expected_db_quick_check,
    }
    value.update(changes)
    return value


def _write_heartbeat(root: Path, spec, **changes: object) -> Path:
    path = root / spec.component / "heartbeat.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_heartbeat(spec, **changes), separators=(",", ":")),
        encoding="utf-8",
    )
    return path


def _healthy_launchctl(command, **kwargs):
    label = command[-1].rsplit("/", 1)[-1]
    news = label == _spec("news").launchd_label
    output = "state = not running\nlast exit code = 0\n" if news else "state = running\n"
    return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")


def _write_all_heartbeats(root: Path, *, at: dt.datetime = NOW) -> None:
    for spec in watchdog.COMPONENTS:
        _write_heartbeat(
            root,
            spec,
            updated_at=(at - dt.timedelta(seconds=2)).isoformat(),
        )


def _write_stream_context(
    path: Path,
    *,
    generated_at: dt.datetime = STREAM_NOW - dt.timedelta(seconds=2),
    active_until: dt.datetime = STREAM_NOW + dt.timedelta(hours=1),
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": generated_at.isoformat(),
                "market": "US",
                "session_date": STREAM_NOW.astimezone(
                    watchdog.ZoneInfo("America/New_York")
                ).date().isoformat(),
                "active_until": active_until.isoformat(),
                "symbol": "AAPL",
                "reason": "intraday_plan",
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _write_stream_expectation(
    path: Path,
    *,
    expected_from: dt.datetime = STREAM_NOW - dt.timedelta(minutes=30),
    expected_until: dt.datetime = STREAM_NOW + dt.timedelta(hours=1),
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_date": STREAM_NOW.astimezone(
                    watchdog.ZoneInfo("America/New_York")
                ).date().isoformat(),
                "expected_from": expected_from.isoformat(),
                "expected_until": expected_until.isoformat(),
                "reason": "intraday_paper_stream",
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _error_code(call) -> str:
    with pytest.raises(watchdog.WatchdogError) as caught:
        call()
    return caught.value.code


def test_module_is_standalone_stdlib_and_has_no_trading_or_network_imports() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )

    assert "turtle_bot" not in imported
    assert "sqlite3" not in imported
    assert "socket" not in imported
    assert "urllib" not in imported
    assert "requests" not in imported
    assert "discord" not in imported


def test_valid_exact_redacted_heartbeat_is_accepted(tmp_path: Path) -> None:
    spec = _spec("stream")
    path = _write_heartbeat(tmp_path, spec)

    value = watchdog.read_heartbeat(
        path,
        spec,
        expected_release_sha=RELEASE_SHA,
        expected_boot_id_hash=BOOT_ID_HASH,
        now=NOW,
    )

    assert value.status_code == "OK"
    assert value.stream_ack_ok is True
    assert value.baseline_fresh is True
    assert value.db_quick_check == "not_applicable"


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"mode": "live"}, "HARD_OFF_INVARIANT_FAILED"),
        ({"live_order_submission": True}, "HARD_OFF_INVARIANT_FAILED"),
        ({"release_sha": "c" * 40}, "RELEASE_MISMATCH"),
        ({"boot_id_hash": "d" * 64}, "BOOT_MISMATCH"),
        ({"component": "planner"}, "COMPONENT_MISMATCH"),
        ({"launchd_label": "com.sands15.other"}, "LAUNCHD_LABEL_MISMATCH"),
        ({"stream_ack_ok": 0}, "HEARTBEAT_SCHEMA_INVALID"),
        ({"baseline_fresh": 1}, "HEARTBEAT_SCHEMA_INVALID"),
        ({"status_code": "TOKEN=secret"}, "HEARTBEAT_STATUS_INVALID"),
        ({"db_quick_check": "unknown"}, "HEARTBEAT_DB_CHECK_INVALID"),
        (
            {"updated_at": (NOW - dt.timedelta(seconds=31)).isoformat()},
            "HEARTBEAT_STALE",
        ),
        (
            {"updated_at": (NOW + dt.timedelta(seconds=6)).isoformat()},
            "HEARTBEAT_CLOCK_SKEW",
        ),
    ],
)
def test_heartbeat_identity_hard_off_and_age_are_fail_closed(
    tmp_path: Path, changes: dict[str, object], code: str
) -> None:
    spec = _spec("stream")
    path = _write_heartbeat(tmp_path, spec, **changes)

    assert _error_code(
        lambda: watchdog.read_heartbeat(
            path,
            spec,
            expected_release_sha=RELEASE_SHA,
            expected_boot_id_hash=BOOT_ID_HASH,
            now=NOW,
        )
    ) == code


def test_heartbeat_rejects_missing_extra_and_duplicate_keys(tmp_path: Path) -> None:
    spec = _spec("planner")
    path = _write_heartbeat(tmp_path, spec)
    missing = _heartbeat(spec)
    missing.pop("status_code")
    path.write_text(json.dumps(missing), encoding="utf-8")
    assert _error_code(
        lambda: watchdog.read_heartbeat(
            path,
            spec,
            expected_release_sha=RELEASE_SHA,
            expected_boot_id_hash=BOOT_ID_HASH,
            now=NOW,
        )
    ) == "HEARTBEAT_SCHEMA_INVALID"

    extra = _heartbeat(spec) | {"account_seq": "private"}
    path.write_text(json.dumps(extra), encoding="utf-8")
    assert _error_code(
        lambda: watchdog.read_heartbeat(
            path,
            spec,
            expected_release_sha=RELEASE_SHA,
            expected_boot_id_hash=BOOT_ID_HASH,
            now=NOW,
        )
    ) == "HEARTBEAT_SCHEMA_INVALID"

    encoded = json.dumps(_heartbeat(spec), separators=(",", ":"))
    path.write_text(encoded[:-1] + ',"status_code":"OK"}', encoding="utf-8")
    assert _error_code(
        lambda: watchdog.read_heartbeat(
            path,
            spec,
            expected_release_sha=RELEASE_SHA,
            expected_boot_id_hash=BOOT_ID_HASH,
            now=NOW,
        )
    ) == "HEARTBEAT_JSON_INVALID"


def test_heartbeat_rejects_symlink_and_large_file(tmp_path: Path) -> None:
    spec = _spec("approval")
    real = _write_heartbeat(tmp_path, spec)
    linked = tmp_path / "linked-heartbeat.json"
    try:
        linked.symlink_to(real)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert _error_code(
        lambda: watchdog.read_heartbeat(
            linked,
            spec,
            expected_release_sha=RELEASE_SHA,
            expected_boot_id_hash=BOOT_ID_HASH,
            now=NOW,
        )
    ) == "HEARTBEAT_UNREADABLE"

    real.write_bytes(b"x" * 4097)
    assert _error_code(
        lambda: watchdog.read_heartbeat(
            real,
            spec,
            expected_release_sha=RELEASE_SHA,
            expected_boot_id_hash=BOOT_ID_HASH,
            now=NOW,
        )
    ) == "HEARTBEAT_UNREADABLE"


def test_launchctl_uses_exact_allowlisted_command_without_shell() -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="state = running\n", stderr="")

    status = watchdog.probe_launchd("gui/501", _spec("stream"), runner=runner)

    assert status.state == "running"
    assert calls[0][0] == [
        "/bin/launchctl",
        "print",
        "gui/501/com.sands15.toss-market-stream-shadow",
    ]
    assert calls[0][1]["stdin"] is subprocess.DEVNULL
    assert calls[0][1]["stderr"] is subprocess.DEVNULL
    assert "shell" not in calls[0][1]


def test_launchctl_rejects_domain_or_label_before_invoking_runner() -> None:
    calls = 0

    def runner(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("must not run")

    malicious = dataclasses.replace(_spec("stream"), launchd_label="com.sands15.x;rm")
    assert _error_code(
        lambda: watchdog.probe_launchd("gui/501", malicious, runner=runner)
    ) == "LAUNCHD_LABEL_NOT_ALLOWED"
    assert _error_code(
        lambda: watchdog.probe_launchd("gui/501/../../system", _spec("stream"), runner=runner)
    ) == "LAUNCHD_DOMAIN_INVALID"
    assert calls == 0


def test_health_maps_db_baseline_stream_and_process_failures(tmp_path: Path) -> None:
    planner = _spec("planner")
    _write_heartbeat(tmp_path, planner, db_quick_check="fail")
    assert watchdog.component_health(
        tmp_path,
        planner,
        expected_release_sha=RELEASE_SHA,
        expected_boot_id_hash=BOOT_ID_HASH,
        launchd_domain="gui/501",
        now=NOW,
        launchctl_runner=_healthy_launchctl,
    ) == "DB_QUICK_CHECK_FAILED"

    stream = _spec("stream")
    _write_heartbeat(tmp_path, stream, stream_ack_ok=False)
    assert watchdog.component_health(
        tmp_path,
        stream,
        expected_release_sha=RELEASE_SHA,
        expected_boot_id_hash=BOOT_ID_HASH,
        launchd_domain="gui/501",
        now=NOW,
        launchctl_runner=_healthy_launchctl,
    ) == "STREAM_ACK_MISSING"

    _write_heartbeat(tmp_path, stream)

    def down(command, **kwargs):
        return subprocess.CompletedProcess(command, 113, stdout="", stderr="")

    assert watchdog.component_health(
        tmp_path,
        stream,
        expected_release_sha=RELEASE_SHA,
        expected_boot_id_hash=BOOT_ID_HASH,
        launchd_domain="gui/501",
        now=NOW,
        launchctl_runner=down,
    ) == "PROCESS_NOT_LOADED"


def test_stream_is_required_only_while_selected_symbol_context_is_active(
    tmp_path: Path,
) -> None:
    heartbeat_root = tmp_path / "heartbeats"
    state_path = tmp_path / "watchdog" / "state.json"
    context_path = (tmp_path / "runtime" / "news-context.json").resolve()
    expectation_path = (
        tmp_path / "runtime" / "stream-expectation.json"
    ).resolve()
    _write_all_heartbeats(heartbeat_root, at=STREAM_NOW)

    def stream_stopped(command, **kwargs):
        label = command[-1].rsplit("/", 1)[-1]
        if label == _spec("stream").launchd_label:
            output = "state = not running\nlast exit code = 0\n"
        else:
            output = "state = running\n"
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    _write_stream_context(context_path)
    _write_stream_expectation(expectation_path)
    active = watchdog.run_once(
        heartbeat_root=heartbeat_root,
        state_path=state_path,
        stream_context_path=context_path,
        stream_expectation_path=expectation_path,
        expected_release_sha=RELEASE_SHA,
        expected_boot_id_hash=BOOT_ID_HASH,
        launchd_domain="gui/501",
        sender=lambda _alert: None,
        now=STREAM_NOW,
        launchctl_runner=stream_stopped,
    )
    assert active["stream"] == "PROCESS_NOT_RUNNING"

    _write_stream_context(
        context_path,
        generated_at=STREAM_NOW - dt.timedelta(hours=2),
        active_until=STREAM_NOW - dt.timedelta(hours=1),
    )
    _write_stream_expectation(
        expectation_path,
        expected_from=STREAM_NOW - dt.timedelta(hours=2),
        expected_until=STREAM_NOW - dt.timedelta(hours=1),
    )
    inactive = watchdog.run_once(
        heartbeat_root=heartbeat_root,
        state_path=state_path,
        stream_context_path=context_path,
        stream_expectation_path=expectation_path,
        expected_release_sha=RELEASE_SHA,
        expected_boot_id_hash=BOOT_ID_HASH,
        launchd_domain="gui/501",
        sender=lambda _alert: None,
        now=STREAM_NOW,
        launchctl_runner=stream_stopped,
    )
    assert inactive["stream"] == "OK"

    _write_stream_expectation(expectation_path)
    context_path.unlink()
    missing = watchdog.run_once(
        heartbeat_root=heartbeat_root,
        state_path=state_path,
        stream_context_path=context_path,
        stream_expectation_path=expectation_path,
        expected_release_sha=RELEASE_SHA,
        expected_boot_id_hash=BOOT_ID_HASH,
        launchd_domain="gui/501",
        sender=lambda _alert: None,
        now=STREAM_NOW,
        launchctl_runner=stream_stopped,
    )
    assert missing["stream"] == "STREAM_CONTEXT_INVALID"


def test_malformed_active_stream_context_fails_closed(tmp_path: Path) -> None:
    heartbeat_root = tmp_path / "heartbeats"
    _write_all_heartbeats(heartbeat_root, at=STREAM_NOW)
    context_path = (tmp_path / "runtime" / "news-context.json").resolve()
    expectation_path = (
        tmp_path / "runtime" / "stream-expectation.json"
    ).resolve()
    _write_stream_context(context_path)
    _write_stream_expectation(expectation_path)
    payload = json.loads(context_path.read_text(encoding="utf-8"))
    payload.pop("active_until")
    context_path.write_text(json.dumps(payload), encoding="utf-8")

    status = watchdog.run_once(
        heartbeat_root=heartbeat_root,
        state_path=tmp_path / "watchdog" / "state.json",
        stream_context_path=context_path,
        stream_expectation_path=expectation_path,
        expected_release_sha=RELEASE_SHA,
        expected_boot_id_hash=BOOT_ID_HASH,
        launchd_domain="gui/501",
        sender=lambda _alert: None,
        now=STREAM_NOW,
        launchctl_runner=_healthy_launchctl,
    )

    assert status["stream"] == "STREAM_CONTEXT_INVALID"


def test_run_once_sends_only_start_change_and_recovery(tmp_path: Path) -> None:
    heartbeat_root = tmp_path / "heartbeats"
    state_path = tmp_path / "watchdog" / "state.json"
    _write_all_heartbeats(heartbeat_root)
    alerts: list[dict[str, object]] = []

    kwargs = {
        "heartbeat_root": heartbeat_root,
        "state_path": state_path,
        "expected_release_sha": RELEASE_SHA,
        "expected_boot_id_hash": BOOT_ID_HASH,
        "launchd_domain": "gui/501",
        "sender": lambda alert: alerts.append(dict(alert)),
        "now": NOW,
        "launchctl_runner": _healthy_launchctl,
    }
    assert set(watchdog.run_once(**kwargs).values()) == {"OK"}
    assert alerts == [
        {
            "schema_version": 1,
            "event": "WATCHDOG_STARTED",
            "component": "watchdog",
            "from": None,
            "to": "OK",
        }
    ]

    alerts.clear()
    watchdog.run_once(**kwargs)
    assert alerts == []

    _write_heartbeat(heartbeat_root, _spec("stream"), baseline_fresh=False)
    watchdog.run_once(**kwargs)
    assert alerts == [
        {
            "schema_version": 1,
            "event": "WATCHDOG_STATE_CHANGED",
            "component": "stream",
            "from": "OK",
            "to": "BASELINE_STALE",
        }
    ]
    assert "heartbeats" not in json.dumps(alerts)

    alerts.clear()
    _write_heartbeat(heartbeat_root, _spec("stream"))
    watchdog.run_once(**kwargs)
    assert alerts == [
        {
            "schema_version": 1,
            "event": "WATCHDOG_RECOVERED",
            "component": "stream",
            "from": "BASELINE_STALE",
            "to": "OK",
        }
    ]


def test_sender_failure_does_not_consume_state_change(tmp_path: Path) -> None:
    heartbeat_root = tmp_path / "heartbeats"
    state_path = tmp_path / "state" / "state.json"
    _write_all_heartbeats(heartbeat_root)

    def fail(alert):
        raise RuntimeError("synthetic sender failure")

    with pytest.raises(RuntimeError, match="synthetic sender failure"):
        watchdog.run_once(
            heartbeat_root=heartbeat_root,
            state_path=state_path,
            expected_release_sha=RELEASE_SHA,
            expected_boot_id_hash=BOOT_ID_HASH,
            launchd_domain="gui/501",
            sender=fail,
            now=NOW,
            launchctl_runner=_healthy_launchctl,
        )

    assert not state_path.exists()


def test_wrapper_and_plist_keep_the_watchdog_argument_free_and_non_trading() -> None:
    wrapper = (ROOT / "ops" / "run-shadow-watchdog.command").read_text(encoding="utf-8")
    plist_path = (
        ROOT
        / "ops"
        / "launchd"
        / "com.sands15.toss-shadow-watchdog.plist.example"
    )
    with plist_path.open("rb") as handle:
        plist = plistlib.load(handle)

    assert wrapper.startswith("#!/bin/zsh -f")
    for required in (
        "$# != 0",
        "/usr/bin/env -i",
        "ulimit -c 0",
        '${repo_root:t}" != "$release_sha',
        ' -I -u "$watchdog_file"',
    ):
        assert required in wrapper
    for forbidden in (
        "TOSS_CLIENT",
        "ACCOUNT_SEQ",
        "DISCORD_APPROVAL",
        "security find-generic-password",
        "turtle_bot",
        "sqlite",
    ):
        assert forbidden not in wrapper
    assert plist["Label"] == "com.sands15.toss-shadow-watchdog"
    assert plist["ProgramArguments"] == [
        "/ABSOLUTE/READ_ONLY/RELEASE/SHA/ops/run-shadow-watchdog.command"
    ]
    assert plist["StartInterval"] == 15
    assert "KeepAlive" not in plist
    assert plist["Umask"] == 63
    environment = plist["EnvironmentVariables"]
    assert set(environment) == {
        "TOSS_WATCHDOG_CONTEXT_PATH",
        "TOSS_WATCHDOG_EXPECTATION_PATH",
        "TOSS_WATCHDOG_HEARTBEAT_ROOT",
        "TOSS_WATCHDOG_LAUNCHD_DOMAIN",
        "TOSS_WATCHDOG_RELEASE_SHA",
        "TOSS_WATCHDOG_STATE_PATH",
    }
