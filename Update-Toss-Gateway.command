#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$repo_root"

service_label="${SERVICE_LABEL:-com.sands15.toss-gateway}"
plist_path="$HOME/Library/LaunchAgents/${service_label}.plist"
gateway_port="${GATEWAY_PORT:-8765}"
standby_port="${STANDBY_GATEWAY_PORT:-8766}"
gateway_host="${GATEWAY_HOST:-127.0.0.1}"
standby_pid=""
serve_on_standby="0"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "This updater is intended for macOS. Current system: $(uname -s)" >&2
  exit 1
fi

if [ ! -f "$plist_path" ]; then
  echo "Gateway service is not installed yet: $plist_path" >&2
  echo "Run Install-Toss-Gateway-Service.command first." >&2
  exit 1
fi

if [ "$gateway_port" = "$standby_port" ]; then
  echo "STANDBY_GATEWAY_PORT must be different from GATEWAY_PORT." >&2
  exit 1
fi

python_seed="${PYTHON:-}"
if [ -z "$python_seed" ]; then
  if [ -x ".venv/bin/python" ]; then
    python_seed="$repo_root/.venv/bin/python"
  elif command -v python3.12 >/dev/null 2>&1; then
    python_seed="python3.12"
  elif command -v python3 >/dev/null 2>&1; then
    python_seed="python3"
  else
    echo "Python 3 was not found. Install Python 3.11+ first." >&2
    exit 1
  fi
fi

if [ ! -d ".venv" ]; then
  "$python_seed" -m venv .venv
fi
python_bin="$repo_root/.venv/bin/python"

cleanup() {
  if [ -n "$standby_pid" ] && kill -0 "$standby_pid" >/dev/null 2>&1; then
    if [ "$serve_on_standby" = "0" ]; then
      kill "$standby_pid" >/dev/null 2>&1 || true
    fi
  fi
}
trap cleanup EXIT

wait_health() {
  local port="$1"
  local label="$2"
  for _ in $(seq 1 45); do
    if curl -fsS "http://${gateway_host}:${port}/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "${label} did not become healthy on port ${port}." >&2
  return 1
}

echo "Fetching latest code..."
git fetch origin
git pull --ff-only origin main

echo "Installing/updating local package..."
"$python_bin" -m pip install -U pip >/dev/null
"$python_bin" -m pip install -e "." >/dev/null

image_name="${TOSS_DASHBOARD_IMAGE:-toss-trading-bot:local}"
echo "Rebuilding dashboard container image ${image_name}..."
docker build -t "$image_name" .

mkdir -p "$repo_root/.local/users"

standby_log="$repo_root/.local/users/gateway-update-standby.log"
echo "Starting standby gateway on port ${standby_port}..."
TAILSCALE_SERVE=0 GATEWAY_PORT="$standby_port" GATEWAY_HOST="$gateway_host" PYTHON="$python_bin" \
  nohup bash ops/run-multi-user-gateway.command >"$standby_log" 2>&1 &
standby_pid="$!"

wait_health "$standby_port" "Standby gateway"

echo "Switching Tailscale Serve to standby gateway..."
tailscale serve --bg "$standby_port"
serve_on_standby="1"

echo "Restarting launchd gateway service..."
launchctl kickstart -k "gui/$(id -u)/${service_label}"

if ! wait_health "$gateway_port" "Managed gateway"; then
  cat <<EOF >&2

Managed gateway did not become healthy.
Traffic is still pointed at the standby gateway on port ${standby_port}.
Check:
  ${standby_log}
  ${repo_root}/.local/users/gateway-launchd.err.log

EOF
  exit 1
fi

echo "Switching Tailscale Serve back to managed gateway..."
tailscale serve --bg "$gateway_port"
serve_on_standby="0"

if kill -0 "$standby_pid" >/dev/null 2>&1; then
  kill "$standby_pid" >/dev/null 2>&1 || true
fi
standby_pid=""

echo "Replacing existing user dashboard containers so they use the rebuilt image..."
docker ps -a --filter 'name=^/toss-dashboard-' --format '{{.Names}}' | while read -r container_name; do
  if [ -n "$container_name" ]; then
    docker rm -f "$container_name" >/dev/null
    echo "  replaced ${container_name}"
  fi
done

cat <<EOF

Toss gateway updated.

Managed gateway:
  http://${gateway_host}:${gateway_port}/health

Tailscale URL:
  Run: tailscale serve status

EOF
