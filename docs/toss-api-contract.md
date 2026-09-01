# Toss API Contract

Verified against official Toss Securities Open API sources on 2026-08-30.

Official sources:

- LLM guide: <https://developers.tossinvest.com/llms.txt>
- Canonical OpenAPI JSON:
  <https://openapi.tossinvest.com/openapi-docs/latest/openapi.json>
- Canonical AsyncAPI JSON:
  <https://openapi.tossinvest.com/openapi-docs/latest/asyncapi.json>

The OpenAPI JSON is the source of truth. Generated clients and tests should be
refreshed from that document before live trading.

The verified REST contract is OpenAPI `1.2.14`; the UTF-8 SHA-256 of the
2026-08-30 response was
`a7b32ba754401d13fa649ba91eebd212420eb1afab28e9c2c0d6ea8d43055fed`.

The verified WebSocket contract is AsyncAPI `1.2.2`; the UTF-8 SHA-256 of the
2026-08-30 response was
`130251057fd9535a3e276099f9166b445f8c51f505f30540758e4b209231282e`.
The `latest` URL may drift, so a version or hash change requires contract review
before deployment.

## WebSocket Market Data

Endpoint:

```text
wss://openapi-ws.tossinvest.com/ws/v1
Authorization: Bearer {access_token}
```

The selected-symbol shadow service sends one full-replace declaration with a
request ID and exactly two market-data subscriptions:

```json
[
  {"id":"opaque-request-id"},
  {"type":"trade:us","codes":["AAPL"]},
  {"type":"orderbook:us","codes":["AAPL"]}
]
```

The server returns one `type=subscriptions` ACK before data. Shadow readiness
requires the exact subscribed set `trade:us:AAPL` and `orderbook:us:AAPL`, the
same request ID, and an empty `rejected` array. `personal:order` is deliberately
absent until a separate live order-state runtime exists.

The symbol is accepted only from the immutable account/session plan written by
the REST selector. The stream has no second symbol configuration path. News and
LLM output cannot add, replace, rank, or otherwise influence that symbol; the
news worker is restricted to the same locked context.

Trade and orderbook streams are lossy, provide no sequence, and send no initial
snapshot. Every connection therefore takes a read-only REST `/prices` and
`/orderbook` baseline after ACK, and every reconnect repeats it. There is no
cursor or atomic REST/stream boundary, so this does not prove gap-free replay
and must not by itself arm a live entry. The client sends raw text `PING` every
60 seconds and requires JSON `{"type":"pong"}`; server market data does not
replace the client keepalive.

REST price and orderbook timestamps are optional and nullable in OpenAPI
`1.2.14`. A null or stale timestamp is a valid response shape but not a verified
trading baseline: the shadow connection stays up, periodically retries REST,
and publishes `shadow_usable=false`. A market-data frame with an absent,
malformed, stale, or non-monotonic timestamp cannot make the snapshot usable.

The future live runtime uses one full-replace declaration containing exactly
the immutable symbol's `trade:us` and `orderbook:us` topics plus
`personal:order:{accountSeq}`. The personal topic is account-wide; it is not a
second symbol-selection path. A live release must stop the separate shadow
stream first, because an account supports at most two simultaneous WebSocket
connections and a newer connection can evict the oldest one.

Personal order events use event names such as `PARTIAL_FILL`, `FILL`, and
`CANCELING`, while REST order state uses `PARTIAL_FILLED`, `FILLED`, and
`PENDING_CANCEL`. The runtime maps them explicitly to a REST re-read trigger;
only REST detail's `execution.filledQuantity` advances the cumulative projection.
It never adds repeated WebSocket event quantities. AsyncAPI marks delivery lossless within a connected personal-topic
session, but a disconnected interval is not replayed. There is no replay cursor, sequence, initial snapshot, or
`clientOrderId` in the personal stream. Startup and reconnect therefore use:

```text
REST holdings/orders/conditionals -> WebSocket connect + exact ACK
-> REST holdings/orders/conditionals again -> publish a stable REST projection
```

The personal stream has no sequence, replay cursor, or event timestamp that can
order a queued frame against either REST snapshot; `order.orderedAt` is the
order's creation time, not an event sequence. Personal frames therefore never
directly advance fills or lifecycle state. They only mark broker state dirty and
trigger a fresh REST detail/snapshot read. This narrows the recovery gap but
cannot prove a gap-free stream. REST order detail, cumulative fills, holdings,
and the local ownership ledger remain the authority.

## Base Server

```text
https://openapi.tossinvest.com
```

## Authentication

Token endpoint:

```text
POST /oauth2/token
Content-Type: application/x-www-form-urlencoded
grant_type=client_credentials
client_id=...
client_secret=...
```

The success body is the top-level `access_token`, `token_type`, and `expires_in`
object. There is no refresh token; use the returned `expires_in` and issue a new
token at expiry. Toss permits only one valid token per OAuth client, so a new
issuance invalidates the previous token. Independent planner and stream processes
must therefore use different OAuth clients unless a single shared token issuer is
introduced.

Authentication failures are operationally distinct: `401 invalid_client` means
the client ID/secret pair is wrong or the client is inactive, while an unapproved
egress IP is `403 access_denied`. Neither is a transient retry condition.

All API calls use:

```text
Authorization: Bearer {access_token}
```

Account, asset, and order APIs also require:

```text
X-Tossinvest-Account: {accountSeq}
```

## Required Endpoint Groups

### Auth

- `POST /oauth2/token`

### Market Data

- `GET /api/v1/candles`
- `GET /api/v1/prices`
- `GET /api/v1/orderbook`
- `GET /api/v1/trades`
- `GET /api/v1/price-limits`

### Rankings

- `GET /api/v1/rankings`

### Market Info

- `GET /api/v1/market-calendar/KR`
- `GET /api/v1/market-calendar/US`
- `GET /api/v1/exchange-rate`

### Stock Info

- `GET /api/v1/stocks`
- `GET /api/v1/stocks/all`
- `GET /api/v1/stocks/{symbol}/warnings`

### Account and Asset

- `GET /api/v1/accounts`
- `GET /api/v1/holdings`

### Orders

- `POST /api/v1/orders`
- `GET /api/v1/orders`
- `GET /api/v1/orders/{orderId}`
- `POST /api/v1/orders/{orderId}/modify`
- `POST /api/v1/orders/{orderId}/cancel`
- `GET /api/v1/buying-power`
- `GET /api/v1/sellable-quantity`
- `GET /api/v1/commissions`

## Candle Request

Endpoint:

```text
GET /api/v1/candles
```

Important query params:

```text
symbol: required
interval: required, one of 1m or 1d
count: optional, max 200
before: optional ISO 8601 inclusive cursor
adjusted: optional, default true
```

For backward pagination, pass the response's `nextBefore` value unchanged as
the next request's `before`. Do not subtract a timestamp locally. Deduplicate
the inclusive boundary candle by its exact timestamp and reject a duplicate
whose normalized OHLCV content differs.

Response candle fields:

```text
timestamp
openPrice
highPrice
lowPrice
closePrice
volume
currency
```

All numeric values arrive as decimal strings. Convert to `Decimal`.

## Automatic Intraday Selector (Shadow Only)

The implemented selector uses this exact ranking request:

```text
GET /api/v1/rankings
type=MARKET_TRADING_AMOUNT
marketCountry=US
duration=realtime
excludeInvestmentCaution=true
count=20
```

This response is only a candidate source. In particular,
`MARKET_TRADING_AMOUNT / US / realtime` and its `tradingAmount` field must not be
described or persisted as completed premarket trading amount. The selector
strictly parses rank, unique symbol, USD values, timestamp freshness, price,
amount, and change thresholds before continuing.

The shadow pipeline is fixed:

1. Intersect the ranked top 20 with strict `GET /stocks/all` results for
   `ACTIVE / STOCK / commonShare=true` on NASDAQ, NYSE, and AMEX.
2. Review only the first five names in ranking order. Exact `GET /stocks`
   details must confirm USD, active common stock, and one of those exchanges.
3. Require `GET /stocks/{symbol}/warnings` to be exactly the empty array `[]`.
   A valid non-empty array skips that candidate; a malformed response fails the
   whole selector closed.
4. Require the prior 20 completed raw daily candles with `adjusted=false`, then
   validate freshness, positive volume, average daily value, and average range.
5. Use only fully completed premarket one-minute candles with `adjusted=false`,
   and validate their freshness, positive aggregate volume, and range.
6. Reconfirm that the whole account is flat and refresh USD cash buying power.
   Then fetch fresh current `/prices` and `/orderbook`, revalidate ranking
   freshness, and recompute the final price and change from the fresh last price.
7. Re-fetch the finalist's warnings and require the exact empty array again,
   then recheck that the whole account is still flat and refresh USD buying
   power once more. Rebuild the plan from that final cash value. At the database
   `lock_at`, revalidate the age of price, orderbook, cash, ranking, warning-check,
   and account-check data.
8. Persist at most one immutable plan per account and US session. Once locked,
   later iterations and restarts load that plan and never re-run selection.

News and LLM output have no selection influence. Only the locked symbol is
exported to the news and WebSocket contexts. This implementation remains
shadow-only and does not call order-create, order-modify, or order-delete paths.

The current REST OpenAPI exposes no authoritative US halt/LULD state field.
An empty warnings array is not proof that trading is not halted, and the null US
`price-limits` fields are not LULD bands. Live promotion is blocked until an
authoritative observed contract for that state is available and enforced. An
external Toss/Mac automatic-selector shadow smoke is still pending.

These final reads and `lock_at` freshness checks narrow but cannot eliminate a
TOCTOU window. Toss does not provide one atomic snapshot spanning broker account
state, market state, and the local SQLite transaction. An external manual order
can therefore race after the last warning/account GET and before the plan INSERT.
Live promotion is also blocked unless an operationally exclusive account, a ban
on manual/other writers for the session, and one fenced account writer are
enforced as the equivalent coordination boundary. If that rule cannot be
guaranteed, the residual race remains a blocker.

## Order Create Request

Endpoint:

```text
POST /api/v1/orders
```

MVP uses only quantity-based orders:

```text
clientOrderId: optional but required by our bot
symbol: required
side: BUY | SELL
orderType: LIMIT | MARKET
timeInForce: DAY | CLS
quantity: decimal string, integer pattern for quantity-based create
price: decimal string, required for LIMIT and forbidden for MARKET
confirmHighValueOrder: boolean, default false
```

MVP excludes amount-based orders:

```text
orderAmount: US MARKET only; do not use in MVP
```

The schema does not expose a session selector or a complete premarket/regular/
after-hours support matrix for US whole-share orders. The bot's
regular-session-only rule is therefore a local fail-closed policy, not a broker
guarantee inferred from the order schema. The pilot uses integer-quantity
`LIMIT + DAY` for entry. Integer-quantity `SELL + MARKET` exists for emergency
or time exit, but it is submitted only while the official calendar says the US
regular session is open and broker state is unambiguous.

## Order Create Response

Important fields:

```text
orderId: required
clientOrderId: optional/nullable echo
```

Persist the locally reserved `clientOrderId` and required broker `orderId`.
When the optional response echo is present, require it to equal the reserved
value exactly; a missing/null echo is not a reason to discard an otherwise
schema-valid `orderId`.

`clientOrderId` is supported only on general and conditional **create** calls.
It is at most 36 characters and may contain letters, digits, `-`, and `_`.
For general order create, the server's documented idempotency window is ten
minutes:

- same key and byte-equivalent request inside the window returns the original
  result;
- same key with a different request conflicts;
- `409 request-in-progress` means the first request may still be running;
- after ten minutes the same key may create another order.

Order list/detail and personal-order WebSocket payloads do not provide a
`clientOrderId` search or echo. The runtime must reserve the deterministic key,
canonical request hash, and first-attempt time in SQLite before the network
call, then persist the returned `orderId` immediately. A create result that is
unknown may use the exact same key and exact same body only within a shorter
local recovery deadline inside the broker's ten-minute window, and only to
recover one order identity. It must never be rebuilt from current values.
After the local deadline it remains `UNKNOWN` and no automatic create retry is
allowed. The conditional-create schema calls the field an idempotency key but
does not explicitly state the same ten-minute window. Until Toss confirms that
window, an ambiguous OCO create gets **zero** automatic recovery POSTs: it is
handled with read-only reconciliation and then `RECOVERY_REQUIRED`. Modify and
cancel have no idempotency key; an ambiguous result is reconciled through REST
and is never blindly repeated.

## Conditional Orders and OCO

The supported types are `SINGLE`, `OCO`, and `OTO`. The intraday exit protection
uses OCO only after a normal BUY's cumulative filled quantity is known.

- OCO is two `SELL` legs sharing one quantity.
- Both legs are `LIMIT`; the stop leg is stop-limit and is not a guaranteed fill.
- Create requires `expireDate` (`YYYY-MM-DD`). It is part of the immutable
  request fixture and idempotency hash. Although detail may expose it, OpenAPI
  does not mark the detail response property required; a missing value therefore
  fails protection verification. Its timezone/cutoff meaning is not inferred
  beyond the literal broker date contract.
- `first.triggerPrice > current price > second.triggerPrice` must hold when the
  group is created.
- A US conditional order watches every tradable session. There is no
  regular-session-only selector.
- OTO provides BUY followed by one SELL, not atomic BUY followed by OCO. There
  is no three-leg bracket/OTOCO contract.
- A triggered leg exposes `triggeredOrderId`; that general order's cumulative
  fill and holdings, not the conditional group's status alone, prove exit.
- Modifying a conditional order cancels and recreates it under a new ID, so v1
  does not resize or trail a live OCO.

The group states are `WATCHING`, `PAUSED`, `ORDERING`, `ORDERED`, `COMPLETED`,
and `EXPIRED`. The top-level group never reports `CANCELED`; `HOLDING` and
`CANCELED` are leg-only states. Any unknown state blocks a new entry. Conditional
cancel is exactly `DELETE /api/v1/conditional-orders/{id}` with success `204 No
Content`; it returns no operation ID. Before a separate time-exit SELL, the
runtime requires a persisted exact-204 acknowledgement followed by a stable
REST snapshot proving that the group is no longer active and that no leg-created
SELL exists. A lost/ambiguous DELETE response plus later OPEN-list absence is
not cancellation proof and leaves the run in `RECOVERY_REQUIRED`.

## Buying Power

Endpoint:

```text
GET /api/v1/buying-power
```

Required:

```text
X-Tossinvest-Account header
currency query param
```

Response:

```text
currency
cashBuyingPower
```

`cashBuyingPower` is a decimal string.

## Sellable Quantity

Endpoint:

```text
GET /api/v1/sellable-quantity
```

Required:

```text
X-Tossinvest-Account header
symbol query param
```

Response:

```text
sellableQuantity
```

KR quantities are integer shares. US may support fractional sellable quantity,
but the MVP should still use quantity-based integer orders unless explicitly
approved otherwise.

## Order Status Handling

`GET /api/v1/orders?status=OPEN` returns pending/open states and is the primary
duplicate-order defense. Use it before new order creation.

`GET /api/v1/orders?status=CLOSED` is an account-scoped source for order
history and its execution summary. Do not use `GET /api/v1/trades`
as account trade history; that endpoint is market-data tick history for a
symbol and does not require `X-Tossinvest-Account`.

REST order status is one of `PENDING`, `PENDING_CANCEL`, `PENDING_REPLACE`,
`PARTIAL_FILLED`, `FILLED`, `CANCELED`, `REJECTED`, `CANCEL_REJECTED`,
`REPLACE_REJECTED`, or `REPLACED`. `execution.filledQuantity` is cumulative and
required. A canceled, rejected, or replaced order can still have a non-zero
fill, so terminal status never substitutes for reading the cumulative fill and
holdings. `PARTIAL_FILLED` is not terminal and is documented in both list-group
schemas, so OPEN/CLOSED membership never substitutes for parsing the exact
order status and detail. If the same order ID appears across the two fetched
sets, deduplicate only identical normalized data and re-read detail; divergent
copies fail reconciliation.

The exact average-fill key is `execution.averageFilledPrice`, not `avgPrice`.
It is a required key whose value may be a decimal string or `null`. Intraday
ownership parsing rejects a missing `execution`, missing cumulative quantity,
unknown status, decreasing cumulative fill, or fill above requested quantity;
it never substitutes zero for malformed broker data.

`OPEN` returns the current open set and ignores pagination. `CLOSED` is
cursor-paginated, so recovery must read every page covering the session rather
than only the default first 20 rows. US modify changes price only. Modify and
cancel success can return a new operation order ID; the original and returned
IDs are stored as one local chain.

The implementation must tolerate unknown future enum values in broker
responses. Unknown order status must block live orders for that symbol until
manually or programmatically reconciled.

## Rate Limits

The official overview describes rate-limit groups and headers including:

- `X-RateLimit-Limit`
- `X-RateLimit-Remaining`
- `X-RateLimit-Reset`
- `Retry-After`

The runtime must keep a per-group budget and must slow polling if remaining
tokens are low. A read-only GET may obey `Retry-After` inside its bounded
reconciliation deadline. A create 429 is UNKNOWN and may continue only through
the endpoint-specific identity rule (general create at most one exact recovery;
conditional create none until its window is confirmed). General cancel and
conditional DELETE never retry after 429.

## Request Safety

Before `POST /api/v1/orders`, OrderGuard must verify:

1. Token exists and is not expiring immediately.
2. Account sequence is resolved.
3. Market is open for the selected market/currency/order type.
4. PositionSync is clean.
5. Symbol is tradable and has no blocking warning; this is not a US halt/LULD
   assertion, so live entry remains blocked without an authoritative status source.
6. No duplicate open order exists.
7. Buying power or sellable quantity is enough.
8. Price is rounded to allowed tick rules.
9. Risk and unit limits are not exceeded.
10. `clientOrderId` is unique in local DB for unresolved orders.

## API Client Test Strategy

Tests must include:

- Decimal string parsing.
- Required `X-Tossinvest-Account` header for account/order APIs.
- Token refresh on 401 exactly once.
- 409 reconciliation path.
- 422 non-retry path.
- 429 backoff path.
- Unknown enum tolerance.
- Exact automatic-selector ranking query and strict top-20-to-top-five universe flow.
- Exact empty warnings, raw prior daily candles, completed premarket one-minute
  candles, fresh price/book, final warning/flat-account/cash/price/change
  rechecks, lock-boundary freshness, and restart without reselection.
- Full CLOSED cursor traversal with cursor-repeat and missing-next-cursor
  rejection; OPEN is treated as one complete unpaginated set.
- Official REST `PARTIAL_FILLED` versus personal WebSocket `PARTIAL_FILL`
  mapping, terminal orders with non-zero cumulative fill, and exact
  `averageFilledPrice` parsing.
- Create accepted-then-timeout, 409/429/5xx, one exact identity recovery inside
  the local eight-minute deadline, and zero create calls after it.
- Cancel/conditional-delete response loss with zero blind retry and preserved
  general-order root/operation ID chain; conditional DELETE must accept only
  exact 204, persist no invented operation ID, and block a separate SELL after
  an ambiguous response.

The current milestone excludes live-account tests. Adapter contract tests use a
fake transport only. Shadow clients additionally use a read-only transport
tripwire that permits `GET` and exactly `POST /oauth2/token`; all other methods
are rejected before the underlying transport. Suite teardown must prove zero
real Toss HTTP and WebSocket connections even when fake order mutations were
exercised. This proof runs with Toss/Discord credentials removed in the parent
process and process-level external egress denied; in-process monkeypatches and
counters are defense in depth, not the sole evidence.
