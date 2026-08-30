#!/bin/zsh -f
set -eu
umask 077
ulimit -c 0

if (( $# != 0 )); then
  /usr/bin/printf '%s\n' 'unexpected intraday shadow argument' >&2
  exit 70
fi

if [[ -n "${SSH_CONNECTION:-}" || -n "${SSH_TTY:-}" ]]; then
  /usr/bin/printf '%s\n' 'intraday shadow must start in the logged-in Aqua session' >&2
  exit 64
fi

for required_name in \
  TOSS_SHADOW_CONFIG_PATH \
  TOSS_SHADOW_STATE_DB \
  TOSS_SHADOW_LOG_DIR \
  TOSS_SHADOW_ALLOWED_CHANNEL_ID \
  TOSS_SHADOW_HEARTBEAT_PATH \
  TOSS_SHADOW_SIMULATION_ID \
  TOSS_SHADOW_SIMULATION_START_DATE \
  TOSS_SHADOW_SIMULATION_END_DATE \
  TOSS_SHADOW_SIMULATION_DB \
  TOSS_SHADOW_EXPERIMENT_HASH \
  TOSS_SHADOW_ACCOUNT_FINGERPRINT \
  TOSS_SHADOW_KEYCHAIN_SLUG; do
  if [[ -z "${(P)required_name:-}" ]]; then
    /usr/bin/printf 'missing required intraday shadow setting: %s\n' "$required_name" >&2
    exit 66
  fi
done

config_path="$TOSS_SHADOW_CONFIG_PATH"
state_db="$TOSS_SHADOW_STATE_DB"
log_dir="$TOSS_SHADOW_LOG_DIR"
heartbeat_path="$TOSS_SHADOW_HEARTBEAT_PATH"
simulation_id="$TOSS_SHADOW_SIMULATION_ID"
simulation_start_date="$TOSS_SHADOW_SIMULATION_START_DATE"
simulation_end_date="$TOSS_SHADOW_SIMULATION_END_DATE"
simulation_db="$TOSS_SHADOW_SIMULATION_DB"
experiment_hash="$TOSS_SHADOW_EXPERIMENT_HASH"
account_fingerprint="$TOSS_SHADOW_ACCOUNT_FINGERPRINT"
keychain_slug="$TOSS_SHADOW_KEYCHAIN_SLUG"
channel_id="$TOSS_SHADOW_ALLOWED_CHANNEL_ID"
repo_root="${0:A:h:h}"
release_sha="${repo_root:t}"
if [[ "$config_path" != /* || "${config_path:t}" != intraday-simulation.yaml || \
      "$state_db" != /* || "$log_dir" != /* || \
      "$heartbeat_path" != /* || "${heartbeat_path:t}" != heartbeat.json || \
      "${heartbeat_path:h:t}" != planner || \
      "$simulation_db" != /* || "${simulation_db:t}" != intraday-paper.sqlite3 || \
      -z "$simulation_id" || ${#simulation_id} -gt 64 || \
      "$simulation_id" == *[^a-z0-9_-]* || \
      ${#experiment_hash} -ne 64 || "$experiment_hash" == *[^0-9a-f]* || \
      ${#account_fingerprint} -ne 64 || "$account_fingerprint" == *[^0-9a-f]* || \
      "$config_path" == "$state_db" || "$keychain_slug" == *[^a-z0-9_-]* || \
      "$simulation_db" == "$state_db" || "$simulation_db" == "$config_path" || \
      "$config_path" == "$repo_root" || "$config_path" == "$repo_root"/* || \
      "$state_db" == "$repo_root" || "$state_db" == "$repo_root"/* || \
      "$simulation_db" == "$repo_root" || "$simulation_db" == "$repo_root"/* || \
      "$log_dir" == "$repo_root" || "$log_dir" == "$repo_root"/* || \
      "$heartbeat_path" == "$repo_root" || "$heartbeat_path" == "$repo_root"/* || \
      "$channel_id" == *[^0-9]* || \
      ${#channel_id} -lt 17 || ${#channel_id} -gt 20 || \
      "$release_sha" == *[^0-9a-f]* ]]; then
  /usr/bin/printf '%s\n' 'invalid intraday shadow setting' >&2
  exit 65
fi
if (( ${#release_sha} != 40 && ${#release_sha} != 64 )); then
  /usr/bin/printf '%s\n' 'invalid intraday shadow setting' >&2
  exit 65
fi

python_bin="$repo_root/.venv/bin/python"
if [[ ! -x "$python_bin" || ! -r "$config_path" ]]; then
  /usr/bin/printf '%s\n' 'intraday shadow release or config is unavailable' >&2
  exit 67
fi

# The clean preflight verifies both the exact release import and the hard-off
# configuration before this wrapper asks Keychain for either Toss credential.
/usr/bin/env -i \
  HOME="${HOME:?missing HOME}" \
  LANG="en_US.UTF-8" \
  "$python_bin" -I -c '
import os, resource, stat, sys
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
assert resource.getrlimit(resource.RLIMIT_CORE) == (0, 0)
from pathlib import Path
import turtle_bot
from turtle_bot.config import load_config
from turtle_bot.operations import (
    _normalize_expected_simulation,
    _require_locked_simulation_config,
    _require_shadow_service_config,
)
root = Path(sys.argv[1]).resolve()
actual = Path(turtle_bot.__file__).resolve()
if root not in actual.parents:
    raise SystemExit("intraday shadow import does not match this release")
config_path = Path(sys.argv[2])
metadata = config_path.lstat()
if (
    not stat.S_ISREG(metadata.st_mode)
    or metadata.st_uid != os.getuid()
    or stat.S_IMODE(metadata.st_mode) & 0o077
):
    raise SystemExit("intraday shadow config is not a private owner file")
config = load_config(config_path)
expected = _normalize_expected_simulation({
    "run_id": sys.argv[4],
    "start_date": sys.argv[5],
    "end_date": sys.argv[6],
    "paper_db": sys.argv[7],
    "experiment_hash": sys.argv[8],
})
_require_shadow_service_config(config)
_require_locked_simulation_config(
    config,
    expected=expected,
    state_db=sys.argv[3],
    expected_account_fingerprint=sys.argv[9],
)
' \
  "$repo_root" \
  "$config_path" \
  "$state_db" \
  "$simulation_id" \
  "$simulation_start_date" \
  "$simulation_end_date" \
  "$simulation_db" \
  "$experiment_hash" \
  "$account_fingerprint" || exit $?

# The runtime shell receives only non-secret named values from a clean
# environment. Its source arrives on stdin so shell quoting cannot truncate the
# handoff. Keychain values remain locals until export and never enter argv.
exec /usr/bin/env -i \
  HOME="${HOME:?missing HOME}" \
  LANG="en_US.UTF-8" \
  TOSS_INTERNAL_REPO_ROOT="$repo_root" \
  TOSS_INTERNAL_CONFIG_PATH="$config_path" \
  TOSS_INTERNAL_STATE_DB="$state_db" \
  TOSS_INTERNAL_LOG_DIR="$log_dir" \
  TOSS_INTERNAL_KEYCHAIN_SLUG="$keychain_slug" \
  TOSS_INTERNAL_CHANNEL_ID="$channel_id" \
  TOSS_INTERNAL_HEARTBEAT_PATH="$heartbeat_path" \
  TOSS_INTERNAL_SIMULATION_ID="$simulation_id" \
  TOSS_INTERNAL_SIMULATION_START_DATE="$simulation_start_date" \
  TOSS_INTERNAL_SIMULATION_END_DATE="$simulation_end_date" \
  TOSS_INTERNAL_SIMULATION_DB="$simulation_db" \
  TOSS_INTERNAL_EXPERIMENT_HASH="$experiment_hash" \
  TOSS_INTERNAL_ACCOUNT_FINGERPRINT="$account_fingerprint" \
  /bin/zsh -f -s <<'TOSS_SHADOW_RUNTIME'
    set -eu
    umask 077
    ulimit -c 0
    repo_root="${TOSS_INTERNAL_REPO_ROOT:?}"
    python_bin="$repo_root/.venv/bin/python"
    config_path="${TOSS_INTERNAL_CONFIG_PATH:?}"
    state_db="${TOSS_INTERNAL_STATE_DB:?}"
    log_dir="${TOSS_INTERNAL_LOG_DIR:?}"
    keychain_slug="${TOSS_INTERNAL_KEYCHAIN_SLUG:?}"
    channel_id="${TOSS_INTERNAL_CHANNEL_ID:?}"
    heartbeat_path="${TOSS_INTERNAL_HEARTBEAT_PATH:?}"
    simulation_id="${TOSS_INTERNAL_SIMULATION_ID:?}"
    simulation_start_date="${TOSS_INTERNAL_SIMULATION_START_DATE:?}"
    simulation_end_date="${TOSS_INTERNAL_SIMULATION_END_DATE:?}"
    simulation_db="${TOSS_INTERNAL_SIMULATION_DB:?}"
    experiment_hash="${TOSS_INTERNAL_EXPERIMENT_HASH:?}"
    account_fingerprint="${TOSS_INTERNAL_ACCOUNT_FINGERPRINT:?}"
    release_sha="${repo_root:t}"
    unset TOSS_INTERNAL_REPO_ROOT TOSS_INTERNAL_CONFIG_PATH \
      TOSS_INTERNAL_STATE_DB TOSS_INTERNAL_LOG_DIR \
      TOSS_INTERNAL_KEYCHAIN_SLUG TOSS_INTERNAL_CHANNEL_ID \
      TOSS_INTERNAL_HEARTBEAT_PATH TOSS_INTERNAL_SIMULATION_ID \
      TOSS_INTERNAL_SIMULATION_START_DATE TOSS_INTERNAL_SIMULATION_END_DATE \
      TOSS_INTERNAL_SIMULATION_DB TOSS_INTERNAL_EXPERIMENT_HASH \
      TOSS_INTERNAL_ACCOUNT_FINGERPRINT

    ca_bundle=/etc/ssl/cert.pem
    if [[ ! -r "$ca_bundle" || ! -s "$ca_bundle" ]]; then
      /usr/bin/printf '%s\n' 'macOS system CA bundle is unavailable' >&2
      exit 71
    fi
    export SSL_CERT_FILE="$ca_bundle"

    client_id="$(/usr/bin/security find-generic-password \
      -w -s toss-trading-bot -a "${keychain_slug}:toss_client_id" \
      2>/dev/null)" || {
        /usr/bin/printf '%s\n' 'Toss client ID is unavailable from Keychain' >&2
        exit 69
      }
    client_secret="$(/usr/bin/security find-generic-password \
      -w -s toss-trading-bot -a "${keychain_slug}:toss_client_secret" \
      2>/dev/null)" || {
        client_id=
        /usr/bin/printf '%s\n' 'Toss client secret is unavailable from Keychain' >&2
        exit 69
      }
    trade_webhook="$(/usr/bin/security find-generic-password \
      -w -s TossTradingBot.DiscordTradeWebhook -a discord-trade-webhook \
      2>/dev/null)" || trade_webhook=
    trade_webhook_pattern='^https://discord[.]com/api(/v[0-9]+)?/webhooks/[0-9]+/[^/?#[:space:]]+/?$'
    if [[ -z "$client_id" || -z "$client_secret" || \
          "$client_id" == *[[:space:]]* || "$client_secret" == *[[:space:]]* ]]; then
      client_id=
      client_secret=
      trade_webhook=
      trade_webhook_pattern=
      /usr/bin/printf '%s\n' 'Toss credentials failed local validation' >&2
      exit 69
    fi
    if [[ -n "$trade_webhook" && ! "$trade_webhook" =~ "$trade_webhook_pattern" ]]; then
      client_id=
      client_secret=
      trade_webhook=
      trade_webhook_pattern=
      /usr/bin/printf '%s\n' 'Discord trade webhook failed local validation' >&2
      exit 69
    fi
    trade_webhook_pattern=

    export TOSS_CLIENT_ID="$client_id"
    export TOSS_CLIENT_SECRET="$client_secret"
    export DISCORD_TRADE_ALERT_WEBHOOK_URL="$trade_webhook"
    export DISCORD_ALLOWED_CHANNEL_ID="$channel_id"
    client_id=
    client_secret=
    trade_webhook=
    cd "$repo_root"
    exec "$python_bin" -I -u -c '
import os, resource, sqlite3, sys, time
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
assert resource.getrlimit(resource.RLIMIT_CORE) == (0, 0)
from pathlib import Path
from turtle_bot.config import load_config
from turtle_bot.operations import run_paper_service
from turtle_runtime.heartbeat import HeartbeatError, RedactedHeartbeatWriter
client_id = os.environ.pop("TOSS_CLIENT_ID", "")
client_secret = os.environ.pop("TOSS_CLIENT_SECRET", "")
trade_webhook = os.environ.pop("DISCORD_TRADE_ALERT_WEBHOOK_URL", "")
channel_id = os.environ.pop("DISCORD_ALLOWED_CHANNEL_ID", "")
config = load_config(sys.argv[1])
env = {
    config.toss.client_id_env: client_id,
    config.toss.client_secret_env: client_secret,
    "DISCORD_TRADE_ALERT_WEBHOOK_URL": trade_webhook,
    "DISCORD_ALLOWED_CHANNEL_ID": channel_id,
}
writer = RedactedHeartbeatWriter(
    sys.argv[4], release_sha=sys.argv[5], component="planner"
)
databases = (Path(sys.argv[2]).resolve(), Path(sys.argv[9]).resolve())
def db_quick_check():
    try:
        for database in databases:
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2) as db:
                if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    return "fail"
        return "ok"
    except (OSError, sqlite3.Error):
        return "fail"
writer.write("STARTING", db_quick_check=db_quick_check())
def heartbeat_sleep(seconds):
    checked = db_quick_check()
    writer.write(
        "OK" if checked == "ok" else "DEGRADED",
        baseline_fresh=True,
        db_quick_check=checked,
    )
    time.sleep(seconds)
try:
    run_paper_service(
        config_path=sys.argv[1],
        state_db=sys.argv[2],
        log_dir=sys.argv[3],
        interval_seconds=config.runtime.interval_seconds,
        env=env,
        expected_mode="shadow",
        expected_simulation={
            "run_id": sys.argv[6],
            "start_date": sys.argv[7],
            "end_date": sys.argv[8],
            "paper_db": sys.argv[9],
            "experiment_hash": sys.argv[10],
        },
        expected_account_fingerprint=sys.argv[11],
        sleep=heartbeat_sleep,
    )
except BaseException:
    try:
        writer.write("ERROR", db_quick_check=db_quick_check())
    except HeartbeatError:
        pass
    raise
' "$config_path" "$state_db" "$log_dir" "$heartbeat_path" "$release_sha" \
    "$simulation_id" "$simulation_start_date" "$simulation_end_date" \
    "$simulation_db" "$experiment_hash" "$account_fingerprint"
TOSS_SHADOW_RUNTIME
