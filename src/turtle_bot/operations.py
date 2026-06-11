from __future__ import annotations

import plistlib
import sys
import time
from os import environ
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .config import load_config
from .health import HealthSnapshot
from .market_calendar import MarketCalendarConfig, MarketCalendarGate
from .notifier import MemoryNotifier
from .paper_runtime import PaperRuntimeConfig, PaperTradingRuntime
from .position_sync import TossPositionSync
from .state_store import SQLiteStateStore
from .toss_client import TOSS_BASE_URL, TossClient, TossCredentials, TossTransport
from .toss_market_data import TossMarketDataConfig, TossReadOnlyMarketDataProvider


DEFAULT_SERVICE_LABEL = "com.sands15.toss-turtle-bot"


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
) -> tuple[OperationsCheck, ...]:
    checks: list[OperationsCheck] = []
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
            checks.append(
                OperationsCheck(
                    "live_disabled",
                    not config.live_enabled,
                    "live trading disabled"
                    if not config.live_enabled
                    else "live trading is enabled; paper service refuses this config",
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


def paper_service_health(
    store: SQLiteStateStore,
    *,
    blockers: Sequence[str] = ("market_data_provider_not_configured",),
    ready: bool = False,
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
        mode="paper",
        ready=ready,
        blockers=tuple(blockers),
        positions=positions,
        open_orders=(),
        watchlist=(),
        generated_at=datetime.now(timezone.utc),
    )


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
    now=lambda: datetime.now(timezone.utc),
) -> HealthSnapshot:
    config = load_config(config_path)
    if config.live_enabled:
        raise RuntimeError("paper service refuses configs with toss.live_enabled=true")

    ensure_runtime_dirs(state_db=state_db, log_dir=log_dir)
    store = SQLiteStateStore(state_db)
    store.record_runtime_event(
        "INFO",
        "paper_service_started",
        {"mode": "paper", "interval_seconds": interval_seconds},
    )

    env_values = env if env is not None else environ
    snapshot = _paper_service_iteration(
        config_path=config_path,
        store=store,
        env=env_values,
        transport=transport,
        now=now,
    )
    if once:
        store.record_runtime_event("INFO", "paper_service_heartbeat", snapshot.as_payload())
        return snapshot

    while True:  # pragma: no cover - exercised by launchd, not unit tests
        snapshot = _paper_service_iteration(
            config_path=config_path,
            store=store,
            env=env_values,
            transport=transport,
            now=now,
        )
        store.record_runtime_event(
            "INFO",
            "paper_service_heartbeat",
            snapshot.as_payload(),
        )
        sleep(interval_seconds)


def _paper_service_iteration(
    *,
    config_path: str | Path,
    store: SQLiteStateStore,
    env: Mapping[str, str],
    transport: TossTransport | None,
    now,
) -> HealthSnapshot:
    config = load_config(config_path)
    blockers = _paper_service_config_blockers(config, env)
    if blockers:
        snapshot = paper_service_health(store, blockers=blockers)
        store.record_runtime_event(
            "WARN",
            "paper_service_blocked",
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
        now=now,
    )
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
        if session.blocker is not None:
            snapshot = paper_service_health(
                store,
                blockers=(session.blocker,),
                ready=False,
            )
            store.record_runtime_event(
                "WARN",
                "paper_service_market_closed",
                snapshot.as_payload() | {"market_session": session.as_payload()},
            )
            return snapshot

    market_data = TossReadOnlyMarketDataProvider(
        client=client,
        config=TossMarketDataConfig(
            candle_interval=config.runtime.candle_interval,
            candle_count=config.runtime.candle_count,
            exclude_current_session=config.runtime.exclude_current_session,
        ),
        store=store,
        now=now,
    )
    runtime = PaperTradingRuntime(
        config=PaperRuntimeConfig(
            symbols=config.runtime.symbols,
            minimum_tick=config.minimum_tick,
            n_method=config.n_method,
            stop_n=config.stop_n,
            max_units_per_symbol=config.max_units_per_symbol,
            pyramid_step_n=config.pyramid_step_n,
        ),
        market_data=market_data,
        position_sync=TossPositionSync(client=client, store=store),
        store=store,
        notifier=MemoryNotifier(),
        now=now,
    )
    runtime.run_once()
    return runtime.health_snapshot()


def _paper_service_config_blockers(config, env: Mapping[str, str]) -> tuple[str, ...]:
    blockers: list[str] = []
    if config.runtime.mode.lower() != "paper":
        blockers.append(f"runtime.mode must be paper, got {config.runtime.mode}")
    if not config.runtime.symbols:
        blockers.append("runtime.symbols is empty")
    if not env.get(config.toss.client_id_env):
        blockers.append(f"{config.toss.client_id_env} is not configured")
    if not env.get(config.toss.client_secret_env):
        blockers.append(f"{config.toss.client_secret_env} is not configured")
    if config.toss.account_seq is None:
        blockers.append("toss.account_seq is not configured")
    return tuple(blockers)
