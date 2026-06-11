from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Mapping, Protocol
from zoneinfo import ZoneInfo


OPEN_STATUS_VALUES = frozenset({"OPEN", "OPENED", "TRADING", "REGULAR"})
CLOSED_STATUS_VALUES = frozenset(
    {"CLOSED", "CLOSE", "HOLIDAY", "WEEKEND", "SUSPENDED", "PREOPEN", "POSTMARKET"}
)


class ReadOnlyMarketCalendarClient(Protocol):
    def get_market_calendar(
        self,
        market: str,
        *,
        date: str | None = None,
    ) -> Mapping[str, Any]:
        ...


@dataclass(frozen=True)
class MarketSessionState:
    market: str
    session_date: date
    is_open: bool
    known: bool
    status: str
    raw: Mapping[str, Any]

    @property
    def blocker(self) -> str | None:
        if not self.known:
            return "market_calendar_unknown"
        if not self.is_open:
            return f"market_session_not_open:{self.status.lower()}"
        return None

    def as_payload(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "session_date": self.session_date.isoformat(),
            "is_open": self.is_open,
            "known": self.known,
            "status": self.status,
            "blocker": self.blocker,
        }


@dataclass(frozen=True)
class MarketCalendarConfig:
    market: str = "KR"
    timezone_name: str = "Asia/Seoul"


class MarketCalendarGate:
    def __init__(
        self,
        *,
        client: ReadOnlyMarketCalendarClient,
        config: MarketCalendarConfig | None = None,
        now=lambda: datetime.now(timezone.utc),
    ) -> None:
        self.client = client
        self.config = config or MarketCalendarConfig()
        self._now = now

    def current_session(self) -> MarketSessionState:
        local_date = self._now().astimezone(ZoneInfo(self.config.timezone_name)).date()
        payload = self.client.get_market_calendar(
            self.config.market,
            date=local_date.isoformat(),
        )
        return parse_market_session(
            payload,
            market=self.config.market,
            session_date=local_date,
        )


def parse_market_session(
    payload: Mapping[str, Any],
    *,
    market: str,
    session_date: date,
) -> MarketSessionState:
    raw_status = _first_value(
        payload,
        "status",
        "marketStatus",
        "sessionStatus",
        "state",
        "marketState",
    )
    raw_is_open = _first_value(
        payload,
        "isOpen",
        "open",
        "isTradingDay",
        "isRegularMarketOpen",
    )

    if isinstance(raw_is_open, bool):
        status = _status_text(raw_status, "OPEN" if raw_is_open else "CLOSED")
        return MarketSessionState(
            market=market.upper(),
            session_date=session_date,
            is_open=raw_is_open,
            known=True,
            status=status,
            raw=dict(payload),
        )

    if raw_status is not None:
        status = _status_text(raw_status, "UNKNOWN")
        upper = status.upper()
        if upper in OPEN_STATUS_VALUES:
            return MarketSessionState(
                market=market.upper(),
                session_date=session_date,
                is_open=True,
                known=True,
                status=status,
                raw=dict(payload),
            )
        if upper in CLOSED_STATUS_VALUES:
            return MarketSessionState(
                market=market.upper(),
                session_date=session_date,
                is_open=False,
                known=True,
                status=status,
                raw=dict(payload),
            )

    return MarketSessionState(
        market=market.upper(),
        session_date=session_date,
        is_open=False,
        known=False,
        status=_status_text(raw_status, "UNKNOWN"),
        raw=dict(payload),
    )


def _first_value(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    for value in payload.values():
        if isinstance(value, Mapping):
            nested = _first_value(value, *keys)
            if nested is not None:
                return nested
    return None


def _status_text(value: Any, default: str) -> str:
    if value is None or value == "":
        return default
    return str(value)

