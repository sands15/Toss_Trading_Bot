from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from typing import Any, Mapping, Protocol
from urllib import parse, request
from urllib.error import HTTPError

from .domain import Candle, as_decimal
from .rate_limit import RateLimitHeaderSnapshot, RateLimitQueue


TOSS_BASE_URL = "https://openapi.tossinvest.com"
ACCOUNT_HEADER = "X-Tossinvest-Account"
DECIMAL_FIELD_NAMES = frozenset(
    {
        "averageFilledPrice",
        "averagePurchasePrice",
        "basisPoint",
        "cashBuyingPower",
        "closePrice",
        "commission",
        "commissionRate",
        "dailyProfitLoss",
        "filledAmount",
        "filledQuantity",
        "highPrice",
        "krw",
        "lastPrice",
        "lowPrice",
        "lowerLimitPrice",
        "marketValue",
        "openPrice",
        "orderAmount",
        "price",
        "profitLoss",
        "quantity",
        "rate",
        "midRate",
        "sellableQuantity",
        "tax",
        "totalPurchaseAmount",
        "upperLimitPrice",
        "usd",
        "volume",
    }
)


@dataclass(frozen=True)
class TossCredentials:
    client_id: str
    client_secret: str


@dataclass(frozen=True)
class TossToken:
    access_token: str
    token_type: str
    expires_at: datetime

    def is_expiring(self, *, now: datetime, within: timedelta = timedelta(minutes=1)) -> bool:
        return self.expires_at <= now + within


@dataclass(frozen=True)
class TossHttpResponse:
    status: int
    headers: Mapping[str, str]
    payload: Any


@dataclass(frozen=True)
class CandlePage:
    candles: tuple[Candle, ...]
    next_before: str | None
    raw: Mapping[str, Any]


class TossTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        query: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        form_body: Mapping[str, Any] | None = None,
    ) -> TossHttpResponse:
        ...


class UrllibTossTransport:
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        query: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        form_body: Mapping[str, Any] | None = None,
    ) -> TossHttpResponse:
        full_url = _with_query(url, query)
        body: bytes | None = None
        request_headers = dict(headers)
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        elif form_body is not None:
            body = parse.urlencode(form_body).encode("utf-8")
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"

        req = request.Request(
            full_url,
            data=body,
            headers=request_headers,
            method=method.upper(),
        )
        try:
            with request.urlopen(req, timeout=15) as response:
                raw_body = response.read().decode("utf-8")
                payload = json.loads(raw_body) if raw_body else None
                return TossHttpResponse(
                    status=response.status,
                    headers=dict(response.headers.items()),
                    payload=payload,
                )
        except HTTPError as exc:
            raw_body = exc.read().decode("utf-8")
            try:
                payload = json.loads(raw_body) if raw_body else None
            except json.JSONDecodeError:
                payload = {"error": {"code": "non-json-error", "message": raw_body}}
            return TossHttpResponse(
                status=exc.code,
                headers=dict(exc.headers.items()),
                payload=payload,
            )


class TossApiError(RuntimeError):
    def __init__(
        self,
        status: int,
        *,
        code: str | None = None,
        message: str | None = None,
        payload: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.payload = payload
        self.headers = dict(headers or {})
        super().__init__(message or code or f"Toss API error {status}")


class TossClient:
    """Read-only Toss OpenAPI client.

    This client intentionally exposes no order creation, modification, or cancel
    method. Trading code must go through a separate, guarded live-order layer.
    """

    def __init__(
        self,
        *,
        credentials: TossCredentials | None = None,
        account_seq: int | str | None = None,
        base_url: str = TOSS_BASE_URL,
        transport: TossTransport | None = None,
        rate_limits: RateLimitQueue | None = None,
        now=lambda: datetime.now(timezone.utc),
    ) -> None:
        self.credentials = credentials
        self.account_seq = str(account_seq) if account_seq is not None else None
        self.base_url = base_url.rstrip("/")
        self.transport = transport or UrllibTossTransport()
        self.rate_limits = rate_limits or RateLimitQueue(now=now)
        self._now = now
        self._token: TossToken | None = None

    @property
    def token(self) -> TossToken | None:
        return self._token

    def issue_token(self) -> TossToken:
        if self.credentials is None:
            raise ValueError("Toss credentials are required to issue a token")

        response = self.transport.request(
            "POST",
            self._url("/oauth2/token"),
            headers={},
            form_body={
                "grant_type": "client_credentials",
                "client_id": self.credentials.client_id,
                "client_secret": self.credentials.client_secret,
            },
        )
        self._capture_rate_limit("auth", response.headers)
        self._raise_for_error(response)

        payload = _expect_mapping(response.payload)
        expires_in = int(payload.get("expires_in", 0))
        token = TossToken(
            access_token=str(payload["access_token"]),
            token_type=str(payload.get("token_type", "Bearer")),
            expires_at=self._now() + timedelta(seconds=expires_in),
        )
        self._token = token
        return token

    def get_candles(
        self,
        symbol: str,
        *,
        interval: str = "1d",
        count: int = 100,
        before: str | datetime | None = None,
        adjusted: bool = True,
    ) -> CandlePage:
        query: dict[str, Any] = {
            "symbol": symbol,
            "interval": interval,
            "count": count,
            "adjusted": adjusted,
        }
        if before is not None:
            query["before"] = before.isoformat() if isinstance(before, datetime) else before

        payload = self._request_json("GET", "/api/v1/candles", query=query, group="market")
        raw = _expect_mapping(payload)
        candles = tuple(
            Candle.from_api({"symbol": symbol, **item})
            for item in _expect_sequence(raw.get("candles", ()))
        )
        return CandlePage(
            candles=candles,
            next_before=raw.get("nextBefore"),
            raw=raw,
        )

    def get_prices(self, symbols: list[str] | tuple[str, ...]) -> Mapping[str, Any]:
        payload = self._request_json(
            "GET",
            "/api/v1/prices",
            query={"symbols": ",".join(symbols)},
            group="market",
        )
        return _normalize_decimal_payload(payload)

    def get_orderbook(self, symbol: str) -> Mapping[str, Any]:
        payload = self._request_json(
            "GET",
            "/api/v1/orderbook",
            query={"symbol": symbol},
            group="market",
        )
        return _normalize_decimal_payload(payload)

    def get_trades(self, symbol: str, *, count: int = 50) -> Mapping[str, Any]:
        payload = self._request_json(
            "GET",
            "/api/v1/trades",
            query={"symbol": symbol, "count": count},
            group="market",
        )
        return _normalize_decimal_payload(payload)

    def get_price_limits(self, symbol: str) -> Mapping[str, Any]:
        payload = self._request_json(
            "GET",
            "/api/v1/price-limits",
            query={"symbol": symbol},
            group="market",
        )
        return _normalize_decimal_payload(payload)

    def get_market_calendar(self, market: str, *, date: str | None = None) -> Mapping[str, Any]:
        market_key = market.strip().upper()
        if market_key not in {"KR", "US"}:
            raise ValueError("market must be KR or US")
        query = {"date": date} if date is not None else None
        payload = self._request_json(
            "GET",
            f"/api/v1/market-calendar/{market_key}",
            query=query,
            group="market_info",
        )
        return _normalize_decimal_payload(payload)

    def get_exchange_rate(
        self,
        *,
        base_currency: str,
        quote_currency: str,
        date_time: str | datetime | None = None,
    ) -> Mapping[str, Any]:
        query: dict[str, Any] = {
            "baseCurrency": base_currency,
            "quoteCurrency": quote_currency,
        }
        if date_time is not None:
            query["dateTime"] = (
                date_time.isoformat() if isinstance(date_time, datetime) else date_time
            )
        payload = self._request_json(
            "GET",
            "/api/v1/exchange-rate",
            query=query,
            group="market_info",
        )
        return _normalize_decimal_payload(payload)

    def get_stocks(self, symbols: list[str] | tuple[str, ...]) -> Mapping[str, Any]:
        payload = self._request_json(
            "GET",
            "/api/v1/stocks",
            query={"symbols": ",".join(symbols)},
            group="market_info",
        )
        return _normalize_decimal_payload(payload)

    def get_stock_warnings(self, symbol: str) -> Mapping[str, Any]:
        payload = self._request_json(
            "GET",
            f"/api/v1/stocks/{parse.quote(symbol, safe='')}/warnings",
            group="market_info",
        )
        return _normalize_decimal_payload(payload)

    def get_accounts(self) -> Mapping[str, Any]:
        payload = self._request_json("GET", "/api/v1/accounts", group="account")
        return _normalize_decimal_payload(payload)

    def get_holdings(self, *, symbol: str | None = None) -> Mapping[str, Any]:
        query = {"symbol": symbol} if symbol is not None else None
        payload = self._request_json(
            "GET",
            "/api/v1/holdings",
            query=query,
            account_required=True,
            group="account",
        )
        return _normalize_decimal_payload(payload)

    def get_orders(
        self,
        *,
        status: str = "OPEN",
        symbol: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> Mapping[str, Any]:
        query: dict[str, Any] = {"status": status, "limit": limit}
        if symbol is not None:
            query["symbol"] = symbol
        if from_date is not None:
            query["from"] = from_date
        if to_date is not None:
            query["to"] = to_date
        if cursor is not None:
            query["cursor"] = cursor
        payload = self._request_json(
            "GET",
            "/api/v1/orders",
            query=query,
            account_required=True,
            group="orders",
        )
        return _normalize_decimal_payload(payload)

    def get_order(self, order_id: str) -> Mapping[str, Any]:
        payload = self._request_json(
            "GET",
            f"/api/v1/orders/{parse.quote(order_id, safe='')}",
            account_required=True,
            group="orders",
        )
        return _normalize_decimal_payload(payload)

    def get_buying_power(self, currency: str) -> Mapping[str, Any]:
        payload = self._request_json(
            "GET",
            "/api/v1/buying-power",
            query={"currency": currency},
            account_required=True,
            group="account",
        )
        return _normalize_decimal_payload(payload)

    def get_sellable_quantity(self, symbol: str) -> Mapping[str, Any]:
        payload = self._request_json(
            "GET",
            "/api/v1/sellable-quantity",
            query={"symbol": symbol},
            account_required=True,
            group="account",
        )
        return _normalize_decimal_payload(payload)

    def get_commissions(self) -> Mapping[str, Any]:
        payload = self._request_json(
            "GET",
            "/api/v1/commissions",
            account_required=True,
            group="account",
        )
        return _normalize_decimal_payload(payload)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        account_required: bool = False,
        group: str = "default",
    ) -> Any:
        response = self._send(
            method,
            path,
            query=query,
            account_required=account_required,
            group=group,
        )
        if response.status == 401:
            self.issue_token()
            response = self._send(
                method,
                path,
                query=query,
                account_required=account_required,
                group=group,
            )
        self._raise_for_error(response)
        return response.payload

    def _send(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        account_required: bool,
        group: str,
    ) -> TossHttpResponse:
        if account_required:
            if self.account_seq is None:
                raise ValueError(f"{ACCOUNT_HEADER} is required for {path}")
        headers = self._auth_headers()
        if account_required:
            headers[ACCOUNT_HEADER] = self.account_seq

        response = self.transport.request(
            method,
            self._url(path),
            headers=headers,
            query=query,
        )
        self._capture_rate_limit(group, response.headers)
        return response

    def _auth_headers(self) -> dict[str, str]:
        token = self._token
        if token is None or token.is_expiring(now=self._now()):
            token = self.issue_token()
        return {"Authorization": f"{token.token_type} {token.access_token}"}

    def _capture_rate_limit(
        self,
        group: str,
        headers: Mapping[str, str] | None,
    ) -> RateLimitHeaderSnapshot:
        return self.rate_limits.update_from_headers(group, headers)

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    @staticmethod
    def _raise_for_error(response: TossHttpResponse) -> None:
        if 200 <= response.status < 300:
            return

        payload = response.payload
        code: str | None = None
        message: str | None = None
        if isinstance(payload, Mapping):
            error = payload.get("error")
            if isinstance(error, Mapping):
                code = str(error.get("code") or error.get("error") or "")
                message = str(error.get("message") or error.get("error_description") or "")
            elif isinstance(error, str):
                code = error
                message = str(payload.get("error_description") or error)
        raise TossApiError(
            response.status,
            code=code or None,
            message=message or None,
            payload=payload,
            headers=response.headers,
        )


def _with_query(url: str, query: Mapping[str, Any] | None) -> str:
    if not query:
        return url
    filtered = {
        key: _query_value(value)
        for key, value in query.items()
        if value is not None
    }
    return f"{url}?{parse.urlencode(filtered)}"


def _query_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _expect_mapping(payload: Any) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise TypeError(f"expected object response, got {type(payload)!r}")
    return payload


def _expect_sequence(payload: Any) -> list[Mapping[str, Any]]:
    if not isinstance(payload, list):
        raise TypeError(f"expected list response, got {type(payload)!r}")
    for item in payload:
        if not isinstance(item, Mapping):
            raise TypeError(f"expected object item, got {type(item)!r}")
    return payload


def _normalize_decimal_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _normalize_decimal_payload_for_key(key, item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize_decimal_payload(item) for item in value]
    return value


def _normalize_decimal_payload_for_key(key: str, value: Any) -> Any:
    if isinstance(value, Mapping):
        return _normalize_decimal_payload(value)
    if isinstance(value, list):
        return [_normalize_decimal_payload(item) for item in value]
    if key in DECIMAL_FIELD_NAMES and isinstance(value, str) and _looks_decimal(value):
        return as_decimal(value)
    return value


def _looks_decimal(value: str) -> bool:
    if value == "":
        return False
    try:
        Decimal(value)
    except Exception:
        return False
    return any(char.isdigit() for char in value)
