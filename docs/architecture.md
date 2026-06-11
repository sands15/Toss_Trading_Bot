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
          -> UniverseBuilder
          -> WatchlistBuilder
          -> MarketDataClient
          -> MarketDataCache
          -> RateLimitQueue
          -> IndicatorEngine
          -> TurtleSignalEngine
          -> RiskManager
          -> OrderGuard
          -> OrderManager
          -> BrokerAdapter
          -> PositionSync
          -> Logger
          -> Notifier
          -> HealthServer
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
runtime -> notifier
runtime -> health
```

Forbidden:

```text
strategy -> toss_api
strategy -> sqlite
strategy -> launchd
domain -> requests/httpx
domain -> operating system APIs
strategy -> notifier
strategy -> health server
```

## Execution Modes

### Backtest

Runs from historical candles only.

- No Toss credentials.
- No network required.
- Uses simulated broker.
- Produces trades, equity curve, and rule audit logs.
- Initial implementation is daily-bar with single-symbol and multi-symbol
  portfolio loops. Intraday simulation should be added only after daily-bar
  Turtle behavior is accepted.
- Daily OHLC ambiguity is handled conservatively: exits are checked before
  pyramids or entries, and gap fills use the open instead of assuming a better
  trigger fill.

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
- Current implementation records paper-only order intents as runtime events and
  notifications after reconciliation passes.
- Paper-only guard results explain why a would-be order is allowed or blocked.
- Paper fill/state simulation updates only paper state tables. It does not
  touch live/reconciliation position tables and does not represent real broker
  execution quality.
- The macOS paper service shell records startup and heartbeat runtime events,
  exposes a read-only health payload, and stays blocked until `runtime.symbols`,
  Toss env credentials, and `toss.account_seq` are configured.
- When configured, paper service uses the read-only Toss market-data provider
  for candles/prices, runs broker reconciliation through read-only holdings and
  open-order endpoints, and only then evaluates Turtle paper intents.
- The read-only market-calendar gate runs before paper intent evaluation. A
  closed or unknown session records a blocker and prevents market/account data
  polling beyond the calendar check for that iteration.

### Live

Submits real orders.

- Must start disabled.
- Requires explicit config flag.
- Requires clean position reconciliation.
- Requires successful paper-mode rehearsal.

## Operational Components

These components are inspired by a reviewed KIS-based automation project, but
adapted to this repository's stricter Turtle and broker-safety rules.

### WatchlistBuilder

The watchlist is prepared before the market opens. It is not a strategy rule;
it is an operational filter that decides which symbols deserve active polling.

Responsibilities:

- Load the configured universe.
- Fetch completed daily candles.
- Calculate distance to previous 20-day and 55-day breakout levels.
- Rank symbols nearest to Turtle breakout levels.
- Mark new candidates compared with the previous watchlist.
- Persist the watchlist and send a premarket notification.
- Current paper service integration builds and persists the watchlist during
  known OPEN/PREOPEN sessions before paper intent evaluation. PREOPEN builds the
  watchlist but still blocks paper order-intent evaluation.

Constraints:

- Watchlist ranking must not create trades by itself.
- A symbol outside the watchlist may still be evaluated in backtests.
- Live mode must log the universe and watchlist used for the session.

### UniverseBuilder

The universe builder automatically selects which stocks are eligible for
watchlist screening. It must be deterministic and rule-based, not AI-selected.

Responsibilities:

- Load exchange-listed stocks from read-only Toss market-info endpoints.
- Exclude instruments by configured market, asset type, warning status,
  suspension/delisting/management flags, and optional ETF policy.
- Apply liquidity filters such as minimum average daily traded value, minimum
  price, and minimum candle history.
- Keep only symbols with enough completed candles to calculate Turtle channels
  and N.
- Persist the universe snapshot used for a session so reports can explain why a
  symbol was included or excluded.

Constraints:

- Universe selection is not a buy recommendation.
- AI must not add symbols to the tradable universe by opinion or narrative.
- A symbol excluded for missing data, stale data, warning status, or broker
  mismatch must remain blocked until the rule-based blocker clears.

### MarketDataCache

The runtime should avoid repeated REST calls for the same price, orderbook, and
candle data.

Responsibilities:

- Store current prices with timestamps.
- Store orderbook snapshots with timestamps.
- Store completed candle pages.
- Expose freshness checks.
- Block signal evaluation when required data is stale.

The cache is an optimization and safety layer, not the source of truth for
account positions. Broker holdings and open orders still come from Toss.

`TossReadOnlyMarketDataProvider` adapts `TossClient.get_candles` and
`TossClient.get_prices` into the paper runtime's market-data interface. It
stores snapshots, caches candles/prices, and excludes the current local session
candle by default so Turtle channels are based on completed candles.

### MarketCalendarGate

The calendar gate adapts Toss `GET /api/v1/market-calendar/{market}` into a
small session state. It treats explicit open states as runnable, explicit
closed/holiday/weekend states as blocked, and unknown payloads as blocked. This
keeps the paper service from making Turtle decisions outside a known market
session. PREOPEN is also blocked for paper order-intent evaluation, but it is
allowed to run watchlist generation.

### RateLimitQueue

Toss rate-limit headers must drive request pacing.

Responsibilities:

- Serialize requests by API group where needed.
- Track `X-RateLimit-Remaining`, `X-RateLimit-Reset`, and `Retry-After`.
- Slow polling before 429 responses become frequent.
- Block non-urgent requests when order/account safety checks need quota.

Order and account reconciliation requests have higher priority than watchlist
screening.

### Notifier

The notifier reports system state and decisions, but it must not decide trades.

Minimum notifications:

- Startup and mode.
- Premarket watchlist.
- Account reconciliation mismatch.
- Paper order candidate.
- Live order submitted, rejected, filled, canceled, or unknown.
- Rate-limit pause.
- End-of-day summary.

### HealthServer

A small local health interface may be added for local monitoring.

Allowed endpoints:

- `GET /health`
- `GET /status`
- `GET /positions`
- `GET /orders/open`
- `GET /watchlist`

Unsafe actions such as start/stop trading, close-all, config mutation, and live
enablement must not be exposed until authentication and operator confirmation
are designed.

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
    build and persist watchlist
    send premarket watchlist notification
    persist indicators

  if active trading window:
    sync positions and open orders
    block if critical mismatch
    fetch or refresh current prices/orderbook through MarketDataCache
    check data freshness
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
- `paper_positions`
- `paper_position_units`
- `signals`
- `order_intents`
- `broker_orders`
- `broker_order_events`
- `broker_account_snapshots`
- `broker_snapshots`
- `strategy_trade_history`
- `api_tokens`
- `api_errors`
- `runtime_events`
- `watchlists`
- `watchlist_items`
- `market_data_snapshots`

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

Request priority under limited quota:

1. Unknown order reconciliation.
2. Open order and holdings sync.
3. Buying power and sellable quantity checks.
4. Current price/orderbook for active positions.
5. Current price/orderbook for watchlist entries.
6. Premarket broad-universe screening.

Unknown order result policy:

1. Do not send a replacement order.
2. Query by open orders/list/detail where possible.
3. Mark local order as `UNKNOWN`.
4. Block new orders for the symbol until reconciled.

## Position Reconciliation

Position reconciliation is a blocker, not a trade decision engine.

Inputs:

- Local `PositionState` rows with `OPEN` status.
- Broker holdings from read-only account APIs.
- Broker open orders from read-only order-list APIs.

Blocking conditions:

- A local open position is missing from broker holdings.
- Broker has a positive holding that local Turtle state does not know about.
- Local quantity and broker quantity differ beyond configured tolerance.
- Any unresolved broker order exists for the symbol.
- Any unknown future broker order status appears.

When reconciliation is not clean, paper/live order candidates for affected
symbols must not be created.

## Observability

Every loop decision must be reconstructable:

- Input prices and timestamps.
- Indicator snapshot id.
- Position snapshot before decision.
- Rule branch selected.
- Guard checks and result.
- Broker request id when available.
- Broker response or error.
- Data freshness age.
- Rate-limit budget at decision time.

Logs should be human-readable, but DB events are the audit source.

## Reference Project Lessons

The reviewed KIS/Node.js automation project confirms that a practical trading
daemon benefits from a central manager, market scheduler, data cache, notifier,
watchlist generation, and health surface. This project adopts those operational
ideas while rejecting direct reuse of its Turtle strategy logic because it does
not preserve the full rules required here: System 1 skip, System 2 failsafe,
0.5N pyramiding, strict N handling, and broker/local reconciliation before
orders.

## AI Boundary

AI is an explanation and summarization layer only.

Allowed:

- Summarize market/news context for symbols already selected by rule-based
  universe/watchlist logic.
- Summarize daily reports, paper/live blockers, runtime events, and anomalous
  logs in human-readable Korean.
- Explain why a symbol appeared in the universe or watchlist based on recorded
  rule outputs.
- Draft operator-facing messages for review.

Disallowed:

- Selecting tradable symbols by opinion, news sentiment, or discretionary
  preference.
- Deciding entries, exits, pyramids, stops, position sizing, or skips.
- Overriding Turtle signals, `OrderGuard`, reconciliation blockers, market
  calendar blockers, or warning-status filters.
- Changing universe, watchlist, risk, or live-mode config without explicit
  operator approval.
