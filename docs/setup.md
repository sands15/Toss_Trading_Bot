# Setup

This guide is for a new user setting up the bot from a fresh checkout.

## 1. Bootstrap

Windows PowerShell:

```powershell
.\ops\setup-local.ps1
```

macOS or Linux:

```bash
bash ops/setup-local.sh
```

The script creates:

- `.venv/`
- `config/local.yaml` from `config/local.example.yaml`
- `state/`
- `logs/`

It does not write Toss API secrets to disk.

## 2. Configure Local Values

Edit `config/local.yaml`:

```yaml
toss:
  live_enabled: false
  account_seq: "YOUR_ACCOUNT_SEQ"
  client_id_env: TOSS_CLIENT_ID
  client_secret_env: TOSS_CLIENT_SECRET
```

Keep `live_enabled: false` during setup, paper runs, and shadow validation.
Set it to `true` only together with `runtime.mode: live` for a controlled
pilot.

## 3. Set Secrets

Windows PowerShell for the current terminal:

```powershell
$env:TOSS_CLIENT_ID = "your-client-id"
$env:TOSS_CLIENT_SECRET = "your-client-secret"
```

macOS or Linux for the current terminal:

```bash
export TOSS_CLIENT_ID="your-client-id"
export TOSS_CLIENT_SECRET="your-client-secret"
```

Use your OS secret manager or shell profile for persistent storage. Do not
commit secrets or `config/local.yaml`.

## 4. Check Readiness

Windows:

```powershell
.\.venv\Scripts\python.exe -m turtle_bot `
  --config config\local.yaml `
  --state-db state\turtle.sqlite3 `
  --log-dir logs `
  --ops-check
```

macOS or Linux:

```bash
.venv/bin/python -m turtle_bot \
  --config config/local.yaml \
  --state-db state/turtle.sqlite3 \
  --log-dir logs \
  --ops-check
```

Fix any blocker before continuing.

## 5. Run One Shadow Validation

Shadow mode uses real read-only Toss account and market data, records strategy
intents and virtual fills, and does not submit live orders.

Windows:

```powershell
.\.venv\Scripts\python.exe -m turtle_bot `
  --config config\local.yaml `
  --state-db state\turtle.sqlite3 `
  --log-dir logs `
  --shadow-service `
  --once
```

macOS or Linux:

```bash
.venv/bin/python -m turtle_bot \
  --config config/local.yaml \
  --state-db state/turtle.sqlite3 \
  --log-dir logs \
  --shadow-service \
  --once
```

Expected result:

- `mode` is `shadow`
- `ready` is true when market/account checks pass
- runtime events may include `shadow_order_intent`, `shadow_fill`, and
  `shadow_reconcile_warning`

## Safety Defaults

- `config/local.yaml` is ignored by git.
- `state/`, `logs/`, `data/`, and backtest reports are ignored by git.
- Live order creation, modification, and cancel are isolated behind the
  guarded `TossLiveBrokerAdapter` path.
- The setup template starts in `runtime.mode: shadow`, not live mode.

## 6. Controlled Live Pilot

Dashboard path for the operator:

1. Enter the Toss API client id/secret.
2. Confirm the Toss account sequence.
3. Press `안전 파일럿 시작`.

The dashboard action applies the safe pilot config automatically:

- `runtime.mode: live`
- `toss.live_enabled: true`
- `live.emergency_stop: false`
- first available symbol allowlisted
- one-share max quantity
- one order per local day
- small daily notional cap
- `cancel_after_ack: true`

Use `거래 중지` on the dashboard to persist `live.emergency_stop: true` and stop the
dashboard-managed trading loop.

Only after a clean shadow run, change both values together:

```yaml
toss:
  live_enabled: true

runtime:
  mode: live
```

Start with one allowlisted symbol and a very small order size. The live runtime
uses the same strategy signal path, but switches off virtual fills and submits
approved order intents through the live execution ledger.

Keep the independent live safety block tight:

```yaml
live:
  emergency_stop: false
  allowed_symbols:
    - "YOUR_PILOT_SYMBOL"
  max_order_quantity: 1
  max_order_notional: 10000
  daily_order_count_limit: 1
  daily_notional_limit: 10000
  require_market_open: true
  require_clean_reconcile: true
  block_unresolved_orders: true
  cancel_after_ack: true
```

`strategy.momentum.target_position_pct` still controls the desired strategy
size. The `live:` block is a final hard cap before the Toss order API call.
