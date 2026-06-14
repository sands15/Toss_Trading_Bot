#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

python_bin="${PYTHON:-python3.12}"
if ! command -v "$python_bin" >/dev/null 2>&1; then
  python_bin="python3"
fi

if [ ! -d ".venv" ]; then
  "$python_bin" -m venv .venv
fi

.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -e ".[dev]"

mkdir -p state logs

if [ ! -f "config/local.yaml" ]; then
  cp "config/local.example.yaml" "config/local.yaml"
  echo "Created config/local.yaml from config/local.example.yaml"
else
  echo "config/local.yaml already exists; left unchanged"
fi

cat <<'EOF'

Next steps:
1. Fill toss.account_seq in config/local.yaml
2. Set TOSS_CLIENT_ID and TOSS_CLIENT_SECRET as environment variables
3. Run: .venv/bin/python -m turtle_bot --config config/local.yaml --state-db state/turtle.sqlite3 --log-dir logs --ops-check
4. Run: .venv/bin/python -m turtle_bot --config config/local.yaml --state-db state/turtle.sqlite3 --log-dir logs --shadow-service --once
EOF
