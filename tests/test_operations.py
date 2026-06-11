from __future__ import annotations

import json
import plistlib
import sys
from pathlib import Path

from turtle_bot.cli import run
from turtle_bot.operations import (
    LaunchdServiceConfig,
    check_operations_config,
    operations_checks_payload,
    render_launchd_plist,
    run_paper_service,
)
from turtle_bot.state_store import SQLiteStateStore


def _write_config(path: Path, *, live_enabled: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "toss:",
                f"  live_enabled: {str(live_enabled).lower()}",
                "strategy:",
                "  minimum_tick: 1",
                "  n_method: turtle",
                "  risk:",
                "    risk_pct_per_unit: 0.005",
                "    stop_n: 2",
                "    pyramid_step_n: 0.5",
                "    max_units_per_symbol: 4",
                "    max_total_long_units: 12",
            ]
        ),
        encoding="utf-8",
    )


def test_render_launchd_plist_is_valid_paper_service_plist(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "local.yaml"
    state_db = tmp_path / "state" / "turtle.sqlite3"
    log_dir = tmp_path / "logs"
    service = LaunchdServiceConfig.default(
        repo_dir=tmp_path,
        config_path=config_path,
        state_db=state_db,
        log_dir=log_dir,
        python_executable=sys.executable,
        interval_seconds=30,
    )

    plist = plistlib.loads(render_launchd_plist(service).encode("utf-8"))

    assert plist["Label"] == "com.sands15.toss-turtle-bot"
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] == {"Crashed": True}
    assert plist["ProgramArguments"] == [
        str(Path(sys.executable).resolve()),
        "-m",
        "turtle_bot",
        "--config",
        str(config_path.resolve()),
        "--state-db",
        str(state_db.resolve()),
        "--log-dir",
        str(log_dir.resolve()),
        "--paper-service",
        "--interval-seconds",
        "30",
    ]
    assert plist["StandardOutPath"].endswith("turtle-paper.out.log")
    assert plist["StandardErrorPath"].endswith("turtle-paper.err.log")


def test_checked_in_launchd_template_is_valid_plist() -> None:
    template = Path("ops/launchd/com.sands15.toss-turtle-bot.plist")

    plist = plistlib.loads(template.read_bytes())

    assert plist["Label"] == "com.sands15.toss-turtle-bot"
    assert "--paper-service" in plist["ProgramArguments"]


def test_operations_check_blocks_live_enabled_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "local.yaml"
    state_db = tmp_path / "state" / "turtle.sqlite3"
    log_dir = tmp_path / "logs"
    _write_config(config_path, live_enabled=True)
    state_db.parent.mkdir()
    log_dir.mkdir()

    payload = operations_checks_payload(
        check_operations_config(
            config_path=config_path,
            state_db=state_db,
            log_dir=log_dir,
        )
    )

    assert payload["status"] == "blocked"
    assert any("live trading is enabled" in blocker for blocker in payload["blockers"])


def test_paper_service_once_records_heartbeat_without_live_orders(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "local.yaml"
    state_db = tmp_path / "state" / "turtle.sqlite3"
    log_dir = tmp_path / "logs"
    _write_config(config_path)

    snapshot = run_paper_service(
        config_path=config_path,
        state_db=state_db,
        log_dir=log_dir,
        interval_seconds=5,
        once=True,
        sleep=lambda _: None,
    )

    store = SQLiteStateStore(state_db)
    events = store.list_runtime_events(limit=2)
    assert snapshot.mode == "paper"
    assert snapshot.ready is False
    assert snapshot.blockers == ("market_data_provider_not_configured",)
    assert [event["message"] for event in events] == [
        "paper_service_heartbeat",
        "paper_service_started",
    ]
    assert store.has_unresolved_client_order_id("anything") is False


def test_cli_writes_launchd_plist_and_runs_paper_service_once(
    tmp_path: Path,
    capsys,
) -> None:
    config_path = tmp_path / "config" / "local.yaml"
    state_db = tmp_path / "state" / "turtle.sqlite3"
    log_dir = tmp_path / "logs"
    plist_path = tmp_path / "LaunchAgents" / "bot.plist"
    _write_config(config_path)

    assert run(
        [
            "--config",
            str(config_path),
            "--state-db",
            str(state_db),
            "--log-dir",
            str(log_dir),
            "--ensure-runtime-dirs",
        ]
    ) == 0
    assert run(
        [
            "--config",
            str(config_path),
            "--repo-dir",
            str(tmp_path),
            "--python-executable",
            sys.executable,
            "--state-db",
            str(state_db),
            "--log-dir",
            str(log_dir),
            "--write-launchd-plist",
            str(plist_path),
        ]
    ) == 0
    capsys.readouterr()
    assert "--paper-service" in plistlib.loads(plist_path.read_bytes())[
        "ProgramArguments"
    ]

    assert run(
        [
            "--config",
            str(config_path),
            "--state-db",
            str(state_db),
            "--log-dir",
            str(log_dir),
            "--paper-service",
            "--once",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "paper"
    assert payload["ready"] is False
