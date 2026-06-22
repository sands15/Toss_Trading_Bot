# Toss API Contract

Verified against official Toss Securities Open API sources on 2026-06-11.

Official sources:

- LLM guide: <https://developers.tossinvest.com/llms.txt>
- Canonical OpenAPI JSON:
  <https://openapi.tossinvest.com/openapi-docs/latest/openapi.json>

The OpenAPI JSON is the source of truth. Generated clients and tests should be
refreshed from that document before live trading.

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

### Market Info

- `GET /api/v1/market-calendar/KR`
- `GET /api/v1/market-calendar/US`
- `GET /api/v1/exchange-rate`

### Stock Info

- `GET /api/v1/stocks`
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
before: optional ISO 8601 exclusive cursor
adjusted: optional, default true
```

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

## Order Create Response

Important fields:

```text
orderId
clientOrderId
```

Persist both.

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

`GET /api/v1/orders?status=CLOSED` is the account-scoped source for recently
finished orders and their execution summary. Do not use `GET /api/v1/trades`
as account trade history; that endpoint is market-data tick history for a
symbol and does not require `X-Tossinvest-Account`.

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
tokens are low. On 429, obey `Retry-After`.

## Request Safety

Before `POST /api/v1/orders`, OrderGuard must verify:

1. Token exists and is not expiring immediately.
2. Account sequence is resolved.
3. Market is open for the selected market/currency/order type.
4. PositionSync is clean.
5. Symbol is tradable and has no blocking warning.
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
