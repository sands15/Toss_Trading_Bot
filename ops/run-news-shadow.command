#!/bin/zsh -f
set -eu
umask 077
ulimit -c 0

if (( $# != 0 )); then
  /usr/bin/printf '%s\n' 'unexpected news shadow argument' >&2
  exit 70
fi

if [[ -n "${SSH_CONNECTION:-}" || -n "${SSH_TTY:-}" ]]; then
  /usr/bin/printf '%s\n' 'news shadow must start in the logged-in Aqua session' >&2
  exit 64
fi

for required_name in \
  TOSS_NEWS_CONFIG_PATH \
  TOSS_NEWS_ALLOWED_CHANNEL_ID \
  TOSS_NEWS_HEARTBEAT_PATH; do
  if [[ -z "${(P)required_name:-}" ]]; then
    /usr/bin/printf 'missing required news shadow setting: %s\n' "$required_name" >&2
    exit 66
  fi
done

config_path="$TOSS_NEWS_CONFIG_PATH"
channel_id="$TOSS_NEWS_ALLOWED_CHANNEL_ID"
heartbeat_path="$TOSS_NEWS_HEARTBEAT_PATH"
llm_env_name="${TOSS_NEWS_LLM_API_KEY_ENV:-}"
repo_root="${0:A:h:h}"
release_sha="${repo_root:t}"
if [[ "$config_path" != /* || "$channel_id" == *[^0-9]* || \
      "$heartbeat_path" != /* || "${heartbeat_path:t}" != heartbeat.json || \
      "${heartbeat_path:h:t}" != news || \
      "$config_path" == "$repo_root" || "$config_path" == "$repo_root"/* || \
      "$heartbeat_path" == "$repo_root" || "$heartbeat_path" == "$repo_root"/* || \
      "$release_sha" == *[^0-9a-f]* || \
      ( -n "$llm_env_name" && "$llm_env_name" != NEWS_LLM_API_KEY ) ]]; then
  /usr/bin/printf '%s\n' 'invalid news shadow setting' >&2
  exit 65
fi
if (( ${#channel_id} < 17 || ${#channel_id} > 20 || \
      (${#release_sha} != 40 && ${#release_sha} != 64) )); then
  /usr/bin/printf '%s\n' 'invalid news shadow setting' >&2
  exit 65
fi

python_bin="$repo_root/.venv/bin/python"
if [[ ! -x "$python_bin" || ! -r "$config_path" ]]; then
  /usr/bin/printf '%s\n' 'news shadow release or config is unavailable' >&2
  exit 67
fi

ca_bundle=/etc/ssl/cert.pem
if [[ ! -r "$ca_bundle" || ! -s "$ca_bundle" ]]; then
  /usr/bin/printf '%s\n' 'macOS system CA bundle is unavailable' >&2
  exit 71
fi

# Validate the exact installed release and account-free news configuration
# before reading any credential from Keychain.
/usr/bin/env -i \
  HOME="${HOME:?missing HOME}" \
  LANG="en_US.UTF-8" \
  SSL_CERT_FILE="$ca_bundle" \
  "$python_bin" -I -c '
import resource
import sys
from pathlib import Path
import turtle_news
from turtle_news.worker import load_config
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
assert resource.getrlimit(resource.RLIMIT_CORE) == (0, 0)
root = Path(sys.argv[1]).resolve()
if root not in Path(turtle_news.__file__).resolve().parents:
    raise SystemExit("news worker import does not match this release")
if (load_config(sys.argv[2]).llm.api_key_env or "") != sys.argv[3]:
    raise SystemExit("news LLM environment does not match config")
' "$repo_root" "$config_path" "$llm_env_name" || exit $?

finnhub_key="$(/usr/bin/security find-generic-password \
  -w -s TossTradingBot.FinnhubApiKey -a news-finnhub \
  2>/dev/null)" || {
    /usr/bin/printf '%s\n' 'news provider key is unavailable from Keychain' >&2
    exit 69
  }
discord_webhook="$(/usr/bin/security find-generic-password \
  -w -s TossTradingBot.DiscordNewsWebhook -a discord-news-webhook \
  2>/dev/null)" || {
    finnhub_key=
    /usr/bin/printf '%s\n' 'news webhook is unavailable from Keychain' >&2
    exit 69
  }
llm_key=
if [[ -n "$llm_env_name" ]]; then
  llm_key="$(/usr/bin/security find-generic-password \
    -w -s TossTradingBot.NewsLlmApiKey -a news-llm \
    2>/dev/null)" || {
      finnhub_key=
      discord_webhook=
      /usr/bin/printf '%s\n' 'news LLM key is unavailable from Keychain' >&2
      exit 69
    }
fi
if [[ -z "$finnhub_key" || -z "$discord_webhook" || \
      "$finnhub_key" == *[[:space:]]* || "$discord_webhook" == *[[:space:]]* || \
      ( -n "$llm_env_name" && -z "$llm_key" ) ]]; then
  finnhub_key=
  discord_webhook=
  llm_key=
  /usr/bin/printf '%s\n' 'news credentials failed local validation' >&2
  exit 69
fi

llm_secret_env=()
if [[ -n "$llm_env_name" ]]; then
  llm_secret_env+=("$llm_env_name=$llm_key")
fi
umask 077
ulimit -c 0
cd "$repo_root"
exec /usr/bin/env -i \
  HOME="${HOME:?missing HOME}" \
  LANG="en_US.UTF-8" \
  SSL_CERT_FILE="$ca_bundle" \
  FINNHUB_API_KEY="$finnhub_key" \
  DISCORD_NEWS_WEBHOOK_URL="$discord_webhook" \
  DISCORD_ALLOWED_CHANNEL_ID="$channel_id" \
  "${llm_secret_env[@]}" \
  "$python_bin" -I -u -c '
import json, os, resource, sys
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
assert resource.getrlimit(resource.RLIMIT_CORE) == (0, 0)
from turtle_news.worker import NewsDigestError, load_config, run_once
from turtle_runtime.heartbeat import RedactedHeartbeatWriter
llm_env_name = sys.argv[2] or None
llm_key = os.environ.pop(llm_env_name, "") if llm_env_name else ""
writer = RedactedHeartbeatWriter(
    sys.argv[3], release_sha=sys.argv[4], component="news"
)
writer.write("STARTING")
config = load_config(sys.argv[1])
if config.llm.api_key_env != llm_env_name:
    writer.write("ERROR")
    print(json.dumps({"ok": False, "code": "llm_environment_mismatch"}, sort_keys=True))
    raise SystemExit(2)
names = {
    config.finnhub_api_key_env: os.environ.pop("FINNHUB_API_KEY", ""),
    config.discord_webhook_env: os.environ.pop("DISCORD_NEWS_WEBHOOK_URL", ""),
    config.discord_channel_env: os.environ.pop("DISCORD_ALLOWED_CHANNEL_ID", ""),
}
if config.llm.api_key_env:
    names[config.llm.api_key_env] = llm_key
env = names
try:
    result = run_once(config, env=env)
except NewsDigestError as exc:
    writer.write("ERROR")
    print(json.dumps({"ok": False, "code": exc.code}, sort_keys=True))
    raise SystemExit(2)
except Exception:
    writer.write("ERROR")
    print(json.dumps({"ok": False, "code": "internal_error"}, sort_keys=True))
    raise SystemExit(3)
payload = {
    "ok": not result.error_codes,
    "symbol": result.symbol,
    "fetched": result.fetched,
    "inserted": result.inserted,
    "sent": result.sent,
    "source_fallbacks": result.source_fallbacks,
    "error_codes": result.error_codes,
}
writer.write("OK" if not result.error_codes else "DEGRADED")
print(json.dumps(payload, sort_keys=True))
raise SystemExit(0 if not result.error_codes else 1)
' "$config_path" "$llm_env_name" "$heartbeat_path" "$release_sha"
