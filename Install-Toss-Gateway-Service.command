#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$repo_root"

service_label="${SERVICE_LABEL:-com.sands15.toss-gateway}"
plist_path="$HOME/Library/LaunchAgents/${service_label}.plist"
gateway_port="${GATEWAY_PORT:-8765}"
gateway_host="${GATEWAY_HOST:-127.0.0.1}"
path_value="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "This installer is intended for macOS. Current system: $(uname -s)" >&2
  exit 1
fi

python_seed="${PYTHON:-}"
if [ -z "$python_seed" ]; then
  if command -v python3.12 >/dev/null 2>&1; then
    python_seed="python3.12"
  elif command -v python3 >/dev/null 2>&1; then
    python_seed="python3"
  else
    echo "Python 3 was not found. Install Python 3.11+ first." >&2
    exit 1
  fi
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker was not found. Install Docker Desktop on this Mac first." >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker is installed, but the Docker engine is not running. Start Docker Desktop first." >&2
  exit 1
fi

if ! command -v tailscale >/dev/null 2>&1; then
  echo "Tailscale was not found. Install Tailscale and log in first." >&2
  exit 1
fi
if ! tailscale status >/dev/null 2>&1; then
  echo "Tailscale is installed, but this Mac is not logged in or not connected." >&2
  exit 1
fi

if [ "$gateway_host" != "127.0.0.1" ] && [ "$gateway_host" != "localhost" ]; then
  echo "The gateway service must bind to 127.0.0.1 so Tailscale Serve can attach identity headers." >&2
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "Creating Python environment..."
  "$python_seed" -m venv .venv
fi

python_bin="$repo_root/.venv/bin/python"

echo "Installing/updating Toss dashboard package..."
"$python_bin" -m pip install -U pip >/dev/null
"$python_bin" -m pip install -e "." >/dev/null

mkdir -p "$HOME/Library/LaunchAgents" "$repo_root/.local/users" "$repo_root/logs"

export TOSS_GATEWAY_SERVICE_LABEL="$service_label"
export TOSS_GATEWAY_PLIST="$plist_path"
export TOSS_GATEWAY_REPO="$repo_root"
export TOSS_GATEWAY_PYTHON="$python_bin"
export TOSS_GATEWAY_PORT="$gateway_port"
export TOSS_GATEWAY_HOST="$gateway_host"
export TOSS_GATEWAY_PATH="$path_value"
export TOSS_GATEWAY_REGISTRATION_ALLOWLIST="${REGISTRATION_ALLOWLIST:-}"
export TOSS_GATEWAY_SECRET_BACKEND="${SECRET_BACKEND:-auto}"
export TOSS_GATEWAY_KEYCHAIN_SERVICE="${KEYCHAIN_SERVICE:-toss-trading-bot}"
export TOSS_GATEWAY_CONTAINER_MEMORY="${CONTAINER_MEMORY:-512m}"
export TOSS_GATEWAY_CONTAINER_CPUS="${CONTAINER_CPUS:-1.0}"
export TOSS_GATEWAY_LOG_MAX_SIZE="${CONTAINER_LOG_MAX_SIZE:-10m}"
export TOSS_GATEWAY_LOG_MAX_FILES="${CONTAINER_LOG_MAX_FILES:-3}"

"$python_bin" - <<'PY'
import os
import plistlib
from pathlib import Path

repo = Path(os.environ["TOSS_GATEWAY_REPO"])
plist_path = Path(os.environ["TOSS_GATEWAY_PLIST"])
env = {
    "PATH": os.environ["TOSS_GATEWAY_PATH"],
    "PYTHON": os.environ["TOSS_GATEWAY_PYTHON"],
    "PYTHONUNBUFFERED": "1",
    "GATEWAY_PORT": os.environ["TOSS_GATEWAY_PORT"],
    "GATEWAY_HOST": os.environ["TOSS_GATEWAY_HOST"],
    "TAILSCALE_SERVE": "1",
    "REGISTRATION_ALLOWLIST": os.environ["TOSS_GATEWAY_REGISTRATION_ALLOWLIST"],
    "SECRET_BACKEND": os.environ["TOSS_GATEWAY_SECRET_BACKEND"],
    "KEYCHAIN_SERVICE": os.environ["TOSS_GATEWAY_KEYCHAIN_SERVICE"],
    "CONTAINER_MEMORY": os.environ["TOSS_GATEWAY_CONTAINER_MEMORY"],
    "CONTAINER_CPUS": os.environ["TOSS_GATEWAY_CONTAINER_CPUS"],
    "CONTAINER_LOG_MAX_SIZE": os.environ["TOSS_GATEWAY_LOG_MAX_SIZE"],
    "CONTAINER_LOG_MAX_FILES": os.environ["TOSS_GATEWAY_LOG_MAX_FILES"],
}
plist = {
    "Label": os.environ["TOSS_GATEWAY_SERVICE_LABEL"],
    "ProgramArguments": ["/bin/bash", str(repo / "ops" / "run-multi-user-gateway.command")],
    "WorkingDirectory": str(repo),
    "EnvironmentVariables": env,
    "RunAtLoad": True,
    "KeepAlive": {"SuccessfulExit": False},
    "StandardOutPath": str(repo / ".local" / "users" / "gateway-launchd.out.log"),
    "StandardErrorPath": str(repo / ".local" / "users" / "gateway-launchd.err.log"),
}
plist_path.parent.mkdir(parents=True, exist_ok=True)
with plist_path.open("wb") as handle:
    plistlib.dump(plist, handle)
PY

uid="$(id -u)"
launchctl bootout "gui/${uid}" "$plist_path" >/dev/null 2>&1 || true
launchctl bootstrap "gui/${uid}" "$plist_path"
launchctl kickstart -k "gui/${uid}/${service_label}"

echo "Waiting for gateway health..."
for _ in $(seq 1 30); do
  if curl -fsS "http://${gateway_host}:${gateway_port}/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

curl -fsS "http://${gateway_host}:${gateway_port}/health" >/dev/null

cat <<EOF

Toss gateway service installed and running.

Service:
  ${service_label}

Plist:
  ${plist_path}

Logs:
  ${repo_root}/.local/users/gateway-launchd.out.log
  ${repo_root}/.local/users/gateway-launchd.err.log

Tailscale URL:
  Run: tailscale serve status

For future updates, run:
  open Update-Toss-Gateway.command

EOF
