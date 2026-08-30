#!/bin/sh
set -eu

if [ "$#" -ne 0 ]; then
  printf '%s\n' 'non-live gate accepts no arguments' >&2
  exit 64
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(dirname -- "$script_dir")
if [ -x "$repo_root/.venv/bin/python" ]; then
  python_bin="$repo_root/.venv/bin/python"
else
  python_bin=$(command -v python3)
fi

cd "$repo_root"
path_value=${PATH:-/usr/bin:/bin}
home_value=${HOME:-/tmp}
tmp_value=${TMPDIR:-/tmp}
lang_value=${LANG:-C.UTF-8}

# sandbox-exec is deprecated on newer macOS releases but remains the only
# built-in per-process egress deny on versions where it is present.  This Mac
# release gate refuses to run without that OS-level boundary.
if [ "$(uname -s)" != "Darwin" ] || [ ! -x /usr/bin/sandbox-exec ]; then
  printf '%s\n' 'non-live Mac gate requires sandbox-exec egress isolation' >&2
  exit 78
fi

exec /usr/bin/sandbox-exec \
  -p '(version 1) (allow default) (deny network*)' \
  /usr/bin/env -i \
  PATH="$path_value" \
  HOME="$home_value" \
  TMPDIR="$tmp_value" \
  LANG="$lang_value" \
  NON_LIVE_GATE=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  PYTHONHASHSEED=0 \
  PYTHONNOUSERSITE=1 \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  "$python_bin" -I ops/non_live_gate_runner.py
