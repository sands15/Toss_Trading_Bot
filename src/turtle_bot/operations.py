from __future__ import annotations

import json
import hashlib
import os
import plistlib
import re
import secrets
import subprocess
import sys
import tempfile
import time
import zipfile
from os import environ
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, Callable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import yaml
from turtle_approval import (
    ApprovalEnvelope,
    load_envelope as load_approval_envelope,
    require_private_directory,
)

from .config import intraday_simulation_experiment_hash, load_config
from .health import HealthServer, HealthSnapshot
from .intraday import build_intraday_plan, intraday_plan_payload
from .intraday_paper import (
    IntradayPaperConfig,
    IntradayPaperStore,
    PaperSimulationError,
    PaperSimulationBlocked,
    simulation_account_key,
)
from .market_calendar import MarketCalendarConfig, MarketCalendarGate
from .notifier import DiscordTradeNotifier, MemoryNotifier
from .paper_runtime import PaperOrderIntent, PaperRuntimeConfig, PaperTradingRuntime
from .position_sync import TossPositionSync
from .state_store import SQLiteStateStore
from .rate_limit import RateLimitQueue
from .toss_client import (
    TOSS_BASE_URL,
    SimulationReadOnlyTossTransport,
    ShadowReadOnlyTossTransport,
    TossApiError,
    TossClient,
    TossCredentials,
    TossTransport,
)
from .toss_conditional import TossConditionalOrderAdapter
from .toss_live_adapter import TossLiveBrokerAdapter
from .live_execution import LiveOrderOrchestrator
from .live_order import OrderIntent, OrderType
from .live_safety import PreTradeSafety, PreTradeSafetyConfig, PreTradeSafetyContext
from .domain import Side, TurtleSystem
from .toss_market_data import TossMarketDataConfig, TossReadOnlyMarketDataProvider
from .universe import Universe, UniverseBuilder, UniversePolicy
from .watchlist import Watchlist, WatchlistBuilder, WatchlistRow


DEFAULT_SERVICE_LABEL = "com.sands15.toss-turtle-bot"
DEFAULT_DASHBOARD_BLOCKERS = (
    "runtime.mode must be paper, shadow, or live",
    "runtime.symbols or runtime.universe_candidate_symbols is required",
    "TOSS_CLIENT_ID is not configured",
    "TOSS_CLIENT_SECRET is not configured",
    "toss.account_seq is not configured",
)
_INTRADAY_SYMBOL_PATTERN = re.compile(
    r"(?=.{1,16}\Z)[A-Z][A-Z0-9]*(?:[.-][A-Z0-9]+)?\Z"
)
_INTRADAY_RANKING_COUNT = 20
# ponytail: keep the first shadow pass bounded to five; add a paced scan cursor
# only if observed shadow days show that this produces false-empty selections.
_INTRADAY_CANDIDATE_REVIEW_LIMIT = 5
_INTRADAY_DAILY_CANDLE_COUNT = 20
_INTRADAY_PREMARKET_CANDLE_COUNT = 200
_INTRADAY_MIN_PREMARKET_CANDLES = 3
_INTRADAY_STOCK_ALL_MIN_INTERVAL_SECONDS = 1.05
_INTRADAY_SELECTOR_OPENAPI_VERSION = "1.2.14"
_INTRADAY_SELECTOR_OPENAPI_SHA256 = (
    "a7b32ba754401d13fa649ba91eebd212420eb1afab28e9c2c0d6ea8d43055fed"
)
_INTRADAY_REQUIRED_CONFIG_FIELDS = (
    "cash_allocation_fraction",
    "risk_fraction",
    "take_profit_fraction",
    "stop_fraction",
    "stop_limit_buffer_fraction",
    "max_entry_slippage_fraction",
    "estimated_round_trip_cost_fraction",
    "estimated_fixed_round_trip_cost",
    "minimum_reward_risk_ratio",
    "max_spread_fraction",
    "max_last_mid_deviation_fraction",
    "max_notional",
    "plan_lead_minutes",
    "minimum_plan_lead_minutes",
    "quote_max_age_seconds",
    "orderbook_max_age_seconds",
    "max_quote_skew_seconds",
    "entry_start_minutes_after_open",
    "entry_expiry_minutes_after_open",
    "force_exit_minutes_before_close",
)
_INTRADAY_AUTOMATIC_SELECTION_FIELDS = (
    "selection_rank_max_age_seconds",
    "selection_min_price",
    "selection_min_trading_amount",
    "selection_min_change_fraction",
    "selection_max_change_fraction",
    "selection_min_average_daily_value",
    "selection_max_average_daily_range_fraction",
    "selection_max_premarket_range_fraction",
)
_INTRADAY_PUBLIC_BLOCKER_REASONS = {
    "intraday_market_holiday": "공식 미국장 캘린더상 휴장일입니다.",
    "intraday_plan_window_not_started": "설정된 장전 계획 가능 시각 전입니다.",
    "intraday_plan_deadline_missed": "장전 계획 마감 시각이 지났습니다.",
    "intraday_plan_deadline_missed_during_reads": "조회 중 장전 계획 마감 시각을 넘겼습니다.",
    "intraday_plan_deadline_missed_before_lock": "계획 잠금 직전에 장전 마감 시각을 넘겼습니다.",
    "intraday_not_in_premarket": "공식 프리마켓 시간 밖입니다.",
    "intraday_account_not_flat": "계좌에 기존 보유 종목이 있습니다.",
    "intraday_open_order_exists": "계좌에 미해결 일반 주문이 있습니다.",
    "intraday_conditional_order_exists": "계좌에 열린 조건주문이 있습니다.",
    "intraday_no_cash_buying_power": "사용 가능한 USD 현금 매수 가능액이 없습니다.",
    "intraday_cost_buffer_below_commission": "설정 비용 버퍼가 확인된 왕복 수수료보다 작습니다.",
    "intraday_spread_too_wide": "프리마켓 스프레드가 설정 한도를 넘었습니다.",
    "intraday_last_mid_deviation": "현재가와 호가 중간값의 괴리가 설정 한도를 넘었습니다.",
    "intraday_ranking_stale": "자동 선정 랭킹이 잠금 시점 기준으로 오래되었습니다.",
    "intraday_price_stale": "현재가가 계획 잠금 시점 기준으로 오래되었습니다.",
    "intraday_orderbook_stale": "호가가 계획 잠금 시점 기준으로 오래되었습니다.",
    "intraday_cash_snapshot_stale": "현금 매수 가능액이 계획 잠금 시점 기준으로 오래되었습니다.",
    "intraday_warning_check_stale": "종목 경고 확인이 계획 잠금 시점 기준으로 오래되었습니다.",
    "intraday_account_check_stale": "계좌 대조 결과가 계획 잠금 시점 기준으로 오래되었습니다.",
    "intraday_no_eligible_candidate": "자동 선정 안전 조건을 모두 통과한 종목이 없습니다.",
    "intraday_stock_warning_changed": "계획 잠금 전에 종목 경고 상태가 변경되었습니다.",
    "intraday_plan_invalid": "현금·위험·가격 조건으로 유효한 계획을 만들 수 없습니다.",
    "intraday_daily_plan_locked_config_changed": "오늘 계획이 잠긴 뒤 설정이 변경되었습니다.",
    "intraday_daily_plan_locked_guardrails_changed": "오늘 계획이 잠긴 뒤 안전 한도가 변경되었습니다.",
    "intraday_execution_engine_not_enabled": "실주문 실행 엔진이 비활성 상태입니다.",
    "intraday_simulation_blocked": "가상 원장에 해결되지 않은 상태가 있어 새 계획을 차단했습니다.",
    "intraday_simulation_integrity_failure": "가상 원장 또는 계획 무결성 검증에 실패했습니다.",
}


class DashboardTradingLoopStopped(RuntimeError):
    """Raised internally to stop the dashboard-managed trading loop."""


class IntradayPlanBlocked(RuntimeError):
    """A fail-closed, user-actionable reason why no daily plan was saved."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class _IntradaySchedule:
    session_date: date
    premarket_open: datetime
    premarket_close: datetime
    regular_open: datetime
    regular_close: datetime


def ensure_local_config(
    config_path: str | Path | None = None,
    *,
    template_path: str | Path | None = None,
) -> tuple[Path, bool]:
    target = Path(config_path or Path("config") / "local.yaml").expanduser()
    if target.exists():
        return target, False
    template = Path(template_path or target.parent / "local.example.yaml").expanduser()
    if not template.exists():
        template = Path(template_path or target.parent / "example.yaml").expanduser()
    if not template.exists():
        raise FileNotFoundError(f"config template not found for {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    return target, True


@dataclass(frozen=True)
class LaunchdServiceConfig:
    label: str
    repo_dir: Path
    python_executable: Path
    config_path: Path
    state_db: Path
    log_dir: Path
    interval_seconds: int = 60

    @classmethod
    def default(
        cls,
        *,
        repo_dir: str | Path,
        config_path: str | Path | None = None,
        state_db: str | Path | None = None,
        log_dir: str | Path | None = None,
        python_executable: str | Path | None = None,
        interval_seconds: int = 60,
        label: str = DEFAULT_SERVICE_LABEL,
    ) -> "LaunchdServiceConfig":
        root = Path(repo_dir).expanduser().resolve()
        return cls(
            label=label,
            repo_dir=root,
            python_executable=Path(python_executable or sys.executable)
            .expanduser()
            .resolve(),
            config_path=Path(config_path or root / "config" / "local.yaml")
            .expanduser()
            .resolve(),
            state_db=Path(state_db or root / "state" / "turtle.sqlite3")
            .expanduser()
            .resolve(),
            log_dir=Path(log_dir or root / "logs").expanduser().resolve(),
            interval_seconds=interval_seconds,
        )

    @property
    def stdout_path(self) -> Path:
        return self.log_dir / "turtle-paper.out.log"

    @property
    def stderr_path(self) -> Path:
        return self.log_dir / "turtle-paper.err.log"

    def program_arguments(self) -> list[str]:
        return [
            str(self.python_executable),
            "-m",
            "turtle_bot",
            "--config",
            str(self.config_path),
            "--state-db",
            str(self.state_db),
            "--log-dir",
            str(self.log_dir),
            "--paper-service",
            "--interval-seconds",
            str(self.interval_seconds),
        ]


@dataclass(frozen=True)
class OperationsCheck:
    name: str
    passed: bool
    message: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "message": self.message,
        }


def render_launchd_plist(config: LaunchdServiceConfig) -> str:
    payload = {
        "Label": config.label,
        "ProgramArguments": config.program_arguments(),
        "WorkingDirectory": str(config.repo_dir),
        "RunAtLoad": True,
        "KeepAlive": {"Crashed": True},
        "StandardOutPath": str(config.stdout_path),
        "StandardErrorPath": str(config.stderr_path),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    }
    return plistlib.dumps(payload, sort_keys=True).decode("utf-8")


def write_launchd_plist(path: str | Path, config: LaunchdServiceConfig) -> Path:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_launchd_plist(config), encoding="utf-8")
    return target


def ensure_runtime_dirs(*, state_db: str | Path, log_dir: str | Path) -> None:
    Path(state_db).expanduser().parent.mkdir(parents=True, exist_ok=True)
    Path(log_dir).expanduser().mkdir(parents=True, exist_ok=True)


def check_operations_config(
    *,
    config_path: str | Path,
    state_db: str | Path,
    log_dir: str | Path,
    env: Mapping[str, str] | None = None,
) -> tuple[OperationsCheck, ...]:
    checks: list[OperationsCheck] = []
    env = env if env is not None else environ
    config_file = Path(config_path).expanduser()
    state_parent = Path(state_db).expanduser().parent
    log_path = Path(log_dir).expanduser()

    if config_file.exists():
        checks.append(
            OperationsCheck("config_exists", True, f"config exists: {config_file}")
        )
        try:
            config = load_config(config_file)
        except Exception as exc:
            checks.append(
                OperationsCheck("config_loads", False, f"config load failed: {exc}")
            )
        else:
            checks.append(OperationsCheck("config_loads", True, "config loads"))
            checks.extend(
                _build_toss_readiness_checks(
                    config=config,
                    env=env,
                )
            )
            mode = _runtime_mode(config)
            checks.append(
                OperationsCheck(
                    "live_mode_gate",
                    (mode == "live" and config.live_enabled)
                    or (mode != "live" and not config.live_enabled),
                    "live mode gate is configured"
                    if (
                        (mode == "live" and config.live_enabled)
                        or (mode != "live" and not config.live_enabled)
                    )
                    else (
                        "toss.live_enabled must be true for runtime.mode live"
                        if mode == "live"
                        else "toss.live_enabled must be false unless runtime.mode is live"
                    ),
                )
            )
            intraday_blockers = _intraday_config_blockers(config)
            if config.strategy_kind == "intraday":
                checks.append(
                    OperationsCheck(
                        "intraday_shadow_gate",
                        not intraday_blockers,
                        "intraday shadow-only configuration is complete"
                        if not intraday_blockers
                        else "; ".join(intraday_blockers),
                    )
                )
    else:
        checks.append(
            OperationsCheck("config_exists", False, f"config missing: {config_file}")
        )

    checks.append(
        OperationsCheck(
            "state_parent_exists",
            state_parent.exists(),
            f"state parent exists: {state_parent}"
            if state_parent.exists()
            else f"state parent missing: {state_parent}",
        )
    )
    checks.append(
        OperationsCheck(
            "log_dir_exists",
            log_path.exists(),
            f"log dir exists: {log_path}"
            if log_path.exists()
            else f"log dir missing: {log_path}",
        )
    )
    return tuple(checks)


def operations_checks_payload(checks: Sequence[OperationsCheck]) -> dict[str, Any]:
    return {
        "status": "ready" if all(check.passed for check in checks) else "blocked",
        "checks": [check.as_payload() for check in checks],
        "blockers": [check.message for check in checks if not check.passed],
    }


def build_dashboard_server(
    *,
    state_db: str | Path,
    config_path: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    start_server: bool = False,
    env: Mapping[str, str] | None = None,
    transport: TossTransport | None = None,
) -> HealthServer:
    watchlist_name = "premarket"
    default_blockers: Sequence[str] = DEFAULT_DASHBOARD_BLOCKERS
    settings_payload: Mapping[str, Any] = {}
    settings_updater: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None
    action_runner: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None
    config_created = False
    if config_path is not None:
        config_path, config_created = ensure_local_config(config_path)
        env_values = _dashboard_env_values(env)
        config = load_config(config_path)
        watchlist_name = config.runtime.watchlist_name
        default_blockers = _paper_service_config_blockers(
            config,
            env_values,
        )
        settings_payload = _dashboard_settings_payload(
            config,
            env_values,
            config_path=config_path,
            config_created=config_created,
        )

        def settings_updater(payload: Mapping[str, Any]) -> Mapping[str, Any]:
            nonlocal config, default_blockers, settings_payload, watchlist_name
            result = update_dashboard_settings(config_path, payload, env=env_values)
            config = load_config(config_path)
            watchlist_name = config.runtime.watchlist_name
            default_blockers = _paper_service_config_blockers(config, env_values)
            settings_payload = _dashboard_settings_payload(
                config,
                env_values,
                config_path=config_path,
                config_created=False,
            )
            return {"config": result, "settings": settings_payload}

        action_lock = Lock()
        loop_lock = Lock()
        loop_stop_event: Event | None = None
        loop_thread: Thread | None = None
        loop_live_consent: Mapping[str, str] | None = None

        def refresh_dashboard_state() -> None:
            nonlocal config, default_blockers, settings_payload, watchlist_name
            config = load_config(config_path)
            watchlist_name = config.runtime.watchlist_name
            default_blockers = _paper_service_config_blockers(config, env_values)
            settings_payload = _dashboard_settings_payload(
                config,
                env_values,
                config_path=config_path,
                config_created=False,
            )

        def start_dashboard_trading_loop(
            live_consent: Mapping[str, str] | None = None
        ) -> str:
            nonlocal loop_stop_event, loop_thread, loop_live_consent
            with loop_lock:
                if loop_thread is not None and loop_thread.is_alive():
                    return "already_running"
                loop_stop_event = Event()
                active_stop_event = loop_stop_event
                loop_live_consent = dict(live_consent) if live_consent else None
                active_config = load_config(config_path)

                def interruptible_sleep(seconds: float) -> None:
                    if active_stop_event.wait(seconds):
                        raise DashboardTradingLoopStopped("dashboard trading loop stopped")
                    if _dashboard_loop_should_stop(config_path):
                        raise DashboardTradingLoopStopped("dashboard emergency stop is active")

                def run_loop() -> None:
                    local_consent = loop_live_consent
                    discord_notifier = DiscordTradeNotifier(env=env_values)
                    with SQLiteStateStore(state_db) as loop_store:
                        loop_store.record_runtime_event(
                            "INFO",
                            "live_trading_loop_started",
                            {"source": "dashboard"},
                        )
                    try:
                        run_paper_service(
                            config_path=config_path,
                            state_db=state_db,
                            log_dir=active_config.runtime.log_dir,
                            interval_seconds=active_config.runtime.interval_seconds,
                            live_consent=local_consent,
                            once=False,
                            sleep=interruptible_sleep,
                            env=env_values,
                        )
                    except DashboardTradingLoopStopped:
                        with SQLiteStateStore(state_db) as loop_store:
                            loop_store.record_runtime_event(
                                "INFO",
                                "live_trading_loop_stopped",
                                {"source": "dashboard"},
                            )
                    except Exception as exc:
                        payload = {"source": "dashboard", "error": str(exc)}
                        with SQLiteStateStore(state_db) as loop_store:
                            loop_store.record_runtime_event(
                                "ERROR",
                                "live_trading_loop_failed",
                                payload,
                            )
                        discord_notifier.notify(
                            "live_trading_loop_failed",
                            level="error",
                            payload=payload,
                        )

                loop_thread = Thread(
                    target=run_loop,
                    name="turtle-dashboard-live-loop",
                    daemon=True,
                )
                loop_thread.start()
                return "started"

        def action_runner(path: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
            nonlocal config, default_blockers, settings_payload, watchlist_name, loop_stop_event
            if path == "/dashboard/actions/apply-safe-pilot":
                consent_context = _extract_live_consent_context(
                    payload=payload,
                    config=config,
                )
                result = apply_safe_pilot_settings(config_path, state_db=state_db)
                refresh_dashboard_state()
                loop_status = start_dashboard_trading_loop(live_consent=consent_context)
                return {
                    "status": "started" if loop_status == "started" else "already_running",
                    "loop": loop_status,
                    "safe_pilot": {
                        "symbol": result["symbol"],
                        "max_order_notional": result["max_order_notional"],
                        "daily_notional_limit": result["daily_notional_limit"],
                    },
                    "settings": settings_payload,
                }
            if path == "/dashboard/actions/stop-trading":
                result = stop_live_trading_settings(config_path)
                if loop_stop_event is not None:
                    loop_stop_event.set()
                refresh_dashboard_state()
                open_orders = _dashboard_open_order_summary(state_db)
                return {
                    "status": "stopped",
                    "kill_switch": result,
                    "open_orders": open_orders,
                    "settings": settings_payload,
                }
            if path == "/dashboard/actions/test-discord-alert":
                discord_notifier = DiscordTradeNotifier(env=env_values)
                if not discord_notifier.enabled:
                    return {
                        "status": "not_configured",
                        "webhook_configured": False,
                    }
                alert_payload = {
                    "source": "dashboard",
                    "account_alias": _account_alias_from_config_path(config_path),
                }
                sent = discord_notifier.notify(
                    "discord_alert_test",
                    level="info",
                    payload=alert_payload,
                )
                with SQLiteStateStore(state_db) as store:
                    event_payload: dict[str, Any] = {
                        "source": "dashboard",
                        "webhook_configured": True,
                        "account_alias": alert_payload["account_alias"],
                    }
                    if not sent and discord_notifier.last_error:
                        event_payload["error"] = discord_notifier.last_error
                    store.record_runtime_event(
                        "INFO" if sent else "WARN",
                        "discord_alert_test_sent"
                        if sent
                        else "discord_alert_test_failed",
                        event_payload,
                    )
                result = {
                    "status": "sent" if sent else "failed",
                    "webhook_configured": True,
                }
                if not sent and discord_notifier.last_error:
                    result["error"] = discord_notifier.last_error
                return result
            if path == "/dashboard/actions/export-backup":
                result = export_dashboard_backup(
                    config_path=config_path,
                    state_db=state_db,
                    log_dir=config.runtime.log_dir,
                )
                with SQLiteStateStore(state_db) as store:
                    store.record_runtime_event(
                        "INFO",
                        "dashboard_backup_exported",
                        {
                            "path": result["path"],
                            "files": result["files"],
                        },
                    )
                return result
            if path == "/dashboard/actions/build-watchlist":
                if not action_lock.acquire(blocking=False):
                    raise RuntimeError("watchlist generation is already running")
                try:
                    config = load_config(config_path)
                    client = _toss_client_from_config(config, env=env_values, transport=transport)
                    with SQLiteStateStore(state_db) as store:
                        market_data = TossReadOnlyMarketDataProvider(
                            client=client,
                            config=TossMarketDataConfig(
                                candle_interval=config.runtime.candle_interval,
                                candle_count=config.runtime.candle_count,
                                local_timezone=config.runtime.timezone_name,
                                exclude_current_session=config.runtime.exclude_current_session,
                            ),
                            store=store,
                            now=lambda: datetime.now(timezone.utc),
                        )
                        universe = _build_universe(
                            config,
                            client=client,
                            market_data=market_data,
                            store=store,
                            now=lambda: datetime.now(timezone.utc),
                        )
                        symbols = universe.symbols() if universe is not None else config.runtime.symbols
                        watchlist = _build_and_save_watchlist(
                            config,
                            market_data=market_data,
                            client=client,
                            store=store,
                            now=lambda: datetime.now(timezone.utc),
                            symbols=symbols,
                        )
                        rows = [] if watchlist is None else [_watchlist_row_payload(row) for row in watchlist.rows]
                    return {
                        "status": "generated",
                        "count": len(rows),
                        "watchlist": rows,
                    }
                finally:
                    action_lock.release()
            if path == "/dashboard/actions/live-smoke-test":
                confirmation = str(payload.get("confirmation") or "").strip()
                if confirmation != "실주문 테스트":
                    raise ValueError("confirmation must be 실주문 테스트")
                if not action_lock.acquire(blocking=False):
                    raise RuntimeError("live smoke test action is already running")
                try:
                    config = load_config(config_path)
                    result = run_live_smoke_test(
                        config_path=config_path,
                        state_db=state_db,
                        env=env_values,
                        transport=transport,
                    )
                    refresh_dashboard_state()
                    return result
                finally:
                    action_lock.release()
            if path != "/dashboard/actions/live-once":
                raise ValueError(f"unsupported action: {path}")
            confirmation = str(payload.get("confirmation") or "").strip()
            if confirmation != "LIVE PILOT 실행":
                raise ValueError("confirmation must be LIVE PILOT 실행")
            if not action_lock.acquire(blocking=False):
                raise RuntimeError("live once action is already running")
            try:
                config = load_config(config_path)
                consent_context = _extract_live_consent_context(
                    payload=payload,
                    config=config,
                )
                snapshot = run_paper_service(
                    config_path=config_path,
                    state_db=state_db,
                    log_dir=config.runtime.log_dir,
                    interval_seconds=config.runtime.interval_seconds,
                    live_consent=consent_context,
                    once=True,
                    env=env_values,
                    transport=transport,
                )
                watchlist_name = config.runtime.watchlist_name
                default_blockers = _paper_service_config_blockers(config, env_values)
                settings_payload = _dashboard_settings_payload(
                    config,
                    env_values,
                    config_path=config_path,
                    config_created=False,
                )
                return {"status": "completed", "snapshot": snapshot.as_payload()}
            finally:
                action_lock.release()

    def snapshot_provider() -> HealthSnapshot:
        with SQLiteStateStore(state_db) as store:
            events = store.list_runtime_events(limit=100)
            latest = _latest_health_event_payload(events)
            current_mode = _runtime_mode(config) if config_path is not None else None
            if latest is None:
                return paper_service_health(
                    store,
                    mode=current_mode or "paper",
                    blockers=default_blockers,
                    ready=not default_blockers,
                    watchlist_name=watchlist_name,
                )
            if (
                current_mode is not None
                and str(latest.get("mode") or "").strip().lower() != current_mode
            ):
                return paper_service_health(
                    store,
                    mode=current_mode,
                    blockers=default_blockers,
                    ready=not default_blockers,
                    watchlist_name=watchlist_name,
                )
            return _health_snapshot_from_payload(
                latest,
                store=store,
                watchlist_name=watchlist_name,
            )

    def events_provider(limit: int | None = None) -> list[dict[str, Any]]:
        with SQLiteStateStore(state_db) as store:
            return store.list_runtime_events(limit=limit)

    def broker_snapshots_provider() -> dict[str, Any]:
        with SQLiteStateStore(state_db) as store:
            return {
                "holdings": _broker_snapshot_record_payload(
                    store.latest_broker_snapshot_record("holdings")
                ),
                "open_orders": _broker_snapshot_record_payload(
                    store.latest_broker_snapshot_record("open_orders")
                ),
                "closed_orders": _broker_snapshot_record_payload(
                    store.latest_broker_snapshot_record("closed_orders")
                ),
            }

    return HealthServer(
        snapshot_provider,
        events_provider=events_provider,
        broker_snapshots_provider=broker_snapshots_provider,
        host=host,
        port=port,
        start_server=start_server,
        settings=settings_payload,
        settings_updater=settings_updater,
        action_runner=action_runner,
    )


def _broker_snapshot_record_payload(record: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(record, Mapping):
        return None
    captured_at = record.get("captured_at")
    if isinstance(captured_at, datetime):
        captured = captured_at.isoformat()
    elif captured_at is None:
        captured = None
    else:
        captured = str(captured_at)
    payload = record.get("payload")
    return {
        "captured_at": captured,
        "payload": dict(payload) if isinstance(payload, Mapping) else payload,
    }


def _dashboard_env_values(env: Mapping[str, str] | None = None) -> Mapping[str, str]:
    if env is not None:
        return env
    values = dict(environ)
    for name in (
        DiscordTradeNotifier.DEFAULT_WEBHOOK_ENV,
        DiscordTradeNotifier.DEFAULT_CHANNEL_ENV,
    ):
        if not values.get(name):
            user_value = _windows_user_environment_value(name)
            if user_value:
                values[name] = user_value
    return values


def _windows_user_environment_value(name: str) -> str:
    if sys.platform != "win32":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value or "").strip()
    except Exception:
        return ""


def _dashboard_settings_payload(
    config,
    env: Mapping[str, str] | None = None,
    *,
    config_path: str | Path | None = None,
    config_created: bool = False,
) -> dict[str, Any]:
    env_values = env if env is not None else environ
    client_id_env = str(config.toss.client_id_env or "").strip()
    client_secret_env = str(config.toss.client_secret_env or "").strip()
    account_alias = ""
    if config_path is not None:
        try:
            raw_config = _read_yaml(Path(config_path))
            raw_toss = raw_config.get("toss", {})
            if isinstance(raw_toss, Mapping):
                account_alias = str(raw_toss.get("account_alias") or "").strip()
        except Exception:
            account_alias = ""
    if not account_alias:
        account_alias = _tailscale_profile_name()
    pilot_symbol = (
        next(iter(config.live.allowed_symbols), None)
        or next(iter(config.runtime.symbols), None)
        or next(iter(config.runtime.universe_candidate_symbols), None)
    )
    pilot_amount = config.live.daily_notional_limit or config.live.max_order_notional
    return {
        "config": {
            "path": str(config_path) if config_path is not None else None,
            "created_from_template": config_created,
        },
        "strategy_kind": config.strategy_kind,
        "runtime": {
            "mode": config.runtime.mode,
            "market": config.runtime.market,
            "timezone": config.runtime.timezone_name,
            "market_calendar_open_sessions": list(config.runtime.market_calendar_open_sessions),
            "day_market_enabled": any(
                str(name).strip().lower() == "daymarket"
                for name in config.runtime.market_calendar_open_sessions
            ),
        },
        "toss": {
            "live_enabled": config.live_enabled,
            "require_live_consent": config.toss.require_live_consent,
            "account_seq_configured": bool(config.toss.account_seq),
            "account_seq": config.toss.account_seq or "",
            "account_alias": account_alias,
            "client_id_configured": bool(client_id_env and env_values.get(client_id_env)),
            "client_secret_configured": bool(
                client_secret_env and env_values.get(client_secret_env)
            ),
            "live_consent_ids_configured": bool(config.toss.allowed_live_consent_ids),
            "live_consent_ids_count": len(config.toss.allowed_live_consent_ids),
        },
        "notifications": {
            "discord_webhook_configured": DiscordTradeNotifier(
                env=env_values
            ).enabled,
            "discord_webhook_env": DiscordTradeNotifier.DEFAULT_WEBHOOK_ENV,
            "discord_channel_env": DiscordTradeNotifier.DEFAULT_CHANNEL_ENV,
        },
        "ai": {
            "enabled": config.ai.enabled,
            "provider": config.ai.provider,
            "model": config.ai.model,
            "base_url": config.ai.base_url,
            "mode": "read_only_explanations",
        },
        "live": {
            "emergency_stop": config.live.emergency_stop,
            "allowed_symbols": list(config.live.allowed_symbols),
            "max_order_quantity": (
                str(config.live.max_order_quantity)
                if config.live.max_order_quantity is not None
                else None
            ),
            "max_order_notional": (
                str(config.live.max_order_notional)
                if config.live.max_order_notional is not None
                else None
            ),
            "daily_order_count_limit": config.live.daily_order_count_limit,
            "daily_notional_limit": (
                str(config.live.daily_notional_limit)
                if config.live.daily_notional_limit is not None
                else None
            ),
            "require_market_open": config.live.require_market_open,
            "require_clean_reconcile": config.live.require_clean_reconcile,
            "block_unresolved_orders": config.live.block_unresolved_orders,
            "confirm_high_value_order": config.live.confirm_high_value_order,
            "cancel_after_ack": config.live.cancel_after_ack,
            "max_consecutive_order_failures": config.live.max_consecutive_order_failures,
        },
        "pilot": {
            "symbol": str(pilot_symbol).upper() if pilot_symbol else None,
            "max_quantity": (
                str(config.live.max_order_quantity)
                if config.live.max_order_quantity is not None
                else "1"
            ),
            "daily_orders": config.live.daily_order_count_limit or 1,
            "daily_amount": str(pilot_amount) if pilot_amount is not None else None,
            "stop_active": config.live.emergency_stop,
            "cancel_after_ack": config.live.cancel_after_ack,
            "failure_fuse": config.live.max_consecutive_order_failures,
        },
        "momentum": {
            "cash_reserve_pct": str(config.momentum_cash_reserve_pct),
            "max_exposure_pct": str(config.momentum_max_exposure_pct),
            "target_position_pct": str(config.momentum_target_position_pct),
            "max_positions": config.momentum_max_positions,
            "accept_top_n": config.momentum_accept_top_n,
            "exit_ma_days": config.momentum_exit_ma_days,
            "lookback_days": config.momentum_lookback_days,
            "skip_days": config.momentum_skip_days,
            "trend_ma_days": config.momentum_trend_ma_days,
        },
    }


def _to_decimal(value: Any, *, field: str, min_value: Decimal, max_value: Decimal) -> Decimal:
    try:
        value_decimal = Decimal(str(value))
    except Exception as exc:  # pragma: no cover - defensive conversion failure
        raise ValueError(f"invalid decimal for {field}") from exc
    if value_decimal < min_value or value_decimal > max_value:
        raise ValueError(f"{field} must be in [{min_value}, {max_value}]")
    return value_decimal


def _to_int(value: Any, *, field: str, min_value: int, max_value: int) -> int:
    try:
        value_int = int(value)
    except Exception as exc:  # pragma: no cover - defensive conversion failure
        raise ValueError(f"invalid integer for {field}") from exc
    if value_int < min_value or value_int > max_value:
        raise ValueError(f"{field} must be in [{min_value}, {max_value}]")
    return value_int


def _coerce_percentage(value: Any, *, field: str) -> Decimal:
    if value is None:
        raise ValueError(f"{field} is required")
    value_decimal = _to_decimal(
        value,
        field=field,
        min_value=Decimal("0"),
        max_value=Decimal("100"),
    )
    if value_decimal > Decimal("1"):
        value_decimal = value_decimal / Decimal("100")
    if value_decimal > Decimal("1") or value_decimal < Decimal("0"):
        raise ValueError(f"{field} must be in [0, 1]")
    return value_decimal


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        content = yaml.safe_load(handle)
    return content if isinstance(content, dict) else {}


def update_momentum_settings(
    config_path: str | Path,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    config_file = Path(config_path)
    if not config_file.exists():
        raise FileNotFoundError(f"config file not found: {config_file}")

    momentum_payload = payload.get("momentum", {})
    if not isinstance(momentum_payload, Mapping):
        raise ValueError("momentum settings missing")

    momentum_cash_reserve_pct = _coerce_percentage(
        momentum_payload.get("cash_reserve_pct"),
        field="cash_reserve_pct",
    )
    momentum_target_position_pct = _coerce_percentage(
        momentum_payload.get("target_position_pct"),
        field="target_position_pct",
    )
    momentum_max_positions = _to_int(
        momentum_payload.get("max_positions"),
        field="max_positions",
        min_value=1,
        max_value=200,
    )
    momentum_accept_top_n = _to_int(
        momentum_payload.get("accept_top_n"),
        field="accept_top_n",
        min_value=1,
        max_value=200,
    )
    momentum_exit_ma_days = _to_int(
        momentum_payload.get("exit_ma_days"),
        field="exit_ma_days",
        min_value=1,
        max_value=10000,
    )
    momentum_lookback_days = _to_int(
        momentum_payload.get("lookback_days"),
        field="lookback_days",
        min_value=1,
        max_value=10000,
    )
    momentum_skip_days = _to_int(
        momentum_payload.get("skip_days"),
        field="skip_days",
        min_value=0,
        max_value=10000,
    )
    momentum_trend_ma_days = _to_int(
        momentum_payload.get("trend_ma_days"),
        field="trend_ma_days",
        min_value=1,
        max_value=10000,
    )

    if momentum_skip_days >= momentum_lookback_days:
        raise ValueError("skip_days must be smaller than lookback_days")

    raw = _read_yaml(config_file)
    strategy = raw.get("strategy", {})
    if not isinstance(strategy, Mapping):
        strategy = {}
    strategy_payload = dict(strategy)
    momentum = strategy_payload.get("momentum", {})
    if not isinstance(momentum, Mapping):
        momentum = {}
    momentum_data = dict(momentum)

    momentum_data["cash_reserve_pct"] = float(momentum_cash_reserve_pct)
    momentum_data["max_exposure_pct"] = float(Decimal("1") - momentum_cash_reserve_pct)
    momentum_data["target_position_pct"] = float(momentum_target_position_pct)
    momentum_data["max_positions"] = momentum_max_positions
    momentum_data["accept_top_n"] = momentum_accept_top_n
    momentum_data["exit_ma_days"] = momentum_exit_ma_days
    momentum_data["lookback_days"] = momentum_lookback_days
    momentum_data["skip_days"] = momentum_skip_days
    momentum_data["trend_ma_days"] = momentum_trend_ma_days

    strategy_payload["momentum"] = momentum_data
    raw["strategy"] = strategy_payload
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(
        yaml.safe_dump(raw, sort_keys=False, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )

    return dict(raw)


def _write_yaml(config_file: Path, raw: Mapping[str, Any]) -> None:
    config_file.write_text(
        yaml.safe_dump(raw, sort_keys=False, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )


def _first_safe_pilot_symbol(config, *, state_db: str | Path | None = None) -> str:
    if state_db is not None:
        try:
            with SQLiteStateStore(state_db) as store:
                latest_watchlist = store.latest_watchlist(name=config.runtime.watchlist_name)
        except Exception:
            latest_watchlist = None
        if latest_watchlist is not None:
            for row in latest_watchlist.rows:
                symbol = str(row.symbol or "").strip().upper()
                if symbol:
                    return symbol
    for symbol in config.runtime.symbols:
        normalized = str(symbol or "").strip().upper()
        if normalized:
            return normalized
    for symbol in config.runtime.universe_candidate_symbols:
        normalized = str(symbol or "").strip().upper()
        if normalized:
            return normalized
    return ""


def _default_safe_pilot_notional(market: str) -> str:
    return "300" if str(market or "").strip().upper() == "US" else "10000"


def apply_safe_pilot_settings(
    config_path: str | Path,
    *,
    state_db: str | Path | None = None,
) -> dict[str, Any]:
    """Apply the smallest dashboard-managed live pilot settings."""
    config_file = Path(config_path)
    if not config_file.exists():
        raise FileNotFoundError(f"config file not found: {config_file}")
    raw = _read_yaml(config_file)
    config = load_config(config_file)
    symbol = _first_safe_pilot_symbol(config, state_db=state_db)
    if not symbol:
        raise ValueError("runtime.symbols or universe_candidate_symbols is required")

    toss = raw.get("toss", {})
    if not isinstance(toss, Mapping):
        toss = {}
    toss_data = dict(toss)
    toss_data["live_enabled"] = True

    runtime = raw.get("runtime", {})
    if not isinstance(runtime, Mapping):
        runtime = {}
    runtime_data = dict(runtime)
    runtime_data["mode"] = "live"

    live = raw.get("live", {})
    if not isinstance(live, Mapping):
        live = {}
    live_data = dict(live)
    notional_limit = str(
        live_data.get("max_order_notional")
        or live_data.get("daily_notional_limit")
        or _default_safe_pilot_notional(config.runtime.market)
    )
    live_data.update(
        {
            "emergency_stop": False,
            "allowed_symbols": [symbol],
            "max_order_quantity": 1,
            "max_order_notional": notional_limit,
            "daily_order_count_limit": 1,
            "daily_notional_limit": notional_limit,
            "require_market_open": True,
            "require_clean_reconcile": True,
            "block_unresolved_orders": True,
            "confirm_high_value_order": False,
            "cancel_after_ack": True,
            "max_consecutive_order_failures": 3,
        }
    )

    raw["toss"] = toss_data
    raw["runtime"] = runtime_data
    raw["live"] = live_data
    _write_yaml(config_file, raw)
    return {
        "status": "configured",
        "symbol": symbol,
        "max_order_notional": notional_limit,
        "daily_notional_limit": notional_limit,
        "config": dict(raw),
    }


def stop_live_trading_settings(config_path: str | Path) -> dict[str, Any]:
    """Persist the dashboard live-trading kill switch."""
    config_file = Path(config_path)
    if not config_file.exists():
        raise FileNotFoundError(f"config file not found: {config_file}")
    raw = _read_yaml(config_file)
    live = raw.get("live", {})
    if not isinstance(live, Mapping):
        live = {}
    live_data = dict(live)
    live_data["emergency_stop"] = True
    raw["live"] = live_data
    _write_yaml(config_file, raw)
    return {"status": "stopped", "emergency_stop": True, "config": dict(raw)}


def export_dashboard_backup(
    *,
    config_path: str | Path,
    state_db: str | Path,
    log_dir: str | Path,
) -> dict[str, Any]:
    config_file = Path(config_path)
    backup_dir = Path(state_db).parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"dashboard-backup-{timestamp}.zip"
    added: list[str] = []

    def add_file(path: Path, arcname: str) -> None:
        if path.exists() and path.is_file():
            archive.write(path, arcname)
            added.append(arcname)

    with zipfile.ZipFile(backup_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        add_file(config_file, "config/local.yaml")
        add_file(Path(state_db), "state/turtle.sqlite3")
        logs = Path(log_dir)
        if not logs.is_absolute():
            config_relative = config_file.parent / logs
            app_relative = config_file.parent.parent / logs
            logs = config_relative if config_relative.exists() else app_relative
        if logs.exists() and logs.is_dir():
            for path in sorted(logs.rglob("*")):
                if path.is_file():
                    add_file(path, f"logs/{path.relative_to(logs).as_posix()}")
    return {
        "status": "exported",
        "path": str(backup_path),
        "files": added,
    }


def _dashboard_loop_should_stop(config_path: str | Path) -> bool:
    try:
        config = load_config(config_path)
    except Exception:
        return False
    return _runtime_mode(config) == "live" and bool(config.live.emergency_stop)


def _dashboard_open_order_summary(state_db: str | Path) -> dict[str, Any]:
    with SQLiteStateStore(state_db) as store:
        events = store.list_runtime_events(limit=100)
    latest = _latest_health_event_payload(events)
    if latest is None:
        return {"count": 0, "items": (), "warning": None}
    open_orders = _payload_items(latest.get("open_orders"))
    warning = (
        "open orders may still need broker-side review"
        if open_orders
        else None
    )
    return {"count": len(open_orders), "items": open_orders, "warning": warning}


def update_dashboard_settings(
    config_path: str | Path,
    payload: Mapping[str, Any],
    *,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Update operator-editable local settings without echoing secrets."""
    config_file = Path(config_path)
    if not config_file.exists():
        raise FileNotFoundError(f"config file not found: {config_file}")
    if not isinstance(payload, Mapping):
        raise ValueError("settings payload missing")

    raw = update_momentum_settings(config_file, payload) if "momentum" in payload else _read_yaml(config_file)
    runtime_payload = payload.get("runtime", {})
    if runtime_payload is None:
        runtime_payload = {}
    if not isinstance(runtime_payload, Mapping):
        raise ValueError("runtime settings must be an object")
    if "day_market_enabled" in runtime_payload:
        runtime = raw.get("runtime", {})
        if not isinstance(runtime, Mapping):
            runtime = {}
        runtime_data = dict(runtime)
        if bool(runtime_payload.get("day_market_enabled")):
            runtime_data["market_calendar_open_sessions"] = ["dayMarket", "regularMarket"]
        else:
            runtime_data["market_calendar_open_sessions"] = ["regularMarket"]
        raw["runtime"] = runtime_data
        _write_yaml(config_file, raw)
    toss_payload = payload.get("toss", {})
    if toss_payload is None:
        toss_payload = {}
    if not isinstance(toss_payload, Mapping):
        raise ValueError("toss settings must be an object")
    toss_changes_requested = any(
        key in toss_payload
        for key in ("account_seq", "client_id", "client_secret", "client_id_env", "client_secret_env")
    )
    if toss_changes_requested:
        confirmation = str(toss_payload.get("identity_confirmation") or "").strip()
        if confirmation != "토스 연결 승인":
            raise ValueError("identity confirmation required for Toss connection changes")

    toss = raw.get("toss", {})
    if not isinstance(toss, Mapping):
        toss = {}
    toss_data = dict(toss)
    client_id_env = str(toss_data.get("client_id_env") or "TOSS_CLIENT_ID").strip()
    client_secret_env = str(toss_data.get("client_secret_env") or "TOSS_CLIENT_SECRET").strip()

    if "client_id_env" in toss_payload:
        client_id_env = str(toss_payload.get("client_id_env") or "").strip()
        if not client_id_env:
            raise ValueError("client_id_env is required")
        toss_data["client_id_env"] = client_id_env
    if "client_secret_env" in toss_payload:
        client_secret_env = str(toss_payload.get("client_secret_env") or "").strip()
        if not client_secret_env:
            raise ValueError("client_secret_env is required")
        toss_data["client_secret_env"] = client_secret_env
    if "account_seq" in toss_payload:
        account_seq = str(toss_payload.get("account_seq") or "").strip()
        if not account_seq:
            raise ValueError("account_seq is required")
        toss_data["account_seq"] = account_seq
    if "account_alias" in toss_payload:
        account_alias = str(toss_payload.get("account_alias") or "").strip()
        toss_data["account_alias"] = account_alias[:80]

    env_values = env if env is not None else environ
    client_id = str(toss_payload.get("client_id") or "").strip()
    client_secret = str(toss_payload.get("client_secret") or "").strip()
    if client_id:
        env_values[client_id_env] = client_id
    if client_secret:
        env_values[client_secret_env] = client_secret

    raw["toss"] = toss_data
    config_file.write_text(
        yaml.safe_dump(raw, sort_keys=False, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )
    return dict(raw)


def run_dashboard_server(
    *,
    state_db: str | Path,
    config_path: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    env: Mapping[str, str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    ensure_runtime_dirs(state_db=state_db, log_dir=Path("logs"))
    config_file = None
    if config_path is not None:
        config_file, _ = ensure_local_config(config_path)
    else:
        config_file, _ = ensure_local_config()
    build_dashboard_server(
        state_db=state_db,
        config_path=config_file,
        host=host,
        port=port,
        start_server=True,
        env=env,
    )
    print(f"Turtle Bot dashboard: http://{host}:{port}/", flush=True)
    while True:  # pragma: no cover - operational host loop
        sleep(3600)


def paper_service_health(
    store: SQLiteStateStore,
    *,
    mode: str = "paper",
    blockers: Sequence[str] = ("market_data_provider_not_configured",),
    ready: bool = False,
    watchlist_name: str = "premarket",
) -> HealthSnapshot:
    positions = tuple(
        {
            "symbol": position.symbol,
            "status": position.status.value,
            "total_qty": str(position.total_qty),
            "avg_entry_price": str(position.avg_entry_price),
        }
        for position in store.list_paper_positions()
    )
    return HealthSnapshot(
        mode=mode,
        ready=ready,
        blockers=tuple(blockers),
        positions=positions,
        open_orders=(),
        watchlist=_latest_watchlist_payload(store, name=watchlist_name),
        generated_at=datetime.now(timezone.utc),
    )


def _latest_health_event_payload(events: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    for event in events:
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if {"mode", "ready", "blockers"}.issubset(payload.keys()):
            return payload
    return None


def _payload_items(value: Any) -> tuple[Mapping[str, Any], ...]:
    if isinstance(value, Mapping):
        items = value.get("items", ())
    else:
        items = value
    if items is None:
        return ()
    return tuple(dict(item) for item in items)


def _payload_timestamp(payload: Mapping[str, Any]) -> datetime:
    raw = payload.get("timestamp")
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _health_snapshot_from_payload(
    payload: Mapping[str, Any],
    *,
    store: SQLiteStateStore,
    watchlist_name: str,
) -> HealthSnapshot:
    watchlist = _payload_items(payload.get("watchlist"))
    latest_watchlist = _latest_watchlist_payload(store, name=watchlist_name)
    if latest_watchlist:
        watchlist = latest_watchlist
    return HealthSnapshot(
        mode=str(payload.get("mode", "paper")),
        ready=bool(payload.get("ready", False)),
        blockers=tuple(str(item) for item in payload.get("blockers", ())),
        positions=_payload_items(payload.get("positions")),
        open_orders=_payload_items(payload.get("open_orders")),
        watchlist=watchlist,
        generated_at=_payload_timestamp(payload),
    )


def _coerce_text(value: Any) -> str:
    text = str(value or "").strip()
    return text


def _mask_secret(value: str) -> str:
    text = _coerce_text(value)
    if not text:
        return ""
    if len(text) <= 4:
        return "*" * len(text)
    return "*" * (len(text) - 4) + text[-4:]


def _extract_live_consent_context(
    *,
    payload: Mapping[str, Any],
    config,
) -> dict[str, str] | None:
    if not getattr(config.toss, "require_live_consent", False):
        return None
    if not config.toss.allowed_live_consent_ids:
        raise ValueError(
            "live consent is enabled but no allowed_live_consent_ids is configured"
        )
    consent_id = _coerce_text(payload.get("consent_id"))
    if not consent_id:
        raise ValueError("consent_id is required for live execution")
    if not any(consent_id == _coerce_text(item) for item in config.toss.allowed_live_consent_ids):
        raise ValueError("consent_id is not authorized")
    context: dict[str, str] = {"consent_id": consent_id}
    operator_id = _coerce_text(payload.get("operator_id"))
    if operator_id:
        context["operator_id"] = operator_id
    return context


def _live_consent_blocker(
    *,
    config,
    live_consent: Mapping[str, str] | None,
) -> str | None:
    if not config.toss.require_live_consent:
        return None
    if not config.toss.allowed_live_consent_ids:
        return "live_consent_ids_not_configured"
    consent_id = _coerce_text(live_consent.get("consent_id") if live_consent else None)
    if not consent_id:
        return "live_consent_id_required"
    if consent_id not in {_coerce_text(item) for item in config.toss.allowed_live_consent_ids}:
        return "live_consent_id_not_allowed"
    return None


def _live_consent_event_metadata(
    live_consent: Mapping[str, str] | None,
) -> dict[str, Any]:
    if not live_consent:
        return {}
    consent_id = _coerce_text(live_consent.get("consent_id"))
    operator_id = _coerce_text(live_consent.get("operator_id"))
    output: dict[str, Any] = {}
    if operator_id:
        output["operator_id"] = operator_id
    if consent_id:
        output["consent_id_mask"] = _mask_secret(consent_id)
    return output


def run_paper_service(
    *,
    config_path: str | Path,
    state_db: str | Path,
    log_dir: str | Path,
    interval_seconds: int = 60,
    once: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    env: Mapping[str, str] | None = None,
    transport: TossTransport | None = None,
    live_consent: Mapping[str, str] | None = None,
    expected_mode: str | None = None,
    expected_simulation: Mapping[str, str] | None = None,
    expected_account_fingerprint: str | None = None,
    paper_status_sink: Callable[..., None] | None = None,
    now=lambda: datetime.now(timezone.utc),
) -> HealthSnapshot:
    if expected_mode not in {None, "shadow"}:
        raise ValueError("expected_mode must be None or shadow")
    simulation_lock = _normalize_expected_simulation(expected_simulation)
    if simulation_lock is not None and expected_mode != "shadow":
        raise ValueError("expected_simulation requires expected_mode=shadow")
    account_lock = (
        str(expected_account_fingerprint).strip()
        if expected_account_fingerprint is not None
        else None
    )
    if account_lock is not None:
        if simulation_lock is None or expected_mode != "shadow":
            raise ValueError(
                "expected_account_fingerprint requires a locked shadow simulation"
            )
        if not re.fullmatch(r"[0-9a-f]{64}", account_lock):
            raise ValueError("expected_account_fingerprint is invalid")
    env_values = env if env is not None else environ
    rate_limits = RateLimitQueue(now=now)
    store: SQLiteStateStore | None = None
    try:
        while True:  # pragma: no branch - one pass when once=True
            config = load_config(config_path)
            active_transport = (
                SimulationReadOnlyTossTransport(transport)
                if simulation_lock is not None
                or expected_mode == "shadow" and config.intraday.simulation_enabled
                else ShadowReadOnlyTossTransport(transport)
                if expected_mode == "shadow"
                else transport
            )
            service_mode = _runtime_mode(config)
            if expected_mode == "shadow":
                _require_shadow_service_config(config)
                if simulation_lock is not None:
                    _require_locked_simulation_config(
                        config,
                        expected=simulation_lock,
                        state_db=state_db,
                        expected_account_fingerprint=account_lock,
                    )
            elif config.live_enabled and service_mode != "live":
                raise RuntimeError(
                    "paper/shadow service refuses configs with toss.live_enabled=true"
                )

            if store is None:
                ensure_runtime_dirs(state_db=state_db, log_dir=log_dir)
                store = SQLiteStateStore(state_db)
                store.record_runtime_event(
                    "INFO",
                    f"{service_mode}_service_started",
                    {"mode": service_mode, "interval_seconds": interval_seconds},
                )

            snapshot = _paper_service_iteration(
                config=config,
                config_path=config_path,
                store=store,
                env=env_values,
                live_consent=live_consent,
                transport=active_transport,
                rate_limits=rate_limits,
                now=now,
            )
            if paper_status_sink is not None:
                _publish_intraday_paper_status(
                    config=config,
                    snapshot=snapshot,
                    sink=paper_status_sink,
                )
            store.record_runtime_event(
                "INFO",
                f"{service_mode}_service_heartbeat",
                snapshot.as_payload(),
            )
            if once:
                return snapshot
            sleep(interval_seconds)
    finally:
        if store is not None:
            store.close()


def _publish_intraday_paper_status(
    *,
    config,
    snapshot: HealthSnapshot,
    sink: Callable[..., None],
) -> None:
    """Give a status sink only the existing public paper payloads."""

    if config.strategy_kind != "intraday" or not config.intraday.simulation_enabled:
        raise RuntimeError("paper status requires intraday simulation mode")
    paper_store = IntradayPaperStore(
        config.intraday.simulation_db_path,
        _intraday_paper_config(config),
    )
    try:
        summary = paper_store.summary(as_of=snapshot.generated_at)
        days = summary.get("days")
        latest_day = (
            _paper_daily_public_payload(days[-1])
            if isinstance(days, list) and days and isinstance(days[-1], Mapping)
            else None
        )
        sink(
            _paper_month_public_payload(summary),
            planner_ready=snapshot.ready,
            blocker_codes=snapshot.blockers,
            latest_day=latest_day,
        )
    finally:
        paper_store.close()


def _require_shadow_service_config(config) -> None:
    failures = []
    if config.strategy_kind != "intraday":
        failures.append("strategy.kind must be intraday")
    if _runtime_mode(config) != "shadow":
        failures.append("runtime.mode must be shadow")
    if config.toss.live_enabled:
        failures.append("toss.live_enabled must be false")
    if config.intraday.live_execution_enabled:
        failures.append("strategy.intraday.live_execution_enabled must be false")
    if not config.live.emergency_stop:
        failures.append("live.emergency_stop must be true")
    if config.live.allowed_symbols:
        failures.append("live.allowed_symbols must be empty")
    if config.toss.base_url != TOSS_BASE_URL:
        failures.append(f"toss.base_url must be {TOSS_BASE_URL}")
    if failures:
        raise RuntimeError("shadow service hard-lock failed: " + "; ".join(failures))


_EXPECTED_SIMULATION_KEYS = frozenset(
    {"run_id", "start_date", "end_date", "paper_db", "experiment_hash"}
)


def _normalize_expected_simulation(
    value: Mapping[str, str] | None,
) -> dict[str, str] | None:
    """Copy and validate the immutable deployment lock before the service loops."""

    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != _EXPECTED_SIMULATION_KEYS:
        raise ValueError("expected_simulation has an invalid schema")
    normalized = {key: str(value[key]).strip() for key in _EXPECTED_SIMULATION_KEYS}
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", normalized["run_id"]):
        raise ValueError("expected_simulation run_id is invalid")
    try:
        start = date.fromisoformat(normalized["start_date"])
        end = date.fromisoformat(normalized["end_date"])
    except ValueError as exc:
        raise ValueError("expected_simulation dates are invalid") from exc
    if start > end or (end - start).days > 31:
        raise ValueError("expected_simulation window is invalid")
    paper_db = Path(normalized["paper_db"]).expanduser()
    if not paper_db.is_absolute() or paper_db.name != "intraday-paper.sqlite3":
        raise ValueError("expected_simulation paper_db is invalid")
    normalized["paper_db"] = str(paper_db.resolve())
    if not re.fullmatch(r"[0-9a-f]{64}", normalized["experiment_hash"]):
        raise ValueError("expected_simulation experiment_hash is invalid")
    return normalized


def _intraday_account_authority_fingerprint(config) -> str:
    """Bind the planner-only account scope without exposing it to the stream."""

    account_seq = str(config.toss.account_seq or "").strip()
    if not account_seq:
        raise ValueError("planner account authority is incomplete")
    encoded = json.dumps(
        {"account_seq": account_seq},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _require_locked_simulation_config(
    config,
    *,
    expected: Mapping[str, str],
    state_db: str | Path,
    expected_account_fingerprint: str | None = None,
) -> None:
    """Fail closed if a reloaded manifest drifts from the deployed experiment."""

    intraday = config.intraday
    failures: list[str] = []
    if not intraday.simulation_enabled:
        failures.append("strategy.intraday.simulation.enabled must stay true")
    actual = {
        "run_id": str(intraday.simulation_id or ""),
        "start_date": (
            intraday.simulation_start_date.isoformat()
            if isinstance(intraday.simulation_start_date, date)
            else ""
        ),
        "end_date": (
            intraday.simulation_end_date.isoformat()
            if isinstance(intraday.simulation_end_date, date)
            else ""
        ),
        "paper_db": str(
            Path(str(intraday.simulation_db_path or "")).expanduser().resolve()
        ),
        "experiment_hash": intraday_simulation_experiment_hash(config),
    }
    for key in _EXPECTED_SIMULATION_KEYS:
        if actual[key] != expected[key]:
            failures.append(f"locked simulation {key} changed")
    if expected_account_fingerprint is not None:
        try:
            actual_account_fingerprint = _intraday_account_authority_fingerprint(config)
        except ValueError:
            failures.append("planner account authority became incomplete")
        else:
            if actual_account_fingerprint != expected_account_fingerprint:
                failures.append("planner account authority changed")
    configured_state_db = Path(str(config.runtime.state_db or "")).expanduser()
    requested_state_db = Path(state_db).expanduser()
    if (
        not configured_state_db.is_absolute()
        or not requested_state_db.is_absolute()
        or configured_state_db.resolve() != requested_state_db.resolve()
    ):
        failures.append("runtime.state_db does not match the locked planner database")
    configured_context = Path(
        str(config.intraday.news_context_path or "")
    ).expanduser()
    if (
        not configured_context.is_absolute()
        or configured_context.name != "news-context.json"
        or not requested_state_db.is_absolute()
        or configured_context.resolve().parent != requested_state_db.resolve().parent
    ):
        failures.append(
            "strategy.intraday.news_context_path must be locked beside the planner database"
        )
    if failures:
        raise RuntimeError(
            "simulation service hard-lock failed: " + "; ".join(failures)
        )


def _account_alias_from_config_path(config_path: str | Path) -> str:
    try:
        raw = _read_yaml(Path(config_path))
    except Exception:
        return _tailscale_profile_name()
    toss = raw.get("toss", {})
    if not isinstance(toss, Mapping):
        return _tailscale_profile_name()
    return str(toss.get("account_alias") or "").strip() or _tailscale_profile_name()


def _tailscale_profile_name() -> str:
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=2,
            check=False,
        )
    except Exception:
        return ""
    if result.returncode != 0 or not result.stdout:
        return ""
    try:
        payload = json.loads(result.stdout)
    except Exception:
        return ""
    self_info = payload.get("Self", {})
    users = payload.get("User", {})
    user_id = self_info.get("UserID")
    user_info = users.get(str(user_id), {}) if user_id is not None else {}
    for value in (
        user_info.get("DisplayName"),
        user_info.get("LoginName"),
        payload.get("CurrentTailnet", {}).get("Name"),
        self_info.get("HostName"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _paper_service_iteration(
    *,
    config,
    config_path: str | Path,
    store: SQLiteStateStore,
    env: Mapping[str, str],
    transport: TossTransport | None,
    rate_limits: RateLimitQueue | None = None,
    live_consent: Mapping[str, str] | None = None,
    now,
) -> HealthSnapshot:
    account_alias = _account_alias_from_config_path(config_path)
    service_mode = _runtime_mode(config)
    blockers = _paper_service_config_blockers(config, env)
    if service_mode == "live":
        live_consent_blocker = _live_consent_blocker(
            config=config,
            live_consent=live_consent,
        )
        if live_consent_blocker is not None:
            blockers = blockers + (live_consent_blocker,)
    if blockers:
        snapshot = paper_service_health(
            store,
            mode=service_mode,
            blockers=blockers,
            watchlist_name=config.runtime.watchlist_name,
        )
        store.record_runtime_event(
            "WARN",
            f"{service_mode}_service_blocked",
            snapshot.as_payload(),
        )
        return snapshot

    credentials = TossCredentials(
        client_id=env[config.toss.client_id_env],
        client_secret=env[config.toss.client_secret_env],
    )
    client = TossClient(
        credentials=credentials,
        account_seq=config.toss.account_seq,
        base_url=config.toss.base_url or TOSS_BASE_URL,
        transport=transport,
        rate_limits=rate_limits,
        now=now,
    )
    if config.strategy_kind == "intraday":
        return _intraday_shadow_iteration(
            config=config,
            client=client,
            store=store,
            env=env,
            account_alias=account_alias,
            now=now,
        )
    market_data = TossReadOnlyMarketDataProvider(
        client=client,
        config=TossMarketDataConfig(
            candle_interval=config.runtime.candle_interval,
            candle_count=config.runtime.candle_count,
            local_timezone=config.runtime.timezone_name,
            exclude_current_session=config.runtime.exclude_current_session,
        ),
        store=store,
        now=now,
    )
    watchlist_built = False
    selected_symbols: tuple[str, ...] = config.runtime.symbols
    if config.runtime.use_market_calendar:
        session = MarketCalendarGate(
            client=client,
            config=MarketCalendarConfig(
                market=config.runtime.market,
                timezone_name=config.runtime.timezone_name,
                open_session_names=config.runtime.market_calendar_open_sessions,
            ),
            now=now,
        ).current_session()
        store.record_runtime_event(
            "INFO" if session.is_open else "WARN",
            "market_session_state",
            session.as_payload(),
        )
        if _should_build_watchlist(session.status):
            universe = _build_universe(
                config,
                client=client,
                market_data=market_data,
                store=store,
                now=now,
            )
            if universe is not None:
                selected_symbols = universe.symbols()
                if not selected_symbols:
                    snapshot = paper_service_health(
                        store,
                        mode=service_mode,
                        blockers=("universe_empty",),
                        ready=False,
                        watchlist_name=config.runtime.watchlist_name,
                    )
                    store.record_runtime_event(
                        "WARN",
                        f"{service_mode}_service_blocked",
                        snapshot.as_payload() | {"universe": universe.as_payload()},
                    )
                    return snapshot
            _build_and_save_watchlist(
                config,
                market_data=market_data,
                client=None,
                store=store,
                now=now,
                symbols=selected_symbols,
            )
            watchlist_built = True
        if session.blocker is not None:
            snapshot = paper_service_health(
                store,
                mode=service_mode,
                blockers=(session.blocker,),
                ready=False,
                watchlist_name=config.runtime.watchlist_name,
            )
            store.record_runtime_event(
                "WARN",
                f"{service_mode}_service_market_closed",
                snapshot.as_payload() | {"market_session": session.as_payload()},
            )
            return snapshot
    else:
        session = None

    if not watchlist_built:
        universe = _build_universe(
            config,
            client=client,
            market_data=market_data,
            store=store,
            now=now,
        )
        if universe is not None:
            selected_symbols = universe.symbols()
            if not selected_symbols:
                snapshot = paper_service_health(
                    store,
                    mode=service_mode,
                    blockers=("universe_empty",),
                    ready=False,
                    watchlist_name=config.runtime.watchlist_name,
                )
                store.record_runtime_event(
                    "WARN",
                    f"{service_mode}_service_blocked",
                    snapshot.as_payload() | {"universe": universe.as_payload()},
                )
                return snapshot
        _build_and_save_watchlist(
            config,
            market_data=market_data,
            client=None,
            store=store,
            now=now,
            symbols=selected_symbols,
        )
    runtime = PaperTradingRuntime(
        config=PaperRuntimeConfig(
            symbols=selected_symbols,
            mode=service_mode,
            strategy_kind=config.strategy_kind,
            minimum_tick=config.minimum_tick,
            n_method=config.n_method,
            stop_n=config.stop_n,
            max_units_per_symbol=config.max_units_per_symbol,
            pyramid_step_n=config.pyramid_step_n,
            simulate_fills=service_mode != "live",
            require_clean_reconcile=service_mode != "shadow",
            momentum_market_symbol=config.momentum_market_symbol,
            momentum_lookback_days=config.momentum_lookback_days,
            momentum_skip_days=config.momentum_skip_days,
            momentum_trend_ma_days=config.momentum_trend_ma_days,
            momentum_exit_ma_days=config.momentum_exit_ma_days,
            momentum_max_positions=config.momentum_max_positions,
            momentum_accept_top_n=config.momentum_accept_top_n,
            momentum_max_exposure_pct=config.momentum_max_exposure_pct,
            momentum_target_position_pct=config.momentum_target_position_pct,
            momentum_min_price=config.momentum_min_price,
            momentum_min_average_daily_value=config.momentum_min_average_daily_value,
            momentum_average_daily_value_days=config.momentum_average_daily_value_days,
            momentum_use_market_filter=config.momentum_use_market_filter,
        ),
        market_data=market_data,
        position_sync=TossPositionSync(
            client=client,
            store=store,
            sync_live_positions=service_mode == "live",
            sync_closed_orders=service_mode == "live",
        ),
        store=store,
        notifier=MemoryNotifier(),
        now=now,
    )
    result = runtime.run_once()
    if service_mode == "live":
        _submit_live_intents(
            config=config,
            client=client,
            store=store,
            result=result,
            market_open=bool(session is None or session.is_open),
            notifier=DiscordTradeNotifier(env=env),
            account_alias=account_alias,
            live_consent=live_consent,
        )
    return _with_latest_watchlist(
        runtime.health_snapshot(),
        store,
        name=config.runtime.watchlist_name,
    )


def _paper_service_config_blockers(config, env: Mapping[str, str]) -> tuple[str, ...]:
    blockers = [
        check.message
        for check in _build_toss_readiness_checks(config=config, env=env)
        if not check.passed
    ]
    mode = _runtime_mode(config)
    if mode == "live" and not config.live_enabled:
        blockers.append("toss.live_enabled must be true for runtime.mode live")
    if mode != "live" and config.live_enabled:
        blockers.append("toss.live_enabled must be false unless runtime.mode is live")
    if mode == "live" and config.live.emergency_stop:
        blockers.append("live.emergency_stop is active")
    if mode == "live" and not config.live.allowed_symbols:
        blockers.append("live.allowed_symbols is required for runtime.mode live")
    blockers.extend(_intraday_config_blockers(config))
    return tuple(blockers)


def _intraday_shadow_iteration(
    *,
    config,
    client: TossClient,
    store: SQLiteStateStore,
    env: Mapping[str, str],
    account_alias: str,
    now,
) -> HealthSnapshot:
    """Create one immutable premarket plan without constructing an order runtime."""

    checked_at = _intraday_now(now)
    session_date = checked_at.astimezone(ZoneInfo("America/New_York")).date()
    manual_symbol = (
        str(config.runtime.symbols[0]).strip().upper()
        if config.intraday.selection_mode == "manual"
        else None
    )
    symbol = manual_symbol or "AUTO"
    paper_config = (
        _intraday_paper_config(config)
        if config.intraday.simulation_enabled
        else None
    )
    account_key = (
        simulation_account_key(paper_config)
        if paper_config is not None
        else _intraday_account_key(config.toss.account_seq)
    )
    notifier = DiscordTradeNotifier(env=env)
    paper_store: IntradayPaperStore | None = None
    try:
        if paper_config is not None:
            paper_store = IntradayPaperStore(
                config.intraday.simulation_db_path,
                paper_config,
            )
            _reconcile_intraday_paper_backlog(
                paper_store=paper_store,
                store=store,
                at=checked_at,
            )
            if session_date < paper_config.start_date:
                return HealthSnapshot(
                    mode="shadow",
                    ready=False,
                    blockers=("intraday_simulation_not_started",),
                    generated_at=checked_at,
                )
            if session_date > paper_config.end_date:
                summary = paper_store.summary(as_of=checked_at)
                summary_status = str(summary.get("status") or "INCOMPLETE")
                store.enqueue_notification_once(
                    notification_key=(
                        f"intraday-paper-run:{paper_config.run_id}:{summary_status}"
                    ),
                    message="intraday_paper_run_report",
                    level=(
                        "info" if summary_status == "COMPLETE" else "warn"
                    ),
                    payload=_paper_month_public_payload(summary),
                    created_at=checked_at,
                )
                _forward_intraday_paper_alerts(
                    paper_store=paper_store,
                    store=store,
                    at=checked_at,
                )
                _drain_intraday_notifications(
                    store=store,
                    notifier=notifier,
                    at=checked_at,
                )
                return HealthSnapshot(
                    mode="shadow",
                    ready=False,
                    blockers=(
                        "intraday_simulation_complete"
                        if summary_status == "COMPLETE"
                        else "intraday_simulation_incomplete",
                    ),
                    generated_at=checked_at,
                )
        calendar_payload = client.get_market_calendar(
            "US",
            date=session_date.isoformat(),
        )
        schedule = _strict_intraday_schedule(
            calendar_payload,
            expected_date=session_date,
        )
        existing = store.load_intraday_plan(
            account_key=account_key,
            session_date=schedule.session_date,
        )
        if existing is not None:
            locked_symbol = str(existing.get("symbol") or "").strip().upper()
            if not _INTRADAY_SYMBOL_PATTERN.fullmatch(locked_symbol):
                raise IntradayPlanBlocked(
                    "intraday_plan_integrity_failure",
                    "stored plan symbol is invalid",
                )
            _assert_intraday_plan_matches_config(
                existing,
                config=config,
                symbol=manual_symbol or locked_symbol,
            )
            symbol = locked_symbol
            _refresh_intraday_news_context(
                config=config,
                record=existing,
                store=store,
                at=checked_at,
                current_regular_close=schedule.regular_close,
            )
            _refresh_intraday_approval_envelope(
                config=config,
                record=existing,
                store=store,
                account_alias=account_alias,
                at=checked_at,
                current_regular_open=schedule.regular_open,
            )
            _ensure_intraday_plan_notification(
                store=store,
                record=existing,
                account_alias=account_alias,
            )
            if paper_store is not None:
                _sync_intraday_paper_plan(
                    paper_store=paper_store,
                    store=store,
                    record=existing,
                    at=checked_at,
                    regular_close=schedule.regular_close,
                )
            _drain_intraday_notifications(
                store=store,
                notifier=notifier,
                at=checked_at,
            )
            return _intraday_existing_plan_snapshot(
                existing,
                schedule=schedule,
                checked_at=checked_at,
                simulation_enabled=paper_store is not None,
            )

        earliest = schedule.regular_open - timedelta(
            minutes=config.intraday.plan_lead_minutes
        )
        deadline = schedule.regular_open - timedelta(
            minutes=config.intraday.minimum_plan_lead_minutes
        )
        if checked_at < earliest:
            return HealthSnapshot(
                mode="shadow",
                ready=False,
                blockers=("intraday_plan_window_not_started",),
                generated_at=checked_at,
            )
        if checked_at > deadline:
            raise IntradayPlanBlocked(
                "intraday_plan_deadline_missed",
                "the immutable premarket plan deadline has passed",
            )
        if not schedule.premarket_open <= checked_at < schedule.premarket_close:
            raise IntradayPlanBlocked(
                "intraday_not_in_premarket",
                "plan creation is allowed only during the official US premarket",
            )

        if paper_store is None:
            _assert_intraday_account_clear(client)
            cash_payload = client.get_buying_power("USD")
            cash_captured_at = _intraday_now(now)
            available_cash = _strict_intraday_cash(cash_payload)
        else:
            paper_store.assert_ready(schedule.session_date)
            available_cash = paper_store.current_cash()
            cash_captured_at = _intraday_now(now)
        commission_rate = _strict_intraday_commission(
            client.get_commissions(),
            session_date=schedule.session_date,
        )
        configured_cost = config.intraday.estimated_round_trip_cost_fraction
        if configured_cost < commission_rate * Decimal("2"):
            raise IntradayPlanBlocked(
                "intraday_cost_buffer_below_commission",
                "estimated round-trip cost must cover at least both broker commission legs",
            )

        if config.intraday.selection_mode == "automatic":
            (
                symbol,
                market,
                plan,
                selection_snapshot,
                available_cash,
                cash_captured_at,
                captured_at,
            ) = _select_automatic_intraday_plan(
                config=config,
                client=client,
                store=store,
                account_key=account_key,
                schedule=schedule,
                configured_cost=configured_cost,
                now=now,
                simulation_available_cash=(
                    available_cash if paper_store is not None else None
                ),
            )
        else:
            prices_payload = client.get_prices((symbol,))
            orderbook_payload = client.get_orderbook(symbol)
            captured_at = _intraday_now(now)
            market = _strict_intraday_market_snapshot(
                prices_payload=prices_payload,
                orderbook_payload=orderbook_payload,
                symbol=symbol,
                captured_at=captured_at,
                schedule=schedule,
                quote_max_age_seconds=config.intraday.quote_max_age_seconds,
                orderbook_max_age_seconds=config.intraday.orderbook_max_age_seconds,
                max_quote_skew_seconds=config.intraday.max_quote_skew_seconds,
                max_spread_fraction=config.intraday.max_spread_fraction,
                max_last_mid_deviation_fraction=(
                    config.intraday.max_last_mid_deviation_fraction
                ),
            )
            try:
                plan = _build_configured_intraday_plan(
                    config=config,
                    account_key=account_key,
                    schedule=schedule,
                    symbol=symbol,
                    market=market,
                    available_cash=available_cash,
                    configured_cost=configured_cost,
                    captured_at=captured_at,
                )
            except (TypeError, ValueError) as exc:
                raise IntradayPlanBlocked("intraday_plan_invalid", str(exc)) from exc
            selection_snapshot = {
                "mode": "manual",
                "source": "runtime.symbols",
                "symbol": symbol,
                "news_or_llm_influence": False,
            }
        if config.intraday.selection_mode == "automatic":
            if not _strict_intraday_warning_clear(
                client.get_stock_warnings(symbol)
            ):
                raise IntradayPlanBlocked(
                    "intraday_stock_warning_changed",
                    "stock warnings changed before the daily plan could be locked",
                )
            selection_snapshot["warnings_checked_at"] = _intraday_now(now).isoformat()
            if paper_store is None:
                _assert_intraday_account_clear(client)
                selection_snapshot["account_checked_at"] = _intraday_now(now).isoformat()
                available_cash = _strict_intraday_cash(client.get_buying_power("USD"))
                cash_captured_at = _intraday_now(now)
            else:
                selection_snapshot["cash_source"] = "virtual_usd_ledger"
                selection_snapshot["account_checked_at"] = _intraday_now(now).isoformat()
                available_cash = paper_store.current_cash()
                cash_captured_at = _intraday_now(now)
            captured_at = cash_captured_at
            try:
                plan = _build_configured_intraday_plan(
                    config=config,
                    account_key=account_key,
                    schedule=schedule,
                    symbol=symbol,
                    market=market,
                    available_cash=available_cash,
                    configured_cost=configured_cost,
                    captured_at=captured_at,
                )
            except (TypeError, ValueError) as exc:
                raise IntradayPlanBlocked("intraday_plan_invalid", str(exc)) from exc
        if captured_at > deadline:
            raise IntradayPlanBlocked(
                "intraday_plan_deadline_missed_during_reads",
                "broker reads completed after the immutable plan deadline",
            )
        cash_age = (captured_at - cash_captured_at).total_seconds()
        if cash_age < 0 or cash_age > min(
            config.intraday.quote_max_age_seconds,
            config.intraday.orderbook_max_age_seconds,
        ):
            raise IntradayPlanBlocked(
                "intraday_cash_snapshot_stale",
                "cash buying power became stale before plan creation completed",
            )
        payload = intraday_plan_payload(plan)
        payload.update(
            {
                "mode": "shadow",
                "status": (
                    "PAPER_PLANNED"
                    if paper_store is not None
                    else "SHADOW_PLANNED"
                ),
                "live_order_submission": False,
                "quantity_is_shadow_maximum": True,
                "llm_influence": False,
                "selection_snapshot": selection_snapshot,
                "cash_snapshot": {
                    "currency": "USD",
                    "cash_buying_power": _decimal_text(available_cash),
                    "captured_at": cash_captured_at.isoformat(),
                    "source": (
                        "virtual_usd_ledger"
                        if paper_store is not None
                        else "toss_cash_buying_power"
                    ),
                },
                "commission_snapshot": {
                    "market_country": "US",
                    "broker_commission_fraction": _decimal_text(commission_rate),
                    "configured_round_trip_cost_fraction": _decimal_text(
                        configured_cost
                    ),
                    "configured_fixed_round_trip_cost": _decimal_text(
                        config.intraday.estimated_fixed_round_trip_cost
                    ),
                },
                "market_snapshot": {
                    key: value.isoformat() if isinstance(value, datetime) else _decimal_text(value)
                    if isinstance(value, Decimal)
                    else value
                    for key, value in market.items()
                },
                "guardrails": _intraday_guardrails(config),
            }
        )
        public_payload = _intraday_public_plan_payload(
            payload,
            account_alias=account_alias,
        )
        notification = _intraday_notification(
            key=_intraday_plan_notification_key(
                account_key=account_key,
                session_date=schedule.session_date,
            ),
            message=(
                "intraday_paper_plan_created"
                if config.intraday.simulation_enabled
                else "intraday_shadow_plan_created"
            ),
            level="info",
            payload=public_payload,
        )
        lock_at = _intraday_now(now)
        if lock_at > deadline:
            raise IntradayPlanBlocked(
                "intraday_plan_deadline_missed_before_lock",
                "the immutable plan deadline passed before the database lock",
            )
        _validate_intraday_prelock_freshness(
            config=config,
            schedule=schedule,
            market=market,
            cash_captured_at=cash_captured_at,
            selection_snapshot=selection_snapshot,
            lock_at=lock_at,
        )
        try:
            stored, inserted = store.save_intraday_plan_once(
                account_key=account_key,
                session_date=schedule.session_date,
                symbol=symbol,
                payload=payload,
                created_at=captured_at,
                notification=notification,
            )
        except ValueError as exc:
            winner = store.load_intraday_plan(
                account_key=account_key,
                session_date=schedule.session_date,
            )
            if winner is None:
                raise IntradayPlanBlocked("intraday_plan_lock_failed", str(exc)) from exc
            winner_symbol = str(winner.get("symbol") or "").strip().upper()
            _assert_intraday_plan_matches_config(
                winner,
                config=config,
                symbol=manual_symbol or winner_symbol,
            )
            _refresh_intraday_news_context(
                config=config,
                record=winner,
                store=store,
                at=captured_at,
                current_regular_close=schedule.regular_close,
            )
            _refresh_intraday_approval_envelope(
                config=config,
                record=winner,
                store=store,
                account_alias=account_alias,
                at=captured_at,
                current_regular_open=schedule.regular_open,
            )
            _ensure_intraday_plan_notification(
                store=store,
                record=winner,
                account_alias=account_alias,
            )
            if paper_store is not None:
                _sync_intraday_paper_plan(
                    paper_store=paper_store,
                    store=store,
                    record=winner,
                    at=captured_at,
                    regular_close=schedule.regular_close,
                )
            _drain_intraday_notifications(
                store=store,
                notifier=notifier,
                at=captured_at,
            )
            return _intraday_existing_plan_snapshot(
                winner,
                schedule=schedule,
                checked_at=captured_at,
                simulation_enabled=paper_store is not None,
            )

        _refresh_intraday_news_context(
            config=config,
            record=stored,
            store=store,
            at=captured_at,
            current_regular_close=schedule.regular_close,
        )
        _refresh_intraday_approval_envelope(
            config=config,
            record=stored,
            store=store,
            account_alias=account_alias,
            at=captured_at,
            current_regular_open=schedule.regular_open,
        )
        if inserted:
            store.record_runtime_event(
                "INFO",
                (
                    "intraday_paper_plan_created"
                    if paper_store is not None
                    else "intraday_shadow_plan_created"
                ),
                public_payload,
            )
        if paper_store is not None:
            _sync_intraday_paper_plan(
                paper_store=paper_store,
                store=store,
                record=stored,
                at=captured_at,
                regular_close=schedule.regular_close,
            )
        _drain_intraday_notifications(
            store=store,
            notifier=notifier,
            at=captured_at,
        )
        return HealthSnapshot(
            mode="shadow",
            ready=True,
            blockers=(),
            generated_at=captured_at,
        )
    except IntradayPlanBlocked as exc:
        if (
            exc.code == "intraday_market_holiday"
            and paper_store is not None
            and session_date.weekday() < 5
        ):
            paper_store.record_market_closed(
                session_date,
                recorded_at=checked_at,
            )
        _record_intraday_blocker_once(
            store=store,
            notifier=notifier,
            code=exc.code,
            diagnostic=_intraday_exception_diagnostic(exc),
            session_date=session_date,
            symbol=symbol,
            account_key=account_key,
            account_alias=account_alias,
            at=checked_at,
        )
        return HealthSnapshot(
            mode="shadow",
            ready=False,
            blockers=(exc.code,),
            generated_at=checked_at,
        )
    except PaperSimulationBlocked as exc:
        code = "intraday_simulation_blocked"
        _record_intraday_blocker_once(
            store=store,
            notifier=notifier,
            code=code,
            diagnostic={"error_type": exc.__class__.__name__},
            session_date=session_date,
            symbol=symbol,
            account_key=account_key,
            account_alias=account_alias,
            at=checked_at,
        )
        return HealthSnapshot(
            mode="shadow",
            ready=False,
            blockers=(code,),
            generated_at=checked_at,
        )
    except PaperSimulationError as exc:
        code = "intraday_simulation_integrity_failure"
        _record_intraday_blocker_once(
            store=store,
            notifier=notifier,
            code=code,
            diagnostic={"error_type": exc.__class__.__name__},
            session_date=session_date,
            symbol=symbol,
            account_key=account_key,
            account_alias=account_alias,
            at=checked_at,
        )
        return HealthSnapshot(
            mode="shadow",
            ready=False,
            blockers=(code,),
            generated_at=checked_at,
        )
    except Exception as exc:
        code = "intraday_read_or_integrity_failure"
        _record_intraday_blocker_once(
            store=store,
            notifier=notifier,
            code=code,
            diagnostic=_intraday_exception_diagnostic(exc),
            session_date=session_date,
            symbol=symbol,
            account_key=account_key,
            account_alias=account_alias,
            at=checked_at,
        )
        return HealthSnapshot(
            mode="shadow",
            ready=False,
            blockers=(code,),
            generated_at=checked_at,
        )
    finally:
        if paper_store is not None:
            paper_store.close()


def _strict_intraday_schedule(
    payload: Any,
    *,
    expected_date: date,
) -> _IntradaySchedule:
    if not isinstance(payload, Mapping):
        raise IntradayPlanBlocked("intraday_calendar_malformed", "calendar result is not an object")
    today = payload.get("today")
    if not isinstance(today, Mapping):
        raise IntradayPlanBlocked("intraday_calendar_malformed", "calendar today is missing")
    try:
        session_date = date.fromisoformat(str(today.get("date") or ""))
    except ValueError as exc:
        raise IntradayPlanBlocked(
            "intraday_calendar_malformed", "calendar today.date is invalid"
        ) from exc
    if session_date != expected_date:
        raise IntradayPlanBlocked(
            "intraday_calendar_date_mismatch",
            "calendar today.date does not match the US local date",
        )
    if "preMarket" not in today or "regularMarket" not in today:
        raise IntradayPlanBlocked(
            "intraday_calendar_malformed",
            "calendar today must explicitly include both market session fields",
        )
    premarket = today["preMarket"]
    regular = today["regularMarket"]
    if premarket is None and regular is None:
        raise IntradayPlanBlocked("intraday_market_holiday", "US market is closed today")
    if not isinstance(premarket, Mapping) or not isinstance(regular, Mapping):
        raise IntradayPlanBlocked(
            "intraday_required_session_unavailable",
            "both preMarket and regularMarket sessions are required",
        )
    premarket_open = _strict_aware_datetime(
        "preMarket.startTime", premarket.get("startTime")
    )
    premarket_close = _strict_aware_datetime(
        "preMarket.endTime", premarket.get("endTime")
    )
    regular_open = _strict_aware_datetime(
        "regularMarket.startTime", regular.get("startTime")
    )
    regular_close = _strict_aware_datetime(
        "regularMarket.endTime", regular.get("endTime")
    )
    if not premarket_open < premarket_close <= regular_open < regular_close:
        raise IntradayPlanBlocked(
            "intraday_calendar_malformed", "official session times are misordered"
        )
    if regular_open.date() != session_date:
        raise IntradayPlanBlocked(
            "intraday_calendar_malformed",
            "regularMarket start date does not match today.date",
        )
    return _IntradaySchedule(
        session_date=session_date,
        premarket_open=premarket_open,
        premarket_close=premarket_close,
        regular_open=regular_open,
        regular_close=regular_close,
    )


def _assert_intraday_account_clear(client: TossClient) -> None:
    holdings = client.get_holdings()
    if not isinstance(holdings, Mapping) or not isinstance(holdings.get("items"), list):
        raise IntradayPlanBlocked(
            "intraday_holdings_malformed", "holdings response did not include items"
        )
    for item in holdings["items"]:
        if not isinstance(item, Mapping) or "quantity" not in item:
            raise IntradayPlanBlocked(
                "intraday_holdings_malformed", "holding item is malformed"
            )
        quantity = _strict_positive_or_zero_decimal("holding.quantity", item["quantity"])
        if quantity > 0:
            raise IntradayPlanBlocked(
                "intraday_account_not_flat",
                "the account has an existing holding; no intraday plan was created",
            )

    orders = client.get_orders(status="OPEN", limit=100)
    if (
        not isinstance(orders, Mapping)
        or not isinstance(orders.get("orders"), list)
        or not isinstance(orders.get("hasNext"), bool)
    ):
        raise IntradayPlanBlocked(
            "intraday_orders_malformed",
            "open-order response must include orders and boolean hasNext",
        )
    if orders["orders"] or orders["hasNext"]:
        raise IntradayPlanBlocked(
            "intraday_open_order_exists",
            "the account has an unresolved general order",
        )

    conditional = TossConditionalOrderAdapter(client).list(status="OPEN", limit=100)
    if conditional["conditionalOrders"] or conditional.get("hasNext") is True:
        raise IntradayPlanBlocked(
            "intraday_conditional_order_exists",
            "the account has an open conditional order",
        )


def _strict_intraday_cash(payload: Any) -> Decimal:
    if not isinstance(payload, Mapping):
        raise IntradayPlanBlocked("intraday_buying_power_malformed", "buying power is not an object")
    if payload.get("currency") != "USD" or "cashBuyingPower" not in payload:
        raise IntradayPlanBlocked(
            "intraday_buying_power_malformed",
            "buying power must contain USD cashBuyingPower",
        )
    cash = _strict_decimal("cashBuyingPower", payload["cashBuyingPower"])
    if cash <= 0:
        raise IntradayPlanBlocked(
            "intraday_no_cash_buying_power", "USD cashBuyingPower must be positive"
        )
    return cash


def _strict_intraday_commission(payload: Any, *, session_date: date) -> Decimal:
    if not isinstance(payload, (list, tuple)):
        raise IntradayPlanBlocked(
            "intraday_commission_malformed", "commission response is not an array"
        )
    rates: list[Decimal] = []
    for item in payload:
        if not isinstance(item, Mapping):
            raise IntradayPlanBlocked(
                "intraday_commission_malformed", "commission item is malformed"
            )
        if str(item.get("marketCountry") or "").upper() != "US":
            continue
        start = _optional_iso_date("commission.startDate", item.get("startDate"))
        end = _optional_iso_date("commission.endDate", item.get("endDate"))
        if (start is not None and session_date < start) or (
            end is not None and session_date > end
        ):
            continue
        rate = _strict_positive_or_zero_decimal(
            "commissionRate", item.get("commissionRate")
        )
        rates.append(rate)
    if not rates:
        raise IntradayPlanBlocked(
            "intraday_us_commission_unavailable",
            "no active US commission rate was returned",
        )
    return max(rates)


def _strict_intraday_market_snapshot(
    *,
    prices_payload: Any,
    orderbook_payload: Any,
    symbol: str,
    captured_at: datetime,
    schedule: _IntradaySchedule,
    quote_max_age_seconds: int,
    orderbook_max_age_seconds: int,
    max_quote_skew_seconds: int,
    max_spread_fraction: Decimal,
    max_last_mid_deviation_fraction: Decimal,
) -> dict[str, Any]:
    if not isinstance(prices_payload, (list, tuple)):
        raise IntradayPlanBlocked("intraday_quote_malformed", "prices result is not an array")
    if len(prices_payload) != 1 or not isinstance(prices_payload[0], Mapping):
        raise IntradayPlanBlocked(
            "intraday_quote_malformed", "prices must contain exactly one object"
        )
    matches = [
        item
        for item in prices_payload
        if isinstance(item, Mapping)
        and str(item.get("symbol") or "").strip().upper() == symbol
    ]
    if len(matches) != 1:
        raise IntradayPlanBlocked(
            "intraday_quote_malformed", "prices must contain exactly one matching symbol"
        )
    price = matches[0]
    if price.get("currency") != "USD":
        raise IntradayPlanBlocked("intraday_quote_currency_mismatch", "price currency must be USD")
    last_price = _strict_decimal("lastPrice", price.get("lastPrice"))
    price_at = _strict_aware_datetime("price.timestamp", price.get("timestamp"))

    if not isinstance(orderbook_payload, Mapping):
        raise IntradayPlanBlocked("intraday_orderbook_malformed", "orderbook is not an object")
    if orderbook_payload.get("currency") != "USD":
        raise IntradayPlanBlocked(
            "intraday_orderbook_currency_mismatch", "orderbook currency must be USD"
        )
    orderbook_at = _strict_aware_datetime(
        "orderbook.timestamp", orderbook_payload.get("timestamp")
    )
    best_bid, best_bid_volume = _best_intraday_level(
        orderbook_payload.get("bids"), side="bid"
    )
    best_ask, best_ask_volume = _best_intraday_level(
        orderbook_payload.get("asks"), side="ask"
    )
    if best_bid >= best_ask:
        raise IntradayPlanBlocked(
            "intraday_orderbook_crossed", "best bid must be lower than best ask"
        )

    _validate_intraday_timestamp(
        "price",
        price_at,
        captured_at=captured_at,
        max_age_seconds=quote_max_age_seconds,
        schedule=schedule,
    )
    _validate_intraday_timestamp(
        "orderbook",
        orderbook_at,
        captured_at=captured_at,
        max_age_seconds=orderbook_max_age_seconds,
        schedule=schedule,
    )
    if abs((price_at - orderbook_at).total_seconds()) > max_quote_skew_seconds:
        raise IntradayPlanBlocked(
            "intraday_quote_orderbook_skew",
            "price and orderbook timestamps are too far apart",
        )

    midpoint = (best_bid + best_ask) / Decimal("2")
    spread_fraction = (best_ask - best_bid) / midpoint
    if spread_fraction > max_spread_fraction:
        raise IntradayPlanBlocked(
            "intraday_spread_too_wide", "premarket spread exceeds the configured maximum"
        )
    last_mid_deviation = abs(last_price - midpoint) / midpoint
    if last_mid_deviation > max_last_mid_deviation_fraction:
        raise IntradayPlanBlocked(
            "intraday_last_mid_deviation",
            "last price is too far from the current orderbook midpoint",
        )
    return {
        "currency": "USD",
        "captured_at": captured_at,
        "price_at": price_at,
        "orderbook_at": orderbook_at,
        "reference_at": max(price_at, orderbook_at),
        "last_price": last_price,
        "best_bid": best_bid,
        "best_bid_volume": best_bid_volume,
        "best_ask": best_ask,
        "best_ask_volume": best_ask_volume,
        "midpoint": midpoint,
        "spread_fraction": spread_fraction,
        "last_mid_deviation_fraction": last_mid_deviation,
        "reference_price": max(last_price, best_ask),
    }


def _build_configured_intraday_plan(
    *,
    config,
    account_key: str,
    schedule: _IntradaySchedule,
    symbol: str,
    market: Mapping[str, Any],
    available_cash: Decimal,
    configured_cost: Decimal,
    captured_at: datetime,
):
    return build_intraday_plan(
        account_id=account_key,
        session_date=schedule.session_date,
        symbol=symbol,
        reference_at=market["reference_at"],
        created_at=captured_at,
        regular_open=schedule.regular_open,
        regular_close=schedule.regular_close,
        entry_start_minutes_after_open=(
            config.intraday.entry_start_minutes_after_open
        ),
        entry_expiry_minutes_after_open=(
            config.intraday.entry_expiry_minutes_after_open
        ),
        force_exit_minutes_before_close=(
            config.intraday.force_exit_minutes_before_close
        ),
        available_cash=available_cash,
        reference_price=market["reference_price"],
        cash_allocation_fraction=config.intraday.cash_allocation_fraction,
        risk_fraction=config.intraday.risk_fraction,
        take_profit_fraction=config.intraday.take_profit_fraction,
        stop_fraction=config.intraday.stop_fraction,
        stop_limit_buffer_fraction=config.intraday.stop_limit_buffer_fraction,
        max_entry_slippage_fraction=config.intraday.max_entry_slippage_fraction,
        estimated_round_trip_cost_fraction=configured_cost,
        estimated_fixed_round_trip_cost=(
            config.intraday.estimated_fixed_round_trip_cost
        ),
        minimum_reward_risk_ratio=config.intraday.minimum_reward_risk_ratio,
        max_quantity=config.intraday.max_quantity,
        max_notional=config.intraday.max_notional,
    )


def _select_automatic_intraday_plan(
    *,
    config,
    client: TossClient,
    store: SQLiteStateStore,
    account_key: str,
    schedule: _IntradaySchedule,
    configured_cost: Decimal,
    now,
    simulation_available_cash: Decimal | None = None,
):
    ranking_payload = client.get_rankings(
        ranking_type="MARKET_TRADING_AMOUNT",
        market_country="US",
        duration="realtime",
        exclude_investment_caution=True,
        count=_INTRADAY_RANKING_COUNT,
    )
    ranked_at, candidates = _strict_intraday_ranking_candidates(
        ranking_payload,
        captured_at=_intraday_now(now),
        schedule=schedule,
        max_age_seconds=config.intraday.selection_rank_max_age_seconds,
        min_price=config.intraday.selection_min_price,
        min_trading_amount=config.intraday.selection_min_trading_amount,
        min_change_fraction=config.intraday.selection_min_change_fraction,
        max_change_fraction=config.intraday.selection_max_change_fraction,
    )
    if not candidates:
        raise IntradayPlanBlocked(
            "intraday_no_eligible_candidate",
            "no ranking candidate passed the automatic selection thresholds",
        )

    tradeable_markets = _intraday_tradeable_us_stock_symbols(
        client=client,
        store=store,
        session_date=schedule.session_date,
        now=now,
    )
    review = [
        candidate
        for candidate in candidates
        if candidate["symbol"] in tradeable_markets
    ][:_INTRADAY_CANDIDATE_REVIEW_LIMIT]
    if not review:
        raise IntradayPlanBlocked(
            "intraday_no_eligible_candidate",
            "no ranking candidate belongs to the current tradeable US universe",
        )
    details = _strict_intraday_stock_details(
        client.get_stocks(tuple(item["symbol"] for item in review)),
        requested_symbols={item["symbol"] for item in review},
    )
    for candidate in review:
        symbol = candidate["symbol"]
        stock = details.get(symbol)
        if stock is None:
            raise IntradayPlanBlocked(
                "intraday_stock_info_malformed",
                "stock details omitted a reviewed symbol",
            )
        if stock.get("market") != tradeable_markets[symbol]:
            raise IntradayPlanBlocked(
                "intraday_stock_info_malformed",
                "stock detail market disagrees with the tradeable universe source",
            )
        if not _eligible_intraday_stock(stock):
            continue
        if not _strict_intraday_warning_clear(client.get_stock_warnings(symbol)):
            continue

        daily_page = client.get_candles(
            symbol,
            interval="1d",
            count=_INTRADAY_DAILY_CANDLE_COUNT + 1,
            before=schedule.regular_open,
            adjusted=False,
        )
        daily = _strict_intraday_daily_metrics(
            daily_page.candles,
            symbol=symbol,
            session_date=schedule.session_date,
            min_average_daily_value=(
                config.intraday.selection_min_average_daily_value
            ),
            max_average_daily_range_fraction=(
                config.intraday.selection_max_average_daily_range_fraction
            ),
        )
        if daily is None:
            continue

        minute_captured_at = _intraday_now(now)
        minute_candles = _fetch_intraday_premarket_candles(
            client,
            symbol=symbol,
            before=minute_captured_at,
            premarket_open=schedule.premarket_open,
        )
        premarket = _strict_intraday_premarket_metrics(
            minute_candles,
            symbol=symbol,
            captured_at=_intraday_now(now),
            schedule=schedule,
            max_age_seconds=config.intraday.selection_rank_max_age_seconds,
            max_range_fraction=(
                config.intraday.selection_max_premarket_range_fraction
            ),
        )
        if premarket is None:
            continue

        if simulation_available_cash is None:
            _assert_intraday_account_clear(client)
            refreshed_cash_payload = client.get_buying_power("USD")
            refreshed_cash_at = _intraday_now(now)
            refreshed_cash = _strict_intraday_cash(refreshed_cash_payload)
        else:
            refreshed_cash = simulation_available_cash
            refreshed_cash_at = _intraday_now(now)
        prices_payload = client.get_prices((symbol,))
        orderbook_payload = client.get_orderbook(symbol)
        captured_at = _intraday_now(now)
        cash_age = (captured_at - refreshed_cash_at).total_seconds()
        if cash_age < 0 or cash_age > min(
            config.intraday.quote_max_age_seconds,
            config.intraday.orderbook_max_age_seconds,
        ):
            raise IntradayPlanBlocked(
                "intraday_cash_snapshot_stale",
                "refreshed cash buying power became stale during final market reads",
            )
        try:
            market = _strict_intraday_market_snapshot(
                prices_payload=prices_payload,
                orderbook_payload=orderbook_payload,
                symbol=symbol,
                captured_at=captured_at,
                schedule=schedule,
                quote_max_age_seconds=config.intraday.quote_max_age_seconds,
                orderbook_max_age_seconds=config.intraday.orderbook_max_age_seconds,
                max_quote_skew_seconds=config.intraday.max_quote_skew_seconds,
                max_spread_fraction=config.intraday.max_spread_fraction,
                max_last_mid_deviation_fraction=(
                    config.intraday.max_last_mid_deviation_fraction
                ),
            )
        except IntradayPlanBlocked as exc:
            if exc.code in {
                "intraday_price_stale",
                "intraday_orderbook_stale",
                "intraday_orderbook_empty",
                "intraday_orderbook_crossed",
                "intraday_spread_too_wide",
                "intraday_last_mid_deviation",
            }:
                continue
            raise
        _validate_intraday_timestamp(
            "ranking",
            ranked_at,
            captured_at=captured_at,
            max_age_seconds=config.intraday.selection_rank_max_age_seconds,
            schedule=schedule,
        )
        final_change_fraction = (
            market["last_price"] - candidate["base_price"]
        ) / candidate["base_price"]
        if (
            market["last_price"] < config.intraday.selection_min_price
            or final_change_fraction
            < config.intraday.selection_min_change_fraction
            or final_change_fraction
            > config.intraday.selection_max_change_fraction
        ):
            continue
        try:
            plan = _build_configured_intraday_plan(
                config=config,
                account_key=account_key,
                schedule=schedule,
                symbol=symbol,
                market=market,
                available_cash=refreshed_cash,
                configured_cost=configured_cost,
                captured_at=captured_at,
            )
        except (TypeError, ValueError):
            continue

        selection_snapshot = {
            "mode": "automatic",
            "source": "MARKET_TRADING_AMOUNT:US:realtime",
            "ranked_at": ranked_at.isoformat(),
            "rank": candidate["rank"],
            "symbol": symbol,
            "ranking_last_price": _decimal_text(candidate["last_price"]),
            "ranking_base_price": _decimal_text(candidate["base_price"]),
            "ranking_change_fraction": _decimal_text(
                candidate["change_fraction"]
            ),
            "final_change_fraction": _decimal_text(final_change_fraction),
            "ranking_trading_volume": _decimal_text(candidate["trading_volume"]),
            "ranking_trading_amount": _decimal_text(candidate["trading_amount"]),
            "market": stock["market"],
            "warnings_clear": True,
            "candles_adjusted": False,
            "openapi_version": _INTRADAY_SELECTOR_OPENAPI_VERSION,
            "openapi_sha256": _INTRADAY_SELECTOR_OPENAPI_SHA256,
            **daily,
            **premarket,
            "news_or_llm_influence": False,
        }
        return (
            symbol,
            market,
            plan,
            selection_snapshot,
            refreshed_cash,
            refreshed_cash_at,
            captured_at,
        )

    raise IntradayPlanBlocked(
        "intraday_no_eligible_candidate",
        "no reviewed ranking candidate passed all automatic selection checks",
    )


def _intraday_tradeable_us_stock_symbols(
    *,
    client: TossClient,
    store: SQLiteStateStore,
    session_date: date,
    now,
) -> dict[str, str]:
    symbols: dict[str, str] = {}
    # ponytail: process-local pacing plus fail-closed 429 handling is sufficient
    # for shadow; persist a throttle timestamp only if cold-restart evidence needs it.
    last_request_at: float | None = None
    for market in ("NASDAQ", "NYSE", "AMEX"):
        cached = store.latest_market_data_snapshot("intraday_stock_universe", market)
        market_symbols = _cached_intraday_stock_universe(
            cached,
            market=market,
            session_date=session_date,
        )
        if market_symbols is None:
            if last_request_at is not None:
                remaining = _INTRADAY_STOCK_ALL_MIN_INTERVAL_SECONDS - (
                    time.monotonic() - last_request_at
                )
                if remaining > 0:
                    time.sleep(remaining)
            response = client.list_stocks(
                market,
                status="ACTIVE",
                security_type="STOCK",
                common_share=True,
            )
            last_request_at = time.monotonic()
            market_symbols = _strict_intraday_tradeable_stock_list(
                response,
                market=market,
            )
            store.record_market_data_snapshot(
                "intraday_stock_universe",
                market,
                {
                    "schema_version": 1,
                    "session_date": session_date.isoformat(),
                    "market": market,
                    "symbols": sorted(market_symbols),
                },
                captured_at=_intraday_now(now),
            )
        overlap = set(symbols).intersection(market_symbols)
        if overlap:
            raise IntradayPlanBlocked(
                "intraday_stock_universe_malformed",
                "stock universe symbols appeared in more than one requested market",
            )
        symbols.update({symbol: market for symbol in market_symbols})
    if not symbols:
        raise IntradayPlanBlocked(
            "intraday_stock_universe_empty", "tradeable US stock universe is empty"
        )
    return symbols


def _cached_intraday_stock_universe(
    payload: Any,
    *,
    market: str,
    session_date: date,
) -> set[str] | None:
    if payload is None:
        return None
    if not isinstance(payload, Mapping) or set(payload) != {
        "schema_version",
        "session_date",
        "market",
        "symbols",
    }:
        raise IntradayPlanBlocked(
            "intraday_stock_universe_malformed", "cached stock universe is malformed"
        )
    if payload.get("schema_version") != 1 or payload.get("market") != market:
        raise IntradayPlanBlocked(
            "intraday_stock_universe_malformed", "cached stock universe identity is invalid"
        )
    if payload.get("session_date") != session_date.isoformat():
        return None
    raw_symbols = payload.get("symbols")
    if not isinstance(raw_symbols, list):
        raise IntradayPlanBlocked(
            "intraday_stock_universe_malformed", "cached stock symbols are malformed"
        )
    symbols = {str(symbol).strip().upper() for symbol in raw_symbols}
    if (
        len(symbols) != len(raw_symbols)
        or not symbols
        or any(not _INTRADAY_SYMBOL_PATTERN.fullmatch(symbol) for symbol in symbols)
    ):
        raise IntradayPlanBlocked(
            "intraday_stock_universe_malformed", "cached stock symbols are invalid"
        )
    return symbols


def _strict_intraday_tradeable_stock_list(
    payload: Any,
    *,
    market: str,
) -> set[str]:
    if not isinstance(payload, list):
        raise IntradayPlanBlocked(
            "intraday_stock_universe_malformed", "stock universe is not an array"
        )
    symbols: set[str] = set()
    for item in payload:
        if not isinstance(item, Mapping):
            raise IntradayPlanBlocked(
                "intraday_stock_universe_malformed", "stock universe item is malformed"
            )
        symbol = str(item.get("symbol") or "").strip().upper()
        if (
            not _INTRADAY_SYMBOL_PATTERN.fullmatch(symbol)
            or symbol in symbols
            or item.get("securityType") != "STOCK"
            or item.get("isCommonShare") is not True
            or not str(item.get("isinCode") or "").strip()
        ):
            raise IntradayPlanBlocked(
                "intraday_stock_universe_malformed",
                f"{market} stock universe item is invalid",
            )
        symbols.add(symbol)
    if not symbols:
        raise IntradayPlanBlocked(
            "intraday_stock_universe_empty", f"{market} stock universe is empty"
        )
    return symbols


def _strict_intraday_ranking_candidates(
    payload: Any,
    *,
    captured_at: datetime,
    schedule: _IntradaySchedule,
    max_age_seconds: int,
    min_price: Decimal,
    min_trading_amount: Decimal,
    min_change_fraction: Decimal,
    max_change_fraction: Decimal,
) -> tuple[datetime, list[dict[str, Any]]]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("rankings"), list):
        raise IntradayPlanBlocked(
            "intraday_ranking_malformed", "ranking response is malformed"
        )
    rankings = payload["rankings"]
    ranked_at = _strict_aware_datetime("ranking.rankedAt", payload.get("rankedAt"))
    if len(rankings) > _INTRADAY_RANKING_COUNT:
        raise IntradayPlanBlocked(
            "intraday_ranking_malformed", "ranking response exceeded requested count"
        )
    _validate_intraday_timestamp(
        "ranking",
        ranked_at,
        captured_at=captured_at,
        max_age_seconds=max_age_seconds,
        schedule=schedule,
    )

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    seen_ranks: set[int] = set()
    for item in rankings:
        if not isinstance(item, Mapping):
            raise IntradayPlanBlocked(
                "intraday_ranking_malformed", "ranking item is malformed"
            )
        rank = item.get("rank")
        symbol = str(item.get("symbol") or "").strip().upper()
        if (
            isinstance(rank, bool)
            or not isinstance(rank, int)
            or not 1 <= rank <= _INTRADAY_RANKING_COUNT
            or rank in seen_ranks
            or not _INTRADAY_SYMBOL_PATTERN.fullmatch(symbol)
            or symbol in seen
        ):
            raise IntradayPlanBlocked(
                "intraday_ranking_malformed", "ranking identity is malformed"
            )
        seen.add(symbol)
        seen_ranks.add(rank)
        if item.get("currency") != "USD" or not isinstance(item.get("price"), Mapping):
            raise IntradayPlanBlocked(
                "intraday_ranking_malformed", "US ranking item must use USD"
            )
        price = item["price"]
        last_price = _strict_decimal("ranking.lastPrice", price.get("lastPrice"))
        base_price = _strict_positive_or_zero_decimal(
            "ranking.basePrice", price.get("basePrice")
        )
        if base_price == 0 or price.get("changeRate") is None:
            continue
        change_fraction = _strict_signed_decimal(
            "ranking.changeRate", price.get("changeRate")
        )
        trading_volume = _strict_decimal(
            "ranking.tradingVolume", item.get("tradingVolume")
        )
        trading_amount = _strict_decimal(
            "ranking.tradingAmount", item.get("tradingAmount")
        )
        if (
            last_price < min_price
            or trading_amount < min_trading_amount
            or change_fraction < min_change_fraction
            or change_fraction > max_change_fraction
        ):
            continue
        candidates.append(
            {
                "rank": rank,
                "symbol": symbol,
                "last_price": last_price,
                "base_price": base_price,
                "change_fraction": change_fraction,
                "trading_volume": trading_volume,
                "trading_amount": trading_amount,
            }
        )
    candidates.sort(key=lambda item: (item["rank"], item["symbol"]))
    return ranked_at, candidates


def _strict_intraday_stock_details(
    payload: Any,
    *,
    requested_symbols: set[str],
) -> dict[str, Mapping[str, Any]]:
    if not isinstance(payload, list):
        raise IntradayPlanBlocked(
            "intraday_stock_info_malformed", "stock details are not an array"
        )
    details: dict[str, Mapping[str, Any]] = {}
    for item in payload:
        if not isinstance(item, Mapping):
            raise IntradayPlanBlocked(
                "intraday_stock_info_malformed", "stock detail is malformed"
            )
        symbol = str(item.get("symbol") or "").strip().upper()
        if symbol not in requested_symbols or symbol in details:
            raise IntradayPlanBlocked(
                "intraday_stock_info_malformed", "stock detail identity is invalid"
            )
        details[symbol] = item
    if set(details) != requested_symbols:
        raise IntradayPlanBlocked(
            "intraday_stock_info_malformed",
            "stock details did not contain every requested symbol",
        )
    return details


def _eligible_intraday_stock(stock: Mapping[str, Any]) -> bool:
    return bool(
        stock.get("status") == "ACTIVE"
        and stock.get("currency") == "USD"
        and stock.get("securityType") == "STOCK"
        and stock.get("isCommonShare") is True
        and stock.get("market") in {"NYSE", "NASDAQ", "AMEX"}
    )


def _strict_intraday_warning_clear(payload: Any) -> bool:
    if not isinstance(payload, list):
        raise IntradayPlanBlocked(
            "intraday_stock_warning_malformed", "stock warnings are not an array"
        )
    for item in payload:
        if not isinstance(item, Mapping) or not str(item.get("warningType") or "").strip():
            raise IntradayPlanBlocked(
                "intraday_stock_warning_malformed", "stock warning is malformed"
            )
    return not payload


def _strict_intraday_daily_metrics(
    candles: Sequence[Any],
    *,
    symbol: str,
    session_date: date,
    min_average_daily_value: Decimal,
    max_average_daily_range_fraction: Decimal,
) -> dict[str, Any] | None:
    valid = _strict_intraday_candles(
        candles,
        symbol=symbol,
        expected_adjusted=False,
        require_positive_volume=True,
    )
    by_date: dict[date, Any] = {}
    eastern = ZoneInfo("America/New_York")
    for candle in valid:
        candle_date = candle.timestamp.astimezone(eastern).date()
        if candle_date >= session_date:
            continue
        if candle_date in by_date:
            raise IntradayPlanBlocked(
                "intraday_daily_candles_malformed", "daily candle date is duplicated"
            )
        by_date[candle_date] = candle
    completed = [by_date[key] for key in sorted(by_date, reverse=True)]
    if len(completed) < _INTRADAY_DAILY_CANDLE_COUNT:
        return None
    completed = completed[:_INTRADAY_DAILY_CANDLE_COUNT]
    if session_date - completed[0].timestamp.astimezone(eastern).date() > timedelta(days=7):
        return None
    divisor = Decimal(_INTRADAY_DAILY_CANDLE_COUNT)
    average_value = sum(
        (candle.close * candle.volume for candle in completed), Decimal("0")
    ) / divisor
    average_range = sum(
        ((candle.high - candle.low) / candle.close for candle in completed),
        Decimal("0"),
    ) / divisor
    if (
        average_value < min_average_daily_value
        or average_range > max_average_daily_range_fraction
    ):
        return None
    return {
        "completed_daily_candles": len(completed),
        "average_daily_value": _decimal_text(average_value),
        "average_daily_range_fraction": _decimal_text(average_range),
        "latest_completed_daily_candle": completed[0].timestamp.isoformat(),
    }


def _strict_intraday_premarket_metrics(
    candles: Sequence[Any],
    *,
    symbol: str,
    captured_at: datetime,
    schedule: _IntradaySchedule,
    max_age_seconds: int,
    max_range_fraction: Decimal,
) -> dict[str, Any] | None:
    valid = _strict_intraday_candles(
        candles,
        symbol=symbol,
        expected_adjusted=False,
        require_positive_volume=False,
    )
    completed = [
        candle
        for candle in valid
        if schedule.premarket_open <= candle.timestamp
        and candle.timestamp + timedelta(minutes=1) <= captured_at
    ]
    completed.sort(key=lambda candle: candle.timestamp)
    if len(completed) < _INTRADAY_MIN_PREMARKET_CANDLES:
        return None
    latest_end = completed[-1].timestamp + timedelta(minutes=1)
    age = (captured_at - latest_end).total_seconds()
    total_volume = sum((candle.volume for candle in completed), Decimal("0"))
    session_high = max(candle.high for candle in completed)
    session_low = min(candle.low for candle in completed)
    range_fraction = (session_high - session_low) / completed[-1].close
    if (
        age < 0
        or age > max_age_seconds
        or total_volume <= 0
        or range_fraction > max_range_fraction
    ):
        return None
    return {
        "completed_premarket_candles": len(completed),
        "premarket_volume": _decimal_text(total_volume),
        "premarket_range_fraction": _decimal_text(range_fraction),
        "latest_completed_premarket_candle": completed[-1].timestamp.isoformat(),
    }


def _fetch_intraday_premarket_candles(
    client: TossClient,
    *,
    symbol: str,
    before: datetime,
    premarket_open: datetime,
) -> tuple[Any, ...]:
    candles: dict[datetime, Any] = {}
    cursor: str | datetime = before
    seen_cursors: set[str] = set()
    reached_session_start = False
    exhausted = False
    for _ in range(3):
        page = client.get_candles(
            symbol,
            interval="1m",
            count=_INTRADAY_PREMARKET_CANDLE_COUNT,
            before=cursor,
            adjusted=False,
        )
        for candle in page.candles:
            existing = candles.get(candle.timestamp)
            if existing is not None and existing != candle:
                raise IntradayPlanBlocked(
                    "intraday_candles_malformed",
                    "inclusive candle pages disagree at the same timestamp",
                )
            candles[candle.timestamp] = candle
        if not page.candles:
            exhausted = True
            break
        if min(candle.timestamp for candle in page.candles) <= premarket_open:
            reached_session_start = True
            break
        next_before = page.next_before
        if not next_before:
            exhausted = True
            break
        if next_before in seen_cursors:
            raise IntradayPlanBlocked(
                "intraday_candles_malformed", "candle cursor repeated before coverage"
            )
        seen_cursors.add(next_before)
        cursor = next_before
    if not reached_session_start and not exhausted:
        raise IntradayPlanBlocked(
            "intraday_candles_incomplete", "premarket candle pagination did not reach session start"
        )
    return tuple(candles[key] for key in sorted(candles))


def _strict_intraday_candles(
    candles: Sequence[Any],
    *,
    symbol: str,
    expected_adjusted: bool,
    require_positive_volume: bool,
) -> list[Any]:
    if isinstance(candles, (str, bytes)) or not isinstance(candles, Sequence):
        raise IntradayPlanBlocked(
            "intraday_candles_malformed", "candles are not a sequence"
        )
    result: list[Any] = []
    seen: set[datetime] = set()
    for candle in candles:
        timestamp = getattr(candle, "timestamp", None)
        if (
            not isinstance(timestamp, datetime)
            or timestamp.tzinfo is None
            or timestamp.utcoffset() is None
            or timestamp in seen
            or str(getattr(candle, "symbol", "")).strip().upper() != symbol
            or getattr(candle, "currency", None) != "USD"
            or getattr(candle, "adjusted", None) is not expected_adjusted
        ):
            raise IntradayPlanBlocked(
                "intraday_candles_malformed", "candle identity is malformed"
            )
        seen.add(timestamp)
        open_price = _strict_decimal("candle.open", getattr(candle, "open", None))
        high = _strict_decimal("candle.high", getattr(candle, "high", None))
        low = _strict_decimal("candle.low", getattr(candle, "low", None))
        close = _strict_decimal("candle.close", getattr(candle, "close", None))
        volume = _strict_positive_or_zero_decimal(
            "candle.volume", getattr(candle, "volume", None)
        )
        if (
            low > high
            or not low <= open_price <= high
            or not low <= close <= high
            or (require_positive_volume and volume <= 0)
        ):
            raise IntradayPlanBlocked(
                "intraday_candles_malformed", "candle values are malformed"
            )
        result.append(candle)
    return result


def _strict_signed_decimal(name: str, value: Any) -> Decimal:
    if value is None or isinstance(value, bool):
        raise IntradayPlanBlocked("intraday_decimal_malformed", f"{name} is missing")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise IntradayPlanBlocked(
            "intraday_decimal_malformed", f"{name} is not a decimal"
        ) from exc
    if not result.is_finite():
        raise IntradayPlanBlocked(
            "intraday_decimal_malformed", f"{name} must be finite"
        )
    return result


def _best_intraday_level(value: Any, *, side: str) -> tuple[Decimal, Decimal]:
    if not isinstance(value, list) or not value:
        raise IntradayPlanBlocked(
            "intraday_orderbook_empty", f"orderbook {side}s are empty"
        )
    levels: list[tuple[Decimal, Decimal]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise IntradayPlanBlocked(
                "intraday_orderbook_malformed", f"orderbook {side} item is malformed"
            )
        price = _strict_decimal(f"{side}.price", item.get("price"))
        volume = _strict_decimal(f"{side}.volume", item.get("volume"))
        levels.append((price, volume))
    return max(levels) if side == "bid" else min(levels)


def _validate_intraday_timestamp(
    name: str,
    value: datetime,
    *,
    captured_at: datetime,
    max_age_seconds: int,
    schedule: _IntradaySchedule,
) -> None:
    if value > captured_at:
        raise IntradayPlanBlocked(
            f"intraday_{name}_from_future", f"{name} timestamp is in the future"
        )
    if (captured_at - value).total_seconds() > max_age_seconds:
        raise IntradayPlanBlocked(
            f"intraday_{name}_stale", f"{name} timestamp is stale"
        )
    if not schedule.premarket_open <= value < schedule.premarket_close:
        raise IntradayPlanBlocked(
            f"intraday_{name}_outside_premarket",
            f"{name} timestamp is outside the official premarket",
        )


def _validate_intraday_prelock_freshness(
    *,
    config,
    schedule: _IntradaySchedule,
    market: Mapping[str, Any],
    cash_captured_at: datetime,
    selection_snapshot: Mapping[str, Any],
    lock_at: datetime,
) -> None:
    _validate_intraday_timestamp(
        "price",
        _strict_aware_datetime("market.price_at", market.get("price_at")),
        captured_at=lock_at,
        max_age_seconds=config.intraday.quote_max_age_seconds,
        schedule=schedule,
    )
    _validate_intraday_timestamp(
        "orderbook",
        _strict_aware_datetime("market.orderbook_at", market.get("orderbook_at")),
        captured_at=lock_at,
        max_age_seconds=config.intraday.orderbook_max_age_seconds,
        schedule=schedule,
    )
    cash_age = (lock_at - cash_captured_at).total_seconds()
    if cash_age < 0 or cash_age > min(
        config.intraday.quote_max_age_seconds,
        config.intraday.orderbook_max_age_seconds,
    ):
        raise IntradayPlanBlocked(
            "intraday_cash_snapshot_stale",
            "cash buying power was stale at the final database lock boundary",
        )
    if selection_snapshot.get("mode") != "automatic":
        return
    _validate_intraday_timestamp(
        "ranking",
        _strict_aware_datetime(
            "selection.ranked_at", selection_snapshot.get("ranked_at")
        ),
        captured_at=lock_at,
        max_age_seconds=config.intraday.selection_rank_max_age_seconds,
        schedule=schedule,
    )
    _validate_intraday_timestamp(
        "warning_check",
        _strict_aware_datetime(
            "selection.warnings_checked_at",
            selection_snapshot.get("warnings_checked_at"),
        ),
        captured_at=lock_at,
        max_age_seconds=min(
            config.intraday.quote_max_age_seconds,
            config.intraday.orderbook_max_age_seconds,
        ),
        schedule=schedule,
    )
    _validate_intraday_timestamp(
        "account_check",
        _strict_aware_datetime(
            "selection.account_checked_at",
            selection_snapshot.get("account_checked_at"),
        ),
        captured_at=lock_at,
        max_age_seconds=min(
            config.intraday.quote_max_age_seconds,
            config.intraday.orderbook_max_age_seconds,
        ),
        schedule=schedule,
    )


def _intraday_existing_plan_snapshot(
    record: Mapping[str, Any],
    *,
    schedule: _IntradaySchedule,
    checked_at: datetime,
    simulation_enabled: bool = False,
) -> HealthSnapshot:
    if checked_at >= schedule.regular_open and not simulation_enabled:
        return HealthSnapshot(
            mode="shadow",
            ready=False,
            blockers=("intraday_execution_engine_not_enabled",),
            generated_at=checked_at,
        )
    return HealthSnapshot(mode="shadow", ready=True, blockers=(), generated_at=checked_at)


def _assert_intraday_plan_matches_config(
    record: Mapping[str, Any],
    *,
    config,
    symbol: str,
) -> None:
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        raise IntradayPlanBlocked(
            "intraday_plan_integrity_failure", "stored plan payload is missing"
        )
    expected = {
        "symbol": symbol,
        "cash_allocation_fraction": _decimal_text(
            config.intraday.cash_allocation_fraction
        ),
        "risk_fraction": _decimal_text(config.intraday.risk_fraction),
        "take_profit_fraction": _decimal_text(config.intraday.take_profit_fraction),
        "stop_fraction": _decimal_text(config.intraday.stop_fraction),
        "stop_limit_buffer_fraction": _decimal_text(
            config.intraday.stop_limit_buffer_fraction
        ),
        "max_entry_slippage_fraction": _decimal_text(
            config.intraday.max_entry_slippage_fraction
        ),
        "estimated_round_trip_cost_fraction": _decimal_text(
            config.intraday.estimated_round_trip_cost_fraction
        ),
        "estimated_fixed_round_trip_cost": _decimal_text(
            config.intraday.estimated_fixed_round_trip_cost
        ),
        "minimum_reward_risk_ratio": _decimal_text(
            config.intraday.minimum_reward_risk_ratio
        ),
        "max_notional": _decimal_text(config.intraday.max_notional),
        "max_quantity": config.intraday.max_quantity,
        "entry_start_minutes_after_open": (
            config.intraday.entry_start_minutes_after_open
        ),
        "entry_expiry_minutes_after_open": (
            config.intraday.entry_expiry_minutes_after_open
        ),
        "force_exit_minutes_before_close": (
            config.intraday.force_exit_minutes_before_close
        ),
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise IntradayPlanBlocked(
            "intraday_daily_plan_locked_config_changed",
            "today's immutable plan was created with different settings",
        )
    if payload.get("guardrails") != _intraday_guardrails(config):
        raise IntradayPlanBlocked(
            "intraday_daily_plan_locked_guardrails_changed",
            "today's immutable plan was created with different guardrails",
        )
    if config.intraday.selection_mode == "automatic":
        _assert_automatic_intraday_selection_snapshot(
            payload.get("selection_snapshot"),
            symbol=symbol,
        )


def _assert_automatic_intraday_selection_snapshot(
    value: Any,
    *,
    symbol: str,
) -> None:
    required = {
        "mode",
        "source",
        "ranked_at",
        "rank",
        "symbol",
        "warnings_clear",
        "warnings_checked_at",
        "account_checked_at",
        "market",
        "openapi_version",
        "openapi_sha256",
        "news_or_llm_influence",
    }
    if not isinstance(value, Mapping) or not required.issubset(value):
        raise IntradayPlanBlocked(
            "intraday_plan_integrity_failure",
            "automatic selection audit snapshot is missing",
        )
    rank = value.get("rank")
    if (
        value.get("mode") != "automatic"
        or value.get("source") != "MARKET_TRADING_AMOUNT:US:realtime"
        or value.get("symbol") != symbol
        or value.get("warnings_clear") is not True
        or value.get("market") not in {"NASDAQ", "NYSE", "AMEX"}
        or value.get("openapi_version") != _INTRADAY_SELECTOR_OPENAPI_VERSION
        or value.get("openapi_sha256") != _INTRADAY_SELECTOR_OPENAPI_SHA256
        or value.get("news_or_llm_influence") is not False
        or isinstance(rank, bool)
        or not isinstance(rank, int)
        or not 1 <= rank <= _INTRADAY_RANKING_COUNT
    ):
        raise IntradayPlanBlocked(
            "intraday_plan_integrity_failure",
            "automatic selection audit snapshot identity is invalid",
        )
    _strict_aware_datetime("selection.ranked_at", value.get("ranked_at"))
    _strict_aware_datetime(
        "selection.warnings_checked_at", value.get("warnings_checked_at")
    )
    _strict_aware_datetime(
        "selection.account_checked_at", value.get("account_checked_at")
    )


def _intraday_guardrails(config) -> dict[str, Any]:
    simulation = bool(config.intraday.simulation_enabled)
    guardrails = {
        "plan_lead_minutes": config.intraday.plan_lead_minutes,
        "minimum_plan_lead_minutes": config.intraday.minimum_plan_lead_minutes,
        "quote_max_age_seconds": config.intraday.quote_max_age_seconds,
        "orderbook_max_age_seconds": config.intraday.orderbook_max_age_seconds,
        "max_quote_skew_seconds": config.intraday.max_quote_skew_seconds,
        "max_spread_fraction": _decimal_text(config.intraday.max_spread_fraction),
        "max_last_mid_deviation_fraction": _decimal_text(
            config.intraday.max_last_mid_deviation_fraction
        ),
        "regular_session_only": True,
        "account_must_be_flat": not simulation,
        "open_orders_must_be_empty": not simulation,
        "cash_source": "virtual_usd_ledger" if simulation else "toss_cash_buying_power",
        "broker_mutations_allowed": False,
        "commission_floor_rule": "configured_round_trip_cost>=2*active_us_commission",
    }
    if config.intraday.selection_mode == "automatic":
        guardrails["selection"] = _intraday_selection_policy(config)
    if simulation:
        guardrails["simulation"] = {
            "id": config.intraday.simulation_id,
            "start_date": config.intraday.simulation_start_date.isoformat(),
            "end_date": config.intraday.simulation_end_date.isoformat(),
            "initial_cash": _decimal_text(
                config.intraday.simulation_initial_cash
            ),
            "slippage_fraction_per_fill": _decimal_text(
                config.intraday.simulation_slippage_fraction
            ),
            "fill_model": "causal-next-book-visible-depth-v1",
            "approval_required": False,
        }
    return guardrails


def _intraday_selection_policy(config) -> dict[str, Any]:
    intraday = config.intraday
    return {
        "mode": intraday.selection_mode,
        "source": "MARKET_TRADING_AMOUNT:US:realtime",
        "rank_max_age_seconds": intraday.selection_rank_max_age_seconds,
        "min_price": _decimal_text(intraday.selection_min_price),
        "min_trading_amount": _decimal_text(intraday.selection_min_trading_amount),
        "min_change_fraction": _decimal_text(intraday.selection_min_change_fraction),
        "max_change_fraction": _decimal_text(intraday.selection_max_change_fraction),
        "min_average_daily_value": _decimal_text(
            intraday.selection_min_average_daily_value
        ),
        "max_average_daily_range_fraction": _decimal_text(
            intraday.selection_max_average_daily_range_fraction
        ),
        "max_premarket_range_fraction": _decimal_text(
            intraday.selection_max_premarket_range_fraction
        ),
        "ranking_count": _INTRADAY_RANKING_COUNT,
        "candidate_review_limit": _INTRADAY_CANDIDATE_REVIEW_LIMIT,
        "eligible_markets": ["NASDAQ", "NYSE", "AMEX"],
        "daily_candle_count": _INTRADAY_DAILY_CANDLE_COUNT,
        "premarket_candle_count": _INTRADAY_PREMARKET_CANDLE_COUNT,
        "minimum_premarket_candles": _INTRADAY_MIN_PREMARKET_CANDLES,
        "news_or_llm_influence": False,
    }


def _refresh_intraday_news_context(
    *,
    config,
    record: Mapping[str, Any],
    store: SQLiteStateStore,
    at: datetime,
    current_regular_close: datetime,
) -> None:
    """Atomically expose only the locked symbol and its validity window.

    Export failures are diagnostic-only: news must never affect trading health
    or mutate the locked plan.
    """

    configured_path = config.intraday.news_context_path
    if not configured_path:
        return

    error_code = "news_context_invalid_plan"
    strict_stream_expectation_failure = False
    temporary_path: Path | None = None
    lock_handle = None
    try:
        payload = record.get("payload")
        session_date = record.get("session_date")
        symbol = str(record.get("symbol") or "").strip().upper()
        if not isinstance(payload, Mapping):
            raise ValueError("locked plan payload is missing")
        if not isinstance(session_date, date):
            raise ValueError("locked plan session date is invalid")
        if not _INTRADAY_SYMBOL_PATTERN.fullmatch(symbol):
            raise ValueError("locked plan symbol is invalid")
        if str(payload.get("symbol") or "").strip().upper() != symbol:
            raise ValueError("locked plan symbol does not match payload")
        planned_regular_close = datetime.fromisoformat(
            str(payload.get("regular_close") or "")
        )
        if (
            planned_regular_close.tzinfo is None
            or planned_regular_close.utcoffset() is None
        ):
            raise ValueError("locked plan regular close must include a timezone")
        if (
            current_regular_close.tzinfo is None
            or current_regular_close.utcoffset() is None
        ):
            raise ValueError("current regular close must include a timezone")
        # The same redacted context drives both news and the selected-symbol
        # stream.  Keep it valid through the official close so the simulator
        # can observe and causally fill the pre-close force-exit window.
        active_until = min(planned_regular_close, current_regular_close)

        if bool(getattr(config.intraday, "simulation_enabled", False)):
            error_code = "stream_expectation_write_failed"
            strict_stream_expectation_failure = True
            _write_intraday_stream_expectation(
                store=store,
                record=record,
                expected_until=active_until,
            )
            strict_stream_expectation_failure = False
        if at.astimezone(timezone.utc) >= active_until.astimezone(timezone.utc):
            return

        error_code = "news_context_invalid_path"
        target = Path(configured_path).expanduser()
        if target.name != "news-context.json":
            raise ValueError("news context path must end with news-context.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        context = {
            "schema_version": 1,
            "generated_at": at.astimezone(timezone.utc).isoformat(),
            "market": "US",
            "session_date": session_date.isoformat(),
            "active_until": active_until.isoformat(),
            "symbol": symbol,
            "reason": "intraday_plan",
        }
        error_code = "news_context_lock_busy"
        lock_handle = _try_intraday_news_context_lock(
            target.with_name(f".{target.name}.lock")
        )
        if lock_handle is None:
            raise RuntimeError("news context is being refreshed by another process")
        existing = _read_existing_intraday_news_context(target)
        if existing is not None:
            existing_session = date.fromisoformat(str(existing["session_date"]))
            existing_generated = datetime.fromisoformat(str(existing["generated_at"]))
            existing_active_until = datetime.fromisoformat(
                str(existing["active_until"])
            )
            if (
                existing_generated.tzinfo is None
                or existing_generated.utcoffset() is None
                or existing_active_until.tzinfo is None
                or existing_active_until.utcoffset() is None
            ):
                raise ValueError("existing context timestamps must include timezones")
            if existing_session > session_date:
                return
            if existing_session == session_date:
                error_code = "news_context_writer_collision"
                if str(existing["symbol"]).strip().upper() != symbol:
                    raise ValueError("another symbol already owns this context path")
                if existing_generated > at.astimezone(timezone.utc):
                    return
                active_until = min(active_until, existing_active_until)
                context["active_until"] = active_until.isoformat()
        serialized = json.dumps(
            context,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        error_code = "news_context_write_failed"
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(serialized)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, target)
        temporary_path = None
        os.chmod(target, 0o600)
        if os.name != "nt":
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_fd = os.open(target.parent, directory_flags)
            try:
                try:
                    os.fsync(directory_fd)
                except OSError:
                    pass
            finally:
                os.close(directory_fd)
    except Exception:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        diagnostic = {
            "code": error_code,
            "session_date": (
                record.get("session_date").isoformat()
                if isinstance(record.get("session_date"), date)
                else None
            ),
            "symbol": _safe_intraday_diagnostic_symbol(record.get("symbol")),
        }
        try:
            if not any(
                event.get("message") == "intraday_news_context_export_failed"
                and event.get("payload") == diagnostic
                for event in store.list_runtime_events(limit=100)
            ):
                store.record_runtime_event(
                    "WARN",
                    "intraday_news_context_export_failed",
                    diagnostic,
                )
        except Exception:
            pass
        if strict_stream_expectation_failure:
            raise
    finally:
        _release_intraday_news_context_lock(lock_handle)


def _read_existing_intraday_news_context(target: Path) -> Mapping[str, Any] | None:
    if not target.exists():
        return None
    if not target.is_file():
        return None
    raw = target.read_bytes()
    if len(raw) > 16_384:
        return None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    required = {
        "schema_version",
        "generated_at",
        "market",
        "session_date",
        "active_until",
        "symbol",
        "reason",
    }
    if not isinstance(parsed, Mapping) or set(parsed) != required:
        return None
    if (
        parsed.get("schema_version") != 1
        or parsed.get("market") != "US"
        or parsed.get("reason") != "intraday_plan"
    ):
        return None
    try:
        date.fromisoformat(str(parsed["session_date"]))
        generated_at = datetime.fromisoformat(str(parsed["generated_at"]))
        active_until = datetime.fromisoformat(str(parsed["active_until"]))
    except (TypeError, ValueError):
        return None
    if (
        generated_at.tzinfo is None
        or generated_at.utcoffset() is None
        or active_until.tzinfo is None
        or active_until.utcoffset() is None
        or not _INTRADAY_SYMBOL_PATTERN.fullmatch(str(parsed["symbol"]))
    ):
        return None
    return parsed


def _refresh_intraday_approval_envelope(
    *,
    config,
    record: Mapping[str, Any],
    store: SQLiteStateStore,
    account_alias: str,
    at: datetime,
    current_regular_open: datetime,
) -> None:
    """Expose one redacted shadow plan to the isolated approval worker."""

    if bool(getattr(config.intraday, "simulation_enabled", False)):
        return

    configured_path = config.intraday.approval_envelope_path
    if not configured_path:
        return

    error_code = "approval_envelope_invalid_plan"
    lock_handle = None
    try:
        payload = record.get("payload")
        session_date = record.get("session_date")
        plan_id = str(record.get("plan_id") or "").strip()
        plan_hash = str(record.get("plan_hash") or "").strip().lower()
        symbol = str(record.get("symbol") or "").strip().upper()
        if not isinstance(payload, Mapping):
            raise ValueError("locked plan payload is missing")
        if not isinstance(session_date, date):
            raise ValueError("locked plan session date is invalid")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", plan_id):
            raise ValueError("locked plan id is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", plan_hash):
            raise ValueError("locked plan hash is invalid")
        if not _INTRADAY_SYMBOL_PATTERN.fullmatch(symbol):
            raise ValueError("locked plan symbol is invalid")
        if payload.get("plan_id") != plan_id or str(payload.get("symbol") or "").upper() != symbol:
            raise ValueError("locked plan metadata does not match payload")
        clean_alias = str(account_alias or "").strip()
        if not clean_alias or len(clean_alias) > 80 or any(ch in clean_alias for ch in "\r\n"):
            raise ValueError("account alias is invalid")
        regular_open = datetime.fromisoformat(str(payload.get("regular_open") or ""))
        if regular_open.tzinfo is None or regular_open.utcoffset() is None:
            raise ValueError("locked plan regular open must include a timezone")
        if current_regular_open.tzinfo is None or current_regular_open.utcoffset() is None:
            raise ValueError("current regular open must include a timezone")
        expires_at = min(regular_open, current_regular_open)
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("approval envelope generation time must include a timezone")
        if at.astimezone(timezone.utc) >= expires_at.astimezone(timezone.utc):
            error_code = "approval_envelope_expired"
            raise ValueError("approval envelope is already expired")

        public = _intraday_public_plan_payload(payload, account_alias=clean_alias)
        immutable_envelope = {
            "schema_version": 1,
            "session_date": session_date.isoformat(),
            "plan_id": plan_id,
            "plan_hash": plan_hash,
            "account_alias": clean_alias,
            "mode": "shadow",
            "live_order_submission": False,
            "symbol": symbol,
            "allocated_cash": public["allocated_cash"],
            "quantity": public["quantity"],
            "entry_trigger": public["entry_trigger"],
            "entry_limit": public["entry_limit"],
            "target_trigger": public["target_trigger"],
            "stop_trigger": public["stop_trigger"],
            "stop_limit": public["stop_limit"],
            "planned_risk": public["planned_risk"],
            "reward_risk_ratio": public["reward_risk_ratio"],
        }
        nonce = secrets.token_urlsafe(18)
        envelope = immutable_envelope | {
            "generated_at": at.astimezone(timezone.utc).isoformat(),
            "expires_at": expires_at.isoformat(),
            "nonce": nonce,
        }
        candidate = ApprovalEnvelope.from_mapping(envelope)

        error_code = "approval_envelope_invalid_path"
        target = Path(configured_path).expanduser()
        if not target.is_absolute() or target.name != "approval-envelope.json":
            raise ValueError("approval envelope path must end with approval-envelope.json")
        error_code = "approval_envelope_invalid_directory"
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        require_private_directory(
            target.parent, "approval_envelope_directory_invalid"
        )
        error_code = "approval_envelope_lock_busy"
        lock_handle = _try_intraday_news_context_lock(
            target.with_name(f".{target.name}.lock")
        )
        if lock_handle is None:
            raise RuntimeError("approval envelope is being refreshed by another process")

        error_code = "approval_envelope_invalid_existing"
        existing = _read_existing_intraday_approval_envelope(target)
        if existing is not None:
            if existing.session_date > session_date:
                return
            if existing.session_date == session_date:
                error_code = "approval_envelope_writer_collision"
                if (
                    existing.plan_id != candidate.plan_id
                    or existing.plan_hash != candidate.plan_hash
                    or existing.symbol != candidate.symbol
                ):
                    raise ValueError("another plan already owns this approval envelope")
                public_values_match = _approval_envelope_public_values(existing) == (
                    _approval_envelope_public_values(candidate)
                )
                effective_expiry = min(existing.expires_at, candidate.expires_at)
                if at.astimezone(timezone.utc) >= effective_expiry:
                    error_code = "approval_envelope_expired"
                    raise ValueError("approval envelope is already expired")
                if existing.expires_at < candidate.expires_at:
                    envelope["expires_at"] = existing.expires_at.isoformat()
                candidate = ApprovalEnvelope.from_mapping(envelope)
                if public_values_match:
                    envelope["nonce"] = existing.nonce
                    candidate = ApprovalEnvelope.from_mapping(envelope)
                    if existing.expires_at == candidate.expires_at:
                        return

        ApprovalEnvelope.from_mapping(envelope)
        error_code = "approval_envelope_write_failed"
        _write_private_json_atomic(
            target,
            envelope,
            expected_existing=existing,
        )
    except Exception:
        diagnostic = {
            "code": error_code,
            "session_date": (
                record.get("session_date").isoformat()
                if isinstance(record.get("session_date"), date)
                else None
            ),
            "symbol": _safe_intraday_diagnostic_symbol(record.get("symbol")),
        }
        try:
            if not any(
                event.get("message") == "intraday_approval_envelope_export_failed"
                and event.get("payload") == diagnostic
                for event in store.list_runtime_events(limit=100)
            ):
                store.record_runtime_event(
                    "WARN",
                    "intraday_approval_envelope_export_failed",
                    diagnostic,
                )
        except Exception:
            pass
    finally:
        _release_intraday_news_context_lock(lock_handle)


def _approval_envelope_public_values(
    envelope: ApprovalEnvelope,
) -> tuple[object, ...]:
    return (
        envelope.account_alias,
        envelope.allocated_cash,
        envelope.quantity,
        envelope.entry_trigger,
        envelope.entry_limit,
        envelope.target_trigger,
        envelope.stop_trigger,
        envelope.stop_limit,
        envelope.planned_risk,
        envelope.reward_risk_ratio,
    )


def _safe_intraday_diagnostic_symbol(value: object) -> str | None:
    clean = str(value or "").strip().upper()
    if len(clean) > 24 or not re.fullmatch(
        r"[A-Z][A-Z0-9]*(?:[.-][A-Z0-9]+)?",
        clean,
    ):
        return None
    return clean


def _read_existing_intraday_approval_envelope(
    target: Path,
) -> ApprovalEnvelope | None:
    try:
        target.lstat()
    except FileNotFoundError:
        return None
    return load_approval_envelope(target)


def _write_private_json_atomic(
    target: Path,
    payload: Mapping[str, Any],
    *,
    expected_existing: ApprovalEnvelope | None,
) -> None:
    temporary_path: Path | None = None
    try:
        serialized = json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(serialized)
            temporary.flush()
            os.chmod(temporary_path, 0o600)
            os.fsync(temporary.fileno())
        if expected_existing is None:
            os.link(temporary_path, target, follow_symlinks=False)
        else:
            current = load_approval_envelope(target)
            if current != expected_existing:
                raise RuntimeError("approval envelope changed during refresh")
            os.replace(temporary_path, target)
            temporary_path = None
        _fsync_parent_directory_best_effort(target.parent)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _fsync_parent_directory_best_effort(directory: Path) -> None:
    descriptor = -1
    try:
        descriptor = os.open(
            directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_intraday_stream_expectation(
    *,
    store: SQLiteStateStore,
    record: Mapping[str, Any],
    expected_until: datetime,
) -> None:
    """Publish a redacted durable signal that the selected-symbol stream is required."""

    if store.path == ":memory:":
        raise ValueError("stream expectation requires a file-backed planner database")
    target = Path(store.path).expanduser().resolve().with_name(
        "stream-expectation.json"
    )
    session_date = record.get("session_date")
    expected_from = datetime.fromisoformat(str(record.get("created_at") or ""))
    if (
        not isinstance(session_date, date)
        or expected_from.tzinfo is None
        or expected_from.utcoffset() is None
        or expected_until.tzinfo is None
        or expected_until.utcoffset() is None
        or expected_from >= expected_until
    ):
        raise ValueError("stream expectation plan window is invalid")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        payload = json.dumps(
            {
                "schema_version": 1,
                "session_date": session_date.isoformat(),
                "expected_from": expected_from.astimezone(timezone.utc).isoformat(),
                "expected_until": expected_until.astimezone(timezone.utc).isoformat(),
                "reason": "intraday_paper_stream",
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(payload)
            temporary.flush()
            os.chmod(temporary_path, 0o600)
            os.fsync(temporary.fileno())
        os.replace(temporary_path, target)
        temporary_path = None
        os.chmod(target, 0o600)
        _fsync_parent_directory_best_effort(target.parent)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _try_intraday_news_context_lock(path: Path):
    try:
        handle = path.open("a+b")
        os.chmod(path, 0o600)
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except (ImportError, OSError):
        try:
            handle.close()
        except (NameError, OSError):
            pass
        return None


def _release_intraday_news_context_lock(handle) -> None:
    if handle is None:
        return
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (ImportError, OSError):
        pass
    finally:
        try:
            handle.close()
        except OSError:
            pass


def _intraday_public_plan_payload(
    payload: Mapping[str, Any],
    *,
    account_alias: str,
) -> dict[str, Any]:
    return {
        "account_alias": account_alias,
        "mode": "shadow",
        "live_order_submission": False,
        "simulation": bool(
            isinstance(payload.get("guardrails"), Mapping)
            and isinstance(payload["guardrails"].get("simulation"), Mapping)
        ),
        "session_date": payload.get("session_date"),
        "symbol": payload.get("symbol"),
        "available_cash": payload.get("available_cash"),
        "allocated_cash": payload.get("allocated_cash"),
        "quantity": payload.get("quantity"),
        "entry_trigger": payload.get("entry_trigger"),
        "entry_limit": payload.get("entry_limit"),
        "target_trigger": payload.get("target_trigger"),
        "stop_trigger": payload.get("stop_trigger"),
        "stop_limit": payload.get("stop_limit"),
        "planned_risk": payload.get("planned_risk"),
        "cash_reserved": payload.get("cash_reserved"),
        "reward_risk_ratio": payload.get("reward_risk_ratio"),
        "entry_start": payload.get("entry_start"),
        "entry_expiry": payload.get("entry_expiry"),
        "force_exit_at": payload.get("force_exit_at"),
    }


def _intraday_notification(
    *,
    key: str,
    message: str,
    level: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "notification_key": key,
        "message": message,
        "level": level,
        "payload": dict(payload),
    }


def _intraday_plan_notification_key(
    *,
    account_key: str,
    session_date: date,
) -> str:
    return f"intraday-plan:{account_key}:{session_date.isoformat()}"


def _intraday_blocker_notification_key(
    *,
    account_key: str,
    session_date: date,
    symbol: str,
    code: str,
) -> str:
    return (
        f"intraday-blocked:{account_key}:{session_date.isoformat()}:"
        f"{symbol}:{code}"
    )


def _ensure_intraday_plan_notification(
    *,
    store: SQLiteStateStore,
    record: Mapping[str, Any],
    account_alias: str,
) -> None:
    payload = record.get("payload")
    session_date = record.get("session_date")
    account_key = str(record.get("account_key") or "")
    if not isinstance(payload, Mapping) or not isinstance(session_date, date) or not account_key:
        raise RuntimeError("stored intraday plan cannot be queued for notification")
    simulation = bool(
        isinstance(payload.get("guardrails"), Mapping)
        and isinstance(payload["guardrails"].get("simulation"), Mapping)
    )
    store.enqueue_notification_once(
        notification_key=_intraday_plan_notification_key(
            account_key=account_key,
            session_date=session_date,
        ),
        message=(
            "intraday_paper_plan_created"
            if simulation
            else "intraday_shadow_plan_created"
        ),
        level="info",
        payload=_intraday_public_plan_payload(payload, account_alias=account_alias),
        created_at=record.get("created_at"),
    )


def _drain_intraday_notifications(
    *,
    store: SQLiteStateStore,
    notifier: DiscordTradeNotifier,
    at: datetime,
    limit: int = 20,
) -> None:
    """Deliver durable public alerts; failed sends remain pending for restart."""

    if not notifier.enabled:
        return
    for _ in range(limit):
        claimed = store.claim_pending_notification(now=at)
        if claimed is None:
            return
        sent = notifier.notify(
            claimed["message"],
            level=claimed["level"],
            payload=claimed["payload"],
        )
        if sent:
            store.mark_notification_sent(
                notification_key=claimed["notification_key"],
                claim_token=claimed["claim_token"],
                sent_at=at,
            )
            continue
        store.mark_notification_failed(
            notification_key=claimed["notification_key"],
            claim_token=claimed["claim_token"],
            error_code="discord_send_failed",
        )
        return


def _intraday_public_blocker_reason(code: str) -> str:
    return _INTRADAY_PUBLIC_BLOCKER_REASONS.get(
        code,
        "안전 검증을 통과하지 못했습니다. 로컬 런타임 기록을 확인하세요.",
    )


def _intraday_exception_diagnostic(exc: Exception) -> dict[str, Any]:
    """Return an allowlisted diagnostic without copying exception text."""

    diagnostic: dict[str, Any] = {"exception_type": type(exc).__name__}
    status = getattr(exc, "status", None)
    if isinstance(status, int) and not isinstance(status, bool):
        diagnostic["http_status"] = status
    return diagnostic


def _record_intraday_blocker_once(
    *,
    store: SQLiteStateStore,
    notifier: DiscordTradeNotifier,
    code: str,
    diagnostic: Mapping[str, Any],
    session_date: date,
    symbol: str,
    account_key: str,
    account_alias: str,
    at: datetime,
) -> None:
    session_text = session_date.isoformat()
    public_payload = {
        "account_alias": account_alias,
        "session_date": session_text,
        "symbol": symbol,
        "blocker": code,
        "reason": _intraday_public_blocker_reason(code),
        "mode": "shadow",
        "live_order_submission": False,
    }
    inserted = store.enqueue_notification_once(
        notification_key=_intraday_blocker_notification_key(
            account_key=account_key,
            session_date=session_date,
            symbol=symbol,
            code=code,
        ),
        message="intraday_shadow_plan_blocked",
        level="warn",
        payload=public_payload,
        created_at=at,
    )
    if inserted:
        store.record_runtime_event(
            "WARN",
            "intraday_shadow_plan_blocked",
            public_payload | {"diagnostic": dict(diagnostic)},
        )
    _drain_intraday_notifications(store=store, notifier=notifier, at=at)


def _strict_aware_datetime(name: str, value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise IntradayPlanBlocked(
                "intraday_timestamp_malformed", f"{name} is not ISO-8601"
            ) from exc
    else:
        raise IntradayPlanBlocked(
            "intraday_timestamp_malformed", f"{name} is missing"
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IntradayPlanBlocked(
            "intraday_timestamp_malformed", f"{name} must include a timezone"
        )
    return parsed


def _strict_decimal(name: str, value: Any) -> Decimal:
    if value is None or isinstance(value, bool):
        raise IntradayPlanBlocked("intraday_decimal_malformed", f"{name} is missing")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise IntradayPlanBlocked(
            "intraday_decimal_malformed", f"{name} is not a decimal"
        ) from exc
    if not result.is_finite() or result <= 0:
        raise IntradayPlanBlocked(
            "intraday_decimal_malformed", f"{name} must be finite and positive"
        )
    return result


def _strict_positive_or_zero_decimal(name: str, value: Any) -> Decimal:
    if value is None or isinstance(value, bool):
        raise IntradayPlanBlocked("intraday_decimal_malformed", f"{name} is missing")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise IntradayPlanBlocked(
            "intraday_decimal_malformed", f"{name} is not a decimal"
        ) from exc
    if not result.is_finite() or result < 0:
        raise IntradayPlanBlocked(
            "intraday_decimal_malformed", f"{name} must be finite and nonnegative"
        )
    return result


def _optional_iso_date(name: str, value: Any) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise IntradayPlanBlocked(
            "intraday_commission_malformed", f"{name} must be an ISO date or null"
        )
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise IntradayPlanBlocked(
            "intraday_commission_malformed", f"{name} is invalid"
        ) from exc


def _intraday_now(now) -> datetime:
    value = now()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("intraday runtime clock must return a timezone-aware datetime")
    return value


def _intraday_account_key(account_seq: Any) -> str:
    raw = str(account_seq or "").strip()
    if not raw:
        raise ValueError("toss.account_seq is required")
    digest = hashlib.sha256(f"toss:{raw}".encode("utf-8")).hexdigest()
    return f"toss-{digest[:24]}"


def _intraday_paper_config(config) -> IntradayPaperConfig:
    intraday = config.intraday
    return IntradayPaperConfig(
        run_id=intraday.simulation_id,
        start_date=intraday.simulation_start_date,
        end_date=intraday.simulation_end_date,
        initial_cash_usd=intraday.simulation_initial_cash,
        slippage_fraction=intraday.simulation_slippage_fraction,
        quote_max_age_seconds=min(
            intraday.quote_max_age_seconds,
            intraday.orderbook_max_age_seconds,
        ),
        future_tolerance_seconds=min(5, intraday.max_quote_skew_seconds),
        experiment_hash=intraday_simulation_experiment_hash(config),
    )


def _paper_month_public_payload(summary: Mapping[str, Any]) -> dict[str, Any]:
    initial = summary.get("initial_cash_usd")
    realized = summary.get("realized_pnl_usd")
    coverage = summary.get("coverage")
    coverage = coverage if isinstance(coverage, Mapping) else {}
    return {
        "run_id": summary.get("run_id"),
        "status": summary.get("status"),
        "start_date": summary.get("start_date"),
        "end_date": summary.get("end_date_inclusive"),
        "initial_cash": initial,
        "current_cash": summary.get("current_cash_usd"),
        "final_equity": summary.get("final_equity_usd"),
        "net_pnl": realized,
        "clean_net_pnl": summary.get("clean_realized_pnl_usd"),
        "return_fraction": summary.get("return_fraction"),
        "clean_return_fraction": summary.get("clean_return_fraction"),
        "trades": summary.get("trade_count"),
        "wins": summary.get("wins"),
        "losses": summary.get("losses"),
        "win_rate": summary.get("win_rate"),
        "average_win": summary.get("average_win_usd"),
        "average_loss": summary.get("average_loss_usd"),
        "profit_factor": summary.get("profit_factor"),
        "expectancy": summary.get("expectancy_usd"),
        "total_fees": summary.get("total_fees_usd"),
        "max_drawdown_fraction": summary.get("max_drawdown_fraction"),
        "max_drawdown": summary.get("max_closed_equity_drawdown_usd"),
        "exit_reason_counts": summary.get("exit_reason_counts"),
        "no_entry_sessions": summary.get("no_entry_count"),
        "invalid_sessions": summary.get("invalid_result_count"),
        "unresolved_positions": summary.get("unresolved_position_count"),
        "waiting_plans": summary.get("waiting_plan_count"),
        "coverage_expected": coverage.get("expected_count"),
        "coverage_covered": coverage.get("covered_count"),
        "coverage_missing": coverage.get("missing_count"),
        "missing_dates": coverage.get("missing"),
        "market_closed_dates": coverage.get("market_closed"),
        "accepted_events": summary.get("accepted_event_count"),
        "journaled_frames": summary.get("journaled_frame_count"),
        "data_gaps": summary.get("data_gap_count"),
        "journal_policy": summary.get("journal_policy"),
        "fee_model": summary.get("fee_model"),
    }


def _paper_daily_public_payload(summary: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "run_id": summary.get("run_id"),
        "session_date": summary.get("session_date"),
        "symbol": summary.get("symbol"),
        "status": summary.get("status"),
        "quantity": summary.get("quantity"),
        "entry_price": summary.get("entry_price"),
        "entry_at": summary.get("entry_at"),
        "entry_fee": summary.get("entry_fee"),
        "exit_price": summary.get("exit_price"),
        "exit_at": summary.get("exit_at"),
        "exit_fee": summary.get("exit_fee"),
        "exit_reason": summary.get("exit_reason"),
        "gross_pnl": summary.get("gross_pnl"),
        "net_pnl": summary.get("realized_pnl") or "0",
        "fees": summary.get("total_fees")
        or summary.get("fees")
        or "0",
        "cash_start": summary.get("cash_before"),
        "cash_end": summary.get("cash_after"),
        "accepted_events": summary.get("accepted_event_count"),
        "journaled_frames": summary.get("journaled_frame_count"),
        "data_gaps": summary.get("data_gap_count"),
        "first_event_at": summary.get("first_event_at"),
        "last_event_at": summary.get("last_event_at"),
        "fee_sources": summary.get("fee_sources"),
        "included_in_clean_metrics": summary.get("included_in_clean_metrics"),
    }


def _forward_intraday_paper_alerts(
    *,
    paper_store: IntradayPaperStore,
    store: SQLiteStateStore,
    at: datetime,
) -> None:
    message_by_event = {
        "entry_filled": "intraday_paper_entry_filled",
        "exit_filled": "intraday_paper_exit_filled",
        "invalid_exit": "intraday_paper_invalid",
        "invalid_no_entry": "intraday_paper_invalid",
        "market_data_gap": "intraday_paper_invalid",
        "unresolved_position": "intraday_paper_invalid",
    }
    for alert in paper_store.list_alerts(pending_only=True):
        event = str(alert.get("event") or "")
        message = message_by_event.get(event)
        if message is not None:
            plan_id = str(alert.get("plan_id") or "")
            payload = dict(alert.get("payload") or {})
            if plan_id:
                try:
                    session = paper_store.daily_summary(
                        payload.get("session_date")
                        or next(
                            day["session_date"]
                            for day in paper_store.summary(as_of=at).get("days", [])
                            if day.get("plan_id") == plan_id
                        )
                    )
                except (StopIteration, KeyError, ValueError):
                    session = {}
                payload = _paper_daily_public_payload(session) | payload
            if event == "entry_filled":
                payload["entry_price"] = payload.get("price")
            elif event in {"exit_filled", "invalid_exit"}:
                payload["exit_price"] = payload.get("price")
                payload["net_pnl"] = payload.get("realized_pnl")
                payload["exit_reason"] = payload.get("reason")
            store.enqueue_notification_once(
                notification_key=f"intraday-paper-alert:{alert['alert_id']}",
                message=message,
                level=str(alert.get("level") or "info"),
                payload=payload,
                created_at=datetime.fromisoformat(str(alert["created_at"])),
            )
        paper_store.mark_alert_forwarded(str(alert["alert_id"]), forwarded_at=at)


def _sync_intraday_paper_plan(
    *,
    paper_store: IntradayPaperStore,
    store: SQLiteStateStore,
    record: Mapping[str, Any],
    at: datetime,
    regular_close: datetime,
) -> None:
    paper_store.ensure_plan(record, registered_at=record.get("created_at"))
    plan_id = str(record["plan_id"])
    current = paper_store.load_plan(plan_id)
    grace = timedelta(seconds=paper_store.config.quote_max_age_seconds)
    status = str(current.get("status") or "")
    entry_expiry = datetime.fromisoformat(str(current["entry_expiry"]))
    force_exit_at = datetime.fromisoformat(str(current["force_exit_at"]))
    if (
        status == "WAITING_ENTRY"
        and at >= entry_expiry + grace
        or status == "OPEN"
        and at >= force_exit_at + grace
    ):
        current = paper_store.finalize_session(plan_id, now=at)
    _forward_intraday_paper_alerts(paper_store=paper_store, store=store, at=at)
    if (
        at >= regular_close + grace
        and current.get("status")
        in {"CLOSED", "INVALID", "NO_ENTRY", "UNRESOLVED"}
    ):
        summary = paper_store.daily_summary(record["session_date"])
        store.enqueue_notification_once(
            notification_key=(
                f"intraday-paper-daily:{paper_store.config.run_id}:"
                f"{record['session_date'].isoformat()}"
            ),
            message="intraday_paper_daily_report",
            level=(
                "warn"
                if summary.get("status") in {"INVALID", "UNRESOLVED", "OPEN"}
                else "info"
            ),
            payload=_paper_daily_public_payload(summary),
            created_at=at,
        )


def _reconcile_intraday_paper_backlog(
    *,
    paper_store: IntradayPaperStore,
    store: SQLiteStateStore,
    at: datetime,
) -> None:
    """Resolve due prior plans before sizing or reporting another session."""

    records = {
        str(record["plan_id"]): record for record in store.list_intraday_plans()
    }
    for day in paper_store.summary(as_of=at).get("days", []):
        if day.get("status") not in {"WAITING_ENTRY", "OPEN"}:
            continue
        plan_id = str(day.get("plan_id") or "")
        record = records.get(plan_id)
        if record is None:
            raise PaperSimulationError(
                "paper plan has no immutable planner record"
            )
        current = paper_store.load_plan(plan_id)
        regular_close = datetime.fromisoformat(str(current["regular_close"]))
        _sync_intraday_paper_plan(
            paper_store=paper_store,
            store=store,
            record=record,
            at=at,
            regular_close=regular_close,
        )


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _toss_client_from_config(
    config,
    *,
    env: Mapping[str, str],
    transport: TossTransport | None = None,
    rate_limits: RateLimitQueue | None = None,
    now=lambda: datetime.now(timezone.utc),
) -> TossClient:
    credentials = TossCredentials(
        client_id=env[config.toss.client_id_env],
        client_secret=env[config.toss.client_secret_env],
    )
    return TossClient(
        credentials=credentials,
        account_seq=config.toss.account_seq,
        base_url=config.toss.base_url or TOSS_BASE_URL,
        transport=transport,
        rate_limits=rate_limits or RateLimitQueue(now=now),
        now=now,
    )


def _runtime_mode(config) -> str:
    mode = str(config.runtime.mode or "").strip().lower()
    return mode or "paper"


def _intraday_config_blockers(config) -> tuple[str, ...]:
    if config.strategy_kind != "intraday":
        return ()

    blockers: list[str] = []
    intraday = config.intraday
    mode = _runtime_mode(config)
    if mode != "shadow":
        blockers.append("intraday is shadow-only; runtime.mode must be shadow")
    if config.live_enabled:
        blockers.append("intraday is shadow-only; toss.live_enabled must be false")
    if intraday.live_execution_enabled:
        blockers.append("strategy.intraday.live_execution_enabled is not implemented")
    if str(config.runtime.market).strip().upper() != "US":
        blockers.append("intraday requires runtime.market US")
    if str(config.runtime.timezone_name).strip() != "America/New_York":
        blockers.append("intraday requires runtime.timezone America/New_York")
    if not config.runtime.use_market_calendar:
        blockers.append("intraday requires runtime.use_market_calendar true")
    if not intraday.regular_session_only:
        blockers.append("intraday requires regular_session_only true")
    if config.runtime.universe_enabled:
        blockers.append("intraday does not support runtime.universe_enabled")
    if config.runtime.watchlist_enabled:
        blockers.append("intraday requires runtime.watchlist_enabled false")
    if (
        isinstance(config.runtime.interval_seconds, bool)
        or not isinstance(config.runtime.interval_seconds, int)
        or config.runtime.interval_seconds < 1
    ):
        blockers.append("intraday requires a positive runtime.interval_seconds")

    symbols = tuple(str(item).strip().upper() for item in config.runtime.symbols)
    selection_mode = str(intraday.selection_mode or "").strip().lower()
    missing_selection: list[str] = []
    if selection_mode == "manual":
        if len(symbols) != 1:
            blockers.append(
                "manual intraday selection requires exactly one runtime.symbols entry"
            )
        elif not _INTRADAY_SYMBOL_PATTERN.fullmatch(symbols[0]):
            blockers.append("intraday runtime symbol has an invalid format")
    elif selection_mode == "automatic":
        if symbols:
            blockers.append(
                "automatic intraday selection requires runtime.symbols to be empty"
            )
        missing_selection = [
            f"strategy.intraday.selection.{name.removeprefix('selection_')}"
            for name in _INTRADAY_AUTOMATIC_SELECTION_FIELDS
            if getattr(intraday, name) is None
        ]
        if missing_selection:
            blockers.append(
                "intraday automatic selection values missing: "
                + ", ".join(missing_selection)
            )
    else:
        blockers.append("strategy.intraday.selection.mode must be manual or automatic")

    missing = [
        f"strategy.intraday.{name}"
        for name in _INTRADAY_REQUIRED_CONFIG_FIELDS
        if getattr(intraday, name) is None
    ]
    if missing:
        blockers.append("intraday required values missing: " + ", ".join(missing))
        return tuple(blockers)

    if selection_mode == "automatic" and not missing_selection:
        for name in (
            "selection_min_price",
            "selection_min_trading_amount",
            "selection_min_average_daily_value",
        ):
            value = getattr(intraday, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                blockers.append(
                    f"strategy.intraday.{name} must be finite and positive"
                )
        for name in (
            "selection_max_average_daily_range_fraction",
            "selection_max_premarket_range_fraction",
        ):
            if not _finite_decimal_between(
                getattr(intraday, name),
                lower=Decimal("0"),
                upper=Decimal("1"),
            ):
                blockers.append(
                    f"strategy.intraday.{name} must be finite and between 0 and 1"
                )
        minimum_change = intraday.selection_min_change_fraction
        maximum_change = intraday.selection_max_change_fraction
        if not _finite_decimal_between(
            minimum_change,
            lower=Decimal("0"),
            upper=Decimal("1"),
            lower_inclusive=True,
        ):
            blockers.append(
                "strategy.intraday.selection_min_change_fraction must be finite, nonnegative, and less than 1"
            )
        if not _finite_decimal_between(
            maximum_change,
            lower=Decimal("0"),
            upper=Decimal("1"),
        ):
            blockers.append(
                "strategy.intraday.selection_max_change_fraction must be finite and between 0 and 1"
            )
        if (
            isinstance(minimum_change, Decimal)
            and isinstance(maximum_change, Decimal)
            and minimum_change >= maximum_change
        ):
            blockers.append(
                "intraday automatic selection min_change_fraction must be below max_change_fraction"
            )
        rank_age = intraday.selection_rank_max_age_seconds
        if isinstance(rank_age, bool) or not isinstance(rank_age, int) or rank_age < 1:
            blockers.append(
                "strategy.intraday.selection_rank_max_age_seconds must be a positive integer"
            )
        if (
            isinstance(intraday.selection_min_price, Decimal)
            and isinstance(intraday.max_notional, Decimal)
            and intraday.selection_min_price > intraday.max_notional
        ):
            blockers.append(
                "intraday automatic selection min_price cannot exceed max_notional"
            )

    for name in (
        "cash_allocation_fraction",
        "risk_fraction",
        "take_profit_fraction",
        "stop_fraction",
        "estimated_round_trip_cost_fraction",
        "max_spread_fraction",
        "max_last_mid_deviation_fraction",
    ):
        value = getattr(intraday, name)
        if not _finite_decimal_between(value, lower=Decimal("0"), upper=Decimal("1")):
            blockers.append(f"strategy.intraday.{name} must be finite and between 0 and 1")
    for name in ("stop_limit_buffer_fraction", "max_entry_slippage_fraction"):
        value = getattr(intraday, name)
        if not _finite_decimal_between(
            value,
            lower=Decimal("0"),
            upper=Decimal("1"),
            lower_inclusive=True,
        ):
            blockers.append(
                f"strategy.intraday.{name} must be finite, nonnegative, and less than 1"
            )
    for name in ("minimum_reward_risk_ratio", "max_notional"):
        value = getattr(intraday, name)
        if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
            blockers.append(f"strategy.intraday.{name} must be finite and positive")
    fixed_cost = intraday.estimated_fixed_round_trip_cost
    if (
        not isinstance(fixed_cost, Decimal)
        or not fixed_cost.is_finite()
        or fixed_cost < 0
    ):
        blockers.append(
            "strategy.intraday.estimated_fixed_round_trip_cost must be finite and nonnegative"
        )
    if (
        isinstance(intraday.risk_fraction, Decimal)
        and isinstance(intraday.cash_allocation_fraction, Decimal)
        and intraday.risk_fraction > intraday.cash_allocation_fraction
    ):
        blockers.append("strategy.intraday.risk_fraction cannot exceed cash allocation")

    positive_ints = (
        "max_quantity",
        "plan_lead_minutes",
        "minimum_plan_lead_minutes",
        "quote_max_age_seconds",
        "orderbook_max_age_seconds",
        "entry_expiry_minutes_after_open",
        "force_exit_minutes_before_close",
    )
    for name in positive_ints:
        value = getattr(intraday, name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            blockers.append(f"strategy.intraday.{name} must be a positive integer")
    for name in ("max_quote_skew_seconds", "entry_start_minutes_after_open"):
        value = getattr(intraday, name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            blockers.append(f"strategy.intraday.{name} must be a nonnegative integer")

    if (
        isinstance(intraday.plan_lead_minutes, int)
        and isinstance(intraday.minimum_plan_lead_minutes, int)
        and intraday.plan_lead_minutes <= intraday.minimum_plan_lead_minutes
    ):
        blockers.append("intraday plan_lead_minutes must exceed minimum_plan_lead_minutes")
    if (
        isinstance(intraday.entry_start_minutes_after_open, int)
        and isinstance(intraday.entry_expiry_minutes_after_open, int)
        and intraday.entry_start_minutes_after_open
        >= intraday.entry_expiry_minutes_after_open
    ):
        blockers.append("intraday entry start must be before entry expiry")
    if (
        isinstance(intraday.plan_lead_minutes, int)
        and isinstance(intraday.minimum_plan_lead_minutes, int)
        and isinstance(config.runtime.interval_seconds, int)
        and (intraday.plan_lead_minutes - intraday.minimum_plan_lead_minutes) * 60
        < config.runtime.interval_seconds * 2
    ):
        blockers.append("intraday planning window must span at least two service intervals")
    if intraday.simulation_enabled:
        simulation_missing = [
            name
            for name in (
                "simulation_id",
                "simulation_start_date",
                "simulation_end_date",
                "simulation_initial_cash",
                "simulation_slippage_fraction",
                "simulation_db_path",
            )
            if getattr(intraday, name) is None
        ]
        if simulation_missing:
            blockers.append(
                "intraday simulation values missing: "
                + ", ".join(
                    f"strategy.intraday.{name}" for name in simulation_missing
                )
            )
        simulation_id = str(intraday.simulation_id or "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", simulation_id):
            blockers.append(
                "strategy.intraday.simulation_id must be a short lowercase identifier"
            )
        start_date = intraday.simulation_start_date
        end_date = intraday.simulation_end_date
        if (
            isinstance(start_date, date)
            and isinstance(end_date, date)
            and (
                start_date > end_date
                or (end_date - start_date).days > 31
            )
        ):
            blockers.append(
                "intraday simulation window must be ordered and no longer than 32 calendar days"
            )
        initial_cash = intraday.simulation_initial_cash
        if (
            not isinstance(initial_cash, Decimal)
            or not initial_cash.is_finite()
            or initial_cash <= 0
        ):
            blockers.append(
                "strategy.intraday.simulation_initial_cash must be finite and positive"
            )
        slippage = intraday.simulation_slippage_fraction
        if (
            not isinstance(slippage, Decimal)
            or not slippage.is_finite()
            or slippage < 0
            or slippage >= Decimal("0.01")
        ):
            blockers.append(
                "strategy.intraday.simulation_slippage_fraction must be between 0 and 0.01"
            )
        db_path = Path(str(intraday.simulation_db_path or ""))
        if (
            not db_path.is_absolute()
            or db_path.name != "intraday-paper.sqlite3"
        ):
            blockers.append(
                "strategy.intraday.simulation_db_path must be an absolute intraday-paper.sqlite3 path"
            )
    return tuple(blockers)


def _finite_decimal_between(
    value: Any,
    *,
    lower: Decimal,
    upper: Decimal,
    lower_inclusive: bool = False,
) -> bool:
    if not isinstance(value, Decimal) or not value.is_finite():
        return False
    lower_ok = value >= lower if lower_inclusive else value > lower
    return lower_ok and value < upper


def _build_toss_readiness_checks(
    *,
    config,
    env: Mapping[str, str],
) -> tuple[OperationsCheck, ...]:
    client_id_env = str(config.toss.client_id_env).strip()
    client_secret_env = str(config.toss.client_secret_env).strip()
    account_seq = (
        str(config.toss.account_seq).strip()
        if config.toss.account_seq is not None
        else ""
    )
    mode = str(config.runtime.mode or "").strip().lower()
    automatic_intraday = bool(
        config.strategy_kind == "intraday"
        and config.intraday.selection_mode == "automatic"
    )
    has_symbols = bool(
        config.runtime.symbols
        or config.runtime.universe_candidate_symbols
        or automatic_intraday
    )
    mode_is_supported = mode in {"paper", "shadow", "live"}
    consent_required = bool(mode == "live" and config.toss.require_live_consent)

    return (
        OperationsCheck(
            "runtime_mode",
            mode_is_supported,
            f"runtime.mode is {mode}"
            if mode_is_supported
            else f"runtime.mode must be paper, shadow, or live, got {config.runtime.mode}",
        ),
        OperationsCheck(
            "runtime_symbols_or_universe_candidates",
            has_symbols,
            "runtime symbols, universe candidates, or intraday automatic selection is configured"
            if has_symbols
            else "runtime.symbols or runtime.universe_candidate_symbols is required",
        ),
        OperationsCheck(
            "toss_client_id_env",
            bool(client_id_env),
            "toss.client_id_env configured"
            if client_id_env
            else "toss.client_id_env is empty",
        ),
        OperationsCheck(
            "toss_client_id",
            bool(client_id_env) and bool(env.get(client_id_env)),
            f"{client_id_env} is configured"
            if bool(client_id_env) and bool(env.get(client_id_env))
            else f"{client_id_env} is not configured",
        ),
        OperationsCheck(
            "toss_client_secret_env",
            bool(client_secret_env),
            "toss.client_secret_env configured"
            if client_secret_env
            else "toss.client_secret_env is empty",
        ),
        OperationsCheck(
            "toss_client_secret",
            bool(client_secret_env) and bool(env.get(client_secret_env)),
            f"{client_secret_env} is configured"
            if bool(client_secret_env) and bool(env.get(client_secret_env))
            else f"{client_secret_env} is not configured",
        ),
        OperationsCheck(
            "toss_account_seq",
            bool(account_seq),
            "toss.account_seq is configured"
            if account_seq
            else "toss.account_seq is not configured",
        ),
        OperationsCheck(
            "toss_live_consent_ids",
            not consent_required or bool(config.toss.allowed_live_consent_ids),
            "live consent allowlist is configured"
            if (not consent_required or bool(config.toss.allowed_live_consent_ids))
            else "live consent allowlist is required for live mode",
        ),
    )


def _submit_live_intents(
    *,
    config,
    client: TossClient,
    store: SQLiteStateStore,
    result,
    market_open: bool,
    notifier: DiscordTradeNotifier | None = None,
    account_alias: str | None = None,
    live_consent: Mapping[str, str] | None = None,
) -> None:
    if not result.intents:
        return
    consent_metadata = _live_consent_event_metadata(live_consent)
    sync_summary = _sync_unresolved_live_execution_orders(
        client=client,
        store=store,
    )
    currency = "USD" if str(config.runtime.market).strip().upper() == "US" else "KRW"
    try:
        buying_power = client.get_buying_power(currency)
        available_cash = _available_cash_from_buying_power(buying_power)
    except Exception as exc:
        payload = {"currency": currency, "error": str(exc), "account_alias": account_alias or ""}
        store.record_runtime_event("WARN", "live_buying_power_unavailable", payload)
        if notifier is not None:
            notifier.notify("live_buying_power_unavailable", level="warn", payload=payload)
        available_cash = None

    orchestrator = LiveOrderOrchestrator(
        safety=PreTradeSafety(
            PreTradeSafetyConfig(
                live_enabled=config.live_enabled,
                emergency_stop=config.live.emergency_stop,
                allowed_symbols=config.live.allowed_symbols,
                max_order_quantity=config.live.max_order_quantity,
                max_order_notional=config.live.max_order_notional,
                daily_order_count_limit=config.live.daily_order_count_limit,
                daily_notional_limit=config.live.daily_notional_limit,
                require_market_open=config.live.require_market_open,
                require_clean_reconcile=config.live.require_clean_reconcile,
                block_unresolved_orders=config.live.block_unresolved_orders,
                max_consecutive_order_failures=config.live.max_consecutive_order_failures,
            )
        ),
        broker=TossLiveBrokerAdapter(
            client,
            confirm_high_value_order=config.live.confirm_high_value_order,
        ),
        store=store,
    )
    daily_summary = store.execution_summary_since(_start_of_local_day(config.runtime.timezone_name))
    consecutive_order_failures = _consecutive_live_order_failures(
        store,
        since=_start_of_local_day(config.runtime.timezone_name),
    )
    for paper_intent in result.intents:
        live_intent = _live_order_intent_from_paper(paper_intent)
        position = store.load_position(live_intent.symbol)
        unresolved_order_exists = any(
            order.symbol == live_intent.symbol for order in result.reconcile.open_orders
        )
        store.record_runtime_event(
            "INFO",
            "live_order_final_guard_snapshot",
            {
                "guard": "final_pre_submit",
                "symbol": live_intent.symbol,
                "side": live_intent.side.value,
                "current_position_qty": str(
                    position.total_qty if position is not None else Decimal("0")
                ),
                "daily_order_count": int(daily_summary["count"]),
                "daily_notional": str(daily_summary["notional"]),
                "unresolved_order_exists": unresolved_order_exists,
                "consecutive_order_failures": consecutive_order_failures,
                "max_consecutive_order_failures": config.live.max_consecutive_order_failures,
                "live_consent": consent_metadata,
            },
        )
        final_guard_blockers = _live_final_guard_blockers(
            config=config,
            live_intent=live_intent,
            position_qty=position.total_qty if position is not None else Decimal("0"),
            unresolved_order_exists=unresolved_order_exists,
            daily_summary=daily_summary,
            consecutive_order_failures=consecutive_order_failures,
            unresolved_execution_count=int(sync_summary["remaining_unresolved"]),
        )
        if final_guard_blockers:
            payload = {
                "guard": "final_pre_submit",
                "blockers": list(final_guard_blockers),
                "symbol": live_intent.symbol,
                "side": live_intent.side.value,
                "quantity": str(live_intent.quantity),
                "account_alias": account_alias or "",
                "current_position_qty": str(
                    position.total_qty if position is not None else Decimal("0")
                ),
                "daily_order_count": int(daily_summary["count"]),
                "daily_notional": str(daily_summary["notional"]),
                "unresolved_order_exists": unresolved_order_exists,
                "consecutive_order_failures": consecutive_order_failures,
                "max_consecutive_order_failures": config.live.max_consecutive_order_failures,
                "unresolved_execution_count": int(sync_summary["remaining_unresolved"]),
                "live_consent": consent_metadata,
            }
            store.record_runtime_event("WARN", "live_order_final_guard_blocked", payload)
            if notifier is not None:
                notifier.notify("live_order_final_guard_blocked", level="warn", payload=payload)
            continue
        execution = orchestrator.submit(
            live_intent,
            context=PreTradeSafetyContext(
                market_open=market_open,
                reconcile_clean=result.reconcile.clean,
                unresolved_order_exists=unresolved_order_exists,
                available_cash=available_cash,
                current_position_qty=position.total_qty if position is not None else Decimal("0"),
                daily_order_count=int(daily_summary["count"]),
                daily_notional=daily_summary["notional"],
                consecutive_order_failures=consecutive_order_failures,
                unresolved_execution_count=int(sync_summary["remaining_unresolved"]),
            ),
        )
        if execution.status.value not in {"REJECTED", "FAILED", "UNKNOWN"}:
            daily_summary["count"] += 1
            if live_intent.notional is not None:
                daily_summary["notional"] += live_intent.notional
            consecutive_order_failures = 0
        elif execution.safety_decision.passed:
            consecutive_order_failures += 1
        execution_level = (
            "INFO"
            if execution.status.value not in {"REJECTED", "FAILED", "UNKNOWN"}
            else "WARN"
        )
        execution_payload = {
            "intent_id": execution.intent_id,
            "status": execution.status.value,
            "broker_order_id": execution.broker_order_id,
            "message": execution.message,
            "safety": execution.safety_decision.as_payload(),
            "symbol": live_intent.symbol,
            "side": live_intent.side.value,
            "quantity": str(live_intent.quantity),
            "notional": str(live_intent.notional) if live_intent.notional is not None else None,
            "account_alias": account_alias or "",
            "live_consent": consent_metadata,
        }
        store.record_runtime_event(
            execution_level,
            "live_order_execution",
            execution_payload,
        )
        if notifier is not None:
            notifier.notify(
                "live_order_execution",
                level=execution_level.lower(),
                payload=execution_payload,
            )
        if (
            config.live.cancel_after_ack
            and execution.status.value == "ACKNOWLEDGED"
            and execution.broker_order_id is not None
        ):
            cancel_execution = orchestrator.cancel_acknowledged(
                live_intent,
                broker_order_id=execution.broker_order_id,
            )
            store.record_runtime_event(
                "INFO"
                if cancel_execution.status.value == "PENDING_CANCEL"
                else "WARN",
                "live_order_cancel_after_ack",
                {
                    "intent_id": cancel_execution.intent_id,
                    "status": cancel_execution.status.value,
                    "broker_order_id": cancel_execution.broker_order_id,
                    "message": cancel_execution.message,
                },
            )


def run_live_smoke_test(
    *,
    config_path: str | Path,
    state_db: str | Path,
    env: Mapping[str, str] | None = None,
    transport: TossTransport | None = None,
    now=lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Submit one tiny live test order and cancel it immediately after ACK."""

    config = load_config(config_path)
    env_values = env if env is not None else environ
    blockers = _paper_service_config_blockers(config, env_values)
    if blockers:
        raise ValueError("; ".join(blockers))
    if _runtime_mode(config) != "live" or not config.live_enabled:
        raise ValueError("실주문 테스트는 live 모드와 toss.live_enabled=true에서만 가능합니다.")
    if config.live.emergency_stop:
        raise ValueError("거래 중지 상태입니다. 실주문 테스트를 하려면 먼저 거래 중지를 해제하세요.")
    if not config.live.cancel_after_ack:
        raise ValueError("실주문 테스트는 접수 즉시 취소 설정(cancel_after_ack=true)이 필요합니다.")

    symbol = (
        config.live.allowed_symbols[0]
        if config.live.allowed_symbols
        else config.runtime.symbols[0]
        if config.runtime.symbols
        else ""
    ).strip().upper()
    if not symbol:
        raise ValueError("실주문 테스트에 사용할 종목이 없습니다.")

    credentials = TossCredentials(
        client_id=env_values[config.toss.client_id_env],
        client_secret=env_values[config.toss.client_secret_env],
    )
    client = TossClient(
        credentials=credentials,
        account_seq=config.toss.account_seq,
        base_url=config.toss.base_url or TOSS_BASE_URL,
        transport=transport,
        rate_limits=RateLimitQueue(now=now),
        now=now,
    )

    with SQLiteStateStore(state_db) as store:
        session = None
        if config.runtime.use_market_calendar:
            session = MarketCalendarGate(
                client=client,
                config=MarketCalendarConfig(
                    market=config.runtime.market,
                    timezone_name=config.runtime.timezone_name,
                    open_session_names=config.runtime.market_calendar_open_sessions,
                ),
                now=now,
            ).current_session()
            store.record_runtime_event(
                "INFO" if session.is_open else "WARN",
                "market_session_state",
                session.as_payload(),
            )
            if config.live.require_market_open and not session.is_open:
                raise ValueError(f"시장 시간이 아닙니다: {session.status}")

        market_data = TossReadOnlyMarketDataProvider(
            client=client,
            config=TossMarketDataConfig(
                candle_interval=config.runtime.candle_interval,
                candle_count=config.runtime.candle_count,
                local_timezone=config.runtime.timezone_name,
                exclude_current_session=config.runtime.exclude_current_session,
            ),
            store=store,
            now=now,
        )
        current_price = market_data.get_current_price(symbol)
        limit_price = max(
            Decimal("0.01"),
            (current_price * Decimal("0.90")).quantize(Decimal("0.01")),
        )

        reconcile = TossPositionSync(
            client=client,
            store=store,
            sync_live_positions=True,
            sync_closed_orders=True,
        ).reconcile()
        currency = "USD" if str(config.runtime.market).strip().upper() == "US" else "KRW"
        buying_power = client.get_buying_power(currency)
        available_cash = _available_cash_from_buying_power(buying_power)
        position = store.load_position(symbol)
        daily_summary = store.execution_summary_since(
            _start_of_local_day(config.runtime.timezone_name)
        )
        sync_summary = _sync_unresolved_live_execution_orders(
            client=client,
            store=store,
        )
        consecutive_order_failures = _consecutive_live_order_failures(
            store,
            since=_start_of_local_day(config.runtime.timezone_name),
        )
        unresolved_order_exists = any(
            order.symbol == symbol for order in reconcile.open_orders
        )

        intent_id = f"live-smoke-{now().strftime('%Y%m%d%H%M%S')}-{symbol}"
        intent = OrderIntent(
            intent_id=intent_id,
            idempotency_key=_live_client_order_id(intent_id),
            symbol=symbol,
            side=Side.BUY,
            quantity=Decimal("1"),
            order_type=OrderType.LIMIT,
            limit_price=limit_price,
            source="dashboard:live-smoke-test",
            reason="manual_live_connection_test",
            created_at=now(),
            metadata={
                "current_price": str(current_price),
                "test_limit_price_ratio": "0.90",
                "cancel_after_ack": True,
            },
        )

        store.record_runtime_event(
            "INFO",
            "live_smoke_test_started",
            {
                "symbol": symbol,
                "side": intent.side.value,
                "quantity": str(intent.quantity),
                "current_price": str(current_price),
                "limit_price": str(limit_price),
            },
        )

        orchestrator = LiveOrderOrchestrator(
            safety=PreTradeSafety(
                PreTradeSafetyConfig(
                    live_enabled=config.live_enabled,
                    emergency_stop=config.live.emergency_stop,
                    allowed_symbols=config.live.allowed_symbols,
                    max_order_quantity=config.live.max_order_quantity,
                    max_order_notional=config.live.max_order_notional,
                    daily_order_count_limit=config.live.daily_order_count_limit,
                    daily_notional_limit=config.live.daily_notional_limit,
                    require_market_open=config.live.require_market_open,
                    require_clean_reconcile=config.live.require_clean_reconcile,
                    block_unresolved_orders=config.live.block_unresolved_orders,
                    max_consecutive_order_failures=config.live.max_consecutive_order_failures,
                )
            ),
            broker=TossLiveBrokerAdapter(
                client,
                confirm_high_value_order=config.live.confirm_high_value_order,
            ),
            store=store,
        )
        execution = orchestrator.submit(
            intent,
            context=PreTradeSafetyContext(
                market_open=bool(session is None or session.is_open),
                reconcile_clean=reconcile.clean,
                unresolved_order_exists=unresolved_order_exists,
                available_cash=available_cash,
                current_position_qty=position.total_qty if position is not None else Decimal("0"),
                daily_order_count=int(daily_summary["count"]),
                daily_notional=daily_summary["notional"],
                consecutive_order_failures=consecutive_order_failures,
                unresolved_execution_count=int(sync_summary["remaining_unresolved"]),
            ),
        )
        execution_payload = {
            "intent_id": execution.intent_id,
            "status": execution.status.value,
            "broker_order_id": execution.broker_order_id,
            "message": execution.message,
            "safety": execution.safety_decision.as_payload(),
            "symbol": intent.symbol,
            "side": intent.side.value,
            "quantity": str(intent.quantity),
            "notional": str(intent.notional) if intent.notional is not None else None,
            "limit_price": str(intent.limit_price),
            "account_alias": _account_alias_from_config_path(config_path),
            "test_order": True,
        }
        discord_notifier = DiscordTradeNotifier(env=env_values)
        store.record_runtime_event(
            "INFO" if execution.accepted else "WARN",
            "live_order_execution",
            execution_payload,
        )
        discord_notifier.notify(
            "live_order_execution",
            level="info" if execution.accepted else "warn",
            payload=execution_payload,
        )

        cancel_payload: dict[str, Any] | None = None
        if execution.status.value == "ACKNOWLEDGED" and execution.broker_order_id is not None:
            cancel_execution = orchestrator.cancel_acknowledged(
                intent,
                broker_order_id=execution.broker_order_id,
            )
            cancel_payload = {
                "intent_id": cancel_execution.intent_id,
                "status": cancel_execution.status.value,
                "broker_order_id": cancel_execution.broker_order_id,
                "message": cancel_execution.message,
                "test_order": True,
                "symbol": intent.symbol,
                "side": intent.side.value,
                "quantity": str(intent.quantity),
                "account_alias": _account_alias_from_config_path(config_path),
            }
            store.record_runtime_event(
                "INFO"
                if cancel_execution.status.value == "PENDING_CANCEL"
                else "WARN",
                "live_order_cancel_after_ack",
                cancel_payload,
            )
            discord_notifier.notify(
                "live_order_cancel_after_ack",
                level="info"
                if cancel_execution.status.value == "PENDING_CANCEL"
                else "warn",
                payload=cancel_payload,
            )

    return {
        "status": "completed" if execution.accepted else "blocked",
        "symbol": symbol,
        "quantity": str(intent.quantity),
        "current_price": str(current_price),
        "limit_price": str(limit_price),
        "execution": execution_payload,
        "cancel": cancel_payload,
    }


def _live_final_guard_blockers(
    *,
    config,
    live_intent: OrderIntent,
    position_qty: Decimal,
    unresolved_order_exists: bool,
    daily_summary: Mapping[str, Any],
    consecutive_order_failures: int = 0,
    unresolved_execution_count: int = 0,
) -> tuple[str, ...]:
    blockers: list[str] = []
    allowed_symbols = {str(symbol).upper() for symbol in config.live.allowed_symbols}
    if not config.live_enabled:
        blockers.append("live_disabled")
    if config.live.emergency_stop:
        blockers.append("emergency_stop_active")
    if allowed_symbols and live_intent.symbol.upper() not in allowed_symbols:
        blockers.append("symbol_not_allowlisted")
    return tuple(blockers)


def _sync_unresolved_live_execution_orders(
    *,
    client: TossClient,
    store: SQLiteStateStore,
    limit: int = 20,
) -> dict[str, Any]:
    unresolved = store.list_unresolved_execution_orders(limit=limit)
    adapter = TossLiveBrokerAdapter(client)
    checked = 0
    updated = 0
    failed = 0
    skipped = 0
    for order in unresolved:
        broker_order_id = order.get("broker_order_id")
        if not broker_order_id:
            skipped += 1
            continue
        checked += 1
        try:
            state = adapter.query_order(str(broker_order_id))
        except Exception as exc:
            failed += 1
            store.record_runtime_event(
                "WARN",
                "live_order_status_sync_failed",
                {
                    "intent_id": order["intent_id"],
                    "broker_order_id": broker_order_id,
                    "status": order["status"],
                    "error": str(exc),
                },
            )
            continue
        state_payload = state.as_payload()
        store.record_execution_order(
            intent_id=order["intent_id"],
            idempotency_key=order["idempotency_key"],
            symbol=order["symbol"],
            side=order["side"],
            status=state.status.value,
            broker_order_id=state.broker_order_id,
            raw={
                "order_state": state_payload,
                "previous_status": order["status"],
                "previous_raw": order.get("raw"),
            },
        )
        store.record_execution_event(
            intent_id=order["intent_id"],
            event_type="broker_status_sync",
            status=state.status.value,
            payload=state_payload,
        )
        updated += 1

    remaining = store.list_unresolved_execution_orders(limit=limit)
    summary = {
        "checked": checked,
        "updated": updated,
        "failed": failed,
        "skipped": skipped,
        "remaining_unresolved": len(remaining),
    }
    store.record_runtime_event(
        "INFO" if not remaining and not failed else "WARN",
        "live_order_status_synced",
        summary,
    )
    return summary


def _consecutive_live_order_failures(
    store: SQLiteStateStore,
    *,
    since: datetime,
    scan_limit: int = 100,
) -> int:
    count = 0
    for event in store.list_runtime_events(limit=scan_limit):
        if event["created_at"] < since:
            break
        if event["message"] != "live_order_execution":
            continue
        payload = event.get("payload") or {}
        if not isinstance(payload, Mapping):
            continue
        status = str(payload.get("status") or "").upper()
        safety = payload.get("safety") if isinstance(payload.get("safety"), Mapping) else {}
        safety_passed = bool(safety.get("passed")) if safety else False
        if status in {"FAILED", "UNKNOWN"} or (status == "REJECTED" and safety_passed):
            count += 1
            continue
        if status in {"ACKNOWLEDGED", "FILLED", "PARTIALLY_FILLED", "PENDING_CANCEL", "CANCELLED"}:
            break
    return count


def _live_order_intent_from_paper(intent: PaperOrderIntent) -> OrderIntent:
    system = TurtleSystem(intent.system) if intent.system else None
    return OrderIntent(
        intent_id=intent.intent_id,
        idempotency_key=_live_client_order_id(intent.intent_id),
        symbol=intent.symbol,
        side=Side(intent.side),
        quantity=intent.quantity,
        order_type=OrderType.LIMIT,
        limit_price=intent.fill_price,
        source=f"{intent.mode}:{intent.signal_kind}",
        reason=intent.reason,
        system=system,
        signal_id=intent.source_signal_id,
        created_at=intent.created_at,
        metadata=intent.as_payload(),
    )


def _live_client_order_id(intent_id: str) -> str:
    return "".join(char if char.isalnum() or char in "-_" else "-" for char in intent_id)[-36:]


def _available_cash_from_buying_power(payload: Mapping[str, Any]) -> Decimal | None:
    for key in ("cashBuyingPower", "buyingPower", "availableCash", "krw", "usd"):
        value = payload.get(key)
        if value is not None:
            return Decimal(str(value))
    return None


def _start_of_local_day(timezone_name: str) -> datetime:
    now_local = datetime.now(ZoneInfo(timezone_name))
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(timezone.utc)


def _should_build_watchlist(status: str) -> bool:
    return status.upper() in {"OPEN", "PREOPEN"}


def _build_and_save_watchlist(
    config,
    *,
    market_data: TossReadOnlyMarketDataProvider | None,
    client: TossClient | None,
    store: SQLiteStateStore,
    now,
    symbols: Sequence[str] | None = None,
) -> Watchlist | None:
    if not config.runtime.watchlist_enabled:
        return None
    provider = market_data
    if provider is None:
        if client is None:
            raise ValueError("market_data or client is required")
        provider = TossReadOnlyMarketDataProvider(
            client=client,
            config=TossMarketDataConfig(
                candle_interval=config.runtime.candle_interval,
                candle_count=config.runtime.candle_count,
                local_timezone=config.runtime.timezone_name,
                exclude_current_session=config.runtime.exclude_current_session,
            ),
            store=store,
            now=now,
        )

    previous = store.load_latest_watchlist(name=config.runtime.watchlist_name)
    previous_symbols = previous.symbols() if previous is not None else ()
    symbol_candles = {}
    blocked: list[str] = []
    for symbol in symbols or config.runtime.symbols:
        try:
            symbol_candles[symbol] = tuple(provider.get_completed_candles(symbol))
        except TossApiError as exc:
            blocked.append(f"{symbol} watchlist candles unavailable: {exc}")
            if exc.status == 429:
                break
        except Exception as exc:
            blocked.append(f"{symbol} watchlist candles unavailable: {exc}")

    if blocked:
        store.record_runtime_event(
            "WARN",
            "premarket_watchlist_blocked",
            {"blockers": blocked},
        )
    watchlist = WatchlistBuilder(
        top_n=config.runtime.watchlist_top_n,
        exclude_current=config.runtime.exclude_current_session,
    ).build(
        symbol_candles,
        previous_watchlist=previous_symbols,
        generated_at=now(),
    )
    store.save_watchlist(watchlist, name=config.runtime.watchlist_name)
    store.record_runtime_event(
        "INFO",
        "premarket_watchlist_generated",
        {
            "name": config.runtime.watchlist_name,
            "count": len(watchlist.rows),
            "symbols": list(watchlist.symbols()),
            "blocked": blocked,
        },
    )
    return watchlist


def _build_universe(
    config,
    *,
    client: TossClient,
    market_data: TossReadOnlyMarketDataProvider,
    store: SQLiteStateStore,
    now,
) -> Universe | None:
    if not config.runtime.universe_enabled:
        return None
    policy = UniversePolicy(
        candidate_symbols=config.runtime.universe_candidate_symbols,
        markets=(config.runtime.market,),
        include_etfs=config.runtime.universe_include_etfs,
        min_price=config.runtime.universe_min_price,
        min_average_daily_value=config.runtime.universe_min_average_daily_value,
        min_completed_candles=config.runtime.universe_min_completed_candles,
    )
    universe = UniverseBuilder(
        client=client,
        market_data=market_data,
        policy=policy,
        now=now,
    ).build()
    store.record_runtime_event(
        "INFO",
        "universe_generated",
        universe.as_payload(),
    )
    return universe


def _latest_watchlist_payload(
    store: SQLiteStateStore,
    *,
    name: str,
) -> tuple[dict[str, Any], ...]:
    watchlist = store.load_latest_watchlist(name=name)
    if watchlist is None:
        return ()
    return tuple(_watchlist_row_payload(row) for row in watchlist.rows)


def _watchlist_row_payload(row: WatchlistRow) -> dict[str, Any]:
    return {
        "symbol": row.symbol,
        "current_price": str(row.current_price),
        "entry_high_20": str(row.entry_high_20) if row.entry_high_20 is not None else None,
        "entry_high_55": str(row.entry_high_55) if row.entry_high_55 is not None else None,
        "distance_to_20": str(row.distance_to_20) if row.distance_to_20 is not None else None,
        "distance_to_55": str(row.distance_to_55) if row.distance_to_55 is not None else None,
        "nearest_distance": str(row.nearest_distance),
        "reason": row.reason,
        "is_new": row.is_new,
    }


def _with_latest_watchlist(
    snapshot: HealthSnapshot,
    store: SQLiteStateStore,
    *,
    name: str,
) -> HealthSnapshot:
    return HealthSnapshot(
        mode=snapshot.mode,
        ready=snapshot.ready,
        blockers=snapshot.blockers,
        positions=snapshot.positions,
        open_orders=snapshot.open_orders,
        watchlist=_latest_watchlist_payload(store, name=name),
        generated_at=snapshot.generated_at,
    )
