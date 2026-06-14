#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error, parse, request


DEFAULT_IMAGE = "toss-trading-bot:local"
DEFAULT_REGISTRY = ".local/users/registry.json"
DEFAULT_USER_ROOT = ".local/users"
DEFAULT_CONTAINER_PORT = 8765
DEFAULT_FIRST_PORT = 19000
SETUP_CONFIRMATION = "토스 연결 승인"


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", value.strip().lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "user"


def clean_env_value(value: str) -> str:
    cleaned = value.strip()
    if "\n" in cleaned or "\r" in cleaned:
        raise ValueError("credential values cannot contain new lines")
    return cleaned


def load_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "next_port": DEFAULT_FIRST_PORT, "ip_map": {}, "users": {}}
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"invalid registry: {path}")
    data.setdefault("version", 1)
    data.setdefault("next_port", DEFAULT_FIRST_PORT)
    data.setdefault("ip_map", {})
    data.setdefault("users", {})
    return data


def save_registry(path: Path, registry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(registry, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    tmp.replace(path)


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


def run_command(args: list[str], *, cwd: Path) -> None:
    subprocess.run(args, cwd=str(cwd), check=True)


@dataclass(frozen=True)
class GatewayConfig:
    repo_root: Path
    registry_path: Path
    users_root: Path
    image_name: str
    container_port: int


class UserGateway:
    def __init__(self, config: GatewayConfig) -> None:
        self.config = config

    def ensure_image(self) -> None:
        run_command(["docker", "build", "-t", self.config.image_name, "."], cwd=self.config.repo_root)

    def user_for_ip(self, client_ip: str) -> dict[str, Any] | None:
        registry = load_registry(self.config.registry_path)
        slug = registry.get("ip_map", {}).get(client_ip)
        if not slug:
            return None
        user = registry.get("users", {}).get(slug)
        return dict(user) if isinstance(user, dict) else None

    def create_user(
        self,
        *,
        client_ip: str,
        display_name: str,
        account_seq: str,
        client_id: str,
        client_secret: str,
    ) -> dict[str, Any]:
        registry = load_registry(self.config.registry_path)
        slug = unique_slug(registry, display_name)
        port = next_available_port(registry)
        container_name = f"toss-dashboard-{slug}"
        user_root = self.config.users_root / slug
        config_dir = user_root / "config"
        state_dir = user_root / "state"
        log_dir = user_root / "logs"
        env_file = user_root / ".env"

        config_dir.mkdir(parents=True, exist_ok=True)
        state_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        write_config_file(config_dir / "local.yaml", account_seq)
        write_env_file(env_file, client_id, client_secret)

        user = {
            "slug": slug,
            "display_name": display_name.strip(),
            "client_ip": client_ip,
            "container_name": container_name,
            "port": port,
            "created_at": int(time.time()),
            "user_root": str(user_root),
        }
        registry["users"][slug] = user
        registry["ip_map"][client_ip] = slug
        save_registry(self.config.registry_path, registry)
        self.ensure_container(user)
        return user

    def ensure_container(self, user: dict[str, Any]) -> None:
        container_name = str(user["container_name"])
        port = str(user["port"])
        slug = str(user["slug"])
        user_root = self.config.users_root / slug
        config_dir = user_root / "config"
        state_dir = user_root / "state"
        log_dir = user_root / "logs"
        env_file = user_root / ".env"

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
                "--env-file",
                str(env_file),
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
        )


def setup_page(client_ip: str, message: str = "") -> bytes:
    escaped_message = html.escape(message)
    escaped_ip = html.escape(client_ip)
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
    .error {{ color:#b91c1c; background:#fee2e2; border:1px solid #fecaca; padding:10px; border-radius:8px; }}
    .ip {{ font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }}
  </style>
</head>
<body>
  <main>
    <h1>처음 접속한 사용자 설정</h1>
    <p>이 Tailscale IP(<span class="ip">{escaped_ip}</span>)에 연결할 전용 컨테이너를 만듭니다. 다음 접속부터는 자동으로 같은 대시보드로 이동합니다.</p>
    {f'<div class="error">{escaped_message}</div>' if escaped_message else ''}
    <form method="post" action="/__setup">
      <label>이름
        <input name="display_name" autocomplete="name" required placeholder="예: Alice">
      </label>
      <label>토스 앱 ID
        <input name="client_id" autocomplete="off" required placeholder="토스 개발자센터에서 발급받은 앱 ID">
      </label>
      <label>토스 앱 비밀키
        <input name="client_secret" type="password" autocomplete="off" required placeholder="토스 개발자센터에서 발급받은 비밀키">
      </label>
      <label>연결할 토스 계좌 번호
        <input name="account_seq" inputmode="numeric" autocomplete="off" required placeholder="예: 7">
      </label>
      <label>본인 확인
        <input name="confirmation" autocomplete="off" required placeholder="{SETUP_CONFIRMATION}">
      </label>
      <button type="submit">내 컨테이너 만들기</button>
    </form>
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

        def send_html(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path.startswith("/__setup"):
                self.send_html(200, setup_page(self.client_ip()))
                return
            self.route_or_setup()

        def do_POST(self) -> None:
            if self.path == "/__setup":
                self.handle_setup()
                return
            self.route_or_setup()

        def handle_setup(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or "0")
            form = parse.parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
            display_name = form.get("display_name", [""])[0].strip()
            client_id = form.get("client_id", [""])[0].strip()
            client_secret = form.get("client_secret", [""])[0].strip()
            account_seq = form.get("account_seq", [""])[0].strip()
            confirmation = form.get("confirmation", [""])[0].strip()

            try:
                if not all([display_name, client_id, client_secret, account_seq]):
                    raise ValueError("모든 값을 입력해 주세요.")
                if confirmation != SETUP_CONFIRMATION:
                    raise ValueError(f"본인 확인 칸에 '{SETUP_CONFIRMATION}'을 그대로 입력해 주세요.")
                gateway.create_user(
                    client_ip=self.client_ip(),
                    display_name=display_name,
                    account_seq=account_seq,
                    client_id=client_id,
                    client_secret=client_secret,
                )
            except Exception as exc:
                self.send_html(400, setup_page(self.client_ip(), str(exc)))
                return

            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def route_or_setup(self) -> None:
            user = gateway.user_for_ip(self.client_ip())
            if user is None:
                self.send_html(200, setup_page(self.client_ip()))
                return
            try:
                gateway.ensure_container(user)
                self.proxy_to_user(user)
            except Exception as exc:
                self.send_html(502, setup_page(self.client_ip(), f"컨테이너 연결 실패: {exc}"))

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
    parser.add_argument("--host", default=None, help="Gateway bind host; defaults to Tailscale IPv4 or 127.0.0.1")
    parser.add_argument("--port", type=int, default=8765, help="Gateway bind port")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--registry", type=Path, default=Path(DEFAULT_REGISTRY))
    parser.add_argument("--users-root", type=Path, default=Path(DEFAULT_USER_ROOT))
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    repo_root = args.repo_root.resolve()
    registry_path = args.registry if args.registry.is_absolute() else repo_root / args.registry
    users_root = args.users_root if args.users_root.is_absolute() else repo_root / args.users_root
    host = args.host or detect_tailscale_ip() or "127.0.0.1"

    gateway = UserGateway(
        GatewayConfig(
            repo_root=repo_root,
            registry_path=registry_path,
            users_root=users_root,
            image_name=args.image,
            container_port=DEFAULT_CONTAINER_PORT,
        )
    )
    server = ThreadingHTTPServer((host, args.port), make_handler(gateway))
    print(f"Multi-user dashboard gateway: http://{host}:{args.port}/", flush=True)
    print(f"Registry: {registry_path}", flush=True)
    print("First-time Tailscale IPs will see the setup form.", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
