#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker was not found. Install Docker Desktop on this Mac first." >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker is installed, but the Docker engine is not running. Start Docker Desktop first." >&2
  exit 1
fi

python_bin="${PYTHON:-python3}"
gateway_port="${GATEWAY_PORT:-8765}"
gateway_host="${GATEWAY_HOST:-}"
registration_allowlist="${REGISTRATION_ALLOWLIST:-}"
setup_rate_limit="${SETUP_RATE_LIMIT:-5}"
setup_rate_window_seconds="${SETUP_RATE_WINDOW_SECONDS:-900}"
container_memory="${CONTAINER_MEMORY:-512m}"
container_cpus="${CONTAINER_CPUS:-1.0}"
container_log_max_size="${CONTAINER_LOG_MAX_SIZE:-10m}"
container_log_max_files="${CONTAINER_LOG_MAX_FILES:-3}"
secret_backend="${SECRET_BACKEND:-auto}"
keychain_service="${KEYCHAIN_SERVICE:-toss-trading-bot}"

if [ -z "$gateway_host" ] && command -v tailscale >/dev/null 2>&1; then
  gateway_host="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"
fi
gateway_host="${gateway_host:-127.0.0.1}"

cat <<EOF

Toss multi-user dashboard gateway
Gateway URL:
  http://${gateway_host}:${gateway_port}/

Visitors will see a login page.
First-time users can create an account and get their own Docker container.
Set REGISTRATION_ALLOWLIST to restrict first-time signup by IP/CIDR.
On macOS, SECRET_BACKEND=auto stores Toss API credentials in Keychain.

Press Ctrl+C in this window to stop the gateway.
User containers keep running until stopped with Docker.

EOF

exec "$python_bin" ops/multi_user_gateway.py \
  --host "$gateway_host" \
  --port "$gateway_port" \
  --registration-allowlist "$registration_allowlist" \
  --setup-rate-limit "$setup_rate_limit" \
  --setup-rate-window-seconds "$setup_rate_window_seconds" \
  --container-memory "$container_memory" \
  --container-cpus "$container_cpus" \
  --container-log-max-size "$container_log_max_size" \
  --container-log-max-files "$container_log_max_files" \
  --secret-backend "$secret_backend" \
  --keychain-service "$keychain_service"
