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
  TOSS_WATCHDOG_LAUNCHD_DOMAIN \
  TOSS_WATCHDOG_ALLOWED_CHANNEL_ID; do
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
channel_id="$TOSS_WATCHDOG_ALLOWED_CHANNEL_ID"
if [[ "$heartbeat_root" != /* || "$context_path" != /* || \
      "${context_path:t}" != news-context.json || "$state_path" != /* || \
      "$expectation_path" != /* || \
      "${expectation_path:t}" != stream-expectation.json || \
      "${expectation_path:h}" != "${context_path:h}" || \
      "$release_sha" == *[^0-9a-f]* || \
      "$launchd_domain" != gui/<-> || "$channel_id" == *[^0-9]* ]]; then
  /usr/bin/printf '%s\n' 'invalid shadow watchdog setting' >&2
  exit 65
fi
if (( (${#release_sha} != 40 && ${#release_sha} != 64) || \
      ${#channel_id} < 17 || ${#channel_id} > 20 )); then
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

ca_bundle=/etc/ssl/cert.pem
if [[ ! -r "$ca_bundle" || ! -s "$ca_bundle" ]]; then
  /usr/bin/printf '%s\n' 'macOS system CA bundle is unavailable' >&2
  exit 71
fi

# Validate the exact installed release before reading the existing approval-bot token.
actual_file="$(/usr/bin/env -i \
  HOME="${HOME:?missing HOME}" \
  LANG="en_US.UTF-8" \
  "$python_bin" -I -c \
  'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' \
  "$watchdog_file")" || exit $?
if [[ "$actual_file" != "$watchdog_file" ]]; then
  /usr/bin/printf '%s\n' 'shadow watchdog import does not match this release' >&2
  exit 68
fi

exec /usr/bin/env -i \
  HOME="${HOME:?missing HOME}" \
  LANG="en_US.UTF-8" \
  SSL_CERT_FILE="$ca_bundle" \
  /bin/zsh -f -c '
    set -eu
    umask 077
    ulimit -c 0

    repo_root="$1"
    channel_id="$8"
    python_bin="$repo_root/.venv/bin/python"
    watchdog_file="$repo_root/ops/shadow_watchdog.py"
    expected_file="$repo_root/ops/shadow_watchdog.py"
    actual_file="$("$python_bin" -I -c "from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())" "$watchdog_file")"
    if [[ "$actual_file" != "$expected_file" ]]; then
      /usr/bin/printf "%s\n" "shadow watchdog import does not match this release" >&2
      exit 68
    fi

    discord_token="$(/usr/bin/security find-generic-password \
      -w \
      -s TossTradingBot.DiscordApprovalToken \
      -a discord-approval-bot \
      2>/dev/null)" || {
        /usr/bin/printf "%s\n" "watchdog token is unavailable from Keychain" >&2
        exit 69
      }
    if [[ ${#discord_token} -lt 40 || ${#discord_token} -gt 256 || \
          "$discord_token" == *[^A-Za-z0-9._-]* ]]; then
      discord_token=
      /usr/bin/printf "%s\n" "watchdog token failed local validation" >&2
      exit 69
    fi

    export TOSS_WATCHDOG_HEARTBEAT_ROOT="$2"
    export TOSS_WATCHDOG_CONTEXT_PATH="$3"
    export TOSS_WATCHDOG_EXPECTATION_PATH="$4"
    export TOSS_WATCHDOG_STATE_PATH="$5"
    export TOSS_WATCHDOG_RELEASE_SHA="$6"
    export TOSS_WATCHDOG_LAUNCHD_DOMAIN="$7"
    export TOSS_WATCHDOG_DISCORD_BOT_TOKEN="$discord_token"
    export TOSS_WATCHDOG_ALLOWED_CHANNEL_ID="$channel_id"
    discord_token=
    channel_id=
    cd "$repo_root"
    exec "$python_bin" -I -u "$watchdog_file"
  ' shadow-watchdog-clean \
  "$repo_root" \
  "$heartbeat_root" \
  "$context_path" \
  "$expectation_path" \
  "$state_path" \
  "$release_sha" \
  "$launchd_domain" \
  "$channel_id"
