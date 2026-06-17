#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hmac
import html
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from email.header import decode_header, make_header
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error, parse, request


DEFAULT_IMAGE = "toss-trading-bot:local"
DEFAULT_REGISTRY = ".local/users/registry.json"
DEFAULT_USER_ROOT = ".local/users"
DEFAULT_AUDIT_LOG = ".local/users/audit.log"
DEFAULT_CONTAINER_PORT = 8765
DEFAULT_FIRST_PORT = 19000
SETUP_CONFIRMATION = "토스 연결 승인"
SETUP_CSRF_COOKIE = "toss_gateway_setup"
MAX_SETUP_BODY_BYTES = 32 * 1024
DEFAULT_SETUP_RATE_LIMIT = 5
DEFAULT_SETUP_RATE_WINDOW_SECONDS = 15 * 60
DEFAULT_CONTAINER_MEMORY = "512m"
DEFAULT_CONTAINER_CPUS = "1.0"
DEFAULT_CONTAINER_LOG_MAX_SIZE = "10m"
DEFAULT_CONTAINER_LOG_MAX_FILES = "3"
DEFAULT_KEYCHAIN_SERVICE = "toss-trading-bot"
SECRET_CLIENT_ID = "toss_client_id"
SECRET_CLIENT_SECRET = "toss_client_secret"
TAILSCALE_USER_LOGIN_HEADER = "Tailscale-User-Login"
TAILSCALE_USER_NAME_HEADER = "Tailscale-User-Name"
DELETE_SECRETS_CONFIRMATION = "DELETE_USER_SECRETS"
CLEANUP_ORPHANS_CONFIRMATION = "CLEANUP_ORPHANS"
OFFBOARD_USER_CONFIRMATION = "OFFBOARD_USER"
USER_CONTAINER_PREFIX = "toss-dashboard-"


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", value.strip().lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "user"


def normalize_tailscale_identity(value: str) -> str:
    return value.strip().lower()


def clean_env_value(value: str) -> str:
    cleaned = value.strip()
    if "\n" in cleaned or "\r" in cleaned:
        raise ValueError("credential values cannot contain new lines")
    return cleaned


def make_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def decode_tailscale_header(value: str) -> str:
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:
        return value.strip()


def tailscale_identity_from_headers(headers: Any) -> dict[str, str] | None:
    raw_login = headers.get(TAILSCALE_USER_LOGIN_HEADER) if headers is not None else None
    if not raw_login:
        return None
    login = normalize_tailscale_identity(decode_tailscale_header(str(raw_login)))
    if not login or "@" not in login:
        return None
    raw_name = headers.get(TAILSCALE_USER_NAME_HEADER) if headers is not None else None
    display_name = decode_tailscale_header(str(raw_name)).strip() if raw_name else login
    return {"identity": login, "display_name": display_name or login}


def cookie_value(cookie_header: str | None, name: str) -> str:
    if not cookie_header:
        return ""
    for part in cookie_header.split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key == name:
            return value.strip()
    return ""


def csrf_is_valid(cookie_header: str | None, form_token: str) -> bool:
    cookie_token = cookie_value(cookie_header, SETUP_CSRF_COOKIE)
    return bool(cookie_token and form_token and hmac.compare_digest(cookie_token, form_token))


def parse_content_length(value: str | None, *, max_bytes: int) -> int:
    try:
        length = int(value or "0")
    except ValueError as exc:
        raise ValueError("invalid content length") from exc
    if length < 0:
        raise ValueError("invalid content length")
    if length > max_bytes:
        raise ValueError("setup request is too large")
    return length


def parse_allowlist(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def client_ip_is_allowed(client_ip: str, allowlist: tuple[str, ...]) -> bool:
    if not allowlist:
        return True
    try:
        address = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for entry in allowlist:
        try:
            if "/" in entry:
                if address in ipaddress.ip_network(entry, strict=False):
                    return True
            elif address == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue
    return False


def load_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "version": 3,
            "next_port": DEFAULT_FIRST_PORT,
            "ip_map": {},
            "login_map": {},
            "identity_map": {},
            "users": {},
        }
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"invalid registry: {path}")
    data.setdefault("version", 3)
    data.setdefault("next_port", DEFAULT_FIRST_PORT)
    data.setdefault("ip_map", {})
    data.setdefault("login_map", {})
    data.setdefault("identity_map", {})
    data.setdefault("users", {})
    return data


def save_registry(path: Path, registry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(registry, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    tmp.replace(path)


def append_audit_event(audit_log_path: Path, event: str, **fields: Any) -> None:
    audit_log_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "time": int(time.time()),
        "event": event,
        **fields,
    }
    with audit_log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        handle.write("\n")


def read_recent_audit_events(path: Path, *, limit: int = 20) -> list[dict[str, Any]]:
    if limit <= 0 or not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def registry_public_view(registry: dict[str, Any]) -> dict[str, Any]:
    users = registry.get("users", {})
    return {
        "version": registry.get("version", 1),
        "next_port": registry.get("next_port", DEFAULT_FIRST_PORT),
        "ip_map": dict(registry.get("ip_map", {})),
        "users": {
            slug: {
                key: value
                for key, value in dict(user).items()
                if key
                in {
                    "slug",
                    "display_name",
                    "client_ip",
                    "container_name",
                    "port",
                    "created_at",
                    "status",
                    "user_root",
                    "tailscale_identity",
                    "last_client_ip",
                    "secret_backend",
                }
            }
            for slug, user in users.items()
            if isinstance(user, dict)
        },
    }


def unmap_ip(registry: dict[str, Any], client_ip: str) -> str | None:
    return registry.get("ip_map", {}).pop(client_ip, None)


def delete_user(registry: dict[str, Any], slug: str) -> dict[str, Any] | None:
    removed = registry.get("users", {}).pop(slug, None)
    ip_map = registry.get("ip_map", {})
    for ip, mapped_slug in list(ip_map.items()):
        if mapped_slug == slug:
            ip_map.pop(ip, None)
    login_map = registry.get("login_map", {})
    for login_id, mapped_slug in list(login_map.items()):
        if mapped_slug == slug:
            login_map.pop(login_id, None)
    identity_map = registry.get("identity_map", {})
    for identity, mapped_slug in list(identity_map.items()):
        if mapped_slug == slug:
            identity_map.pop(identity, None)
    return dict(removed) if isinstance(removed, dict) else None


def registered_user_slugs(registry: dict[str, Any]) -> set[str]:
    return {str(slug) for slug in registry.get("users", {})}


def registered_container_names(registry: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for user in registry.get("users", {}).values():
        if isinstance(user, dict) and user.get("container_name"):
            names.add(str(user["container_name"]))
    return names


def list_gateway_containers(repo_root: Path, *, prefix: str = USER_CONTAINER_PREFIX) -> list[str]:
    result = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.Names}}"],
        cwd=str(repo_root),
        check=True,
        capture_output=True,
        text=True,
    )
    return sorted(name for name in result.stdout.splitlines() if name.startswith(prefix))


def list_gateway_container_statuses(repo_root: Path, *, prefix: str = USER_CONTAINER_PREFIX) -> list[dict[str, str]]:
    result = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{json .}}"],
        cwd=str(repo_root),
        check=True,
        capture_output=True,
        text=True,
    )
    containers: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        name = str(item.get("Names", ""))
        if not name.startswith(prefix):
            continue
        containers.append(
            {
                "name": name,
                "state": str(item.get("State", "")),
                "status": str(item.get("Status", "")),
            }
        )
    return sorted(containers, key=lambda item: item["name"])


def list_user_dirs(users_root: Path) -> list[str]:
    if not users_root.exists():
        return []
    return sorted(
        item.name
        for item in users_root.iterdir()
        if item.is_dir() and not item.name.startswith(".") and item.name != "_trash"
    )


def find_orphan_resources(registry: dict[str, Any], users_root: Path, repo_root: Path) -> dict[str, list[str]]:
    user_slugs = registered_user_slugs(registry)
    container_names = registered_container_names(registry)
    containers = list_gateway_containers(repo_root)
    user_dirs = list_user_dirs(users_root)
    return {
        "orphan_containers": [name for name in containers if name not in container_names],
        "stale_user_dirs": [name for name in user_dirs if name not in user_slugs],
    }


def trash_user_dir(users_root: Path, slug: str) -> Path:
    source = users_root / slug
    if not source.exists():
        return source
    trash_root = users_root / "_trash"
    trash_root.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    target = trash_root / f"{slug}-{timestamp}"
    suffix = 2
    while target.exists():
        target = trash_root / f"{slug}-{timestamp}-{suffix}"
        suffix += 1
    shutil.move(str(source), str(target))
    return target


def next_available_port(registry: dict[str, Any]) -> int:
    used = {int(user.get("port", 0)) for user in registry.get("users", {}).values()}
    port = int(registry.get("next_port") or DEFAULT_FIRST_PORT)
    while port in used:
        port += 1
    registry["next_port"] = port + 1
    return port


def unique_slug(registry: dict[str, Any], requested: str) -> str:
    base = slugify(requested)
    users = registry.get("users", {})
    if base not in users:
        return base
    suffix = 2
    while f"{base}-{suffix}" in users:
        suffix += 1
    return f"{base}-{suffix}"


def write_env_file(path: Path, client_id: str, client_secret: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "# User-specific Toss API credentials.",
                "# This file is local-only and ignored by git.",
                f"TOSS_CLIENT_ID={clean_env_value(client_id)}",
                f"TOSS_CLIENT_SECRET={clean_env_value(client_secret)}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def write_placeholder_env_file(path: Path, backend: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "# Toss credentials are not stored in this file.",
                f"# Secret backend: {backend}",
                "# The gateway injects TOSS_CLIENT_ID and TOSS_CLIENT_SECRET at container start.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass


class SecretStore:
    backend_name = "base"

    def put_user_credentials(self, user_slug: str, *, client_id: str, client_secret: str) -> None:
        raise NotImplementedError

    def get_user_credentials(self, user_slug: str) -> dict[str, str]:
        raise NotImplementedError

    def delete_user_credentials(self, user_slug: str) -> None:
        raise NotImplementedError

    def import_user_env_if_needed(self, user_slug: str, env_file: Path) -> bool:
        if not env_file.exists():
            return False
        try:
            existing = self.get_user_credentials(user_slug)
        except Exception:
            existing = {}
        if existing.get("TOSS_CLIENT_ID") and existing.get("TOSS_CLIENT_SECRET"):
            return False
        values = read_env_file(env_file)
        client_id = values.get("TOSS_CLIENT_ID", "")
        client_secret = values.get("TOSS_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            return False
        self.put_user_credentials(user_slug, client_id=client_id, client_secret=client_secret)
        write_placeholder_env_file(env_file, self.backend_name)
        return True


class FileSecretStore(SecretStore):
    backend_name = "file"

    def __init__(self, users_root: Path) -> None:
        self.users_root = users_root

    def env_file(self, user_slug: str) -> Path:
        return self.users_root / user_slug / ".env"

    def put_user_credentials(self, user_slug: str, *, client_id: str, client_secret: str) -> None:
        write_env_file(self.env_file(user_slug), client_id, client_secret)

    def get_user_credentials(self, user_slug: str) -> dict[str, str]:
        values = read_env_file(self.env_file(user_slug))
        return {
            "TOSS_CLIENT_ID": values.get("TOSS_CLIENT_ID", ""),
            "TOSS_CLIENT_SECRET": values.get("TOSS_CLIENT_SECRET", ""),
        }

    def delete_user_credentials(self, user_slug: str) -> None:
        try:
            self.env_file(user_slug).unlink()
        except FileNotFoundError:
            pass


class KeychainSecretStore(SecretStore):
    backend_name = "keychain"

    def __init__(self, service: str = DEFAULT_KEYCHAIN_SERVICE) -> None:
        self.service = service
        if shutil.which("security") is None:
            raise RuntimeError("macOS security command was not found")

    def account_name(self, user_slug: str, name: str) -> str:
        return f"{user_slug}:{name}"

    def put(self, user_slug: str, name: str, value: str) -> None:
        cleaned = clean_env_value(value)
        subprocess.run(
            [
                "security",
                "add-generic-password",
                "-a",
                self.account_name(user_slug, name),
                "-s",
                self.service,
                "-w",
                cleaned,
                "-U",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def get(self, user_slug: str, name: str) -> str:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-a",
                self.account_name(user_slug, name),
                "-s",
                self.service,
                "-w",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.rstrip("\r\n")

    def delete(self, user_slug: str, name: str) -> None:
        subprocess.run(
            [
                "security",
                "delete-generic-password",
                "-a",
                self.account_name(user_slug, name),
                "-s",
                self.service,
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def put_user_credentials(self, user_slug: str, *, client_id: str, client_secret: str) -> None:
        self.put(user_slug, SECRET_CLIENT_ID, client_id)
        self.put(user_slug, SECRET_CLIENT_SECRET, client_secret)

    def get_user_credentials(self, user_slug: str) -> dict[str, str]:
        return {
            "TOSS_CLIENT_ID": self.get(user_slug, SECRET_CLIENT_ID),
            "TOSS_CLIENT_SECRET": self.get(user_slug, SECRET_CLIENT_SECRET),
        }

    def delete_user_credentials(self, user_slug: str) -> None:
        self.delete(user_slug, SECRET_CLIENT_ID)
        self.delete(user_slug, SECRET_CLIENT_SECRET)


def resolve_secret_backend(requested: str) -> str:
    backend = (requested or "auto").strip().lower()
    if backend == "auto":
        return "keychain" if sys.platform == "darwin" else "file"
    if backend not in {"file", "keychain"}:
        raise ValueError("secret backend must be auto, file, or keychain")
    return backend


def make_secret_store(backend: str, users_root: Path, *, keychain_service: str = DEFAULT_KEYCHAIN_SERVICE) -> SecretStore:
    resolved = resolve_secret_backend(backend)
    if resolved == "keychain":
        return KeychainSecretStore(keychain_service)
    return FileSecretStore(users_root)


def write_config_file(path: Path, account_seq: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""toss:
  live_enabled: false
  account_seq: "{clean_env_value(account_seq)}"
  client_id_env: TOSS_CLIENT_ID
  client_secret_env: TOSS_CLIENT_SECRET
ai:
  enabled: false
  provider: openai_compatible
  model: bRadu/gemma-4-E2B-it-textonly
  base_url: http://localhost:8000/v1
  api_key_env: TURTLE_AI_API_KEY
  timeout_seconds: 30
  max_tokens: 700
  temperature: 0.2
runtime:
  mode: shadow
  market: US
  timezone: America/New_York
  use_market_calendar: true
  state_db: state/turtle.sqlite3
  log_dir: logs
  interval_seconds: 60
  candle_interval: 1d
  candle_count: 320
  exclude_current_session: true
  watchlist_enabled: true
  watchlist_top_n: 20
  watchlist_name: premarket
  universe_enabled: false
  universe_candidate_symbols: []
  universe_include_etfs: true
  universe_min_price: 5
  universe_min_average_daily_value: 50000000
  universe_min_completed_candles: 275
  symbols:
    - QQQ
    - SPY
    - XLK
    - SMH
    - NVDA
    - MSFT
    - AAPL
    - AVGO
    - AMD
    - META
strategy:
  kind: momentum
  minimum_tick: 0.01
  n_method: turtle
  momentum:
    market_symbol: SPY
    lookback_days: 126
    skip_days: 21
    trend_ma_days: 200
    exit_ma_days: 75
    max_positions: 5
    cash_reserve_pct: 0.50
    accept_top_n: 2
    target_position_pct: 0.10
    min_price: 5
    min_average_daily_value: 50000000
    average_daily_value_days: 20
    use_market_filter: true
  risk:
    risk_pct_per_unit: 0.005
    stop_n: 2.0
    pyramid_step_n: 0.5
    max_units_per_symbol: 4
    max_total_long_units: 12
    max_total_short_units: 12
""",
        encoding="utf-8",
    )


def run_command(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    subprocess.run(args, cwd=str(cwd), check=True, env=env)


@dataclass(frozen=True)
class GatewayConfig:
    repo_root: Path
    registry_path: Path
    users_root: Path
    image_name: str
    container_port: int
    audit_log_path: Path | None = None
    secret_backend: str = "auto"
    keychain_service: str = DEFAULT_KEYCHAIN_SERVICE
    setup_rate_limit: int = DEFAULT_SETUP_RATE_LIMIT
    setup_rate_window_seconds: int = DEFAULT_SETUP_RATE_WINDOW_SECONDS
    registration_allowlist: tuple[str, ...] = ()
    container_memory: str = DEFAULT_CONTAINER_MEMORY
    container_cpus: str = DEFAULT_CONTAINER_CPUS
    container_log_max_size: str = DEFAULT_CONTAINER_LOG_MAX_SIZE
    container_log_max_files: str = DEFAULT_CONTAINER_LOG_MAX_FILES


class UserGateway:
    def __init__(self, config: GatewayConfig) -> None:
        self.config = config
        self.audit_log_path = config.audit_log_path or config.registry_path.parent / "audit.log"
        self.secret_store = make_secret_store(
            config.secret_backend,
            config.users_root,
            keychain_service=config.keychain_service,
        )
        self._registry_lock = threading.Lock()
        self._setup_attempts_lock = threading.Lock()
        self._setup_attempts: dict[str, list[float]] = {}

    def ensure_image(self) -> None:
        run_command(["docker", "build", "-t", self.config.image_name, "."], cwd=self.config.repo_root)

    def remove_container(self, container_name: str) -> None:
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            cwd=str(self.config.repo_root),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def audit(self, event: str, **fields: Any) -> None:
        if "path" in fields:
            fields["request_path"] = fields.pop("path")
        append_audit_event(self.audit_log_path, event, **fields)

    def registration_allowed(self, client_ip: str) -> bool:
        return client_ip_is_allowed(client_ip, self.config.registration_allowlist)

    def consume_setup_attempt(self, client_ip: str, *, now: float | None = None) -> tuple[bool, int]:
        limit = int(self.config.setup_rate_limit)
        window = int(self.config.setup_rate_window_seconds)
        if limit <= 0 or window <= 0:
            return True, 0

        timestamp = time.time() if now is None else now
        cutoff = timestamp - window
        with self._setup_attempts_lock:
            attempts = [item for item in self._setup_attempts.get(client_ip, []) if item >= cutoff]
            if len(attempts) >= limit:
                retry_after = max(1, int(window - (timestamp - attempts[0])))
                self._setup_attempts[client_ip] = attempts
                return False, retry_after
            attempts.append(timestamp)
            self._setup_attempts[client_ip] = attempts
        return True, 0

    def user_for_ip(self, client_ip: str) -> dict[str, Any] | None:
        with self._registry_lock:
            registry = load_registry(self.config.registry_path)
        slug = registry.get("ip_map", {}).get(client_ip)
        if not slug:
            return None
        user = registry.get("users", {}).get(slug)
        return dict(user) if isinstance(user, dict) else None

    def user_for_identity(self, identity: str) -> dict[str, Any] | None:
        normalized_identity = normalize_tailscale_identity(identity)
        with self._registry_lock:
            registry = load_registry(self.config.registry_path)
        slug = registry.get("identity_map", {}).get(normalized_identity)
        if not slug:
            return None
        user = registry.get("users", {}).get(slug)
        return dict(user) if isinstance(user, dict) else None

    def user_by_slug(self, slug: str) -> dict[str, Any]:
        with self._registry_lock:
            registry = load_registry(self.config.registry_path)
        user = registry.get("users", {}).get(slug)
        if not isinstance(user, dict):
            raise ValueError(f"unknown user: {slug}")
        return dict(user)

    def update_user_status(self, slug: str, status: str) -> dict[str, Any]:
        with self._registry_lock:
            registry = load_registry(self.config.registry_path)
            user = registry.get("users", {}).get(slug)
            if not isinstance(user, dict):
                raise ValueError(f"unknown user: {slug}")
            user["status"] = status
            registry["users"][slug] = user
            save_registry(self.config.registry_path, registry)
            return dict(user)

    def update_user_secret_backend(self, slug: str) -> None:
        with self._registry_lock:
            registry = load_registry(self.config.registry_path)
            user = registry.get("users", {}).get(slug)
            if isinstance(user, dict):
                user["secret_backend"] = self.secret_store.backend_name
                registry["users"][slug] = user
                save_registry(self.config.registry_path, registry)

    def record_user_client_ip(self, slug: str, client_ip: str) -> None:
        with self._registry_lock:
            registry = load_registry(self.config.registry_path)
            user = registry.get("users", {}).get(slug)
            if isinstance(user, dict):
                user["last_client_ip"] = client_ip
                registry["users"][slug] = user
                save_registry(self.config.registry_path, registry)

    def stop_user(self, slug: str) -> dict[str, Any]:
        user = self.user_by_slug(slug)
        run_command(["docker", "stop", str(user["container_name"])], cwd=self.config.repo_root)
        updated = self.update_user_status(slug, "stopped")
        self.audit("container_stopped", slug=slug, container_name=str(user["container_name"]))
        return updated

    def start_user(self, slug: str) -> dict[str, Any]:
        user = self.user_by_slug(slug)
        self.ensure_container(user)
        updated = self.update_user_status(slug, "running")
        self.audit("container_started", slug=slug, container_name=str(user["container_name"]))
        return updated

    def restart_user(self, slug: str) -> dict[str, Any]:
        user = self.user_by_slug(slug)
        run_command(["docker", "restart", str(user["container_name"])], cwd=self.config.repo_root)
        self.wait_for_user_http(user)
        updated = self.update_user_status(slug, "running")
        self.audit("container_restarted", slug=slug, container_name=str(user["container_name"]))
        return updated

    def remove_user_container(self, slug: str) -> dict[str, Any]:
        user = self.user_by_slug(slug)
        self.remove_container(str(user["container_name"]))
        updated = self.update_user_status(slug, "container_removed")
        self.audit("container_removed", slug=slug, container_name=str(user["container_name"]))
        return updated

    def delete_user_secrets(self, slug: str) -> dict[str, Any]:
        self.secret_store.delete_user_credentials(slug)
        self.audit("user_secrets_deleted", slug=slug, backend=self.secret_store.backend_name)
        return {"slug": slug, "secret_backend": self.secret_store.backend_name}

    def list_orphans(self) -> dict[str, list[str]]:
        registry = load_registry(self.config.registry_path)
        return find_orphan_resources(registry, self.config.users_root, self.config.repo_root)

    def cleanup_orphans(self) -> dict[str, Any]:
        orphans = self.list_orphans()
        removed_containers: list[str] = []
        trashed_user_dirs: list[dict[str, str]] = []
        for container_name in orphans["orphan_containers"]:
            self.remove_container(container_name)
            removed_containers.append(container_name)
            self.audit("orphan_container_removed", container_name=container_name)
        for slug in orphans["stale_user_dirs"]:
            target = trash_user_dir(self.config.users_root, slug)
            trashed_user_dirs.append({"slug": slug, "trash_path": str(target)})
            self.audit("stale_user_dir_trashed", slug=slug, trash_path=str(target))
        return {
            "removed_orphan_containers": removed_containers,
            "trashed_stale_user_dirs": trashed_user_dirs,
        }

    def offboard_user(self, slug: str) -> dict[str, Any]:
        user = self.user_by_slug(slug)
        container_name = str(user.get("container_name", f"{USER_CONTAINER_PREFIX}{slug}"))
        self.remove_container(container_name)
        self.secret_store.delete_user_credentials(slug)
        trash_path = trash_user_dir(self.config.users_root, slug)
        with self._registry_lock:
            registry = load_registry(self.config.registry_path)
            removed_user = delete_user(registry, slug)
            save_registry(self.config.registry_path, registry)
        self.audit(
            "user_offboarded",
            slug=slug,
            container_name=container_name,
            trash_path=str(trash_path),
            secret_backend=self.secret_store.backend_name,
        )
        return {
            "slug": slug,
            "removed_user": removed_user,
            "removed_container": container_name,
            "deleted_secrets": True,
            "trash_path": str(trash_path),
            "secret_backend": self.secret_store.backend_name,
        }

    def admin_status(self, *, audit_limit: int = 20) -> dict[str, Any]:
        registry = load_registry(self.config.registry_path)
        users = registry.get("users", {})
        status_counts: dict[str, int] = {}
        for user in users.values():
            if not isinstance(user, dict):
                continue
            status = str(user.get("status", "unknown"))
            status_counts[status] = status_counts.get(status, 0) + 1

        docker_error = ""
        try:
            docker_containers = list_gateway_container_statuses(self.config.repo_root)
        except Exception as exc:
            docker_containers = []
            docker_error = str(exc)

        try:
            orphans = self.list_orphans()
        except Exception as exc:
            orphans = {"orphan_containers": [], "stale_user_dirs": []}
            docker_error = docker_error or str(exc)

        return {
            "registry_path": str(self.config.registry_path),
            "users_root": str(self.config.users_root),
            "audit_log_path": str(self.audit_log_path),
            "secret_backend": self.secret_store.backend_name,
            "user_count": len(users),
            "users_by_status": status_counts,
            "registered_containers": sorted(registered_container_names(registry)),
            "docker_containers": docker_containers,
            "docker_error": docker_error,
            "orphan_containers": orphans["orphan_containers"],
            "stale_user_dirs": orphans["stale_user_dirs"],
            "recent_audit_events": read_recent_audit_events(self.audit_log_path, limit=audit_limit),
        }

    def create_user(
        self,
        *,
        client_ip: str,
        tailscale_identity: str,
        display_name: str,
        account_seq: str,
        client_id: str,
        client_secret: str,
    ) -> dict[str, Any]:
        if not account_seq.strip().isdigit():
            raise ValueError("account sequence must contain digits only")
        normalized_identity = normalize_tailscale_identity(tailscale_identity)
        if not normalized_identity:
            raise ValueError("Tailscale identity is required")

        with self._registry_lock:
            registry = load_registry(self.config.registry_path)
            existing_slug = registry.get("identity_map", {}).get(normalized_identity)
            if existing_slug:
                existing_user = registry.get("users", {}).get(existing_slug)
                if isinstance(existing_user, dict):
                    return dict(existing_user)

            slug = unique_slug(registry, display_name)
            port = next_available_port(registry)
            container_name = f"toss-dashboard-{slug}"
            user_root = self.config.users_root / slug
            user = {
                "slug": slug,
                "display_name": display_name.strip(),
                "client_ip": client_ip,
                "last_client_ip": client_ip,
                "tailscale_identity": normalized_identity,
                "secret_backend": self.secret_store.backend_name,
                "container_name": container_name,
                "port": port,
                "created_at": int(time.time()),
                "status": "provisioning",
                "user_root": str(user_root),
            }
            registry["users"][slug] = user
            registry["ip_map"][client_ip] = slug
            registry["identity_map"][normalized_identity] = slug
            save_registry(self.config.registry_path, registry)
            self.audit(
                "setup_provisioning",
                client_ip=client_ip,
                slug=slug,
                tailscale_identity=normalized_identity,
                display_name=display_name.strip(),
            )

        container_name = f"toss-dashboard-{slug}"
        user_root = self.config.users_root / slug
        config_dir = user_root / "config"
        state_dir = user_root / "state"
        log_dir = user_root / "logs"
        env_file = user_root / ".env"

        try:
            config_dir.mkdir(parents=True, exist_ok=True)
            state_dir.mkdir(parents=True, exist_ok=True)
            log_dir.mkdir(parents=True, exist_ok=True)
            write_config_file(config_dir / "local.yaml", account_seq)
            self.secret_store.put_user_credentials(slug, client_id=client_id, client_secret=client_secret)
            if self.secret_store.backend_name != "file":
                write_placeholder_env_file(env_file, self.secret_store.backend_name)
            self.ensure_container(user)
        except Exception:
            self.remove_container(container_name)
            self.secret_store.delete_user_credentials(slug)
            self.audit("setup_failed", client_ip=client_ip, slug=slug)
            with self._registry_lock:
                registry = load_registry(self.config.registry_path)
                if registry.get("ip_map", {}).get(client_ip) == slug:
                    registry.get("ip_map", {}).pop(client_ip, None)
                if registry.get("identity_map", {}).get(normalized_identity) == slug:
                    registry.get("identity_map", {}).pop(normalized_identity, None)
                if registry.get("users", {}).get(slug, {}).get("client_ip") == client_ip:
                    registry.get("users", {}).pop(slug, None)
                save_registry(self.config.registry_path, registry)
            raise

        with self._registry_lock:
            registry = load_registry(self.config.registry_path)
            saved_user = registry.get("users", {}).get(slug)
            if isinstance(saved_user, dict):
                saved_user["status"] = "running"
                registry["users"][slug] = saved_user
                save_registry(self.config.registry_path, registry)
                self.audit(
                    "setup_succeeded",
                    client_ip=client_ip,
                    slug=slug,
                    tailscale_identity=normalized_identity,
                )
                return dict(saved_user)
        return user

    def wait_for_user_http(self, user: dict[str, Any], *, timeout_seconds: float = 45.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        url = f"http://127.0.0.1:{int(user['port'])}/health"
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                with request.urlopen(url, timeout=2) as response:
                    if 200 <= response.status < 500:
                        return
            except Exception as exc:  # pragma: no cover - timing dependent
                last_error = exc
            time.sleep(0.5)
        raise TimeoutError(f"user container did not become ready: {last_error}")

    def ensure_container(self, user: dict[str, Any]) -> None:
        container_name = str(user["container_name"])
        port = str(user["port"])
        slug = str(user["slug"])
        user_root = self.config.users_root / slug
        config_dir = user_root / "config"
        state_dir = user_root / "state"
        log_dir = user_root / "logs"
        env_file = user_root / ".env"
        if self.secret_store.import_user_env_if_needed(slug, env_file):
            self.update_user_secret_backend(slug)
            self.audit("secret_imported", slug=slug, backend=self.secret_store.backend_name)
        credentials = self.secret_store.get_user_credentials(slug)
        client_id = credentials.get("TOSS_CLIENT_ID", "")
        client_secret = credentials.get("TOSS_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            raise RuntimeError(f"Toss credentials are not available for user: {slug}")

        existing = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            cwd=str(self.config.repo_root),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        if container_name in existing:
            running = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", container_name],
                cwd=str(self.config.repo_root),
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            if running == "true":
                return
            run_command(["docker", "start", container_name], cwd=self.config.repo_root)
            self.wait_for_user_http(user)
            return

        self.ensure_image()
        run_command(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container_name,
                "--restart",
                "unless-stopped",
                "--memory",
                self.config.container_memory,
                "--cpus",
                self.config.container_cpus,
                "--log-opt",
                f"max-size={self.config.container_log_max_size}",
                "--log-opt",
                f"max-file={self.config.container_log_max_files}",
                "--env",
                "TOSS_CLIENT_ID",
                "--env",
                "TOSS_CLIENT_SECRET",
                "-p",
                f"127.0.0.1:{port}:{self.config.container_port}",
                "-v",
                f"{config_dir.resolve()}:/app/config",
                "-v",
                f"{state_dir.resolve()}:/app/state",
                "-v",
                f"{log_dir.resolve()}:/app/logs",
                self.config.image_name,
                "--dashboard-server",
                "--config",
                "/app/config/local.yaml",
                "--state-db",
                "/app/state/turtle.sqlite3",
                "--host",
                "0.0.0.0",
                "--port",
                str(self.config.container_port),
            ],
            cwd=self.config.repo_root,
            env={
                **os.environ,
                "TOSS_CLIENT_ID": client_id,
                "TOSS_CLIENT_SECRET": client_secret,
            },
        )
        self.wait_for_user_http(user)


def setup_page(
    client_ip: str,
    *,
    tailscale_identity: str,
    display_name: str,
    message: str = "",
    csrf_token: str = "",
) -> bytes:
    escaped_message = html.escape(message)
    escaped_identity = html.escape(tailscale_identity)
    escaped_display_name = html.escape(display_name)
    escaped_csrf = html.escape(csrf_token)
    body = f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Toss Dashboard Setup</title>
  <style>
    body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif; background:#f6f8fb; color:#0f172a; }}
    main {{ max-width:640px; margin:8vh auto; padding:24px; }}
    form {{ display:grid; gap:14px; background:#fff; border:1px solid #dbe2ea; border-radius:10px; padding:22px; box-shadow:0 16px 36px rgba(15,23,42,.08); }}
    label {{ display:grid; gap:6px; font-weight:800; font-size:14px; }}
    input {{ height:42px; border:1px solid #cbd5e1; border-radius:8px; padding:0 12px; font-size:15px; }}
    button {{ height:44px; border:0; border-radius:8px; background:#2563eb; color:#fff; font-weight:900; cursor:pointer; }}
    p {{ color:#475569; line-height:1.5; }}
    small {{ color:#64748b; font-weight:600; line-height:1.45; }}
    .error {{ color:#b91c1c; background:#fee2e2; border:1px solid #fecaca; padding:10px; border-radius:8px; }}
    .ip {{ font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }}
    .identity {{ background:#f1f5f9; border:1px solid #dbe2ea; border-radius:8px; padding:12px; }}
    .hint {{ background:#eff6ff; border:1px solid #bfdbfe; border-radius:8px; padding:12px; color:#1e3a8a; }}
  </style>
</head>
<body>
  <main>
    <h1>처음 접속한 사용자 설정</h1>
    <p>Tailscale 계정 기준으로 개인 대시보드를 만듭니다. 같은 Tailscale 계정이면 다른 기기에서도 같은 대시보드로 이동합니다.</p>
    <div class="identity">
      <div><strong>{escaped_display_name}</strong></div>
      <div class="ip">{escaped_identity}</div>
      <div>Tailscale 계정으로 사용자를 구분합니다.</div>
    </div>
    <p class="hint">아래 값은 토스증권 Open API에서 발급받은 앱 정보와 연결할 계좌 식별번호입니다. API 값은 화면에 다시 보여주지 않고 이 서버의 보안 저장소에만 저장합니다.</p>
    {f'<div class="error">{escaped_message}</div>' if escaped_message else ''}
    <form method="post" action="/__setup">
      <input type="hidden" name="csrf_token" value="{escaped_csrf}">
      <label>토스 앱 ID
        <input name="client_id" autocomplete="off" required placeholder="토스증권 Open API 앱의 Client ID">
        <small>토스증권 Open API에서 만든 앱의 공개 식별값입니다.</small>
      </label>
      <label>토스 앱 비밀키
        <input name="client_secret" type="password" autocomplete="off" required placeholder="토스증권 Open API 앱의 Client Secret">
        <small>토큰 발급에 쓰는 비밀값입니다. 저장 후에는 다시 표시하지 않습니다.</small>
      </label>
      <label>연결할 토스 계좌 식별번호
        <input name="account_seq" inputmode="numeric" autocomplete="off" required placeholder="예: 7">
        <small>토스 API가 계좌를 구분할 때 쓰는 숫자입니다. 계좌번호 전체를 입력하는 칸이 아닙니다.</small>
      </label>
      <label>본인 확인
        <input name="confirmation" autocomplete="off" required placeholder="{SETUP_CONFIRMATION}">
        <small>실수로 다른 사람 계정을 연결하지 않도록 안내 문구를 그대로 입력합니다.</small>
      </label>
      <button type="submit">내 대시보드 만들기</button>
    </form>
  </main>
</body>
</html>"""
    return body.encode("utf-8")


def identity_required_page(message: str = "") -> bytes:
    escaped_message = html.escape(message)
    body = f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Tailscale Identity Required</title>
  <style>
    body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif; background:#f6f8fb; color:#0f172a; }}
    main {{ max-width:520px; margin:10vh auto; padding:24px; }}
    section {{ background:#fff; border:1px solid #dbe2ea; border-radius:10px; padding:22px; box-shadow:0 16px 36px rgba(15,23,42,.08); }}
    p {{ color:#475569; line-height:1.5; }}
    .error {{ color:#b91c1c; background:#fee2e2; border:1px solid #fecaca; padding:10px; border-radius:8px; }}
    code {{ background:#f1f5f9; border-radius:6px; padding:2px 5px; }}
  </style>
</head>
<body>
  <main>
    <section>
      <h1>Tailscale identity가 필요합니다</h1>
      <p>이 게이트웨이는 앱 비밀번호를 쓰지 않습니다. <code>tailscale serve</code>를 통해 접속해야 Tailscale 사용자 계정을 확인할 수 있습니다.</p>
      {f'<div class="error">{escaped_message}</div>' if escaped_message else ''}
    </section>
  </main>
</body>
</html>"""
    return body.encode("utf-8")


def make_handler(gateway: UserGateway):
    class GatewayHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write("%s - - [%s] %s\n" % (self.client_address[0], self.log_date_time_string(), fmt % args))

        def client_ip(self) -> str:
            return self.client_address[0]

        def send_html(
            self,
            status: int,
            body: bytes,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def send_setup_page(self, status: int = 200, message: str = "") -> None:
            identity = tailscale_identity_from_headers(self.headers)
            if identity is None:
                self.send_identity_required_page(401, "Tailscale Serve identity header가 없습니다.")
                return
            token = make_csrf_token()
            body = setup_page(
                self.client_ip(),
                tailscale_identity=identity["identity"],
                display_name=identity["display_name"],
                message=message,
                csrf_token=token,
            )
            self.send_html(
                status,
                body,
                {
                    "Set-Cookie": (
                        f"{SETUP_CSRF_COOKIE}={token}; Path=/; "
                        "HttpOnly; SameSite=Lax"
                    ),
                    "Cache-Control": "no-store",
                },
            )

        def send_identity_required_page(self, status: int = 401, message: str = "") -> None:
            body = identity_required_page(message)
            self.send_html(
                status,
                body,
                {
                    "Cache-Control": "no-store",
                },
            )

        def do_GET(self) -> None:
            if self.path == "/health":
                self.send_json(200, {"status": "ok", "service": "multi-user-gateway"})
                return
            if self.path.startswith("/__setup"):
                self.send_setup_page()
                return
            self.route_or_setup()

        def do_POST(self) -> None:
            if self.path == "/__setup":
                self.handle_setup()
                return
            self.route_or_setup()

        def handle_setup(self) -> None:
            identity = tailscale_identity_from_headers(self.headers)
            if identity is None:
                gateway.audit("identity_missing", client_ip=self.client_ip(), request_path=self.path)
                self.send_identity_required_page(401, "Tailscale Serve를 통해 다시 접속해 주세요.")
                return
            if not gateway.registration_allowed(self.client_ip()):
                gateway.audit(
                    "setup_registration_denied",
                    client_ip=self.client_ip(),
                    tailscale_identity=identity["identity"],
                    request_path=self.path,
                )
                self.send_setup_page(403, "이 기기는 등록 허용 목록에 없습니다. 관리자에게 Tailscale IP 등록을 요청하세요.")
                return
            allowed, retry_after = gateway.consume_setup_attempt(self.client_ip())
            if not allowed:
                gateway.audit(
                    "setup_rate_limited",
                    client_ip=self.client_ip(),
                    tailscale_identity=identity["identity"],
                    retry_after=retry_after,
                )
                self.send_setup_page(429, f"설정 요청이 너무 많습니다. {retry_after}초 뒤에 다시 시도하세요.")
                return
            try:
                length = parse_content_length(
                    self.headers.get("Content-Length"),
                    max_bytes=MAX_SETUP_BODY_BYTES,
                )
            except ValueError as exc:
                self.send_setup_page(413, str(exc))
                return
            form = parse.parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
            client_id = form.get("client_id", [""])[0].strip()
            client_secret = form.get("client_secret", [""])[0].strip()
            account_seq = form.get("account_seq", [""])[0].strip()
            confirmation = form.get("confirmation", [""])[0].strip()
            csrf_token = form.get("csrf_token", [""])[0].strip()

            try:
                if not csrf_is_valid(self.headers.get("Cookie"), csrf_token):
                    raise ValueError("설정 페이지를 새로고침한 뒤 다시 제출해 주세요.")
                if not all([client_id, client_secret, account_seq]):
                    raise ValueError("모든 값을 입력해 주세요.")
                if confirmation != SETUP_CONFIRMATION:
                    raise ValueError(f"본인 확인 칸에 '{SETUP_CONFIRMATION}'을 그대로 입력해 주세요.")
                user = gateway.create_user(
                    client_ip=self.client_ip(),
                    tailscale_identity=identity["identity"],
                    display_name=identity["display_name"],
                    account_seq=account_seq,
                    client_id=client_id,
                    client_secret=client_secret,
                )
            except Exception as exc:
                self.send_setup_page(400, str(exc))
                return

            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def route_or_setup(self) -> None:
            identity = tailscale_identity_from_headers(self.headers)
            if identity is None:
                gateway.audit("identity_missing", client_ip=self.client_ip(), request_path=self.path)
                self.send_identity_required_page()
                return
            user = gateway.user_for_identity(identity["identity"])
            if user is None:
                self.send_setup_page()
                return
            gateway.record_user_client_ip(str(user["slug"]), self.client_ip())
            try:
                gateway.ensure_container(user)
                self.proxy_to_user(user)
            except Exception as exc:
                self.send_setup_page(502, f"컨테이너 연결 실패: {exc}")

        def proxy_to_user(self, user: dict[str, Any]) -> None:
            upstream = f"http://127.0.0.1:{int(user['port'])}{self.path}"
            body = None
            if self.command in {"POST", "PUT", "PATCH"}:
                length = int(self.headers.get("Content-Length", "0") or "0")
                body = self.rfile.read(length) if length > 0 else b""
            headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower() not in {"host", "connection", "content-length", "accept-encoding"}
            }
            headers["X-Forwarded-For"] = self.client_ip()
            req = request.Request(upstream, data=body, headers=headers, method=self.command)
            try:
                with request.urlopen(req, timeout=60) as resp:
                    payload = resp.read()
                    self.forward_response(resp.status, resp.headers, payload)
            except error.HTTPError as exc:
                payload = exc.read()
                self.forward_response(exc.code, exc.headers, payload)

        def forward_response(self, status: int, headers: Any, payload: bytes) -> None:
            self.send_response(status)
            for key, value in headers.items():
                if key.lower() in {"connection", "content-length", "transfer-encoding", "content-encoding"}:
                    continue
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return GatewayHandler


def detect_tailscale_ip() -> str | None:
    tailscale = shutil.which("tailscale")
    if not tailscale:
        return None
    result = subprocess.run([tailscale, "ip", "-4"], check=False, capture_output=True, text=True)
    for line in result.stdout.splitlines():
        cleaned = line.strip()
        if cleaned:
            return cleaned
    return None


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Tailscale-aware per-user dashboard gateway")
    parser.add_argument("--host", default=None, help="Gateway bind host; defaults to 127.0.0.1")
    parser.add_argument("--port", type=int, default=8765, help="Gateway bind port")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--registry", type=Path, default=Path(DEFAULT_REGISTRY))
    parser.add_argument("--users-root", type=Path, default=Path(DEFAULT_USER_ROOT))
    parser.add_argument("--audit-log", type=Path, default=Path(DEFAULT_AUDIT_LOG))
    parser.add_argument(
        "--secret-backend",
        default="auto",
        help="Secret backend for Toss API credentials: auto, keychain, or file",
    )
    parser.add_argument("--keychain-service", default=DEFAULT_KEYCHAIN_SERVICE)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument(
        "--setup-rate-limit",
        type=int,
        default=DEFAULT_SETUP_RATE_LIMIT,
        help="Maximum setup submissions per client IP within the rate window; 0 disables the limit",
    )
    parser.add_argument(
        "--setup-rate-window-seconds",
        type=int,
        default=DEFAULT_SETUP_RATE_WINDOW_SECONDS,
        help="Rate-limit window for setup submissions",
    )
    parser.add_argument(
        "--registration-allowlist",
        default="",
        help="Comma-separated Tailscale IPs or CIDRs allowed to self-register; empty allows all",
    )
    parser.add_argument("--container-memory", default=DEFAULT_CONTAINER_MEMORY, help="Docker memory limit per user container")
    parser.add_argument("--container-cpus", default=DEFAULT_CONTAINER_CPUS, help="Docker CPU limit per user container")
    parser.add_argument(
        "--container-log-max-size",
        default=DEFAULT_CONTAINER_LOG_MAX_SIZE,
        help="Docker log max-size per user container",
    )
    parser.add_argument(
        "--container-log-max-files",
        default=DEFAULT_CONTAINER_LOG_MAX_FILES,
        help="Docker log max-file per user container",
    )
    parser.add_argument("--list-users", action="store_true", help="Print registry users as JSON and exit")
    parser.add_argument("--unmap-ip", metavar="IP", help="Remove one IP-to-user mapping and exit")
    parser.add_argument("--delete-user", metavar="SLUG", help="Remove one user from the registry and exit")
    parser.add_argument("--stop-user", metavar="SLUG", help="Stop one user's Docker container and exit")
    parser.add_argument("--start-user", metavar="SLUG", help="Start one user's Docker container and exit")
    parser.add_argument("--restart-user", metavar="SLUG", help="Restart one user's Docker container and exit")
    parser.add_argument(
        "--remove-user-container",
        metavar="SLUG",
        help="Remove one user's Docker container without deleting registry or files and exit",
    )
    parser.add_argument(
        "--delete-user-secrets",
        metavar="SLUG",
        help="Delete one user's Toss credentials from the configured secret backend and exit",
    )
    parser.add_argument("--list-orphans", action="store_true", help="Print orphan containers and stale user folders as JSON and exit")
    parser.add_argument(
        "--cleanup-orphans",
        action="store_true",
        help="Remove orphan containers and move stale user folders into users-root/_trash",
    )
    parser.add_argument(
        "--offboard-user",
        metavar="SLUG",
        help="Remove a user container, delete stored Toss credentials, trash local files, and delete registry mappings",
    )
    parser.add_argument("--admin-status", action="store_true", help="Print admin status summary as JSON and exit")
    parser.add_argument("--audit-limit", type=int, default=20, help="Recent audit events to include in admin status")
    parser.add_argument(
        "--confirm",
        default="",
        help=(
            "Required confirmation phrase for destructive admin commands. "
            f"Use {DELETE_SECRETS_CONFIRMATION}, {CLEANUP_ORPHANS_CONFIRMATION}, or {OFFBOARD_USER_CONFIRMATION}."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    repo_root = args.repo_root.resolve()
    registry_path = args.registry if args.registry.is_absolute() else repo_root / args.registry
    users_root = args.users_root if args.users_root.is_absolute() else repo_root / args.users_root
    audit_log_path = args.audit_log if args.audit_log.is_absolute() else repo_root / args.audit_log

    if args.list_users:
        print(json.dumps(registry_public_view(load_registry(registry_path)), ensure_ascii=False, indent=2))
        return 0
    if args.unmap_ip:
        registry = load_registry(registry_path)
        removed_slug = unmap_ip(registry, args.unmap_ip)
        save_registry(registry_path, registry)
        print(json.dumps({"unmapped_ip": args.unmap_ip, "user": removed_slug}, ensure_ascii=False))
        return 0
    if args.delete_user:
        registry = load_registry(registry_path)
        removed_user = delete_user(registry, args.delete_user)
        save_registry(registry_path, registry)
        print(json.dumps({"deleted_user": args.delete_user, "user": removed_user}, ensure_ascii=False))
        return 0
    if args.delete_user_secrets and args.confirm != DELETE_SECRETS_CONFIRMATION:
        print(
            json.dumps(
                {
                    "error": "confirmation_required",
                    "confirm": DELETE_SECRETS_CONFIRMATION,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    if args.cleanup_orphans and args.confirm != CLEANUP_ORPHANS_CONFIRMATION:
        print(
            json.dumps(
                {
                    "error": "confirmation_required",
                    "confirm": CLEANUP_ORPHANS_CONFIRMATION,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    if args.offboard_user and args.confirm != OFFBOARD_USER_CONFIRMATION:
        print(
            json.dumps(
                {
                    "error": "confirmation_required",
                    "confirm": OFFBOARD_USER_CONFIRMATION,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2

    gateway = UserGateway(
        GatewayConfig(
            repo_root=repo_root,
            registry_path=registry_path,
            users_root=users_root,
            audit_log_path=audit_log_path,
            secret_backend=args.secret_backend,
            keychain_service=args.keychain_service,
            image_name=args.image,
            container_port=DEFAULT_CONTAINER_PORT,
            setup_rate_limit=args.setup_rate_limit,
            setup_rate_window_seconds=args.setup_rate_window_seconds,
            registration_allowlist=parse_allowlist(args.registration_allowlist),
            container_memory=args.container_memory,
            container_cpus=args.container_cpus,
            container_log_max_size=args.container_log_max_size,
            container_log_max_files=args.container_log_max_files,
        )
    )

    if args.stop_user:
        user = gateway.stop_user(args.stop_user)
        print(json.dumps({"stopped_user": args.stop_user, "user": user}, ensure_ascii=False))
        return 0
    if args.start_user:
        user = gateway.start_user(args.start_user)
        print(json.dumps({"started_user": args.start_user, "user": user}, ensure_ascii=False))
        return 0
    if args.restart_user:
        user = gateway.restart_user(args.restart_user)
        print(json.dumps({"restarted_user": args.restart_user, "user": user}, ensure_ascii=False))
        return 0
    if args.remove_user_container:
        user = gateway.remove_user_container(args.remove_user_container)
        print(json.dumps({"removed_user_container": args.remove_user_container, "user": user}, ensure_ascii=False))
        return 0
    if args.delete_user_secrets:
        result = gateway.delete_user_secrets(args.delete_user_secrets)
        print(json.dumps({"deleted_user_secrets": args.delete_user_secrets, "result": result}, ensure_ascii=False))
        return 0
    if args.list_orphans:
        print(json.dumps(gateway.list_orphans(), ensure_ascii=False, indent=2))
        return 0
    if args.cleanup_orphans:
        print(json.dumps(gateway.cleanup_orphans(), ensure_ascii=False, indent=2))
        return 0
    if args.offboard_user:
        print(json.dumps({"offboarded_user": args.offboard_user, "result": gateway.offboard_user(args.offboard_user)}, ensure_ascii=False, indent=2))
        return 0
    if args.admin_status:
        print(json.dumps(gateway.admin_status(audit_limit=args.audit_limit), ensure_ascii=False, indent=2))
        return 0

    host = args.host or "127.0.0.1"
    server = ThreadingHTTPServer((host, args.port), make_handler(gateway))
    print(f"Multi-user dashboard gateway: http://{host}:{args.port}/", flush=True)
    print(f"Registry: {registry_path}", flush=True)
    print(f"Audit log: {audit_log_path}", flush=True)
    print(f"Secret backend: {gateway.secret_store.backend_name}", flush=True)
    print("This gateway expects Tailscale Serve identity headers.", flush=True)
    print("First-time Tailscale identities can be restricted with the registration allowlist.", flush=True)
    if args.registration_allowlist:
        print(f"Registration allowlist: {args.registration_allowlist}", flush=True)
    print(
        "Setup rate limit: "
        f"{args.setup_rate_limit} submissions / {args.setup_rate_window_seconds} seconds / IP",
        flush=True,
    )
    print(
        "Container limits: "
        f"memory={args.container_memory}, cpus={args.container_cpus}, "
        f"log={args.container_log_max_size} x {args.container_log_max_files}",
        flush=True,
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
