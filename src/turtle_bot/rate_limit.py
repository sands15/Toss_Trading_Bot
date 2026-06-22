from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone, timedelta
from enum import IntEnum
from typing import Callable, Mapping
from uuid import uuid4


class RateLimitPriority(IntEnum):
    """Higher-priority request classes come first (lower numeric value)."""

    RECONCILIATION = 0
    ORDER = 1
    ACCOUNT = 2
    BROAD_SCREENING = 3
    DEFAULT = 4


def _coerce_datetime(value: str | float | int | None) -> datetime | None:
    if value is None:
        return None

    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def _coerce_reset_at(value: str | float | int | None, now: datetime) -> datetime | None:
    parsed = _coerce_float(value)
    if parsed is None:
        return None

    # Some APIs send an epoch timestamp, while others send seconds-until-reset.
    # Treat small values as a relative duration so rate-limit pauses do not land
    # near 1970-01-01.
    if parsed < 10_000_000:
        return now + timedelta(seconds=max(0.0, parsed))
    return datetime.fromtimestamp(parsed, tz=timezone.utc)


def _coerce_float(value: str | int | float | None) -> float | None:
    if value is None:
        return None

    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _coerce_int(value: str | int | float | None) -> int | None:
    parsed = _coerce_float(value)
    if parsed is None:
        return None
    return int(parsed)


@dataclass(frozen=True)
class _GroupState:
    rate_limit: int | None = None
    remaining: int | None = None
    reset_at: datetime | None = None
    paused_until: datetime | None = None


@dataclass(frozen=True)
class QueuedRequest:
    request_id: str
    group: str
    priority: RateLimitPriority
    seq: int


@dataclass(frozen=True)
class RateLimitHeaderSnapshot:
    group: str
    limit: int | None
    remaining: int | None
    reset_at: datetime | None
    paused_until: datetime | None


class RateLimitQueue:
    """Simple deterministic scheduler with request priority and rate-limit pause support."""

    def __init__(self, *, now: Callable[[], datetime] | None = None):
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._groups: dict[str, _GroupState] = {}
        self._queue: list[QueuedRequest] = []
        self._seq = 0

    def _ensure_group(self, group: str) -> _GroupState:
        return self._groups.setdefault(group, _GroupState())

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    @staticmethod
    def _normalize_priority(
        priority: RateLimitPriority | int | str | None,
    ) -> RateLimitPriority:
        if isinstance(priority, RateLimitPriority):
            return priority
        if isinstance(priority, int):
            return RateLimitPriority(priority)
        if isinstance(priority, str):
            key = priority.strip().upper()
            if key in ("RECONCILIATION", "RECONCILE", "CRITICAL"):
                return RateLimitPriority.RECONCILIATION
            if key in ("ACCOUNT", "ORDER"):
                return RateLimitPriority.ACCOUNT
            if key in ("SCREENING", "BROAD_SCREENING"):
                return RateLimitPriority.BROAD_SCREENING
        return RateLimitPriority.DEFAULT

    def is_group_paused(self, group: str, now: datetime | None = None) -> bool:
        state = self._ensure_group(group)
        now = now or self._now()
        if state.paused_until is not None and state.paused_until > now:
            return True
        if (
            state.remaining is not None
            and state.remaining <= 0
            and state.reset_at is not None
            and state.reset_at > now
        ):
            return True
        return False

    def group_pause_until(self, group: str) -> datetime | None:
        return self._ensure_group(group).paused_until

    def get_group_snapshot(self, group: str) -> RateLimitHeaderSnapshot:
        state = self._ensure_group(group)
        return RateLimitHeaderSnapshot(
            group=group,
            limit=state.rate_limit,
            remaining=state.remaining,
            reset_at=state.reset_at,
            paused_until=state.paused_until,
        )

    def seconds_until_resumed(self, group: str, now: datetime | None = None) -> float:
        now = now or self._now()
        pause_until = self.group_pause_until(group)
        if pause_until is None or pause_until <= now:
            return 0.0
        return max(0.0, (pause_until - now).total_seconds())

    def update_from_headers(
        self,
        group: str,
        headers: Mapping[str, str] | None,
        *,
        now: datetime | None = None,
    ) -> RateLimitHeaderSnapshot:
        now = now or self._now()
        state = self._ensure_group(group)

        limit = state.rate_limit
        remaining = state.remaining
        reset_at = state.reset_at
        paused_until = state.paused_until

        if headers:
            normalized = {
                str(key).lower().replace("_", "-"): value for key, value in headers.items()
            }
            limit = _coerce_int(normalized.get("x-ratelimit-limit")) or limit
            remaining = _coerce_int(normalized.get("x-ratelimit-remaining"))
            reset_at = _coerce_reset_at(normalized.get("x-ratelimit-reset"), now) or reset_at
            if normalized.get("retry-after") is not None:
                retry_after = _coerce_float(normalized.get("retry-after"))
                if retry_after and retry_after > 0:
                    candidate = now + timedelta(seconds=retry_after)
                    paused_until = candidate if paused_until is None else max(paused_until, candidate)

        if remaining is not None and remaining <= 0:
            if reset_at is None:
                reset_at = now + timedelta(minutes=1)
            paused_until = reset_at if paused_until is None else max(paused_until, reset_at)

        state = replace(
            state,
            rate_limit=limit,
            remaining=remaining,
            reset_at=reset_at,
            paused_until=paused_until,
        )
        self._groups[group] = state
        return RateLimitHeaderSnapshot(
            group=group,
            limit=state.rate_limit,
            remaining=state.remaining,
            reset_at=state.reset_at,
            paused_until=state.paused_until,
        )

    def consume(self, group: str, *, amount: int = 1) -> None:
        state = self._ensure_group(group)
        remaining = state.remaining
        if remaining is None:
            return
        remaining = max(0, remaining - amount)
        self._groups[group] = replace(state, remaining=remaining)

    def pause_group(
        self,
        group: str,
        *,
        seconds: float,
        now: datetime | None = None,
    ) -> RateLimitHeaderSnapshot:
        now = now or self._now()
        state = self._ensure_group(group)
        candidate = now + timedelta(seconds=max(0.0, seconds))
        paused_until = (
            candidate
            if state.paused_until is None
            else max(state.paused_until, candidate)
        )
        state = replace(state, paused_until=paused_until)
        self._groups[group] = state
        return RateLimitHeaderSnapshot(
            group=group,
            limit=state.rate_limit,
            remaining=state.remaining,
            reset_at=state.reset_at,
            paused_until=state.paused_until,
        )

    def enqueue(
        self,
        group: str,
        *,
        request_id: str | None = None,
        priority: RateLimitPriority | int | str | None = None,
    ) -> QueuedRequest:
        request = QueuedRequest(
            request_id=request_id or str(uuid4()),
            group=group,
            priority=self._normalize_priority(priority),
            seq=self._next_seq(),
        )
        self._queue.append(request)
        return request

    def dequeue_ready(self, now: datetime | None = None) -> QueuedRequest | None:
        now = now or self._now()
        ready = [
            request
            for request in self._queue
            if not self.is_group_paused(request.group, now=now)
        ]
        if not ready:
            return None

        selected = min(ready, key=lambda request: (request.priority.value, request.seq))
        self._queue = [
            request
            for request in self._queue
            if request is not selected
        ]
        return selected

    @property
    def pending_count(self) -> int:
        return len(self._queue)
