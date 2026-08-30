from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from decimal import Decimal
from os import environ
from pathlib import Path

from . import __version__
from .ai_summary import AiSummaryConfig, OpenAICompatibleAiClient
from .backtest import (
    BacktestConfig,
    BacktestCosts,
    BacktestEngine,
    backtest_result_to_dict,
    export_backtest_report_json,
    load_candles_csv,
)
from .config import load_config
from .data_download import (
    download_krx_ohlcv,
    fetch_naver_kospi200_symbols,
    write_symbols_file,
)
from .operations import (
    LaunchdServiceConfig,
    check_operations_config,
    ensure_runtime_dirs,
    operations_checks_payload,
    run_dashboard_server,
    render_launchd_plist,
    run_paper_service,
    write_launchd_plist,
)
from .pit_universe import load_pit_universe_csv
from .reports import DailyReportConfig, export_daily_report_json
from .runtime import Runtime
from .momentum_backtest import (
    MomentumBacktestConfig,
    export_momentum_backtest_report_json,
    load_momentum_backtest_candles,
    run_momentum_backtest,
)
from .scan_backtest import (
    ScanBacktestConfig,
    export_scan_backtest_report_json,
    load_scan_backtest_candles,
    run_scan_backtest,
)
from .state_store import SQLiteStateStore
from .domain import PositionDirection


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
        "--dashboard-server",
        action="store_true",
        help="Run the read-only local dashboard server backed by the SQLite state DB",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host for --dashboard-server",
        metavar="HOST",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Port for --dashboard-server",
        metavar="PORT",
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
        "--shadow-service",
        action="store_true",
        help="Run the shadow validation service heartbeat loop from a shadow config",
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
    parser.add_argument(
        "--daily-report-ai-summary",
        action="store_true",
        help="Append an AI Korean summary to --daily-report output",
    )
    parser.add_argument(
        "--backtest-csv",
        type=Path,
        action="append",
        help="Load daily OHLCV CSV data for a backtest; repeat for more files",
        metavar="PATH",
    )
    parser.add_argument(
        "--backtest-report",
        type=Path,
        help="Write a backtest JSON report to PATH",
        metavar="PATH",
    )
    parser.add_argument(
        "--backtest-portfolio",
        action="store_true",
        help="Run a portfolio backtest across symbols in the loaded CSV data",
    )
    parser.add_argument(
        "--pit-universe-csv",
        type=Path,
        help=(
            "Point-in-time universe CSV for portfolio/scan/momentum backtests; "
            "requires snapshot coverage for every tested date"
        ),
        metavar="PATH",
    )
    parser.add_argument(
        "--backtest-default-symbol",
        default="",
        help="Symbol to use when a backtest CSV has no symbol/ticker column",
        metavar="SYMBOL",
    )
    parser.add_argument(
        "--initial-equity",
        default=None,
        help="Backtest starting equity; defaults to 10000000",
        metavar="DECIMAL",
    )
    parser.add_argument(
        "--unit-qty",
        default="1",
        help="Fixed unit quantity when risk sizing is not used",
        metavar="DECIMAL",
    )
    parser.add_argument(
        "--risk-pct-per-unit",
        default=None,
        help="Risk sizing percentage per unit, e.g. 0.007",
        metavar="DECIMAL",
    )
    parser.add_argument(
        "--lot-size",
        default="1",
        help="Lot size for risk-based unit sizing",
        metavar="DECIMAL",
    )
    parser.add_argument(
        "--max-total-long-units",
        type=int,
        default=None,
        help="Maximum total open long units across the portfolio",
        metavar="N",
    )
    parser.add_argument(
        "--max-total-short-units",
        type=int,
        default=None,
        help="Maximum total open short units across the portfolio",
        metavar="N",
    )
    parser.add_argument(
        "--backtest-direction",
        choices=("long", "short", "both"),
        default=None,
        help="Allowed backtest direction; defaults to config or long",
    )
    parser.add_argument(
        "--commission-rate",
        default="0",
        help="Backtest commission rate per fill, e.g. 0.00015",
        metavar="DECIMAL",
    )
    parser.add_argument(
        "--fixed-commission",
        default="0",
        help="Fixed commission per fill",
        metavar="DECIMAL",
    )
    parser.add_argument(
        "--tax-rate",
        default="0",
        help="Sell-side tax rate for backtest exits",
        metavar="DECIMAL",
    )
    parser.add_argument(
        "--slippage-rate",
        default="0",
        help="Slippage rate applied to simulated fills",
        metavar="DECIMAL",
    )
    parser.add_argument(
        "--download-krx-ohlcv",
        action="store_true",
        help="Download KRX daily OHLCV data through the optional pykrx dependency",
    )
    parser.add_argument(
        "--download-naver-kospi200-symbols",
        action="store_true",
        help="Download current KOSPI200 symbols from Naver Finance",
    )
    parser.add_argument(
        "--symbols-output",
        type=Path,
        default=Path("data/symbols/kospi200.txt"),
        help="Output path for downloaded symbol lists",
        metavar="PATH",
    )
    parser.add_argument(
        "--krx-symbol",
        action="append",
        default=[],
        help="KRX symbol to download; repeat or use comma-separated symbols",
        metavar="SYMBOL",
    )
    parser.add_argument(
        "--krx-symbols-file",
        type=Path,
        help="Text file with one KRX symbol per line",
        metavar="PATH",
    )
    parser.add_argument(
        "--krx-start",
        default="20150101",
        help="KRX download start date in YYYYMMDD",
        metavar="YYYYMMDD",
    )
    parser.add_argument(
        "--krx-end",
        default="20260612",
        help="KRX download end date in YYYYMMDD",
        metavar="YYYYMMDD",
    )
    parser.add_argument(
        "--krx-raw-dir",
        type=Path,
        default=Path("data/raw/krx"),
        help="Directory for raw pykrx CSV copies",
        metavar="PATH",
    )
    parser.add_argument(
        "--krx-normalized-dir",
        type=Path,
        default=Path("data/normalized/krx"),
        help="Directory for normalized backtest CSV files",
        metavar="PATH",
    )
    parser.add_argument(
        "--krx-sleep-seconds",
        type=float,
        default=1.0,
        help="Delay between KRX symbol downloads",
        metavar="SECONDS",
    )
    parser.add_argument(
        "--krx-unadjusted",
        action="store_true",
        help="Download unadjusted KRX prices instead of adjusted prices",
    )
    parser.add_argument(
        "--krx-continue-on-error",
        action="store_true",
        help="Continue KRX downloads and report failures instead of stopping",
    )
    parser.add_argument(
        "--momentum-backtest",
        action="store_true",
        help="Run relative-strength momentum backtest using data files in --momentum-data-dir",
    )
    parser.add_argument(
        "--momentum-data-dir",
        type=Path,
        default=Path("data/normalized/us_yahoo"),
        help="Directory of normalized daily OHLCV CSV files for momentum backtest",
        metavar="PATH",
    )
    parser.add_argument(
        "--momentum-market-symbol",
        default="SPY",
        help="Market filter symbol for momentum backtest",
        metavar="SYMBOL",
    )
    parser.add_argument(
        "--momentum-lookback-days",
        type=int,
        default=126,
        help="Momentum lookback window in trading days",
        metavar="N",
    )
    parser.add_argument(
        "--momentum-skip-days",
        type=int,
        default=21,
        help="Recent trading days excluded from momentum score",
        metavar="N",
    )
    parser.add_argument(
        "--momentum-trend-ma-days",
        type=int,
        default=200,
        help="Moving-average window for market and symbol trend filters",
        metavar="N",
    )
    parser.add_argument(
        "--momentum-exit-ma-days",
        type=int,
        default=75,
        help="Moving-average window for momentum exits",
        metavar="N",
    )
    parser.add_argument(
        "--momentum-max-positions",
        type=int,
        default=5,
        help="Maximum simultaneous momentum positions",
        metavar="N",
    )
    parser.add_argument(
        "--momentum-accept-top-n",
        type=int,
        default=2,
        help="Number of top-ranked momentum symbols accepted per day",
        metavar="N",
    )
    parser.add_argument(
        "--momentum-max-exposure-pct",
        default="0.50",
        help="Maximum total momentum position exposure as a fraction of equity",
        metavar="DECIMAL",
    )
    parser.add_argument(
        "--momentum-cash-reserve-pct",
        default=None,
        help="Minimum cash reserve fraction for momentum; overrides --momentum-max-exposure-pct",
        metavar="DECIMAL",
    )
    parser.add_argument(
        "--momentum-target-position-pct",
        default="0.10",
        help="Target equity allocation per new momentum position",
        metavar="DECIMAL",
    )
    parser.add_argument(
        "--momentum-min-price",
        default="5",
        help="Minimum close price for momentum candidates",
        metavar="DECIMAL",
    )
    parser.add_argument(
        "--momentum-min-average-daily-value",
        default="50000000",
        help="Minimum 20-day average traded value for momentum candidates",
        metavar="DECIMAL",
    )
    parser.add_argument(
        "--momentum-no-market-filter",
        action="store_true",
        help="Disable the SPY moving-average market filter",
    )
    parser.add_argument(
        "--momentum-buy-commission-rate",
        default="0.001",
        help="Momentum buy commission rate per fill; Toss US stock default is 0.001",
        metavar="DECIMAL",
    )
    parser.add_argument(
        "--momentum-sell-commission-rate",
        default="0.001",
        help="Momentum sell commission rate per fill; Toss US stock default is 0.001",
        metavar="DECIMAL",
    )
    parser.add_argument(
        "--momentum-sell-sec-fee-rate",
        default="0.0000206",
        help="Momentum sell-side SEC fee rate; Toss US stock default is 0.0000206",
        metavar="DECIMAL",
    )
    parser.add_argument(
        "--momentum-min-sec-fee",
        default="0.01",
        help="Minimum SEC fee per momentum sell fill in USD",
        metavar="DECIMAL",
    )
    parser.add_argument(
        "--scan-backtest",
        action="store_true",
        help="Run market-scan backtest using data files in --scan-data-dir",
    )
    parser.add_argument(
        "--scan-data-dir",
        type=Path,
        default=Path("data/normalized/krx"),
        help="Directory of normalized daily OHLCV CSV files for scan backtest",
        metavar="PATH",
    )
    parser.add_argument(
        "--scan-top-n",
        type=int,
        default=20,
        help="Number of market-scan recommendations per day",
        metavar="N",
    )
    parser.add_argument(
        "--accept-top-n",
        type=int,
        default=5,
        help="Number of recommended symbols accepted per day",
        metavar="N",
    )
    parser.add_argument(
        "--accept-hold-days",
        type=int,
        default=1,
        help="Trading days to keep accepted symbols eligible for entry",
        metavar="N",
    )
    parser.add_argument(
        "--scan-min-price",
        default="1000",
        help="Minimum prior close for scan candidates",
        metavar="DECIMAL",
    )
    parser.add_argument(
        "--scan-min-average-daily-value",
        default="100000000",
        help="Minimum average traded value for scan candidates",
        metavar="DECIMAL",
    )
    parser.add_argument(
        "--scan-max-breakout-distance-pct",
        default=None,
        help="Maximum percent distance below breakout line for scan candidates",
        metavar="DECIMAL",
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


def _decimal(value: str | Decimal | None, default: Decimal | None = None) -> Decimal:
    if value is None:
        if default is None:
            raise ValueError("decimal value is required")
        return default
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _momentum_max_exposure_from_args(args: argparse.Namespace) -> Decimal:
    if args.momentum_cash_reserve_pct is not None:
        return Decimal("1") - _decimal(args.momentum_cash_reserve_pct)
    return _decimal(args.momentum_max_exposure_pct)


def _backtest_directions(value: str | None) -> tuple[PositionDirection, ...] | None:
    if value is None:
        return None
    if value == "both":
        return (PositionDirection.LONG, PositionDirection.SHORT)
    if value == "short":
        return (PositionDirection.SHORT,)
    return (PositionDirection.LONG,)


def _load_pit_universe_from_args(args: argparse.Namespace):
    if args.pit_universe_csv is not None:
        return load_pit_universe_csv(args.pit_universe_csv)
    if args.config is None:
        return None
    loaded_config = load_config(args.config)
    if loaded_config.runtime.pit_universe_csv is None:
        return None
    pit_path = Path(loaded_config.runtime.pit_universe_csv)
    if not pit_path.is_absolute():
        pit_path = args.config.parent / pit_path
    return load_pit_universe_csv(pit_path)


def _backtest_config(args: argparse.Namespace) -> BacktestConfig:
    loaded_config = load_config(args.config) if args.config is not None else None
    return BacktestConfig(
        initial_equity=_decimal(
            args.initial_equity,
            Decimal("10000000"),
        ),
        unit_qty=_decimal(args.unit_qty, Decimal("1")),
        risk_pct_per_unit=(
            _decimal(args.risk_pct_per_unit)
            if args.risk_pct_per_unit is not None
            else (loaded_config.risk_pct_per_unit if loaded_config is not None else None)
        ),
        lot_size=_decimal(args.lot_size, Decimal("1")),
        minimum_tick=(
            loaded_config.minimum_tick if loaded_config is not None else Decimal("0")
        ),
        n_method=loaded_config.n_method if loaded_config is not None else "turtle",
        stop_n=loaded_config.stop_n if loaded_config is not None else Decimal("2"),
        max_units_per_symbol=(
            loaded_config.max_units_per_symbol if loaded_config is not None else 4
        ),
        max_total_long_units=(
            args.max_total_long_units
            if args.max_total_long_units is not None
            else (loaded_config.max_total_long_units if loaded_config is not None else 12)
        ),
        max_total_short_units=(
            args.max_total_short_units
            if args.max_total_short_units is not None
            else (loaded_config.max_total_short_units if loaded_config is not None else 12)
        ),
        pyramid_step_n=(
            loaded_config.pyramid_step_n if loaded_config is not None else Decimal("0.5")
        ),
        allowed_directions=(
            _backtest_directions(args.backtest_direction)
            or (
                loaded_config.backtest_allowed_directions
                if loaded_config is not None
                else (PositionDirection.LONG,)
            )
        ),
        costs=BacktestCosts(
            commission_rate=_decimal(args.commission_rate, Decimal("0")),
            fixed_commission=_decimal(args.fixed_commission, Decimal("0")),
            tax_rate=_decimal(args.tax_rate, Decimal("0")),
            slippage_rate=_decimal(args.slippage_rate, Decimal("0")),
        ),
    )


def _momentum_backtest_config(
    args: argparse.Namespace,
    *,
    pit_universe,
) -> MomentumBacktestConfig:
    return MomentumBacktestConfig(
        initial_equity=_decimal(args.initial_equity, Decimal("100000")),
        market_symbol=args.momentum_market_symbol,
        momentum_lookback_days=args.momentum_lookback_days,
        momentum_skip_days=args.momentum_skip_days,
        trend_ma_days=args.momentum_trend_ma_days,
        exit_ma_days=args.momentum_exit_ma_days,
        max_positions=args.momentum_max_positions,
        accept_top_n=args.momentum_accept_top_n,
        max_exposure_pct=_momentum_max_exposure_from_args(args),
        target_position_pct=_decimal(args.momentum_target_position_pct),
        min_price=_decimal(args.momentum_min_price),
        min_average_daily_value=_decimal(args.momentum_min_average_daily_value),
        use_market_filter=not args.momentum_no_market_filter,
        buy_commission_rate=_decimal(args.momentum_buy_commission_rate),
        sell_commission_rate=_decimal(args.momentum_sell_commission_rate),
        sell_sec_fee_rate=_decimal(args.momentum_sell_sec_fee_rate),
        min_sec_fee=_decimal(args.momentum_min_sec_fee),
        pit_universe=pit_universe,
    )


def _krx_symbols(args: argparse.Namespace) -> tuple[str, ...]:
    symbols: list[str] = []
    for value in args.krx_symbol:
        symbols.extend(part.strip() for part in value.split(",") if part.strip())
    if args.krx_symbols_file is not None:
        for line in args.krx_symbols_file.read_text(encoding="utf-8").splitlines():
            clean = line.strip()
            if clean and not clean.startswith("#"):
                symbols.append(clean)
    return tuple(
        dict.fromkeys(
            symbol.zfill(6) if symbol.isdigit() and len(symbol) < 6 else symbol
            for symbol in symbols
        )
    )


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

    if args.dashboard_server:
        run_dashboard_server(
            config_path=args.config,
            state_db=args.state_db,
            host=args.host,
            port=args.port,
        )
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

    if args.paper_service or args.shadow_service:
        config_path = _require_config(parser, args.config)
        snapshot = run_paper_service(
            config_path=config_path,
            state_db=args.state_db,
            log_dir=args.log_dir,
            interval_seconds=args.interval_seconds,
            once=args.once,
            expected_mode="shadow" if args.shadow_service else None,
        )
        if args.once:
            print(json.dumps(snapshot.as_payload(), indent=2, sort_keys=True))
        return 0

    if args.daily_report is not None:
        loaded_config = load_config(args.config) if args.config is not None else None
        with SQLiteStateStore(args.state_db) as store:
            report = export_daily_report_json(
                store,
                args.daily_report,
                config=DailyReportConfig(
                    report_date=_report_date(args.report_date, args.report_timezone),
                    timezone_name=args.report_timezone,
                ),
            )
        if args.daily_report_ai_summary:
            if loaded_config is None:
                parser.error("--daily-report-ai-summary requires --config")
            if loaded_config.ai.provider != "openai_compatible":
                parser.error(
                    "--daily-report-ai-summary currently supports only "
                    "ai.provider=openai_compatible"
                )
            api_key = environ.get(loaded_config.ai.api_key_env)
            summary = OpenAICompatibleAiClient(
                config=AiSummaryConfig(
                    base_url=loaded_config.ai.base_url,
                    model=loaded_config.ai.model,
                    api_key=api_key,
                    timeout_seconds=loaded_config.ai.timeout_seconds,
                    max_tokens=loaded_config.ai.max_tokens,
                    temperature=float(loaded_config.ai.temperature),
                )
            ).summarize_daily_report(report)
            report = {**report, "ai_summary": summary}
            args.daily_report.write_text(
                json.dumps(report, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    if args.backtest_csv:
        pit_universe = _load_pit_universe_from_args(args)
        candles = []
        for csv_path in args.backtest_csv:
            candles.extend(
                load_candles_csv(
                    csv_path,
                    default_symbol=args.backtest_default_symbol,
                )
            )
        engine = BacktestEngine(_backtest_config(args))
        result = (
            engine.run_portfolio(
                candles,
                entry_filter=(
                    (lambda timestamp, symbol: pit_universe.is_eligible(timestamp, symbol))
                    if pit_universe is not None
                    else None
                ),
            )
            if args.backtest_portfolio
            else engine.run(candles)
        )
        if args.backtest_report is not None:
            export_backtest_report_json(result, args.backtest_report)
        payload = backtest_result_to_dict(result)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.momentum_backtest:
        candles = load_momentum_backtest_candles(args.momentum_data_dir)
        pit_universe = _load_pit_universe_from_args(args)
        result = run_momentum_backtest(
            candles,
            config=_momentum_backtest_config(
                args,
                pit_universe=pit_universe,
            ),
        )
        if args.backtest_report is not None:
            export_momentum_backtest_report_json(result, args.backtest_report)
        print(json.dumps(result.as_payload(), indent=2, sort_keys=True))
        return 0

    if args.scan_backtest:
        pit_universe = _load_pit_universe_from_args(args)
        candles = load_scan_backtest_candles(args.scan_data_dir)
        backtest_config = _backtest_config(args)
        engine = BacktestEngine(backtest_config)
        scan_result = run_scan_backtest(
            candles,
            engine=engine,
            config=ScanBacktestConfig(
                scan_top_n=args.scan_top_n,
                accept_top_n=args.accept_top_n,
                accept_hold_days=args.accept_hold_days,
                min_price=_decimal(args.scan_min_price, Decimal("1000")),
                min_average_daily_value=_decimal(
                    args.scan_min_average_daily_value,
                    Decimal("100000000"),
                ),
                max_breakout_distance_pct=(
                    _decimal(args.scan_max_breakout_distance_pct)
                    if args.scan_max_breakout_distance_pct is not None
                    else None
                ),
                pit_universe=pit_universe,
                scan_directions=backtest_config.allowed_directions,
            ),
        )
        if args.backtest_report is not None:
            export_scan_backtest_report_json(scan_result, args.backtest_report)
        print(json.dumps(scan_result.as_payload(), indent=2, sort_keys=True))
        return 0

    if args.download_krx_ohlcv:
        symbols = _krx_symbols(args)
        if not symbols:
            parser.error("--download-krx-ohlcv requires --krx-symbol or --krx-symbols-file")
        results = download_krx_ohlcv(
            symbols=symbols,
            start=args.krx_start,
            end=args.krx_end,
            raw_dir=args.krx_raw_dir,
            normalized_dir=args.krx_normalized_dir,
            sleep_seconds=args.krx_sleep_seconds,
            adjusted=not args.krx_unadjusted,
            continue_on_error=args.krx_continue_on_error,
        )
        payload = {
            "download_type": "krx_ohlcv",
            "start": args.krx_start,
            "end": args.krx_end,
            "adjusted": not args.krx_unadjusted,
            "items": [
                {
                    "symbol": result.symbol,
                    "rows": result.rows,
                    "raw_path": str(result.raw_path),
                    "normalized_path": str(result.normalized_path),
                    "error": result.error,
                }
                for result in results
            ],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.download_naver_kospi200_symbols:
        symbols = fetch_naver_kospi200_symbols()
        target = write_symbols_file(symbols, args.symbols_output)
        payload = {
            "download_type": "naver_kospi200_symbols",
            "count": len(symbols),
            "path": str(target),
            "symbols": list(symbols),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.config is not None:
        parser.print_usage()
        return 1

    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
