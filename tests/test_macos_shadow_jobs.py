from __future__ import annotations

import plistlib
import re
from datetime import datetime, timezone
from pathlib import Path

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
        for block in re.findall(r"'\n'\"'\"'\n(.*?)\n'\"'\"'", source, re.DOTALL):
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
