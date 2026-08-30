#!/bin/zsh -f
set -eu
umask 077
ulimit -c 0

if (( $# != 0 )); then
  /usr/bin/printf '%s\n' 'unexpected approval worker argument' >&2
  exit 70
fi

if [[ -n "${SSH_CONNECTION:-}" || -n "${SSH_TTY:-}" ]]; then
  /usr/bin/printf '%s\n' 'approval worker must start in the logged-in Aqua session' >&2
  exit 64
fi

for required_name in \
  DISCORD_ALLOWED_GUILD_ID \
  DISCORD_ALLOWED_CHANNEL_ID \
  DISCORD_ALLOWED_USER_ID \
  DISCORD_APPROVAL_ENVELOPE_PATH \
  DISCORD_APPROVAL_INBOX_DIR \
  DISCORD_APPROVAL_HEARTBEAT_PATH; do
  if [[ -z "${(P)required_name:-}" ]]; then
    /usr/bin/printf 'missing required approval setting: %s\n' "$required_name" >&2
    exit 66
  fi
done

for public_id in \
  "$DISCORD_ALLOWED_GUILD_ID" \
  "$DISCORD_ALLOWED_CHANNEL_ID" \
  "$DISCORD_ALLOWED_USER_ID"; do
  if [[ "$public_id" == *[^0-9]* || \
        ${#public_id} -lt 17 || ${#public_id} -gt 20 ]]; then
    /usr/bin/printf '%s\n' 'invalid approval worker setting' >&2
    exit 65
  fi
done
if [[ "$DISCORD_APPROVAL_ENVELOPE_PATH" != /* || \
      "${DISCORD_APPROVAL_ENVELOPE_PATH:t}" != approval-envelope.json || \
      "$DISCORD_APPROVAL_INBOX_DIR" != /* || \
      "${DISCORD_APPROVAL_INBOX_DIR:t}" != approval-inbox || \
      "$DISCORD_APPROVAL_HEARTBEAT_PATH" != /* || \
      "${DISCORD_APPROVAL_HEARTBEAT_PATH:t}" != heartbeat.json || \
      "${DISCORD_APPROVAL_HEARTBEAT_PATH:h:t}" != approval ]]; then
  /usr/bin/printf '%s\n' 'invalid approval worker setting' >&2
  exit 65
fi

repo_root="${0:A:h:h}"
release_sha="${repo_root:t}"
if [[ "$release_sha" == *[^0-9a-f]* || \
      (${#release_sha} != 40 && ${#release_sha} != 64) ]]; then
  /usr/bin/printf '%s\n' 'approval worker release SHA mismatch' >&2
  exit 65
fi
if [[ "$DISCORD_APPROVAL_ENVELOPE_PATH" == "$repo_root" || \
      "$DISCORD_APPROVAL_ENVELOPE_PATH" == "$repo_root"/* || \
      "$DISCORD_APPROVAL_INBOX_DIR" == "$repo_root" || \
      "$DISCORD_APPROVAL_INBOX_DIR" == "$repo_root"/* || \
      "$DISCORD_APPROVAL_HEARTBEAT_PATH" == "$repo_root" || \
      "$DISCORD_APPROVAL_HEARTBEAT_PATH" == "$repo_root"/* ]]; then
  /usr/bin/printf '%s\n' 'approval runtime must be outside the release' >&2
  exit 65
fi
python_bin="$repo_root/.venv/bin/python"
if [[ ! -x "$python_bin" ]]; then
  /usr/bin/printf '%s\n' 'approval worker virtual environment is missing' >&2
  exit 65
fi

ca_bundle=/etc/ssl/cert.pem
if [[ ! -r "$ca_bundle" || ! -s "$ca_bundle" ]]; then
  /usr/bin/printf '%s\n' 'macOS system CA bundle is unavailable' >&2
  exit 71
fi

# Validate the exact installed release before reading the bot token.
/usr/bin/env -i \
  HOME="${HOME:?missing HOME}" \
  LANG="en_US.UTF-8" \
  SSL_CERT_FILE="$ca_bundle" \
  "$python_bin" -I -c '
import resource
import sys
from pathlib import Path
import turtle_approval
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
assert resource.getrlimit(resource.RLIMIT_CORE) == (0, 0)
root = Path(sys.argv[1]).resolve()
if root not in Path(turtle_approval.__file__).resolve().parents:
    raise SystemExit("approval worker import does not match this release")
' "$repo_root" || exit $?

token_value="$(/usr/bin/security find-generic-password \
  -w \
  -s TossTradingBot.DiscordApprovalToken \
  -a discord-approval-bot \
  2>/dev/null)" || {
    /usr/bin/printf '%s\n' 'approval token is unavailable from Keychain' >&2
    exit 68
  }
if [[ ${#token_value} -lt 40 || "$token_value" == *[[:space:]]* ]]; then
  token_value=
  /usr/bin/printf '%s\n' 'approval token failed local validation' >&2
  exit 69
fi

umask 077
ulimit -c 0
cd "$repo_root"
exec /usr/bin/env -i \
  HOME="${HOME:?missing HOME}" \
  LANG="en_US.UTF-8" \
  SSL_CERT_FILE="$ca_bundle" \
  DISCORD_ALLOWED_GUILD_ID="$DISCORD_ALLOWED_GUILD_ID" \
  DISCORD_ALLOWED_CHANNEL_ID="$DISCORD_ALLOWED_CHANNEL_ID" \
  DISCORD_ALLOWED_USER_ID="$DISCORD_ALLOWED_USER_ID" \
  DISCORD_APPROVAL_ENVELOPE_PATH="$DISCORD_APPROVAL_ENVELOPE_PATH" \
  DISCORD_APPROVAL_INBOX_DIR="$DISCORD_APPROVAL_INBOX_DIR" \
  DISCORD_APPROVAL_BOT_TOKEN="$token_value" \
  "$python_bin" -I -u -c '
import resource, sys
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
assert resource.getrlimit(resource.RLIMIT_CORE) == (0, 0)
from turtle_approval.worker import main
from turtle_runtime.heartbeat import RedactedHeartbeatWriter
writer = RedactedHeartbeatWriter(
    sys.argv[1], release_sha=sys.argv[2], component="approval"
)
writer.write("STARTING")
result = main(heartbeat=lambda status: writer.write(status))
writer.write("STOPPED" if result == 0 else "ERROR")
raise SystemExit(result)
' "$DISCORD_APPROVAL_HEARTBEAT_PATH" "$release_sha"
