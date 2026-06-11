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
12. Enter runtime loop.

The bot must never submit orders immediately after process start before
reconciliation.

## Runtime Windows

Times must be based on market calendar APIs, not hard-coded clocks. Static
times may be used only as fallback in paper/backtest mode.

Loop profiles:

- Premarket: fetch candles, prepare channels, verify account.
- Market open: price/orderbook polling, order guard, state sync.
- Postmarket: final order reconciliation, report, candle cache refresh.
- Closed: slow health loop only.

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
