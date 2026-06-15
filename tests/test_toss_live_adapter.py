from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

import pytest

from turtle_bot.domain import Side
from turtle_bot.live_execution import LiveBrokerError, LiveOrderOrchestrator
from turtle_bot.live_order import ExecutionStatus, OrderIntent, OrderType, TimeInForce
from turtle_bot.live_safety import PreTradeSafety, PreTradeSafetyConfig, PreTradeSafetyContext
from turtle_bot.state_store import SQLiteStateStore
from turtle_bot.toss_client import ACCOUNT_HEADER, TossClient, TossCredentials, TossHttpResponse
from turtle_bot.toss_live_adapter import ModifyOrderRequest, TossLiveBrokerAdapter


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
                json_body=dict(json_body) if json_body is not None else None,
                form_body=dict(form_body) if form_body is not None else None,
            )
        )
        if not self.responses:
            raise AssertionError("no fake response queued")
        return self.responses.pop(0)


def _token_payload() -> dict[str, Any]:
    return {
        "access_token": "tok",
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


def test_toss_live_adapter_places_limit_order_with_safe_client_order_id() -> None:
    transport = FakeTransport(
        [
            TossHttpResponse(200, {}, _token_payload()),
            TossHttpResponse(
                200,
                {},
                {"result": {"orderId": "order-1", "clientOrderId": "client-1"}},
            ),
        ]
    )
    adapter = TossLiveBrokerAdapter(_client(transport, account_seq=99))
    intent = OrderIntent(
        intent_id="intent-1",
        idempotency_key="very-long-client-order-id-that-exceeds-thirty-six-characters",
        symbol="005930",
        side=Side.BUY,
        quantity=Decimal("10"),
        order_type=OrderType.LIMIT,
        limit_price=Decimal("70000"),
        source="test",
        reason="live_adapter_contract",
    )

    ticket = adapter.place_order(intent)

    assert ticket.broker_order_id == "order-1"
    assert ticket.status == ExecutionStatus.ACKNOWLEDGED
    request = transport.requests[1]
    assert request.method == "POST"
    assert request.url == "https://example.test/api/v1/orders"
    assert request.headers[ACCOUNT_HEADER] == "99"
    assert request.json_body is not None
    assert request.json_body["symbol"] == "005930"
    assert request.json_body["side"] == "BUY"
    assert request.json_body["orderType"] == "LIMIT"
    assert request.json_body["quantity"] == "10"
    assert request.json_body["price"] == "70000"
    assert len(str(request.json_body["clientOrderId"])) <= 36


def test_toss_live_adapter_supports_modify_cancel_and_query_mapping() -> None:
    transport = FakeTransport(
        [
            TossHttpResponse(200, {}, _token_payload()),
            TossHttpResponse(200, {}, {"result": {"orderId": "replace-1"}}),
            TossHttpResponse(200, {}, {"result": {"orderId": "cancel-1"}}),
            TossHttpResponse(
                200,
                {},
                {
                    "result": {
                        "orderId": "cancel-1",
                        "status": "CANCELED",
                        "quantity": "10",
                        "execution": {
                            "filledQuantity": "3",
                            "averageFilledPrice": "70100",
                        },
                    }
                },
            ),
        ]
    )
    adapter = TossLiveBrokerAdapter(_client(transport, account_seq=99))

    modified = adapter.modify_order(
        "order-1",
        ModifyOrderRequest(
            order_type=OrderType.LIMIT,
            quantity=Decimal("10"),
            price=Decimal("70100"),
        ),
    )
    cancelled = adapter.cancel_order("replace-1")
    state = adapter.query_order("cancel-1")

    assert modified.status == ExecutionStatus.PENDING_REPLACE
    assert modified.broker_order_id == "replace-1"
    assert cancelled.status == ExecutionStatus.PENDING_CANCEL
    assert cancelled.broker_order_id == "cancel-1"
    assert state.status == ExecutionStatus.CANCELLED
    assert state.filled_quantity == Decimal("3")
    assert state.remaining_quantity == Decimal("7")
    assert state.average_fill_price == Decimal("70100")
    assert transport.requests[1].url == "https://example.test/api/v1/orders/order-1/modify"
    assert transport.requests[2].url == "https://example.test/api/v1/orders/replace-1/cancel"
    assert transport.requests[3].url == "https://example.test/api/v1/orders/cancel-1"


def test_toss_live_adapter_marks_server_error_as_unknown_state() -> None:
    transport = FakeTransport(
        [
            TossHttpResponse(200, {}, _token_payload()),
            TossHttpResponse(
                500,
                {},
                {"error": {"code": "internal-error", "message": "temporary failure"}},
            ),
        ]
    )
    adapter = TossLiveBrokerAdapter(_client(transport, account_seq=99))
    intent = OrderIntent(
        intent_id="intent-1",
        idempotency_key="idem-1",
        symbol="005930",
        side=Side.BUY,
        quantity=Decimal("1"),
        order_type=OrderType.MARKET,
        source="test",
        reason="server_error",
    )

    with pytest.raises(LiveBrokerError) as exc:
        adapter.place_order(intent)

    assert exc.value.unknown_state is True


def test_toss_live_adapter_accepts_cls_time_in_force() -> None:
    transport = FakeTransport(
        [
            TossHttpResponse(200, {}, _token_payload()),
            TossHttpResponse(200, {}, {"result": {"orderId": "order-1"}}),
        ]
    )
    adapter = TossLiveBrokerAdapter(_client(transport, account_seq=99))
    intent = OrderIntent(
        intent_id="intent-1",
        idempotency_key="idem-1",
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("1"),
        order_type=OrderType.LIMIT,
        limit_price=Decimal("185.5"),
        time_in_force=TimeInForce.CLS,
        source="test",
        reason="loc_order",
    )

    adapter.place_order(intent)

    assert transport.requests[1].json_body["timeInForce"] == "CLS"


def test_live_orchestrator_can_submit_through_toss_adapter_and_record_ledger() -> None:
    transport = FakeTransport(
        [
            TossHttpResponse(200, {}, _token_payload()),
            TossHttpResponse(200, {}, {"result": {"orderId": "order-1"}}),
        ]
    )
    client = _client(transport, account_seq=99)
    adapter = TossLiveBrokerAdapter(client)
    intent = OrderIntent(
        intent_id="intent-1",
        idempotency_key="idem-1",
        symbol="005930",
        side=Side.BUY,
        quantity=Decimal("1"),
        order_type=OrderType.LIMIT,
        limit_price=Decimal("70000"),
        source="test",
        reason="orchestrated_live_path",
    )

    with SQLiteStateStore() as store:
        orchestrator = LiveOrderOrchestrator(
            safety=PreTradeSafety(
                PreTradeSafetyConfig(
                    live_enabled=True,
                    allowed_symbols=("005930",),
                    max_order_notional=Decimal("100000"),
                )
            ),
            broker=adapter,
            store=store,
        )
        result = orchestrator.submit(
            intent,
            context=PreTradeSafetyContext(
                market_open=True,
                reconcile_clean=True,
                available_cash=Decimal("100000"),
            ),
        )
        order = store.load_execution_order("intent-1")
        events = store.list_execution_events(intent_id="intent-1")

    assert result.status == ExecutionStatus.ACKNOWLEDGED
    assert result.broker_order_id == "order-1"
    assert order is not None
    assert order["broker_order_id"] == "order-1"
    assert order["status"] == "ACKNOWLEDGED"
    assert [event["event_type"] for event in events] == ["broker_ack", "submit_started"]
