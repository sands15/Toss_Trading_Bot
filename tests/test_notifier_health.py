from __future__ import annotations

from turtle_bot.health import HealthServer, HealthSnapshot
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
