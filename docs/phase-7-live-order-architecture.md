# Phase 7 Live Order Architecture

This document defines the intended architecture for Phase 7: turning the Toss
Trading Bot from a paper/shadow validation platform into a controlled live order
bot.

The current system should keep treating live trading as disabled until every
layer below is implemented, tested, and operationally observable.

## Current Baseline

As of Phase 6, the bot is close to complete as a paper/shadow validation system.
It has strategy runtime, read-only Toss access, reconciliation primitives, state
storage, dashboard readiness checks, and tests around simulated order behavior.

It is not yet a full live auto-ordering product because the real order
create/modify/cancel layer is intentionally absent.

## Phase 7 Goal

Phase 7 should add live execution without weakening the existing paper/shadow
safety model.

The target design is to separate these responsibilities clearly:

1. Strategy decides what it wants.
2. Order domain converts that into a normalized intent.
3. Pre-trade safety decides whether the intent is allowed.
4. Execution orchestration submits and tracks the order.
5. Broker adapter talks to Toss.
6. Reconciliation treats broker state as truth.
7. Operations can pause, inspect, and recover the system.

## Layer 1: Order Domain

Purpose: convert strategy output into a normalized `OrderIntent`.

Responsibilities:

- Normalize `symbol`, `side`, `quantity`, `price`, `order_type`, and time limit.
- Generate a deterministic idempotency key.
- Attach risk context such as strategy name, signal timestamp, account, and
  position snapshot.
- Reject malformed intent before it reaches safety or broker code.

Expected outputs:

- `OrderIntent`
- `OrderIntentRejected`

Suggested module:

- `src/turtle_bot/domain/order.py`

## Layer 2: Pre-Trade Safety

Purpose: act as the final gate before any live broker mutation.

Required checks:

- Live trading is explicitly enabled.
- Symbol is in the allowlist.
- Market is open and tradable.
- Quantity and notional amount are within configured limits.
- Available cash and position are sufficient.
- Daily order count and daily notional limits are not exceeded.
- Daily realized/unrealized loss limit is not exceeded.
- No unresolved broker order blocks the same symbol/side path.
- Emergency stop is off.
- Reconciliation status is clean.

Failure behavior:

- Do not submit the order.
- Persist a `risk_block` event with the reason.
- Return a structured rejection code.

Suggested module:

- `src/turtle_bot/safety/pretrade.py`

## Layer 3: Execution Orchestrator

Purpose: manage the live order state machine and idempotency boundary.

Responsibilities:

- Convert approved `OrderIntent` into broker requests.
- Persist state before submitting to Toss.
- Prevent duplicate submit for the same idempotency key.
- Track lifecycle states.
- Promote uncertain broker responses to `UNKNOWN` and force reconciliation.
- Own retry policy for query/recovery, not blind resubmission.

Required state machine:

```text
PENDING
SENT
ACKNOWLEDGED
PARTIAL_FILLED
FILLED
CANCELLED
REJECTED
FAILED
UNKNOWN
```

Suggested module:

- `src/turtle_bot/execution/orchestrator.py`

## Layer 4: Broker Adapter

Purpose: isolate Toss live order API calls behind a narrow interface.

The existing read-only client should stay clearly separated from mutation
methods. Live mutation should be introduced through a dedicated adapter so
paper/shadow paths do not accidentally gain write capability.

Required interface:

```python
class BrokerOrderAdapter:
    def place_order(self, intent: OrderIntent) -> BrokerOrderTicket:
        ...

    def modify_order(self, ticket_id: str, request: ModifyOrderRequest) -> BrokerOrderTicket:
        ...

    def cancel_order(self, ticket_id: str) -> BrokerOrderTicket:
        ...

    def query_order(self, ticket_id: str) -> BrokerOrderState:
        ...
```

Implementation rules:

- No strategy code may call this adapter directly.
- Only the execution orchestrator may call live mutation methods.
- Every request and response must be recorded in the execution ledger.
- API exceptions must not disappear into logs only; they must become execution
  events.

Suggested module:

- `src/turtle_bot/execution/broker_adapter.py`

## Layer 5: Reconciliation And Truth

Purpose: keep local state aligned with Toss broker state.

Broker state is the source of truth. Local state is an execution ledger and
runtime cache, not authority.

Responsibilities:

- Periodically query unresolved broker orders.
- Merge broker status into local execution state.
- Detect local/broker quantity mismatch.
- Detect unknown broker status.
- Pause live trading when reconciliation is stale or dirty.
- Produce operator-readable reasons for blocking live trading.

Suggested module:

- `src/turtle_bot/reconciliation/service.py`

## Layer 6: Execution Ledger

Purpose: preserve an auditable history of live trading decisions and broker
effects.

Required records:

- Order intent created.
- Risk decision made.
- Broker request sent.
- Broker response received.
- Broker query result received.
- State transition applied.
- Manual operator action taken.
- Emergency stop or automatic pause triggered.

Minimum tables or logical stores:

- `order_intent`
- `execution_order`
- `execution_event`
- `risk_block`
- `manual_action`

Rules:

- Execution events should be append-only.
- Current state can be materialized, but raw events must be preserved.
- Unknown or failed states must retain enough context for replay and debugging.

Suggested module:

- `src/turtle_bot/storage/ledger.py`

## Layer 7: Operations And Guardrails

Purpose: make live trading controllable by a human operator.

Required controls:

- Global live trading enable flag.
- Emergency stop.
- Per-symbol allowlist.
- Per-order max notional.
- Daily max notional.
- Daily max order count.
- Daily loss limit.
- Reconciliation freshness threshold.
- Dashboard readiness flag: `can_submit_live_orders`.

Required dashboard states:

- `LIVE_DISABLED`
- `READY_FOR_SHADOW`
- `READY_FOR_LIVE_PILOT`
- `LIVE_PAUSED_BY_RISK`
- `LIVE_PAUSED_BY_RECONCILIATION`
- `LIVE_ACTIVE`

Suggested module:

- `src/turtle_bot/ops/live_mode.py`

## Layer 8: Runtime Coordinator

Purpose: select paper, shadow, or live mode while preserving one strategy
pipeline.

Runtime rules:

- `paper` records simulated orders only.
- `shadow` records real broker observations and simulated orders only.
- `live` may submit broker orders only after all safety checks pass.
- Mode switching must be explicit.
- Live mode must never be enabled by default config examples.

Suggested integration points:

- `src/turtle_bot/runtime.py`
- `src/turtle_bot/paper_runtime.py`
- `src/turtle_bot/operations.py`
- `src/turtle_bot/cli.py`

## Implementation Order

Recommended sequence:

1. Define `OrderIntent`, order state enums, and execution event models.
2. Add execution ledger persistence.
3. Add pre-trade safety with hard-coded live-disabled default.
4. Add broker adapter interface with no live implementation enabled yet.
5. Add execution orchestrator against a fake adapter.
6. Wire shadow mode through the same intent and ledger path.
7. Add reconciliation-driven live readiness.
8. Implement Toss live adapter against the latest Toss Open API contract.
9. Add dashboard and ops controls for live pilot readiness.
10. Run a controlled live pilot with tiny limits and full logging.

## Live Pilot Entry Criteria

Do not start a live pilot until all criteria are met:

- Latest Toss Open API order contract has been reviewed.
- Contract tests cover create, modify, cancel, and query mapping.
- Full shadow session has completed with clean reconciliation.
- Emergency stop is proven to block new orders.
- Duplicate idempotency key submit is proven to be blocked.
- Unknown broker response recovery is proven.
- Per-order, per-day, and per-symbol limits are active.
- Dashboard reports `can_submit_live_orders = true` only when all gates are
  clean.

## Live Pilot Limits

Initial live pilot should use deliberately tiny limits:

- Very small per-order notional cap.
- One or two allowlisted symbols.
- Low daily order count.
- No pyramiding.
- No automatic re-entry after a failed or unknown order.
- Manual operator approval before increasing limits.

## Non-Negotiable Safety Rules

- Never submit live orders while reconciliation is dirty.
- Never retry a failed submit by blindly sending a second order.
- Never allow strategy code to call Toss mutation APIs directly.
- Never default examples or local config into live mode.
- Never treat a local filled state as authoritative without broker confirmation.
- Never hide broker API exceptions in logs only.

## Completion Definition

Phase 7 can be considered complete only when the system can:

- Produce a normalized order intent from strategy output.
- Block unsafe intent before broker submission.
- Submit a live Toss order through a single orchestrated path.
- Track the broker state through fill, cancel, failure, or unknown recovery.
- Reconcile local state against Toss state.
- Pause live trading automatically on risk or reconciliation failure.
- Show clear operator status in the dashboard.
- Preserve enough ledger data to explain every live decision after the fact.

