from __future__ import annotations

import json
import sys
from os import environ
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol
from urllib import request


@dataclass(frozen=True)
class Notification:
    message: str
    level: str
    payload: Mapping[str, Any] | None
    emitted_at: datetime


class Notifier(Protocol):
    """Output-only notification interface used by runtime components."""

    def notify(
        self,
        message: str,
        *,
        level: str = "info",
        payload: Mapping[str, Any] | None = None,
    ) -> None: ...


class MemoryNotifier:
    """Simple in-memory notifier for deterministic tests and dry-run tooling."""

    def __init__(self) -> None:
        self.items: list[Notification] = []

    def notify(
        self,
        message: str,
        *,
        level: str = "info",
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        self.items.append(
            Notification(
                message=message,
                level=level,
                payload=payload,
                emitted_at=datetime.now(timezone.utc),
            )
        )

    def snapshot(self) -> tuple[Notification, ...]:
        return tuple(self.items)


class ConsoleNotifier:
    """Human-readable notifier for local/manual runs."""

    def __init__(self, *, stream=None):
        self.stream = stream if stream is not None else sys.stdout

    def notify(
        self,
        message: str,
        *,
        level: str = "info",
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        body: dict[str, Any] = {
            "level": level,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if payload is not None:
            body["payload"] = dict(payload)
        self.stream.write(json.dumps(body) + "\n")
        self.stream.flush()


class DiscordTradeNotifier:
    """Send compact trade/failure alerts to a Discord webhook."""

    DEFAULT_WEBHOOK_ENV = "DISCORD_TRADE_ALERT_WEBHOOK_URL"

    def __init__(
        self,
        *,
        webhook_url: str | None = None,
        webhook_url_env: str = DEFAULT_WEBHOOK_ENV,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float = 5.0,
        sender: Callable[[str, bytes, float], None] | None = None,
    ) -> None:
        self.webhook_url_env = webhook_url_env
        self.env = env if env is not None else environ
        self.webhook_url = (webhook_url or self.env.get(webhook_url_env) or "").strip()
        self.timeout_seconds = timeout_seconds
        self.sender = sender if sender is not None else self._post_json

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    def notify(
        self,
        message: str,
        *,
        level: str = "info",
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        if not self.enabled:
            return
        content = self._content_for(message, level=level, payload=payload or {})
        if not content:
            return
        body = json.dumps({"content": content}, ensure_ascii=False).encode("utf-8")
        try:
            self.sender(self.webhook_url, body, self.timeout_seconds)
        except Exception:
            return

    @classmethod
    def _post_json(cls, url: str, body: bytes, timeout_seconds: float) -> None:
        req = request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "turtle-trading-bot",
            },
            method="POST",
        )
        with request.urlopen(req, timeout=timeout_seconds) as response:
            response.read()

    @classmethod
    def _content_for(
        cls,
        message: str,
        *,
        level: str,
        payload: Mapping[str, Any],
    ) -> str | None:
        text = str(message or "")
        normalized_level = str(level or "info").upper()
        if text == "live_order_execution":
            status = str(payload.get("status") or "").upper()
            account_label = cls._account_label(payload)
            status_label = cls._status_label(status)
            symbol = str(payload.get("symbol") or "종목 확인 필요").upper()
            side = cls._side_label(payload.get("side"))
            quantity = payload.get("quantity") or payload.get("qty") or "수량 확인 필요"
            if status in {"REJECTED", "FAILED", "UNKNOWN"}:
                reason = payload.get("message") or status_label
                return f"[거래 실패] {account_label} · {symbol} {side} {quantity}주 - {reason}"
            return f"[거래 알림] {account_label} · {symbol} {side} {quantity}주 - {status_label}"
        if text in {
            "live_order_final_guard_blocked",
            "live_buying_power_unavailable",
            "live_trading_loop_failed",
            "live_service_blocked",
        }:
            account_label = cls._account_label(payload)
            symbol = str(payload.get("symbol") or "거래").upper()
            reason = cls._failure_reason(payload)
            return f"[거래 차단] {account_label} · {symbol} - {reason}"
        if normalized_level == "ERROR":
            return f"[거래 실패] {cls._account_label(payload)} · {cls._failure_reason(payload) or text}"
        return None

    @staticmethod
    def _account_label(payload: Mapping[str, Any]) -> str:
        alias = str(payload.get("account_alias") or "").strip()
        return alias or "계좌 별명 미설정"

    @staticmethod
    def _side_label(side: Any) -> str:
        text = str(side or "").upper()
        if text == "BUY":
            return "매수"
        if text == "SELL":
            return "매도"
        return text or "주문"

    @staticmethod
    def _status_label(status: Any) -> str:
        text = str(status or "").upper()
        labels = {
            "ACKNOWLEDGED": "주문 접수",
            "FILLED": "체결 완료",
            "PARTIALLY_FILLED": "일부 체결",
            "PENDING_CANCEL": "취소 요청",
            "CANCELLED": "취소 완료",
            "REJECTED": "주문 거절",
            "FAILED": "주문 실패",
            "UNKNOWN": "확인 필요",
        }
        return labels.get(text, text or "주문 접수")

    @staticmethod
    def _failure_reason(payload: Mapping[str, Any]) -> str:
        blockers = payload.get("blockers")
        if isinstance(blockers, list) and blockers:
            return ", ".join(str(item) for item in blockers)
        for key in ("error", "message", "reason", "status"):
            value = payload.get(key)
            if value:
                return DiscordTradeNotifier._status_label(value) if key == "status" else str(value)
        return "사유 확인 필요"