# Reference Project Notes

Reviewed project:

```text
https://github.com/hypark5540/turtle-trading-automataion
```

Review date: 2026-06-11.

## What It Is

The reference project is a Node.js automated trading system for Korea
Investment Securities OpenAPI, not Toss Securities Open API. It includes:

- Express server and simple web UI.
- Central `TradingManager`.
- KIS REST and WebSocket clients.
- Market scheduler.
- Market data cache.
- Multiple strategy classes.
- Dynamic stock universe and screening.
- Slack notifications.
- Daily PnL tracking.
- PM2/NSSM-style 24-hour operation guidance.

## Useful Ideas to Adopt

### Central Runtime Manager

A central manager coordinates API clients, market data cache, scheduler,
notifier, and strategy workers. This is useful and should be reflected in our
runtime layer.

Our adaptation:

- Keep the manager outside the strategy core.
- Run reconciliation before any trading loop.
- Keep live mode disabled unless explicitly enabled.

### Market Data Cache

The reference project caches prices, orderbooks, and candles to avoid excessive
REST calls. We should adopt this as `MarketDataCache`.

Our adaptation:

- Cache entries must have freshness timestamps.
- Stale data blocks paper/live order candidates.
- Broker account state must never come only from cache.

### Watchlist Builder

The reference project builds a Turtle watchlist from symbols near 20-day and
55-day highs. This is operationally valuable.

Our adaptation:

- Watchlist is a polling filter, not a trade signal.
- It must use current-bar exclusion.
- It should persist candidates and mark new symbols.
- It must not override System 1 skip or System 2 rules.

### Rate-Limit Queue

The reference project uses a simple request queue to respect API limits.

Our adaptation:

- Track Toss rate-limit headers.
- Prioritize order/account reconciliation over screening.
- On 429, obey `Retry-After`.

### Notifications

Premarket watchlist, startup, order events, errors, and daily PnL reports are
useful.

Our adaptation:

- Notifier is an output-only dependency.
- Strategy cannot call the notifier directly.
- Notifier cannot approve or override trades.

### Health Surface

The reference Express server is useful for local visibility.

Our adaptation:

- Start with read-only endpoints only.
- No live enablement, close-all, or config mutation until authentication and
  confirmation rules exist.

## What Not to Copy

### Turtle Strategy Logic

Do not copy the reference Turtle strategy as the source of truth.

Reasons:

- System 1 and System 2 separation is incomplete.
- System 1 skip rule is missing.
- System 2 failsafe behavior is not enforced.
- Pyramiding at 0.5N is not implemented as required here.
- N handling is ATR-oriented and not the original smoothing rule.
- Exits are not separated as S1 10-day and S2 20-day rules.

Our `docs/turtle-rules.md` remains the authority.

### Live Order Flow

The reference project submits orders directly from trader classes and then
updates local positions. That is too optimistic for this project.

Our order flow must be:

1. Generate signal.
2. Build order intent.
3. Run OrderGuard.
4. Submit through BrokerAdapter only in live mode.
5. Persist broker response.
6. Reconcile open orders and fills.
7. Update local position only from broker-confirmed state.

### In-Memory Position Truth

The reference project keeps active positions in memory maps. This is not enough
for a 24/7 daemon that may restart.

Our adaptation:

- SQLite is the durable local state.
- Toss holdings and open orders are the external truth.
- Any mismatch blocks new orders.

### JavaScript Number Math

The reference uses JavaScript numbers for prices and quantities. This project
uses Python `Decimal` for money, price, N, stop, and quantity calculations.

## Resulting Design Decision

Adopt the reference project's operational shape:

```text
manager + scheduler + cache + watchlist + notifier + health surface
```

Reject its strategy and order-state shortcuts:

```text
direct strategy-to-order calls
memory-only positions
partial Turtle rules
live mutation endpoints
```

The result should be a more conservative bot: less flashy, more auditable, and
closer to the Turtle rules.
