from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path

from . import __version__
from .config import load_config
from .operations import (
    LaunchdServiceConfig,
    check_operations_config,
    ensure_runtime_dirs,
    operations_checks_payload,
    render_launchd_plist,
    run_paper_service,
    write_launchd_plist,
)
from .reports import DailyReportConfig, export_daily_report_json
from .runtime import Runtime
from .state_store import SQLiteStateStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="turtle_bot",
        description="Turtle Trading bot (strategy core)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"turtle-bot {__version__}",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Load a YAML config file",
        metavar="PATH",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate config file and exit",
    )
    parser.add_argument(
        "--health-json",
        action="store_true",
        help="Print read-only health payload from default empty runtime state",
    )
    parser.add_argument(
        "--state-db",
        type=Path,
        default=Path("state/turtle.sqlite3"),
        help="SQLite state database path for operational commands",
        metavar="PATH",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=Path("logs"),
        help="Runtime log directory for operational commands",
        metavar="PATH",
    )
    parser.add_argument(
        "--repo-dir",
        type=Path,
        default=Path.cwd(),
        help="Repository working directory for launchd rendering",
        metavar="PATH",
    )
    parser.add_argument(
        "--python-executable",
        type=Path,
        help="Python executable path for launchd rendering",
        metavar="PATH",
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=60,
        help="Paper service heartbeat interval",
    )
    parser.add_argument(
        "--ops-check",
        action="store_true",
        help="Validate local paper-mode operational paths and config",
    )
    parser.add_argument(
        "--ensure-runtime-dirs",
        action="store_true",
        help="Create state/log directories used by operational commands",
    )
    parser.add_argument(
        "--render-launchd-plist",
        action="store_true",
        help="Print a paper-mode launchd plist template",
    )
    parser.add_argument(
        "--write-launchd-plist",
        type=Path,
        help="Write a paper-mode launchd plist template to PATH",
        metavar="PATH",
    )
    parser.add_argument(
        "--paper-service",
        action="store_true",
        help="Run the paper-mode service heartbeat loop",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one paper service heartbeat and exit",
    )
    parser.add_argument(
        "--daily-report",
        type=Path,
        help="Write a postmarket daily JSON report to PATH",
        metavar="PATH",
    )
    parser.add_argument(
        "--report-date",
        help="Report date in YYYY-MM-DD format; defaults to today in report timezone",
        metavar="YYYY-MM-DD",
    )
    parser.add_argument(
        "--report-timezone",
        default="Asia/Seoul",
        help="Timezone used to group runtime events by trading day",
    )
    return parser


def _require_config(parser: argparse.ArgumentParser, config: Path | None) -> Path:
    if config is None:
        parser.error("this command requires --config")
    return config


def _launchd_config(args: argparse.Namespace) -> LaunchdServiceConfig:
    return LaunchdServiceConfig.default(
        repo_dir=args.repo_dir,
        config_path=args.config,
        state_db=args.state_db,
        log_dir=args.log_dir,
        python_executable=args.python_executable,
        interval_seconds=args.interval_seconds,
    )


def _report_date(value: str | None, timezone_name: str) -> date:
    if value:
        return date.fromisoformat(value)
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(timezone_name)).date()
    except Exception:
        return datetime.now().date()


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.check_config:
        if args.config is None:
            parser.error("--check-config requires --config")
        load_config(args.config)
        return 0

    if args.health_json:
        payload = Runtime.default().health_snapshot().as_payload()
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.ensure_runtime_dirs:
        ensure_runtime_dirs(state_db=args.state_db, log_dir=args.log_dir)
        return 0

    if args.ops_check:
        config_path = _require_config(parser, args.config)
        checks = check_operations_config(
            config_path=config_path,
            state_db=args.state_db,
            log_dir=args.log_dir,
        )
        payload = operations_checks_payload(checks)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["status"] == "ready" else 2

    if args.render_launchd_plist:
        _require_config(parser, args.config)
        print(render_launchd_plist(_launchd_config(args)), end="")
        return 0

    if args.write_launchd_plist is not None:
        _require_config(parser, args.config)
        target = write_launchd_plist(args.write_launchd_plist, _launchd_config(args))
        print(str(target))
        return 0

    if args.paper_service:
        config_path = _require_config(parser, args.config)
        snapshot = run_paper_service(
            config_path=config_path,
            state_db=args.state_db,
            log_dir=args.log_dir,
            interval_seconds=args.interval_seconds,
            once=args.once,
        )
        if args.once:
            print(json.dumps(snapshot.as_payload(), indent=2, sort_keys=True))
        return 0

    if args.daily_report is not None:
        with SQLiteStateStore(args.state_db) as store:
            report = export_daily_report_json(
                store,
                args.daily_report,
                config=DailyReportConfig(
                    report_date=_report_date(args.report_date, args.report_timezone),
                    timezone_name=args.report_timezone,
                ),
            )
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    if args.config is not None:
        parser.print_usage()
        return 1

    return 0
