#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

python_bin="${PYTHON:-}"
if [ -z "$python_bin" ]; then
  if [ -x ".venv/bin/python" ]; then
    python_bin=".venv/bin/python"
  elif command -v python3.12 >/dev/null 2>&1; then
    python_bin="python3.12"
  else
    python_bin="python3"
  fi
fi

if [ ! -d ".venv" ]; then
  echo "Creating .venv..."
  "$python_bin" -m venv .venv
fi

python_bin=".venv/bin/python"

echo "Installing/updating local package..."
"$python_bin" -m pip install -U pip >/dev/null
"$python_bin" -m pip install -e ".[dev]" >/dev/null

mkdir -p state logs

config_path="${CONFIG_PATH:-config/local.yaml}"
state_db="${STATE_DB:-state/turtle.sqlite3}"
dashboard_port="${DASHBOARD_PORT:-8765}"

if [ ! -f "$config_path" ]; then
  if [ "$config_path" = "config/local.yaml" ] && [ -f "config/local.example.yaml" ]; then
    cp "config/local.example.yaml" "$config_path"
    echo "Created $config_path from config/local.example.yaml"
  else
    echo "Config file not found: $config_path" >&2
    exit 1
  fi
fi

tailscale_ip=""
if command -v tailscale >/dev/null 2>&1; then
  tailscale_ip="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"
fi

dashboard_host="${DASHBOARD_HOST:-${tailscale_ip:-127.0.0.1}}"

cat <<EOF

Toss Turtle Bot dashboard
Host: $dashboard_host
Port: $dashboard_port
Config: $config_path

Open on this Mac:
  http://$dashboard_host:$dashboard_port/
EOF

if [ -n "$tailscale_ip" ] && { [ "$dashboard_host" = "$tailscale_ip" ] || [ "$dashboard_host" = "0.0.0.0" ]; }; then
  cat <<EOF

Open from another Tailscale device:
  http://$tailscale_ip:$dashboard_port/
EOF
elif [ -z "$tailscale_ip" ]; then
  cat <<EOF

Tailscale IP was not detected.
Install/login to Tailscale, then run:
  tailscale ip -4

Then open:
  http://<that-ip>:$dashboard_port/
EOF
else
  cat <<EOF

Tailscale IP detected: $tailscale_ip
Remote Tailnet access is disabled because DASHBOARD_HOST is set to $dashboard_host.
Set DASHBOARD_HOST=$tailscale_ip to expose only on Tailscale.
EOF
fi

cat <<EOF

If macOS asks about incoming connections, allow Python.
Press Ctrl+C in this window to stop the dashboard.

EOF

exec "$python_bin" -m turtle_bot \
  --dashboard-server \
  --config "$config_path" \
  --state-db "$state_db" \
  --host "$dashboard_host" \
  --port "$dashboard_port"
