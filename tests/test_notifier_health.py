from __future__ import annotations

from datetime import datetime, timezone

from turtle_bot.health import HealthServer, HealthSnapshot, TOSS_LOGO_ASSET, dashboard_html
from turtle_bot.notifier import MemoryNotifier


def test_memory_notifier_collects_output_only_messages() -> None:
    notifier = MemoryNotifier()
    notifier.notify("startup", level="info", payload={"step": "start"})
    notifier.notify("watchlist", level="warn", payload={"count": 1})

    items = notifier.snapshot()
    assert len(items) == 2
    assert items[0].message == "startup"
    assert items[0].level == "info"


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
        settings={
            "momentum": {
                "cash_reserve_pct": "0.50",
                "max_exposure_pct": "0.50",
            }
        },
    )

    events_payload = server.payload_for_path("/events", {"limit": ["1"]})
    assert events_payload["count"] == 1
    assert events_payload["items"][0]["created_at"] == "2026-06-12T01:00:00+00:00"

    summary_payload = server.payload_for_path("/events/summary")
    assert summary_payload["total"] == 2
    assert summary_payload["blockers"] == ["price_stale"]
    assert summary_payload["paper_runtime_blocks"] == 1

    dashboard = server.payload_for_path("/dashboard")
    assert dashboard["status"]["status"] == "blocked"
    assert dashboard["watchlist"]["count"] == 1
    assert dashboard["positions"]["count"] == 1
    assert dashboard["paper_intents"]["count"] == 1
    assert dashboard["runtime_events"]["count"] == 2
    assert dashboard["settings"]["momentum"]["cash_reserve_pct"] == "0.50"
    assert dashboard["live_readiness"]["can_submit_live_orders"] is False
    assert dashboard["live_readiness"]["summary"]["blocked"] >= 1
    assert any(
        check["id"] == "live_order_engine"
        for check in dashboard["live_readiness"]["checks"]
    )
    assert "raw_links" not in dashboard


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
    assert "페이퍼 서비스 점검 완료" in html
    assert "Toss API 인증 정보가 아직 없습니다." in html
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
    assert "/dashboard/actions/apply-safe-pilot" in html
    assert "/dashboard/actions/stop-trading" in html
    assert "LIVE PILOT 실행" in html
    assert "function runLiveOnce" in html
    assert "function applySafePilot" in html
    assert "function stopTrading" in html
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
    assert 'id="toss-client-id-status"' in html
    assert 'id="toss-identity-confirmation"' in html
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
    assert "현재 운영이 차단 상태입니다" in html
    assert "차단 설정 확인" in html
    assert 'id="dashboard-clock" class="clock-text">현재 --:--:--</span>' in html
    assert "function updateDashboardClock()" in html
    assert "setInterval(updateDashboardClock, 1000)" in html
    assert "운영이 차단되어 있으면 이벤트가 적을 수 있습니다." in html
    assert "갱신 ${shortTimestamp" not in html
    assert 'class="ghost-line" style="width:100px"' not in html
    assert "grid-template-columns: minmax(68px, 84px) minmax(0, 1fr) minmax(72px, 94px)" in html
    assert ".event-line > *" in html
    assert "overflow-wrap: anywhere;" in html
