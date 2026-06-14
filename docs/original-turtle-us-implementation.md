# Original Turtle (US Equities/ETFs) Target

## Scope

This document is the implementation target for a rules-first US equities and ETF
Turtle deployment in this repository. The final target is original Turtle rule
coverage, including long and short systems. Live short execution stays blocked
until broker permissions, borrow availability, margin rules, and operator policy
are explicit and tested.

## Source-of-Truth Principles

1. Strategy behavior is governed by `docs/turtle-rules.md` and the original
   Turtle logic in the repo (`src/turtle_bot/strategy.py` and `src/turtle_bot/indicators.py`).
2. Official Toss API contracts are the protocol contract for live data and broker
   behavior (`docs/toss-api-contract.md` and upstream OpenAPI).
3. A run is reproducible from recorded inputs: timestamped market candles,
   warnings, price/quote snapshots, and universe membership as-of date.
4. Any uncertainty blocks execution (`block`, not `approximate`).
5. Every signal/blocker must map to an explicit Turtle rule or guard check and be
   auditable in runtime events.

## Exact Original-Style Rules (US Adaptation)

- One market at a time: `US`. Use US sessions in `America/New_York` context.
- Final strategy scope includes both long and short breakouts. Live execution may
  run long-only as a safety/policy restriction, but that restriction must be
  explicit in config and reports.
- Daily-bar basis with completed-bar inputs only.
- Donchian channels on daily OHLCV:
  - Long entry triggers: 20-day high (System 1) and 55-day high (System 2) using
    the previous completed bar as the latest input.
  - Short entry triggers: 20-day low (System 1) and 55-day low (System 2) using
    the previous completed bar as the latest input.
  - Long exits: 10-day low (System 1) and 20-day low (System 2).
  - Short exits: 10-day high (System 1) and 20-day high (System 2).
  - N smoothing: N[t] = (19*N[t-1] + TR[t]) / 20, where TR is standard true range.
  - Long breakout check uses >= entry_high + minimum_tick, including gap-open context.
  - Short breakout check uses <= entry_low - minimum_tick, including gap-open context.
- System-1 skip rule retained:
  skip only the next System-1 signal after a profitable System-1 outcome.
- Same-symbol event priority:
  STOP -> CHANNEL_EXIT -> PYRAMID -> SYSTEM2_ENTRY -> SYSTEM1_ENTRY.
- Pyramid to winners only; add every 0.5N using the initial entry N.
- Max 4 units per symbol. Portfolio caps must distinguish long units and short
  units; initial live policy may set short cap to 0.
- Risk sizing:
  quantity = floor(account_equity * risk_pct_per_unit / (2*N)) with lot/tick rounding.
- MVP quantity is integer shares only; no amount-based orders.

## Data Requirements (Including Survivorship-Bias-Free PIT Universe)

- Candles: Toss 1d candles, Decimal-safe parsing. Exclude partial current-session bar.
- History depth: at least 56 completed sessions before symbol eligibility.
- Market session: GET /api/v1/market-calendar/US gate.
- Executability metadata: symbol warnings, tradability, and order constraints.
- Price precision: symbol-specific tick/limit source required for safe rounding.
- Point-in-time universe:
  - Universe input must be PIT snapshots by as-of date.
  - CSV schema: `as_of` or `date`, `symbol`, `included` or `eligible`;
    optional `reasons`, `market`, `instrument_type`.
  - Symbols enter/leave based on as-of tradability windows only.
  - Persist as-of universe rows + inclusion/exclusion reasons.
  - Missing PIT coverage for session date is a hard blocker.
- ETF policy:
  - `runtime.universe_include_etfs` must be explicit.
  - ETFs pass the same channel and warning checks as equities unless excluded.

## Implementation Phases

## Current Implementation Status

- Implemented:
  - Strategy-layer long/short direction model.
  - Short S1/S2 entry signals, short channel exits, short stops, and short pyramids.
  - Direction-specific S1 skip state.
  - Position direction persistence in SQLite state.
  - Backtest long/short direction policy, short cash/PnL/equity math, and separate
    long/short portfolio unit caps.
  - CLI/config controls for backtest direction and short unit caps.
  - CSV PIT universe loader/writer, exact-date coverage blocker, scan-backtest
    filtering, portfolio entry filtering, and config/CLI PIT path support.
- Not implemented yet:
  - Authoritative survivorship-bias-free US PIT source ingestion.
  - Broker borrow availability, margin, and shortability checks.
  - Live short order submission. This remains blocked by policy and broker checks.
  - Full US data adapter alignment and PIT replay suite.

### Phase 1: Source Contracts and Safety Baseline
- Finalize this rule/source hierarchy and required US settings.
- Add tests for calendar logic, source precedence, and default blocker behavior.

### Phase 2: PIT Universe Pipeline
- Build PIT ingestion schema for symbol membership and status history.
- Selector uses only as-of eligible symbols.
- Persist a `universe_generated` snapshot for every run.

### Phase 3: US Market Adapter Alignment
- Update market-data and stock-info adapters for US edge cases.
- Enforce completed-candle and staleness rules end-to-end.

### Phase 4: Backtest and Simulation Conformance
- Add US-specific fixtures for gap behavior, S1 skip continuity, and pyramiding.
- Add PIT-aware replay tests with deterministic outputs.

### Phase 5: Guard-First Execution Integration
- Keep paper intent flow intact and do not submit broker orders until approved.
- Add live guard checks for PIT membership, open-status reconciliation, tick precision,
  duplicate intent IDs, and risk caps.

### Phase 6: Controlled Live Pilot
- One-market one-symbol pilot with hard daily loss and order-count caps.
- Expand only after full paper validation and acceptance suite pass.

## Live Blockers (Must Block Order Submission)

- Missing/invalid config (live flag, credentials, account_seq).
- Market closed/unknown by Toss US calendar.
- Stale or missing candles/prices/metadata.
- Local position state mismatch with broker (broker-only, local-only, quantity mismatch).
- Unresolved broker order status (open/pending/partial/unknown).
- Duplicate unresolved clientOrderId.
- Missing PIT membership for session date.
- Non-tradable metadata flags (warnings, suspension, delist, halt).
- Missing tick/price-limit info.
- Unit cap, buying power, or sellable quantity breach.

## Acceptance Tests

- Backtest behavior:
  - No signal before sufficient history.
  - S1/S2 entries respect completed-bar channels.
  - Short S1/S2 entries use completed-bar low channels when short mode is enabled.
  - Short exits use completed-bar high channels and buy-to-cover side.
  - S1 skip applies exactly once after a profitable S1 outcome.
  - S1 skip is direction-specific; a profitable short S1 does not skip a long S1.
  - Exits/stops have priority over adds and entries.
  - 0.5N pyramids use the position entry N.
  - Short pyramids add only after a favorable 0.5N move downward.
  - Gap entries fill at open in backtest.
- Data and universe:
  - PIT universe reproducibility by as-of date.
  - Stable inclusion/exclusion reasons.
  - Stale or unknown market-data/calendar blocks execution.
- Safety:
  - Reconciliation mismatch blocks new orders.
  - Open/unknown/duplicate broker order states block symbol trading.
  - Live transition is blocked unless all blockers are clear.
