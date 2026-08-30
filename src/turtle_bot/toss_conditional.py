from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Mapping
from urllib import parse

from .toss_client import ACCOUNT_HEADER, TossApiError, TossClient


CONDITIONAL_ORDER_GROUP = "conditional_order"
CONDITIONAL_ORDER_HISTORY_GROUP = "conditional_order_history"

_CREATE_REQUIRED = frozenset(
    {"symbol", "type", "quantity", "orderType", "expireDate", "first"}
)
_CREATE_ALLOWED = _CREATE_REQUIRED | {
    "second",
    "clientOrderId",
    "confirmHighValueOrder",
}
_MODIFY_REQUIRED = frozenset(
    {"type", "quantity", "orderType", "expireDate", "first"}
)
_MODIFY_ALLOWED = _MODIFY_REQUIRED | {"second", "confirmHighValueOrder"}
_LEG_REQUIRED = frozenset({"orderSide", "triggerPrice"})
_LEG_ALLOWED = _LEG_REQUIRED | {"orderPrice"}
_SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9.\-]+$")
_CURSOR_PATTERN = re.compile(r"^[A-Za-z0-9_\-]+$")
_CLIENT_ORDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]+$")
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DECIMAL_RESPONSE_FIELDS = frozenset(
    {"quantity", "triggerPrice", "targetProfitRate", "orderPrice"}
)
_UNKNOWN_HTTP_STATUSES = frozenset({408, 409, 425, 429})


class ConditionalOrderUnknownStateError(RuntimeError):
    """The broker may have accepted a request, so its outcome must be reconciled."""

    unknown_state = True

    def __init__(
        self,
        operation: str,
        message: str,
        *,
        status: int | None = None,
        payload: Any = None,
    ) -> None:
        self.operation = operation
        self.status = status
        self.payload = payload
        super().__init__(f"{operation} outcome is UNKNOWN: {message}")


class TossConditionalOrderAdapter:
    """Minimal adapter for Toss conditional-order endpoints (OpenAPI v1.2.14)."""

    def __init__(self, client: TossClient) -> None:
        self.client = client

    def create(
        self,
        payload: Mapping[str, Any],
        *,
        current_price: Decimal | str | int | float | None = None,
    ) -> Mapping[str, Any]:
        body = _validate_payload(payload, creating=True, current_price=current_price)
        result = self._mutate(
            "create",
            "POST",
            "/api/v1/conditional-orders",
            json_body=body,
            expected_status=200,
        )
        return _expect_conditional_order_id(result, "create")

    def list(
        self,
        *,
        status: str = "OPEN",
        symbol: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> Mapping[str, Any]:
        if status not in {"OPEN", "CLOSED"}:
            raise ValueError("status must be OPEN or CLOSED")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 to 100")
        query: dict[str, Any] = {"status": status, "limit": limit}
        if symbol is not None:
            query["symbol"] = _validate_pattern(symbol, "symbol", _SYMBOL_PATTERN)
        if cursor is not None:
            query["cursor"] = _validate_pattern(cursor, "cursor", _CURSOR_PATTERN)

        result = self._read("list", "/api/v1/conditional-orders", query=query)
        if (
            not isinstance(result, Mapping)
            or not isinstance(result.get("conditionalOrders"), list)
            or not isinstance(result.get("hasNext"), bool)
            or any(not isinstance(item, Mapping) for item in result["conditionalOrders"])
        ):
            raise _unknown("list", "response did not match the documented list schema")
        return result

    def get(self, conditional_order_id: str) -> Mapping[str, Any]:
        order_id = _required_id(conditional_order_id)
        result = self._read(
            "get",
            f"/api/v1/conditional-orders/{parse.quote(order_id, safe='')}",
        )
        return _expect_conditional_order_id(result, "get")

    def modify(
        self,
        conditional_order_id: str,
        payload: Mapping[str, Any],
        *,
        current_price: Decimal | str | int | float | None = None,
    ) -> Mapping[str, Any]:
        order_id = _required_id(conditional_order_id)
        body = _validate_payload(payload, creating=False, current_price=current_price)
        result = self._mutate(
            "modify",
            "POST",
            f"/api/v1/conditional-orders/{parse.quote(order_id, safe='')}/modify",
            json_body=body,
            expected_status=200,
        )
        return _expect_conditional_order_id(result, "modify")

    def delete(self, conditional_order_id: str) -> None:
        order_id = _required_id(conditional_order_id)
        self._mutate(
            "delete",
            "DELETE",
            f"/api/v1/conditional-orders/{parse.quote(order_id, safe='')}",
            json_body=None,
            expected_status=204,
        )

    def _read(
        self,
        operation: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
    ) -> Any:
        try:
            payload = self.client._request_json(
                "GET",
                path,
                query=query,
                account_required=True,
                group=CONDITIONAL_ORDER_HISTORY_GROUP,
            )
        except TossApiError as exc:
            if _is_unknown_status(exc.status):
                raise _unknown(
                    operation,
                    str(exc),
                    status=exc.status,
                    payload=exc.payload,
                ) from exc
            raise
        except OSError as exc:
            raise _unknown(operation, str(exc) or type(exc).__name__) from exc
        return _normalize_conditional_decimals(payload)

    def _mutate(
        self,
        operation: str,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None,
        expected_status: int,
    ) -> Any:
        if self.client.account_seq is None:
            raise ValueError(f"{ACCOUNT_HEADER} is required for {path}")
        if self.client.rate_limits.is_group_paused(CONDITIONAL_ORDER_GROUP):
            raise _unknown(operation, "conditional_order rate-limit group is paused", status=429)

        headers = self.client._auth_headers()
        headers[ACCOUNT_HEADER] = self.client.account_seq
        try:
            response = self.client.transport.request(
                method,
                self.client._url(path),
                headers=headers,
                json_body=json_body,
            )
            self.client._capture_rate_limit(CONDITIONAL_ORDER_GROUP, response.headers)
            if _is_unknown_status(response.status):
                self.client._raise_for_error(response)
                raise _unknown(
                    operation,
                    f"unexpected HTTP {response.status}",
                    status=response.status,
                )
            if response.status != expected_status:
                if 200 <= response.status < 300:
                    raise _unknown(
                        operation,
                        f"expected HTTP {expected_status}, received HTTP {response.status}",
                        status=response.status,
                        payload=response.payload,
                    )
                self.client._raise_for_error(response)

            if expected_status == 204:
                return None
            return _normalize_conditional_decimals(
                _unwrap_result_without_guessing(response.payload, operation)
            )
        except ConditionalOrderUnknownStateError:
            raise
        except TossApiError as exc:
            if _is_unknown_status(exc.status):
                raise _unknown(
                    operation,
                    str(exc),
                    status=exc.status,
                    payload=exc.payload,
                ) from exc
            raise
        except Exception as exc:
            # Once transport.request starts, a malformed/truncated response leaves
            # acceptance unknowable. Never expose it as retry-safe.
            raise _unknown(operation, str(exc) or type(exc).__name__) from exc


def _validate_payload(
    payload: Mapping[str, Any],
    *,
    creating: bool,
    current_price: Decimal | str | int | float | None,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise TypeError("conditional-order payload must be an object")
    required = _CREATE_REQUIRED if creating else _MODIFY_REQUIRED
    allowed = _CREATE_ALLOWED if creating else _MODIFY_ALLOWED
    missing = required - payload.keys()
    unknown_keys = payload.keys() - allowed
    if missing:
        raise ValueError(f"missing required fields: {', '.join(sorted(missing))}")
    if unknown_keys:
        raise ValueError(f"unsupported fields: {', '.join(sorted(unknown_keys))}")

    conditional_type = _enum(payload["type"], "type", {"SINGLE", "OCO", "OTO"})
    order_type = _enum(payload["orderType"], "orderType", {"LIMIT", "MARKET"})
    if conditional_type in {"OCO", "OTO"} and order_type != "LIMIT":
        raise ValueError(f"{conditional_type} requires orderType LIMIT")

    body: dict[str, Any] = {}
    if creating:
        body["symbol"] = _validate_pattern(payload["symbol"], "symbol", _SYMBOL_PATTERN)
    body.update(
        {
            "type": conditional_type,
            "quantity": _positive_decimal_string(payload["quantity"], "quantity"),
            "orderType": order_type,
            "expireDate": _date_string(payload["expireDate"]),
            "first": _validate_leg(payload["first"], "first", order_type),
        }
    )

    second_supplied = "second" in payload
    if conditional_type == "SINGLE":
        if second_supplied:
            raise ValueError("SINGLE must not include second")
    else:
        if not second_supplied or payload["second"] is None:
            raise ValueError(f"{conditional_type} requires second")
        body["second"] = _validate_leg(payload["second"], "second", order_type)

    first_side = body["first"]["orderSide"]
    second_side = body.get("second", {}).get("orderSide")
    if conditional_type == "OCO":
        if (first_side, second_side) != ("SELL", "SELL"):
            raise ValueError("OCO requires first SELL and second SELL")
        market_price = _positive_decimal(current_price, "current_price")
        first_trigger = Decimal(body["first"]["triggerPrice"])
        second_trigger = Decimal(body["second"]["triggerPrice"])
        if not first_trigger > market_price > second_trigger:
            raise ValueError("OCO requires first.triggerPrice > current_price > second.triggerPrice")
    elif conditional_type == "OTO" and (first_side, second_side) != ("BUY", "SELL"):
        raise ValueError("OTO requires first BUY and second SELL")

    if creating and "clientOrderId" in payload:
        client_order_id = payload["clientOrderId"]
        if not isinstance(client_order_id, str) or not _CLIENT_ORDER_ID_PATTERN.fullmatch(
            client_order_id
        ):
            raise ValueError("clientOrderId may contain only letters, digits, '-' and '_'")
        if len(client_order_id) > 36:
            raise ValueError("clientOrderId must be at most 36 characters")
        body["clientOrderId"] = client_order_id
    if "confirmHighValueOrder" in payload:
        if not isinstance(payload["confirmHighValueOrder"], bool):
            raise ValueError("confirmHighValueOrder must be boolean")
        body["confirmHighValueOrder"] = payload["confirmHighValueOrder"]
    return body


def _validate_leg(value: Any, name: str, order_type: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    missing = _LEG_REQUIRED - value.keys()
    unknown_keys = value.keys() - _LEG_ALLOWED
    if missing:
        raise ValueError(f"{name} missing required fields: {', '.join(sorted(missing))}")
    if unknown_keys:
        raise ValueError(f"{name} has unsupported fields: {', '.join(sorted(unknown_keys))}")
    has_order_price = "orderPrice" in value
    if order_type == "LIMIT" and not has_order_price:
        raise ValueError(f"{name}.orderPrice is required for LIMIT")
    if order_type == "MARKET" and has_order_price:
        raise ValueError(f"{name}.orderPrice is not allowed for MARKET")

    leg = {
        "orderSide": _enum(value["orderSide"], f"{name}.orderSide", {"BUY", "SELL"}),
        "triggerPrice": _positive_decimal_string(
            value["triggerPrice"], f"{name}.triggerPrice"
        ),
    }
    if has_order_price:
        leg["orderPrice"] = _positive_decimal_string(
            value["orderPrice"], f"{name}.orderPrice"
        )
    return leg


def _enum(value: Any, name: str, allowed: set[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{name} must be one of {', '.join(sorted(allowed))}")
    return value


def _validate_pattern(value: Any, name: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"{name} has an invalid format")
    return value


def _date_string(value: Any) -> str:
    if isinstance(value, datetime):
        raise ValueError("expireDate must be a date without a time")
    raw = value.isoformat() if isinstance(value, date) else value
    if not isinstance(raw, str) or not _DATE_PATTERN.fullmatch(raw):
        raise ValueError("expireDate must use YYYY-MM-DD")
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("expireDate must be a valid date") from exc
    return raw


def _positive_decimal(value: Any, name: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{name} must be a positive decimal")
    try:
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a positive decimal") from exc
    if not decimal_value.is_finite() or decimal_value <= 0:
        raise ValueError(f"{name} must be a positive decimal")
    return decimal_value


def _positive_decimal_string(value: Any, name: str) -> str:
    raw = format(_positive_decimal(value, name), "f")
    normalized = raw.rstrip("0").rstrip(".") if "." in raw else raw
    if len(normalized) > 30:
        raise ValueError(f"{name} must be at most 30 characters")
    return normalized


def _required_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("conditional_order_id is required")
    return value


def _is_unknown_status(status: int) -> bool:
    return status in _UNKNOWN_HTTP_STATUSES or status >= 500


def _unknown(
    operation: str,
    message: str,
    *,
    status: int | None = None,
    payload: Any = None,
) -> ConditionalOrderUnknownStateError:
    return ConditionalOrderUnknownStateError(
        operation,
        message,
        status=status,
        payload=payload,
    )


def _unwrap_result_without_guessing(payload: Any, operation: str) -> Any:
    if not isinstance(payload, Mapping) or "result" not in payload:
        raise _unknown(operation, "response did not include result", payload=payload)
    return payload["result"]


def _expect_conditional_order_id(payload: Any, operation: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise _unknown(operation, "response result was not an object", payload=payload)
    conditional_order_id = payload.get("conditionalOrderId")
    if not isinstance(conditional_order_id, str) or not conditional_order_id:
        raise _unknown(operation, "response did not include conditionalOrderId", payload=payload)
    return payload


def _normalize_conditional_decimals(value: Any) -> Any:
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            item = _normalize_conditional_decimals(item)
            if key in _DECIMAL_RESPONSE_FIELDS and isinstance(item, str):
                try:
                    item = Decimal(item)
                except InvalidOperation:
                    pass
            normalized[key] = item
        return normalized
    if isinstance(value, list):
        return [_normalize_conditional_decimals(item) for item in value]
    return value
