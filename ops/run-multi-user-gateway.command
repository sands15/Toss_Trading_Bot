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

if [ -z "$gateway_host" ] && command -v tailscale >/dev/null 2>&1; then
  gateway_host="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"
fi
gateway_host="${gateway_host:-127.0.0.1}"

cat <<EOF

Toss multi-user dashboard gateway
Gateway URL:
  http://${gateway_host}:${gateway_port}/

Unknown Tailscale client IPs will see a setup page.
Known client IPs will be routed to their own Docker container.

Press Ctrl+C in this window to stop the gateway.
User containers keep running until stopped with Docker.

EOF

exec "$python_bin" ops/multi_user_gateway.py \
  --host "$gateway_host" \
  --port "$gateway_port"
