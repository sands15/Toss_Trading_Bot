from __future__ import annotations

from datetime import datetime, timezone

import turtle_bot.health as health_module
from turtle_bot.health import HealthServer, HealthSnapshot, TOSS_LOGO_ASSET, dashboard_html, public_ip_payload
from turtle_bot.notifier import DiscordTradeNotifier, MemoryNotifier


def test_memory_notifier_collects_output_only_messages() -> None:
    notifier = MemoryNotifier()
    notifier.notify("startup", level="info", payload={"step": "start"})
    notifier.notify("watchlist", level="warn", payload={"count": 1})

    items = notifier.snapshot()
    assert len(items) == 2
    assert items[0].message == "startup"
    assert items[0].level == "info"


def test_discord_trade_notifier_sends_compact_trade_alert() -> None:
    sent: list[tuple[str, bytes, float]] = []
    notifier = DiscordTradeNotifier(
        env={"DISCORD_TRADE_ALERT_WEBHOOK_URL": "https://discord.test/webhook"},
        sender=lambda url, body, timeout: sent.append((url, body, timeout)),
    )

    sent_ok = notifier.notify(
        "live_order_execution",
        level="info",
        payload={
            "account_alias": "정훈 미국주식 계좌",
            "symbol": "SPCX",
            "side": "BUY",
            "quantity": "1",
            "status": "ACKNOWLEDGED",
        },
    )

    assert sent_ok is True
    assert sent
    assert sent[0][0] == "https://discord.test/webhook"
    assert b"SPCX" in sent[0][1]
    assert "정훈 미국주식 계좌".encode("utf-8") in sent[0][1]
    assert "매수".encode("utf-8") in sent[0][1]
    assert "1주".encode("utf-8") in sent[0][1]
    assert "주문 접수".encode("utf-8") in sent[0][1]
    assert b"ACKNOWLEDGED" not in sent[0][1]


def test_discord_trade_notifier_sends_cancel_after_ack_alert() -> None:
    sent: list[tuple[str, bytes, float]] = []
    notifier = DiscordTradeNotifier(
        env={"DISCORD_TRADE_ALERT_WEBHOOK_URL": "https://discord.test/webhook"},
        sender=lambda url, body, timeout: sent.append((url, body, timeout)),
    )

    notifier.notify(
        "live_order_cancel_after_ack",
        level="info",
        payload={
            "account_alias": "정훈 미국주식 계좌",
            "symbol": "XLK",
            "quantity": "1",
            "status": "PENDING_CANCEL",
        },
    )

    assert sent
    assert b"XLK" in sent[0][1]
    assert "정훈 미국주식 계좌".encode("utf-8") in sent[0][1]
    assert "취소 요청".encode("utf-8") in sent[0][1]


def test_discord_trade_notifier_sends_dashboard_test_alert() -> None:
    sent: list[tuple[str, bytes, float]] = []
    notifier = DiscordTradeNotifier(
        env={"DISCORD_TRADE_ALERT_WEBHOOK_URL": "https://discord.test/webhook"},
        sender=lambda url, body, timeout: sent.append((url, body, timeout)),
    )

    sent_ok = notifier.notify(
        "discord_alert_test",
        level="info",
        payload={"account_alias": "정훈 미국주식 계좌"},
    )

    assert sent_ok is True
    assert sent
    assert "거래 알림 테스트".encode("utf-8") in sent[0][1]
    assert "정훈 미국주식 계좌".encode("utf-8") in sent[0][1]


def test_discord_trade_notifier_keeps_safe_failure_reason() -> None:
    def fail_send(url: str, body: bytes, timeout: float) -> None:
        raise RuntimeError("network unreachable")

    notifier = DiscordTradeNotifier(
        env={"DISCORD_TRADE_ALERT_WEBHOOK_URL": "https://discord.test/webhook"},
        sender=fail_send,
    )

    sent_ok = notifier.notify(
        "discord_alert_test",
        level="info",
        payload={"account_alias": "정훈 미국주식 계좌"},
    )

    assert sent_ok is False
    assert notifier.last_error == "network unreachable"


def test_discord_trade_notifier_ignores_non_trade_noise() -> None:
    sent: list[tuple[str, bytes, float]] = []
    notifier = DiscordTradeNotifier(
        env={"DISCORD_TRADE_ALERT_WEBHOOK_URL": "https://discord.test/webhook"},
        sender=lambda url, body, timeout: sent.append((url, body, timeout)),
    )

    sent_ok = notifier.notify("settings_saved", level="info", payload={"path": "config/local.yaml"})

    assert sent_ok is False
    assert sent == []


def test_health_payload_is_read_only_without_binding() -> None:
    snapshot = HealthSnapshot(
        mode="paper",
        ready=False,
        blockers=("price_stale",),
        positions=(),
        open_orders=(),
        watchlist=(),
    )
    server = HealthServer(snapshot)

    health_payload = server.payload_for_path("/health")
    assert health_payload["status"] == "blocked"
    assert health_payload["mode"] == "paper"
    assert health_payload["ready"] is False
    assert health_payload["blockers"] == ["price_stale"]

    status_payload = server.payload_for_path("/positions")
    assert "positions" in status_payload
    assert status_payload["count"] == 0

    orders_payload = server.payload_for_path("/orders/open")
    assert orders_payload["count"] == 0

    watchlist_payload = server.payload_for_path("/watchlist")
    assert watchlist_payload["count"] == 0

    try:
        server.payload_for_path("/close")
    except ValueError as exc:
        assert "unsupported" in str(exc)
    else:
        raise AssertionError("mutation endpoint unexpectedly supported")


def test_dashboard_and_events_payloads_are_read_only_aggregates() -> None:
    snapshot = HealthSnapshot(
        mode="paper",
        ready=False,
        blockers=("price_stale",),
        positions=({"symbol": "AAA", "status": "OPEN"},),
        open_orders=({"intent_id": "paper-1", "symbol": "AAA"},),
        watchlist=({"symbol": "AAA", "nearest_distance": "1"},),
    )
    events = [
        {
            "id": 5,
            "level": "INFO",
            "message": "live_order_status_synced",
            "payload": {"checked": 1, "updated": 1, "failed": 0, "remaining_unresolved": 0},
            "created_at": datetime(2026, 6, 12, 1, 3, tzinfo=timezone.utc),
        },
        {
            "id": 4,
            "level": "INFO",
            "message": "broker_order_history_synced",
            "payload": {"closed_orders_count": 1},
            "created_at": datetime(2026, 6, 12, 1, 2, tzinfo=timezone.utc),
        },
        {
            "id": 3,
            "level": "INFO",
            "message": "broker_account_synced",
            "payload": {
                "holdings_count": 1,
                "open_orders_count": 0,
                "synced_live_positions": True,
            },
            "created_at": datetime(2026, 6, 12, 1, 1, tzinfo=timezone.utc),
        },
        {
            "id": 2,
            "level": "WARN",
            "message": "paper_service_blocked",
            "payload": {"blockers": ["price_stale"]},
            "created_at": datetime(2026, 6, 12, 1, 0, tzinfo=timezone.utc),
        },
        {
            "id": 1,
            "level": "INFO",
            "message": "paper_service_started",
            "payload": {"mode": "paper"},
            "created_at": datetime(2026, 6, 12, 0, 0, tzinfo=timezone.utc),
        },
    ]
    server = HealthServer(
        snapshot,
        events_provider=lambda limit: events[:limit] if limit is not None else events,
        broker_snapshots_provider=lambda: {
            "closed_orders": {
                "captured_at": "2026-06-12T01:02:00+00:00",
                "payload": {
                    "orders": [
                        {
                            "orderId": "closed-1",
                            "symbol": "AAA",
                            "side": "BUY",
                            "status": "FILLED",
                            "quantity": "1",
                            "execution": {
                                "filledQuantity": "1",
                                "averageFilledPrice": "100",
                            },
                            "orderedAt": "2026-06-12T10:00:00+09:00",
                        }
                    ]
                },
            }
        },
        settings={
            "momentum": {
                "cash_reserve_pct": "0.50",
                "max_exposure_pct": "0.50",
            }
        },
    )

    events_payload = server.payload_for_path("/events", {"limit": ["1"]})
    assert events_payload["count"] == 1
    assert events_payload["items"][0]["created_at"] == "2026-06-12T01:03:00+00:00"

    summary_payload = server.payload_for_path("/events/summary")
    assert summary_payload["total"] == 5
    assert summary_payload["blockers"] == ["시세가 최신인지 확인이 필요합니다."]
    assert summary_payload["paper_runtime_blocks"] == 1

    dashboard = server.payload_for_path("/dashboard")
    assert dashboard["status"]["status"] == "blocked"
    assert dashboard["watchlist"]["count"] == 1
    assert dashboard["positions"]["count"] == 1
    assert dashboard["paper_intents"]["count"] == 1
    assert dashboard["runtime_events"]["count"] == 5
    assert dashboard["settings"]["momentum"]["cash_reserve_pct"] == "0.50"
    assert dashboard["broker_snapshots"]["closed_orders"]["captured_at"] == "2026-06-12T01:02:00+00:00"
    assert dashboard["live_monitor"]["order_history"]["source"] == "orders_closed"
    assert dashboard["live_monitor"]["order_history"]["closed_orders_count"] == 1
    assert dashboard["live_monitor"]["order_status_tracking"]["remaining_unresolved"] == 0
    assert dashboard["live_readiness"]["can_submit_live_orders"] is False
    assert dashboard["live_readiness"]["summary"]["blocked"] >= 1
    assert any(
        check["id"] == "live_order_engine"
        for check in dashboard["live_readiness"]["checks"]
    )
    assert "raw_links" not in dashboard


def test_events_payload_rewrites_toss_ip_allowlist_error() -> None:
    snapshot = HealthSnapshot(mode="live", ready=False)
    events = [
        {
            "id": 1,
            "level": "ERROR",
            "message": "live_trading_loop_failed",
            "payload": {"source": "dashboard", "error": "IP address not allowed"},
            "created_at": datetime(2026, 6, 22, 18, 14, tzinfo=timezone.utc),
        }
    ]
    server = HealthServer(snapshot, events_provider=lambda limit: events[:limit] if limit else events)

    events_payload = server.payload_for_path("/events")
    error = events_payload["items"][0]["payload"]["error"]
    assert "Toss가 현재 맥북/컨테이너 공개 IP를 거절했습니다" in error
    assert "Toss 개발자센터 앱 허용 IP" in error
    assert "IP address not allowed" not in error

    dashboard = server.payload_for_path("/dashboard")
    dashboard_error = dashboard["runtime_events"]["items"][0]["payload"]["error"]
    assert dashboard_error == error


def test_dashboard_live_readiness_can_report_submit_ready_state() -> None:
    snapshot = HealthSnapshot(
        mode="live",
        ready=True,
        blockers=(),
        positions=(),
        open_orders=({"intent_id": "live-1", "symbol": "AAA"},),
        watchlist=({"symbol": "AAA", "nearest_distance": "1"},),
    )
    events = [
        {
            "id": 1,
            "level": "INFO",
            "message": "live_order_execution",
            "payload": {"status": "ACKNOWLEDGED"},
            "created_at": datetime(2026, 6, 12, 1, 0, tzinfo=timezone.utc),
        },
    ]
    server = HealthServer(
        snapshot,
        events_provider=lambda limit: events[:limit] if limit is not None else events,
        settings={
            "strategy_kind": "turtle",
            "runtime": {"mode": "live"},
            "toss": {
                "live_enabled": True,
                "account_seq_configured": True,
            },
        },
    )

    dashboard = server.payload_for_path("/dashboard")

    readiness = dashboard["live_readiness"]
    assert readiness["state"] == "ready_for_live"
    assert readiness["can_submit_live_orders"] is True
    assert readiness["submit_disabled_reason"] is None
    assert readiness["summary"]["blocked"] == 0
    assert dashboard["runtime_summary"]["live_order_executions"] == 1


def test_dashboard_html_is_responsive_and_uses_read_only_endpoints() -> None:
    html = dashboard_html()

    assert '<meta name="viewport"' in html
    assert "@media (max-width: 820px)" in html
    assert 'getJson("/dashboard")' in html
    assert 'id="dashboard-health-list"' in html
    assert 'getJson("/health")' in html
    assert 'getJson("/positions")' in html
    assert 'getJson("/orders/open")' in html
    assert 'getJson("/watchlist")' in html
    assert 'getJson("/events/summary?limit=50")' in html
    assert "never submits" in html
    assert "EVENT_LABELS" in html
    assert "COLUMN_LABELS" in html
    assert '<img class="logo" src="/assets/toss-symbol.png" alt="Toss logo"' in html
    assert TOSS_LOGO_ASSET.exists()
    assert "Read-only" not in html
    assert "read-only" not in html
    assert "실주문 비활성" in html
    assert "자동 점검 완료" in html
    assert "TOSS_CLIENT_ID" not in html
    assert "TOSS_CLIENT_SECRET" not in html
    assert "원본 데이터" not in html
    assert 'data-view="raw"' not in html
    assert 'id="view-raw"' not in html
    assert "raw_links" not in html
    assert "empty-state" in html
    assert "renderEventCards" in html
    assert "blockerShortLabel" in html
    assert 'id="sidebar-ready"' in html
    assert "left: max(8px, env(safe-area-inset-left))" in html
    assert 'id="refresh-button"' in html
    assert "addEventListener(\"click\", () => refresh().catch(console.error))" in html
    assert 'onclick="refresh()"' not in html
    assert "window.location.hash = view" in html
    assert "history.replaceState" not in html
    assert "width: auto;" in html
    assert "repeat(6, minmax(0, 1fr))" in html
    assert "dashboard.settings_write_enabled" in html
    assert 'id="view-live"' in html
    assert 'id="live-readiness-checks"' in html
    assert "function renderLiveReadiness" in html
    assert "/dashboard/actions/live-once" in html
    assert "/dashboard/actions/live-smoke-test" in html
    assert "/dashboard/actions/apply-safe-pilot" in html
    assert "/dashboard/actions/stop-trading" in html
    assert "/dashboard/network/public-ip" in html
    assert 'id="live-once-confirmation-token"' in html
    assert 'id="live-smoke-test-button"' in html
    assert 'id="live-public-ip-check-button"' in html
    assert 'id="settings-public-ip-check-button"' in html
    assert "현재 공개 IP 확인" in html
    assert "Toss 개발자센터 앱 허용 IP" in html
    assert "Toss가 현재 맥북/컨테이너 공개 IP를 거절했습니다" in html
    assert "원문:" not in html
    assert "LIVE PILOT 실행" in html
    assert "실주문 테스트" in html
    assert 'placeholder="위 문구를 그대로 입력"' in html
    assert "function runLiveOnce" in html
    assert "function runLiveSmokeTest" in html
    assert "function applySafePilot" in html
    assert "function stopTrading" in html
    assert "function safePilotPrerequisites" in html
    assert "function setSafePilotControls" in html
    assert "function renderPilotSummary" in html
    assert "function renderSetupFlow" in html
    assert 'data-pilot-summary' in html
    assert 'data-pilot-field="symbol"' in html
    assert "거래 대상" in html
    assert "최대 수량" in html
    assert "하루 주문" in html
    assert "하루 금액" in html
    assert "실패 퓨즈" in html
    assert 'data-pilot-field="failure-fuse"' in html
    assert 'id="setup-flow"' in html
    assert 'data-setup-step="api"' in html
    assert 'data-setup-status' in html
    assert "키/계좌를 바꿨다면" in html
    assert "자동 실행 준비됨" in html
    assert "알림 설정" in html
    assert 'id="notification-enable-button"' in html
    assert 'id="notification-test-button"' in html
    assert 'id="discord-notification-test-button"' in html
    assert 'id="notification-toast-stack"' in html
    assert "function notifyImportantEvents" in html
    assert "function sendDiscordTestNotification" in html
    assert "function enableBrowserNotifications" in html
    assert "updateNotificationStatus(dashboard.settings || {})" in html
    assert "TRADE_NOTIFICATION_EVENTS" in html
    assert "FAILURE_NOTIFICATION_EVENTS" in html
    assert "function compactTradeNotification" in html
    assert "function tradeStatusLabel" in html
    assert "주문 접수" in html
    assert "매수/매도 수량과 실패" in html
    assert 'id="safe-pilot-next-action"' in html
    assert "next-action-copy" in html
    assert 'id="safe-pilot-state-badge"' in html
    assert 'id="onboarding-safe-pilot-button"' in html
    assert 'id="onboarding-live-stop-button"' in html
    assert 'id="safe-pilot-button"' in html
    assert 'id="live-stop-button"' in html
    assert 'id="settings-live-stop-button"' in html
    assert "dashboard.live_readiness" in html
    assert "can_submit_live_orders" in html
    assert "cash_reserve_pct" in html
    assert 'id="toss-client-id"' in html
    assert 'id="toss-client-secret"' in html
    assert 'id="toss-account-seq"' in html
    assert 'id="toss-account-alias"' in html
    assert 'id="toss-settings-save-button"' in html
    assert "계좌 별명" in html
    assert 'id="toss-client-id-status"' in html
    assert 'id="toss-identity-confirmation"' in html
    assert 'id="toss-identity-confirmation-token"' in html
    assert "토스 연결 승인" in html
    assert 'id="toss-client-id-env"' not in html
    assert 'id="toss-client-secret-env"' not in html
    assert "환경변수 이름" not in html
    assert 'class="settings-detail"' in html
    assert "<summary>자세히 보기</summary>" in html
    assert "function setTossSettings" in html
    assert ".bottom-nav a.active svg" in html
    assert "현금 보유 비중" in html
    assert "설정 저장" in html
    assert 'type="range"' in html
    assert "settings-strategy-json" not in html
    assert "settings-raw-links" not in html
    assert 'id="dashboard-operator-brief"' in html
    assert "function renderOperatorBrief" in html
    assert "function primaryAction" in html
    assert "function eventDetail" in html
    assert "levelLabel" in html
    assert "TABLE_COLUMNS" in html
    assert "감시 종목 후보를 먼저 넣으세요" in html
    assert "Toss API 인증 정보를 설정하세요" in html
    assert "아직 시작할 준비가 안 됐습니다" in html
    assert "필수 설정 확인" in html
    assert 'id="dashboard-clock" class="clock-text">현재 --:--:--</span>' in html
    assert "function updateDashboardClock()" in html
    assert "setInterval(updateDashboardClock, 1000)" in html
    assert "아직 기록이 없습니다. 설정을 저장한 뒤 봇 점검 기록을 기다려 주세요." in html
    assert "갱신 ${shortTimestamp" not in html
    assert 'class="ghost-line" style="width:100px"' not in html
    assert "grid-template-columns: minmax(68px, 84px) minmax(0, 1fr) minmax(72px, 94px)" in html
    assert ".event-line > *" in html
    assert "overflow-wrap: anywhere;" in html


def test_public_ip_payload_reads_json_service(monkeypatch) -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return b'{"ip":"203.0.113.7"}'

    monkeypatch.setattr(health_module, "urlopen", lambda url, timeout: Response())

    payload = public_ip_payload()

    assert payload["status"] == "ok"
    assert payload["public_ip"] == "203.0.113.7"
    assert "allowlist" in payload["message"]


def test_health_server_exposes_public_ip_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(
        health_module,
        "public_ip_payload",
        lambda: {"status": "ok", "public_ip": "203.0.113.8"},
    )
    server = HealthServer(lambda: HealthSnapshot())

    assert server.payload_for_path("/dashboard/network/public-ip") == {
        "status": "ok",
        "public_ip": "203.0.113.8",
    }
