from __future__ import annotations

from datetime import datetime, timedelta, timezone

from turtle_bot.rate_limit import RateLimitPriority, RateLimitQueue


def test_priority_queue_dequeues_reconciliation_before_screening() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    queue = RateLimitQueue(now=lambda: now)

    queue.enqueue("watchlist", request_id="watch", priority=RateLimitPriority.BROAD_SCREENING)
    queue.enqueue("account", request_id="acct", priority=RateLimitPriority.ACCOUNT)
    queue.enqueue("recon", request_id="critical", priority=RateLimitPriority.RECONCILIATION)

    assert queue.dequeue_ready().request_id == "critical"
    assert queue.dequeue_ready().request_id == "acct"
    assert queue.dequeue_ready().request_id == "watch"


def test_update_headers_sets_retry_pause_state() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    queue = RateLimitQueue(now=lambda: now)

    queue.update_from_headers(
        "candles",
        {"Retry-After": "45"},
        now=now,
    )

    assert queue.is_group_paused("candles", now=now) is True
    assert queue.group_pause_until("candles") == now + timedelta(seconds=45)


def test_remaining_zero_with_reset_sets_group_pause_until() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    queue = RateLimitQueue(now=lambda: now)
    reset = (now + timedelta(minutes=2)).timestamp()

    queue.update_from_headers(
        "orders",
        {
            "X-RateLimit-Limit": "10",
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(int(reset)),
        },
        now=now,
    )

    snapshot = queue.get_group_snapshot("orders")
    assert snapshot.remaining == 0
    assert snapshot.limit == 10
    assert snapshot.reset_at == datetime.fromtimestamp(reset, tz=timezone.utc)
    assert snapshot.paused_until == datetime.fromtimestamp(reset, tz=timezone.utc)


def test_reset_header_accepts_seconds_until_reset() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    queue = RateLimitQueue(now=lambda: now)

    queue.update_from_headers(
        "orders",
        {
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": "2",
        },
        now=now,
    )

    snapshot = queue.get_group_snapshot("orders")
    assert snapshot.reset_at == now + timedelta(seconds=2)
    assert snapshot.paused_until == now + timedelta(seconds=2)


def test_paused_group_requests_wait_until_resume() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    queue = RateLimitQueue(now=lambda: now)
    queue.update_from_headers("orders", {"Retry-After": "60"}, now=now)

    queue.enqueue("orders", request_id="high")
    queue.enqueue("watchlist", request_id="low", priority=RateLimitPriority.BROAD_SCREENING)

    ready_request = queue.dequeue_ready()
    assert ready_request is not None
    assert ready_request.request_id == "low"
