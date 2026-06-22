from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import date as date_cls, datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import ipaddress
from pathlib import Path
from threading import Thread
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen


PayloadProvider = Callable[[], Mapping[str, Any]]
EventsProvider = Callable[[int | None], list[Mapping[str, Any]]]
ActionRunner = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]
BrokerSnapshotsProvider = Callable[[], Mapping[str, Any]]
TOSS_LOGO_ASSET = Path(__file__).with_name("assets") / "toss-symbol.png"
PUBLIC_IP_SERVICES = (
    "https://api.ipify.org?format=json",
    "https://ifconfig.me/ip",
)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def public_ip_payload(*, timeout_seconds: float = 4.0) -> dict[str, Any]:
    errors: list[str] = []
    for url in PUBLIC_IP_SERVICES:
        try:
            with urlopen(url, timeout=timeout_seconds) as response:
                raw = response.read(512).decode("utf-8", errors="replace").strip()
            data: Any = json.loads(raw) if raw.startswith("{") else raw
            candidate = data.get("ip") if isinstance(data, Mapping) else data
            ip_text = str(candidate or "").strip()
            ipaddress.ip_address(ip_text)
            return {
                "status": "ok",
                "public_ip": ip_text,
                "source": url,
                "checked_at": _now_utc().isoformat(),
                "message": "Toss OpenAPI allowlist must include this outbound public IP.",
            }
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    return {
        "status": "unavailable",
        "public_ip": "",
        "source": "",
        "checked_at": _now_utc().isoformat(),
        "message": "Unable to detect outbound public IP from this runtime.",
        "errors": errors,
    }


@dataclass
class HealthSnapshot:
    mode: str = "idle"
    ready: bool = True
    blockers: tuple[str, ...] = field(default_factory=tuple)
    positions: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    open_orders: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    watchlist: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    generated_at: datetime = field(default_factory=_now_utc)

    def as_payload(self) -> dict[str, Any]:
        return {
            "status": "ready" if self.ready else "blocked",
            "mode": self.mode,
            "timestamp": self.generated_at.isoformat(),
            "ready": self.ready,
            "watchlist": {
                "count": len(self.watchlist),
                "items": list(self.watchlist),
            },
            "positions": {
                "count": len(self.positions),
                "items": list(self.positions),
            },
            "open_orders": {
                "count": len(self.open_orders),
                "items": list(self.open_orders),
            },
            "blockers": list(self.blockers),
        }

    def status_payload(self) -> dict[str, Any]:
        return {
            "status": "ready" if self.ready else "blocked",
            "mode": self.mode,
            "ready": self.ready,
            "blockers": list(self.blockers),
            "last_heartbeat_at": self.generated_at.isoformat(),
            "last_event_at": None,
        }

    def positions_payload(self) -> dict[str, Any]:
        return {"positions": list(self.positions), "count": len(self.positions)}

    def open_orders_payload(self) -> dict[str, Any]:
        return {"open_orders": list(self.open_orders), "count": len(self.open_orders)}

    def watchlist_payload(self) -> dict[str, Any]:
        return {"watchlist": list(self.watchlist), "count": len(self.watchlist)}


def _coerce_payload_items(value: Any) -> tuple[Mapping[str, Any], ...]:
    if isinstance(value, Mapping):
        return (dict(value),)
    if value is None:
        return ()
    if isinstance(value, Iterable):
        return tuple(dict(item) for item in value)
    return ()


def _normalize_payload(raw: Mapping[str, Any]) -> HealthSnapshot:
    positions = raw.get("positions", ())
    open_orders = raw.get("open_orders", ())
    watchlist = raw.get("watchlist", ())

    if isinstance(positions, Mapping):
        positions = positions.get("items", ())
    if isinstance(open_orders, Mapping):
        open_orders = open_orders.get("items", ())
    if isinstance(watchlist, Mapping):
        watchlist = watchlist.get("items", ())

    timestamp = raw.get("timestamp")
    if isinstance(timestamp, str):
        try:
            timestamp = datetime.fromisoformat(timestamp)
        except ValueError:
            timestamp = _now_utc()

    return HealthSnapshot(
        mode=str(raw.get("mode", "idle")),
        ready=bool(raw.get("ready", True)),
        blockers=tuple(str(item) for item in raw.get("blockers", ())),
        positions=_coerce_payload_items(positions),
        open_orders=_coerce_payload_items(open_orders),
        watchlist=_coerce_payload_items(watchlist),
        generated_at=timestamp if isinstance(timestamp, datetime) else _now_utc(),
    )


def _iso_datetime(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return value
    return None


def _extract_event_blockers(payload: Any) -> tuple[str, ...]:
    blockers = ()
    if not isinstance(payload, Mapping):
        return blockers
    raw = payload.get("blockers")
    if raw is None:
        return blockers
    if isinstance(raw, str):
        return (raw,)
    if not isinstance(raw, Iterable):
        return blockers
    return tuple(str(item) for item in raw)


def _friendly_runtime_error_label(value: Any) -> str:
    raw = str(value or "").strip()
    normalized = raw.lower()
    if any(
        marker in normalized
        for marker in (
            "ip adress not allowed",
            "ip address not allowed",
            "outbound public ip is not in the toss ip allowlist",
            "address is not allowed",
            "not allowed ip",
            "not allowed address",
        )
    ):
        return (
            "Toss가 현재 맥북/컨테이너 공개 IP를 거절했습니다. "
            "현재 공개 IP 확인 버튼으로 나온 IP를 Toss 개발자센터 앱 허용 IP에 추가한 뒤 다시 실행하세요."
        )
    return raw


def _coerce_events_payload(items: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for item in items:
        created = item.get("created_at")
        if isinstance(created, datetime):
            created = created.isoformat()
        elif created is not None and not isinstance(created, str):
            created = str(created)
        payload = item.get("payload")
        if isinstance(payload, Mapping):
            payload = dict(payload)
            blockers = payload.get("blockers")
            if isinstance(blockers, Iterable) and not isinstance(blockers, (str, bytes)):
                payload["blockers"] = [_friendly_blocker_label(blocker) for blocker in blockers]
            if "error" in payload:
                payload["error"] = _friendly_runtime_error_label(payload.get("error"))

        output.append(
            {
                "id": item.get("id"),
                "level": item.get("level"),
                "message": item.get("message"),
                "payload": payload,
                "created_at": created,
            }
        )
    return output


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _summarize_events(items: list[Mapping[str, Any]]) -> dict[str, Any]:
    total = len(items)
    if total == 0:
        return {
            "total": 0,
            "first_event_at": None,
            "last_event_at": None,
            "by_level": {},
            "by_message": {},
            "blockers": [],
            "paper_order_intents": 0,
            "paper_fills": 0,
            "paper_guard_checks": 0,
            "shadow_order_intents": 0,
            "shadow_fills": 0,
            "shadow_guard_checks": 0,
            "paper_runtime_blocks": 0,
        }

    by_level = Counter()
    by_message = Counter()
    blockers: list[str] = []
    seen_blockers = set[str]()
    timestamps: list[datetime] = []

    for item in items:
        level = str(item.get("level", "UNKNOWN"))
        message = str(item.get("message", "UNKNOWN"))
        by_level[level] += 1
        by_message[message] += 1
        for blocker in _extract_event_blockers(item.get("payload")):
            friendly = _friendly_blocker_label(blocker)
            if friendly not in seen_blockers:
                blockers.append(friendly)
                seen_blockers.add(friendly)
        created = _timestamp(item.get("created_at"))
        if created is not None:
            timestamps.append(created)

    return {
        "total": total,
        "first_event_at": min(timestamps).isoformat() if timestamps else None,
        "last_event_at": max(timestamps).isoformat() if timestamps else None,
        "by_level": dict(by_level),
        "by_message": dict(by_message),
        "blockers": blockers,
        "paper_order_intents": by_message.get("paper_order_intent", 0),
        "paper_fills": by_message.get("paper_fill", 0),
        "paper_guard_checks": by_message.get("paper_order_guard", 0),
        "shadow_order_intents": by_message.get("shadow_order_intent", 0),
        "shadow_fills": by_message.get("shadow_fill", 0),
        "shadow_guard_checks": by_message.get("shadow_order_guard", 0),
        "live_order_intents": by_message.get("live_order_intent", 0),
        "live_order_executions": by_message.get("live_order_execution", 0),
        "live_runtime_blocks": by_message.get("live_service_blocked", 0),
        "paper_runtime_blocks": by_message.get("paper_service_blocked", 0)
        + by_message.get("shadow_service_blocked", 0)
        + by_message.get("live_service_blocked", 0),
    }


def _settings_value(settings: Mapping[str, Any], *path: str) -> Any:
    value: Any = settings
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _has_blocker(blockers: Iterable[str], *needles: str) -> bool:
    return any(any(needle in blocker for needle in needles) for blocker in blockers)


def _friendly_blocker_label(blocker: Any) -> str:
    text = str(blocker or "")
    if "TOSS_CLIENT_ID" in text or "TOSS_CLIENT_SECRET" in text:
        return "토스 API 키가 아직 입력되지 않았습니다."
    if "account_seq" in text:
        return "거래 계좌 번호가 아직 입력되지 않았습니다."
    if "runtime.symbols" in text or "universe_candidate_symbols" in text:
        return "거래할 종목이 아직 없습니다."
    if "market_session_not_open" in text:
        return "지금은 주문을 평가할 시간이 아닙니다."
    if "market_calendar_unknown" in text:
        return "시장 개장 여부를 확인하지 못했습니다."
    if "universe_empty" in text:
        return "조건에 맞는 종목 후보가 없습니다."
    if "price_stale" in text or "stale" in text:
        return "시세가 최신인지 확인이 필요합니다."
    if "reconcile" in text:
        return "계좌와 봇 기록을 다시 확인해야 합니다."
    if "live_consent_ids_not_configured" in text:
        return "수동 승인 확인 설정이 켜져 있지만 등록된 코드가 없습니다."
    if "live_consent_id_required" in text:
        return "수동 승인 확인이 켜져 있어 자동 실행을 막고 있습니다."
    if "live_consent_id_not_allowed" in text:
        return "입력한 수동 승인 코드가 등록된 목록에 없습니다."
    if "consecutive_order_failures" in text:
        return "실주문 실패가 연속으로 발생해 새 주문을 멈췄습니다."
    if "unresolved_live_execution" in text or "unresolved execution" in text:
        return "아직 최종 상태가 확인되지 않은 실주문이 있습니다."
    if "live.emergency_stop" in text:
        return "거래 중지 상태입니다."
    if "toss.live_enabled" in text or "runtime.mode" in text:
        return "실거래 시작 설정이 아직 맞춰지지 않았습니다."
    if "toss_live_consent_ids" in text or "live consent allowlist is required for live mode" in text:
        return "수동 승인 확인 설정을 끄거나 승인 코드 목록을 설정해야 합니다."
    return text


def _friendly_status_payload(status: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(status)
    raw_blockers = status.get("blockers")
    if isinstance(raw_blockers, Iterable) and not isinstance(raw_blockers, (str, bytes)):
        friendly: list[str] = []
        for blocker in raw_blockers:
            label = _friendly_blocker_label(blocker)
            if label not in friendly:
                friendly.append(label)
        payload["blockers"] = friendly
    return payload


def _readiness_check(
    check_id: str,
    label: str,
    status: str,
    summary: str,
    action: str,
) -> dict[str, str]:
    return {
        "id": check_id,
        "label": label,
        "status": status,
        "summary": summary,
        "action": action,
    }


def _build_live_readiness_payload(
    *,
    status: Mapping[str, Any],
    settings: Mapping[str, Any],
    snapshot: HealthSnapshot,
    events_summary: Mapping[str, Any],
) -> dict[str, Any]:
    blockers = tuple(str(item) for item in status.get("blockers", ()))
    runtime_mode = str(_settings_value(settings, "runtime", "mode") or status.get("mode") or "idle")
    strategy_kind = str(_settings_value(settings, "strategy_kind") or "unknown")
    live_enabled = bool(_settings_value(settings, "toss", "live_enabled"))
    client_id_configured = _settings_value(settings, "toss", "client_id_configured")
    client_secret_configured = _settings_value(settings, "toss", "client_secret_configured")
    api_configured = (
        bool(client_id_configured) and bool(client_secret_configured)
        if client_id_configured is not None or client_secret_configured is not None
        else not _has_blocker(blockers, "TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET")
    )
    account_configured = bool(_settings_value(settings, "toss", "account_seq_configured"))
    consent_required = bool(_settings_value(settings, "toss", "require_live_consent"))
    consent_configured = bool(_settings_value(settings, "toss", "live_consent_ids_configured"))
    consent_count = int(_settings_value(settings, "toss", "live_consent_ids_count") or 0)

    effective_blockers = tuple(
        blocker
        for blocker in blockers
        if not (
            (api_configured and _has_blocker((blocker,), "TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET"))
            or (account_configured and _has_blocker((blocker,), "account_seq"))
        )
    )
    api_blocked = not api_configured or _has_blocker(effective_blockers, "TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET")
    account_blocked = _has_blocker(effective_blockers, "account_seq")
    universe_blocked = _has_blocker(effective_blockers, "runtime.symbols", "universe_candidate_symbols", "universe_empty")
    market_blocked = _has_blocker(effective_blockers, "market_session_not_open", "market_calendar_unknown")
    reconcile_blocked = _has_blocker(effective_blockers, "reconcile", "price_stale", "stale")
    open_order_count = len(snapshot.open_orders)
    event_total = int(events_summary.get("total") or 0)
    status_ready = bool(status.get("ready")) or not effective_blockers

    mode_is_live = runtime_mode == "live"
    live_gate_blocked = (mode_is_live and not live_enabled) or (not mode_is_live and live_enabled)
    consent_blocked = consent_required and not consent_configured
    can_submit_live_orders = (
        mode_is_live
        and live_enabled
        and status_ready
        and not api_blocked
        and not account_blocked
        and account_configured
        and not consent_blocked
        and not universe_blocked
        and not market_blocked
        and not reconcile_blocked
        and open_order_count > 0
    )

    checks = [
        _readiness_check(
            "toss_credentials",
            "토스 API 인증",
            "blocked" if api_blocked else "done",
            "토스 앱 인증 정보 확인 필요"
            if api_blocked
            else "토스 연결 키가 입력되어 있습니다.",
            "토스 개발자센터에서 발급받은 앱 ID와 비밀키를 설정하세요."
            if api_blocked
            else "비밀키는 화면에 다시 보여주지 않습니다.",
        ),
        _readiness_check(
            "toss_account",
            "계좌 연결",
            "blocked" if account_blocked or not account_configured else "done",
            "거래 계좌 번호가 아직 확인되지 않았습니다."
            if account_blocked or not account_configured
            else "거래에 사용할 계좌 번호가 입력되어 있습니다.",
            "설정 화면에서 계좌 번호를 입력하세요."
            if account_blocked or not account_configured
            else "실거래 전 사용할 계좌가 맞는지만 확인하세요.",
        ),
        _readiness_check(
            "strategy_mode",
            "전략/런타임 모드",
            "blocked" if live_gate_blocked else "done" if runtime_mode in {"paper", "shadow", "live"} else "warn",
            f"현재 전략은 {strategy_kind}, 거래 상태는 {runtime_mode}입니다.",
            "안전 파일럿 시작 버튼이 필요한 실거래 설정을 자동으로 맞춥니다."
            if live_gate_blocked
            else "실거래 중에는 조건을 통과한 주문 후보만 토스로 보냅니다."
            if mode_is_live
            else "실거래 전에는 계좌 조회와 주문 후보가 정상인지 먼저 확인합니다.",
        ),
        _readiness_check(
            "universe",
            "종목 후보",
            "blocked" if universe_blocked else "done",
            "감시할 종목 후보가 부족합니다."
            if universe_blocked
            else "종목 후보/유니버스 설정이 준비되어 있습니다.",
            "거래할 종목을 하나 이상 넣어 주세요."
            if universe_blocked
            else "봇은 이 목록 안에서만 주문 후보를 만듭니다.",
        ),
        _readiness_check(
            "live_consent",
            "자동매매 확인 방식",
            "blocked" if consent_blocked else "done",
            "수동 승인 확인이 켜져 있지만 승인 코드 목록이 비어 있습니다."
            if consent_blocked
            else "24시간 자동매매용으로 수동 승인 없이 실행됩니다."
            if not consent_required
            else f"수동 승인 코드가 총 {consent_count}개 등록되어 있습니다.",
            "24시간 자동매매를 원하면 toss.require_live_consent를 false로 두세요."
            if consent_required
            else "자동 실행 중에는 주문마다 별도 코드를 다시 입력하지 않습니다.",
        ),
        _readiness_check(
            "market_data",
            "시장/시세 상태",
            "warn" if market_blocked or reconcile_blocked or not status_ready else "done",
            "주문 판단 전에 확인할 항목이 있습니다."
            if market_blocked or reconcile_blocked or not status_ready
            else "시장 상태와 시세 확인이 준비되어 있습니다.",
            "장이 열렸는지, 최근 점검이 정상인지 확인한 뒤 시작하세요."
            if market_blocked or reconcile_blocked or not status_ready
            else "주문 직전에도 같은 조건을 다시 확인합니다.",
        ),
        _readiness_check(
            "open_orders",
            "주문 후보",
            "done" if open_order_count else "warn" if mode_is_live else "done",
            f"주문 후보 {open_order_count}건이 표시됩니다."
            if open_order_count
            else "현재 표시된 주문 후보는 없습니다.",
            "실거래 중에는 이 후보가 실제 주문 대상입니다."
            if open_order_count
            else "매수 신호가 없으면 실거래를 시작해도 주문은 나가지 않습니다.",
        ),
        _readiness_check(
            "event_audit",
            "최근 이벤트 기록",
            "done" if event_total else "warn",
            f"최근 이벤트 {event_total}건을 확인했습니다."
            if event_total
            else "아직 이벤트 로그가 비어 있습니다.",
            "먼저 봇이 최근에 정상 점검을 한 기록이 있어야 합니다."
            if not event_total
            else "이벤트 탭에서 WARN/ERROR를 확인하세요.",
        ),
        _readiness_check(
            "live_order_engine",
            "실주문 제출 엔진",
            "done" if mode_is_live and live_enabled else "warn",
            "실제 주문을 보낼 준비 경로가 연결되어 있습니다."
            if mode_is_live and live_enabled
            else "실주문 엔진은 연결되어 있지만 현재 모드에서는 주문을 제출하지 않습니다.",
            "실거래 중에는 최근 이벤트와 주문 기록을 같이 확인하세요."
            if mode_is_live and live_enabled
            else "안전 파일럿 시작 버튼이 이 설정을 자동으로 맞춥니다.",
        ),
    ]
    counts = Counter(str(item["status"]) for item in checks)
    blocked_count = counts.get("blocked", 0)
    warning_count = counts.get("warn", 0)
    state = (
        "ready_for_live"
        if can_submit_live_orders
        else "blocked"
        if blocked_count
        else "needs_review"
        if warning_count
        else "ready_for_shadow"
    )
    headline = (
        "실거래를 시작할 준비가 됐습니다"
        if can_submit_live_orders
        else "아직 실거래를 시작할 수 없습니다"
        if blocked_count
        else "시작 전 마지막 확인이 필요합니다"
        if warning_count
        else "shadow 검증 기준은 통과했습니다"
    )
    return {
        "state": state,
        "headline": headline,
        "summary": {
            "done": counts.get("done", 0),
            "warning": warning_count,
            "blocked": blocked_count,
        },
        "runtime_mode": runtime_mode,
        "strategy_kind": strategy_kind,
        "live_enabled": live_enabled,
        "live_consent_required": consent_required,
        "live_consent_configured": consent_configured,
        "live_consent_count": consent_count,
        "can_submit_live_orders": can_submit_live_orders,
        "submit_disabled_reason": None
        if can_submit_live_orders
        else (
            "토스 키, 계좌, 종목, 시장 상태, 주문 후보, 자동매매 확인 방식이 준비되어야 해요."
            if consent_blocked
            else "토스 키, 계좌, 거래 종목, 시장 상태, 주문 후보가 준비되어야 합니다."
        ),
        "checks": checks,
    }


def _latest_event_by_message(
    events: list[Mapping[str, Any]],
    *messages: str,
) -> Mapping[str, Any] | None:
    wanted = set(messages)
    for event in events:
        if str(event.get("message") or "") in wanted:
            return event
    return None


def _snapshot_order_count(snapshot: Mapping[str, Any] | None) -> int:
    if not isinstance(snapshot, Mapping):
        return 0
    payload = snapshot.get("payload")
    if not isinstance(payload, Mapping):
        return 0
    raw = payload.get("orders")
    if isinstance(raw, list):
        return len(raw)
    raw = payload.get("items")
    if isinstance(raw, list):
        return len(raw)
    return 0


def _snapshot_items(snapshot: Mapping[str, Any] | None, *keys: str) -> list[Mapping[str, Any]]:
    if not isinstance(snapshot, Mapping):
        return []
    payload = snapshot.get("payload")
    if not isinstance(payload, Mapping):
        return []
    for key in keys:
        raw = payload.get(key)
        if isinstance(raw, list):
            return [dict(item) for item in raw if isinstance(item, Mapping)]
    return []


def _event_payload(event: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(event, Mapping):
        return {}
    payload = event.get("payload")
    return payload if isinstance(payload, Mapping) else {}


def _event_created_at(event: Mapping[str, Any] | None) -> str | None:
    if not isinstance(event, Mapping):
        return None
    return _iso_datetime(event.get("created_at"))


def _build_live_monitor_payload(
    *,
    events: list[Mapping[str, Any]],
    snapshot: HealthSnapshot,
    broker_snapshots: Mapping[str, Any],
) -> dict[str, Any]:
    account_sync = _latest_event_by_message(events, "broker_account_synced")
    order_history_sync = _latest_event_by_message(
        events,
        "broker_order_history_synced",
        "broker_order_history_sync_failed",
    )
    status_sync = _latest_event_by_message(
        events,
        "live_order_status_synced",
        "live_order_status_sync_failed",
    )
    last_execution = _latest_event_by_message(
        events,
        "live_order_execution",
        "live_order_cancel_after_ack",
    )

    status_payload = _event_payload(status_sync)
    execution_payload = _event_payload(last_execution)
    closed_orders = broker_snapshots.get("closed_orders")
    holdings = broker_snapshots.get("holdings")
    open_orders = broker_snapshots.get("open_orders")
    closed_order_items = _snapshot_items(closed_orders, "orders", "items")
    latest_closed_items = closed_order_items[:5]
    order_history_error = None
    if order_history_sync and order_history_sync.get("message") == "broker_order_history_sync_failed":
        order_history_error = str(_event_payload(order_history_sync).get("error") or "")

    return {
        "account_sync": {
            "last_synced_at": _event_created_at(account_sync)
            or (holdings.get("captured_at") if isinstance(holdings, Mapping) else None),
            "holdings_count": _event_payload(account_sync).get("holdings_count"),
            "open_orders_count": _event_payload(account_sync).get("open_orders_count"),
            "synced_live_positions": bool(
                _event_payload(account_sync).get("synced_live_positions")
            ),
        },
        "order_history": {
            "source": "orders_closed",
            "source_label": "계좌 주문 목록(status=CLOSED)",
            "market_trades_are_account_history": False,
            "last_synced_at": _event_created_at(order_history_sync)
            or (closed_orders.get("captured_at") if isinstance(closed_orders, Mapping) else None),
            "closed_orders_count": _snapshot_order_count(closed_orders),
            "error": order_history_error,
            "items": latest_closed_items,
        },
        "order_status_tracking": {
            "last_synced_at": _event_created_at(status_sync),
            "checked": status_payload.get("checked"),
            "updated": status_payload.get("updated"),
            "failed": status_payload.get("failed"),
            "remaining_unresolved": status_payload.get("remaining_unresolved"),
        },
        "current": {
            "positions_count": len(snapshot.positions),
            "open_orders_count": len(snapshot.open_orders)
            or _snapshot_order_count(open_orders),
            "unresolved_execution_count": status_payload.get("remaining_unresolved"),
        },
        "last_execution": {
            "message": last_execution.get("message") if isinstance(last_execution, Mapping) else None,
            "created_at": _event_created_at(last_execution),
            "symbol": execution_payload.get("symbol"),
            "side": execution_payload.get("side"),
            "quantity": execution_payload.get("quantity"),
            "status": execution_payload.get("status"),
            "broker_order_id": execution_payload.get("broker_order_id"),
            "safety": execution_payload.get("safety"),
        },
    }


def _events_for_day(items: list[Mapping[str, Any]], target: date_cls) -> list[Mapping[str, Any]]:
    output: list[Mapping[str, Any]] = []
    for item in items:
        created = _timestamp(item.get("created_at"))
        if created is not None and created.date() == target:
            output.append(item)
    return output


def _first_day_event(items: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    if not items:
        return None
    if isinstance(items[0].get("created_at"), datetime):
        return items[0]
    if isinstance(items[0].get("created_at"), str):
        return items[0]
    return items[0]


def dashboard_html() -> str:
    _skeleton_reference = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Toss Turtle Bot</title>
  <style>
    :root {
      --bg: #f7f9fc;
      --panel: #ffffff;
      --panel-soft: #f8fafd;
      --text: #101828;
      --muted: #7b8aa3;
      --line: #e3e9f2;
      --line-soft: #edf1f7;
      --blue: #2563eb;
      --blue-soft: #edf4ff;
      --green: #36c690;
      --amber: #ffbd65;
      --shadow: 0 16px 36px rgba(31, 46, 76, 0.06);
    }

    * { box-sizing: border-box; }

    html, body {
      margin: 0;
      min-height: 100%;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      color: var(--text);
      background: var(--bg);
      overflow-x: hidden;
    }

    button, a { font: inherit; }

    .app {
      min-height: 100vh;
      display: grid;
      grid-template-rows: 86px 1fr;
      background:
        radial-gradient(circle at 68% 18%, rgba(37, 99, 235, 0.05), transparent 30%),
        linear-gradient(180deg, #fbfcff 0%, var(--bg) 48%, #f5f8fc 100%);
    }

    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 20px;
      padding: 0 28px;
      background: rgba(255, 255, 255, 0.9);
      border-bottom: 1px solid var(--line);
      backdrop-filter: blur(14px);
      position: sticky;
      top: 0;
      z-index: 10;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 16px;
      min-width: 0;
    }

    .logo {
      width: 58px;
      height: 46px;
      border-radius: 8px;
      background: #ffffff;
      flex: 0 0 auto;
      display: block;
      object-fit: contain;
    }

    .brand-title {
      display: flex;
      align-items: center;
      gap: 18px;
      min-width: 0;
    }

    .brand-title strong {
      font-size: 19px;
      white-space: nowrap;
    }

    .execution-badge {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: #1f2937;
      font-size: 13px;
      font-weight: 700;
      white-space: nowrap;
    }

    .execution-badge::before {
      content: "";
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--green);
    }

    .top-actions {
      display: flex;
      align-items: center;
      gap: 12px;
      min-width: 0;
    }

    .clock-line {
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
      color: #516079;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #f8fafc;
      padding: 9px 12px;
      box-shadow: 0 8px 18px rgba(31, 46, 76, 0.04);
    }

    .clock-line svg {
      width: 18px;
      height: 18px;
      stroke-width: 2;
    }

    .clock-line span {
      white-space: nowrap;
    }

    .clock-text {
      color: #516079;
      font-size: 12px;
      font-weight: 800;
      line-height: 1;
    }

    .ghost-line {
      display: inline-block;
      height: 10px;
      border-radius: 99px;
      background: #dfe5ee;
    }

    .btn {
      height: 48px;
      border-radius: 8px;
      border: 1px solid var(--line);
      background: #ffffff;
      color: #1f2a44;
      padding: 0 18px;
      display: inline-flex;
      align-items: center;
      gap: 10px;
      font-weight: 800;
      cursor: pointer;
      text-decoration: none;
      box-shadow: 0 10px 20px rgba(31, 46, 76, 0.04);
    }

    .btn.primary {
      background: linear-gradient(145deg, #2f6df4, #1d4ed8);
      color: #ffffff;
      border-color: #1d4ed8;
      box-shadow: 0 14px 26px rgba(37, 99, 235, 0.24);
    }

    .btn svg {
      width: 18px;
      height: 18px;
      stroke-width: 2.4;
    }

    .btn:disabled {
      cursor: not-allowed;
      opacity: 0.72;
      box-shadow: none;
    }

    .shell {
      display: grid;
      grid-template-columns: 178px minmax(0, 1fr);
      min-height: 0;
    }

    .sidebar {
      background: rgba(255, 255, 255, 0.82);
      border-right: 1px solid var(--line);
      padding: 30px 20px 22px;
      display: flex;
      flex-direction: column;
      gap: 22px;
      position: sticky;
      top: 86px;
      height: calc(100vh - 86px);
      overflow: auto;
    }

    .nav {
      display: grid;
      gap: 10px;
    }

    .nav a,
    .theme-button {
      min-height: 52px;
      border-radius: 8px;
      color: #8492aa;
      text-decoration: none;
      display: flex;
      align-items: center;
      gap: 14px;
      padding: 0 14px;
      font-size: 13px;
      font-weight: 800;
      border: 1px solid transparent;
    }

    .nav a.active {
      color: var(--blue);
      background: #edf4ff;
      border-color: #e7eefb;
    }

    .nav svg,
    .theme-button svg,
    .bottom-nav svg {
      width: 19px;
      height: 19px;
      stroke-width: 2.2;
      flex: 0 0 auto;
    }

    .theme-button {
      margin-top: auto;
      padding-left: 14px;
      min-height: 42px;
    }

    .sidebar-status {
      margin-top: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcff;
      padding: 14px;
      display: grid;
      gap: 10px;
    }

    .sidebar-status strong {
      font-size: 13px;
    }

    .sidebar-status p {
      margin: 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
      overflow-wrap: anywhere;
      display: -webkit-box;
      -webkit-line-clamp: 3;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }

    .sidebar-action {
      min-height: 34px;
      border-radius: 8px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 0 12px;
      background: var(--blue);
      color: #ffffff;
      text-decoration: none;
      font-size: 12px;
      font-weight: 900;
    }

    .main {
      min-width: 0;
      padding: 22px;
    }

    .view {
      display: none;
      min-width: 0;
    }

    .view.active {
      display: block;
    }

    .dashboard-grid {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1.12fr) minmax(320px, 0.98fr);
      gap: 20px;
      align-items: stretch;
    }

    .stat-row {
      grid-column: 1 / -1;
      display: grid;
      grid-template-columns: 1.55fr repeat(4, minmax(150px, 1fr));
      gap: 16px;
    }

    .card {
      background: rgba(255, 255, 255, 0.86);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      min-width: 0;
      overflow: hidden;
    }

    .stat-card {
      min-height: 122px;
      padding: 24px;
      display: flex;
      align-items: center;
      gap: 18px;
    }

    .stat-card.compact {
      justify-content: flex-start;
      gap: 20px;
    }

    .icon-tile {
      width: 64px;
      height: 64px;
      border-radius: 8px;
      background: var(--blue-soft);
      color: var(--blue);
      display: grid;
      place-items: center;
      flex: 0 0 auto;
    }

    .icon-tile.small {
      width: 46px;
      height: 46px;
      border-radius: 50%;
    }

    .icon-tile svg {
      width: 24px;
      height: 24px;
      stroke-width: 2.4;
    }

    .stat-label {
      margin: 0 0 12px;
      font-size: 13px;
      font-weight: 900;
      color: #0f172a;
    }

    .stat-value {
      margin: 0;
      font-size: 30px;
      line-height: 1.05;
      font-weight: 900;
    }

    .stat-lines {
      min-width: 0;
      flex: 1;
      display: grid;
      gap: 12px;
    }

    .pill-ghost {
      width: 54px;
      height: 22px;
      border-radius: 99px;
      background: #eaf1ff;
      position: relative;
      margin-left: auto;
      flex: 0 0 auto;
    }

    .pill-ghost::after {
      content: "";
      position: absolute;
      inset: 7px 10px;
      border-radius: 99px;
      background: #b9cdf8;
    }

    .section-card {
      min-height: 322px;
      padding: 0;
    }

    .section-card.short {
      min-height: 250px;
    }

    .section-card.wide {
      grid-column: 1 / -1;
      min-height: 198px;
    }

    .section-title {
      margin: 0;
      min-height: 66px;
      display: flex;
      align-items: center;
      padding: 0 24px;
      font-size: 17px;
      font-weight: 900;
      border-bottom: 1px solid transparent;
    }

    .operator-brief {
      grid-column: 1 / -1;
      padding: 18px;
      display: grid;
      grid-template-columns: minmax(0, 1.2fr) repeat(2, minmax(220px, 0.7fr));
      gap: 12px;
      align-items: stretch;
    }

    .brief-item {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcff;
      padding: 14px;
      display: grid;
      gap: 8px;
      min-width: 0;
    }

    .brief-item.primary {
      border-color: #bfdbfe;
      background: #eff6ff;
    }

    .brief-item.warn {
      border-color: #fed7aa;
      background: #fff7ed;
    }

    .brief-item.done {
      border-color: #a7f3d0;
      background: #ecfdf5;
    }

    .brief-kicker {
      color: var(--muted);
      font-size: 11px;
      font-weight: 900;
      text-transform: uppercase;
    }

    .brief-item strong {
      color: #102033;
      font-size: 15px;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }

    .brief-item p {
      margin: 0;
      color: #516079;
      font-size: 13px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }

    .brief-item a {
      width: fit-content;
      color: var(--blue);
      font-size: 13px;
      font-weight: 900;
      text-decoration: none;
    }

    .list-skeleton {
      display: grid;
    }

    .status-row {
      min-height: 49px;
      display: grid;
      grid-template-columns: 24px minmax(0, 1fr) 44px;
      gap: 16px;
      align-items: center;
      padding: 0 24px;
      border-top: 1px solid var(--line-soft);
    }

    .dot {
      width: 20px;
      height: 20px;
      border-radius: 50%;
      background: #dfe5ee;
    }

    .chart-area {
      height: 244px;
      margin: 0 26px 20px;
      position: relative;
      display: grid;
      align-content: stretch;
      padding: 6px 0 48px;
    }

    .dash-line {
      border-top: 1px dashed #dfe5ee;
    }

    .chart-center {
      position: absolute;
      left: 50%;
      top: 48%;
      transform: translate(-50%, -50%);
      width: 48px;
      height: 48px;
      border-radius: 50%;
      display: grid;
      place-items: center;
      background: rgba(237, 242, 249, 0.9);
      color: #b9c4d5;
    }

    .chart-center svg {
      width: 22px;
      height: 22px;
    }

    .chart-legend {
      position: absolute;
      left: 10px;
      right: 10px;
      bottom: 12px;
      display: flex;
      justify-content: space-around;
      gap: 18px;
    }

    .donut-wrap {
      display: grid;
      grid-template-columns: 160px minmax(0, 1fr);
      gap: 34px;
      align-items: center;
      padding: 0 28px;
      min-height: 168px;
    }

    .donut {
      width: 138px;
      height: 138px;
      border-radius: 50%;
      background: conic-gradient(#dfe4ec 0 8%, #f3f5f8 8% 33%, #dfe4ec 33% 66%, #eef1f6 66% 100%);
      position: relative;
    }

    .donut::after {
      content: "";
      position: absolute;
      inset: 43px;
      border-radius: 50%;
      background: #ffffff;
    }

    .summary-lines {
      display: grid;
      gap: 14px;
    }

    .summary-line {
      display: grid;
      grid-template-columns: 12px minmax(0, 1fr);
      gap: 12px;
      align-items: center;
    }

    .summary-dot {
      width: 12px;
      height: 12px;
      border-radius: 50%;
      background: #dfe5ee;
    }

    .mini-strip {
      margin: 20px 16px 0;
      min-height: 64px;
      border: 1px solid var(--line);
      border-radius: 8px;
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 20px;
      padding: 17px;
    }

    .table-skeleton {
      display: grid;
      border-top: 1px solid var(--line-soft);
    }

    .table-row {
      min-height: 43px;
      display: grid;
      grid-template-columns: 28px 1fr 0.65fr 0.75fr 0.55fr;
      gap: 18px;
      align-items: center;
      padding: 0 22px;
      border-bottom: 1px solid var(--line-soft);
    }

    .timeline {
      position: relative;
      margin: 10px 24px 0;
      padding: 0;
      list-style: none;
      display: grid;
      gap: 19px;
      min-width: 0;
      max-width: calc(100% - 48px);
    }

    .timeline::before {
      content: "";
      position: absolute;
      left: 5px;
      top: 6px;
      bottom: 6px;
      width: 1px;
      background: #dde5ef;
    }

    .timeline li {
      display: grid;
      grid-template-columns: 12px 58px minmax(0, 1fr) 50px;
      gap: 16px;
      align-items: center;
      position: relative;
      min-height: 18px;
      min-width: 0;
      max-width: 100%;
    }

    .event-dot {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: #76a7ff;
      z-index: 1;
    }

    .event-dot.warn { background: #ffd39c; }
    .event-dot.ok { background: #abe9cd; }

    .bot-summary {
      padding: 0 18px 18px;
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 18px;
    }

    .summary-tile {
      min-height: 74px;
      border: 1px solid var(--line);
      border-radius: 8px;
      display: flex;
      align-items: center;
      gap: 18px;
      padding: 14px;
    }

    .log-lines {
      padding: 0 24px 20px;
      display: grid;
      gap: 16px;
    }

    .log-line {
      display: grid;
      grid-template-columns: 16px minmax(0, 1fr) 48px;
      gap: 16px;
      align-items: center;
    }

    .log-menu {
      margin-left: auto;
      color: #8aa0bc;
    }

    .empty-view {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      gap: 20px;
    }

    .user-view {
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 20px;
    }

    .data-panel {
      padding: 24px;
    }

    .primary-panel {
      min-height: 260px;
    }

    .live-grid {
      display: grid;
      grid-template-columns: minmax(0, 1.04fr) minmax(320px, 0.76fr);
      gap: 20px;
      align-items: start;
    }

    .live-panel {
      min-height: 280px;
    }

    .live-wide {
      grid-column: 1 / -1;
    }

    .live-status-card {
      border: 1px solid #fed7aa;
      background: #fff7ed;
    }

    .live-status-card.done {
      border-color: #a7f3d0;
      background: #ecfdf5;
    }

    .live-status-card.blocked {
      border-color: #fecaca;
      background: #fff1f2;
    }

    .live-hero {
      display: grid;
      gap: 14px;
    }

    .live-hero strong {
      color: #101828;
      font-size: 22px;
      line-height: 1.25;
    }

    .live-summary {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
    }

    .live-summary-item {
      min-height: 82px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.72);
      padding: 12px;
      display: grid;
      gap: 6px;
      align-content: center;
    }

    .live-summary-item span {
      color: var(--muted);
      font-size: 12px;
      font-weight: 900;
    }

    .live-summary-item strong {
      font-size: 24px;
      line-height: 1;
    }

    .live-check-list {
      display: grid;
      gap: 10px;
    }

    .live-check {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #ffffff;
      padding: 14px;
      display: grid;
      grid-template-columns: 28px minmax(0, 1fr) auto;
      gap: 12px;
      align-items: start;
    }

    .live-check strong {
      display: block;
      font-size: 14px;
      line-height: 1.3;
    }

    .live-check p {
      margin: 4px 0 0;
      color: #516079;
      font-size: 13px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }

    .live-dot {
      width: 16px;
      height: 16px;
      border-radius: 50%;
      margin-top: 3px;
      background: #93c5fd;
    }

    .live-dot.done { background: #34d399; }
    .live-dot.warn { background: #f59e0b; }
    .live-dot.blocked { background: #ef4444; }

    .live-disabled-box {
      border: 1px solid #fecaca;
      border-radius: 8px;
      background: #fff1f2;
      padding: 16px;
      display: grid;
      gap: 12px;
    }

    .live-disabled-box .btn:disabled {
      justify-content: center;
      cursor: not-allowed;
      opacity: 0.72;
      box-shadow: none;
    }

    .live-disabled-box ul {
      margin: 0;
      padding-left: 18px;
      color: #516079;
      font-size: 13px;
      line-height: 1.55;
    }

    .panel-heading {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 18px;
      margin-bottom: 18px;
    }

    .panel-heading h2 {
      margin-bottom: 0;
    }

    .eyebrow {
      margin: 0 0 6px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 900;
    }

    .panel-copy {
      margin: -6px 0 16px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.55;
    }

    .data-panel h2 {
      margin: 0 0 16px;
      font-size: 18px;
    }

    .data-table {
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: auto;
      background: #fff;
      max-height: 420px;
    }

    .data-table:empty {
      display: none;
    }

    .data-table table {
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
    }

    .data-table th {
      position: sticky;
      top: 0;
      background: #f8fafc;
      color: #475569;
      z-index: 1;
    }

    .data-table th,
    .data-table td {
      padding: 12px 14px;
      border-bottom: 1px solid var(--line-soft);
      text-align: left;
      font-size: 13px;
      word-break: break-word;
      overflow-wrap: anywhere;
    }

    .view-json {
      margin: 0;
      max-height: 420px;
      overflow: auto;
      border-radius: 8px;
      background: #101828;
      color: #d5deec;
      padding: 16px;
      font-size: 12px;
      white-space: pre;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }

    .hidden-json {
      display: none;
    }

    .settings-form {
      margin-top: 12px;
      display: grid;
      gap: 12px;
    }

    .settings-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }

    .settings-field {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      display: grid;
      gap: 8px;
      min-width: 0;
      background: #fbfcff;
    }

    .settings-label {
      margin: 0;
      color: var(--text);
      font-size: 13px;
      font-weight: 800;
      line-height: 1.3;
    }

    .field-help {
      margin-left: 6px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 16px;
      height: 16px;
      border-radius: 999px;
      border: 1px solid #cbd5e1;
      background: #f8fafc;
      color: #475569;
      font-size: 11px;
      font-weight: 900;
      cursor: help;
    }

    .settings-helper {
      margin: 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
    }

    .confirmation-token {
      display: inline-flex;
      align-items: center;
      width: fit-content;
      max-width: 100%;
      border: 1px solid #bfdbfe;
      border-radius: 8px;
      background: #eff6ff;
      color: #1d4ed8;
      padding: 8px 10px;
      font-size: 13px;
      font-weight: 900;
      overflow-wrap: anywhere;
    }

    .pilot-summary {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 8px;
      margin: 4px 0;
    }

    .pilot-summary.compact {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .pilot-item {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #ffffff;
      padding: 10px;
      min-width: 0;
      display: grid;
      gap: 4px;
    }

    .pilot-item span {
      color: var(--muted);
      font-size: 11px;
      font-weight: 800;
    }

    .pilot-item strong {
      color: #0f172a;
      font-size: 15px;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }

    .next-action-copy {
      border: 1px solid #bfdbfe;
      border-radius: 8px;
      background: #eff6ff;
      color: #1e3a8a;
      padding: 12px;
      font-size: 13px;
      line-height: 1.45;
      font-weight: 800;
    }

    .notification-panel {
      border: 1px solid #bfdbfe;
      border-radius: 10px;
      background: linear-gradient(135deg, #eff6ff, #ffffff);
      padding: 12px;
      display: grid;
      gap: 8px;
    }

    .notification-panel .settings-actions {
      margin-top: 0;
    }

    .notification-toast-stack {
      position: fixed;
      right: 18px;
      bottom: 86px;
      z-index: 80;
      display: grid;
      gap: 8px;
      width: min(360px, calc(100vw - 32px));
      pointer-events: none;
    }

    .notification-toast {
      border: 1px solid #bfdbfe;
      border-left: 5px solid var(--blue);
      border-radius: 10px;
      background: #ffffff;
      box-shadow: 0 18px 42px rgba(15, 23, 42, 0.18);
      padding: 12px;
      display: grid;
      gap: 4px;
      pointer-events: auto;
      animation: toast-in 180ms ease-out;
    }

    .notification-toast.warn {
      border-color: #fed7aa;
      border-left-color: #f59e0b;
    }

    .notification-toast.error {
      border-color: #fecaca;
      border-left-color: #ef4444;
    }

    .notification-toast strong {
      color: #0f172a;
      font-size: 13px;
      line-height: 1.3;
    }

    .notification-toast p {
      margin: 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
    }

    @keyframes toast-in {
      from {
        opacity: 0;
        transform: translateY(8px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
      }
    }

    .setup-flow {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
      margin: 4px 0 8px;
    }

    .setup-step {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #f8fafc;
      padding: 10px;
      display: grid;
      gap: 6px;
      min-width: 0;
    }

    .setup-step strong {
      color: #0f172a;
      font-size: 13px;
      line-height: 1.3;
    }

    .setup-step p {
      margin: 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
    }

    .setup-step.done {
      border-color: #bbf7d0;
      background: #f0fdf4;
    }

    .setup-step.warn {
      border-color: #fed7aa;
      background: #fff7ed;
    }

    .setup-step.idle {
      border-color: var(--line);
      background: #f8fafc;
    }

    @media (max-width: 820px) {
      .setup-flow {
        grid-template-columns: 1fr;
      }
      .notification-toast-stack {
        right: 10px;
        bottom: 72px;
        width: calc(100vw - 20px);
      }
    }

    .settings-detail {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcff;
      overflow: hidden;
    }

    .settings-detail summary {
      cursor: pointer;
      list-style: none;
      padding: 12px;
      color: var(--blue);
      font-size: 13px;
      font-weight: 800;
      user-select: none;
    }

    .settings-detail summary::-webkit-details-marker {
      display: none;
    }

    .settings-detail summary::after {
      content: "+";
      float: right;
      color: var(--muted);
      font-weight: 900;
    }

    .settings-detail[open] summary::after {
      content: "-";
    }

    .settings-detail-body {
      display: grid;
      gap: 10px;
      padding: 0 12px 12px;
    }

    .settings-row {
      display: grid;
      gap: 7px;
      min-width: 0;
    }

    .settings-row.inline-input {
      align-items: end;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
    }

    .settings-inline {
      display: flex;
      gap: 8px;
      align-items: center;
      min-width: 0;
    }

    .settings-inline input {
      min-width: 0;
      width: min(9rem, 100%);
    }

    .settings-inline .unit {
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }

    .settings-field input[type="number"],
    .settings-field input[type="password"],
    .settings-field input[type="range"],
    .settings-field input[type="text"] {
      width: 100%;
      border-radius: 6px;
      border: 1px solid #d2deec;
      background: #ffffff;
      color: var(--text);
      padding: 9px 10px;
      font-size: 14px;
      min-width: 0;
    }

    .settings-field input[type="number"],
    .settings-field input[type="password"],
    .settings-field input[type="text"] {
      height: 36px;
    }

    .credential-status {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      margin-top: 2px;
    }

    .settings-slider-box {
      display: grid;
      gap: 8px;
      min-width: 0;
    }

    .settings-actions {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
    }

    .settings-status {
      margin: 0;
      min-height: 20px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
    }

    .settings-status.ok {
      color: #166534;
    }

    .settings-status.error {
      color: #b91c1c;
    }

    .developer-hint {
      margin: 4px 0 0;
      color: #6b7280;
      font-size: 11px;
      word-break: break-word;
      overflow-wrap: anywhere;
    }

    .empty-state {
      min-height: 154px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: linear-gradient(180deg, #fbfcff, #ffffff);
      display: flex;
      align-items: center;
      gap: 18px;
      padding: 22px;
    }

    .empty-state .icon-tile {
      width: 52px;
      height: 52px;
      border-radius: 50%;
    }

    .empty-state strong {
      display: block;
      margin-bottom: 6px;
      font-size: 15px;
    }

    .empty-state p {
      margin: 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.55;
    }

    .empty-state a {
      color: var(--blue);
      font-weight: 900;
      text-decoration: none;
    }

    .event-cards {
      border: 0;
      display: grid;
      gap: 10px;
      max-height: none;
      background: transparent;
      overflow: visible;
    }

    .event-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #ffffff;
      padding: 14px 16px;
      display: grid;
      grid-template-columns: minmax(68px, 84px) minmax(0, 1fr) minmax(72px, 94px);
      gap: 16px;
      align-items: start;
      min-width: 0;
      max-width: 100%;
    }

    .event-card > * {
      min-width: 0;
    }

    .event-card strong {
      display: block;
      margin-bottom: 4px;
      font-size: 13px;
      overflow-wrap: anywhere;
    }

    .event-card p {
      margin: 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
      overflow-wrap: anywhere;
      word-break: break-word;
    }

    .event-card .helper-text {
      justify-self: end;
      max-width: 100%;
      text-align: right;
      white-space: normal;
      overflow-wrap: anywhere;
    }

    .level-badge {
      width: fit-content;
      border-radius: 999px;
      padding: 4px 9px;
      background: #ecfdf5;
      color: #047857;
      font-size: 11px;
      font-weight: 900;
    }

    .level-badge.warn,
    .level-badge.error {
      background: #fff7ed;
      color: #b45309;
    }

    .endpoint-list {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }

    .endpoint-list button {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 9px 12px;
      cursor: pointer;
    }

    .status-copy,
    .blocker-list {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcff;
      padding: 14px;
      color: #516079;
      font-size: 13px;
      line-height: 1.55;
      word-break: break-word;
    }

    .action-list {
      list-style: none;
      padding: 0;
      margin: 0;
      display: grid;
      gap: 12px;
    }

    .action-list li {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 14px;
    }

    .status-pill {
      display: inline-flex;
      align-items: center;
      width: fit-content;
      border-radius: 999px;
      border: 1px solid #bfdbfe;
      background: #eff6ff;
      color: #1d4ed8;
      padding: 4px 10px;
      font-size: 12px;
      font-weight: 800;
    }

    .status-pill.warn {
      background: #fff7ed;
      border-color: #fed7aa;
      color: #b45309;
    }

    .status-pill.done {
      background: #ecfdf5;
      border-color: #a7f3d0;
      color: #047857;
    }

    .status-pill.blocked {
      background: #fff1f2;
      border-color: #fecdd3;
      color: #be123c;
    }

    .metric-value {
      display: block;
      margin: 4px 0 8px;
      font-size: 28px;
      line-height: 1;
      font-weight: 900;
      color: #0f172a;
    }

    .metric-note,
    .helper-text {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
    }

    .info-list {
      display: grid;
      gap: 0;
    }

    .info-row {
      min-height: 52px;
      display: grid;
      grid-template-columns: 12px minmax(0, 1fr) auto;
      gap: 14px;
      align-items: center;
      padding: 0 24px;
      border-top: 1px solid var(--line-soft);
    }

    .info-row strong,
    .event-line strong {
      color: #1f2a44;
      font-size: 13px;
    }

    .info-row span,
    .event-line span {
      min-width: 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }

    .next-step {
      margin: 14px 20px 20px;
      border: 1px solid #fed7aa;
      border-radius: 8px;
      background: #fff7ed;
      padding: 14px;
      display: grid;
      gap: 8px;
    }

    .next-step.blocked {
      border-color: #fecdd3;
      background: #fef2f2;
    }

    .next-step.done {
      border-color: #a7f3d0;
      background: #ecfdf5;
    }

    .next-step strong {
      color: #1f2a44;
      font-size: 13px;
    }

    .next-step p {
      margin: 0;
      color: #516079;
      font-size: 13px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }

    .next-step a {
      width: fit-content;
      color: var(--blue);
      font-size: 13px;
      font-weight: 900;
      text-decoration: none;
    }

    .summary-stack {
      padding: 0 24px 22px;
      display: grid;
      gap: 12px;
    }

    .summary-chip {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcff;
      padding: 12px 14px;
      display: grid;
      gap: 4px;
      min-width: 0;
    }

    .summary-chip strong {
      font-size: 13px;
    }

    .event-line {
      display: grid;
      grid-template-columns: 12px 52px minmax(0, 1fr) 92px;
      gap: 14px;
      align-items: center;
      min-height: 32px;
      min-width: 0;
      max-width: 100%;
    }

    .event-line > * {
      min-width: 0;
    }

    .event-line .event-dot {
      align-self: center;
    }

    .event-line .helper-text {
      font-size: 11px;
      justify-self: end;
      text-align: right;
      white-space: normal;
      overflow-wrap: anywhere;
    }

    .sr-data {
      position: absolute;
      left: -10000px;
      width: 1px;
      height: 1px;
      overflow: hidden;
    }

    .bottom-nav {
      display: none;
    }

    @media (max-width: 1100px) {
      .stat-row {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .operator-brief {
        grid-template-columns: 1fr;
      }

      .dashboard-grid,
      .empty-view,
      .live-grid {
        grid-template-columns: 1fr;
      }

      .settings-grid {
        grid-template-columns: 1fr;
      }

      .pilot-summary {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .section-card,
      .section-card.short {
        min-height: auto;
      }
    }

    @media (max-width: 820px) {
      .app {
        grid-template-rows: auto 1fr;
      }

      .topbar {
        min-height: 76px;
        padding: 14px 14px;
        align-items: flex-start;
      }

      .brand-title {
        display: grid;
        gap: 4px;
      }

      .top-actions {
        display: none;
      }

      .shell {
        grid-template-columns: 1fr;
      }

      .sidebar {
        display: none;
      }

      .main {
        padding: 12px 12px 92px;
      }

      .stat-row {
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 10px;
      }

      .stat-row > .stat-card:first-child {
        grid-column: 1 / -1;
      }

      .stat-card {
        min-height: 112px;
        padding: 16px;
        align-items: flex-start;
        flex-direction: column;
      }

      .stat-card.compact {
        gap: 12px;
      }

      .pilot-summary,
      .pilot-summary.compact {
        grid-template-columns: 1fr;
      }

      .icon-tile,
      .icon-tile.small {
        width: 42px;
        height: 42px;
        border-radius: 8px;
      }

      .stat-value {
        font-size: 24px;
      }

      .section-title {
        min-height: 54px;
        padding: 0 16px;
      }

      .status-row,
      .table-row {
        padding: 0 16px;
        gap: 10px;
      }

      .timeline {
        margin: 8px 16px 18px;
        max-width: calc(100% - 32px);
      }

      .event-line {
        grid-template-columns: 12px 44px minmax(0, 1fr);
      }

      .event-line .helper-text {
        grid-column: 3;
        justify-self: start;
        text-align: left;
      }

      .event-card {
        grid-template-columns: 1fr;
        gap: 8px;
      }

      .event-card .helper-text {
        justify-self: start;
        text-align: left;
      }

      .chart-area {
        margin: 0 18px 18px;
      }

      .donut-wrap {
        grid-template-columns: 1fr;
        justify-items: center;
        gap: 18px;
      }

      .mini-strip,
      .bot-summary {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .bottom-nav {
        position: fixed;
        left: max(8px, env(safe-area-inset-left));
        right: max(8px, env(safe-area-inset-right));
        width: auto;
        max-width: none;
        bottom: calc(12px + env(safe-area-inset-bottom));
        z-index: 20;
        display: grid;
        grid-template-columns: repeat(6, minmax(0, 1fr));
        gap: 4px;
        padding: 8px 6px;
        background: rgba(255, 255, 255, 0.96);
        border: 1px solid rgba(226, 232, 240, 0.92);
        border-radius: 999px;
        box-shadow: 0 18px 45px rgba(15, 23, 42, 0.16);
        backdrop-filter: blur(18px);
        -webkit-backdrop-filter: blur(18px);
        justify-self: stretch;
      }

      .bottom-nav a {
        min-width: 0;
        min-height: 48px;
        border-radius: 8px;
        color: #8492aa;
        text-decoration: none;
        display: grid;
        place-items: center;
        gap: 2px;
        font-size: 10px;
        font-weight: 800;
      }

      .bottom-nav a.active {
        color: var(--blue);
        background: transparent;
      }

      .bottom-nav a svg {
        width: 26px;
        height: 26px;
        padding: 5px;
        border-radius: 999px;
        box-sizing: border-box;
        transition: background 160ms ease, box-shadow 160ms ease, color 160ms ease;
      }

      .bottom-nav a.active svg {
        background: #edf4ff;
        box-shadow: 0 8px 18px rgba(37, 99, 235, 0.14);
      }
    }
  </style>
</head>
<body>
  <div class="app">
    <header class="topbar">
      <div class="brand">
        <img class="logo" src="/assets/toss-symbol.png" alt="Toss logo" loading="eager" decoding="async">
        <div class="brand-title">
          <strong>Toss Turtle Bot</strong>
          <span class="execution-badge">실주문 비활성</span>
        </div>
      </div>
      <div class="top-actions">
        <div class="clock-line" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle cx="12" cy="12" r="9"></circle><path d="M12 7v5l3 2"></path></svg>
          <span id="dashboard-clock" class="clock-text">현재 --:--:--</span>
        </div>
        <button class="btn" type="button" id="refresh-button">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M21 12a9 9 0 0 1-9 9 8.7 8.7 0 0 1-6-2.3"></path><path d="M3 12a9 9 0 0 1 15-6.7"></path><path d="M3 19v-5h5"></path><path d="M21 5v5h-5"></path></svg>
          새로고침
        </button>
      </div>
    </header>

    <div class="shell">
      <aside class="sidebar">
        <nav class="nav" aria-label="Dashboard sections">
          <a class="active" href="#dashboard" data-view="dashboard"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><rect x="3" y="3" width="7" height="7"></rect><rect x="14" y="3" width="7" height="7"></rect><rect x="3" y="14" width="7" height="7"></rect><rect x="14" y="14" width="7" height="7"></rect></svg>대시보드</a>
          <a href="#watchlist" data-view="watchlist"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="m4 17 5-5 4 4 7-8"></path><path d="M16 8h4v4"></path></svg>관심</a>
          <a href="#positions" data-view="positions"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M4 19V7a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v12"></path><path d="M8 5V3h8v2"></path><path d="M4 11h16"></path></svg>포지션</a>
          <a href="#orders" data-view="orders"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><rect x="7" y="3" width="10" height="18" rx="2"></rect><path d="M10 8h4"></path><path d="M10 12h4"></path><path d="M10 16h2"></path></svg>주문</a>
          <a href="#live" data-view="live"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M12 3 5 6v6c0 4 3 7 7 9 4-2 7-5 7-9V6l-7-3Z"></path><path d="m9 12 2 2 4-5"></path></svg>실거래</a>
          <a href="#events" data-view="events"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><rect x="7" y="3" width="10" height="18" rx="2"></rect><path d="M10 8h4"></path><path d="M10 12h4"></path><path d="M10 16h2"></path></svg>이벤트</a>
          <a href="#settings" data-view="settings"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 0 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6V21a2 2 0 0 1-4 0v-.1a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 0 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.6-1H3a2 2 0 0 1 0-4h.1a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 0 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3 1.7 1.7 0 0 0 1-1.6V3a2 2 0 0 1 4 0v.1a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 0 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.1a2 2 0 0 1 0 4h-.1a1.7 1.7 0 0 0-1.6 1Z"></path></svg>설정</a>
        </nav>
        <section class="sidebar-status" aria-label="현재 운영 상태">
          <strong id="sidebar-ready">상태 확인 중</strong>
          <p id="sidebar-mode">모드: -</p>
          <p id="sidebar-blockers">차단 항목: -</p>
          <a class="sidebar-action" href="#settings" data-view="settings">설정 확인</a>
        </section>
      </aside>

      <main class="main">
        <section id="view-dashboard" class="view active" data-view="dashboard">
          <div class="dashboard-grid">
            <section class="stat-row">
              <article class="card stat-card">
                <div class="icon-tile"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="m12 3 7 4v10l-7 4-7-4V7l7-4Z"></path><path d="M12 8v8"></path><path d="m9 10 3-2 3 2"></path></svg></div>
                <div class="stat-lines">
                  <span class="ghost-line" style="width:124px"></span>
                  <span class="ghost-line" style="width:74px"></span>
                </div>
                <span class="pill-ghost"></span>
              </article>
              <article class="card stat-card compact">
                <div class="icon-tile small"><svg viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9L12 3Z"></path></svg></div>
                <div>
                  <p class="stat-label">관심 종목</p>
                  <span class="ghost-line" style="width:52px"></span>
                  <span class="ghost-line" style="width:82px;margin-top:12px;display:block"></span>
                </div>
              </article>
              <article class="card stat-card compact">
                <div class="icon-tile small"><svg viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M13 2a10 10 0 1 0 9 11h-9V2Z"></path><path d="M15 2.2V9h6.8A10 10 0 0 0 15 2.2Z"></path></svg></div>
                <div>
                  <p class="stat-label">보유 포지션</p>
                  <span class="ghost-line" style="width:52px"></span>
                  <span class="ghost-line" style="width:82px;margin-top:12px;display:block"></span>
                </div>
              </article>
              <article class="card stat-card compact">
                <div class="icon-tile small"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M8 4h8v16H8z"></path><path d="M10 8h4"></path><path d="M10 12h4"></path><path d="M10 16h2"></path></svg></div>
                <div>
                  <p class="stat-label">미체결 주문</p>
                  <span class="ghost-line" style="width:52px"></span>
                  <span class="ghost-line" style="width:82px;margin-top:12px;display:block"></span>
                </div>
              </article>
              <article class="card stat-card compact">
                <div class="icon-tile small"><svg viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M12 22a2.5 2.5 0 0 0 2.4-1.8H9.6A2.5 2.5 0 0 0 12 22ZM18 16v-5a6 6 0 1 0-12 0v5l-2 2v1h16v-1l-2-2Z"></path></svg></div>
                <div>
                  <p class="stat-label">총 이벤트</p>
                  <span class="ghost-line" style="width:52px"></span>
                  <span class="ghost-line" style="width:82px;margin-top:12px;display:block"></span>
                </div>
              </article>
            </section>

            <section id="dashboard-operator-brief" class="card operator-brief" aria-label="운영 요약">
              <div class="brief-item primary">
                <span class="brief-kicker">우선 확인</span>
                <strong>상태를 불러오는 중</strong>
                <p>현재 설정과 최근 이벤트를 확인하고 있습니다.</p>
              </div>
              <div class="brief-item">
                <span class="brief-kicker">최근 기록</span>
                <strong>-</strong>
                <p>이벤트가 들어오면 여기에 마지막 기록이 표시됩니다.</p>
              </div>
              <div class="brief-item">
                <span class="brief-kicker">데이터</span>
                <strong>-</strong>
                <p>관심 종목, 포지션, 주문 수를 요약합니다.</p>
              </div>
            </section>

            <article class="card section-card">
              <h2 class="section-title">시스템 상태</h2>
              <div id="dashboard-health-list" class="list-skeleton">
                <div class="status-row"><span class="dot"></span><span class="ghost-line" style="width:74%"></span><span class="ghost-line"></span></div>
                <div class="status-row"><span class="dot"></span><span class="ghost-line" style="width:74%"></span><span class="ghost-line"></span></div>
                <div class="status-row"><span class="dot"></span><span class="ghost-line" style="width:74%"></span><span class="ghost-line"></span></div>
                <div class="status-row"><span class="dot"></span><span class="ghost-line" style="width:74%"></span><span class="ghost-line"></span></div>
                <div class="status-row"><span class="dot"></span><span class="ghost-line" style="width:74%"></span><span class="ghost-line"></span></div>
              </div>
            </article>

            <article class="card section-card">
              <h2 class="section-title">관심 종목</h2>
              <div class="chart-area">
                <span class="dash-line"></span>
                <span class="dash-line"></span>
                <span class="dash-line"></span>
                <span class="dash-line"></span>
                <div class="chart-center"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="m4 17 5-5 4 4 7-8"></path><path d="M16 8h4v4"></path></svg></div>
                <div class="chart-legend">
                  <span class="ghost-line" style="width:45px"></span>
                  <span class="ghost-line" style="width:45px"></span>
                  <span class="ghost-line" style="width:45px"></span>
                  <span class="ghost-line" style="width:45px"></span>
                  <span class="ghost-line" style="width:45px"></span>
                </div>
              </div>
            </article>

            <article class="card section-card">
              <h2 class="section-title">보유 포지션</h2>
              <div class="donut-wrap">
                <div class="donut"></div>
                <div class="summary-lines">
                  <span class="ghost-line" style="width:78%"></span>
                  <span class="ghost-line" style="width:44%"></span>
                  <span style="height:14px"></span>
                  <div class="summary-line"><span class="summary-dot"></span><span class="ghost-line" style="width:64%"></span></div>
                  <div class="summary-line"><span class="summary-dot"></span><span class="ghost-line" style="width:54%"></span></div>
                </div>
              </div>
              <div class="mini-strip">
                <span class="ghost-line"></span><span class="ghost-line"></span><span class="ghost-line"></span><span class="ghost-line"></span>
                <span class="ghost-line"></span><span class="ghost-line"></span><span class="ghost-line"></span><span class="ghost-line"></span>
              </div>
            </article>

            <article class="card section-card short">
              <h2 class="section-title">미체결 주문</h2>
              <div id="dashboard-open-orders-table" class="table-skeleton">
                <div class="table-row"><span class="dot"></span><span class="ghost-line"></span><span class="ghost-line"></span><span class="ghost-line"></span><span class="ghost-line"></span></div>
                <div class="table-row"><span class="dot"></span><span class="ghost-line"></span><span class="ghost-line"></span><span class="ghost-line"></span><span class="ghost-line"></span></div>
                <div class="table-row"><span class="dot"></span><span class="ghost-line"></span><span class="ghost-line"></span><span class="ghost-line"></span><span class="ghost-line"></span></div>
              </div>
            </article>

            <article class="card section-card short">
              <h2 class="section-title">최근 이벤트</h2>
              <ul id="dashboard-events-timeline" class="timeline">
                <li><span class="event-dot"></span><span class="ghost-line"></span><span class="ghost-line"></span><span class="ghost-line"></span></li>
                <li><span class="event-dot warn"></span><span class="ghost-line"></span><span class="ghost-line" style="width:86%"></span><span class="ghost-line"></span></li>
                <li><span class="event-dot ok"></span><span class="ghost-line"></span><span class="ghost-line" style="width:66%"></span><span class="ghost-line"></span></li>
                <li><span class="event-dot"></span><span class="ghost-line"></span><span class="ghost-line" style="width:92%"></span><span class="ghost-line"></span></li>
              </ul>
            </article>

            <article class="card section-card short">
              <h2 class="section-title">봇 요약</h2>
              <div class="bot-summary">
                <div class="summary-tile"><div class="icon-tile small"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle cx="12" cy="12" r="9"></circle><path d="M12 7v5l3 2"></path></svg></div><div class="stat-lines"><span class="ghost-line"></span><span class="ghost-line" style="width:60%"></span></div></div>
                <div class="summary-tile"><div class="icon-tile small"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="m4 17 5-5 4 4 7-8"></path><path d="M16 8h4v4"></path></svg></div><div class="stat-lines"><span class="ghost-line"></span><span class="ghost-line" style="width:60%"></span></div></div>
                <div class="summary-tile"><div class="icon-tile small"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M12 3 5 6v6c0 4 3 7 7 9 4-2 7-5 7-9V6l-7-3Z"></path></svg></div><div class="stat-lines"><span class="ghost-line"></span><span class="ghost-line" style="width:60%"></span></div></div>
                <div class="summary-tile"><div class="icon-tile small"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M4 7h16"></path><path d="M4 12h16"></path><path d="M4 17h16"></path></svg></div><div class="stat-lines"><span class="ghost-line"></span><span class="ghost-line" style="width:60%"></span></div></div>
              </div>
            </article>

            <article class="card section-card wide">
              <h2 class="section-title">실시간 로그 <span class="log-menu">⋮</span></h2>
              <div class="log-lines">
                <div class="log-line"><span class="dot"></span><span class="ghost-line"></span><span class="ghost-line"></span></div>
                <div class="log-line"><span class="dot"></span><span class="ghost-line"></span><span class="ghost-line"></span></div>
                <div class="log-line"><span class="dot"></span><span class="ghost-line"></span><span class="ghost-line"></span></div>
                <div class="log-line"><span class="dot"></span><span class="ghost-line"></span><span class="ghost-line"></span></div>
              </div>
            </article>
          </div>
        </section>

        <section id="view-watchlist" class="view" data-view="watchlist">
          <div class="user-view">
            <article class="card data-panel primary-panel">
              <div class="panel-heading">
                <div>
                  <p class="eyebrow">감시 대상</p>
                  <h2>관심 종목</h2>
                </div>
                <span id="watchlist-count-badge" class="status-pill">0개</span>
              </div>
              <div id="watchlist-table" class="data-table"></div>
            </article>
          </div>
        </section>

        <section id="view-positions" class="view" data-view="positions">
          <div class="user-view">
            <article class="card data-panel primary-panel">
              <div class="panel-heading">
                <div>
                  <p class="eyebrow">현재 보유</p>
                  <h2>포지션</h2>
                </div>
                <span id="positions-count-badge" class="status-pill">0개</span>
              </div>
              <div id="positions-table" class="data-table"></div>
            </article>
          </div>
        </section>

        <section id="view-orders" class="view" data-view="orders">
          <div class="user-view">
            <article class="card data-panel primary-panel">
              <div class="panel-heading">
                <div>
                  <p class="eyebrow">실주문 비활성</p>
                  <h2>주문</h2>
                </div>
                <span id="orders-count-badge" class="status-pill">0개</span>
              </div>
              <div id="orders-table" class="data-table"></div>
            </article>
          </div>
        </section>

        <section id="view-live" class="view" data-view="live">
          <div class="live-grid">
            <article id="live-readiness-card" class="card data-panel live-panel live-status-card blocked">
              <div class="panel-heading">
                <div>
                  <p class="eyebrow">실거래 전환</p>
                  <h2>실거래 준비도</h2>
                </div>
                <span id="live-readiness-status" class="status-pill blocked">확인 중</span>
              </div>
              <div class="live-hero">
                <strong id="live-readiness-headline">실거래 상태를 불러오는 중입니다</strong>
                <p id="live-readiness-copy" class="panel-copy">토스 키, 계좌, 시장 상태, 주문 후보가 준비됐는지 확인합니다.</p>
                <div id="live-readiness-summary" class="live-summary"></div>
              </div>
            </article>
            <article class="card data-panel live-panel">
              <h2>주문 제출 상태</h2>
              <div class="live-disabled-box">
                <strong>자동 안전 파일럿</strong>
                <p id="live-submit-reason" class="panel-copy">토스 키와 계좌가 준비되면 작은 한도로 자동 거래를 시작합니다.</p>
                <div class="pilot-summary compact" data-pilot-summary>
                  <div class="pilot-item"><span>거래 대상</span><strong data-pilot-field="symbol">-</strong></div>
                  <div class="pilot-item"><span>최대 수량</span><strong data-pilot-field="quantity">-</strong></div>
                  <div class="pilot-item"><span>하루 주문</span><strong data-pilot-field="daily-orders">-</strong></div>
                  <div class="pilot-item"><span>하루 금액</span><strong data-pilot-field="daily-amount">-</strong></div>
                  <div class="pilot-item"><span>실패 퓨즈</span><strong data-pilot-field="failure-fuse">-</strong></div>
                  <div class="pilot-item"><span>현재 상태</span><strong data-pilot-field="state">-</strong></div>
                </div>
                <button type="button" class="btn primary" id="safe-pilot-button">안전 파일럿 시작</button>
                <button type="button" class="btn" id="live-stop-button">거래 중지</button>
                <p id="safe-pilot-result" class="panel-copy"></p>
              <strong>1회만 시험 실행</strong>
              <p class="panel-copy">자동으로 계속 돌리지 않고 한 번만 확인할 때 사용합니다.</p>
              <p class="panel-copy">24시간 자동매매는 시작 버튼을 누른 뒤 정해둔 한도 안에서 계속 점검합니다. 주문마다 코드를 다시 입력하지 않습니다.</p>
              <label for="live-operator-id" class="settings-label">
                누가 눌렀는지 메모(선택)
                <span
                  class="field-help"
                  title="내부 기록용 메모입니다. 자동매매 허용 조건에는 쓰지 않습니다."
                  aria-label="누가 눌렀는지 메모용도 설명"
                >?</span>
              </label>
              <p class="panel-copy">누가 버튼을 눌렀는지 남기는 표시값입니다. 실거래 허용 판단에는 사용되지 않습니다.</p>
              <input id="live-operator-id" class="settings-input" type="text" autocomplete="off" placeholder="예: friend-a" />
              <span class="confirmation-token" id="live-once-confirmation-token">LIVE PILOT 실행</span>
              <input id="live-once-confirmation" class="settings-input" type="text" autocomplete="off" placeholder="위 문구를 그대로 입력" aria-describedby="live-once-confirmation-token" />
              <button type="button" class="btn primary" id="live-once-button">1회 실행</button>
                <p id="live-once-result" class="panel-copy"></p>
              <strong>실주문 연결 테스트</strong>
              <p class="panel-copy">전략 신호를 기다리지 않고 허용 종목 1주를 낮은 제한가로 주문한 뒤, 접수되면 바로 취소합니다.</p>
              <span class="confirmation-token" id="live-smoke-confirmation-token">실주문 테스트</span>
              <input id="live-smoke-confirmation" class="settings-input" type="text" autocomplete="off" placeholder="위 문구를 그대로 입력" aria-describedby="live-smoke-confirmation-token" />
              <button type="button" class="btn danger" id="live-smoke-test-button">실주문 연결 테스트</button>
              <p id="live-smoke-test-result" class="panel-copy"></p>
              <strong>Toss 허용 IP 확인</strong>
              <p class="panel-copy">맥북 컨테이너에서 실제 Toss로 나가는 공개 IP입니다. Toss 개발자센터 앱의 허용 IP에 이 값을 추가해야 실주문 요청이 통과합니다.</p>
              <button type="button" class="btn" id="live-public-ip-check-button">현재 공개 IP 확인</button>
              <button type="button" class="btn" id="live-public-ip-copy-button" disabled>IP 복사</button>
              <p id="live-public-ip-result" class="panel-copy"></p>
                <ul>
                  <li>안전 파일럿은 선택된 종목 1주, 하루 1건, 소액 한도로만 움직입니다</li>
                  <li>거래 중지를 누르면 새 주문을 막고 자동 실행을 멈춥니다</li>
                  <li>시험 모드에서는 주문 접수 확인 뒤 바로 취소 요청을 보냅니다</li>
                </ul>
              </div>
            </article>
            <article class="card data-panel live-panel live-wide">
              <div class="panel-heading">
                <div>
                  <p class="eyebrow">실사용 모니터</p>
                  <h2>계좌·주문 동기화</h2>
                </div>
                <span id="live-monitor-status" class="status-pill">확인 중</span>
              </div>
              <div id="live-monitor-grid" class="pilot-summary"></div>
              <div id="live-monitor-history" class="data-table"></div>
            </article>
            <article class="card data-panel live-panel live-wide">
              <div class="panel-heading">
                <div>
                  <p class="eyebrow">체크리스트</p>
                  <h2>확인할 것들</h2>
                </div>
                <span id="live-check-count" class="status-pill">0개</span>
              </div>
              <div id="live-readiness-checks" class="live-check-list"></div>
            </article>
          </div>
        </section>

        <section id="view-events" class="view" data-view="events">
          <div class="user-view">
            <article class="card data-panel primary-panel">
              <div class="panel-heading">
                <div>
                  <p class="eyebrow">최근 기록</p>
                  <h2>이벤트</h2>
                </div>
                <span id="events-count-badge" class="status-pill">0개</span>
              </div>
              <div id="events-table" class="data-table event-cards"></div>
            </article>
          </div>
        </section>

        <section id="view-settings" class="view" data-view="settings">
          <div class="empty-view">
            <article class="card data-panel">
              <h2>설정 안내</h2>
              <p id="settings-headline" class="status-copy">필수 입력을 끝내면 이 화면에서 바로 안전 파일럿을 시작할 수 있습니다.</p>
              <span id="safe-pilot-state-badge" class="status-pill blocked">파일럿 대기</span>
              <p id="safe-pilot-next-action" class="next-action-copy">필수 입력을 확인하는 중입니다.</p>
              <div class="setup-flow" id="setup-flow" aria-label="첫 설정 진행 단계">
                <div class="setup-step idle" data-setup-step="api">
                  <span class="status-pill todo" data-setup-status>대기</span>
                  <strong>1. 토스 키 입력</strong>
                  <p>앱 ID와 비밀키를 넣으면 첫 단계가 끝납니다.</p>
                </div>
                <div class="setup-step idle" data-setup-step="account">
                  <span class="status-pill todo" data-setup-status>대기</span>
                  <strong>2. 계좌 번호 입력</strong>
                  <p>거래에 쓸 토스 계좌 번호를 연결합니다.</p>
                </div>
                <div class="setup-step idle" data-setup-step="start">
                  <span class="status-pill todo" data-setup-status>대기</span>
                  <strong>3. 안전 파일럿 시작</strong>
                  <p>준비가 끝나면 작은 한도로 자동 실행합니다.</p>
                </div>
              </div>
              <div class="pilot-summary" data-pilot-summary>
                <div class="pilot-item"><span>거래 대상</span><strong data-pilot-field="symbol">-</strong></div>
                <div class="pilot-item"><span>최대 수량</span><strong data-pilot-field="quantity">-</strong></div>
                <div class="pilot-item"><span>하루 주문</span><strong data-pilot-field="daily-orders">-</strong></div>
                <div class="pilot-item"><span>하루 금액</span><strong data-pilot-field="daily-amount">-</strong></div>
                <div class="pilot-item"><span>실패 퓨즈</span><strong data-pilot-field="failure-fuse">-</strong></div>
                <div class="pilot-item"><span>현재 상태</span><strong data-pilot-field="state">-</strong></div>
              </div>
              <ul id="settings-onboarding-list" class="action-list"></ul>
              <div class="settings-actions">
                <button type="button" class="btn primary" id="onboarding-safe-pilot-button">안전 파일럿 시작</button>
                <button type="button" class="btn" id="onboarding-live-stop-button">거래 중지</button>
              </div>
              <p id="onboarding-live-action-result" class="panel-copy"></p>
            </article>
            <article class="card data-panel">
              <h2>토스 API / 계좌 연결</h2>
              <p class="panel-copy">토스에서 받은 앱 ID, 비밀키, 거래할 계좌 번호를 입력합니다. 비밀키는 저장 후 화면에 다시 보이지 않습니다.</p>
              <section class="settings-form">
                <div class="settings-grid">
                  <div class="settings-field">
                    <label for="toss-client-id" class="settings-label">토스 앱 ID</label>
                    <input id="toss-client-id" type="password" autocomplete="off" placeholder="토스 개발자센터에서 복사한 앱 ID" />
                    <p class="settings-helper">토스 개발자센터에서 앱을 만들면 나오는 ID입니다.</p>
                    <div class="credential-status">
                      <span id="toss-client-id-status" class="status-pill todo">미설정</span>
                    </div>
                  </div>
                  <div class="settings-field">
                    <label for="toss-client-secret" class="settings-label">토스 앱 비밀키</label>
                    <input id="toss-client-secret" type="password" autocomplete="off" placeholder="토스 개발자센터에서 복사한 비밀키" />
                    <p class="settings-helper">토스 연결에 필요한 비밀키입니다. 저장 후 다시 보여주지 않습니다.</p>
                    <div class="credential-status">
                      <span id="toss-client-secret-status" class="status-pill todo">미설정</span>
                    </div>
                  </div>
                  <div class="settings-field">
                    <label for="toss-account-seq" class="settings-label">연결할 토스 계좌 번호</label>
                    <input id="toss-account-seq" type="text" inputmode="numeric" autocomplete="off" placeholder="예: 7" />
                    <p class="settings-helper">토스 계좌 목록에서 보이는 accountSeq 번호를 넣습니다.</p>
                    <div class="credential-status">
                      <span id="toss-account-status" class="status-pill todo">미연결</span>
                    </div>
                  </div>
                  <div class="settings-field">
                    <label for="toss-account-alias" class="settings-label">계좌 별명</label>
                    <input id="toss-account-alias" type="text" autocomplete="off" placeholder="예: 정훈 미국주식 계좌" />
                    <p class="settings-helper">Discord 알림에 표시할 이름입니다. 비워두면 Tailscale 프로필 이름을 사용하고, 계좌번호는 보내지 않습니다.</p>
                  </div>
                  <div class="settings-field">
                    <label for="toss-identity-confirmation" class="settings-label">로컬 본인 확인</label>
                    <span class="confirmation-token" id="toss-identity-confirmation-token">토스 연결 승인</span>
                    <input id="toss-identity-confirmation" type="text" autocomplete="off" placeholder="위 문구를 그대로 입력" aria-describedby="toss-identity-confirmation-token" />
                    <p class="settings-helper">토스 API 키나 계좌 연결값을 저장하려면 위 문구를 그대로 입력하세요.</p>
                    <p class="settings-helper"><strong>키/계좌를 바꿨다면</strong> 이 확인 문구를 입력하고 아래의 설정 저장을 눌러야 적용됩니다.</p>
                  </div>
                </div>
                <div class="settings-actions">
                  <button id="toss-settings-save-button" type="button" class="btn primary" disabled>토스 설정 저장</button>
                  <p id="toss-settings-save-status" class="settings-status" role="status" aria-live="polite"></p>
                </div>
              </section>
              <section class="settings-form">
                <h3>Toss 허용 IP</h3>
                <p class="settings-helper">맥북 운영 환경은 윈도우 테스트와 달리 컨테이너에서 주문을 보냅니다. 아래 IP가 Toss 개발자센터 앱의 허용 IP에 들어 있어야 합니다.</p>
                <div class="settings-actions">
                  <button type="button" class="btn" id="settings-public-ip-check-button">현재 공개 IP 확인</button>
                  <button type="button" class="btn" id="settings-public-ip-copy-button" disabled>IP 복사</button>
                </div>
                <p id="settings-public-ip-result" class="settings-status" role="status" aria-live="polite"></p>
              </section>
            </article>
            <article class="card data-panel">
              <h2>실거래 중지 스위치</h2>
              <p class="panel-copy">언제든 누르면 새 주문을 막고 자동 실행을 멈춥니다.</p>
              <button type="button" class="btn" id="settings-live-stop-button">거래 중지</button>
              <p id="settings-live-stop-result" class="panel-copy"></p>
            </article>
            <article class="card data-panel">
              <h2>알림 설정</h2>
              <div class="notification-panel">
                <p id="notification-status" class="panel-copy">매수/매도 수량과 실패 알림만 보여드립니다.</p>
                <p class="settings-helper">브라우저 알림을 켜면 어떤 종목을 몇 주 주문했는지, 또는 주문이 왜 막혔는지만 알려드립니다. 브라우저나 폰에서 허용을 눌러야 작동합니다.</p>
                <div class="settings-actions">
                  <button type="button" class="btn" id="notification-enable-button">브라우저 알림 켜기</button>
                  <button type="button" class="btn" id="notification-test-button">테스트 알림</button>
                  <button type="button" class="btn" id="discord-notification-test-button">Discord 테스트 전송</button>
                </div>
              </div>
            </article>
            <article class="card data-panel">
              <h2>모멘텀 전략 설정</h2>
              <p class="panel-copy">자주 바꾸는 값만 먼저 보여줍니다. 전략 계산식에 가까운 값은 자세히 보기 안에 넣었습니다.</p>
              <section class="settings-form">
                <div class="settings-grid">
                  <div class="settings-field">
                    <label for="momentum-cash-reserve-percent-slider" class="settings-label">
                      현금 보유 비중
                    </label>
                    <div class="settings-slider-box">
                      <input
                        id="momentum-cash-reserve-percent-slider"
                        type="range"
                        min="0"
                        max="100"
                        step="0.1"
                      />
                      <div class="settings-inline">
                        <input
                          id="momentum-cash-reserve-percent"
                          type="number"
                          min="0"
                          max="100"
                          step="0.1"
                        />
                        <span class="unit">%</span>
                      </div>
                    </div>
                    <p id="momentum-max-exposure-preview" class="settings-helper"></p>
                  </div>
                  <div class="settings-field">
                    <label for="momentum-target-position-pct" class="settings-label">
                      종목당 매수 비중
                    </label>
                    <input id="momentum-target-position-pct" type="number" step="0.01" min="0" max="1" />
                    <p class="settings-helper">예: 0.10은 계좌의 10%씩 매수한다는 뜻입니다.</p>
                  </div>
                  <div class="settings-field">
                    <label for="momentum-max-positions" class="settings-label">최대 보유 종목 수</label>
                    <input id="momentum-max-positions" type="number" min="1" max="200" step="1" />
                    <p class="settings-helper">동시에 들고 있을 수 있는 종목 수입니다.</p>
                  </div>
                  <div class="settings-field">
                    <label for="momentum-accept-top-n" class="settings-label">하루 신규 진입 수</label>
                    <input id="momentum-accept-top-n" type="number" min="1" max="200" step="1" />
                    <p class="settings-helper">하루에 새로 살 수 있는 종목 수입니다.</p>
                  </div>
                </div>
                <details class="settings-detail">
                  <summary>자세히 보기</summary>
                  <div class="settings-detail-body settings-grid">
                  <div class="settings-field">
                    <label for="momentum-exit-ma-days" class="settings-label">청산 이동평균</label>
                    <input id="momentum-exit-ma-days" type="number" min="1" max="10000" step="1" />
                    <p class="settings-helper">가격이 이 기간의 평균선 아래로 내려가면 매도 후보로 봅니다.</p>
                  </div>
                  <div class="settings-field">
                    <label for="momentum-lookback-days" class="settings-label">모멘텀 기간</label>
                    <input id="momentum-lookback-days" type="number" min="1" max="10000" step="1" />
                    <p class="settings-helper">얼마나 긴 과거 수익률로 강한 종목을 고를지 정합니다.</p>
                  </div>
                  <div class="settings-field">
                    <label for="momentum-skip-days" class="settings-label">최근 제외 기간</label>
                    <input id="momentum-skip-days" type="number" min="0" max="10000" step="1" />
                    <p class="settings-helper">너무 최근의 급등락을 점수 계산에서 빼는 기간입니다.</p>
                  </div>
                  <div class="settings-field">
                    <label for="momentum-trend-ma-days" class="settings-label">시장 추세선</label>
                    <input id="momentum-trend-ma-days" type="number" min="1" max="10000" step="1" />
                    <p class="settings-helper">SPY가 이 평균선 아래에 있으면 시장이 약하다고 보고 진입을 막습니다.</p>
                  </div>
                  </div>
                </details>
                <div class="settings-actions">
                  <button id="settings-save-button" type="button" class="btn primary" disabled>설정 저장</button>
                  <p id="settings-save-status" class="settings-status" role="status" aria-live="polite"></p>
                </div>
                <p id="settings-save-hint" class="developer-hint"></p>
              </section>
            </article>
            <article class="card data-panel">
              <h2>확인할 항목</h2>
              <p class="panel-copy">원본 JSON 대신 사용자가 처리해야 할 항목만 보여줍니다.</p>
              <div id="settings-blockers-list" class="blocker-list"></div>
            </article>
          </div>
        </section>
        <p class="sr-data">Local dashboard only. Settings can be saved locally, but this interface never submits orders.</p>
      </main>
    </div>
  </div>

  <nav class="bottom-nav" aria-label="Mobile dashboard sections">
    <a class="active" href="#dashboard" data-view="dashboard"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><rect x="3" y="3" width="7" height="7"></rect><rect x="14" y="3" width="7" height="7"></rect><rect x="3" y="14" width="7" height="7"></rect><rect x="14" y="14" width="7" height="7"></rect></svg>대시보드</a>
    <a href="#watchlist" data-view="watchlist"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="m4 17 5-5 4 4 7-8"></path><path d="M16 8h4v4"></path></svg>관심 종목</a>
    <a href="#positions" data-view="positions"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M13 2.1a10 10 0 1 0 8.9 8.9H13V2.1Z"></path><path d="M15 2.1V9h6.9"></path></svg>포지션</a>
    <a href="#orders" data-view="orders"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><rect x="7" y="3" width="10" height="18" rx="2"></rect><path d="M10 8h4"></path><path d="M10 12h4"></path><path d="M10 16h2"></path></svg>주문</a>
    <a href="#live" data-view="live"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M12 3 5 6v6c0 4 3 7 7 9 4-2 7-5 7-9V6l-7-3Z"></path><path d="m9 12 2 2 4-5"></path></svg>실거래</a>
    <a href="#settings" data-view="settings"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 0 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6V21a2 2 0 0 1-4 0v-.1a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 0 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.6-1H3a2 2 0 0 1 0-4h.1a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 0 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3 1.7 1.7 0 0 0 1-1.6V3a2 2 0 0 1 4 0v.1a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 0 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.1a2 2 0 0 1 0 4h-.1a1.7 1.7 0 0 0-1.6 1Z"></path></svg>설정</a>
  </nav>

  <div id="notification-toast-stack" class="notification-toast-stack" aria-live="polite" aria-atomic="false"></div>

  <script>
    const ONBOARDING_STEPS = [
      {
        title: "Toss API 인증 정보",
        body: "토스 개발자센터에서 앱 ID와 비밀키를 발급받아 입력하세요.",
        group: "필수",
        match: (blocker) => blocker.includes("토스 API")
      },
      {
        title: "거래 계좌 연결",
        body: "거래에 사용할 계좌 번호를 입력하세요.",
        group: "필수",
        match: (blocker) => blocker.includes("계좌")
      },
      {
        title: "감시 종목 후보",
        body: "봇이 확인할 종목을 하나 이상 준비하세요.",
        group: "필수",
        match: (blocker) => blocker.includes("종목")
      },
      {
        title: "최근 점검 기록",
        body: "이벤트 탭에서 봇이 최근에 정상 점검했는지 확인하세요.",
        group: "확인",
        match: () => false,
        eventMessage: "paper_service_heartbeat"
      }
    ];

    const EVENT_LABELS = {
      paper_service_started: "페이퍼 서비스 시작",
      paper_service_heartbeat: "자동 점검 완료",
      paper_service_blocked: "설정 미완료로 중지",
      market_session_state: "시장 상태 확인",
      paper_service_market_closed: "시장 휴장/대기",
      premarket_watchlist_blocked: "관심 종목 생성 일부 실패",
      premarket_watchlist_generated: "관심 종목 생성 완료",
      paper_market_data_blocked: "시세 조회 확인 필요",
      market_data_rate_limit_paused: "시세 조회 잠시 대기",
      broker_account_synced: "토스 계좌 동기화",
      broker_order_history_synced: "토스 주문 히스토리 동기화",
      broker_order_history_sync_failed: "토스 주문 히스토리 확인 실패",
      live_order_status_synced: "주문 상태 추적",
      live_order_status_sync_failed: "주문 상태 확인 실패",
      universe_generated: "후보 종목 필터링 완료",
      paper_reconcile_blocked: "계좌 확인 필요",
      paper_order_guard: "주문 안전 조건 확인",
      paper_order_intent: "페이퍼 주문 후보 기록",
      paper_fill: "페이퍼 체결 반영",
      paper_runtime_blocked: "설정 확인 필요"
    };

    const SETTINGS_KEYS = {
      accountValue: ["account", "seq"].join("_"),
      accountReady: ["account", "seq", "configured"].join("_"),
      accountAlias: ["account", "alias"].join("_"),
      stopActive: ["emergency", "stop"].join("_"),
    };

    const COLUMN_LABELS = {
      time: "시간",
      level: "레벨",
      event: "이벤트",
      detail: "상세",
      symbol: "종목",
      name: "이름",
      status: "상태",
      side: "방향",
      quantity: "수량",
      filled_quantity: "체결 수량",
      remaining_quantity: "잔여 수량",
      price: "가격",
      observed_price: "관측가",
      fill_price: "체결가",
      avg_price: "평단",
      average_price: "평단",
      stop_price: "손절가",
      entry_price: "진입가",
      nearest_distance: "진입선 거리",
      created_at: "생성 시각",
      updated_at: "갱신 시각",
      reason: "사유"
    };

    const TABLE_COLUMNS = {
      watchlist: ["symbol", "name", "nearest_distance", "status", "updated_at"],
      positions: ["symbol", "status", "quantity", "average_price", "stop_price", "updated_at"],
      orders: ["symbol", "side", "quantity", "price", "status", "created_at"]
    };

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    function displayValue(value) {
      if (Array.isArray(value)) return value.length ? value.join(", ") : "-";
      if (value && typeof value === "object") return JSON.stringify(value);
      if (value === true) return "예";
      if (value === false) return "아니요";
      return value == null || value === "" ? "-" : value;
    }

    function shortTimestamp(value) {
      if (!value) return "-";
      const parsed = new Date(value);
      if (Number.isNaN(parsed.getTime())) return String(value);
      return parsed.toLocaleString("ko-KR", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit"
      });
    }

    function currentClockText() {
      const time = new Intl.DateTimeFormat("ko-KR", {
        timeZone: "Asia/Seoul",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false
      }).format(new Date());
      return `현재 ${time}`;
    }

    function updateDashboardClock() {
      const clockText = document.getElementById("dashboard-clock");
      if (clockText) clockText.textContent = currentClockText();
    }

    function eventLabel(message) {
      return EVENT_LABELS[message] || String(message || "이벤트");
    }

    function levelLabel(level) {
      const text = String(level || "INFO").toUpperCase();
      if (text === "ERROR") return "오류";
      if (text === "WARN") return "확인";
      return "정보";
    }

    function levelClass(level) {
      const text = String(level || "INFO").toUpperCase();
      if (text === "ERROR") return "error";
      if (text === "WARN") return "warn";
      return "";
    }

    function columnLabel(key) {
      return COLUMN_LABELS[key] || String(key || "").replaceAll("_", " ");
    }

    function blockerLabel(blocker) {
      const text = String(blocker || "");
      return text;
    }

    function blockerDetail(blocker) {
      return blockerLabel(blocker);
    }

    function groupedBlockerDetails(blockers) {
      const groups = new Map();
      blockers.forEach((blocker) => {
        const friendly = blockerLabel(blocker);
        groups.set(friendly, []);
      });
      return [...groups.keys()];
    }

    function groupedBlockerLabels(blockers) {
      return uniqueValues((blockers || []).map(blockerLabel));
    }

    function blockerShortLabel(blocker) {
      const text = String(blocker || "");
      if (text.includes("토스 API")) return "Toss 인증 필요";
      if (text.includes("계좌")) return "계좌 연결 필요";
      if (text.includes("종목")) return "종목 후보 없음";
      if (text.includes("주문을 평가할 시간")) return "시장 시간 아님";
      if (text.includes("개장 여부")) return "개장 정보 확인 필요";
      return blockerLabel(blocker);
    }

    function groupedBlockerShortLabels(blockers) {
      return uniqueValues((blockers || []).map(blockerShortLabel));
    }

    function primaryAction(status) {
      const blockers = Array.isArray(status && status.blockers) ? status.blockers : [];
      const ready = Boolean(status && status.ready);
      const first = blockers.find((blocker) => String(blocker).includes("종목"))
        || blockers.find((blocker) => String(blocker).includes("토스 API"))
        || blockers.find((blocker) => String(blocker).includes("계좌"))
        || blockers[0];
      if (!ready) {
        const reason = (first ? blockerLabel(first) : "운영 준비").replace(/[.!?…]+$/g, "");
        return {
          title: "아직 시작할 준비가 안 됐습니다",
          body: `먼저 확인할 항목: ${reason}. 설정 탭에서 이 항목부터 채워 주세요.`,
          href: "#settings",
          label: "필수 설정 확인",
          kind: "blocked"
        };
      }
      if (!first) {
        return {
          title: "운영 상태를 확인하세요",
          body: "필수 설정은 끝났습니다. 이벤트 탭에서 최근 점검과 시장 상태를 확인하면 됩니다.",
          href: "#events",
          label: "이벤트 보기",
          kind: "done"
        };
      }
      const text = String(first);
      if (text.includes("종목")) {
        return {
          title: "감시 종목 후보를 먼저 넣으세요",
          body: "종목이 없으면 봇이 무엇을 살지 판단할 수 없습니다.",
          href: "#settings",
          label: "설정 확인",
          kind: "warn"
        };
      }
      if (text.includes("토스 API")) {
        return {
          title: "Toss API 인증 정보를 설정하세요",
          body: "토스 키가 있어야 계좌와 시장 정보를 확인할 수 있습니다.",
          href: "#settings",
          label: "설정 확인",
          kind: "warn"
        };
      }
      if (text.includes("계좌")) {
        return {
          title: "거래 계좌 번호를 연결하세요",
          body: "계좌 번호가 있어야 어떤 계좌로 거래할지 알 수 있습니다.",
          href: "#settings",
          label: "설정 확인",
          kind: "warn"
        };
      }
      return {
        title: blockerLabel(first),
        body: "설정 탭에서 빠진 항목을 확인하세요.",
        href: "#settings",
        label: "설정 확인",
        kind: "warn"
      };
    }

    function eventDetail(entry) {
      const payload = entry && entry.payload && typeof entry.payload === "object" ? entry.payload : {};
      if (Array.isArray(payload.blockers) && payload.blockers.length) {
        const labels = uniqueValues(payload.blockers.map(blockerLabel));
        const visible = labels.slice(0, 3).join(" ");
        return labels.length > 3 ? `${visible} 외 ${labels.length - 3}건` : visible;
      }
      if (payload.market_session && payload.market_session.status) {
        return `시장 상태: ${payload.market_session.status}`;
      }
      if (payload.symbol) {
        if (payload.error) return `종목 ${payload.symbol}: ${payload.error}`;
        return `종목 ${payload.symbol}${payload.side ? ` / ${payload.side}` : ""}`;
      }
      if (payload.count != null) {
        return `${payload.count}건`;
      }
      if (payload.holdings_count != null || payload.open_orders_count != null) {
        return `보유 ${payload.holdings_count || 0}개 / 미체결 ${payload.open_orders_count || 0}개`;
      }
      if (payload.closed_orders_count != null) {
        return `종료 주문 ${payload.closed_orders_count || 0}개`;
      }
      if (payload.remaining_unresolved != null) {
        return `확인 ${payload.checked || 0}개 / 미완료 ${payload.remaining_unresolved || 0}개`;
      }
      return "";
    }

    function statusText(kind) {
      if (kind === "done") return "완료";
      if (kind === "warn") return "진행 필요";
      if (kind === "blocked") return "확인 필요";
      return "확인";
    }

    function liveStateLabel(state) {
      if (state === "ready_for_shadow") return "shadow 기준 통과";
      if (state === "needs_review") return "검토 필요";
      if (state === "blocked") return "준비 필요";
      return "확인 중";
    }

    function liveStateClass(state) {
      if (state === "ready_for_shadow") return "done";
      if (state === "needs_review") return "warn";
      return "blocked";
    }

    function renderLiveReadiness(readiness) {
      const data = readiness || {};
      const state = data.state || "blocked";
      const kind = liveStateClass(state);
      const card = document.getElementById("live-readiness-card");
      const badge = document.getElementById("live-readiness-status");
      const headline = document.getElementById("live-readiness-headline");
      const copy = document.getElementById("live-readiness-copy");
      const summary = document.getElementById("live-readiness-summary");
      const checksBox = document.getElementById("live-readiness-checks");
      const checkCount = document.getElementById("live-check-count");
      const submitReason = document.getElementById("live-submit-reason");
      const checks = Array.isArray(data.checks) ? data.checks : [];
      const counts = data.summary || {};

      if (card) {
        card.className = `card data-panel live-panel live-status-card ${kind}`;
      }
      if (badge) {
        badge.className = `status-pill ${kind}`;
        badge.textContent = liveStateLabel(state);
      }
      if (headline) {
        headline.textContent = data.headline || "실거래 상태를 확인하세요";
      }
      if (copy) {
        const mode = data.runtime_mode ? modeLabel(data.runtime_mode) : "대기";
        const strategy = data.strategy_kind || "unknown";
        copy.textContent = `현재 전략은 ${strategy}, 거래 상태는 ${mode}입니다. 실거래 시작: ${data.can_submit_live_orders ? "가능" : "아직 준비 필요"}.`;
      }
      if (summary) {
        const items = [
          ["통과", counts.done || 0],
          ["검토", counts.warning || 0],
          ["확인 필요", counts.blocked || 0],
        ];
        summary.innerHTML = items.map(([label, value]) => `
          <div class="live-summary-item">
            <span>${escapeHtml(label)}</span>
            <strong>${escapeHtml(value)}</strong>
          </div>`).join("");
      }
      if (submitReason) {
        submitReason.textContent = data.submit_disabled_reason || "아직 실제 주문은 보내지 않습니다.";
      }
      if (checkCount) {
        checkCount.textContent = `${checks.length}개`;
      }
      if (checksBox) {
        if (!checks.length) {
          checksBox.innerHTML = emptyState("확인 항목이 없습니다", "대시보드 데이터를 다시 불러오세요.", "#dashboard", "대시보드 보기");
          return;
        }
        checksBox.innerHTML = checks.map((check) => {
          const checkKind = check.status || "warn";
          return `<div class="live-check">
            <span class="live-dot ${escapeHtml(checkKind)}"></span>
            <div>
              <strong>${escapeHtml(check.label || "확인 항목")}</strong>
              <p>${escapeHtml(check.summary || "")}</p>
              <p>${escapeHtml(check.action || "")}</p>
            </div>
            <span class="status-pill ${escapeHtml(checkKind)}">${escapeHtml(statusText(checkKind))}</span>
          </div>`;
        }).join("");
      }
    }

    function renderLiveMonitor(monitor) {
      const data = monitor || {};
      const status = document.getElementById("live-monitor-status");
      const grid = document.getElementById("live-monitor-grid");
      const history = document.getElementById("live-monitor-history");
      const account = data.account_sync || {};
      const historySync = data.order_history || {};
      const tracking = data.order_status_tracking || {};
      const current = data.current || {};
      const last = data.last_execution || {};
      const unresolved = Number(current.unresolved_execution_count ?? tracking.remaining_unresolved ?? 0);
      const historyError = historySync.error;
      const ok = !historyError && unresolved === 0;

      if (status) {
        status.className = `status-pill ${ok ? "done" : "warn"}`;
        status.textContent = ok ? "정상 추적" : "확인 필요";
      }
      if (grid) {
        const cards = [
          ["계좌 동기화", shortTimestamp(account.last_synced_at), `보유 ${account.holdings_count ?? "-"} / 미체결 ${account.open_orders_count ?? "-"}`],
          ["주문 히스토리", shortTimestamp(historySync.last_synced_at), historyError ? "확인 실패" : `종료 주문 ${historySync.closed_orders_count ?? 0}개`],
          ["주문 상태 추적", shortTimestamp(tracking.last_synced_at), `확인 ${tracking.checked ?? 0} / 미완료 ${tracking.remaining_unresolved ?? 0}`],
          ["현재 실주문", last.status || "-", [last.symbol, last.side, last.quantity ? `${last.quantity}주` : ""].filter(Boolean).join(" ") || "최근 주문 없음"],
          ["히스토리 기준", historySync.source_label || "계좌 주문 목록", historySync.market_trades_are_account_history === false ? "시장 체결 틱 제외" : "확인 필요"],
        ];
        grid.innerHTML = cards.map(([label, value, helper]) => `
          <div class="pilot-item">
            <span>${escapeHtml(label)}</span>
            <strong>${escapeHtml(value || "-")}</strong>
            <span>${escapeHtml(helper || "")}</span>
          </div>`).join("");
      }
      if (history) {
        const rows = Array.isArray(historySync.items) ? historySync.items : [];
        if (historyError) {
          history.innerHTML = emptyState("주문 히스토리를 확인하지 못했습니다", historyError);
          return;
        }
        if (!rows.length) {
          history.innerHTML = emptyState("최근 종료 주문이 없습니다", "토스 계좌 주문 히스토리가 비어 있거나 아직 동기화 전입니다.");
          return;
        }
        const compactRows = rows.map((row) => ({
          symbol: row.symbol,
          side: row.side,
          status: row.status,
          quantity: row.quantity,
          filled_quantity: row.execution && row.execution.filledQuantity,
          price: row.price || (row.execution && row.execution.averageFilledPrice),
          updated_at: row.orderedAt || row.canceledAt || (row.execution && row.execution.filledAt),
        }));
        renderTable("live-monitor-history", compactRows, ["symbol", "side", "status", "quantity", "filled_quantity", "price", "updated_at"], "최근 종료 주문이 없습니다");
      }
    }

    function uniqueValues(items) {
      return [...new Set(items.filter((item) => item != null && item !== ""))];
    }

    function getJson(path) {
      return fetch(path, { cache: "no-store" }).then((response) => {
        if (!response.ok) throw new Error(`${path} failed: ${response.status}`);
        return response.json();
      });
    }

    async function postJson(path, payload) {
      const toActionErrorMessage = (errorPayload, fallback, pathName) => {
        if (!errorPayload || typeof errorPayload !== "object") return fallback;
        const raw = String(errorPayload.error || errorPayload.message || "").trim();
        const normalized = raw.toLowerCase();

        if (
          normalized.includes("live consent is enabled but no allowed_live_consent_ids is configured") ||
          normalized.includes("live_consent_ids_not_configured") ||
          normalized.includes("consent allowlist is required")
        ) {
          return "수동 승인 확인이 켜져 있습니다. 24시간 자동매매를 쓰려면 설정에서 toss.require_live_consent를 false로 바꿔 주세요.";
        }
        if (
          normalized.includes("consent_id is required") ||
          normalized.includes("live_consent_id_required")
        ) {
          return "수동 승인 확인이 켜져 있어 자동 실행을 막고 있습니다. 24시간 자동매매에는 이 설정을 끄는 것이 맞습니다.";
        }
        if (
          normalized.includes("consent_id is not authorized") ||
          normalized.includes("live_consent_id_not_allowed")
        ) {
          return "수동 승인 코드가 등록된 목록과 맞지 않습니다. 자동매매용이면 수동 승인 확인을 끄세요.";
        }
        if (
          normalized.includes("ip adress not allowed") ||
          normalized.includes("ip address not allowed") ||
          normalized.includes("outbound public ip is not in the toss ip allowlist") ||
          normalized.includes("address is not allowed") ||
          normalized.includes("not allowed ip") ||
          normalized.includes("not allowed address")
        ) {
          return "Toss가 현재 맥북/컨테이너 공개 IP를 거절했습니다. '현재 공개 IP 확인' 버튼으로 나온 IP를 Toss 개발자센터 앱 허용 IP에 추가한 뒤 다시 실행하세요.";
        }
        if (normalized.includes("confirmation must be")) {
          return pathName === "/dashboard/actions/live-smoke-test"
            ? "실주문 연결 테스트 확인 문구를 정확히 입력해야 합니다. 문구: 실주문 테스트"
            : "1회 실행 확인 문구를 정확히 입력해야 합니다. 문구: LIVE PILOT 실행";
        }
        if (normalized.includes("live once action is already running")) {
          return "현재 1회 실행이 진행 중입니다. 잠시 후 다시 시도하세요.";
        }
        if (normalized.includes("live smoke test action is already running")) {
          return "실주문 연결 테스트가 진행 중입니다. 잠시 후 다시 시도하세요.";
        }
        if (normalized.includes("dashboard actions require --config")) {
          return "대시보드 실행 권한이 없습니다. 설정 파일 모드로 실행하세요.";
        }
        if (pathName === "/dashboard/actions/live-once" && normalized.includes("required for live execution")) {
          return "수동 승인 확인 설정이 실거래 실행을 막고 있습니다.";
        }
        return raw || fallback;
      };

      const response = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload || {})
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(toActionErrorMessage(data, `${path} failed: ${response.status}`, path));
      }
      return data;
    }

    async function runLiveOnce() {
      const input = document.getElementById("live-once-confirmation");
      const button = document.getElementById("live-once-button");
      const result = document.getElementById("live-once-result");
      const operatorIdInput = document.getElementById("live-operator-id");
      const confirmation = input ? input.value.trim() : "";
      const operatorId = operatorIdInput ? operatorIdInput.value.trim() : "";
      if (button) button.disabled = true;
      if (result) result.textContent = "1회 실행 요청 중...";
      try {
        const payload = await postJson("/dashboard/actions/live-once", {
          confirmation,
          operator_id: operatorId,
        });
        if (result) {
          const status = payload && payload.snapshot ? payload.snapshot.status : payload.status;
          result.textContent = `실행 완료: ${status || "completed"}`;
        }
        await refresh();
      } catch (error) {
        if (result) result.textContent = `실행 실패: ${error.message}`;
      } finally {
        if (button) button.disabled = false;
      }
    }

    async function applySafePilot(buttonId = "safe-pilot-button", resultId = "safe-pilot-result") {
      const button = document.getElementById(buttonId);
      const result = document.getElementById(resultId);
      const operatorIdInput = document.getElementById("live-operator-id");
      const operatorId = operatorIdInput ? operatorIdInput.value.trim() : "";
      if (button) button.disabled = true;
      if (result) result.textContent = "안전 파일럿을 준비하고 자동 실행을 시작하는 중입니다...";
      try {
        const payload = await postJson("/dashboard/actions/apply-safe-pilot", {
          operator_id: operatorId,
        });
        const pilot = payload.safe_pilot || {};
        if (result) {
          const symbol = pilot.symbol || "선택 종목";
          result.textContent = payload.loop === "already_running"
            ? `이미 자동 실행 중입니다. ${symbol} 한도와 이벤트 탭을 확인하세요.`
            : `자동 실행 준비됨: ${symbol}. 새 주문은 표시된 한도 안에서만 나갑니다.`;
        }
        await refresh();
      } catch (error) {
        if (result) result.textContent = `시작 실패: ${error.message}`;
      } finally {
        if (button) button.disabled = false;
      }
    }

    async function runLiveSmokeTest() {
      const input = document.getElementById("live-smoke-confirmation");
      const button = document.getElementById("live-smoke-test-button");
      const result = document.getElementById("live-smoke-test-result");
      const confirmation = input ? input.value.trim() : "";
      if (button) button.disabled = true;
      if (result) result.textContent = "실주문 연결 테스트 요청 중...";
      try {
        const payload = await postJson("/dashboard/actions/live-smoke-test", { confirmation });
        const execution = payload.execution || {};
        const cancel = payload.cancel || {};
        if (result) {
          result.textContent = cancel.status
            ? `테스트 완료: ${payload.symbol} 1주 접수 ${execution.status || "-"} / 취소 ${cancel.status}`
            : `테스트 결과: ${payload.symbol || "종목"} 1주 ${execution.status || payload.status || "확인 필요"}`;
        }
        const symbol = payload.symbol || execution.symbol || "종목";
        const executionStatus = tradeStatusLabel(execution.status || payload.status);
        const cancelStatus = cancel.status ? tradeStatusLabel(cancel.status) : "";
        const body = cancelStatus
          ? `1주 / ${executionStatus} / ${cancelStatus}`
          : `1주 / ${executionStatus || "확인 필요"}`;
        showToast(`실주문 테스트 · ${symbol}`, body, "info");
        notifyBrowser(`실주문 테스트 · ${symbol}`, body);
        await refresh();
      } catch (error) {
        if (result) result.textContent = `테스트 실패: ${error.message}`;
        showToast("실주문 테스트 실패", error.message, "error");
        notifyBrowser("실주문 테스트 실패", error.message);
      } finally {
        if (button) button.disabled = false;
      }
    }

    let latestTossPublicIp = "";

    function setPublicIpCopyButtons(enabled) {
      ["live-public-ip-copy-button", "settings-public-ip-copy-button"].forEach((id) => {
        const button = document.getElementById(id);
        if (button) button.disabled = !enabled;
      });
    }

    function updatePublicIpResults(message) {
      ["live-public-ip-result", "settings-public-ip-result"].forEach((id) => {
        const target = document.getElementById(id);
        if (target) target.textContent = message;
      });
    }

    async function checkTossPublicIp(buttonId = "settings-public-ip-check-button") {
      const button = document.getElementById(buttonId);
      if (button) button.disabled = true;
      setPublicIpCopyButtons(false);
      updatePublicIpResults("현재 공개 IP 확인 중...");
      try {
        const payload = await getJson("/dashboard/network/public-ip");
        latestTossPublicIp = payload.public_ip || "";
        if (!latestTossPublicIp) {
          throw new Error(payload.message || "공개 IP를 확인하지 못했습니다.");
        }
        setPublicIpCopyButtons(true);
        updatePublicIpResults(`현재 공개 IP: ${latestTossPublicIp} · Toss 개발자센터 앱 허용 IP에 추가하세요.`);
      } catch (error) {
        latestTossPublicIp = "";
        updatePublicIpResults(`IP 확인 실패: ${error.message}`);
      } finally {
        if (button) button.disabled = false;
      }
    }

    async function copyTossPublicIp() {
      if (!latestTossPublicIp) {
        updatePublicIpResults("먼저 현재 공개 IP 확인을 눌러 주세요.");
        return;
      }
      try {
        await navigator.clipboard.writeText(latestTossPublicIp);
        updatePublicIpResults(`복사됨: ${latestTossPublicIp}`);
      } catch (error) {
        updatePublicIpResults(`복사 실패: ${latestTossPublicIp} 값을 직접 선택해서 복사해 주세요.`);
      }
    }

    async function stopTrading(buttonId = "live-stop-button", resultId = "safe-pilot-result") {
      const button = document.getElementById(buttonId);
      const result = document.getElementById(resultId);
      if (button) button.disabled = true;
      if (result) result.textContent = "거래 중지 스위치 적용 중...";
      try {
        const payload = await postJson("/dashboard/actions/stop-trading", {});
        const openOrders = payload.open_orders || {};
        if (result) {
          result.textContent = openOrders.count > 0
            ? `거래를 멈췄습니다. 아직 끝나지 않은 주문 ${openOrders.count}건은 Toss에서 확인하세요.`
            : "거래를 멈췄습니다. 새 주문은 보내지 않습니다.";
        }
        await refresh();
      } catch (error) {
        if (result) result.textContent = `중지 실패: ${error.message}`;
      } finally {
        if (button) button.disabled = false;
      }
    }

    function safePilotPrerequisites(status, settings) {
      const blockers = Array.isArray(status && status.blockers) ? status.blockers : [];
      const toss = settings && settings.toss ? settings.toss : {};
      const missingSymbol = blockers.some((blocker) => String(blocker).includes("종목"));
      const missingApi = blockers.some((blocker) => String(blocker).includes("토스 API"));
      const missingAccount = blockers.some((blocker) => String(blocker).includes("계좌"));
      const consentRequired = Boolean(toss.require_live_consent);
      const consentConfigured = Boolean(toss.live_consent_ids_configured);
      const consentCount = Number(toss.live_consent_ids_count || 0);
      const consentMissing = consentRequired && !consentConfigured;
      const accountReady = Boolean(toss[SETTINGS_KEYS.accountReady]);
      const ready = Boolean(
        toss.client_id_configured
          && toss.client_secret_configured
          && accountReady
          && !missingSymbol
          && !missingApi
          && !missingAccount
          && !consentRequired
      );
      const reason = !toss.client_id_configured || !toss.client_secret_configured || missingApi
            ? "Toss API 키 필요"
            : !accountReady || missingAccount
              ? "계좌 연결 필요"
              : missingSymbol
                ? "거래 심볼 필요"
              : consentRequired
                ? `수동 승인 확인 끄기 필요${consentMissing ? ` (등록 코드 ${consentCount}개)` : ""}`
            : "시작 가능";
      return { ready, reason };
    }

    function setSafePilotControls(status, settings) {
      const gate = safePilotPrerequisites(status, settings);
      ["safe-pilot-button", "onboarding-safe-pilot-button"].forEach((id) => {
        const button = document.getElementById(id);
        if (!button) return;
        button.disabled = !gate.ready;
        button.title = gate.ready ? "안전 파일럿 시작" : gate.reason;
      });
      const badge = document.getElementById("safe-pilot-state-badge");
      if (badge) {
        const live = settings && settings.live ? settings.live : {};
        const stopped = Boolean(live[SETTINGS_KEYS.stopActive]);
        badge.className = `status-pill ${gate.ready && !stopped ? "done" : "blocked"}`;
        badge.textContent = stopped ? `중지됨 / ${gate.reason}` : gate.reason;
      }
      const nextAction = document.getElementById("safe-pilot-next-action");
      if (nextAction) {
        nextAction.textContent = gate.ready
          ? "준비가 끝났습니다. 안전 파일럿 시작을 누르면 아래 한도 안에서 자동 실행됩니다."
          : `다음 할 일: ${gate.reason}`;
      }
    }

    function renderSetupFlow(status, settings) {
      const toss = settings && settings.toss ? settings.toss : {};
      const gate = safePilotPrerequisites(status, settings);
      const apiReady = Boolean(toss.client_id_configured && toss.client_secret_configured);
      const accountReady = Boolean(toss[SETTINGS_KEYS.accountReady]);
      const steps = {
        api: {
          kind: apiReady ? "done" : "warn",
          label: apiReady ? "완료" : "진행 필요",
        },
        account: {
          kind: accountReady ? "done" : apiReady ? "warn" : "idle",
          label: accountReady ? "완료" : apiReady ? "진행 필요" : "대기",
        },
        start: {
          kind: gate.ready ? "done" : "idle",
          label: gate.ready ? "시작 가능" : "대기",
        },
      };
      Object.entries(steps).forEach(([key, step]) => {
        const node = document.querySelector(`[data-setup-step="${key}"]`);
        if (!node) return;
        node.classList.remove("done", "warn", "idle");
        node.classList.add(step.kind);
        const pill = node.querySelector("[data-setup-status]");
        if (!pill) return;
        pill.textContent = step.label;
        pill.className = `status-pill ${step.kind === "done" ? "done" : step.kind === "warn" ? "warn" : "todo"}`;
      });
    }

    function renderPilotSummary(settings) {
      const pilot = settings && settings.pilot ? settings.pilot : {};
      const symbol = pilot.symbol || "아직 선택 안 됨";
      const quantity = pilot.max_quantity ? `최대 ${pilot.max_quantity}주` : "최대 1주";
      const dailyOrders = pilot.daily_orders ? `하루 ${pilot.daily_orders}건` : "하루 1건";
      const dailyAmount = pilot.daily_amount ? `${pilot.daily_amount}` : "소액 제한";
      const failureFuse = pilot.failure_fuse != null ? `연속 실패 ${pilot.failure_fuse}회 중지` : "꺼짐";
      const state = pilot.stop_active ? "거래 중지 상태" : "시작 가능";
      document.querySelectorAll("[data-pilot-summary]").forEach((box) => {
        const values = {
          symbol,
          quantity,
          "daily-orders": dailyOrders,
          "daily-amount": dailyAmount,
          "failure-fuse": failureFuse,
          state,
        };
        Object.entries(values).forEach(([key, value]) => {
          const target = box.querySelector(`[data-pilot-field="${key}"]`);
          if (target) target.textContent = value;
        });
      });
    }

    function setActiveView(target) {
      document.querySelectorAll(".view").forEach((view) => {
        view.classList.toggle("active", view.dataset.view === target);
      });
      document.querySelectorAll("a[data-view]").forEach((anchor) => {
        const active = anchor.dataset.view === target;
        anchor.classList.toggle("active", active);
        if (active) anchor.setAttribute("aria-current", "true");
        else anchor.removeAttribute("aria-current");
      });
    }

    function emptyState(title, body, href = "#settings", label = "설정 확인") {
      return `<div class="empty-state">
        <div class="icon-tile"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle cx="12" cy="12" r="9"></circle><path d="M8 12h8"></path><path d="M12 8v8"></path></svg></div>
        <div>
          <strong>${escapeHtml(title)}</strong>
          <p>${escapeHtml(body)} <a href="${escapeHtml(href)}" data-view="${href.replace("#", "")}">${escapeHtml(label)}</a></p>
        </div>
      </div>`;
    }

    function renderTable(elementId, rows, columns, fallbackTitle, fallbackBody, fallbackHref = "#settings") {
      const container = document.getElementById(elementId);
      if (!container) return;
      if (!rows || !rows.length) {
        container.innerHTML = emptyState(fallbackTitle, fallbackBody, fallbackHref);
        return;
      }
      const keys = columns || Object.keys(rows[0] || {});
      const head = keys.map((key) => `<th>${escapeHtml(columnLabel(key))}</th>`).join("");
      const body = rows.map((row) => `<tr>${keys.map((key) => `<td>${escapeHtml(displayValue(row[key]))}</td>`).join("")}</tr>`).join("");
      container.innerHTML = `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
    }

    function setCountBadge(id, count, suffix = "개") {
      const badge = document.getElementById(id);
      if (badge) badge.textContent = `${count}${suffix}`;
    }

    function payloadItems(payload, key) {
      if (!payload) return [];
      if (Array.isArray(payload.items)) return payload.items;
      if (Array.isArray(payload[key])) return payload[key];
      return [];
    }

    function statusKind(ready) {
      return ready ? "done" : "blocked";
    }

    function modeLabel(mode) {
      const text = String(mode || "idle");
      if (text === "paper") return "페이퍼";
      if (text === "live") return "실거래";
      if (text === "idle") return "대기";
      return text;
    }

    function renderSidebarStatus(status) {
      const readyText = document.getElementById("sidebar-ready");
      const modeText = document.getElementById("sidebar-mode");
      const blockerText = document.getElementById("sidebar-blockers");
      if (!readyText || !modeText || !blockerText) return;
      const blockers = Array.isArray(status && status.blockers) ? status.blockers : [];
      const ready = Boolean(status && status.ready);
      readyText.textContent = ready ? "운영 가능" : "확인 필요";
      readyText.className = `status-pill ${statusKind(ready)}`;
      modeText.textContent = `모드: ${modeLabel(status && status.mode)}`;
      const labels = groupedBlockerShortLabels(blockers);
      blockerText.textContent = labels.length
        ? `확인할 항목 ${labels.length}개: ${labels.slice(0, 2).join(" / ")}${labels.length > 2 ? " ..." : ""}`
        : "현재 막힌 항목이 없습니다.";
    }

    function renderMetricCards(status, watchRows, positionRows, orderRows, summary) {
      const row = document.querySelector(".stat-row");
      if (!row) return;
      const ready = Boolean(status && status.ready);
      const mode = status && status.mode ? status.mode : "idle";
      const eventTotal = summary && summary.total != null ? summary.total : 0;
      row.innerHTML = `
        <article class="card stat-card">
          <div class="icon-tile"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="m12 3 7 4v10l-7 4-7-4V7l7-4Z"></path><path d="M12 8v8"></path><path d="m9 10 3-2 3 2"></path></svg></div>
          <div class="stat-lines">
            <p class="stat-label">운영 모드</p>
            <span class="metric-value">${escapeHtml(modeLabel(mode))}</span>
            <span class="status-pill ${statusKind(ready)}">${ready ? "준비됨" : "확인 필요"}</span>
          </div>
        </article>
        <article class="card stat-card compact">
          <div class="icon-tile small"><svg viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9L12 3Z"></path></svg></div>
          <div><p class="stat-label">관심 종목</p><span class="metric-value">${watchRows.length}</span><span class="metric-note">감시 중</span></div>
        </article>
        <article class="card stat-card compact">
          <div class="icon-tile small"><svg viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M13 2a10 10 0 1 0 9 11h-9V2Z"></path><path d="M15 2.2V9h6.8A10 10 0 0 0 15 2.2Z"></path></svg></div>
          <div><p class="stat-label">보유 포지션</p><span class="metric-value">${positionRows.length}</span><span class="metric-note">open</span></div>
        </article>
        <article class="card stat-card compact">
          <div class="icon-tile small"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M8 4h8v16H8z"></path><path d="M10 8h4"></path><path d="M10 12h4"></path><path d="M10 16h2"></path></svg></div>
          <div><p class="stat-label">미체결 주문</p><span class="metric-value">${orderRows.length}</span><span class="metric-note">주문 미제출</span></div>
        </article>
        <article class="card stat-card compact">
          <div class="icon-tile small"><svg viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M12 22a2.5 2.5 0 0 0 2.4-1.8H9.6A2.5 2.5 0 0 0 12 22ZM18 16v-5a6 6 0 1 0-12 0v5l-2 2v1h16v-1l-2-2Z"></path></svg></div>
          <div><p class="stat-label">총 이벤트</p><span class="metric-value">${eventTotal}</span><span class="metric-note">latest</span></div>
        </article>`;
    }

    function renderOperatorBrief(status, eventRows, watchRows, positionRows, orderRows) {
      const container = document.getElementById("dashboard-operator-brief");
      if (!container) return;
      const action = primaryAction(status);
      const ready = Boolean(status && status.ready);
      const lastEvent = eventRows && eventRows.length ? eventRows[0] : null;
      const blockers = Array.isArray(status && status.blockers) ? status.blockers : [];
      const dataSummary = [
        `관심 ${watchRows.length}개`,
        `포지션 ${positionRows.length}개`,
        `주문 ${orderRows.length}개`
      ].join(" / ");
      const lastEventTitle = lastEvent ? eventLabel(lastEvent.message) : "아직 이벤트가 없습니다";
      const lastEventBody = lastEvent
        ? `${levelLabel(lastEvent.level)} · ${eventDetail(lastEvent)} · ${shortTimestamp(lastEvent.created_at)}`
        : ready
          ? "페이퍼 서비스가 실행되면 최근 기록이 표시됩니다."
          : "운영이 차단되어 있어 아직 이벤트가 누적되지 않습니다. 설정 탭에서 차단 항목을 우선 확인해 주세요.";
      const blockerBody = blockers.length
        ? groupedBlockerShortLabels(blockers).slice(0, 3).join(" / ")
        : ready ? "차단 항목 없음" : "설정에서 차단 항목을 먼저 확인하세요.";
      container.innerHTML = `
        <div class="brief-item primary ${action.kind}">
          <span class="brief-kicker">우선 확인</span>
          <strong>${escapeHtml(action.title)}</strong>
          <p>${escapeHtml(action.body)}</p>
          <a href="${escapeHtml(action.href)}" data-view="${escapeHtml(action.href.replace("#", ""))}">${escapeHtml(action.label)}</a>
        </div>
        <div class="brief-item">
          <span class="brief-kicker">최근 기록</span>
          <strong>${escapeHtml(lastEventTitle)}</strong>
          <p>${escapeHtml(lastEventBody)}</p>
        </div>
        <div class="brief-item">
          <span class="brief-kicker">데이터 상태</span>
          <strong>${escapeHtml(dataSummary)}</strong>
          <p>${escapeHtml(blockerBody)}</p>
        </div>`;
    }

    function renderHealthPanel(status) {
      const container = document.getElementById("dashboard-health-list");
      if (!container) return;
      const blockers = Array.isArray(status && status.blockers) ? status.blockers : [];
      const ready = Boolean(status && status.ready);
      const labels = groupedBlockerLabels(blockers);
      const rows = [
        ["상태", status && status.ready ? "준비됨" : "설정 확인 필요", status && status.ready ? "done" : "blocked"],
        ["모드", status && status.mode ? status.mode : "idle", ""],
        ["마지막 점검", shortTimestamp(status && status.last_heartbeat_at), ""],
        ["마지막 이벤트", shortTimestamp(status && status.last_event_at), ""],
        ["차단 항목", blockers.length ? `${blockers.length}개` : "없음", blockers.length ? "warn" : "done"]
      ];
      container.className = "info-list";
      const nextStep = labels.length
        ? `<div class="next-step blocked"><strong>다음 할 일</strong><p>${escapeHtml(labels.slice(0, 2).join(" · "))} 항목부터 우선 처리해 주세요.</p><a href="#settings" data-view="settings">설정에서 바로 처리</a></div>`
        : `<div class="next-step ${ready ? "done" : "blocked"}"><strong>다음 할 일</strong><p>${ready ? "필수 설정은 끝났습니다. 최근 이벤트에서 봇 상태를 확인하세요." : "아직 필요한 입력이 남아 있습니다. 설정 화면에서 빠진 항목을 채운 뒤 새로고침하세요."}</p><a href="${ready ? "#events" : "#settings"}" data-view="${ready ? "events" : "settings"}">${ready ? "이벤트 확인" : "필수 설정 확인"}</a></div>`;
      container.innerHTML = rows.map(([label, value, kind]) => `
        <div class="info-row">
          <span class="dot"></span>
          <span><strong>${escapeHtml(label)}</strong><br>${escapeHtml(value)}</span>
          ${kind ? `<span class="status-pill ${kind}">${escapeHtml(value)}</span>` : `<span class="helper-text">read</span>`}
        </div>`).join("") + nextStep;
    }

    function renderWatchSummary(watchRows) {
      const chart = document.querySelector(".chart-area");
      if (!chart) return;
      const top = watchRows.slice(0, 5);
      if (!top.length) {
        chart.innerHTML = `<div class="summary-stack">${emptyState("관심 종목이 없습니다", "감시할 종목 후보를 설정하면 이곳에 표시됩니다.")}</div>`;
        return;
      }
      chart.innerHTML = `<div class="summary-stack">${top.map((row) => `
        <div class="summary-chip">
          <strong>${escapeHtml(row.symbol || "-")}</strong>
          <span class="helper-text">nearest ${escapeHtml(displayValue(row.nearest_distance))}</span>
        </div>`).join("")}</div>`;
    }

    function renderPositionSummary(positionRows) {
      const donut = document.querySelector(".donut-wrap");
      const strip = document.querySelector(".mini-strip");
      if (donut) {
        const open = positionRows.filter((row) => String(row.status || "").toUpperCase() === "OPEN").length;
        donut.innerHTML = `
          <div class="donut"></div>
          <div class="summary-lines">
            <div class="summary-chip"><strong>${positionRows.length} positions</strong><span class="helper-text">${open} open positions</span></div>
            <div class="summary-chip"><strong>paper mode</strong><span class="helper-text">실거래 주문은 제출하지 않습니다.</span></div>
          </div>`;
      }
      if (strip) {
        strip.innerHTML = positionRows.slice(0, 4).map((row) => `<span class="ghost-line" title="${escapeHtml(row.symbol || "-")}"></span>`).join("") || `<span class="helper-text">보유 포지션 없음</span>`;
      }
    }

    function renderEventCards(elementId, items) {
      const container = document.getElementById(elementId);
      if (!container) return;
      if (!items || !items.length) {
        container.innerHTML = emptyState("아직 이벤트가 없습니다", "운영이 차단되어 있으면 이벤트가 적을 수 있습니다. 먼저 설정 탭에서 차단 항목을 확인하고 다시 새로고침하면 기록이 표시됩니다.", "#settings", "차단 항목 보기");
        return;
      }
      container.innerHTML = items.slice(0, 12).map((entry) => {
        const level = String(entry.level || "INFO").toUpperCase();
        const detail = eventDetail(entry);
        return `<div class="event-card">
          <span class="level-badge ${levelClass(level)}">${escapeHtml(levelLabel(level))}</span>
          <div><strong>${escapeHtml(eventLabel(entry.message))}</strong><p>${escapeHtml(detail)}</p></div>
          <span class="helper-text">${escapeHtml(shortTimestamp(entry.created_at))}</span>
        </div>`;
      }).join("");
    }

    function renderTimeline(elementId, items) {
      const container = document.getElementById(elementId);
      if (!container) return;
      if (!items || !items.length) {
        container.innerHTML = `<li class="event-line"><span class="event-dot"></span><strong>-</strong><span>아직 기록이 없습니다. 설정을 저장한 뒤 봇 점검 기록을 기다려 주세요.</span><span class="helper-text">빈 상태</span></li>`;
        return;
      }
      container.innerHTML = items.slice(0, 6).map((entry) => {
        const level = String(entry.level || "INFO").toUpperCase();
        const dot = level === "WARN" ? "warn" : level === "ERROR" ? "warn" : "ok";
        const detail = eventDetail(entry);
        const label = detail ? `${eventLabel(entry.message)}: ${detail}` : eventLabel(entry.message);
        return `<li class="event-line"><span class="event-dot ${dot}"></span><strong>${escapeHtml(levelLabel(level))}</strong><span>${escapeHtml(label)}</span><span class="helper-text">${escapeHtml(shortTimestamp(entry.created_at))}</span></li>`;
      }).join("");
    }

    function renderBotSummary(status, summary, watchRows, orderRows) {
      const container = document.querySelector(".bot-summary");
      if (!container) return;
      const blockers = Array.isArray(status && status.blockers) ? status.blockers.length : 0;
      const eventTotal = summary && summary.total != null ? summary.total : 0;
      const tiles = [
        ["준비 상태", status && status.ready ? "운영 가능" : `${blockers}개 확인 필요`],
        ["관심 종목", `${watchRows.length}개`],
        ["주문 상태", `${orderRows.length}개 확인 · 실주문 비활성`],
        ["이벤트", `${eventTotal}개 기록`]
      ];
      container.innerHTML = tiles.map(([label, value]) => `
        <div class="summary-tile">
          <div class="icon-tile small"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M4 12h16"></path><path d="M4 7h16"></path><path d="M4 17h16"></path></svg></div>
          <div><strong>${escapeHtml(label)}</strong><br><span class="helper-text">${escapeHtml(value)}</span></div>
        </div>`).join("");
    }

    function renderLogLines(events) {
      const container = document.querySelector(".log-lines");
      if (!container) return;
      if (!events || !events.length) {
        container.innerHTML = `<div class="log-line"><span class="dot"></span><span class="helper-text">표시할 로그가 없습니다.</span><span></span></div>`;
        return;
      }
      container.innerHTML = events.slice(0, 4).map((event) => `
        <div class="log-line">
          <span class="dot"></span>
          <span class="helper-text">${escapeHtml(eventLabel(event.message))}</span>
          <span class="helper-text">${escapeHtml(event.level || "")}</span>
        </div>`).join("");
    }

    const SETTINGS_DEFAULT = {
      cash_reserve_pct: 0.50,
      target_position_pct: 0.10,
      max_positions: 5,
      accept_top_n: 2,
      exit_ma_days: 75,
      lookback_days: 126,
      skip_days: 21,
      trend_ma_days: 200,
    };
    let settingsFormInitialized = false;
    let settingsInputsBound = false;
    let settingsWritable = false;
    let currentTossAccountSeq = "";
    let currentTossAccountAlias = "";
    let newestSeenEventId = null;
    let browserNotificationsEnabled = localStorage.getItem("turtleBrowserNotifications") === "enabled";
    let discordWebhookConfigured = false;

    const TRADE_NOTIFICATION_EVENTS = new Set([
      "live_order_execution",
      "live_order_cancel_after_ack",
    ]);

    const FAILURE_NOTIFICATION_EVENTS = new Set([
      "live_order_final_guard_blocked",
      "live_buying_power_unavailable",
      "live_service_blocked",
      "live_trading_loop_failed",
      "paper_service_blocked",
    ]);

    const WATCHLIST_NOTIFICATION_EVENTS = new Set([
      "premarket_watchlist_blocked",
      "paper_market_data_blocked",
      "market_data_rate_limit_paused",
    ]);

    function toFiniteNumber(value, fallback) {
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : fallback;
    }

    function clamp(value, minValue, maxValue) {
      return Math.max(minValue, Math.min(maxValue, value));
    }

    function safeInteger(value, fallback, minValue, maxValue) {
      const parsed = toFiniteNumber(value, NaN);
      if (!Number.isFinite(parsed)) {
        return fallback;
      }
      const intValue = parseInt(String(parsed), 10);
      return clamp(intValue, minValue, maxValue);
    }

    function momentumFromSettings(settings) {
      const source = (settings && settings.momentum) || settings || {};
      return {
        cash_reserve_pct: toFiniteNumber(source.cash_reserve_pct, SETTINGS_DEFAULT.cash_reserve_pct),
        target_position_pct: toFiniteNumber(source.target_position_pct, SETTINGS_DEFAULT.target_position_pct),
        max_positions: safeInteger(source.max_positions, SETTINGS_DEFAULT.max_positions, 1, 200),
        accept_top_n: safeInteger(source.accept_top_n, SETTINGS_DEFAULT.accept_top_n, 1, 200),
        exit_ma_days: safeInteger(source.exit_ma_days, SETTINGS_DEFAULT.exit_ma_days, 1, 10000),
        lookback_days: safeInteger(source.lookback_days, SETTINGS_DEFAULT.lookback_days, 1, 10000),
        skip_days: safeInteger(source.skip_days, SETTINGS_DEFAULT.skip_days, 0, 10000),
        trend_ma_days: safeInteger(source.trend_ma_days, SETTINGS_DEFAULT.trend_ma_days, 1, 10000),
      };
    }

    function normalizeMomentumPayload(payload) {
      const source = momentumFromSettings(payload);
      const reserve = clamp(source.cash_reserve_pct, 0, 1);
      return {
        cash_reserve_pct: reserve,
        max_exposure_pct: clamp(1 - reserve, 0, 1),
        target_position_pct: clamp(source.target_position_pct, 0, 1),
        max_positions: source.max_positions,
        accept_top_n: source.accept_top_n,
        exit_ma_days: source.exit_ma_days,
        lookback_days: source.lookback_days,
        skip_days: source.skip_days,
        trend_ma_days: source.trend_ma_days,
      };
    }

    function renderMomentumPreview(cashReservePercent) {
      const preview = document.getElementById("momentum-max-exposure-preview");
      if (!preview) return;
      const reserve = clamp(toFiniteNumber(cashReservePercent, 0), 0, 100);
      preview.textContent = `주식 최대 비중: ${clamp(100 - reserve, 0, 100).toFixed(1)}%`;
    }

    function setCashReserveValue(cashReservePercent) {
      const slider = document.getElementById("momentum-cash-reserve-percent-slider");
      const input = document.getElementById("momentum-cash-reserve-percent");
      const normalized = clamp(toFiniteNumber(cashReservePercent, 0), 0, 100);
      if (slider) slider.value = normalized.toFixed(1);
      if (input) input.value = normalized.toFixed(1);
      renderMomentumPreview(normalized);
    }

    function setMomentumSettings(payload) {
      const settings = normalizeMomentumPayload(payload);
      setCashReserveValue(settings.cash_reserve_pct * 100);
      const targetPosition = document.getElementById("momentum-target-position-pct");
      const maxPositions = document.getElementById("momentum-max-positions");
      const acceptTopN = document.getElementById("momentum-accept-top-n");
      const exitMaDays = document.getElementById("momentum-exit-ma-days");
      const lookbackDays = document.getElementById("momentum-lookback-days");
      const skipDays = document.getElementById("momentum-skip-days");
      const trendMaDays = document.getElementById("momentum-trend-ma-days");

      if (targetPosition) targetPosition.value = settings.target_position_pct;
      if (maxPositions) maxPositions.value = String(settings.max_positions);
      if (acceptTopN) acceptTopN.value = String(settings.accept_top_n);
      if (exitMaDays) exitMaDays.value = String(settings.exit_ma_days);
      if (lookbackDays) lookbackDays.value = String(settings.lookback_days);
      if (skipDays) skipDays.value = String(settings.skip_days);
      if (trendMaDays) trendMaDays.value = String(settings.trend_ma_days);

    }

    function setPillState(elementId, okText, todoText, isOk) {
      const element = document.getElementById(elementId);
      if (!element) return;
      element.textContent = isOk ? okText : todoText;
      element.className = `status-pill ${isOk ? "done" : "todo"}`;
    }

    function browserNotificationPermission() {
      if (!("Notification" in window)) return "unsupported";
      return Notification.permission;
    }

    function updateNotificationStatus(settings) {
      if (settings && settings.notifications) {
        discordWebhookConfigured = Boolean(settings.notifications.discord_webhook_configured);
      }
      const status = document.getElementById("notification-status");
      const button = document.getElementById("notification-enable-button");
      const discordButton = document.getElementById("discord-notification-test-button");
      const permission = browserNotificationPermission();
      if (status) {
        const discordText = discordWebhookConfigured
          ? "Discord 웹훅도 감지됐습니다."
          : "Discord 웹훅은 아직 이 서버에서 감지되지 않았습니다.";
        if (permission === "unsupported") {
          status.textContent = `이 브라우저는 시스템 알림을 지원하지 않습니다. 화면 안 알림은 계속 표시됩니다. ${discordText}`;
        } else if (permission === "granted" && browserNotificationsEnabled) {
          status.textContent = `브라우저 알림이 켜져 있습니다. 주문 수량과 실패만 골라서 알려드립니다. ${discordText}`;
        } else if (permission === "denied") {
          status.textContent = `브라우저에서 알림이 차단되어 있습니다. 브라우저 설정에서 허용해야 켤 수 있습니다. ${discordText}`;
        } else {
          status.textContent = `주문 수량과 실패는 화면 안 알림으로 보여드립니다. 원하면 브라우저 알림도 켤 수 있습니다. ${discordText}`;
        }
      }
      if (button) {
        button.disabled = permission === "unsupported" || permission === "denied";
        button.textContent = permission === "granted" && browserNotificationsEnabled
          ? "브라우저 알림 켜짐"
          : "브라우저 알림 켜기";
      }
      if (discordButton) {
        discordButton.disabled = !discordWebhookConfigured;
        discordButton.textContent = discordWebhookConfigured
          ? "Discord 테스트 전송"
          : "Discord 웹훅 없음";
      }
    }

    function showToast(title, body, kind = "info") {
      const stack = document.getElementById("notification-toast-stack");
      if (!stack) return;
      const toast = document.createElement("div");
      toast.className = `notification-toast ${kind}`;
      toast.innerHTML = `<strong>${escapeHtml(title)}</strong><p>${escapeHtml(body)}</p>`;
      stack.prepend(toast);
      while (stack.children.length > 4) {
        stack.lastElementChild.remove();
      }
      window.setTimeout(() => toast.remove(), 7000);
    }

    function notifyBrowser(title, body) {
      if (!browserNotificationsEnabled) return;
      if (browserNotificationPermission() !== "granted") return;
      try {
        new Notification(title, { body });
      } catch (_) {
        browserNotificationsEnabled = false;
        localStorage.removeItem("turtleBrowserNotifications");
        updateNotificationStatus();
      }
    }

    function notificationKind(entry) {
      const level = String(entry && entry.level || "INFO").toUpperCase();
      if (level === "ERROR") return "error";
      if (level === "WARN") return "warn";
      return "info";
    }

    function isImportantNotificationEvent(entry) {
      if (!entry) return false;
      const level = String(entry.level || "INFO").toUpperCase();
      const message = String(entry.message || "");
      if (TRADE_NOTIFICATION_EVENTS.has(message)) return true;
      if (FAILURE_NOTIFICATION_EVENTS.has(message)) return true;
      if (WATCHLIST_NOTIFICATION_EVENTS.has(message)) return true;
      if (level === "ERROR") return true;
      return level === "WARN" && (
        message.includes("blocked") ||
        message.includes("failed") ||
        message.includes("unavailable") ||
        message.includes("rejected")
      );
    }

    function tradeSideLabel(side) {
      const text = String(side || "").toUpperCase();
      if (text === "BUY") return "매수";
      if (text === "SELL") return "매도";
      return text || "주문";
    }

    function tradeStatusLabel(status) {
      const text = String(status || "").toUpperCase();
      const labels = {
        ACKNOWLEDGED: "주문 접수",
        FILLED: "체결 완료",
        PARTIALLY_FILLED: "일부 체결",
        PENDING_CANCEL: "취소 요청",
        CANCELLED: "취소 완료",
        REJECTED: "주문 거절",
        FAILED: "주문 실패",
        UNKNOWN: "확인 필요",
      };
      return labels[text] || text;
    }

    function compactTradeNotification(entry) {
      const payload = entry && entry.payload && typeof entry.payload === "object" ? entry.payload : {};
      const message = String(entry && entry.message || "");
      const symbol = payload.symbol || payload.ticker || payload.code || "종목 확인 필요";
      const accountAlias = payload.account_alias || "내 계좌";
      const side = tradeSideLabel(payload.side || payload.order_side || payload.trade_side);
      const quantity = payload.quantity || payload.qty || payload.order_quantity || payload.shares;
      const status = tradeStatusLabel(payload.status || payload.execution_status || payload.state);
      if (TRADE_NOTIFICATION_EVENTS.has(message)) {
        const qtyText = quantity ? `${quantity}주` : "수량 확인 필요";
        return {
          title: `${accountAlias} · ${symbol} ${side}`,
          body: `${qtyText}${status ? ` / 상태 ${status}` : ""}`,
        };
      }
      const detail = eventDetail(entry) || eventLabel(message);
      if (WATCHLIST_NOTIFICATION_EVENTS.has(message)) {
        const isRateLimit = message === "market_data_rate_limit_paused"
          || String(payload.error || "").includes("요청 제한")
          || String(payload.error || "").includes("rate-limit");
        return {
          title: isRateLimit
            ? "시세 조회 잠시 대기"
            : message === "paper_market_data_blocked"
              ? "시세 조회 확인 필요"
              : "관심 종목 생성 일부 실패",
          body: detail,
        };
      }
      if (message === "live_trading_loop_failed") {
        return {
          title: "자동매매 루프 실패",
          body: detail,
        };
      }
      return {
        title: "거래 실패/차단",
        body: `${accountAlias} · ${symbol}: ${detail}`,
      };
    }

    function notifyImportantEvents(events) {
      const rows = Array.isArray(events) ? events : [];
      const numericIds = rows
        .map((event) => Number(event.id))
        .filter((id) => Number.isFinite(id));
      if (!numericIds.length) return;
      const newest = Math.max(...numericIds);
      if (newestSeenEventId == null) {
        newestSeenEventId = newest;
        return;
      }
      const fresh = rows
        .filter((event) => Number(event.id) > newestSeenEventId)
        .filter(isImportantNotificationEvent)
        .sort((a, b) => Number(a.id) - Number(b.id));
      newestSeenEventId = Math.max(newestSeenEventId, newest);
      fresh.forEach((event) => {
        const notice = compactTradeNotification(event);
        const title = notice.title;
        const body = notice.body;
        showToast(title, body, notificationKind(event));
        notifyBrowser(title, body);
      });
    }

    async function enableBrowserNotifications() {
      if (!("Notification" in window)) {
        showToast("알림 미지원", "이 브라우저는 시스템 알림을 지원하지 않습니다.", "warn");
        updateNotificationStatus();
        return;
      }
      const permission = await Notification.requestPermission();
      if (permission === "granted") {
        browserNotificationsEnabled = true;
        localStorage.setItem("turtleBrowserNotifications", "enabled");
        showToast("알림 켜짐", "중요한 거래 이벤트가 생기면 알려드릴게요.", "info");
        notifyBrowser("토스 트레이딩 봇", "브라우저 알림이 켜졌습니다.");
      } else {
        browserNotificationsEnabled = false;
        localStorage.removeItem("turtleBrowserNotifications");
        showToast("알림 대기", "브라우저 알림 권한이 허용되지 않았습니다.", "warn");
      }
      updateNotificationStatus(dashboard.settings || {});
    }

    function testNotification() {
      showToast("정훈 미국주식 계좌 · SPCX 매수", "1주 / 상태 접수", "info");
      notifyBrowser("정훈 미국주식 계좌 · SPCX 매수", "1주 / 상태 접수");
    }

    async function sendDiscordTestNotification() {
      const button = document.getElementById("discord-notification-test-button");
      if (button) button.disabled = true;
      try {
        const payload = await postJson("/dashboard/actions/test-discord-alert", {});
        if (payload.status === "sent") {
          showToast("Discord 테스트 전송", "웹훅으로 테스트 메시지를 보냈습니다.", "info");
        } else if (payload.status === "not_configured") {
          showToast("Discord 웹훅 없음", "DISCORD_TRADE_ALERT_WEBHOOK_URL을 설정하고 서버를 다시 시작하세요.", "warn");
        } else {
          showToast("Discord 전송 실패", payload.error || "웹훅 응답을 확인하지 못했습니다.", "warn");
        }
        await refresh();
      } catch (error) {
        showToast("Discord 전송 실패", error.message, "error");
      } finally {
        if (button) button.disabled = !discordWebhookConfigured;
      }
    }

    function setTossSettings(settings) {
      const toss = (settings && settings.toss) || {};
      const accountSeq = document.getElementById("toss-account-seq");
      const accountAlias = document.getElementById("toss-account-alias");
      const clientId = document.getElementById("toss-client-id");
      const clientSecret = document.getElementById("toss-client-secret");
      const identityConfirmation = document.getElementById("toss-identity-confirmation");

      currentTossAccountSeq = String(toss[SETTINGS_KEYS.accountValue] || "");
      currentTossAccountAlias = String(toss[SETTINGS_KEYS.accountAlias] || "");
      if (accountSeq) accountSeq.value = currentTossAccountSeq;
      if (accountAlias) accountAlias.value = currentTossAccountAlias;
      if (clientId) clientId.value = "";
      if (clientSecret) clientSecret.value = "";
      if (identityConfirmation) identityConfirmation.value = "";

      setPillState("toss-client-id-status", "설정됨", "미설정", Boolean(toss.client_id_configured));
      setPillState("toss-client-secret-status", "설정됨", "미설정", Boolean(toss.client_secret_configured));
      setPillState("toss-account-status", "연결값 있음", "미연결", Boolean(toss[SETTINGS_KEYS.accountReady] || toss[SETTINGS_KEYS.accountValue]));
    }

    function settingsSaveButtons() {
      return ["settings-save-button", "toss-settings-save-button"]
        .map((id) => document.getElementById(id))
        .filter(Boolean);
    }

    function settingsSaveStatuses() {
      return ["settings-save-status", "toss-settings-save-status"]
        .map((id) => document.getElementById(id))
        .filter(Boolean);
    }

    function setSettingsStatus(text, className = "settings-status") {
      settingsSaveStatuses().forEach((status) => {
        status.textContent = text;
        status.className = className;
      });
    }

    function setSettingsWritable(enabled) {
      settingsWritable = Boolean(enabled);
      const saveButtons = settingsSaveButtons();
      const hint = document.getElementById("settings-save-hint");
      saveButtons.forEach((button) => {
        button.disabled = !settingsWritable;
      });
      if (hint) {
        hint.textContent = settingsWritable
          ? ""
          : "현재는 --config 경로가 없어 저장이 비활성입니다. config 파일 경로로 대시보드를 실행해야 저장이 가능합니다.";
      }
      if (!settingsWritable) {
        setSettingsStatus("저장 비활성 상태");
      }
    }

    async function saveMomentumSettings() {
      const slider = document.getElementById("momentum-cash-reserve-percent-slider");
      const cashReserveInput = document.getElementById("momentum-cash-reserve-percent");
      const targetPosition = document.getElementById("momentum-target-position-pct");
      const maxPositions = document.getElementById("momentum-max-positions");
      const acceptTopN = document.getElementById("momentum-accept-top-n");
      const exitMaDays = document.getElementById("momentum-exit-ma-days");
      const lookbackDays = document.getElementById("momentum-lookback-days");
      const skipDays = document.getElementById("momentum-skip-days");
      const trendMaDays = document.getElementById("momentum-trend-ma-days");
      const tossClientId = document.getElementById("toss-client-id");
      const tossClientSecret = document.getElementById("toss-client-secret");
      const tossAccountSeq = document.getElementById("toss-account-seq");
      const tossAccountAlias = document.getElementById("toss-account-alias");
      const tossIdentityConfirmation = document.getElementById("toss-identity-confirmation");
      const saveButtons = settingsSaveButtons();

      if (!settingsWritable) {
        setSettingsStatus("저장할 수 없습니다. --config 경로를 확인하세요.", "settings-status error");
        return;
      }
      if (!saveButtons.length) {
        return;
      }

      const payload = {
        momentum: {
          cash_reserve_pct: clamp(toFiniteNumber(cashReserveInput?.value, slider ? slider.value : 0) / 100, 0, 1),
          target_position_pct: clamp(toFiniteNumber(targetPosition?.value, SETTINGS_DEFAULT.target_position_pct), 0, 1),
          max_positions: safeInteger(maxPositions?.value, SETTINGS_DEFAULT.max_positions, 1, 200),
          accept_top_n: safeInteger(acceptTopN?.value, SETTINGS_DEFAULT.accept_top_n, 1, 200),
          exit_ma_days: safeInteger(exitMaDays?.value, SETTINGS_DEFAULT.exit_ma_days, 1, 10000),
          lookback_days: safeInteger(lookbackDays?.value, SETTINGS_DEFAULT.lookback_days, 1, 10000),
          skip_days: safeInteger(skipDays?.value, SETTINGS_DEFAULT.skip_days, 0, 10000),
          trend_ma_days: safeInteger(trendMaDays?.value, SETTINGS_DEFAULT.trend_ma_days, 1, 10000),
        },
      };
      const accountSeq = String(tossAccountSeq?.value || "").trim();
      const accountAlias = String(tossAccountAlias?.value || "").trim();
      const clientId = String(tossClientId?.value || "").trim();
      const clientSecret = String(tossClientSecret?.value || "").trim();
      const identityConfirmation = String(tossIdentityConfirmation?.value || "").trim();
      const tossPayload = {};
      if (accountSeq && accountSeq !== currentTossAccountSeq) tossPayload[SETTINGS_KEYS.accountValue] = accountSeq;
      if (accountAlias !== currentTossAccountAlias) tossPayload[SETTINGS_KEYS.accountAlias] = accountAlias;
      if (clientId) tossPayload.client_id = clientId;
      if (clientSecret) tossPayload.client_secret = clientSecret;
      if (Object.keys(tossPayload).length) {
        const connectionKeys = ["account_seq", "client_id", "client_secret", "client_id_env", "client_secret_env"];
        const connectionChangeRequested = connectionKeys.some((key) => Object.prototype.hasOwnProperty.call(tossPayload, key));
        if (connectionChangeRequested && identityConfirmation !== "토스 연결 승인") {
          setSettingsStatus("토스 API 키나 계좌 번호를 저장하려면 본인 확인 문구를 먼저 입력하세요.", "settings-status error");
          return;
        }
        if (connectionChangeRequested) {
          tossPayload.identity_confirmation = identityConfirmation;
        }
        payload.toss = tossPayload;
      }

      saveButtons.forEach((button) => {
        button.disabled = true;
      });
      setSettingsStatus("저장 중입니다...");
      try {
        const response = await fetch("/dashboard/settings", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify(payload),
        });
        const body = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(body.error || `저장 실패 (${response.status})`);
        }
        setSettingsStatus("저장 완료", "settings-status ok");
        if (body.settings) {
          if (body.settings.momentum) {
            setMomentumSettings(body.settings.momentum);
          }
          setTossSettings(body.settings);
        }
      } catch (error) {
        setSettingsStatus(`저장 실패: ${error.message}`, "settings-status error");
      } finally {
        saveButtons.forEach((button) => {
          button.disabled = !settingsWritable;
        });
      }
    }

    function bindSettingsInteractions() {
      const slider = document.getElementById("momentum-cash-reserve-percent-slider");
      const input = document.getElementById("momentum-cash-reserve-percent");
      const saveButtons = settingsSaveButtons();
      if (slider && input) {
        if (!settingsInputsBound) {
          slider.addEventListener("input", () => {
            const value = clamp(toFiniteNumber(slider.value, SETTINGS_DEFAULT.cash_reserve_pct * 100), 0, 100);
            setCashReserveValue(value);
          });
          input.addEventListener("input", () => {
            const value = clamp(toFiniteNumber(input.value, SETTINGS_DEFAULT.cash_reserve_pct * 100), 0, 100);
            setCashReserveValue(value);
          });
          settingsInputsBound = true;
        }
      }
      saveButtons.forEach((saveButton) => {
        if (!saveButton.dataset.bound) {
          saveButton.dataset.bound = "true";
          saveButton.addEventListener("click", saveMomentumSettings);
        }
      });
    }

    function renderOnboarding(blockers, events, settings, settingsWriteEnabled) {
      const list = document.getElementById("settings-onboarding-list");
      const rawBlockers = Array.isArray(blockers) ? blockers : [];
      const recentEvents = Array.isArray(events) ? events : [];
      const headline = document.getElementById("settings-headline");
      if (headline) {
        headline.textContent = rawBlockers.length
          ? `먼저 ${groupedBlockerDetails(rawBlockers)[0]} 항목부터 확인하세요.`
          : "필수 입력은 끝났습니다. 안전 파일럿을 시작하기 전에 최근 점검 기록만 확인하세요.";
      }
      if (list) {
        list.innerHTML = ONBOARDING_STEPS.map((step, index) => {
          const matched = rawBlockers.filter(step.match);
          const eventSeen = step.eventMessage
            ? recentEvents.some((event) => event.message === step.eventMessage)
            : true;
          const kind = matched.length || !eventSeen ? "warn" : "done";
          const detail = matched.length
            ? uniqueValues(matched.map(blockerLabel)).join(" ")
            : eventSeen
              ? step.body
              : "아직 최근 자동 점검 기록이 없습니다.";
          return `<li>
            <strong>${index + 1}. ${escapeHtml(step.title)}</strong>
            <p><span class="status-pill">${escapeHtml(step.group)}</span></p>
            <p>${escapeHtml(detail)}</p>
            <span class="status-pill ${kind}">${statusText(kind)}</span>
          </li>`;
        }).join("");
      }
      const blockerBox = document.getElementById("settings-blockers-list");
      if (blockerBox) blockerBox.textContent = rawBlockers.length
        ? groupedBlockerDetails(rawBlockers).join("\\n")
        : "현재 추가로 입력할 필수 항목이 없습니다.";
      const configInfo = settings && settings.config ? settings.config : {};
      if (configInfo.created_from_template) {
        const headline = document.getElementById("settings-headline");
        if (headline) {
          headline.innerHTML = `<strong>설정 파일이 준비됐습니다</strong> ${escapeHtml(configInfo.path || "config/local.yaml")}에서 필요한 값만 채우면 됩니다.`;
        }
      }
      if (!settingsFormInitialized) {
        setMomentumSettings(settings || SETTINGS_DEFAULT);
        setTossSettings(settings || {});
        settingsFormInitialized = true;
      }
      bindSettingsInteractions();
      setSettingsWritable(Boolean(settingsWriteEnabled));
    }

    async function refresh() {
      const [dashboard, health, positions, openOrders, watchlist, events, summary] = await Promise.all([
        getJson("/dashboard"),
        getJson("/health"),
        getJson("/positions"),
        getJson("/orders/open"),
        getJson("/watchlist"),
        getJson("/events?limit=50"),
        getJson("/events/summary?limit=50")
      ]);

      const status = dashboard.status || health || {};
      const watchRows = payloadItems(watchlist, "watchlist");
      const positionRows = payloadItems(positions, "positions");
      const orderRows = payloadItems(openOrders, "open_orders").length
        ? payloadItems(openOrders, "open_orders")
        : payloadItems(dashboard.paper_intents, "open_orders");
      const eventRows = payloadItems(events, "items");

      renderMetricCards(status, watchRows, positionRows, orderRows, summary);
      renderOperatorBrief(status, eventRows, watchRows, positionRows, orderRows);
      renderSidebarStatus(status);
      renderHealthPanel(status);
      renderWatchSummary(watchRows);
      renderPositionSummary(positionRows);
      renderLiveReadiness(dashboard.live_readiness || {});
      renderLiveMonitor(dashboard.live_monitor || {});
      renderTable("dashboard-open-orders-table", orderRows, null, "미체결 주문이 없습니다", "페이퍼 모드에서 주문 후보가 생기면 여기에 표시됩니다.", "#events");
      renderTimeline("dashboard-events-timeline", eventRows);
      renderBotSummary(status, summary, watchRows, orderRows);
      renderLogLines(eventRows);
      notifyImportantEvents(eventRows);
      updateNotificationStatus(dashboard.settings || {});

      setCountBadge("watchlist-count-badge", watchRows.length);
      setCountBadge("positions-count-badge", positionRows.length);
      setCountBadge("orders-count-badge", orderRows.length);
      setCountBadge("events-count-badge", eventRows.length);

      renderOnboarding(
        status.blockers || [],
        eventRows,
        dashboard.settings || {},
        dashboard.settings_write_enabled
      );
      setSafePilotControls(status, dashboard.settings || {});
      renderSetupFlow(status, dashboard.settings || {});
      renderPilotSummary(dashboard.settings || {});
    }

    function bindNavigation() {
      document.body.addEventListener("click", (event) => {
        const anchor = event.target.closest("a[data-view]");
        if (!anchor) return;
        const view = anchor.dataset.view;
        if (!view) return;
        event.preventDefault();
        if (initialView() === view) {
          setActiveView(view);
          return;
        }
        window.location.hash = view;
      });
      const button = document.getElementById("refresh-button");
      if (button) button.addEventListener("click", () => refresh().catch(console.error));
      const liveOnceButton = document.getElementById("live-once-button");
      if (liveOnceButton) liveOnceButton.addEventListener("click", () => runLiveOnce().catch(console.error));
      const liveSmokeButton = document.getElementById("live-smoke-test-button");
      if (liveSmokeButton) liveSmokeButton.addEventListener("click", () => runLiveSmokeTest().catch(console.error));
      const safePilotButton = document.getElementById("safe-pilot-button");
      if (safePilotButton) safePilotButton.addEventListener("click", () => applySafePilot().catch(console.error));
      const onboardingSafePilotButton = document.getElementById("onboarding-safe-pilot-button");
      if (onboardingSafePilotButton) {
        onboardingSafePilotButton.addEventListener("click", () => applySafePilot("onboarding-safe-pilot-button", "onboarding-live-action-result").catch(console.error));
      }
      const liveStopButton = document.getElementById("live-stop-button");
      if (liveStopButton) liveStopButton.addEventListener("click", () => stopTrading().catch(console.error));
      const onboardingLiveStopButton = document.getElementById("onboarding-live-stop-button");
      if (onboardingLiveStopButton) {
        onboardingLiveStopButton.addEventListener("click", () => stopTrading("onboarding-live-stop-button", "onboarding-live-action-result").catch(console.error));
      }
      const settingsLiveStopButton = document.getElementById("settings-live-stop-button");
      if (settingsLiveStopButton) {
        settingsLiveStopButton.addEventListener("click", () => stopTrading("settings-live-stop-button", "settings-live-stop-result").catch(console.error));
      }
      ["live-public-ip-check-button", "settings-public-ip-check-button"].forEach((id) => {
        const ipButton = document.getElementById(id);
        if (ipButton) ipButton.addEventListener("click", () => checkTossPublicIp(id).catch(console.error));
      });
      ["live-public-ip-copy-button", "settings-public-ip-copy-button"].forEach((id) => {
        const copyButton = document.getElementById(id);
        if (copyButton) copyButton.addEventListener("click", () => copyTossPublicIp().catch(console.error));
      });
      const notificationEnableButton = document.getElementById("notification-enable-button");
      if (notificationEnableButton) {
        notificationEnableButton.addEventListener("click", () => enableBrowserNotifications().catch(console.error));
      }
      const notificationTestButton = document.getElementById("notification-test-button");
      if (notificationTestButton) {
        notificationTestButton.addEventListener("click", testNotification);
      }
      const discordNotificationTestButton = document.getElementById("discord-notification-test-button");
      if (discordNotificationTestButton) {
        discordNotificationTestButton.addEventListener("click", () => sendDiscordTestNotification().catch(console.error));
      }
    }

    function initialView() {
      const allowed = new Set(["dashboard", "watchlist", "positions", "orders", "live", "events", "settings"]);
      const hash = window.location.hash ? window.location.hash.slice(1) : "dashboard";
      return allowed.has(hash) ? hash : "dashboard";
    }

    bindNavigation();
    setActiveView(initialView());
    updateDashboardClock();
    window.addEventListener("hashchange", () => setActiveView(initialView()));
    refresh().catch(console.error);
    setInterval(updateDashboardClock, 1000);
    setInterval(() => refresh().catch(console.error), 6000);
  </script>
</body>
</html>"""
    return _skeleton_reference


def _legacy_dashboard_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Toss Turtle Bot Dashboard</title>
  <style>
    :root {
      --bg: #f6f8fb;
      --panel: #ffffff;
      --text: #0f172a;
      --muted: #475569;
      --line: #dbe2ea;
      --ok: #0ea5a5;
      --warn: #f59e0b;
      --bad: #dc2626;
      --shadow: 0 14px 30px rgba(15, 23, 42, 0.08);
    }

    * {
      box-sizing: border-box;
    }

    html, body {
      margin: 0;
      width: 100%;
      min-height: 100%;
      font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at 70% 10%, rgba(37, 99, 235, 0.05), transparent 30%),
        linear-gradient(180deg, #fbfcff 0%, var(--bg) 48%, #f5f8fc 100%);
      overflow-x: hidden;
    }

    a,
    button {
      font: inherit;
    }

    .topbar {
      height: 82px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 20px;
      padding: 0 28px;
      background: rgba(255, 255, 255, 0.9);
      border-bottom: 1px solid var(--line);
      backdrop-filter: blur(14px);
      -webkit-backdrop-filter: blur(14px);
      position: sticky;
      top: 0;
      z-index: 20;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 14px;
      min-width: 0;
    }

    .logo {
      width: 40px;
      height: 40px;
      border-radius: 8px;
      display: grid;
      place-items: center;
      background: linear-gradient(145deg, #3b82f6, #1d4ed8);
      box-shadow: 0 12px 24px rgba(37, 99, 235, 0.22);
      color: #ffffff;
      flex: 0 0 auto;
    }

    .logo::after {
      content: "";
      width: 18px;
      height: 18px;
      border-radius: 50%;
      background: #ffffff;
      opacity: 0.96;
    }

    .brand-text {
      min-width: 0;
      display: grid;
      gap: 3px;
    }

    .brand-text strong {
      font-size: 19px;
      line-height: 1.1;
      white-space: nowrap;
    }

    .brand-text span {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.25;
      white-space: nowrap;
    }

    .top-actions {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 10px;
      min-width: 0;
    }

    .top-clock {
      color: var(--muted);
      font-size: 13px;
      font-weight: 700;
      white-space: nowrap;
    }

    .btn {
      height: 44px;
      border-radius: 8px;
      border: 1px solid var(--line);
      background: #ffffff;
      color: #1f2a44;
      padding: 0 15px;
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-weight: 800;
      cursor: pointer;
      text-decoration: none;
      box-shadow: 0 10px 20px rgba(31, 46, 76, 0.04);
      white-space: nowrap;
    }

    .btn.primary {
      background: linear-gradient(145deg, #2f6df4, #1d4ed8);
      color: #ffffff;
      border-color: #1d4ed8;
      box-shadow: 0 14px 26px rgba(37, 99, 235, 0.22);
    }

    .btn svg {
      width: 18px;
      height: 18px;
      stroke-width: 2.4;
      flex: 0 0 auto;
    }

    .page-shell {
      min-height: calc(100vh - 82px);
      display: grid;
      grid-template-columns: 190px minmax(0, 1fr);
      width: 100%;
      min-width: 0;
    }

    .sidebar {
      background: rgba(255, 255, 255, 0.82);
      color: var(--text);
      border-right: 1px solid var(--line);
      padding: 28px 18px 22px;
      display: flex;
      flex-direction: column;
      gap: 20px;
    }

    .nav {
      display: grid;
      gap: 14px;
    }

    .nav a,
    .icon-button {
      min-height: 50px;
      color: #8492aa;
      text-decoration: none;
      border-radius: 8px;
      padding: 0 14px;
      display: flex;
      align-items: center;
      gap: 12px;
      border: 1px solid transparent;
      font-size: 13px;
      font-weight: 800;
    }

    .nav a.active {
      color: #2563eb;
      background: #edf4ff;
      border-color: #e7eefb;
    }

    .nav a svg,
    .icon-button svg {
      width: 19px;
      height: 19px;
      stroke-width: 2.2;
      flex: 0 0 auto;
    }

    .icon-button {
      margin-top: auto;
      min-height: 42px;
    }

    .main {
      min-width: 0;
      padding: 22px;
      max-width: 100%;
    }

    .view-shell {
      padding: 0;
      background: transparent;
      display: grid;
      gap: 16px;
      min-width: 0;
      max-width: 100%;
    }

    .dashboard-header {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      margin-bottom: 2px;
    }

    .dashboard-title {
      margin: 0;
      font-size: 24px;
      line-height: 1.15;
    }

    .dashboard-header > div {
      min-width: 0;
    }

    .dashboard-subtitle {
      margin: 5px 0 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }

    .card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      box-shadow: var(--shadow);
      min-width: 0;
      max-width: 100%;
    }

    .summary-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
    }

    .metric-title {
      margin: 0;
      color: var(--muted);
      font-size: 13px;
      text-transform: uppercase;
      letter-spacing: 0.2px;
    }

    .metric-value {
      font-size: 30px;
      font-weight: 700;
      display: block;
      margin-top: 6px;
    }

    .view {
      display: none;
      flex-direction: column;
      gap: 12px;
      min-width: 0;
    }

    .view.active {
      display: grid;
      gap: 14px;
    }

    .view-grid,
    .view-split,
    .raw-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 14px;
    }

    .panel-title {
      margin: 0 0 8px;
      font-size: 16px;
    }

    .status-copy,
    .status-strip,
    .status-mini {
      border-radius: 6px;
      padding: 10px;
      border: 1px solid var(--line);
      background: #f8fafc;
      color: var(--muted);
      display: grid;
      gap: 4px;
      min-width: 0;
      overflow-wrap: anywhere;
    }

    .status-mini-label {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
    }

    .status-mini-value,
    .status-copy p {
      margin: 0;
      color: var(--text);
      line-height: 1.35;
      font-size: 13px;
    }

    .action-list {
      display: grid;
      gap: 8px;
      margin: 0;
      padding: 0;
      list-style: none;
    }

    .action-list li {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 12px;
      display: grid;
      gap: 4px;
      min-width: 0;
    }

    .action-list li strong {
      font-size: 14px;
      overflow-wrap: anywhere;
    }

    .action-list li p {
      margin: 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.4;
      overflow-wrap: anywhere;
    }

    .status-pill {
      border-radius: 99px;
      padding: 2px 10px;
      width: fit-content;
      font-size: 12px;
      background: #e2e8f0;
      color: #1f2937;
      border: 1px solid #cbd5e1;
    }

    .status-pill.ok { background: #dcfce7; border-color: #86efac; color: #166534; }
    .status-pill.warn { background: #fef3c7; border-color: #f59e0b; color: #92400e; }
    .status-pill.blocked { background: #fee2e2; border-color: #fca5a5; color: #991b1b; }
    .status-pill.todo { background: #eff6ff; border-color: #bfdbfe; color: #1d4ed8; }
    .status-pill.done { background: #dcfce7; border-color: #86efac; color: #166534; }

    .detail-table {
      border: 1px solid var(--line);
      border-radius: 8px;
      margin: 0;
      padding: 10px;
      max-height: 320px;
      overflow: auto;
      background: #fff;
    }

    .health-cards {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
    }

    .health-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      background: #f8fafc;
      min-width: 0;
    }

    .health-card strong {
      display: block;
      font-size: 12px;
      color: var(--muted);
      margin-bottom: 6px;
    }

    .health-card span {
      display: block;
      font-size: 13px;
      line-height: 1.4;
      overflow-wrap: anywhere;
      color: var(--text);
    }

    .detail-table table {
      width: 100%;
      border-collapse: collapse;
    }

    .detail-table th,
    .detail-table td {
      text-align: left;
      border-bottom: 1px solid var(--line);
      font-size: 12px;
      padding: 8px 6px;
      vertical-align: top;
      overflow-wrap: anywhere;
      word-break: break-word;
    }

    .timeline {
      list-style: none;
      margin: 0;
      padding: 0;
      display: grid;
      gap: 8px;
    }

    .timeline li {
      border-left: 3px solid #94a3b8;
      padding-left: 10px;
      font-size: 13px;
      color: var(--muted);
    }

    .timeline .level-ERROR { border-color: var(--bad); color: #b91c1c; }
    .timeline .level-WARN { border-color: var(--warn); color: #b45309; }
    .timeline .level-INFO { border-color: var(--ok); color: #065f46; }

    .endpoint-list {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }

    .endpoint-list button {
      border: 1px solid #cbd5e1;
      border-radius: 99px;
      background: #fff;
      padding: 6px 10px;
      font-size: 12px;
      cursor: pointer;
    }

    .endpoint-list button.active {
      background: #e2e8f0;
      border-color: #64748b;
    }

    .view-json {
      background: #0f172a;
      color: #cbd5e1;
      border-radius: 8px;
      padding: 10px;
      margin: 0;
      overflow: auto;
      max-height: 340px;
      font-size: 12px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      white-space: pre;
      overflow-wrap: anywhere;
    }

    .view-meta {
      margin: 0 0 8px;
      color: #64748b;
      font-size: 13px;
    }

    .blocker-list {
      display: block;
      white-space: pre-wrap;
      font-size: 13px;
      color: var(--text);
      padding: 10px;
      border: 1px dashed var(--line);
      border-radius: 8px;
      background: #f8fafc;
    }

    .bottom-nav {
      position: fixed;
      left: 14px;
      right: 14px;
      bottom: calc(14px + env(safe-area-inset-bottom));
      transform: none;
      width: auto;
      background: rgba(255, 255, 255, 0.94);
      display: none;
      gap: 0;
      padding: 10px 12px;
      border: 1px solid rgba(226, 232, 240, 0.92);
      border-radius: 999px;
      box-shadow: 0 18px 45px rgba(15, 23, 42, 0.16);
      justify-content: stretch;
      z-index: 50;
      box-sizing: border-box;
      backdrop-filter: blur(18px);
      -webkit-backdrop-filter: blur(18px);
    }

    .bottom-nav a {
      color: #8b95a1;
      text-decoration: none;
      flex: 1 1 0;
      min-width: 0;
      border-radius: 18px;
      padding: 7px 2px 6px;
      display: flex;
      justify-content: center;
      align-items: center;
      gap: 5px;
      font-size: 11px;
      font-weight: 700;
      line-height: 1.1;
      text-align: center;
      border: 1px solid transparent;
      flex-direction: column;
      white-space: nowrap;
      transition: color 160ms ease, transform 160ms ease;
    }

    .bottom-nav a.active {
      color: #2563eb;
    }

    .bottom-nav a:active {
      transform: translateY(1px);
    }

    .bottom-nav a svg {
      width: 22px;
      height: 22px;
      stroke-width: 2.2;
    }

    .sr-data {
      position: absolute;
      width: 1px;
      height: 1px;
      overflow: hidden;
      left: -10000px;
      top: auto;
    }

    @media (max-width: 1100px) {
      .page-shell {
        grid-template-columns: 172px minmax(0, 1fr);
      }

      .topbar {
        padding: 0 20px;
      }

      .summary-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .top-clock {
        display: none;
      }
    }

    @media (max-width: 820px) {
      .topbar {
        display: none;
      }

      .page-shell {
        min-height: 100vh;
        display: block;
      }

      .sidebar {
        display: none;
      }

      .main {
        padding: 0;
      }

      .view-shell {
        padding: 10px;
        padding-bottom: 128px;
      }

      .summary-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .view-grid,
      .view-split,
      .raw-grid {
        grid-template-columns: 1fr;
      }

      .dashboard-header {
        display: grid;
        gap: 8px;
      }

      .dashboard-title {
        font-size: 20px;
      }

      .dashboard-subtitle {
        display: none;
      }

      .metric-value {
        font-size: 28px;
      }

      .health-cards {
        grid-template-columns: 1fr;
      }

      .bottom-nav {
        display: flex;
        left: 10px;
        right: 10px;
        width: auto;
        max-width: none;
        transform: none;
        bottom: calc(12px + env(safe-area-inset-bottom));
        padding: 9px 8px;
      }

      .bottom-nav a {
        font-size: 11px;
      }

      .bottom-nav a svg {
        width: 22px;
        height: 22px;
      }
    }

    @media (max-width: 420px) {
      .bottom-nav {
        left: 8px;
        right: 8px;
        width: auto;
        max-width: none;
        padding: 8px 6px;
      }

      .bottom-nav a {
        font-size: 10px;
        gap: 4px;
      }

      .bottom-nav a svg {
        width: 21px;
        height: 21px;
      }
    }

  </style>
</head>
<body>
  <header class="topbar">
    <div class="brand">
      <span class="logo" aria-hidden="true"></span>
      <span class="brand-text">
        <strong>Toss Turtle Bot</strong>
        <span>Read-only runtime dashboard</span>
      </span>
    </div>
    <div class="top-actions">
      <span class="top-clock">Local dashboard</span>
      <button class="btn primary" type="button" onclick="refresh()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M21 12a9 9 0 0 1-9 9 9.8 9.8 0 0 1-6.4-2.4"></path><path d="M3 12a9 9 0 0 1 15.4-6.4"></path><path d="M18 2v4h-4"></path><path d="M6 22v-4h4"></path></svg>
        새로고침
      </button>
      <a class="btn" href="/dashboard" target="_blank" rel="noopener">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"></path><path d="M14 2v6h6"></path><path d="M8 13h8"></path><path d="M8 17h5"></path></svg>
        JSON 열기
      </a>
    </div>
  </header>
  <div class="page-shell">
    <aside class="sidebar">
      <nav class="nav" aria-label="Dashboard sections">
        <a class="active" href="#dashboard" data-view="dashboard"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><rect x="3" y="3" width="7" height="7"></rect><rect x="14" y="3" width="7" height="7"></rect><rect x="3" y="14" width="7" height="7"></rect><rect x="14" y="14" width="7" height="7"></rect></svg>Dashboard</a>
        <a href="#watchlist" data-view="watchlist"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="m4 17 5-5 4 4 7-8"></path><path d="M16 8h4v4"></path></svg>Watchlist</a>
        <a href="#positions" data-view="positions"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M4 19V7a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v12"></path><path d="M8 5V3h8v2"></path><path d="M4 11h16"></path></svg>Positions</a>
        <a href="#events" data-view="events"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><rect x="7" y="3" width="10" height="18" rx="2"></rect><path d="M10 8h4"></path><path d="M10 12h4"></path><path d="M10 16h2"></path></svg>Events</a>
        <a href="#raw" data-view="raw"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M21 16v-2H3v2a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2ZM3 8h18v4H3z"></path><path d="M3 8l6 5 5-3 7 3"></path></svg>Raw/API</a>
        <a href="#settings" data-view="settings"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 0 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6V21a2 2 0 0 1-4 0v-.1a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 0 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.6-1H3a2 2 0 0 1 0-4h.1a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 0 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3 1.7 1.7 0 0 0 1-1.6V3a2 2 0 0 1 4 0v.1a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 0 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.1a2 2 0 0 1 0 4h-.1a1.7 1.7 0 0 0-1.6 1Z"></path></svg>Settings</a>
      </nav>
      <a class="icon-button" style="margin-top:auto" href="#theme" title="Theme">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M12 3a8.8 8.8 0 0 0 9 11.7A9 9 0 1 1 12 3Z"></path></svg>
      </a>
    </aside>

    <main class="main">
      <div class="view-shell">
        <header class="dashboard-header">
          <div>
            <h1 class="dashboard-title">Toss Turtle Bot</h1>
            <p class="dashboard-subtitle">Read-only dashboard for setup, scan status, paper positions, and runtime events.</p>
          </div>
          <span class="status-pill todo">Read-only</span>
        </header>

        <section id="view-dashboard" class="view active" data-view="dashboard">
          <section class="summary-grid">
            <article class="card">
              <strong class="metric-title">Mode</strong>
              <span id="metric-mode-text" class="metric-value">Loading</span>
              <span id="metric-mode-pill" class="status-pill">status</span>
            </article>
            <article class="card">
              <strong class="metric-title">Watchlist</strong>
              <span id="metric-watchlist-text" class="metric-value">0</span>
              <span class="status-pill">symbols</span>
            </article>
            <article class="card">
              <strong class="metric-title">Positions</strong>
              <span id="metric-positions-text" class="metric-value">0</span>
              <span class="status-pill">open</span>
            </article>
            <article class="card">
              <strong class="metric-title">Paper Intents</strong>
              <span id="metric-intents-text" class="metric-value">0</span>
              <span class="status-pill">today</span>
            </article>
          </section>

          <section class="card">
            <h2 class="panel-title">System Status</h2>
            <div id="dashboard-status-strip" class="status-strip status-copy"></div>
            <div id="dashboard-status-copy" class="status-copy"></div>
            <div id="dashboard-action-list" class="action-list"></div>
            <div id="dashboard-blockers-list" class="blocker-list"></div>
          </section>

          <section class="card">
            <h2 class="panel-title">Health Details</h2>
            <div id="dashboard-health-list" class="detail-table"></div>
          </section>

          <section class="card">
            <h2 class="panel-title">Open Orders</h2>
            <div id="dashboard-open-orders-table" class="detail-table"></div>
          </section>

          <section class="card">
            <h2 class="panel-title">Events Summary</h2>
            <div id="dashboard-events-summary" class="status-copy"></div>
          </section>

          <section class="card">
            <h2 class="panel-title">Latest Events</h2>
            <ul id="dashboard-events-timeline" class="timeline"></ul>
          </section>
        </section>

        <section id="view-watchlist" class="view" data-view="watchlist">
          <section class="view-grid">
            <article class="card">
              <h2 class="panel-title">Watchlist Items</h2>
              <div id="watchlist-table" class="detail-table"></div>
            </article>
            <article class="card">
              <h2 class="panel-title">Watchlist Raw JSON</h2>
              <pre id="watchlist-json" class="view-json"></pre>
            </article>
          </section>
          <section class="card">
            <h2 class="panel-title">Watchlist Signals</h2>
            <div class="status-mini">
              <span class="status-mini-label">Source</span>
              <span class="status-mini-value">Watchlist data is loaded from /watchlist and dashboard data.</span>
            </div>
          </section>
        </section>

        <section id="view-positions" class="view" data-view="positions">
          <section class="view-grid">
            <article class="card">
              <h2 class="panel-title">Positions Items</h2>
              <div id="positions-table" class="detail-table"></div>
            </article>
            <article class="card">
              <h2 class="panel-title">Positions Raw JSON</h2>
              <pre id="positions-json" class="view-json"></pre>
            </article>
          </section>
          <section class="card">
            <h2 class="panel-title">Position Signals</h2>
            <div class="status-mini">
              <span class="status-mini-label">Signal</span>
              <span class="status-mini-value">Position data is loaded from /positions and dashboard health checks.</span>
            </div>
          </section>
        </section>

        <section id="view-orders" class="view" data-view="orders">
          <section class="view-grid">
            <article class="card">
              <h2 class="panel-title">Open Orders</h2>
              <div id="orders-table" class="detail-table"></div>
            </article>
            <article class="card">
              <h2 class="panel-title">Orders Raw JSON</h2>
              <pre id="orders-json" class="view-json"></pre>
            </article>
          </section>
          <section class="card">
            <h2 class="panel-title">Order Signals</h2>
            <div class="status-mini">
              <span class="status-mini-label">Source</span>
              <span class="status-mini-value">Open order data is loaded from /orders/open and dashboard paper intents.</span>
            </div>
          </section>
        </section>

        <section id="view-events" class="view" data-view="events">
          <section class="view-grid">
            <article class="card">
              <h2 class="panel-title">Events Summary</h2>
              <div id="events-summary-strip" class="status-strip status-copy"></div>
            </article>
            <article class="card">
              <h2 class="panel-title">Events Timeline</h2>
              <ul id="events-timeline" class="timeline"></ul>
            </article>
          </section>
          <section class="card">
            <h2 class="panel-title">Event Items</h2>
            <div id="events-table" class="detail-table"></div>
          </section>
          <section class="card">
            <h2 class="panel-title">Events Raw JSON</h2>
            <pre id="events-json" class="view-json"></pre>
          </section>
        </section>

        <section id="view-raw" class="view" data-view="raw">
          <section class="view-grid raw-grid">
            <article class="card">
              <h2 class="panel-title">Raw Endpoint List</h2>
              <div id="endpoint-list" class="endpoint-list"></div>
            </article>
            <article class="card">
              <h2 class="panel-title">Endpoint JSON</h2>
              <p class="view-meta">Selected endpoint: <span id="raw-endpoint-label"></span></p>
              <pre id="raw-endpoint-json" class="view-json"></pre>
            </article>
          </section>
          <section class="card">
            <h2 class="panel-title">Aggregate Payload</h2>
            <pre id="raw-aggregate-json" class="view-json"></pre>
          </section>
        </section>

        <section id="view-settings" class="view" data-view="settings">
          <section class="card">
            <h2 class="panel-title">Settings and Toss Onboarding</h2>
            <p id="settings-headline" class="status-copy">
              <strong>New user onboarding</strong>
              This guide helps you set up Toss credentials safely before running paper service.
            </p>
            <ul id="settings-onboarding-list" class="action-list"></ul>
          </section>
          <section class="card">
            <h2 class="panel-title">Credential Safety</h2>
            <div class="status-copy">
              <strong>Never paste secrets in chat.</strong>
              <p>Store <code>TOSS_CLIENT_ID</code> and <code>TOSS_CLIENT_SECRET</code> only in environment variables or OS secure storage.</p>
              <p>Do not commit <code>config/local.yaml</code> or any secret values to git.</p>
            </div>
          </section>
          <section class="card">
            <h2 class="panel-title">Cash Reserve</h2>
            <div class="status-copy">
              <strong>Use <code>strategy.momentum.cash_reserve_pct</code>.</strong>
              <p><code>0.50</code> keeps at least 50% cash and limits momentum stock exposure to 50%.</p>
              <p><code>target_position_pct</code> still controls one new position size.</p>
            </div>
            <pre id="settings-strategy-json" class="view-json"></pre>
          </section>
          <section class="card">
            <h2 class="panel-title">Checklist Signals</h2>
            <div id="settings-blockers-list" class="blocker-list"></div>
            <div class="status-copy">
              <strong>Raw/API diagnostics</strong>
              <pre id="settings-raw-links" class="view-json"></pre>
            </div>
          </section>
        </section>

        <p class="sr-data">Local dashboard only. This interface reads bot state and never submits orders. Data is loaded from read-only endpoints.</p>
      </div>
    </main>
  </div>

  <nav class="bottom-nav" aria-label="Mobile dashboard sections">
    <a class="active" href="#dashboard" data-view="dashboard"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><rect x="3" y="3" width="7" height="7"></rect><rect x="14" y="3" width="7" height="7"></rect><rect x="3" y="14" width="7" height="7"></rect><rect x="14" y="14" width="7" height="7"></rect></svg>대시보드</a>
    <a href="#watchlist" data-view="watchlist"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="m4 17 5-5 4 4 7-8"></path><path d="M16 8h4v4"></path></svg>관심 종목</a>
    <a href="#positions" data-view="positions"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M13 2.1a10 10 0 1 0 8.9 8.9H13V2.1Z"></path><path d="M15 2.1V9h6.9"></path></svg>포지션</a>
    <a href="#orders" data-view="orders"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><rect x="7" y="3" width="10" height="18" rx="2"></rect><path d="M10 8h4"></path><path d="M10 12h4"></path><path d="M10 16h2"></path></svg>주문</a>
    <a href="#settings" data-view="settings"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 0 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6V21a2 2 0 0 1-4 0v-.1a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 0 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.6-1H3a2 2 0 0 1 0-4h.1a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 0 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3 1.7 1.7 0 0 0 1-1.6V3a2 2 0 0 1 4 0v.1a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 0 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.1a2 2 0 0 1 0 4h-.1a1.7 1.7 0 0 0-1.6 1Z"></path></svg>설정</a>
  </nav>

  <script>
    const ONBOARDING_STEPS = [
      { title: "Step 1: Prepare config/local.yaml", body: "Keep config local and live trading off." },
      { title: "Step 2: Issue Toss API credentials", body: "Create a Toss app and get the two API keys." },
      { title: "Step 3: Store credentials safely", body: "Store keys locally, not in chat or git." },
      { title: "Step 4: Configure toss.account_seq", body: "Add the target account sequence locally." },
      { title: "Step 5: Add scan universe and symbols", body: "Add symbols for the scanner to inspect." },
      { title: "Step 6: Run paper service and verify events", body: "Run paper service, then check Events." }
    ];

    function setActiveView(target) {
      document.querySelectorAll('.view').forEach((view) => {
        view.classList.toggle('active', view.getAttribute('data-view') === target);
      });
      document.querySelectorAll('[href^=\"#\"]').forEach((anchor) => {
        if (anchor.getAttribute('data-view') === target) {
          anchor.classList.add('active');
          anchor.setAttribute('aria-current', 'true');
        } else if (anchor.classList.contains('active') && anchor.getAttribute('href').startsWith('#')) {
          anchor.classList.remove('active');
          anchor.removeAttribute('aria-current');
        }
      });
    }

    function setText(elementId, value) {
      const element = document.getElementById(elementId);
      if (!element) {
        return;
      }
      element.textContent = value == null ? "" : String(value);
    }

    function setBadge(elementId, value, kind) {
      const element = document.getElementById(elementId);
      if (!element) {
        return;
      }
      element.textContent = value;
      element.classList.remove("ok", "warn", "blocked");
      if (kind === "ok") {
        element.classList.add("ok");
      } else if (kind === "warn") {
        element.classList.add("warn");
      } else if (kind === "blocked") {
        element.classList.add("blocked");
      }
    }

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    function displayValue(value) {
      if (Array.isArray(value)) {
        return value.length ? value.join(", ") : "-";
      }
      if (value && typeof value === "object") {
        return JSON.stringify(value);
      }
      if (value === true) {
        return "true";
      }
      if (value === false) {
        return "false";
      }
      return value == null || value === "" ? "-" : value;
    }

    function renderHealthDetails(elementId, status) {
      const container = document.getElementById(elementId);
      if (!container) {
        return;
      }
      if (!status) {
        container.innerHTML = `<p class="status-mini-value">No health data</p>`;
        return;
      }
      const blockers = Array.isArray(status.blockers) && status.blockers.length
        ? status.blockers.join("\\n")
        : "No blockers";
      const items = [
        ["Status", status.status || "unknown"],
        ["Mode", status.mode || "idle"],
        ["Ready", status.ready ? "true" : "false"],
        ["Blockers", blockers],
        ["Last heartbeat", status.last_heartbeat_at || "-"],
        ["Last event", status.last_event_at || "-"]
      ];
      container.innerHTML = `<div class="health-cards">${items.map(([label, value]) => `
        <div class="health-card">
          <strong>${escapeHtml(label)}</strong>
          <span>${escapeHtml(value)}</span>
        </div>`).join("")}</div>`;
    }

    function renderActionList(status) {
      const container = document.getElementById("dashboard-action-list");
      if (!container) {
        return;
      }
      const blockers = Array.isArray(status && status.blockers) ? status.blockers : [];
      const actions = [];
      if (blockers.some((item) => item.includes("TOSS_CLIENT_ID") || item.includes("TOSS_CLIENT_SECRET"))) {
        actions.push(["Toss API credentials", "Issue the Toss API keys and store them locally."]);
      }
      if (blockers.some((item) => item.includes("account_seq"))) {
        actions.push(["Account sequence", "Add the target account sequence to local config."]);
      }
      if (blockers.some((item) => item.includes("runtime.symbols") || item.includes("universe_candidate_symbols"))) {
        actions.push(["Scan universe", "Add symbols so the bot has something to scan."]);
      }
      if (!actions.length) {
        actions.push(["Ready for paper checks", "No setup blocker is visible. Use Events to confirm the latest paper-service run."]);
      }
      container.innerHTML = actions.map(([title, body]) => `
        <li>
          <strong>${escapeHtml(title)}</strong>
          <p>${escapeHtml(body)}</p>
        </li>`).join("");
    }

    function renderTable(elementId, rows, columns, fallback) {
      const container = document.getElementById(elementId);
      if (!container) {
        return;
      }
      if (!rows || !rows.length) {
        container.innerHTML = `<p class="status-mini-value">${escapeHtml(fallback)}</p>`;
        return;
      }
      const keys = columns || Object.keys(rows[0] || {});
      const header = `<tr>${keys.map((column) => `<th>${escapeHtml(column)}</th>`).join("")}</tr>`;
      const body = rows
        .map((item) => `<tr>${keys.map((column) => `<td>${escapeHtml(displayValue(item[column]))}</td>`).join("")}</tr>`)
        .join("");
      container.innerHTML = `<table><thead>${header}</thead><tbody>${body}</tbody></table>`;
    }

    function renderTimeline(elementId, items) {
      const container = document.getElementById(elementId);
      if (!container) {
        return;
      }
      if (!items || !items.length) {
        container.innerHTML = "<li>No events yet.</li>";
        return;
      }
      container.innerHTML = items.map((entry) => {
        const level = (entry.level || "UNKNOWN");
        const created = entry.created_at || "";
        const blockers = Array.isArray(entry.payload && entry.payload.blockers)
          ? entry.payload.blockers.join(", ")
          : "";
        const suffix = blockers ? ` (${blockers})` : "";
        return `<li class=\"level-${level}\"><strong>${created}</strong> [${level}] ${entry.message || "unknown"}${suffix}</li>`;
      }).join("");
    }

    function renderEndpointList(rawLinks) {
      const endpointList = document.getElementById('endpoint-list');
      if (!endpointList || !rawLinks) {
        return;
      }
      endpointList.innerHTML = Object.entries(rawLinks).map(([name, path]) => {
        return `<button data-endpoint="${path}" type="button">${name}: ${path}</button>`;
      }).join("");
      endpointList.querySelectorAll('button').forEach((button) => {
        button.addEventListener('click', async (event) => {
          const target = event.currentTarget.getAttribute('data-endpoint');
          if (!target) {
            return;
          }
          endpointList.querySelectorAll('button').forEach((element) => element.classList.remove('active'));
          event.currentTarget.classList.add('active');
          document.getElementById('raw-endpoint-label').textContent = target;
          const data = await getJson(target);
          const payload = JSON.stringify(data, null, 2);
          document.getElementById('raw-endpoint-json').textContent = payload;
          document.getElementById('raw-aggregate-json').textContent = payload;
        });
      });
    }

    function setupStepState(index, blockers) {
      const joined = (blockers || []).join(" ");
      if (index === 1 || index === 2) {
        return joined.includes("TOSS_CLIENT_ID") || joined.includes("TOSS_CLIENT_SECRET")
          ? ["Needed", "todo"]
          : ["Done", "done"];
      }
      if (index === 3) {
        return joined.includes("account_seq") ? ["Needed", "todo"] : ["Done", "done"];
      }
      if (index === 4) {
        return joined.includes("runtime.symbols") || joined.includes("universe_candidate_symbols")
          ? ["Needed", "todo"]
          : ["Done", "done"];
      }
      if (index === 5) {
        return blockers && blockers.length ? ["Waiting", "warn"] : ["Ready", "done"];
      }
      return ["Check", "warn"];
    }

    function renderOnboarding(blockers, rawLinks, settings) {
      const list = document.getElementById('settings-onboarding-list');
      if (!list) {
        return;
      }
      const status = blockers && blockers.length ? `${blockers.length} blockers` : "No blockers";
      const badgeType = blockers && blockers.length ? "warn" : "ok";
      const items = ONBOARDING_STEPS.map((step, index) => {
        const [label, kind] = setupStepState(index, blockers || []);
        return `
        <li>
          <strong>${escapeHtml(step.title)}</strong>
          <p>${escapeHtml(step.body)}</p>
          <span class="status-pill ${kind}">${escapeHtml(label)}</span>
        </li>`;
      }).join("");
      list.innerHTML = `${items}<li><strong>Current readiness</strong><p>${escapeHtml(status)}</p><span class="status-pill ${badgeType}">${escapeHtml(status)}</span></li>`;
      const blockersPanel = document.getElementById('settings-blockers-list');
      if (blockersPanel) {
        blockersPanel.textContent = blockers && blockers.length ? blockers.join("\\n") : "No blockers detected.";
      }
      const rawPanel = document.getElementById('settings-raw-links');
      if (rawPanel) {
        rawPanel.textContent = JSON.stringify(rawLinks || {}, null, 2);
      }
      const strategyPanel = document.getElementById('settings-strategy-json');
      if (strategyPanel) {
        strategyPanel.textContent = JSON.stringify(settings || {
          momentum: {
            cash_reserve_pct: "0.50",
            max_exposure_pct: "0.50",
            target_position_pct: "0.10"
          }
        }, null, 2);
      }
      const headline = document.getElementById('settings-headline');
      if (headline) {
        headline.innerHTML = `<strong>New user onboarding</strong> Start with the first Needed step.`;
      }
    }

    async function getJson(path) {
      const response = await fetch(path, { cache: "no-store" });
      if (!response.ok) {
        const body = await response.text();
        throw new Error(`${path} failed: ${response.status} ${body}`);
      }
      return response.json();
    }

    async function refresh() {
      try {
        const [dashboard, health, positions, openOrders, watchlist, events, summary] = await Promise.all([
          getJson("/dashboard"),
          getJson("/health"),
          getJson("/positions"),
          getJson("/orders/open"),
          getJson("/watchlist"),
          getJson("/events?limit=50"),
          getJson("/events/summary?limit=50")
        ]);

        const status = dashboard.status || {};
        setText("metric-mode-text", status.mode || "idle");
        setText("metric-watchlist-text", watchlist.count || 0);
        setText("metric-positions-text", positions.count || 0);
        setText("metric-intents-text", (dashboard.paper_intents || {}).count || 0);
        setBadge("metric-mode-pill", status.ready ? "READY" : "BLOCKED", status.ready ? "ok" : "blocked");
        setText("dashboard-status-strip", status.ready ? "Paper service can run" : "Setup is not complete");
        setText("dashboard-status-copy", status.ready
          ? "No setup blocker is visible."
          : "Fix setup items below. Read-only.");
        renderActionList(status);

        renderTimeline("dashboard-events-timeline", dashboard.runtime_events ? dashboard.runtime_events.items || [] : []);
        renderHealthDetails("dashboard-health-list", dashboard.status);
        renderTable("dashboard-open-orders-table", (dashboard.paper_intents || {}).items || [], null, "No paper intents");
        renderTable("orders-table", openOrders.items || (dashboard.paper_intents || {}).items || [], null, "No open orders");

        setText("dashboard-events-summary", `Total events: ${(dashboard.runtime_summary || {}).total || 0}`);
        renderTable("watchlist-table", watchlist.items || [], null, "No watchlist entries");
        renderTable("positions-table", positions.items || [], null, "No positions");
        renderTimeline("events-timeline", events.items || []);
        renderTable("events-table", events.items || [], ["id", "level", "message", "created_at"], "No events yet");
        setText("events-summary-strip", `Total events: ${summary.total || 0}`);

        document.getElementById("watchlist-json").textContent = JSON.stringify(watchlist, null, 2);
        document.getElementById("positions-json").textContent = JSON.stringify(positions, null, 2);
        document.getElementById("orders-json").textContent = JSON.stringify(openOrders, null, 2);
        document.getElementById("events-json").textContent = JSON.stringify({ summary, events }, null, 2);
        document.getElementById("raw-aggregate-json").textContent = JSON.stringify(dashboard, null, 2);

        if (dashboard.raw_links) {
          renderEndpointList(dashboard.raw_links);
          renderOnboarding(status.blockers || [], dashboard.raw_links, dashboard.settings || {});
        }
      } catch (error) {
        console.error(error);
      }
    }

    function bindNavigation() {
      document.querySelectorAll('a[data-view]').forEach((anchor) => {
        anchor.addEventListener('click', (event) => {
          const href = anchor.getAttribute('href');
          if (!href || !href.startsWith('#')) {
            return;
          }
          const target = href.substring(1);
          event.preventDefault();
          setActiveView(target);
          history.replaceState(null, "", `#${target}`);
        });
      });
    }

    function initialView() {
      const allowed = new Set(["dashboard", "watchlist", "positions", "orders", "events", "raw", "settings"]);
      const current = window.location.hash ? window.location.hash.substring(1) : "dashboard";
      return allowed.has(current) ? current : "dashboard";
    }

    bindNavigation();
    setActiveView(initialView());
    window.addEventListener("hashchange", () => setActiveView(initialView()));
    refresh();
    setInterval(refresh, 6000);
  </script>
</body>
</html>"""


class HealthServer:
    """Read-only health payload producer and optional local HTTP endpoint host."""

    def __init__(
        self,
        snapshot_provider: PayloadProvider | HealthSnapshot,
        *,
        events_provider: EventsProvider | None = None,
        broker_snapshots_provider: BrokerSnapshotsProvider | None = None,
        settings: Mapping[str, Any] | None = None,
        settings_updater: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
        action_runner: ActionRunner | None = None,
        host: str = "127.0.0.1",
        port: int = 8765,
        start_server: bool = False,
    ) -> None:
        self._snapshot_provider = (
            snapshot_provider
            if callable(snapshot_provider)
            else lambda: snapshot_provider
        )
        self._events_provider = events_provider if events_provider is not None else (lambda *_: [])
        self._broker_snapshots_provider = (
            broker_snapshots_provider if broker_snapshots_provider is not None else lambda: {}
        )
        self._settings = dict(settings or {})
        self._settings_updater = settings_updater
        self._action_runner = action_runner
        self.host = host
        self.port = port
        self._server: HTTPServer | None = None
        if start_server:
            self.start()

    def _snapshot(self) -> HealthSnapshot:
        raw = self._snapshot_provider()
        if isinstance(raw, HealthSnapshot):
            return raw
        if isinstance(raw, Mapping):
            return _normalize_payload(raw)
        return HealthSnapshot()

    def _events(self, limit: int | None = None) -> list[Mapping[str, Any]]:
        if limit is None:
            try:
                events = self._events_provider()
            except TypeError:
                events = self._events_provider(None)
        else:
            try:
                events = self._events_provider(limit)
            except TypeError:
                events = self._events_provider()
        if not isinstance(events, list):
            return []
        if limit is not None:
            return events[:limit]
        return events

    def _event_payload_items(self, query: Mapping[str, list[str]] | None = None) -> list[Mapping[str, Any]]:
        limit = None
        if query is not None:
            raw_limit = query.get("limit")
            if raw_limit:
                try:
                    limit = int(str(raw_limit[0]))
                except ValueError:
                    limit = None
        events = self._events(limit)
        return _coerce_events_payload(events)

    def _events_summary(self, query: Mapping[str, list[str]] | None = None) -> dict[str, Any]:
        events = self._events()
        if query is not None:
            raw_date = query.get("date")
            if raw_date:
                try:
                    target = date_cls.fromisoformat(str(raw_date[0]))
                    events = _events_for_day(events, target)
                except ValueError:
                    pass
        return _summarize_events(events)

    def _broker_snapshots(self) -> Mapping[str, Any]:
        try:
            snapshots = self._broker_snapshots_provider()
        except Exception:
            return {}
        return snapshots if isinstance(snapshots, Mapping) else {}

    def _dashboard_payload(self) -> dict[str, Any]:
        snapshot = self._snapshot()
        events = self._events()
        broker_snapshots = self._broker_snapshots()
        first = _first_day_event(events)
        raw_status = snapshot.status_payload()
        status = _friendly_status_payload(raw_status)
        if first is not None:
            status["last_event_at"] = _iso_datetime(first.get("created_at"))
        runtime_summary = _summarize_events(events)

        return {
            "generated_at": snapshot.generated_at.isoformat(),
            "status": status,
            "watchlist": {
                **snapshot.watchlist_payload(),
                "generated_at": None,
            },
            "positions": snapshot.positions_payload(),
            "paper_intents": snapshot.open_orders_payload(),
            "runtime_events": {
                "count": len(events),
                "items": _coerce_events_payload(events),
            },
            "runtime_summary": runtime_summary,
            "broker_snapshots": dict(broker_snapshots),
            "live_monitor": _build_live_monitor_payload(
                events=events,
                snapshot=snapshot,
                broker_snapshots=broker_snapshots,
            ),
            "settings": self._settings,
            "settings_write_enabled": self._settings_updater is not None,
            "live_readiness": _build_live_readiness_payload(
                status=raw_status,
                settings=self._settings,
                snapshot=snapshot,
                events_summary=runtime_summary,
            ),
        }

    def payload_for_path(
        self,
        path: str,
        query: Mapping[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        normalized = path.lower()
        snapshot = self._snapshot()

        if normalized in {"/health", "/status"}:
            return snapshot.as_payload()
        if normalized == "/positions":
            return snapshot.positions_payload()
        if normalized == "/orders/open":
            return snapshot.open_orders_payload()
        if normalized == "/watchlist":
            return snapshot.watchlist_payload()
        if normalized == "/events":
            return {
                "count": len(self._event_payload_items(query)),
                "items": self._event_payload_items(query),
            }
        if normalized == "/events/summary":
            return self._events_summary(query)
        if normalized == "/dashboard":
            return self._dashboard_payload()
        if normalized == "/dashboard/network/public-ip":
            return public_ip_payload()
        raise ValueError(f"unsupported read-only path: {path}")

    def action_for_path(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        normalized = path.lower()
        if normalized not in {
            "/dashboard/actions/live-once",
            "/dashboard/actions/live-smoke-test",
            "/dashboard/actions/apply-safe-pilot",
            "/dashboard/actions/stop-trading",
            "/dashboard/actions/test-discord-alert",
        }:
            raise ValueError(f"unsupported action path: {path}")
        if self._action_runner is None:
            raise RuntimeError("dashboard actions require --config")
        return dict(self._action_runner(normalized, payload))

    def start(self) -> None:
        if self._server is not None:
            return

        server_ref = self

        class _Handler(BaseHTTPRequestHandler):
            @staticmethod
            def _send_json(handler: "_Handler", status: int, payload: Mapping[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                handler.send_response(status)
                handler.send_header("Content-Type", "application/json")
                handler.send_header("Content-Length", str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)

            def do_GET(self):
                parsed = urlparse(self.path)
                path = parsed.path or "/"
                query = parse_qs(parsed.query)

                if path in {"/", "/dashboard.html"}:
                    body = dashboard_html().encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if path == "/assets/toss-symbol.png":
                    try:
                        body = TOSS_LOGO_ASSET.read_bytes()
                    except OSError:
                        self.send_response(404)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"error": "not found"}).encode("utf-8"))
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Cache-Control", "public, max-age=3600")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if path in {
                    "/health",
                    "/status",
                    "/positions",
                    "/orders/open",
                    "/watchlist",
                    "/events",
                    "/events/summary",
                    "/dashboard",
                    "/dashboard/network/public-ip",
                }:
                    payload = server_ref.payload_for_path(path, query)
                    body = json.dumps(payload).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                body = json.dumps({"error": "not found"}).encode("utf-8")
                self.wfile.write(body)

            def do_POST(self):
                parsed = urlparse(self.path)
                path = parsed.path or "/"
                if path.startswith("/dashboard/actions/"):
                    if server_ref._action_runner is None:
                        self._send_json(self, 409, {"error": "dashboard actions require --config"})
                        return
                    try:
                        length = int(self.headers.get("Content-Length", "0"))
                        if length <= 0:
                            raise ValueError("request body missing")
                        payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                        self._send_json(self, 400, {"error": "invalid json", "message": str(exc)})
                        return
                    try:
                        result = server_ref.action_for_path(path, payload)
                    except ValueError as exc:
                        self._send_json(self, 400, {"error": str(exc)})
                        return
                    except RuntimeError as exc:
                        self._send_json(self, 409, {"error": str(exc)})
                        return
                    except Exception as exc:
                        self._send_json(self, 500, {"error": "action failed", "message": str(exc)})
                        return
                    self._send_json(self, 200, result)
                    return
                if path != "/dashboard/settings":
                    self.send_response(405)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    body = json.dumps({"error": "method not allowed"}).encode("utf-8")
                    self.wfile.write(body)
                    return
                if server_ref._settings_updater is None:
                    self._send_json(self, 409, {"error": "settings updates require --config"})
                    return

                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0:
                        raise ValueError("request body missing")
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    self._send_json(self, 400, {"error": "invalid json", "message": str(exc)})
                    return

                try:
                    result = server_ref._settings_updater(payload)
                except ValueError as exc:
                    self._send_json(self, 400, {"error": str(exc)})
                    return
                except Exception as exc:
                    self._send_json(self, 500, {"error": "failed to save settings", "message": str(exc)})
                    return

                next_settings = result.get("settings") if isinstance(result, Mapping) else None
                if isinstance(next_settings, Mapping):
                    server_ref._settings = dict(next_settings)
                else:
                    config_result = result.get("config", result) if isinstance(result, Mapping) else {}
                    momentum = config_result.get("strategy", {}) if isinstance(config_result, Mapping) else {}
                    momentum_data = momentum.get("momentum", {}) if isinstance(momentum, Mapping) else {}
                    if isinstance(momentum_data, Mapping):
                        server_ref._settings["momentum"] = {
                            "cash_reserve_pct": str(momentum_data.get("cash_reserve_pct", "")),
                            "max_exposure_pct": str(momentum_data.get("max_exposure_pct", "")),
                            "target_position_pct": str(momentum_data.get("target_position_pct", "")),
                            "max_positions": momentum_data.get("max_positions", ""),
                            "accept_top_n": momentum_data.get("accept_top_n", ""),
                            "exit_ma_days": momentum_data.get("exit_ma_days", ""),
                            "lookback_days": momentum_data.get("lookback_days", ""),
                            "skip_days": momentum_data.get("skip_days", ""),
                            "trend_ma_days": momentum_data.get("trend_ma_days", ""),
                        }
                self._send_json(self, 200, {"status": "saved", "settings": server_ref._settings})

            def log_message(
                self,
                format: str,
                *args: object,
            ) -> None:  # pragma: no cover
                return

        self._server = HTTPServer((self.host, self.port), _Handler)
        thread = Thread(target=self._server.serve_forever, daemon=True)
        thread.start()

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
