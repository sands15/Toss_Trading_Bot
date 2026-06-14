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
- The checked-in Toss client has no order creation, modification, or cancel
  method.
- The setup template starts in `runtime.mode: shadow`, not live mode.
