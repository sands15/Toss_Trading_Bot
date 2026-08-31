from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import importlib.util
import json
import os
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
    symbols: tuple[str, ...] | None = None,
) -> None:
    identity = (
        {"schema_version": 1, "symbol": "AAPL"}
        if symbols is None
        else {"schema_version": 2, "symbols": list(symbols)}
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                **identity,
                "generated_at": generated_at.isoformat(),
                "market": "US",
                "session_date": STREAM_NOW.astimezone(
                    watchdog.ZoneInfo("America/New_York")
                ).date().isoformat(),
                "active_until": active_until.isoformat(),
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


def test_module_is_standalone_stdlib_and_has_no_trading_imports() -> None:
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


class _DiscordResponse:
    def __init__(self, status: int, payload: object = None) -> None:
        self.status = status
        self.payload = (
            payload
            if type(payload) is bytes
            else json.dumps(payload if payload is not None else {}).encode("utf-8")
        )

    def read(self, _limit: int) -> bytes:
        return self.payload


class _DiscordConnection:
    def __init__(self, responses: list[_DiscordResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[str, str, bytes | None, dict[str, str]]] = []
        self.closed = False

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.requests.append((method, path, body, dict(headers or {})))

    def getresponse(self) -> _DiscordResponse:
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


def _discord_factory(connection: _DiscordConnection, calls: list[tuple]) -> object:
    def factory(host: str, **kwargs: object) -> _DiscordConnection:
        calls.append((host, kwargs))
        return connection

    return factory


def test_discord_sender_verifies_exact_channel_then_posts_redacted_alert(
    capsys,
) -> None:
    channel_id = "123456789012345678"
    token = "A" * 40
    connection = _DiscordConnection(
        [
            _DiscordResponse(200, {"id": channel_id, "name": "private"}),
            _DiscordResponse(200, {"id": "message-id"}),
        ]
    )
    factory_calls: list[tuple] = []
    alert = {
        "schema_version": 1,
        "event": "WATCHDOG_RECOVERED",
        "component": "stream",
        "from": "HEARTBEAT_STALE",
        "to": "OK",
    }

    watchdog.send_discord_alert(
        alert,
        bot_token=token,
        allowed_channel_id=channel_id,
        connection_factory=_discord_factory(connection, factory_calls),
    )

    assert factory_calls[0][0] == "discord.com"
    assert factory_calls[0][1]["timeout"] == 5.0
    assert len(connection.requests) == 2
    get_request, post_request = connection.requests
    assert get_request[:3] == (
        "GET",
        f"/api/v10/channels/{channel_id}",
        None,
    )
    assert post_request[0:2] == (
        "POST",
        f"/api/v10/channels/{channel_id}/messages",
    )
    assert get_request[3]["Authorization"] == f"Bot {token}"
    assert post_request[3]["Authorization"] == f"Bot {token}"
    body = json.loads(post_request[2].decode("ascii"))
    assert body == {
        "allowed_mentions": {"parse": []},
        "content": (
            "[Toss bot watchdog] WATCHDOG_RECOVERED | "
            "stream: HEARTBEAT_STALE -> OK"
        ),
    }
    assert token not in post_request[2].decode("ascii")
    assert channel_id not in post_request[2].decode("ascii")
    assert connection.closed is True
    assert capsys.readouterr() == ("", "")


def test_discord_sender_refuses_remote_channel_mismatch_without_post() -> None:
    allowed = "123456789012345678"
    other = "223456789012345678"
    connection = _DiscordConnection([_DiscordResponse(200, {"id": other})])
    calls: list[tuple] = []

    code = _error_code(
        lambda: watchdog.send_discord_alert(
            {
                "schema_version": 1,
                "event": "WATCHDOG_STATE_CHANGED",
                "component": "planner",
                "from": "OK",
                "to": "HEARTBEAT_STALE",
            },
            bot_token="A" * 40,
            allowed_channel_id=allowed,
            connection_factory=_discord_factory(connection, calls),
        )
    )

    assert code == "DISCORD_CHANNEL_MISMATCH"
    assert [request[0] for request in connection.requests] == ["GET"]
    assert other not in str(code)


@pytest.mark.parametrize(
    ("responses", "code"),
    [
        ([_DiscordResponse(429)], "DISCORD_RATE_LIMITED"),
        ([_DiscordResponse(503)], "DISCORD_UNAVAILABLE"),
        (
            [_DiscordResponse(200, {"id": "123456789012345678"}), _DiscordResponse(429)],
            "DISCORD_RATE_LIMITED",
        ),
        (
            [_DiscordResponse(200, {"id": "123456789012345678"}), _DiscordResponse(500)],
            "DISCORD_UNAVAILABLE",
        ),
    ],
)
def test_discord_sender_fails_closed_on_rate_limit_or_server_error(
    responses: list[_DiscordResponse],
    code: str,
) -> None:
    connection = _DiscordConnection(responses)

    assert _error_code(
        lambda: watchdog.send_discord_alert(
            {
                "schema_version": 1,
                "event": "WATCHDOG_STARTED",
                "component": "watchdog",
                "from": None,
                "to": "OK",
            },
            bot_token="A" * 40,
            allowed_channel_id="123456789012345678",
            connection_factory=_discord_factory(connection, []),
        )
    ) == code
    assert connection.closed is True


def test_main_without_discord_configuration_keeps_stdout_only_mode(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    required = {
        "TOSS_WATCHDOG_HEARTBEAT_ROOT": str(tmp_path / "heartbeats"),
        "TOSS_WATCHDOG_CONTEXT_PATH": str(tmp_path / "news-context.json"),
        "TOSS_WATCHDOG_EXPECTATION_PATH": str(tmp_path / "stream-expectation.json"),
        "TOSS_WATCHDOG_STATE_PATH": str(tmp_path / "state.json"),
        "TOSS_WATCHDOG_RELEASE_SHA": RELEASE_SHA,
        "TOSS_WATCHDOG_LAUNCHD_DOMAIN": "gui/501",
    }
    for name, value in required.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(watchdog.DISCORD_TOKEN_ENV, raising=False)
    monkeypatch.delenv(watchdog.DISCORD_CHANNEL_ENV, raising=False)
    monkeypatch.setattr(watchdog, "macos_boot_id_hash", lambda: BOOT_ID_HASH)

    def fake_run_once(**kwargs):
        assert watchdog.DISCORD_TOKEN_ENV not in os.environ
        assert watchdog.DISCORD_CHANNEL_ENV not in os.environ
        kwargs["sender"](
            {
                "schema_version": 1,
                "event": "WATCHDOG_STARTED",
                "component": "watchdog",
                "from": None,
                "to": "OK",
            }
        )
        return {"planner": "OK"}

    monkeypatch.setattr(watchdog, "run_once", fake_run_once)
    monkeypatch.setattr(
        watchdog,
        "send_discord_alert",
        lambda *_args, **_kwargs: pytest.fail("network sender must stay disabled"),
    )

    assert watchdog.main() == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {
        "schema_version": 1,
        "event": "WATCHDOG_STARTED",
        "component": "watchdog",
        "from": None,
        "to": "OK",
    }
    assert output.err == ""


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


def test_two_lane_stream_context_is_active_and_rejects_duplicate_symbols(
    tmp_path: Path,
) -> None:
    context_path = (tmp_path / "runtime" / "news-context.json").resolve()
    _write_stream_context(context_path, symbols=("AAPL", "MSFT"))

    assert watchdog.stream_context_state(context_path, now=STREAM_NOW) == "active"

    _write_stream_context(context_path, symbols=("AAPL", "AAPL"))
    assert _error_code(
        lambda: watchdog.stream_context_state(context_path, now=STREAM_NOW)
    ) == "STREAM_CONTEXT_INVALID"


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


def test_partial_sender_failure_retries_only_unacknowledged_alerts(
    tmp_path: Path,
) -> None:
    heartbeat_root = tmp_path / "heartbeats"
    state_path = tmp_path / "state" / "state.json"
    _write_all_heartbeats(heartbeat_root)
    _write_heartbeat(heartbeat_root, _spec("planner"), status_code="ERROR")
    _write_heartbeat(heartbeat_root, _spec("stream"), baseline_fresh=False)
    first_attempts: list[dict[str, object]] = []

    def fail_second(alert):
        first_attempts.append(dict(alert))
        if len(first_attempts) == 2:
            raise RuntimeError("synthetic sender failure")

    with pytest.raises(RuntimeError, match="synthetic sender failure"):
        watchdog.run_once(
            heartbeat_root=heartbeat_root,
            state_path=state_path,
            expected_release_sha=RELEASE_SHA,
            expected_boot_id_hash=BOOT_ID_HASH,
            launchd_domain="gui/501",
            sender=fail_second,
            now=NOW,
            launchctl_runner=_healthy_launchctl,
        )

    assert [alert["component"] for alert in first_attempts] == [
        "watchdog",
        "planner",
    ]
    persisted = json.loads(state_path.read_text(encoding="ascii"))
    assert persisted["schema_version"] == 2
    assert [alert["component"] for alert in persisted["pending_alerts"]] == [
        "planner",
        "stream",
    ]

    retry_alerts: list[dict[str, object]] = []
    kwargs = {
        "heartbeat_root": heartbeat_root,
        "state_path": state_path,
        "expected_release_sha": RELEASE_SHA,
        "expected_boot_id_hash": BOOT_ID_HASH,
        "launchd_domain": "gui/501",
        "sender": lambda alert: retry_alerts.append(dict(alert)),
        "now": NOW,
        "launchctl_runner": _healthy_launchctl,
    }
    watchdog.run_once(**kwargs)

    assert [alert["component"] for alert in retry_alerts] == ["planner", "stream"]
    assert "watchdog" not in {alert["component"] for alert in retry_alerts}
    assert json.loads(state_path.read_text(encoding="ascii"))["pending_alerts"] == []

    retry_alerts.clear()
    watchdog.run_once(**kwargs)
    assert retry_alerts == []


def test_legacy_watchdog_state_migrates_without_duplicate_startup(
    tmp_path: Path,
) -> None:
    heartbeat_root = tmp_path / "heartbeats"
    state_path = tmp_path / "state" / "state.json"
    _write_all_heartbeats(heartbeat_root)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "components": {
                    spec.component: "OK" for spec in watchdog.COMPONENTS
                },
            }
        ),
        encoding="ascii",
    )
    alerts: list[dict[str, object]] = []

    watchdog.run_once(
        heartbeat_root=heartbeat_root,
        state_path=state_path,
        expected_release_sha=RELEASE_SHA,
        expected_boot_id_hash=BOOT_ID_HASH,
        launchd_domain="gui/501",
        sender=lambda alert: alerts.append(dict(alert)),
        now=NOW,
        launchctl_runner=_healthy_launchctl,
    )

    assert alerts == []
    migrated = json.loads(state_path.read_text(encoding="ascii"))
    assert migrated["schema_version"] == 2
    assert migrated["pending_alerts"] == []


def test_state_directory_fsync_failure_stops_before_alert_delivery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    heartbeat_root = tmp_path / "heartbeats"
    state_path = tmp_path / "state" / "state.json"
    _write_all_heartbeats(heartbeat_root)
    fsync_calls: list[Path] = []
    alerts: list[dict[str, object]] = []

    def fail_directory_fsync(path: Path) -> None:
        fsync_calls.append(path)
        raise watchdog.WatchdogError("STATE_DURABILITY_FAILED")

    monkeypatch.setattr(watchdog, "_fsync_state_directory", fail_directory_fsync)

    with pytest.raises(watchdog.WatchdogError) as caught:
        watchdog.run_once(
            heartbeat_root=heartbeat_root,
            state_path=state_path,
            expected_release_sha=RELEASE_SHA,
            expected_boot_id_hash=BOOT_ID_HASH,
            launchd_domain="gui/501",
            sender=lambda alert: alerts.append(dict(alert)),
            now=NOW,
            launchctl_runner=_healthy_launchctl,
        )

    assert caught.value.code == "STATE_DURABILITY_FAILED"
    assert fsync_calls == [state_path.parent]
    assert state_path.exists()
    assert alerts == []


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
        "TOSS_WATCHDOG_ALLOWED_CHANNEL_ID",
    }
    assert "security find-generic-password" in wrapper
    assert "TossTradingBot.DiscordApprovalToken" in wrapper
    assert "discord-approval-bot" in wrapper
    assert "TossTradingBot.DiscordWatchdogToken" not in wrapper
    assert "TOSS_WATCHDOG_DISCORD_BOT_TOKEN" in wrapper
    assert wrapper.index("actual_file=") < wrapper.index("security find-generic-password")
    assert "TOSS_INTERNAL_WATCHDOG_TOKEN" not in wrapper
    assert "discord_token" not in wrapper.split("' shadow-watchdog-clean", 1)[1]
