from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha1
import re
from typing import Any, Mapping
from urllib import parse

from .domain import as_decimal
from .live_execution import LiveBrokerError
from .live_order import (
    BrokerOrderState,
    BrokerOrderTicket,
    ExecutionStatus,
    OrderIntent,
    OrderType,
)
from .toss_client import ACCOUNT_HEADER, TossApiError, TossClient, _normalize_decimal_payload, _unwrap_result


TERMINAL_ERROR_CODES = frozenset(
    {
        "already-filled",
        "already-canceled",
        "already-rejected",
        "cancel-restricted",
        "modify-restricted",
        "insufficient-balance",
        "insufficient-quantity",
        "order-hours-closed",
        "order-type-not-allowed",
        "prerequisite-required",
        "account-restricted",
        "max-order-amount-exceeded",
        "idempotency-key-conflict",
    }
)
CLIENT_ORDER_ID_PATTERN = re.compile(r"[^a-zA-Z0-9\-_]")


_IP_NOT_ALLOWED_TOKENS = (
    "ip adress not allowed",
    "ip address not allowed",
    "address is not allowed",
    "not allowed ip",
    "not allowed address",
)


def _decorate_api_error_message(message: str) -> str:
    normalized = message.lower()
    if any(token in normalized for token in _IP_NOT_ALLOWED_TOKENS):
        return (
            "Toss API rejected this request from the current outbound network path. "
            "Check the container's public egress IP, VPN/proxy/cloud routing, and the "
            "official Toss Open API response before assuming an IP allowlist is required. "
            f"Original: {message}"
        )
    return message


@dataclass(frozen=True)
class ModifyOrderRequest:
    order_type: OrderType
    quantity: Decimal | None = None
    price: Decimal | None = None
    confirm_high_value_order: bool = False

    def as_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "orderType": self.order_type.value,
            "confirmHighValueOrder": self.confirm_high_value_order,
        }
        if self.quantity is not None:
            payload["quantity"] = str(as_decimal(self.quantity))
        if self.price is not None:
            payload["price"] = str(as_decimal(self.price))
        return payload


class TossLiveBrokerAdapter:
    """Live Toss order adapter.

    This is the only layer that should call Toss order mutation endpoints.
    It expects pre-trade safety and execution ledger checks to happen in
    LiveOrderOrchestrator before any method here is called.
    """

    def __init__(
        self,
        client: TossClient,
        *,
        confirm_high_value_order: bool = False,
    ) -> None:
        self.client = client
        self.confirm_high_value_order = confirm_high_value_order

    def place_order(self, intent: OrderIntent) -> BrokerOrderTicket:
        payload = self._order_payload(intent)
        result = self._request_order_json("POST", "/api/v1/orders", json_body=payload)
        order_id = _first_str(result, "orderId", "id")
        if order_id is None:
            raise LiveBrokerError("Toss order response did not include orderId", unknown_state=True)
        return BrokerOrderTicket(
            broker_order_id=order_id,
            status=ExecutionStatus.ACKNOWLEDGED,
            raw=dict(result),
        )

    def modify_order(self, ticket_id: str, request: dict[str, Any] | ModifyOrderRequest) -> BrokerOrderTicket:
        payload = request.as_json() if isinstance(request, ModifyOrderRequest) else dict(request)
        result = self._request_order_json(
            "POST",
            f"/api/v1/orders/{parse.quote(ticket_id, safe='')}/modify",
            json_body=payload,
        )
        order_id = _first_str(result, "orderId", "id")
        if order_id is None:
            raise LiveBrokerError("Toss modify response did not include orderId", unknown_state=True)
        return BrokerOrderTicket(
            broker_order_id=order_id,
            status=ExecutionStatus.PENDING_REPLACE,
            raw=dict(result),
        )

    def cancel_order(self, ticket_id: str) -> BrokerOrderTicket:
        result = self._request_order_json(
            "POST",
            f"/api/v1/orders/{parse.quote(ticket_id, safe='')}/cancel",
            json_body={},
        )
        order_id = _first_str(result, "orderId", "id")
        if order_id is None:
            raise LiveBrokerError("Toss cancel response did not include orderId", unknown_state=True)
        return BrokerOrderTicket(
            broker_order_id=order_id,
            status=ExecutionStatus.PENDING_CANCEL,
            raw=dict(result),
        )

    def query_order(self, ticket_id: str) -> BrokerOrderState:
        raw = dict(self.client.get_order(ticket_id))
        status = _map_toss_status(raw.get("status"))
        execution = raw.get("execution") if isinstance(raw.get("execution"), Mapping) else {}
        filled_quantity = _decimal_from_mapping(execution, "filledQuantity", default=Decimal("0"))
        quantity = _decimal_from_mapping(raw, "quantity", default=None)
        remaining_quantity = None
        if quantity is not None:
            remaining_quantity = max(quantity - filled_quantity, Decimal("0"))
        return BrokerOrderState(
            broker_order_id=str(raw.get("orderId") or ticket_id),
            status=status,
            filled_quantity=filled_quantity,
            remaining_quantity=remaining_quantity,
            average_fill_price=_decimal_from_mapping(execution, "averageFilledPrice", default=None),
            raw=raw,
        )

    def _order_payload(self, intent: OrderIntent) -> dict[str, Any]:
        if intent.quantity != intent.quantity.to_integral_value():
            raise LiveBrokerError("Toss quantity-based orders require integer quantity")
        payload: dict[str, Any] = {
            "clientOrderId": _client_order_id(intent.idempotency_key or intent.intent_id),
            "symbol": intent.symbol,
            "side": intent.side.value,
            "orderType": intent.order_type.value,
            "quantity": str(intent.quantity),
            "confirmHighValueOrder": self.confirm_high_value_order,
        }
        if intent.time_in_force.value != "DAY":
            payload["timeInForce"] = intent.time_in_force.value
        if intent.order_type == OrderType.LIMIT:
            if intent.limit_price is None:
                raise LiveBrokerError("LIMIT order requires limit_price")
            payload["price"] = str(intent.limit_price)
        elif intent.limit_price is not None:
            raise LiveBrokerError("MARKET order cannot include limit_price")
        return payload

    def _request_order_json(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        try:
            response = self._send_order_request(method, path, json_body=json_body)
            if response.status == 401:
                self.client.issue_token()
                response = self._send_order_request(method, path, json_body=json_body)
            self.client._raise_for_error(response)
        except TossApiError as exc:
            raise _broker_error_from_toss(exc) from exc
        payload = _normalize_decimal_payload(_unwrap_result(response.payload))
        if not isinstance(payload, Mapping):
            raise LiveBrokerError("Toss order response was not an object", unknown_state=True)
        return payload

    def _send_order_request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any],
    ):
        if self.client.account_seq is None:
            raise ValueError(f"{ACCOUNT_HEADER} is required for {path}")
        headers = self.client._auth_headers()
        headers[ACCOUNT_HEADER] = self.client.account_seq
        response = self.client.transport.request(
            method,
            self.client._url(path),
            headers=headers,
            json_body=json_body,
        )
        self.client._capture_rate_limit("orders", response.headers)
        return response


def _client_order_id(value: str) -> str:
    cleaned = CLIENT_ORDER_ID_PATTERN.sub("-", value).strip("-_")
    if not cleaned:
        cleaned = sha1(value.encode("utf-8")).hexdigest()
    if len(cleaned) <= 36:
        return cleaned
    digest = sha1(cleaned.encode("utf-8")).hexdigest()[:33]
    return f"oc-{digest}"


def _first_str(payload: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value is not None:
            return str(value)
    return None


def _map_toss_status(value: Any) -> ExecutionStatus:
    raw = str(value or "").upper()
    if raw == "CANCELED":
        return ExecutionStatus.CANCELLED
    try:
        return ExecutionStatus(raw)
    except ValueError:
        return ExecutionStatus.UNKNOWN


def _decimal_from_mapping(
    payload: Mapping[str, Any],
    key: str,
    *,
    default: Decimal | None,
) -> Decimal | None:
    value = payload.get(key)
    if value is None:
        return default
    return as_decimal(value)


def _broker_error_from_toss(exc: TossApiError) -> LiveBrokerError:
    code = exc.code or ""
    message = _decorate_api_error_message(str(exc))
    unknown_state = exc.status >= 500 or exc.status == 429 or not code
    if code in TERMINAL_ERROR_CODES or 400 <= exc.status < 500 and exc.status != 429:
        unknown_state = False
    return LiveBrokerError(message, unknown_state=unknown_state)
