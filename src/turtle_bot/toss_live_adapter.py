from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, DecimalException
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
from .toss_client import ACCOUNT_HEADER, TossApiError, TossClient, _normalize_decimal_payload


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
        self._filled_quantity_high_water: dict[str, Decimal] = {}

    def place_order(self, intent: OrderIntent) -> BrokerOrderTicket:
        payload = self.serialize_order(intent)
        result = self._request_order_json("POST", "/api/v1/orders", json_body=payload)
        order_id = _required_str(result, "orderId", context="order response")
        _validate_optional_client_order_id_echo(result, str(payload["clientOrderId"]))
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
        order_id = _required_str(result, "orderId", context="modify response")
        return BrokerOrderTicket(
            broker_order_id=order_id,
            status=ExecutionStatus.PENDING_REPLACE,
            raw=dict(result),
        )

    def serialize_order(self, intent: OrderIntent) -> dict[str, Any]:
        """Return the exact JSON body that place_order will send."""

        return self._order_payload(intent)

    def cancel_order(self, ticket_id: str) -> BrokerOrderTicket:
        result = self._request_order_json(
            "POST",
            f"/api/v1/orders/{parse.quote(ticket_id, safe='')}/cancel",
            json_body={},
        )
        order_id = _required_str(result, "orderId", context="cancel response")
        return BrokerOrderTicket(
            broker_order_id=order_id,
            status=ExecutionStatus.PENDING_CANCEL,
            raw=dict(result),
        )

    def query_order(self, ticket_id: str) -> BrokerOrderState:
        response = self.client.get_order(ticket_id)
        if not isinstance(response, Mapping):
            raise LiveBrokerError("Toss order detail must be an object", unknown_state=True)
        raw = dict(response)
        status = _map_toss_status(raw.get("status"))
        broker_order_id = _required_str(raw, "orderId", context="order detail")
        if broker_order_id != ticket_id:
            raise LiveBrokerError("Toss order detail returned a different orderId", unknown_state=True)
        execution = raw.get("execution")
        if not isinstance(execution, Mapping):
            raise LiveBrokerError("Toss order detail execution must be an object", unknown_state=True)
        filled_quantity = _required_decimal(execution, "filledQuantity", context="execution")
        quantity = _required_decimal(raw, "quantity", context="order detail")
        average_fill_price = _nullable_decimal(
            execution,
            "averageFilledPrice",
            context="execution",
        )
        if filled_quantity < 0 or quantity < 0:
            raise LiveBrokerError("Toss order quantities must be nonnegative", unknown_state=True)
        if filled_quantity > quantity:
            raise LiveBrokerError(
                "Toss filledQuantity exceeds requested quantity",
                unknown_state=True,
            )
        previous_fill = self._filled_quantity_high_water.get(ticket_id)
        if previous_fill is not None and filled_quantity < previous_fill:
            raise LiveBrokerError(
                "Toss cumulative filledQuantity decreased",
                unknown_state=True,
            )
        if filled_quantity == 0:
            if average_fill_price is not None:
                raise LiveBrokerError(
                    "Toss averageFilledPrice must be null before a fill",
                    unknown_state=True,
                )
        elif average_fill_price is None or average_fill_price <= 0:
            raise LiveBrokerError(
                "Toss averageFilledPrice must be positive after a fill",
                unknown_state=True,
            )
        self._filled_quantity_high_water[ticket_id] = filled_quantity
        return BrokerOrderState(
            broker_order_id=broker_order_id,
            status=status,
            filled_quantity=filled_quantity,
            remaining_quantity=quantity - filled_quantity,
            average_fill_price=average_fill_price,
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
        except LiveBrokerError:
            raise
        except Exception as exc:
            raise LiveBrokerError(
                "Toss order transport outcome is unknown",
                unknown_state=True,
            ) from exc
        if response.status != 200:
            raise LiveBrokerError(
                f"Toss order endpoint returned unexpected HTTP {response.status}",
                unknown_state=True,
            )
        envelope = response.payload
        if not isinstance(envelope, Mapping) or not isinstance(envelope.get("result"), Mapping):
            raise LiveBrokerError(
                "Toss order response did not include an object result",
                unknown_state=True,
            )
        payload = _normalize_decimal_payload(envelope["result"])
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


def _required_str(payload: Mapping[str, Any], key: str, *, context: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise LiveBrokerError(
            f"Toss {context} did not include a valid {key}",
            unknown_state=True,
        )
    return value


def _validate_optional_client_order_id_echo(
    payload: Mapping[str, Any],
    expected: str,
) -> None:
    if "clientOrderId" not in payload or payload["clientOrderId"] is None:
        return
    if not isinstance(payload["clientOrderId"], str) or payload["clientOrderId"] != expected:
        raise LiveBrokerError(
            "Toss order response clientOrderId did not match the request",
            unknown_state=True,
        )


def _map_toss_status(value: Any) -> ExecutionStatus:
    raw = str(value or "").upper()
    if raw == "CANCELED":
        return ExecutionStatus.CANCELLED
    try:
        return ExecutionStatus(raw)
    except ValueError:
        return ExecutionStatus.UNKNOWN


def _required_decimal(
    payload: Mapping[str, Any],
    key: str,
    *,
    context: str,
) -> Decimal:
    value = payload.get(key)
    if (
        key not in payload
        or value is None
        or isinstance(value, bool)
        or not isinstance(value, (Decimal, str))
    ):
        raise LiveBrokerError(
            f"Toss {context} did not include a valid {key}",
            unknown_state=True,
        )
    try:
        parsed = as_decimal(value)
    except (DecimalException, TypeError, ValueError) as exc:
        raise LiveBrokerError(
            f"Toss {context} did not include a valid {key}",
            unknown_state=True,
        ) from exc
    if not parsed.is_finite():
        raise LiveBrokerError(
            f"Toss {context} {key} must be finite",
            unknown_state=True,
        )
    return parsed


def _nullable_decimal(
    payload: Mapping[str, Any],
    key: str,
    *,
    context: str,
) -> Decimal | None:
    if key not in payload:
        raise LiveBrokerError(
            f"Toss {context} did not include {key}",
            unknown_state=True,
        )
    if payload[key] is None:
        return None
    return _required_decimal(payload, key, context=context)


def _broker_error_from_toss(exc: TossApiError) -> LiveBrokerError:
    code = (exc.code or "").strip().lower()
    message = _decorate_api_error_message(str(exc))
    if code == "idempotency-key-conflict":
        return LiveBrokerError(
            f"Toss order identity conflict: {message}",
            unknown_state=True,
            code=code,
        )
    unknown_state = exc.status >= 500 or exc.status == 429 or not code
    if code in TERMINAL_ERROR_CODES or 400 <= exc.status < 500 and exc.status != 429:
        unknown_state = False
    return LiveBrokerError(message, unknown_state=unknown_state, code=code or None)
