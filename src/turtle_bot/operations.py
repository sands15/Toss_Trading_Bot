from __future__ import annotations

import plistlib
import sys
import time
from os import environ
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from decimal import Decimal

import yaml
from .config import load_config
from .health import HealthServer, HealthSnapshot
from .market_calendar import MarketCalendarConfig, MarketCalendarGate
from .notifier import MemoryNotifier
from .paper_runtime import PaperRuntimeConfig, PaperTradingRuntime
from .position_sync import TossPositionSync
from .state_store import SQLiteStateStore
from .toss_client import TOSS_BASE_URL, TossClient, TossCredentials, TossTransport
from .toss_market_data import TossMarketDataConfig, TossReadOnlyMarketDataProvider
from .universe import Universe, UniverseBuilder, UniversePolicy
from .watchlist import Watchlist, WatchlistBuilder, WatchlistRow


DEFAULT_SERVICE_LABEL = "com.sands15.toss-turtle-bot"
DEFAULT_DASHBOARD_BLOCKERS = (
    "runtime.mode must be paper or shadow",
    "runtime.symbols or runtime.universe_candidate_symbols is required",
    "TOSS_CLIENT_ID is not configured",
    "TOSS_CLIENT_SECRET is not configured",
    "toss.account_seq is not configured",
)


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


def build_dashboard_server(
    *,
    state_db: str | Path,
    config_path: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    start_server: bool = False,
    env: Mapping[str, str] | None = None,
) -> HealthServer:
    watchlist_name = "premarket"
    default_blockers: Sequence[str] = DEFAULT_DASHBOARD_BLOCKERS
    settings_payload: Mapping[str, Any] = {}
    settings_updater: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None
    if config_path is not None:
        env_values = env if env is not None else environ
        config = load_config(config_path)
        watchlist_name = config.runtime.watchlist_name
        default_blockers = _paper_service_config_blockers(
            config,
            env_values,
        )
        settings_payload = _dashboard_settings_payload(config, env_values)

        def settings_updater(payload: Mapping[str, Any]) -> Mapping[str, Any]:
            nonlocal config, default_blockers, settings_payload, watchlist_name
            result = update_dashboard_settings(config_path, payload, env=env_values)
            config = load_config(config_path)
            watchlist_name = config.runtime.watchlist_name
            default_blockers = _paper_service_config_blockers(config, env_values)
            settings_payload = _dashboard_settings_payload(config, env_values)
            return {"config": result, "settings": settings_payload}

    def snapshot_provider() -> HealthSnapshot:
        with SQLiteStateStore(state_db) as store:
            events = store.list_runtime_events(limit=100)
            latest = _latest_health_event_payload(events)
            if latest is None:
                return paper_service_health(
                    store,
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

    return HealthServer(
        snapshot_provider,
        events_provider=events_provider,
        host=host,
        port=port,
        start_server=start_server,
        settings=settings_payload,
        settings_updater=settings_updater,
    )


def _dashboard_settings_payload(
    config,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    env_values = env if env is not None else environ
    client_id_env = str(config.toss.client_id_env or "").strip()
    client_secret_env = str(config.toss.client_secret_env or "").strip()
    return {
        "strategy_kind": config.strategy_kind,
        "runtime": {
            "mode": config.runtime.mode,
            "market": config.runtime.market,
            "timezone": config.runtime.timezone_name,
        },
        "toss": {
            "live_enabled": config.live_enabled,
            "account_seq_configured": bool(config.toss.account_seq),
            "account_seq": config.toss.account_seq or "",
            "client_id_env": client_id_env,
            "client_secret_env": client_secret_env,
            "client_id_configured": bool(client_id_env and env_values.get(client_id_env)),
            "client_secret_configured": bool(
                client_secret_env and env_values.get(client_secret_env)
            ),
            "required_env": [
                config.toss.client_id_env,
                config.toss.client_secret_env,
            ],
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
    build_dashboard_server(
        state_db=state_db,
        config_path=config_path,
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
    service_mode = _runtime_mode(config)

    ensure_runtime_dirs(state_db=state_db, log_dir=log_dir)
    store = SQLiteStateStore(state_db)
    store.record_runtime_event(
        "INFO",
        f"{service_mode}_service_started",
        {"mode": service_mode, "interval_seconds": interval_seconds},
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
            transport=transport,
            now=now,
        )
        store.record_runtime_event(
            "INFO",
            f"{service_mode}_service_heartbeat",
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
    service_mode = _runtime_mode(config)
    blockers = _paper_service_config_blockers(config, env)
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
            simulate_fills=True,
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
        position_sync=TossPositionSync(client=client, store=store),
        store=store,
        notifier=MemoryNotifier(),
        now=now,
    )
    runtime.run_once()
    return _with_latest_watchlist(
        runtime.health_snapshot(),
        store,
        name=config.runtime.watchlist_name,
    )


def _paper_service_config_blockers(config, env: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(
        check.message
        for check in _build_toss_readiness_checks(config=config, env=env)
        if not check.passed
    )


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
    mode_is_supported = mode in {"paper", "shadow"}

    return (
        OperationsCheck(
            "runtime_mode",
            mode_is_supported,
            f"runtime.mode is {mode}"
            if mode_is_supported
            else f"runtime.mode must be paper or shadow, got {config.runtime.mode}",
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
    )


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
