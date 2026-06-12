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
    assert dashboard["raw_links"]["events"] == "/events"


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
    assert "페이퍼 서비스 점검 완료" in html
    assert "Toss API 인증 정보가 아직 없습니다." in html
    assert 'id="sidebar-ready"' in html
    assert "left: max(8px, env(safe-area-inset-left))" in html
    assert 'id="refresh-button"' in html
    assert "addEventListener(\"click\", () => refresh().catch(console.error))" in html
    assert 'onclick="refresh()"' not in html
    assert "width: auto;" in html
    assert "repeat(5, minmax(0, 1fr))" in html
    assert "renderOnboarding(status.blockers || [], dashboard.raw_links || {}, eventRows)" in html
