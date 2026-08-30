#!/bin/zsh -f
set -eu
umask 077
ulimit -c 0

if (( $# != 0 )); then
  /usr/bin/printf '%s\n' 'unexpected shadow watchdog argument' >&2
  exit 70
fi

if [[ -n "${SSH_CONNECTION:-}" || -n "${SSH_TTY:-}" ]]; then
  /usr/bin/printf '%s\n' 'shadow watchdog must start in the logged-in Aqua session' >&2
  exit 64
fi

for required_name in \
  TOSS_WATCHDOG_HEARTBEAT_ROOT \
  TOSS_WATCHDOG_CONTEXT_PATH \
  TOSS_WATCHDOG_EXPECTATION_PATH \
  TOSS_WATCHDOG_STATE_PATH \
  TOSS_WATCHDOG_RELEASE_SHA \
  TOSS_WATCHDOG_LAUNCHD_DOMAIN; do
  if [[ -z "${(P)required_name:-}" ]]; then
    /usr/bin/printf 'missing required shadow watchdog setting: %s\n' "$required_name" >&2
    exit 66
  fi
done

heartbeat_root="$TOSS_WATCHDOG_HEARTBEAT_ROOT"
context_path="$TOSS_WATCHDOG_CONTEXT_PATH"
expectation_path="$TOSS_WATCHDOG_EXPECTATION_PATH"
state_path="$TOSS_WATCHDOG_STATE_PATH"
release_sha="$TOSS_WATCHDOG_RELEASE_SHA"
launchd_domain="$TOSS_WATCHDOG_LAUNCHD_DOMAIN"
if [[ "$heartbeat_root" != /* || "$context_path" != /* || \
      "${context_path:t}" != news-context.json || "$state_path" != /* || \
      "$expectation_path" != /* || \
      "${expectation_path:t}" != stream-expectation.json || \
      "${expectation_path:h}" != "${context_path:h}" || \
      "$release_sha" == *[^0-9a-f]* || \
      "$launchd_domain" != gui/<-> ]]; then
  /usr/bin/printf '%s\n' 'invalid shadow watchdog setting' >&2
  exit 65
fi
if (( ${#release_sha} != 40 && ${#release_sha} != 64 )); then
  /usr/bin/printf '%s\n' 'invalid shadow watchdog setting' >&2
  exit 65
fi

repo_root="${0:A:h:h}"
if [[ "${repo_root:t}" != "$release_sha" ]]; then
  /usr/bin/printf '%s\n' 'shadow watchdog release SHA mismatch' >&2
  exit 68
fi
python_bin="$repo_root/.venv/bin/python"
watchdog_file="$repo_root/ops/shadow_watchdog.py"
if [[ ! -x "$python_bin" || ! -r "$watchdog_file" ]]; then
  /usr/bin/printf '%s\n' 'shadow watchdog release is incomplete' >&2
  exit 67
fi

exec /usr/bin/env -i \
  HOME="${HOME:?missing HOME}" \
  LANG="en_US.UTF-8" \
  /bin/zsh -f -c '
    set -eu
    umask 077
    ulimit -c 0

    repo_root="$1"
    python_bin="$repo_root/.venv/bin/python"
    watchdog_file="$repo_root/ops/shadow_watchdog.py"
    expected_file="$repo_root/ops/shadow_watchdog.py"
    actual_file="$("$python_bin" -I -c "from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())" "$watchdog_file")"
    if [[ "$actual_file" != "$expected_file" ]]; then
      /usr/bin/printf "%s\n" "shadow watchdog import does not match this release" >&2
      exit 68
    fi

    export TOSS_WATCHDOG_HEARTBEAT_ROOT="$2"
    export TOSS_WATCHDOG_CONTEXT_PATH="$3"
    export TOSS_WATCHDOG_EXPECTATION_PATH="$4"
    export TOSS_WATCHDOG_STATE_PATH="$5"
    export TOSS_WATCHDOG_RELEASE_SHA="$6"
    export TOSS_WATCHDOG_LAUNCHD_DOMAIN="$7"
    cd "$repo_root"
    exec "$python_bin" -I -u "$watchdog_file"
  ' shadow-watchdog-clean \
  "$repo_root" \
  "$heartbeat_root" \
  "$context_path" \
  "$expectation_path" \
  "$state_path" \
  "$release_sha" \
  "$launchd_domain"
