from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .health import HealthSnapshot


@dataclass(frozen=True)
class RuntimeState:
    """Minimal paper-mode-safe runtime state surface."""

    mode: str = "idle"
    ready: bool = True
    blockers: tuple[str, ...] = field(default_factory=tuple)
    positions: tuple[dict, ...] = field(default_factory=tuple)
    open_orders: tuple[dict, ...] = field(default_factory=tuple)
    watchlist: tuple[dict, ...] = field(default_factory=tuple)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class Runtime:
    """Read-only shell runtime used by CLI and health helpers."""

    state: RuntimeState = field(default_factory=RuntimeState)

    @classmethod
    def default(cls) -> "Runtime":
        return cls()

    def health_snapshot(self) -> HealthSnapshot:
        return HealthSnapshot(
            mode=self.state.mode,
            ready=self.state.ready,
            blockers=self.state.blockers,
            positions=self.state.positions,
            open_orders=self.state.open_orders,
            watchlist=self.state.watchlist,
            generated_at=self.state.started_at,
        )
