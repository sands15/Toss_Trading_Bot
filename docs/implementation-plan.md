# Implementation Plan

This plan is ordered to avoid accidental live trading before the strategy and
state model are trustworthy.

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

## Phase 2: Backtest Engine

Implement:

- CSV candle loader.
- Simulated broker.
- Conservative same-bar ordering.
- Fees/slippage/tax hooks.
- Trade and equity output.

Acceptance:

- Deterministic results from fixture data.
- Audit log explains every trade.
- Same-bar stop/pyramid conflict chooses stop.

## Phase 3: Toss Read-only Client

Implement:

- Auth client.
- Market data client.
- Account client.
- Holdings/open orders client.
- Rate-limit metadata capture.
- RateLimitQueue with request priority classes.
- HTTP mock tests.

Acceptance:

- Official OpenAPI field names are normalized correctly.
- Decimal string values never become floats.
- Account/order APIs include `X-Tossinvest-Account`.
- 401, 409, 422, 429 are handled by policy.
- Order/account requests can be prioritized over broad watchlist screening.

## Phase 4: State Store, Watchlist, and Position Sync

Implement:

- SQLite schema and migrations.
- Watchlist tables.
- Premarket watchlist builder for symbols near 20-day and 55-day breakout
  levels.
- Position/state persistence.
- Broker holdings reconciliation.
- Open order reconciliation.
- Manual trade/mismatch detection.
- Market data snapshot freshness tracking.

Acceptance:

- Restart can recover open position state.
- Local/broker mismatch blocks new orders.
- Duplicate unresolved client order id blocks new orders.
- Watchlist generation cannot directly create trades.
- Stale current price/orderbook data blocks live and paper order candidates.

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
