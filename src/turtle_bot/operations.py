from __future__ import annotations

import json
import plistlib
import subprocess
import sys
import time
from os import environ
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, Callable, Mapping, Sequence
from decimal import Decimal
from zoneinfo import ZoneInfo

import yaml
from .config import load_config
from .health import HealthServer, HealthSnapshot
from .market_calendar import MarketCalendarConfig, MarketCalendarGate
from .notifier import DiscordTradeNotifier, MemoryNotifier
from .paper_runtime import PaperOrderIntent, PaperRuntimeConfig, PaperTradingRuntime
from .position_sync import TossPositionSync
from .state_store import SQLiteStateStore
from .rate_limit import RateLimitQueue
from .toss_client import TOSS_BASE_URL, TossApiError, TossClient, TossCredentials, TossTransport
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


class DashboardTradingLoopStopped(RuntimeError):
    """Raised internally to stop the dashboard-managed trading loop."""


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
    webhook_env = DiscordTradeNotifier.DEFAULT_WEBHOOK_ENV
    if not values.get(webhook_env):
        user_value = _windows_user_environment_value(webhook_env)
        if user_value:
            values[webhook_env] = user_value
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
            "discord_webhook_configured": bool(
                env_values.get(DiscordTradeNotifier.DEFAULT_WEBHOOK_ENV)
            ),
            "discord_webhook_env": DiscordTradeNotifier.DEFAULT_WEBHOOK_ENV,
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
    now=lambda: datetime.now(timezone.utc),
) -> HealthSnapshot:
    config = load_config(config_path)
    service_mode = _runtime_mode(config)
    if config.live_enabled and service_mode != "live":
        raise RuntimeError("paper/shadow service refuses configs with toss.live_enabled=true")

    ensure_runtime_dirs(state_db=state_db, log_dir=log_dir)
    store = SQLiteStateStore(state_db)
    store.record_runtime_event(
        "INFO",
        f"{service_mode}_service_started",
        {"mode": service_mode, "interval_seconds": interval_seconds},
    )

    env_values = env if env is not None else environ
    rate_limits = RateLimitQueue(now=now)
    snapshot = _paper_service_iteration(
        config_path=config_path,
        store=store,
        env=env_values,
        live_consent=live_consent,
        transport=transport,
        rate_limits=rate_limits,
        now=now,
    )
    if once:
        store.record_runtime_event(
            "INFO",
            f"{service_mode}_service_heartbeat",
            snapshot.as_payload(),
        )
        return snapshot

    while True:  # pragma: no cover - exercised by launchd, not unit tests
        snapshot = _paper_service_iteration(
            config_path=config_path,
            store=store,
            env=env_values,
            live_consent=live_consent,
            transport=transport,
            rate_limits=rate_limits,
            now=now,
        )
        store.record_runtime_event(
            "INFO",
            f"{service_mode}_service_heartbeat",
            snapshot.as_payload(),
        )
        sleep(interval_seconds)


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
    config_path: str | Path,
    store: SQLiteStateStore,
    env: Mapping[str, str],
    transport: TossTransport | None,
    rate_limits: RateLimitQueue | None = None,
    live_consent: Mapping[str, str] | None = None,
    now,
) -> HealthSnapshot:
    config = load_config(config_path)
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
    return tuple(blockers)


def _runtime_mode(config) -> str:
    mode = str(config.runtime.mode or "").strip().lower()
    return mode or "paper"


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
    has_symbols = bool(config.runtime.symbols or config.runtime.universe_candidate_symbols)
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
            "runtime.symbols or runtime.universe_candidate_symbols is configured"
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
