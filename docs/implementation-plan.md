# Implementation Plan

This plan is ordered to avoid accidental live trading before the strategy and
state model are trustworthy.

Every phase must pass the Turtle Principle Gate in
[Turtle Rules](turtle-rules.md). A phase is not complete if it makes a workflow
more convenient while weakening entry, exit, N, stop, skip, pyramiding, unit, or
reconciliation rules.

## Phase 0: Repository Skeleton

Create:

```text
pyproject.toml
src/turtle_bot/
tests/
config/example.yaml
ops/launchd/
```

Baseline commands:

```bash
python -m turtle_bot --help
pytest
```

Acceptance:

- Package installs on macOS and Windows.
- Tests run without Toss credentials.
- No live trading code path is enabled by default.
- No scaffold introduces a path around the Turtle Principle Gate.

## Phase 1: Domain and Strategy Core

Implement:

- Domain models.
- Decimal-safe candle normalization.
- N calculation.
- Donchian channels with current-bar exclusion.
- System 1 and System 2 signals.
- System 1 skip rule state.
- Stop, channel exit, and pyramid evaluation.

Acceptance tests:

- Current candle is excluded from channels.
- No signal before enough history.
- System 1 skip rule blocks only the next eligible S1 signal after a winner.
- System 2 is never skipped.
- Stop exits take priority over pyramids and entries.
- Pyramiding adds only after favorable 0.5N movement.
- Four-unit cap is enforced.
- Each generated signal includes enough context to explain the Turtle rule that
  produced it.

## Phase 2: Backtest Engine

Implement:

- CSV candle loader. Done for daily candles.
- Simulated broker. Done for single-symbol and multi-symbol daily-bar fills.
- Conservative same-bar ordering. Done: exits are checked before pyramids and
  entries.
- Fees/slippage/tax hooks. Done as configurable cost hooks.
- Trade and equity output. Done with trade records, equity curve, and audit
  events.

Acceptance:

- Deterministic results from fixture data. Covered by tests.
- Audit log explains every trade. Covered by tests for entry, stop, and gap
  reasons.
- Same-bar stop/pyramid conflict chooses stop. Covered by tests.
- Backtest assumptions are conservative when intraday order is unknowable.
  Covered for stop before pyramid and gap fills at open.
- Multi-symbol portfolio loop. Covered by tests.
- Configurable unit sizing from account equity and N. Covered by tests.
- Report export format for review. Done as JSON with Decimal values preserved
  as strings.

## Phase 3: Toss Read-only Client

Implement:

- Auth client. Done for `client_credentials` token issuance.
- Market data client. Done for candles, prices, orderbook, trades, and price
  limits.
- Market info client. Done for KR/US market calendar, exchange rate, stocks,
  and stock warnings.
- Account client. Done for accounts, buying power, sellable quantity, and
  commissions.
- Holdings/open orders client. Done for holdings, order list, and order detail.
- Rate-limit metadata capture. Done through `RateLimitQueue` header updates.
- RateLimitQueue with request priority classes.
- HTTP mock tests. Done with fake transport; no Toss credentials required.

Acceptance:

- Official OpenAPI field names are normalized correctly. Covered for endpoint
  paths, query names, and account header.
- Decimal string values never become floats. Covered for candles, prices,
  quantities, limits, buying power, and exchange rates. Identifier strings such
  as account numbers remain strings.
- Account/order APIs include `X-Tossinvest-Account`. Covered by tests.
- 401, 409, 422, 429 are handled by policy. Covered by tests.
- Order/account requests can be prioritized over broad watchlist screening.
  Covered by existing `RateLimitQueue` tests.

Live order create/modify/cancel is still intentionally unimplemented.

## Phase 4: State Store, Watchlist, and Position Sync

Implement:

- SQLite schema and migrations. Done with idempotent table creation and WAL for
  file databases.
- Watchlist tables. Done.
- Premarket watchlist builder for symbols near 20-day and 55-day breakout
  levels. Done.
- Position/state persistence. Done.
- Broker holdings reconciliation. Done through `TossPositionSync`.
- Open order reconciliation. Done for read-only broker order payloads.
- Manual trade/mismatch detection. Done by blocking local-only, broker-only,
  and quantity-mismatch states.
- Market data snapshot freshness tracking. Done in `MarketDataCache`; broker
  snapshot persistence added for holdings/open orders.

Acceptance:

- Restart can recover open position state. Covered by state store tests.
- Local/broker mismatch blocks new orders. Covered by reconcile tests.
- Duplicate unresolved client order id blocks new orders. Covered by state
  store tests.
- Watchlist generation cannot directly create trades. Covered by watchlist
  design and tests.
- Stale current price/orderbook data blocks live and paper order candidates.
  Covered by cache freshness tests.
- Durable state can explain why live trading is blocked after restart. Broker
  holdings/open order snapshots and reconcile blockers are persisted or
  serializable for health/reporting.

## Phase 5: Paper Trading Runtime

Implement:

- Runtime loop.
- Scheduler.
- Price polling.
- MarketDataCache.
- OrderIntent generation.
- OrderGuard.
- Paper broker recording.
- Notifier interface and console/log notifier.
- Reports.
- Local read-only health/status server if it can be kept safe.

Acceptance:

- Paper mode can run without sending orders.
- Every signal has a guard result.
- Every would-be order has a reason and rule snapshot.
- Premarket watchlist is logged and included in the daily report.
- Health/status endpoints expose state without mutating trading behavior.

## Phase 6: macOS Operations

Implement:

- `launchd` plist template.
- Setup/check commands.
- Log paths.
- Health command.
- Amphetamine/power checklist.
- Paper-mode service template.

Acceptance:

- macOS service starts paper mode.
- Restart runs reconciliation before decisions.
- Windows tests still pass.
- Service logs include mode, watchlist, market state, and current blocker
  status.

## Phase 7: Controlled Live Pilot

Live is not part of the initial implementation unless explicitly approved.

Preconditions:

- Backtest behavior accepted.
- Read-only Toss client verified.
- Paper mode observed through at least one full market session.
- OrderGuard tested with real account read-only checks.
- User explicitly enables live mode.

Pilot constraints:

- Minimum quantity.
- One symbol.
- One market.
- Hard daily order cap.
- Hard daily loss cap.
- Immediate alert on every order.
- Health API remains read-only.
- Any unknown broker state blocks all new orders for the affected symbol.

## Initial Work Package for Spark

Implement only Phases 0 and part of Phase 1:

- Project skeleton.
- CLI entrypoint.
- Config loader.
- Domain models.
- Indicator functions.
- Signal engine for long-only S1/S2.
- Unit tests for channel exclusion, N calculation, and basic signals.

Do not implement live Toss order submission in the first work package.

## Reference-Informed Work Package

After the strategy core, implement the operational layer in this order:

1. `RateLimitQueue`
2. `MarketDataCache`
3. `WatchlistBuilder`
4. `Notifier`
5. Read-only `HealthServer`
6. Runtime integration in paper mode

This ordering is intentional. It gives the paper runtime the same operational
shape as live mode before any live order path exists.
