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
- HTTP mock tests.

Acceptance:

- Official OpenAPI field names are normalized correctly.
- Decimal string values never become floats.
- Account/order APIs include `X-Tossinvest-Account`.
- 401, 409, 422, 429 are handled by policy.

## Phase 4: State Store and Position Sync

Implement:

- SQLite schema and migrations.
- Position/state persistence.
- Broker holdings reconciliation.
- Open order reconciliation.
- Manual trade/mismatch detection.

Acceptance:

- Restart can recover open position state.
- Local/broker mismatch blocks new orders.
- Duplicate unresolved client order id blocks new orders.

## Phase 5: Paper Trading Runtime

Implement:

- Runtime loop.
- Scheduler.
- Price polling.
- OrderIntent generation.
- OrderGuard.
- Paper broker recording.
- Reports.

Acceptance:

- Paper mode can run without sending orders.
- Every signal has a guard result.
- Every would-be order has a reason and rule snapshot.

## Phase 6: macOS Operations

Implement:

- `launchd` plist template.
- Setup/check commands.
- Log paths.
- Health command.

Acceptance:

- macOS service starts paper mode.
- Restart runs reconciliation before decisions.
- Windows tests still pass.

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
