from __future__ import annotations

import json
import re
import sys
from os import environ
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol
from urllib import error, parse, request


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
    DEFAULT_CHANNEL_ENV = "DISCORD_ALLOWED_CHANNEL_ID"
    _CHANNEL_ID_RE = re.compile(r"[1-9]\d{16,19}\Z")
    _WEBHOOK_PATH_RE = re.compile(r"/api(?:/v\d+)?/webhooks/\d+/[^/]+/?\Z")

    def __init__(
        self,
        *,
        webhook_url: str | None = None,
        webhook_url_env: str = DEFAULT_WEBHOOK_ENV,
        channel_id: str | None = None,
        channel_id_env: str = DEFAULT_CHANNEL_ENV,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float = 5.0,
        sender: Callable[[str, bytes, float], None] | None = None,
        channel_resolver: Callable[[str, float], str] | None = None,
    ) -> None:
        self.webhook_url_env = webhook_url_env
        self.channel_id_env = channel_id_env
        self.env = env if env is not None else environ
        self.webhook_url = (
            webhook_url or self.env.get(webhook_url_env) or ""
        ).strip().rstrip("/")
        self.channel_id = (
            channel_id or self.env.get(channel_id_env) or ""
        ).strip()
        self.timeout_seconds = timeout_seconds
        self.sender = sender if sender is not None else self._post_json
        self.channel_resolver = (
            channel_resolver
            if channel_resolver is not None
            else self._fetch_webhook_channel_id
        )
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(
            self._valid_webhook_url(self.webhook_url)
            and self._CHANNEL_ID_RE.fullmatch(self.channel_id)
        )

    def notify(
        self,
        message: str,
        *,
        level: str = "info",
        payload: Mapping[str, Any] | None = None,
    ) -> bool:
        self.last_error = None
        if not self.enabled:
            missing = (
                self.webhook_url_env
                if not self._valid_webhook_url(self.webhook_url)
                else self.channel_id_env
            )
            self.last_error = f"{missing} is not configured or invalid"
            return False
        content = self._content_for(message, level=level, payload=payload or {})
        if not content:
            self.last_error = f"message is not a Discord trade alert: {message}"
            return False
        try:
            actual_channel_id = str(
                self.channel_resolver(self.webhook_url, self.timeout_seconds)
            ).strip()
        except Exception:
            self.last_error = "discord_channel_verification_failed"
            return False
        if not self._CHANNEL_ID_RE.fullmatch(actual_channel_id):
            self.last_error = "discord_channel_verification_failed"
            return False
        if actual_channel_id != self.channel_id:
            self.last_error = "discord_channel_mismatch"
            return False
        body = json.dumps(
            {
                "content": content[:1900],
                "allowed_mentions": {"parse": []},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        try:
            self.sender(self.webhook_url, body, self.timeout_seconds)
        except Exception as exc:
            self.last_error = self._safe_error_message(exc)
            return False
        return True

    @classmethod
    def _valid_webhook_url(cls, value: str) -> bool:
        try:
            parts = parse.urlsplit(value)
            return bool(
                parts.scheme == "https"
                and parts.hostname
                in {"discord.com", "canary.discord.com", "ptb.discord.com"}
                and parts.username is None
                and parts.password is None
                and parts.port in {None, 443}
                and not parts.query
                and not parts.fragment
                and cls._WEBHOOK_PATH_RE.fullmatch(parts.path)
            )
        except ValueError:
            return False

    @classmethod
    def _fetch_webhook_channel_id(cls, url: str, timeout_seconds: float) -> str:
        req = request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "turtle-trading-bot",
            },
            method="GET",
        )
        with request.urlopen(req, timeout=timeout_seconds) as response:
            raw = response.read(65_537)
        if len(raw) > 65_536:
            raise ValueError("discord webhook metadata is too large")
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError("discord webhook metadata is invalid")
        return str(value.get("channel_id") or "")

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

    @staticmethod
    def _safe_error_message(exc: Exception) -> str:
        if isinstance(exc, error.HTTPError):
            return f"HTTP {exc.code}"
        if isinstance(exc, error.URLError):
            reason_type = type(exc.reason).__name__
            return f"URL error ({reason_type})"
        return exc.__class__.__name__

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
        if text == "live_order_cancel_after_ack":
            status = str(payload.get("status") or "").upper()
            account_label = cls._account_label(payload)
            status_label = cls._status_label(status)
            symbol = str(payload.get("symbol") or "종목 확인 필요").upper()
            quantity = payload.get("quantity") or payload.get("qty") or "수량 확인 필요"
            return f"[거래 알림] {account_label} · {symbol} {quantity}주 - {status_label}"
        if text == "discord_alert_test":
            account_label = cls._account_label(payload)
            return f"[거래 알림 테스트] {account_label} · Discord 연결 확인"
        if text in {"intraday_shadow_plan_created", "intraday_paper_plan_created"}:
            account_label = cls._account_label(payload)
            symbol = str(payload.get("symbol") or "종목 확인 필요").upper()
            quantity = payload.get("quantity") or "수량 확인 필요"
            entry_trigger = payload.get("entry_trigger") or "?"
            entry_limit = payload.get("entry_limit") or "?"
            target = payload.get("target_trigger") or "?"
            stop_trigger = payload.get("stop_trigger") or "?"
            stop_limit = payload.get("stop_limit") or "?"
            planned_risk = payload.get("planned_risk") or "?"
            reward_risk = payload.get("reward_risk_ratio") or "?"
            entry_start = payload.get("entry_start") or "?"
            entry_expiry = payload.get("entry_expiry") or "?"
            mode_label = "가상매매" if text == "intraday_paper_plan_created" else "SHADOW"
            return (
                f"[단타 계획 · {mode_label}/실주문 없음] {account_label} · {symbol} {quantity}주 · "
                f"진입 {entry_trigger}~{entry_limit} · 익절 {target} · "
                f"손절 {stop_trigger}/{stop_limit} · 계획위험 ${planned_risk} · "
                f"R:R {reward_risk} · 진입창 {entry_start}~{entry_expiry}"
            )
        if text == "intraday_paper_entry_filled":
            symbol = str(payload.get("symbol") or "종목 확인 필요").upper()
            quantity = payload.get("quantity") or "?"
            price = payload.get("entry_price") or "?"
            cash = payload.get("cash_after") or "?"
            return (
                f"[가상매수 · 실주문 없음] {symbol} {quantity}주 · "
                f"체결가 ${price} · 잔여 가상현금 ${cash}"
            )
        if text == "intraday_paper_exit_filled":
            symbol = str(payload.get("symbol") or "종목 확인 필요").upper()
            quantity = payload.get("quantity") or "?"
            price = payload.get("exit_price") or "?"
            pnl = payload.get("net_pnl") or "?"
            reason = payload.get("exit_reason") or "?"
            return (
                f"[가상청산 · 실주문 없음] {symbol} {quantity}주 · "
                f"체결가 ${price} · 순손익 ${pnl} · {reason}"
            )
        if text == "intraday_paper_daily_report":
            session = payload.get("session_date") or "?"
            status = payload.get("status") or "?"
            pnl = payload.get("net_pnl") or "0"
            cash = payload.get("cash_end") or payload.get("current_cash") or "?"
            fees = payload.get("fees") or "0"
            gaps = payload.get("data_gaps") or 0
            reason = payload.get("exit_reason") or "-"
            return (
                f"[가상매매 일일결과 · 실주문 없음] {session} · {status} · "
                f"순손익 ${pnl} · 수수료 ${fees} · 가상현금 ${cash} · "
                f"청산 {reason} · 데이터갭 {gaps}건"
            )
        if text == "intraday_paper_run_report":
            status = payload.get("status") or "?"
            initial = payload.get("initial_cash") or "?"
            final = payload.get("final_equity")
            final = "미확정" if final is None else f"${final}"
            pnl = payload.get("net_pnl") or "?"
            return_rate = payload.get("return_fraction")
            return_rate = "미확정" if return_rate is None else str(return_rate)
            trades = payload.get("trades") or 0
            invalid = payload.get("invalid_sessions") or 0
            unresolved = payload.get("unresolved_positions") or 0
            missing = payload.get("coverage_missing") or 0
            fees = payload.get("total_fees") or "0"
            drawdown = payload.get("max_drawdown_fraction") or "0"
            return (
                f"[가상매매 한달결과 · 실주문 없음] {status} · 초기 ${initial} · "
                f"최종자산 {final} · 순손익 ${pnl} · 수익률 {return_rate} · "
                f"거래 {trades}회 · 수수료 ${fees} · 최대낙폭 {drawdown} · "
                f"무효 {invalid}일/미해결 {unresolved}건/누락 {missing}일"
            )
        if text == "intraday_paper_invalid":
            session = payload.get("session_date") or "?"
            reason = payload.get("reason") or payload.get("status") or "데이터 확인 필요"
            return f"[가상매매 무효 · 실주문 없음] {session} · {reason}"
        if text == "intraday_shadow_plan_blocked":
            account_label = cls._account_label(payload)
            symbol = str(payload.get("symbol") or "거래").upper()
            reason = payload.get("reason") or payload.get("blocker") or "사유 확인 필요"
            return (
                f"[단타 계획 차단 · SHADOW/실주문 없음] "
                f"{account_label} · {symbol} - {reason}"
            )
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
        return alias or "내 계좌"

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
