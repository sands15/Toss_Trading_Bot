#!/bin/zsh -f
set -eu
umask 077
ulimit -c 0

if (( $# != 0 )); then
  /usr/bin/printf '%s\n' 'unexpected shadow stream argument' >&2
  exit 70
fi

if [[ -n "${SSH_CONNECTION:-}" || -n "${SSH_TTY:-}" ]]; then
  /usr/bin/printf '%s\n' 'shadow stream must start in the logged-in Aqua session' >&2
  exit 64
fi

for required_name in \
  TOSS_STREAM_CONTEXT_PATH \
  TOSS_STREAM_SNAPSHOT_PATH \
  TOSS_STREAM_SIMULATION_CONFIG_PATH \
  TOSS_STREAM_PLAN_DB \
  TOSS_STREAM_HEARTBEAT_PATH \
  TOSS_STREAM_SIMULATION_ID \
  TOSS_STREAM_SIMULATION_START_DATE \
  TOSS_STREAM_SIMULATION_END_DATE \
  TOSS_STREAM_SIMULATION_DB \
  TOSS_STREAM_EXPERIMENT_HASH \
  TOSS_STREAM_KEYCHAIN_SLUG; do
  if [[ -z "${(P)required_name:-}" ]]; then
    /usr/bin/printf 'missing required shadow stream setting: %s\n' "$required_name" >&2
    exit 66
  fi
done

context_path="$TOSS_STREAM_CONTEXT_PATH"
snapshot_path="$TOSS_STREAM_SNAPSHOT_PATH"
simulation_config_path="$TOSS_STREAM_SIMULATION_CONFIG_PATH"
plan_db="$TOSS_STREAM_PLAN_DB"
heartbeat_path="$TOSS_STREAM_HEARTBEAT_PATH"
simulation_id="$TOSS_STREAM_SIMULATION_ID"
simulation_start_date="$TOSS_STREAM_SIMULATION_START_DATE"
simulation_end_date="$TOSS_STREAM_SIMULATION_END_DATE"
simulation_db="$TOSS_STREAM_SIMULATION_DB"
experiment_hash="$TOSS_STREAM_EXPERIMENT_HASH"
keychain_slug="$TOSS_STREAM_KEYCHAIN_SLUG"
repo_root="${0:A:h:h}"
release_sha="${repo_root:t}"
if [[ "$context_path" != /* || "${context_path:t}" != news-context.json ]]; then
  /usr/bin/printf '%s\n' 'invalid shadow stream context path' >&2
  exit 65
fi
if [[ "$snapshot_path" != /* || "${snapshot_path:t}" != market-stream.json ]]; then
  /usr/bin/printf '%s\n' 'invalid shadow stream snapshot path' >&2
  exit 65
fi
if [[ "$simulation_config_path" != /* || \
      "${simulation_config_path:t}" != intraday-simulation.yaml || \
      "$plan_db" != /* || "${plan_db:t}" != intraday.sqlite3 || \
      "$heartbeat_path" != /* || "${heartbeat_path:t}" != heartbeat.json || \
      "${heartbeat_path:h:t}" != stream || \
      "$simulation_db" != /* || "${simulation_db:t}" != intraday-paper.sqlite3 || \
      -z "$simulation_id" || ${#simulation_id} -gt 64 || \
      "$simulation_id" == *[^a-z0-9_-]* || \
      ${#experiment_hash} -ne 64 || "$experiment_hash" == *[^0-9a-f]* ]]; then
  /usr/bin/printf '%s\n' 'invalid paper simulation path' >&2
  exit 65
fi
if [[ "$context_path" == "$snapshot_path" || \
      "$context_path" == "$simulation_config_path" || \
      "$context_path" == "$plan_db" || \
      "$snapshot_path" == "$simulation_config_path" || \
      "$snapshot_path" == "$plan_db" || \
      "$simulation_config_path" == "$plan_db" || \
      "$simulation_db" == "$plan_db" || \
      "$simulation_db" == "$simulation_config_path" || \
      "$heartbeat_path" == "$context_path" || \
      "$heartbeat_path" == "$snapshot_path" || \
      "$heartbeat_path" == "$simulation_config_path" || \
      "$heartbeat_path" == "$plan_db" || \
      "$keychain_slug" == *[^a-z0-9_-]* || \
      "$context_path" == "$repo_root" || "$context_path" == "$repo_root"/* || \
      "$snapshot_path" == "$repo_root" || "$snapshot_path" == "$repo_root"/* || \
      "$simulation_config_path" == "$repo_root" || \
      "$simulation_config_path" == "$repo_root"/* || \
      "$plan_db" == "$repo_root" || "$plan_db" == "$repo_root"/* || \
      "$simulation_db" == "$repo_root" || "$simulation_db" == "$repo_root"/* || \
      "$heartbeat_path" == "$repo_root" || "$heartbeat_path" == "$repo_root"/* || \
      "$release_sha" == *[^0-9a-f]* || \
      (${#release_sha} != 40 && ${#release_sha} != 64) ]]; then
  /usr/bin/printf '%s\n' 'invalid shadow stream setting' >&2
  exit 65
fi

python_bin="$repo_root/.venv/bin/python"
if [[ ! -x "$python_bin" ]]; then
  /usr/bin/printf '%s\n' 'shadow stream virtual environment is missing' >&2
  exit 67
fi

ca_bundle=/etc/ssl/cert.pem
if [[ ! -r "$ca_bundle" || ! -s "$ca_bundle" ]]; then
  /usr/bin/printf '%s\n' 'macOS system CA bundle is unavailable' >&2
  exit 71
fi

# Validate the exact installed release and immutable, account-free experiment
# before reading either Toss credential from Keychain.
/usr/bin/env -i \
  HOME="${HOME:?missing HOME}" \
  LANG="en_US.UTF-8" \
  "$python_bin" -I -c '
import resource
import sys
from pathlib import Path
import turtle_bot
from turtle_bot.config import load_config
from turtle_bot.operations import (
    _normalize_expected_simulation,
    _require_locked_simulation_config,
    _require_shadow_service_config,
)
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
assert resource.getrlimit(resource.RLIMIT_CORE) == (0, 0)
root = Path(sys.argv[1]).resolve()
if root not in Path(turtle_bot.__file__).resolve().parents:
    raise SystemExit("shadow stream import does not match this release")
config = load_config(sys.argv[2])
if config.toss.account_seq is not None:
    raise SystemExit("stream simulation config must not contain account_seq")
if Path(str(config.intraday.news_context_path or "")).resolve() != Path(sys.argv[3]).resolve():
    raise SystemExit("stream context path does not match simulation config")
expected = _normalize_expected_simulation({
    "run_id": sys.argv[5],
    "start_date": sys.argv[6],
    "end_date": sys.argv[7],
    "paper_db": sys.argv[8],
    "experiment_hash": sys.argv[9],
})
_require_shadow_service_config(config)
_require_locked_simulation_config(config, expected=expected, state_db=sys.argv[4])
' "$repo_root" "$simulation_config_path" "$context_path" "$plan_db" \
  "$simulation_id" "$simulation_start_date" "$simulation_end_date" \
  "$simulation_db" "$experiment_hash" || exit $?

# Start the credential-bearing process from a clean environment. The runtime
# shell source arrives on stdin and all handoff values are non-secret named
# environment entries; Keychain values never enter argv.
exec /usr/bin/env -i \
  HOME="${HOME:?missing HOME}" \
  LANG="en_US.UTF-8" \
  TOSS_INTERNAL_REPO_ROOT="$repo_root" \
  TOSS_INTERNAL_CONTEXT_PATH="$context_path" \
  TOSS_INTERNAL_SNAPSHOT_PATH="$snapshot_path" \
  TOSS_INTERNAL_SIMULATION_CONFIG_PATH="$simulation_config_path" \
  TOSS_INTERNAL_PLAN_DB="$plan_db" \
  TOSS_INTERNAL_HEARTBEAT_PATH="$heartbeat_path" \
  TOSS_INTERNAL_SIMULATION_ID="$simulation_id" \
  TOSS_INTERNAL_SIMULATION_START_DATE="$simulation_start_date" \
  TOSS_INTERNAL_SIMULATION_END_DATE="$simulation_end_date" \
  TOSS_INTERNAL_SIMULATION_DB="$simulation_db" \
  TOSS_INTERNAL_EXPERIMENT_HASH="$experiment_hash" \
  TOSS_INTERNAL_KEYCHAIN_SLUG="$keychain_slug" \
  /bin/zsh -f -s <<'TOSS_STREAM_RUNTIME'
    set -eu
    umask 077
    ulimit -c 0
    repo_root="${TOSS_INTERNAL_REPO_ROOT:?}"
    python_bin="$repo_root/.venv/bin/python"
    context_path="${TOSS_INTERNAL_CONTEXT_PATH:?}"
    snapshot_path="${TOSS_INTERNAL_SNAPSHOT_PATH:?}"
    simulation_config_path="${TOSS_INTERNAL_SIMULATION_CONFIG_PATH:?}"
    plan_db="${TOSS_INTERNAL_PLAN_DB:?}"
    heartbeat_path="${TOSS_INTERNAL_HEARTBEAT_PATH:?}"
    simulation_id="${TOSS_INTERNAL_SIMULATION_ID:?}"
    simulation_start_date="${TOSS_INTERNAL_SIMULATION_START_DATE:?}"
    simulation_end_date="${TOSS_INTERNAL_SIMULATION_END_DATE:?}"
    simulation_db="${TOSS_INTERNAL_SIMULATION_DB:?}"
    experiment_hash="${TOSS_INTERNAL_EXPERIMENT_HASH:?}"
    keychain_slug="${TOSS_INTERNAL_KEYCHAIN_SLUG:?}"
    release_sha="${repo_root:t}"
    unset TOSS_INTERNAL_REPO_ROOT TOSS_INTERNAL_CONTEXT_PATH \
      TOSS_INTERNAL_SNAPSHOT_PATH TOSS_INTERNAL_SIMULATION_CONFIG_PATH \
      TOSS_INTERNAL_PLAN_DB TOSS_INTERNAL_HEARTBEAT_PATH \
      TOSS_INTERNAL_SIMULATION_ID TOSS_INTERNAL_SIMULATION_START_DATE \
      TOSS_INTERNAL_SIMULATION_END_DATE TOSS_INTERNAL_SIMULATION_DB \
      TOSS_INTERNAL_EXPERIMENT_HASH TOSS_INTERNAL_KEYCHAIN_SLUG

    ca_bundle=/etc/ssl/cert.pem
    if [[ ! -r "$ca_bundle" || ! -s "$ca_bundle" ]]; then
      /usr/bin/printf '%s\n' 'macOS system CA bundle is unavailable' >&2
      exit 71
    fi
    export SSL_CERT_FILE="$ca_bundle"

    client_id="$(/usr/bin/security find-generic-password \
      -w -s toss-trading-bot -a "$keychain_slug:toss_client_id" \
      2>/dev/null)" || {
        /usr/bin/printf '%s\n' 'Toss client ID is unavailable from Keychain' >&2
        exit 69
      }
    client_secret="$(/usr/bin/security find-generic-password \
      -w -s toss-trading-bot -a "$keychain_slug:toss_client_secret" \
      2>/dev/null)" || {
        client_id=
        /usr/bin/printf '%s\n' 'Toss client secret is unavailable from Keychain' >&2
        exit 69
      }
    if [[ -z "$client_id" || -z "$client_secret" || \
          "$client_id" == *[[:space:]]* || "$client_secret" == *[[:space:]]* ]]; then
      client_id=
      client_secret=
      /usr/bin/printf '%s\n' 'Toss credentials failed local validation' >&2
      exit 69
    fi

    export TOSS_CLIENT_ID="$client_id"
    export TOSS_CLIENT_SECRET="$client_secret"
    client_id=
    client_secret=
    cd "$repo_root"
    exec "$python_bin" -I -u -m turtle_bot.toss_stream \
      --context "$context_path" \
      --snapshot "$snapshot_path" \
      --simulation-config "$simulation_config_path" \
      --plan-db "$plan_db" \
      --heartbeat "$heartbeat_path" \
      --release-sha "$release_sha" \
      --expected-simulation-id "$simulation_id" \
      --expected-simulation-start-date "$simulation_start_date" \
      --expected-simulation-end-date "$simulation_end_date" \
      --expected-simulation-db "$simulation_db" \
      --expected-experiment-hash "$experiment_hash"
TOSS_STREAM_RUNTIME
