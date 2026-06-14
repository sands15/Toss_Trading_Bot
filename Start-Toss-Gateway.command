#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$repo_root"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "This launcher is intended for macOS. Current system: $(uname -s)" >&2
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
  echo "Open Tailscale, log in, then run this file again." >&2
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

cat <<EOF

Starting Toss multi-user gateway...

When the window prints the gateway info, open the Tailscale HTTPS URL shown by:
  tailscale serve status

If this is the first user, the setup page will ask for:
  - Toss app ID
  - Toss app secret
  - Toss account sequence

EOF

PYTHON="$python_bin" bash ops/run-multi-user-gateway.command
