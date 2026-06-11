from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

import pytest

from turtle_bot.domain import Candle
from turtle_bot.toss_client import (
    ACCOUNT_HEADER,
    TossApiError,
    TossClient,
    TossCredentials,
    TossHttpResponse,
)


@dataclass
class RecordedRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    query: Mapping[str, Any] | None
    json_body: Mapping[str, Any] | None
    form_body: Mapping[str, Any] | None


class FakeTransport:
    def __init__(self, responses: list[TossHttpResponse]) -> None:
        self.responses = responses
        self.requests: list[RecordedRequest] = []

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
        self.requests.append(
            RecordedRequest(
                method=method,
                url=url,
                headers=dict(headers),
                query=dict(query) if query is not None else None,
                json_body=json_body,
                form_body=form_body,
            )
        )
        if not self.responses:
            raise AssertionError("no fake response queued")
        return self.responses.pop(0)


def _token_payload(token: str = "tok") -> dict[str, Any]:
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": 3600,
    }


def _client(transport: FakeTransport, *, account_seq: int | None = 7) -> TossClient:
    return TossClient(
        credentials=TossCredentials("id", "secret"),
        account_seq=account_seq,
        base_url="https://example.test",
        transport=transport,
        now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_issue_token_uses_client_credentials_form_body():
    transport = FakeTransport([TossHttpResponse(200, {}, _token_payload())])
    client = _client(transport)

    token = client.issue_token()

    assert token.access_token == "tok"
    request = transport.requests[0]
    assert request.method == "POST"
    assert request.url == "https://example.test/oauth2/token"
    assert request.form_body == {
        "grant_type": "client_credentials",
        "client_id": "id",
        "client_secret": "secret",
    }


def test_get_candles_normalizes_decimal_candles():
    transport = FakeTransport(
        [
            TossHttpResponse(200, {}, _token_payload()),
            TossHttpResponse(
                200,
                {"X-RateLimit-Remaining": "5"},
                {
                    "candles": [
                        {
                            "timestamp": "2026-01-01T00:00:00+00:00",
                            "openPrice": "100.1",
                            "highPrice": "110.2",
                            "lowPrice": "90.3",
                            "closePrice": "105.4",
                            "volume": "12345",
                            "currency": "KRW",
                        }
                    ],
                    "nextBefore": None,
                },
            ),
        ]
    )
    client = _client(transport)

    page = client.get_candles("005930", count=1)

    assert isinstance(page.candles[0], Candle)
    assert page.candles[0].symbol == "005930"
    assert page.candles[0].open == Decimal("100.1")
    assert transport.requests[1].query == {
        "symbol": "005930",
        "interval": "1d",
        "count": 1,
        "adjusted": True,
    }
    assert transport.requests[1].headers["Authorization"] == "Bearer tok"
    assert client.rate_limits.get_group_snapshot("market").remaining == 5


def test_account_read_methods_include_required_account_header():
    transport = FakeTransport(
        [
            TossHttpResponse(200, {}, _token_payload()),
            TossHttpResponse(200, {}, {"items": []}),
            TossHttpResponse(200, {}, {"orders": [], "nextCursor": None, "hasNext": False}),
            TossHttpResponse(200, {}, {"sellableQuantity": "10"}),
        ]
    )
    client = _client(transport, account_seq=99)

    client.get_holdings()
    client.get_orders(status="OPEN")
    sellable = client.get_sellable_quantity("005930")

    assert transport.requests[1].headers[ACCOUNT_HEADER] == "99"
    assert transport.requests[2].headers[ACCOUNT_HEADER] == "99"
    assert transport.requests[3].headers[ACCOUNT_HEADER] == "99"
    assert sellable["sellableQuantity"] == Decimal("10")


def test_account_numbers_stay_strings_while_money_fields_become_decimal():
    transport = FakeTransport(
        [
            TossHttpResponse(200, {}, _token_payload()),
            TossHttpResponse(
                200,
                {},
                {
                    "accounts": [
                        {
                            "accountNo": "12345678901",
                            "accountSeq": 99,
                            "accountType": "BROKERAGE",
                        }
                    ]
                },
            ),
            TossHttpResponse(
                200,
                {},
                {
                    "currency": "KRW",
                    "cashBuyingPower": "5000000",
                },
            ),
        ]
    )
    client = _client(transport, account_seq=99)

    accounts = client.get_accounts()
    buying_power = client.get_buying_power("KRW")

    assert accounts["accounts"][0]["accountNo"] == "12345678901"
    assert accounts["accounts"][0]["accountSeq"] == 99
    assert buying_power["cashBuyingPower"] == Decimal("5000000")


def test_market_info_methods_use_official_read_only_paths():
    transport = FakeTransport(
        [
            TossHttpResponse(200, {}, _token_payload()),
            TossHttpResponse(200, {}, {"trades": []}),
            TossHttpResponse(200, {}, {"upperLimitPrice": "93000", "lowerLimitPrice": "50400"}),
            TossHttpResponse(200, {}, {"isOpen": True}),
            TossHttpResponse(200, {}, {"rate": "1390.5"}),
            TossHttpResponse(200, {}, {"stocks": []}),
            TossHttpResponse(200, {}, {"warnings": []}),
        ]
    )
    client = _client(transport)

    client.get_trades("005930", count=10)
    limits = client.get_price_limits("005930")
    client.get_market_calendar("KR", date="2026-01-02")
    exchange_rate = client.get_exchange_rate(base_currency="USD", quote_currency="KRW")
    client.get_stocks(("005930", "AAPL"))
    client.get_stock_warnings("005930")

    assert transport.requests[1].url == "https://example.test/api/v1/trades"
    assert transport.requests[1].query == {"symbol": "005930", "count": 10}
    assert limits["upperLimitPrice"] == Decimal("93000")
    assert transport.requests[3].url == "https://example.test/api/v1/market-calendar/KR"
    assert transport.requests[4].query == {
        "baseCurrency": "USD",
        "quoteCurrency": "KRW",
    }
    assert exchange_rate["rate"] == Decimal("1390.5")
    assert transport.requests[5].query == {"symbols": "005930,AAPL"}
    assert transport.requests[6].url == "https://example.test/api/v1/stocks/005930/warnings"


def test_account_header_is_required_for_account_methods():
    transport = FakeTransport([])
    client = _client(transport, account_seq=None)

    with pytest.raises(ValueError, match=ACCOUNT_HEADER):
        client.get_holdings()
    assert transport.requests == []


def test_401_refreshes_token_exactly_once_before_retry():
    transport = FakeTransport(
        [
            TossHttpResponse(200, {}, _token_payload("old")),
            TossHttpResponse(401, {}, {"error": {"code": "unauthorized", "message": "expired"}}),
            TossHttpResponse(200, {}, _token_payload("new")),
            TossHttpResponse(200, {}, {"accounts": []}),
        ]
    )
    client = _client(transport)

    client.get_accounts()

    assert [request.method for request in transport.requests] == ["POST", "GET", "POST", "GET"]
    assert transport.requests[1].headers["Authorization"] == "Bearer old"
    assert transport.requests[3].headers["Authorization"] == "Bearer new"


def test_409_and_422_raise_without_retry():
    for status in (409, 422):
        transport = FakeTransport(
            [
                TossHttpResponse(200, {}, _token_payload()),
                TossHttpResponse(
                    status,
                    {},
                    {"error": {"code": "broker-rule", "message": "blocked"}},
                ),
            ]
        )
        client = _client(transport)

        with pytest.raises(TossApiError) as exc:
            client.get_orders(status="OPEN")

        assert exc.value.status == status
        assert exc.value.code == "broker-rule"
        assert len(transport.requests) == 2


def test_429_captures_retry_after_and_raises():
    transport = FakeTransport(
        [
            TossHttpResponse(200, {}, _token_payload()),
            TossHttpResponse(
                429,
                {"Retry-After": "30"},
                {"error": {"code": "rate-limited", "message": "slow down"}},
            ),
        ]
    )
    client = _client(transport)

    with pytest.raises(TossApiError) as exc:
        client.get_prices(["005930"])

    assert exc.value.status == 429
    assert client.rate_limits.seconds_until_resumed("market") == 30


def test_unknown_enum_values_are_preserved_as_strings():
    transport = FakeTransport(
        [
            TossHttpResponse(200, {}, _token_payload()),
            TossHttpResponse(
                200,
                {},
                {
                    "orders": [
                        {
                            "orderId": "abc",
                            "symbol": "005930",
                            "side": "BUY",
                            "orderType": "NEW_KIND",
                            "timeInForce": "NEW_TIF",
                            "status": "FUTURE_STATUS",
                            "quantity": "10",
                            "currency": "KRW",
                            "orderedAt": "2026-01-01T09:00:00+09:00",
                            "execution": {
                                "filledQuantity": "0",
                                "averageFilledPrice": None,
                                "filledAmount": None,
                                "commission": None,
                                "tax": None,
                                "filledAt": None,
                                "settlementDate": None,
                            },
                        }
                    ],
                    "nextCursor": None,
                    "hasNext": False,
                },
            ),
        ]
    )
    client = _client(transport)

    payload = client.get_orders(status="OPEN")

    order = payload["orders"][0]
    assert order["orderType"] == "NEW_KIND"
    assert order["timeInForce"] == "NEW_TIF"
    assert order["status"] == "FUTURE_STATUS"
    assert order["quantity"] == Decimal("10")
    assert order["orderId"] == "abc"


def test_client_exposes_no_order_mutation_methods():
    client = _client(FakeTransport([]))

    assert not hasattr(client, "create_order")
    assert not hasattr(client, "modify_order")
    assert not hasattr(client, "cancel_order")
