from __future__ import annotations

import plistlib
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from turtle_bot.notifier import DiscordTradeNotifier
from turtle_bot.operations import _drain_intraday_notifications
from turtle_bot.state_store import SQLiteStateStore


ROOT = Path(__file__).resolve().parents[1]
LAUNCHD = ROOT / "ops" / "launchd"
JOBS = {
    "com.sands15.toss-intraday-shadow": "run-intraday-shadow.command",
    "com.sands15.toss-market-stream-shadow": "run-toss-stream.command",
    "com.sands15.toss-discord-approval": "run-discord-approval.command",
    "com.sands15.toss-news-shadow": "run-news-shadow.command",
    "com.sands15.toss-shadow-watchdog": "run-shadow-watchdog.command",
}
SECRET_PLIST_MARKERS = {
    "ACCOUNT_SEQ",
    "API_KEY",
    "CLIENT_ID",
    "CLIENT_SECRET",
    "TOKEN",
    "WEBHOOK",
}

_NESTED_SINGLE_QUOTE = "'\"'\"'"


def _templates() -> dict[str, tuple[Path, dict[str, object]]]:
    result: dict[str, tuple[Path, dict[str, object]]] = {}
    for path in LAUNCHD.glob("*.plist.example"):
        with path.open("rb") as handle:
            payload = plistlib.load(handle)
        result[str(payload["Label"])] = (path, payload)
    return result


def test_release_allowlist_is_exactly_five_shadow_jobs() -> None:
    templates = _templates()

    assert set(templates) == set(JOBS)
    assert len(list(LAUNCHD.glob("*.plist.example"))) == 5
    for label, wrapper_name in JOBS.items():
        path, payload = templates[label]
        assert payload["Label"] == label
        assert payload["LimitLoadToSessionType"] == "Aqua"
        arguments = payload["ProgramArguments"]
        assert len(arguments) == 1
        assert str(arguments[0]).endswith(f"/ops/{wrapper_name}")
        assert payload["Umask"] == 63
        assert payload["WorkingDirectory"].startswith("/ABSOLUTE/READ_ONLY/RELEASE/")
        raw = path.read_text(encoding="utf-8")
        assert not re.search(r"(?<![A-Za-z0-9])\d{17,20}(?![A-Za-z0-9])", raw)
        assert re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", raw) is None
        assert "discord.com/api/webhooks/" not in raw.lower()
        environment = payload.get("EnvironmentVariables", {})
        assert not any(
            marker in str(key).upper()
            for key in environment
            for marker in SECRET_PLIST_MARKERS
        )


def test_all_five_wrappers_fail_closed_at_the_same_process_boundary() -> None:
    for wrapper_name in JOBS.values():
        source = (ROOT / "ops" / wrapper_name).read_text(encoding="utf-8")
        assert source.startswith("#!/bin/zsh -f\nset -eu\numask 077\nulimit -c 0")
        assert "$# != 0" in source
        assert source.count("ulimit -c 0") >= 2
        assert source.count("umask 077") >= 2
        assert "/usr/bin/env -i" in source
        if wrapper_name == "run-shadow-watchdog.command":
            assert '"${repo_root:t}" != "$release_sha"' in source
        else:
            assert 'release_sha="${repo_root:t}"' in source
        assert '"$python_bin" -I' in source
        nested_python = re.findall(
            re.escape(f"-c {_NESTED_SINGLE_QUOTE}\n")
            + r"(.*?)\n"
            + re.escape(_NESTED_SINGLE_QUOTE),
            source,
            re.DOTALL,
        )
        direct_python = re.findall(
            r'"\$python_bin" -I(?: -u)? -c \'\n(.*?)\n\'', source, re.DOTALL
        )
        for block in nested_python + direct_python:
            compile(block, wrapper_name, "exec")
        for forbidden in (
            "--live",
            "dashboard-server",
            "multi_user_gateway",
            "personal:order",
            "receipt_consumer",
            "run-multi-user-gateway",
        ):
            assert forbidden not in source

    for wrapper_name in set(JOBS.values()) - {"run-shadow-watchdog.command"}:
        source = (ROOT / "ops" / wrapper_name).read_text(encoding="utf-8")
        assert "resource.setrlimit(resource.RLIMIT_CORE, (0, 0))" in source
        assert "resource.getrlimit(resource.RLIMIT_CORE) == (0, 0)" in source
        assert "SSL_CERT_FILE" in source

    watchdog = (ROOT / "ops" / "shadow_watchdog.py").read_text(encoding="utf-8")
    assert "resource.setrlimit(resource.RLIMIT_CORE, (0, 0))" in watchdog
    assert "resource.getrlimit(resource.RLIMIT_CORE) != (0, 0)" in watchdog


def test_each_job_has_only_its_required_authority() -> None:
    templates = _templates()
    environments = {
        label: set(payload.get("EnvironmentVariables", {}))
        for label, (_, payload) in templates.items()
    }

    assert environments["com.sands15.toss-intraday-shadow"] == {
        "TOSS_SHADOW_ACCOUNT_FINGERPRINT",
        "TOSS_SHADOW_ALLOWED_CHANNEL_ID",
        "TOSS_SHADOW_CONFIG_PATH",
        "TOSS_SHADOW_EXPERIMENT_HASH",
        "TOSS_SHADOW_HEARTBEAT_PATH",
        "TOSS_SHADOW_KEYCHAIN_SLUG",
        "TOSS_SHADOW_LOG_DIR",
        "TOSS_SHADOW_SIMULATION_DB",
        "TOSS_SHADOW_SIMULATION_END_DATE",
        "TOSS_SHADOW_SIMULATION_ID",
        "TOSS_SHADOW_SIMULATION_START_DATE",
        "TOSS_SHADOW_STATE_DB",
    }
    assert environments["com.sands15.toss-market-stream-shadow"] == {
        "TOSS_STREAM_CONTEXT_PATH",
        "TOSS_STREAM_EXPERIMENT_HASH",
        "TOSS_STREAM_HEARTBEAT_PATH",
        "TOSS_STREAM_KEYCHAIN_SLUG",
        "TOSS_STREAM_PLAN_DB",
        "TOSS_STREAM_SIMULATION_DB",
        "TOSS_STREAM_SIMULATION_END_DATE",
        "TOSS_STREAM_SIMULATION_ID",
        "TOSS_STREAM_SIMULATION_START_DATE",
        "TOSS_STREAM_SIMULATION_CONFIG_PATH",
        "TOSS_STREAM_SNAPSHOT_PATH",
    }
    assert environments["com.sands15.toss-discord-approval"] == {
        "DISCORD_ALLOWED_CHANNEL_ID",
        "DISCORD_ALLOWED_GUILD_ID",
        "DISCORD_ALLOWED_USER_ID",
        "DISCORD_APPROVAL_ENVELOPE_PATH",
        "DISCORD_APPROVAL_HEARTBEAT_PATH",
        "DISCORD_APPROVAL_INBOX_DIR",
    }
    assert environments["com.sands15.toss-news-shadow"] == {
        "TOSS_NEWS_ALLOWED_CHANNEL_ID",
        "TOSS_NEWS_CONFIG_PATH",
        "TOSS_NEWS_HEARTBEAT_PATH",
    }
    assert environments["com.sands15.toss-shadow-watchdog"] == {
        "TOSS_WATCHDOG_CONTEXT_PATH",
        "TOSS_WATCHDOG_EXPECTATION_PATH",
        "TOSS_WATCHDOG_HEARTBEAT_ROOT",
        "TOSS_WATCHDOG_LAUNCHD_DOMAIN",
        "TOSS_WATCHDOG_RELEASE_SHA",
        "TOSS_WATCHDOG_STATE_PATH",
    }

    assert templates["com.sands15.toss-news-shadow"][1]["StartInterval"] == 900
    assert "KeepAlive" not in templates["com.sands15.toss-news-shadow"][1]
    stream_job = templates["com.sands15.toss-market-stream-shadow"][1]
    assert stream_job["WatchPaths"] == [
        stream_job["EnvironmentVariables"]["TOSS_STREAM_CONTEXT_PATH"]
    ]
    assert "KeepAlive" not in stream_job
    assert "RunAtLoad" not in stream_job
    assert "StartInterval" not in stream_job
    assert templates["com.sands15.toss-shadow-watchdog"][1]["StartInterval"] == 15


def test_secret_handoff_is_keychain_only_and_python_pops_environment() -> None:
    planner = (ROOT / "ops" / "run-intraday-shadow.command").read_text(
        encoding="utf-8"
    )
    assert planner.index("_require_shadow_service_config") < planner.index(
        "security find-generic-password"
    )
    assert 'os.environ.pop("TOSS_CLIENT_ID", "")' in planner
    assert 'os.environ.pop("TOSS_CLIENT_SECRET", "")' in planner
    assert "TossTradingBot.DiscordTradeWebhook" in planner
    assert '2>/dev/null)" || trade_webhook=' in planner
    assert (
        "trade_webhook_pattern='^https://discord[.]com/api(/v[0-9]+)?/webhooks/"
        "[0-9]+/[^/?#[:space:]]+/?$'"
    ) in planner
    assert 'if [[ -n "$trade_webhook" && ! "$trade_webhook" =~' in planner
    assert 'os.environ.pop("DISCORD_TRADE_ALERT_WEBHOOK_URL", "")' in planner
    assert 'os.environ.pop("DISCORD_ALLOWED_CHANNEL_ID", "")' in planner
    assert 'expected_mode="shadow"' in planner
    assert "_require_locked_simulation_config" in planner
    assert "expected_account_fingerprint=" in planner
    assert "config_path.lstat()" in planner
    assert "expected_simulation=" in planner
    assert "/bin/zsh -f -c" not in planner
    assert "/bin/zsh -f -s <<'TOSS_SHADOW_RUNTIME'" in planner
    assert "unset TOSS_INTERNAL_REPO_ROOT" in planner

    stream = (ROOT / "ops" / "run-toss-stream.command").read_text(
        encoding="utf-8"
    )
    for source in (planner, stream):
        assert source.rindex("exec /usr/bin/env -i") < source.index(
            "security find-generic-password"
        )
        assert re.search(
            r'^\s*TOSS_CLIENT_(?:ID|SECRET)="\$client_', source, re.MULTILINE
        ) is None
        assert 'export TOSS_CLIENT_ID="$client_id"' in source
        assert 'export TOSS_CLIENT_SECRET="$client_secret"' in source
    assert "/bin/zsh -f -c" not in stream
    assert "/bin/zsh -f -s <<'TOSS_STREAM_RUNTIME'" in stream
    assert "unset TOSS_INTERNAL_REPO_ROOT" in stream

    news = (ROOT / "ops" / "run-news-shadow.command").read_text(encoding="utf-8")
    assert news.index("news worker import does not match this release") < news.index(
        "security find-generic-password"
    )
    assert "TossTradingBot.FinnhubApiKey" in news
    assert "TossTradingBot.DiscordNewsWebhook" in news
    assert "os.environ.pop" in news
    assert "run_once(config, env=env)" in news
    assert "TOSS_CLIENT_ID" not in news
    assert "TOSS_CLIENT_SECRET" not in news


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS zsh and Keychain")
@pytest.mark.parametrize(
    ("wrapper_name", "release_character"),
    [
        ("run-intraday-shadow.command", "a"),
        ("run-toss-stream.command", "b"),
    ],
)
def test_nested_clean_shell_receives_all_nonsecret_arguments_on_macos(
    tmp_path: Path,
    wrapper_name: str,
    release_character: str,
) -> None:
    release = (tmp_path / (release_character * 40)).resolve()
    ops_dir = release / "ops"
    python_bin = release / ".venv" / "bin" / "python"
    ops_dir.mkdir(parents=True)
    python_bin.parent.mkdir(parents=True)
    wrapper = ops_dir / wrapper_name
    wrapper.write_bytes((ROOT / "ops" / wrapper_name).read_bytes())
    wrapper.chmod(0o700)
    capture = release / "preflight-arguments.bin"
    python_bin.write_text(
        "#!/bin/zsh -f\n"
        "set -eu\n"
        'capture_path="${0:A:h:h:h}/preflight-arguments.bin"\n'
        "/usr/bin/printf '%s\\000' \"$@\" > \"$capture_path\"\n",
        encoding="utf-8",
    )
    python_bin.chmod(0o700)

    runtime = tmp_path / f"runtime-{release_character}"
    home = runtime / "home"
    home.mkdir(parents=True)
    simulation_id = "handoff-test"
    start_date = "2026-08-01"
    end_date = "2026-08-31"
    simulation_db = runtime / "intraday-paper.sqlite3"
    experiment_hash = "c" * 64
    keychain_slug = f"handoff-{uuid.uuid4().hex}"
    environment = {"HOME": str(home), "LANG": "en_US.UTF-8"}

    if wrapper_name == "run-intraday-shadow.command":
        config_path = runtime / "intraday-simulation.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("mode: shadow\n", encoding="utf-8")
        config_path.chmod(0o600)
        state_db = runtime / "intraday.sqlite3"
        log_dir = runtime / "logs"
        heartbeat_path = runtime / "heartbeats" / "planner" / "heartbeat.json"
        account_fingerprint = "d" * 64
        environment.update(
            {
                "TOSS_SHADOW_CONFIG_PATH": str(config_path),
                "TOSS_SHADOW_STATE_DB": str(state_db),
                "TOSS_SHADOW_LOG_DIR": str(log_dir),
                "TOSS_SHADOW_ALLOWED_CHANNEL_ID": "123456789012345678",
                "TOSS_SHADOW_HEARTBEAT_PATH": str(heartbeat_path),
                "TOSS_SHADOW_SIMULATION_ID": simulation_id,
                "TOSS_SHADOW_SIMULATION_START_DATE": start_date,
                "TOSS_SHADOW_SIMULATION_END_DATE": end_date,
                "TOSS_SHADOW_SIMULATION_DB": str(simulation_db),
                "TOSS_SHADOW_EXPERIMENT_HASH": experiment_hash,
                "TOSS_SHADOW_ACCOUNT_FINGERPRINT": account_fingerprint,
                "TOSS_SHADOW_KEYCHAIN_SLUG": keychain_slug,
            }
        )
        expected_tail = [
            str(release),
            str(config_path),
            str(state_db),
            simulation_id,
            start_date,
            end_date,
            str(simulation_db),
            experiment_hash,
            account_fingerprint,
        ]
    else:
        context_path = runtime / "news-context.json"
        snapshot_path = runtime / "market-stream.json"
        config_path = runtime / "intraday-simulation.yaml"
        plan_db = runtime / "intraday.sqlite3"
        heartbeat_path = runtime / "heartbeats" / "stream" / "heartbeat.json"
        environment.update(
            {
                "TOSS_STREAM_CONTEXT_PATH": str(context_path),
                "TOSS_STREAM_SNAPSHOT_PATH": str(snapshot_path),
                "TOSS_STREAM_SIMULATION_CONFIG_PATH": str(config_path),
                "TOSS_STREAM_PLAN_DB": str(plan_db),
                "TOSS_STREAM_HEARTBEAT_PATH": str(heartbeat_path),
                "TOSS_STREAM_SIMULATION_ID": simulation_id,
                "TOSS_STREAM_SIMULATION_START_DATE": start_date,
                "TOSS_STREAM_SIMULATION_END_DATE": end_date,
                "TOSS_STREAM_SIMULATION_DB": str(simulation_db),
                "TOSS_STREAM_EXPERIMENT_HASH": experiment_hash,
                "TOSS_STREAM_KEYCHAIN_SLUG": keychain_slug,
            }
        )
        expected_tail = [
            str(release),
            str(config_path),
            str(context_path),
            str(plan_db),
            simulation_id,
            start_date,
            end_date,
            str(simulation_db),
            experiment_hash,
        ]

    completed = subprocess.run(
        [str(wrapper)],
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )

    assert completed.returncode == 69
    assert completed.stdout == ""
    assert "Toss client ID is unavailable from Keychain" in completed.stderr
    assert "parameter not set" not in completed.stderr
    captured = capture.read_bytes().split(b"\0")
    assert captured.pop() == b""
    decoded = [value.decode("utf-8") for value in captured]
    assert decoded[:2] == ["-I", "-c"]
    assert decoded[-9:] == expected_tail
    assert len(decoded) == 12


def test_missing_optional_trade_webhook_leaves_durable_outbox_pending(
    tmp_path: Path,
) -> None:
    state_db = tmp_path / "intraday.sqlite3"
    now = datetime(2026, 8, 30, tzinfo=timezone.utc)
    notifier = DiscordTradeNotifier(
        env={"DISCORD_ALLOWED_CHANNEL_ID": "123456789012345678"}
    )
    with SQLiteStateStore(state_db) as store:
        store.enqueue_notification_once(
            notification_key="paper-daily-without-webhook",
            message="intraday_paper_daily_report",
            level="info",
            payload={"status": "NO_ENTRY"},
            created_at=now,
        )

        _drain_intraday_notifications(store=store, notifier=notifier, at=now)

        outbox = store.list_notification_outbox()
    assert notifier.enabled is False
    assert len(outbox) == 1
    assert outbox[0]["status"] == "PENDING"
    assert outbox[0]["attempt_count"] == 0
    assert outbox[0]["claimed_at"] is None

    approval_worker = (ROOT / "src" / "turtle_approval" / "worker.py").read_text(
        encoding="utf-8"
    )
    assert "os.environ.pop(TOKEN_ENV, None)" in approval_worker
    stream_worker = (ROOT / "src" / "turtle_bot" / "toss_stream.py").read_text(
        encoding="utf-8"
    )
    assert 'os.environ.pop("TOSS_CLIENT_ID", "")' in stream_worker
    assert 'os.environ.pop("TOSS_CLIENT_SECRET", "")' in stream_worker


def test_approval_and_news_use_direct_clean_python_execution() -> None:
    sources = {
        name: (ROOT / "ops" / name).read_text(encoding="utf-8")
        for name in ("run-discord-approval.command", "run-news-shadow.command")
    }
    for name, source in sources.items():
        assert "/bin/zsh -f -c" not in source
        blocks = re.findall(
            r'"\$python_bin" -I(?: -u)? -c \'\n(.*?)\n\'', source, re.DOTALL
        )
        assert len(blocks) == 2
        for block in blocks:
            compile(block, name, "exec")

    approval = sources["run-discord-approval.command"]
    assert approval.index("approval worker import does not match this release") < (
        approval.index("security find-generic-password")
    )


def test_redacted_heartbeat_producers_are_bound_to_release_and_component() -> None:
    wrappers = {
        "planner": (ROOT / "ops" / "run-intraday-shadow.command").read_text(
            encoding="utf-8"
        ),
        "stream": (ROOT / "ops" / "run-toss-stream.command").read_text(
            encoding="utf-8"
        ),
        "approval": (ROOT / "ops" / "run-discord-approval.command").read_text(
            encoding="utf-8"
        ),
        "news": (ROOT / "ops" / "run-news-shadow.command").read_text(
            encoding="utf-8"
        ),
    }
    for component, source in wrappers.items():
        assert "heartbeat.json" in source
        assert "release_sha" in source
        if component != "stream":
            assert "RedactedHeartbeatWriter" in source
            assert f'component="{component}"' in source
    stream_worker = (ROOT / "src" / "turtle_bot" / "toss_stream.py").read_text(
        encoding="utf-8"
    )
    assert "RedactedHeartbeatWriter" in stream_worker
    assert 'component="stream"' in stream_worker

    assert "PRAGMA quick_check" in wrappers["planner"]
    assert "db_quick_check=checked" in wrappers["planner"]
    for component in ("stream", "approval", "news"):
        assert "PRAGMA quick_check" not in wrappers[component]
        assert "db_quick_check=" not in wrappers[component]

    assert '--heartbeat "$heartbeat_path"' in wrappers["stream"]
    assert '--release-sha "$release_sha"' in wrappers["stream"]
    assert 'writer.write("OK" if not result.error_codes else "DEGRADED")' in (
        wrappers["news"]
    )
