from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol


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
