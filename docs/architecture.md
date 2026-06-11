# System Architecture

The bot has one strategy core and multiple execution modes. Strategy code must
not know Toss endpoint paths. Broker and market data integrations live behind
interfaces.

## Target Operating Model

```text
macOS launchd
  -> turtle-bot process
      -> Runtime
          -> Scheduler
          -> StateStore
          -> MarketDataClient
          -> IndicatorEngine
          -> TurtleSignalEngine
          -> RiskManager
          -> OrderGuard
          -> OrderManager
          -> BrokerAdapter
          -> PositionSync
          -> Logger
          -> Notifier
```

Windows must be able to run the same Python package for tests, backtests, and
paper mode. Only macOS service files are platform-specific.

## Dependency Direction

Allowed:

```text
runtime -> strategy -> domain
runtime -> broker -> domain
runtime -> storage -> domain
runtime -> config
```

Forbidden:

```text
strategy -> toss_api
strategy -> sqlite
strategy -> launchd
domain -> requests/httpx
domain -> operating system APIs
```

## Execution Modes

### Backtest

Runs from historical candles only.

- No Toss credentials.
- No network required.
- Uses simulated broker.
- Produces trades, equity curve, and rule audit logs.

### Read-only Live

Reads Toss account and market data without creating orders.

- Auth required.
- Accounts, holdings, open orders, candles, prices.
- Verifies schema and rate-limit behavior.
- Stores broker snapshots.

### Paper

Uses live Toss market/account data but does not submit orders.

- Runs the full Turtle loop.
- Builds order intents.
- Applies OrderGuard.
- Logs what would have been submitted.

### Live

Submits real orders.

- Must start disabled.
- Requires explicit config flag.
- Requires clean position reconciliation.
- Requires successful paper-mode rehearsal.

## Core Domain Objects

### Candle

```text
timestamp: aware datetime
symbol: str
open: Decimal
high: Decimal
low: Decimal
close: Decimal
volume: Decimal
currency: str
adjusted: bool
source: str
```

Use `Decimal` for all money and quantity calculations. Do not use float for
order prices, N, stops, or quantities.

### IndicatorSnapshot

```text
symbol: str
as_of: datetime
n: Decimal
n_method: str
entry_high_20: Decimal
entry_low_20: Decimal
entry_high_55: Decimal
entry_low_55: Decimal
exit_low_10: Decimal
exit_low_20: Decimal
ready: bool
```

### Signal

```text
signal_id: str
symbol: str
system: S1 | S2
kind: ENTRY | EXIT | STOP | PYRAMID
side: BUY | SELL
trigger_price: Decimal
observed_price: Decimal
triggered_at: datetime
reason: str
```

### PositionState

```text
symbol: str
side: LONG
system: S1 | S2
status: OPEN | CLOSING | CLOSED | BLOCKED
total_qty: Decimal
avg_entry_price: Decimal
entry_n: Decimal
current_stop_price: Decimal
last_unit_entry_price: Decimal
units: list[Unit]
```

### Unit

```text
unit_no: int
qty: Decimal
entry_price: Decimal
n_at_entry: Decimal
stop_price: Decimal
broker_order_id: str | None
client_order_id: str
```

### OrderIntent

```text
intent_id: str
symbol: str
side: BUY | SELL
quantity: Decimal
order_type: LIMIT | MARKET
limit_price: Decimal | None
time_in_force: DAY
source_signal_id: str
client_order_id: str
risk_snapshot_id: str
```

## Runtime Loop

High-level live/paper loop:

```text
load config
init state store
init auth and clients
resolve account

while process is running:
  refresh market session state

  if premarket preparation window:
    fetch completed candles
    calculate channels and N
    persist indicators

  if active trading window:
    sync positions and open orders
    block if critical mismatch
    fetch current prices/orderbook
    evaluate exits, pyramids, entries
    convert approved signals to order intents
    run OrderGuard
    submit or record order depending on mode
    persist every decision

  if postmarket window:
    sync orders and holdings
    fetch completed candles when available
    write daily report

  sleep according to mode and rate-limit budget
```

## State Store

SQLite is the default local store. Use WAL mode for resilience.

Tables:

- `candles`
- `indicator_snapshots`
- `positions`
- `position_units`
- `signals`
- `order_intents`
- `broker_orders`
- `broker_order_events`
- `broker_account_snapshots`
- `strategy_trade_history`
- `api_tokens`
- `api_errors`
- `runtime_events`

Sensitive token values must not be stored. Store expiry metadata and masked
hashes only.

## Idempotency

Toss `clientOrderId` is required for live orders.

Rules:

- Maximum 36 chars.
- Allowed chars: letters, numbers, hyphen, underscore.
- Deterministic for a single signal/order intent.
- Also protected by local DB uniqueness because broker-side idempotency has a
  finite window.

Format:

```text
TT-{YYYYMMDD}-{SYMBOL}-{SIDE}-{SYSTEM}-{UNIT}
```

If this exceeds 36 chars, use a short hash suffix.

## Error Handling

HTTP handling policy:

- 400: no retry; request bug or invalid price/tick.
- 401: refresh token once, then retry once.
- 403: stop live trading and alert.
- 404: re-query resource, then block related symbol.
- 409: reconcile by client order id; do not create another order.
- 422: no retry; broker business rule rejection.
- 429: obey `Retry-After`; reduce loop speed.
- 500 class: retry with small cap, then reconcile unknown order state.

Unknown order result policy:

1. Do not send a replacement order.
2. Query by open orders/list/detail where possible.
3. Mark local order as `UNKNOWN`.
4. Block new orders for the symbol until reconciled.

## Observability

Every loop decision must be reconstructable:

- Input prices and timestamps.
- Indicator snapshot id.
- Position snapshot before decision.
- Rule branch selected.
- Guard checks and result.
- Broker request id when available.
- Broker response or error.

Logs should be human-readable, but DB events are the audit source.

## AI Boundary

AI may summarize reports, detect anomalies, and help debug logs. AI must not
decide entries, exits, sizing, or whether to override OrderGuard.
