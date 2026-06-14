# macOS Operations

The production target is a macOS machine running continuously.

## Runtime Assumptions

- Amphetamine or equivalent prevents system sleep.
- The machine remains connected to power.
- Network is stable enough for broker polling.
- The process is managed by `launchd`.
- Secrets are not committed to git.

Amphetamine prevents sleep. `launchd` restarts the bot after crash, logout, or
reboot. Use both.

## Python Environment

Recommended:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[dev]"
```

## Dashboard Over Tailscale

### Single Local Operator

For a single Mac desktop/session launcher, run:

```bash
chmod +x ops/run-dashboard-macos.command
open ops/run-dashboard-macos.command
```

The launcher prepares `.venv`, creates `config/local.yaml` from the example if
needed, detects the Mac's Tailscale IPv4 address, and binds the dashboard to
that Tailscale address on port `8765`. This keeps the dashboard off the normal
LAN interface by default.

It prints both local and Tailscale URLs. From another Tailscale device, open:

```text
http://<mac-tailscale-ip>:8765/
```

To use a different port:

```bash
DASHBOARD_PORT=8766 ops/run-dashboard-macos.command
```

If macOS asks whether Python may accept incoming connections, allow it. If the
Tailscale URL does not open, confirm Tailscale is logged in on the Mac with:

```bash
tailscale status
tailscale ip -4
```

To force local-only access:

```bash
DASHBOARD_HOST=127.0.0.1 ops/run-dashboard-macos.command
```

### Per-User Docker Containers

If more than one person will use the dashboard, prefer the multi-user gateway.
It identifies users by their Tailscale client IP. A first-time IP sees a setup
page for name, Toss app ID, Toss app secret, and account sequence. After setup,
that IP is automatically routed to its own Docker container.

Start the gateway:

```bash
chmod +x ops/run-multi-user-gateway.command
open ops/run-multi-user-gateway.command
```

Open the gateway URL it prints:

```text
http://<mac-tailscale-ip>:8765/
```

The gateway stores its routing registry at:

```text
.local/users/registry.json
```

Each user gets separate local files:

```text
.local/users/<user>/config/local.yaml
.local/users/<user>/state/turtle.sqlite3
.local/users/<user>/logs/
.local/users/<user>/.env
```

User containers are bound to `127.0.0.1:<internal-port>` on the Mac. Only the
gateway is exposed on the Tailscale address.

For manual user container management without the gateway:

```bash
chmod +x ops/run-user-dashboard-container.command
ops/run-user-dashboard-container.command alice
```

Use a different port for each user:

```bash
DASHBOARD_PORT=8766 ops/run-user-dashboard-container.command bob
DASHBOARD_PORT=8767 ops/run-user-dashboard-container.command charlie
```

Store that user's Toss credentials in:

```text
.local/users/<user>/.env
```

Manage a user container:

```bash
docker logs -f toss-dashboard-alice
docker stop toss-dashboard-alice
docker start toss-dashboard-alice
```

This is still a local Tailnet deployment pattern, not a public SaaS model. A
public multi-tenant service still needs authentication, account ownership,
encrypted secret storage, admin controls, and audit logs before real users are
invited.

Windows development should use the same package:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e ".[dev]"
```

## Configuration

Use a local config file and environment variables.

Never commit:

- `TOSS_CLIENT_ID`
- `TOSS_CLIENT_SECRET`
- account identifiers if the user considers them private
- live trading enable flags

Suggested local files:

```text
.env
config/local.yaml
state/turtle.sqlite3
logs/
```

Minimum paper-mode config once Toss read-only credentials are available:

```yaml
toss:
  live_enabled: false
  account_seq: "7"
  client_id_env: TOSS_CLIENT_ID
  client_secret_env: TOSS_CLIENT_SECRET

runtime:
  mode: paper
  market: KR
  timezone: Asia/Seoul
  use_market_calendar: true
  watchlist_enabled: true
  watchlist_top_n: 20
  watchlist_name: premarket
  symbols:
    - "005930"
  state_db: state/turtle.sqlite3
  log_dir: logs

ai:
  enabled: false
  provider: openai_compatible
  model: bRadu/gemma-4-E2B-it-textonly
  base_url: http://localhost:8000/v1
  api_key_env: TURTLE_AI_API_KEY
```

`runtime.symbols` is the manual starting point. When
`runtime.universe_enabled=true`, `UniverseBuilder` selects an eligible universe
from `runtime.universe_candidate_symbols` through deterministic read-only
filters: market, instrument type, warning status, liquidity, price, and enough
completed candle history for Turtle rules.

## launchd Service

Template path in repo:

```text
ops/launchd/com.sands15.toss-turtle-bot.plist
```

Install location:

```text
~/Library/LaunchAgents/com.sands15.toss-turtle-bot.plist
```

Expected behavior:

- Starts at login.
- Restarts on crash.
- Writes stdout/stderr to repo or user log directory.
- Runs paper mode by default.
- Live mode requires explicit config.

The checked-in template contains placeholder paths. Generate a local plist with
absolute paths before installing it:

```bash
python -m turtle_bot \
  --config config/local.yaml \
  --state-db state/turtle.sqlite3 \
  --log-dir logs \
  --ensure-runtime-dirs

python -m turtle_bot \
  --config config/local.yaml \
  --repo-dir "$PWD" \
  --python-executable "$PWD/.venv/bin/python" \
  --state-db "$PWD/state/turtle.sqlite3" \
  --log-dir "$PWD/logs" \
  --write-launchd-plist "$HOME/Library/LaunchAgents/com.sands15.toss-turtle-bot.plist"
```

Before bootstrapping, run the paper-mode operations check:

```bash
python -m turtle_bot \
  --config config/local.yaml \
  --state-db state/turtle.sqlite3 \
  --log-dir logs \
  --ops-check
```

The paper service can also be smoke-tested without entering the infinite
launchd loop:

```bash
python -m turtle_bot \
  --config config/local.yaml \
  --state-db state/turtle.sqlite3 \
  --log-dir logs \
  --paper-service \
  --once
```

Postmarket daily report export:

```bash
python -m turtle_bot \
  --state-db state/turtle.sqlite3 \
  --daily-report reports/daily-$(date +%F).json \
  --report-date "$(date +%F)" \
  --report-timezone Asia/Seoul
```

The report is read-only over SQLite state. It summarizes runtime events,
blockers, watchlist rows, paper positions, and latest broker snapshots. AI may
summarize this report for the operator, but the report itself remains the
auditable source of facts.

AI daily report summary through an OpenAI-compatible API:

```bash
python -m turtle_bot \
  --config config/local.yaml \
  --state-db state/turtle.sqlite3 \
  --daily-report reports/daily-$(date +%F).json \
  --report-date "$(date +%F)" \
  --report-timezone Asia/Seoul \
  --daily-report-ai-summary
```

The configured server must expose `/v1/chat/completions`. The default model
string is `bRadu/gemma-4-E2B-it-textonly`, but the bot treats the model as an
API implementation detail. On Apple Silicon, the preferred future local path is
an MLX int4 model served behind an OpenAI-compatible API. NVIDIA/vLLM,
Transformers, or llama.cpp servers are acceptable test or alternate backends as
long as they preserve the same API contract. AI summary output is
operator-facing prose only; it must not feed back into universe selection,
watchlist ranking, Turtle signals, sizing, or guards.

Example commands:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.sands15.toss-turtle-bot.plist
launchctl kickstart -k gui/$(id -u)/com.sands15.toss-turtle-bot
launchctl print gui/$(id -u)/com.sands15.toss-turtle-bot
launchctl bootout gui/$(id -u)/com.sands15.toss-turtle-bot
```

## Power Settings

Amphetamine should be configured to prevent system sleep indefinitely while the
bot is expected to run.

For fixed machines such as Mac mini, consider also checking:

```bash
pmset -g
```

Do not rely on display sleep status. Display sleep is fine; system sleep is not.

## Startup Safety Sequence

On every process start:

1. Load config.
2. Open DB and acquire process lock.
3. Resolve mode.
4. Authenticate if mode needs Toss.
5. Resolve account sequence.
6. Fetch holdings.
7. Fetch open orders.
8. Reconcile local positions.
9. Block live orders if mismatch exists.
10. Fetch latest completed candles.
11. Calculate indicator snapshots.
12. Build or load the premarket watchlist.
13. Start health/status surface if enabled.
14. Enter runtime loop.

The bot must never submit orders immediately after process start before
reconciliation.

The current paper-mode service is intentionally conservative. Without
`runtime.symbols`, Toss env credentials, and `toss.account_seq`, it records a
blocked health payload and no trade decision is evaluated. Once those read-only
inputs exist, it checks the Toss read-only market calendar first. If the session
is closed or unknown, it records a blocker and stops that iteration. If the
session is PREOPEN, it builds and persists the premarket watchlist but still
blocks paper order-intent evaluation. If the session is open, it builds the
watchlist, reconciles holdings/open orders through read-only endpoints, fetches
prices, and then runs the paper Turtle loop. It does not submit, cancel, or
modify broker orders.

## Runtime Windows

Times must be based on market calendar APIs, not hard-coded clocks. Static
times may be used only as fallback in paper/backtest mode. The current paper
service uses the Toss market-calendar endpoint as a gate before evaluating
paper intents.

Loop profiles:

- Premarket: fetch candles, prepare channels, verify account.
- Premarket watchlist: rank symbols near 20-day and 55-day breakout levels,
  persist the session watchlist, and notify.
- Automatic universe selection: rule-based screening before watchlist
  generation. AI may summarize the screening result but must not select symbols.
- Market open: price/orderbook polling through cache, order guard, state sync.
- Postmarket: final order reconciliation, report, candle cache refresh.
- Closed: slow health loop only.

## Daily Operating Rhythm

Suggested KST rhythm for KR market operation:

- 07:00: token/account readiness check.
- 07:30: completed candle refresh and Turtle watchlist generation.
- 08:00: system-active notification with current blockers.
- 09:00: market-open reconciliation and paper/live loop activation.
- 15:30: market-close handling and order reconciliation.
- 16:00: final daily report.

These are operational checkpoints, not trading rules. The scheduler should use
Toss market-calendar APIs for actual market sessions.

## Failure Policies

### Network Failure

- Do not submit orders.
- Keep process alive.
- Retry with capped backoff.
- Alert if failure persists beyond threshold.

### API Rate Limit

- Obey `Retry-After`.
- Reduce polling frequency.
- Do not compensate by bursting later.
- Preserve quota for account reconciliation and order-state checks before
  broad watchlist or universe refreshes.

### Process Crash

`launchd` restarts the process. On restart, startup safety sequence runs before
any order can be considered.

### Unknown Broker State

If order state is unknown:

- Mark symbol blocked.
- Query open orders and order detail.
- Do not create a replacement order.
- Require clean reconciliation before unblocking.

## Windows Compatibility

Windows is a development and test platform, not the primary daemon runtime.

Must work on Windows:

- Unit tests.
- Backtests.
- Read-only client tests with mocked HTTP.
- Paper loop with mocked or real read-only API if credentials exist.

May be macOS-only:

- `launchd` files.
- Keychain integration.
- Amphetamine assumptions.

Use `pathlib.Path`, not hard-coded `/` or `\`.

## Health Surface

The local health/status service must be read-only until authentication and
operator confirmation are designed.

Allowed:

- Current mode.
- Market session state.
- Current blockers.
- Latest watchlist.
- Cached data freshness.
- Open positions and unresolved orders.

Disallowed for now:

- Enabling live mode.
- Starting or stopping trading.
- Closing all positions.
- Editing credentials or account settings.
