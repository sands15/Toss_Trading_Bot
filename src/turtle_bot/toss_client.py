from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import re
from typing import Any, Mapping, Protocol
from urllib import parse, request
from urllib.error import HTTPError

from .domain import Candle, as_decimal
from .rate_limit import RateLimitHeaderSnapshot, RateLimitQueue


TOSS_BASE_URL = "https://openapi.tossinvest.com"
ACCOUNT_HEADER = "X-Tossinvest-Account"
_SHADOW_SYMBOL = re.compile(
    r"(?=.{1,16}\Z)[A-Z][A-Z0-9]*(?:[.-][A-Z0-9]+)?\Z"
)
_SHADOW_CURSOR = re.compile(r"[A-Za-z0-9_=-]{1,256}\Z")
_SHADOW_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_SHADOW_GET_QUERIES = {
    "/api/v1/market-calendar/US": (frozenset({"date"}), frozenset()),
    "/api/v1/rankings": (
        frozenset(
            {
                "type",
                "marketCountry",
                "duration",
                "excludeInvestmentCaution",
                "count",
            }
        ),
        frozenset(),
    ),
    "/api/v1/stocks": (frozenset({"symbols"}), frozenset()),
    "/api/v1/stocks/all": (
        frozenset({"market", "status", "securityType", "commonShare"}),
        frozenset(),
    ),
    "/api/v1/candles": (
        frozenset({"symbol", "interval", "count", "adjusted"}),
        frozenset({"before"}),
    ),
    "/api/v1/prices": (frozenset({"symbols"}), frozenset()),
    "/api/v1/orderbook": (frozenset({"symbol"}), frozenset()),
    "/api/v1/accounts": (frozenset(), frozenset()),
    "/api/v1/holdings": (frozenset(), frozenset({"symbol"})),
    "/api/v1/orders": (
        frozenset({"status", "limit"}),
        frozenset({"symbol", "from", "to", "cursor"}),
    ),
    "/api/v1/conditional-orders": (
        frozenset({"status", "limit"}),
        frozenset({"symbol", "cursor"}),
    ),
    "/api/v1/buying-power": (frozenset({"currency"}), frozenset()),
    "/api/v1/commissions": (frozenset(), frozenset()),
}
DECIMAL_FIELD_NAMES = frozenset(
    {
        "averageFilledPrice",
        "averagePurchasePrice",
        "basisPoint",
        "cashBuyingPower",
        "changeRate",
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
        "basePrice",
        "midRate",
        "sellableQuantity",
        "tax",
        "totalPurchaseAmount",
        "tradingAmount",
        "tradingVolume",
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


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class UrllibTossTransport:
    def __init__(self, *, opener: Any | None = None) -> None:
        self._opener = opener or request.build_opener(_NoRedirect())

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
            with self._opener.open(req, timeout=15) as response:
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


class ShadowTransportViolation(RuntimeError):
    """A shadow request crossed its fixed read-only Toss boundary."""


class ShadowReadOnlyTossTransport:
    """Fail closed before dispatching anything outside the shadow contract."""

    def __init__(self, delegate: TossTransport | None = None) -> None:
        self.delegate = delegate or UrllibTossTransport()

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
        clean_method = str(method).upper()
        path = _shadow_path(url)
        if clean_method == "POST" and path == "/oauth2/token":
            _validate_shadow_oauth(query, json_body, form_body)
        elif clean_method == "GET":
            _validate_shadow_get(path, query, json_body, form_body)
        else:
            raise ShadowTransportViolation(
                f"shadow transport rejected {clean_method} {path}"
            )

        response = self.delegate.request(
            clean_method,
            url,
            headers=headers,
            query=query,
            json_body=json_body,
            form_body=form_body,
        )
        if 300 <= response.status < 400:
            raise ShadowTransportViolation(
                f"shadow transport rejected HTTP redirect {response.status}"
            )
        return response


class SimulationReadOnlyTossTransport(ShadowReadOnlyTossTransport):
    """Read-only forward-test boundary with personal trading state blocked.

    The commission schedule remains available because Toss exposes it behind
    the account header. Cash, holdings, order history, and conditional orders
    are deliberately unavailable: simulation sizing must use its own ledger.
    """

    _BLOCKED_PATHS = frozenset(
        {
            "/api/v1/accounts",
            "/api/v1/holdings",
            "/api/v1/orders",
            "/api/v1/conditional-orders",
            "/api/v1/buying-power",
            "/api/v1/sellable-quantity",
        }
    )

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
        path = _shadow_path(url)
        if str(method).upper() == "GET":
            if path in self._BLOCKED_PATHS:
                raise ShadowTransportViolation(
                    f"simulation transport rejected personal account read {path}"
                )
            has_account_header = bool(str(headers.get(ACCOUNT_HEADER) or "").strip())
            if path == "/api/v1/commissions":
                if not has_account_header:
                    raise ShadowTransportViolation(
                        "simulation commission read requires its account header"
                    )
            elif has_account_header:
                raise ShadowTransportViolation(
                    "simulation public market read rejected an account header"
                )
        return super().request(
            method,
            url,
            headers=headers,
            query=query,
            json_body=json_body,
            form_body=form_body,
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

        payload = self._request_json(
            "GET",
            "/api/v1/candles",
            query=query,
            group="market_data_chart",
        )
        raw = _expect_mapping(payload)
        candles = tuple(
            Candle.from_api({"symbol": symbol, **item, "adjusted": adjusted})
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
            group="market_data",
        )
        return _normalize_decimal_payload(payload)

    def get_orderbook(self, symbol: str) -> Mapping[str, Any]:
        payload = self._request_json(
            "GET",
            "/api/v1/orderbook",
            query={"symbol": symbol},
            group="market_data",
        )
        return _normalize_decimal_payload(payload)

    def get_trades(self, symbol: str, *, count: int = 50) -> Mapping[str, Any]:
        payload = self._request_json(
            "GET",
            "/api/v1/trades",
            query={"symbol": symbol, "count": count},
            group="market_data",
        )
        return _normalize_decimal_payload(payload)

    def get_price_limits(self, symbol: str) -> Mapping[str, Any]:
        payload = self._request_json(
            "GET",
            "/api/v1/price-limits",
            query={"symbol": symbol},
            group="market_data",
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

    def get_rankings(
        self,
        *,
        ranking_type: str,
        market_country: str,
        duration: str,
        exclude_investment_caution: bool = True,
        count: int = 20,
    ) -> Mapping[str, Any]:
        ranking = str(ranking_type).strip().upper()
        market = str(market_country).strip().upper()
        period = str(duration).strip().lower()
        if ranking not in {
            "MARKET_TRADING_AMOUNT",
            "MARKET_TRADING_VOLUME",
            "TOP_GAINERS",
            "TOP_LOSERS",
            "TOSS_SECURITIES_TRADING_AMOUNT",
            "TOSS_SECURITIES_TRADING_VOLUME",
        }:
            raise ValueError("unsupported ranking type")
        if market not in {"KR", "US"}:
            raise ValueError("market_country must be KR or US")
        if period not in {"realtime", "1d", "1w", "1mo", "3mo", "6mo", "1y"}:
            raise ValueError("unsupported ranking duration")
        if ranking in {"TOP_GAINERS", "TOP_LOSERS"} and period == "realtime":
            raise ValueError("TOP_GAINERS and TOP_LOSERS do not support realtime")
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 100:
            raise ValueError("ranking count must be between 1 and 100")
        payload = self._request_json(
            "GET",
            "/api/v1/rankings",
            query={
                "type": ranking,
                "marketCountry": market,
                "duration": period,
                "excludeInvestmentCaution": bool(exclude_investment_caution),
                "count": count,
            },
            group="ranking",
        )
        return _normalize_decimal_payload(payload)

    def get_stocks(self, symbols: list[str] | tuple[str, ...]) -> Mapping[str, Any]:
        payload = self._request_json(
            "GET",
            "/api/v1/stocks",
            query={"symbols": ",".join(symbols)},
            group="stock",
        )
        return _normalize_decimal_payload(payload)

    def list_stocks(
        self,
        market: str,
        *,
        status: str = "ACTIVE",
        security_type: str = "STOCK",
        common_share: bool = True,
    ) -> list[Mapping[str, Any]]:
        clean_market = str(market).strip().upper()
        clean_status = str(status).strip().upper()
        clean_security_type = str(security_type).strip().upper()
        if clean_market not in {
            "KOSPI",
            "KOSDAQ",
            "NYSE",
            "NASDAQ",
            "AMEX",
            "KR_ETC",
            "US_ETC",
        }:
            raise ValueError("unsupported stock market")
        if clean_status not in {"SCHEDULED", "ACTIVE", "DELISTED"}:
            raise ValueError("unsupported stock status")
        if clean_security_type not in {
            "STOCK",
            "FOREIGN_STOCK",
            "DEPOSITARY_RECEIPT",
            "INFRASTRUCTURE_FUND",
            "REIT",
            "ETF",
            "FOREIGN_ETF",
            "ETN",
            "STOCK_WARRANTS",
        }:
            raise ValueError("unsupported security type")
        payload = self._request_json(
            "GET",
            "/api/v1/stocks/all",
            query={
                "market": clean_market,
                "status": clean_status,
                "securityType": clean_security_type,
                "commonShare": bool(common_share),
            },
            group="stock_all",
        )
        if not isinstance(payload, list) or any(
            not isinstance(item, Mapping) for item in payload
        ):
            raise TypeError("stock universe response must be an array of objects")
        return list(payload)

    def get_stock_warnings(self, symbol: str) -> Mapping[str, Any]:
        payload = self._request_json(
            "GET",
            f"/api/v1/stocks/{parse.quote(symbol, safe='')}/warnings",
            group="stock",
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
        if self.rate_limits.is_group_paused(group):
            wait_seconds = int(self.rate_limits.seconds_until_resumed(group))
            raise TossApiError(
                429,
                code="rate-limit-paused",
                message=(
                    f"{group} 요청 제한 대기 중입니다."
                    + (f" 약 {wait_seconds}초 뒤 다시 시도하세요." if wait_seconds else "")
                ),
                payload={
                    "error": {
                        "code": "rate-limit-paused",
                        "message": "request group is paused by local rate-limit guard",
                        "group": group,
                        "retryAfterSeconds": wait_seconds,
                    }
                },
            )
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
        if response.status == 429:
            retry_after = _retry_after_seconds(response.headers) or 60.0
            self.rate_limits.pause_group(group, seconds=retry_after)
        self._raise_for_error(response)
        return _normalize_decimal_payload(_unwrap_result(response.payload))

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


def _shadow_path(url: str) -> str:
    try:
        parsed = parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ShadowTransportViolation("shadow transport rejected malformed URL") from exc
    if (
        parsed.scheme != "https"
        or parsed.netloc not in {"openapi.tossinvest.com", "openapi.tossinvest.com:443"}
        or parsed.hostname != "openapi.tossinvest.com"
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or "\\" in parsed.path
        or "%" in parsed.path
        or "//" in parsed.path
        or "/./" in parsed.path
        or "/../" in parsed.path
    ):
        raise ShadowTransportViolation("shadow transport rejected non-canonical Toss URL")
    return parsed.path


def _validate_shadow_oauth(
    query: Mapping[str, Any] | None,
    json_body: Mapping[str, Any] | None,
    form_body: Mapping[str, Any] | None,
) -> None:
    if query or json_body is not None or not isinstance(form_body, Mapping):
        raise ShadowTransportViolation("shadow OAuth request shape is invalid")
    if set(form_body) != {"grant_type", "client_id", "client_secret"}:
        raise ShadowTransportViolation("shadow OAuth form keys are invalid")
    if form_body.get("grant_type") != "client_credentials":
        raise ShadowTransportViolation("shadow OAuth grant type is invalid")
    if not all(
        isinstance(form_body.get(key), str) and bool(form_body.get(key))
        for key in ("client_id", "client_secret")
    ):
        raise ShadowTransportViolation("shadow OAuth credentials are invalid")


def _validate_shadow_get(
    path: str,
    query: Mapping[str, Any] | None,
    json_body: Mapping[str, Any] | None,
    form_body: Mapping[str, Any] | None,
) -> None:
    if json_body is not None or form_body is not None:
        raise ShadowTransportViolation("shadow GET request must not have a body")
    schema = _SHADOW_GET_QUERIES.get(path)
    if schema is None:
        match = re.fullmatch(
            r"/api/v1/stocks/([A-Z][A-Z0-9]*(?:[.-][A-Z0-9]+)?)/warnings",
            path,
        )
        if match is None or _SHADOW_SYMBOL.fullmatch(match.group(1)) is None:
            raise ShadowTransportViolation(f"shadow GET path is not allowed: {path}")
        schema = (frozenset(), frozenset())
    values = dict(query or {})
    required, optional = schema
    if not all(isinstance(key, str) for key in values):
        raise ShadowTransportViolation("shadow GET query keys must be strings")
    keys = frozenset(values)
    if not required <= keys or keys - required - optional:
        raise ShadowTransportViolation(f"shadow GET query is invalid for {path}")
    if any(
        value is None
        or isinstance(value, (bytes, bytearray, Mapping, list, tuple, set))
        or any(ord(char) < 32 for char in str(value))
        for value in values.values()
    ):
        raise ShadowTransportViolation("shadow GET query value is invalid")
    _validate_shadow_query_values(path, values)


def _validate_shadow_query_values(path: str, query: Mapping[str, Any]) -> None:
    for key in ("symbol",):
        if key in query and _SHADOW_SYMBOL.fullmatch(str(query[key])) is None:
            raise ShadowTransportViolation(f"shadow query {key} is invalid")
    if "symbols" in query:
        symbols = str(query["symbols"]).split(",")
        if (
            not symbols
            or len(symbols) != len(set(symbols))
            or any(_SHADOW_SYMBOL.fullmatch(symbol) is None for symbol in symbols)
        ):
            raise ShadowTransportViolation("shadow query symbols is invalid")
    for key in ("date", "from", "to"):
        if key in query and _SHADOW_DATE.fullmatch(str(query[key])) is None:
            raise ShadowTransportViolation(f"shadow query {key} is invalid")
    if "cursor" in query and _SHADOW_CURSOR.fullmatch(str(query["cursor"])) is None:
        raise ShadowTransportViolation("shadow query cursor is invalid")
    for key in ("count", "limit"):
        if key in query and (
            isinstance(query[key], bool)
            or not isinstance(query[key], int)
            or not 1 <= query[key] <= (200 if path == "/api/v1/candles" else 100)
        ):
            raise ShadowTransportViolation(f"shadow query {key} is invalid")
    if path == "/api/v1/market-calendar/US" and not query:
        raise ShadowTransportViolation("shadow calendar date is required")
    if path == "/api/v1/rankings" and (
        query.get("marketCountry") != "US"
        or query.get("duration") != "realtime"
        or not isinstance(query.get("excludeInvestmentCaution"), bool)
    ):
        raise ShadowTransportViolation("shadow ranking query is invalid")
    if path == "/api/v1/orders" and query.get("status") not in {"OPEN", "CLOSED"}:
        raise ShadowTransportViolation("shadow order status is invalid")
    if path == "/api/v1/conditional-orders" and query.get("status") not in {
        "OPEN",
        "CLOSED",
    }:
        raise ShadowTransportViolation("shadow conditional-order status is invalid")
    if path == "/api/v1/buying-power" and query.get("currency") != "USD":
        raise ShadowTransportViolation("shadow buying-power currency is invalid")


def _retry_after_seconds(headers: Mapping[str, str] | None) -> float | None:
    if not headers:
        return None
    normalized = {str(key).lower(): value for key, value in headers.items()}
    raw = normalized.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(str(raw).strip()))
    except ValueError:
        return None


def _query_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _expect_mapping(payload: Any) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise TypeError(f"expected object response, got {type(payload)!r}")
    return payload


def _unwrap_result(payload: Any) -> Any:
    if isinstance(payload, Mapping) and "result" in payload:
        return payload["result"]
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
