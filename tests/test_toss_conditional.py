from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from http.client import IncompleteRead
import json
from typing import Any, Mapping

import pytest

from turtle_bot.toss_client import ACCOUNT_HEADER, TossApiError, TossClient, TossCredentials, TossHttpResponse
from turtle_bot.toss_conditional import (
    CONDITIONAL_ORDER_GROUP,
    CONDITIONAL_ORDER_HISTORY_GROUP,
    ConditionalOrderUnknownStateError,
    TossConditionalOrderAdapter,
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
    def __init__(self, responses: list[TossHttpResponse | BaseException]) -> None:
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
                method,
                url,
                dict(headers),
                dict(query) if query is not None else None,
                dict(json_body) if json_body is not None else None,
                dict(form_body) if form_body is not None else None,
            )
        )
        if not self.responses:
            raise AssertionError("no fake response queued")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _token() -> TossHttpResponse:
    return TossHttpResponse(
        200,
        {},
        {"access_token": "tok", "token_type": "Bearer", "expires_in": 3600},
    )


def _client(transport: FakeTransport, *, account_seq: int | None = 7) -> TossClient:
    return TossClient(
        credentials=TossCredentials("id", "secret"),
        account_seq=account_seq,
        base_url="https://example.test",
        transport=transport,
        now=lambda: datetime(2026, 8, 28, tzinfo=timezone.utc),
    )


def _single_payload(*, order_type: str = "LIMIT") -> dict[str, Any]:
    first: dict[str, Any] = {
        "orderSide": "SELL",
        "triggerPrice": Decimal("105.00"),
    }
    if order_type == "LIMIT":
        first["orderPrice"] = Decimal("104.90")
    return {
        "symbol": "AAPL",
        "type": "SINGLE",
        "quantity": Decimal("2.00"),
        "orderType": order_type,
        "expireDate": date(2026, 8, 29),
        "first": first,
    }


def _oco_payload() -> dict[str, Any]:
    return {
        "symbol": "AAPL",
        "type": "OCO",
        "quantity": Decimal("2.00"),
        "orderType": "LIMIT",
        "clientOrderId": "intraday-20260828-AAPL",
        "expireDate": "2026-08-29",
        "first": {
            "orderSide": "SELL",
            "triggerPrice": Decimal("105.50"),
            "orderPrice": Decimal("105.50"),
        },
        "second": {
            "orderSide": "SELL",
            "triggerPrice": Decimal("95.00"),
            "orderPrice": Decimal("94.90"),
        },
        "confirmHighValueOrder": False,
    }


def test_create_oco_validates_relation_and_sends_normalized_payload() -> None:
    transport = FakeTransport(
        [
            _token(),
            TossHttpResponse(
                200,
                {"X-RateLimit-Limit": "5", "X-RateLimit-Remaining": "4"},
                {
                    "result": {
                        "conditionalOrderId": "condition-1",
                        "clientOrderId": "intraday-20260828-AAPL",
                    }
                },
            ),
        ]
    )
    client = _client(transport, account_seq=99)

    result = TossConditionalOrderAdapter(client).create(
        _oco_payload(), current_price=Decimal("100")
    )

    assert result["conditionalOrderId"] == "condition-1"
    request = transport.requests[1]
    assert request.method == "POST"
    assert request.url == "https://example.test/api/v1/conditional-orders"
    assert request.headers[ACCOUNT_HEADER] == "99"
    assert request.json_body == {
        "symbol": "AAPL",
        "type": "OCO",
        "quantity": "2",
        "orderType": "LIMIT",
        "expireDate": "2026-08-29",
        "first": {
            "orderSide": "SELL",
            "triggerPrice": "105.5",
            "orderPrice": "105.5",
        },
        "second": {
            "orderSide": "SELL",
            "triggerPrice": "95",
            "orderPrice": "94.9",
        },
        "clientOrderId": "intraday-20260828-AAPL",
        "confirmHighValueOrder": False,
    }
    snapshot = client.rate_limits.get_group_snapshot(CONDITIONAL_ORDER_GROUP)
    assert (snapshot.limit, snapshot.remaining) == (5, 4)


def test_create_supports_exact_single_market_and_oto_contracts() -> None:
    transport = FakeTransport(
        [
            _token(),
            TossHttpResponse(200, {}, {"result": {"conditionalOrderId": "single-1"}}),
            TossHttpResponse(200, {}, {"result": {"conditionalOrderId": "oto-1"}}),
        ]
    )
    adapter = TossConditionalOrderAdapter(_client(transport))

    adapter.create(_single_payload(order_type="MARKET"))
    adapter.create(
        {
            "symbol": "AAPL",
            "type": "OTO",
            "quantity": "1",
            "orderType": "LIMIT",
            "expireDate": "2026-08-29",
            "first": {
                "orderSide": "BUY",
                "triggerPrice": "99",
                "orderPrice": "99",
            },
            "second": {
                "orderSide": "SELL",
                "triggerPrice": "105",
                "orderPrice": "105",
            },
        }
    )

    assert "orderPrice" not in transport.requests[1].json_body["first"]
    assert transport.requests[2].json_body["second"]["orderSide"] == "SELL"


def test_payload_validation_rejects_invalid_type_specific_shapes_before_network() -> None:
    adapter = TossConditionalOrderAdapter(_client(FakeTransport([])))

    single_with_second = _single_payload()
    single_with_second["second"] = dict(single_with_second["first"])
    with pytest.raises(ValueError, match="must not include second"):
        adapter.create(single_with_second)

    market_with_price = _single_payload(order_type="MARKET")
    market_with_price["first"]["orderPrice"] = "105"
    with pytest.raises(ValueError, match="not allowed for MARKET"):
        adapter.create(market_with_price)

    oco_market = _oco_payload()
    oco_market["orderType"] = "MARKET"
    with pytest.raises(ValueError, match="requires orderType LIMIT"):
        adapter.create(oco_market, current_price="100")

    oco_wrong_side = _oco_payload()
    oco_wrong_side["first"]["orderSide"] = "BUY"
    with pytest.raises(ValueError, match="first SELL and second SELL"):
        adapter.create(oco_wrong_side, current_price="100")

    with pytest.raises(ValueError, match="current_price"):
        adapter.create(_oco_payload())

    with pytest.raises(ValueError, match="first.triggerPrice > current_price"):
        adapter.create(_oco_payload(), current_price="106")

    oto_wrong_side = _oco_payload()
    oto_wrong_side["type"] = "OTO"
    with pytest.raises(ValueError, match="first BUY and second SELL"):
        adapter.create(oto_wrong_side)


def test_payload_validation_rejects_unknown_fields_and_invalid_client_order_id() -> None:
    adapter = TossConditionalOrderAdapter(_client(FakeTransport([])))

    unknown = _single_payload()
    unknown["futureField"] = True
    with pytest.raises(ValueError, match="unsupported fields"):
        adapter.create(unknown)

    too_long = _single_payload()
    too_long["clientOrderId"] = "x" * 37
    with pytest.raises(ValueError, match="at most 36"):
        adapter.create(too_long)

    invalid_chars = _single_payload()
    invalid_chars["clientOrderId"] = "not allowed"
    with pytest.raises(ValueError, match="only letters"):
        adapter.create(invalid_chars)


def test_list_and_get_use_history_group_and_normalize_condition_decimals() -> None:
    transport = FakeTransport(
        [
            _token(),
            TossHttpResponse(
                200,
                {"X-RateLimit-Limit": "10", "X-RateLimit-Remaining": "9"},
                {
                    "result": {
                        "conditionalOrders": [
                            {
                                "conditionalOrderId": "condition-1",
                                "quantity": "2",
                                "first": {
                                    "triggerPrice": "105.5",
                                    "orderPrice": "105.5",
                                    "targetProfitRate": None,
                                },
                            }
                        ],
                        "nextCursor": None,
                        "hasNext": False,
                    }
                },
            ),
            TossHttpResponse(
                200,
                {},
                {
                    "result": {
                        "conditionalOrderId": "condition-1",
                        "quantity": "2",
                        "second": {
                            "triggerPrice": "95.0",
                            "orderPrice": "94.9",
                        },
                    }
                },
            ),
        ]
    )
    client = _client(transport, account_seq=42)
    adapter = TossConditionalOrderAdapter(client)

    page = adapter.list(status="OPEN", symbol="AAPL", cursor="next_1", limit=50)
    detail = adapter.get("condition/1")

    item = page["conditionalOrders"][0]
    assert item["quantity"] == Decimal("2")
    assert item["first"]["triggerPrice"] == Decimal("105.5")
    assert item["first"]["orderPrice"] == Decimal("105.5")
    assert detail["second"]["triggerPrice"] == Decimal("95.0")
    assert detail["second"]["orderPrice"] == Decimal("94.9")
    assert transport.requests[1].query == {
        "status": "OPEN",
        "limit": 50,
        "symbol": "AAPL",
        "cursor": "next_1",
    }
    assert transport.requests[2].url.endswith("/api/v1/conditional-orders/condition%2F1")
    assert all(request.headers[ACCOUNT_HEADER] == "42" for request in transport.requests[1:])
    history = client.rate_limits.get_group_snapshot(CONDITIONAL_ORDER_HISTORY_GROUP)
    mutations = client.rate_limits.get_group_snapshot(CONDITIONAL_ORDER_GROUP)
    assert (history.limit, history.remaining) == (10, 9)
    assert mutations.limit is None


def test_modify_sends_full_contract_and_returns_replacement_id() -> None:
    transport = FakeTransport(
        [
            _token(),
            TossHttpResponse(200, {}, {"result": {"conditionalOrderId": "replacement-1"}}),
        ]
    )
    adapter = TossConditionalOrderAdapter(_client(transport))
    payload = _oco_payload()
    payload.pop("symbol")
    payload.pop("clientOrderId")

    result = adapter.modify("condition/old", payload, current_price="100")

    assert result["conditionalOrderId"] == "replacement-1"
    assert transport.requests[1].url.endswith(
        "/api/v1/conditional-orders/condition%2Fold/modify"
    )
    assert "symbol" not in transport.requests[1].json_body
    assert "clientOrderId" not in transport.requests[1].json_body


def test_delete_requires_exact_204_and_sends_no_body() -> None:
    transport = FakeTransport([_token(), TossHttpResponse(204, {}, None)])
    adapter = TossConditionalOrderAdapter(_client(transport))

    assert adapter.delete("condition-1") is None
    assert transport.requests[1].method == "DELETE"
    assert transport.requests[1].json_body is None


@pytest.mark.parametrize("status", [409, 429, 500, 503])
def test_mutation_ambiguous_http_status_is_unknown_and_never_retried(status: int) -> None:
    transport = FakeTransport(
        [
            _token(),
            TossHttpResponse(
                status,
                {"Retry-After": "30"} if status == 429 else {},
                {"error": {"code": "ambiguous", "message": "unknown outcome"}},
            ),
            TossHttpResponse(200, {}, {"result": {"conditionalOrderId": "must-not-run"}}),
        ]
    )
    adapter = TossConditionalOrderAdapter(_client(transport))

    with pytest.raises(ConditionalOrderUnknownStateError) as exc:
        adapter.create(_single_payload())

    assert exc.value.unknown_state is True
    assert exc.value.status == status
    assert len(transport.requests) == 2


def test_mutation_network_failure_is_unknown_and_never_retried() -> None:
    transport = FakeTransport([_token(), TimeoutError("socket timed out")])
    adapter = TossConditionalOrderAdapter(_client(transport))

    with pytest.raises(ConditionalOrderUnknownStateError, match="socket timed out"):
        adapter.create(_single_payload())

    assert len(transport.requests) == 2


@pytest.mark.parametrize(
    "failure",
    [
        json.JSONDecodeError("malformed response", "{", 1),
        IncompleteRead(b'{"result":', 10),
    ],
)
def test_mutation_malformed_or_truncated_response_is_unknown_and_never_retried(
    failure: Exception,
) -> None:
    transport = FakeTransport([_token(), failure])
    adapter = TossConditionalOrderAdapter(_client(transport))

    with pytest.raises(ConditionalOrderUnknownStateError):
        adapter.create(_single_payload())

    assert len(transport.requests) == 2


def test_mutation_does_not_retry_401() -> None:
    transport = FakeTransport(
        [
            _token(),
            TossHttpResponse(
                401,
                {},
                {"error": {"code": "unauthorized", "message": "expired token"}},
            ),
            TossHttpResponse(200, {}, {"result": {"conditionalOrderId": "must-not-run"}}),
        ]
    )
    adapter = TossConditionalOrderAdapter(_client(transport))

    with pytest.raises(TossApiError) as exc:
        adapter.create(_single_payload())

    assert exc.value.status == 401
    assert len(transport.requests) == 2


def test_ambiguous_success_shapes_are_unknown() -> None:
    create_transport = FakeTransport([_token(), TossHttpResponse(200, {}, {"result": {}})])
    with pytest.raises(ConditionalOrderUnknownStateError, match="conditionalOrderId"):
        TossConditionalOrderAdapter(_client(create_transport)).create(_single_payload())

    delete_transport = FakeTransport(
        [_token(), TossHttpResponse(200, {}, {"result": {"conditionalOrderId": "condition-1"}})]
    )
    with pytest.raises(ConditionalOrderUnknownStateError, match="expected HTTP 204"):
        TossConditionalOrderAdapter(_client(delete_transport)).delete("condition-1")


def test_known_client_error_remains_a_toss_api_error() -> None:
    transport = FakeTransport(
        [
            _token(),
            TossHttpResponse(
                422,
                {},
                {"error": {"code": "condition-already-met", "message": "already met"}},
            ),
        ]
    )

    with pytest.raises(TossApiError) as exc:
        TossConditionalOrderAdapter(_client(transport)).create(_single_payload())

    assert exc.value.status == 422
    assert exc.value.code == "condition-already-met"
