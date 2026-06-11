from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Any, Callable, Iterable, Mapping


PayloadProvider = Callable[[], Mapping[str, Any]]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


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
        return self.as_payload()

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


class HealthServer:
    """Read-only health payload producer and optional local HTTP endpoint host."""

    def __init__(
        self,
        snapshot_provider: PayloadProvider | HealthSnapshot,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        start_server: bool = False,
    ) -> None:
        self._snapshot_provider = (
            snapshot_provider
            if callable(snapshot_provider)
            else lambda: snapshot_provider
        )
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

    def payload_for_path(self, path: str) -> dict[str, Any]:
        snapshot = self._snapshot()
        normalized = path.lower()
        if normalized in {"/health", "/status"}:
            return snapshot.as_payload()
        if normalized == "/positions":
            return snapshot.positions_payload()
        if normalized == "/orders/open":
            return snapshot.open_orders_payload()
        if normalized == "/watchlist":
            return snapshot.watchlist_payload()
        raise ValueError(f"unsupported read-only path: {path}")

    def start(self) -> None:
        if self._server is not None:
            return

        server_ref = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path in {
                    "/health",
                    "/status",
                    "/positions",
                    "/orders/open",
                    "/watchlist",
                }:
                    payload = server_ref.payload_for_path(self.path)
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
                self.send_response(405)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                body = json.dumps({"error": "method not allowed"}).encode("utf-8")
                self.wfile.write(body)

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

