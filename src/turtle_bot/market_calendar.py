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
    sessions: tuple[Mapping[str, Any], ...] = ()
    tradable_sessions: tuple[str, ...] = ()

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
            "sessions": [dict(item) for item in self.sessions],
            "tradable_sessions": list(self.tradable_sessions),
            "current_session": self.status if self.is_open else None,
            "next_session": _next_session_payload(self.sessions),
        }


@dataclass(frozen=True)
class MarketCalendarConfig:
    market: str = "KR"
    timezone_name: str = "Asia/Seoul"
    open_session_names: tuple[str, ...] = ()


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
        now = self._now()
        local_date = now.astimezone(ZoneInfo(self.config.timezone_name)).date()
        payload = self.client.get_market_calendar(
            self.config.market,
            date=local_date.isoformat(),
        )
        return parse_market_session(
            payload,
            market=self.config.market,
            session_date=local_date,
            now=now,
            open_session_names=self.config.open_session_names,
        )


def parse_market_session(
    payload: Mapping[str, Any],
    *,
    market: str,
    session_date: date,
    now: datetime | None = None,
    open_session_names: tuple[str, ...] = (),
) -> MarketSessionState:
    official_state = _parse_official_session_payload(
        payload,
        market=market,
        session_date=session_date,
        now=now,
        open_session_names=open_session_names,
    )
    if official_state is not None:
        return official_state

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


def _parse_official_session_payload(
    payload: Mapping[str, Any],
    *,
    market: str,
    session_date: date,
    now: datetime | None,
    open_session_names: tuple[str, ...],
) -> MarketSessionState | None:
    today = payload.get("today")
    if not isinstance(today, Mapping):
        return None

    all_sessions = _official_session_candidates(today, market=market)
    session_payloads = _session_payloads(all_sessions)
    if not all_sessions:
        return MarketSessionState(
            market=market.upper(),
            session_date=session_date,
            is_open=False,
            known=True,
            status="HOLIDAY",
            raw=dict(payload),
            sessions=session_payloads,
            tradable_sessions=(),
        )
    allowed_names = _normalized_session_names(
        open_session_names or _default_open_session_names(market)
    )
    tradable_sessions = tuple(
        session_name
        for session_name, _session in all_sessions
        if session_name.lower() in allowed_names
    )

    if now is None:
        return MarketSessionState(
            market=market.upper(),
            session_date=session_date,
            is_open=False,
            known=True,
            status="SCHEDULED",
            raw=dict(payload),
            sessions=session_payloads,
            tradable_sessions=tradable_sessions,
        )

    now_utc = _aware_utc(now)
    for name, session in all_sessions:
        start = _parse_datetime(session.get("startTime"))
        end = _parse_datetime(session.get("endTime"))
        if start is None or end is None:
            continue
        if start <= now_utc <= end:
            allowed = name.lower() in allowed_names
            return MarketSessionState(
                market=market.upper(),
                session_date=session_date,
                is_open=allowed,
                known=True,
                status=name.upper(),
                raw=dict(payload),
                sessions=session_payloads,
                tradable_sessions=tradable_sessions,
            )

    return MarketSessionState(
        market=market.upper(),
        session_date=session_date,
        is_open=False,
        known=True,
        status="CLOSED",
        raw=dict(payload),
        sessions=session_payloads,
        tradable_sessions=tradable_sessions,
    )


def _official_session_candidates(
    today: Mapping[str, Any],
    *,
    market: str,
) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    market_key = market.upper()
    if market_key == "KR":
        integrated = today.get("integrated")
        if not isinstance(integrated, Mapping):
            return ()
        source = integrated
        names = ("regularMarket",)
    elif market_key == "US":
        source = today
        names = ("dayMarket", "preMarket", "regularMarket", "afterMarket")
    else:
        source = today
        names = ("regularMarket",)

    sessions: list[tuple[str, Mapping[str, Any]]] = []
    for name in names:
        value = source.get(name)
        if isinstance(value, Mapping):
            sessions.append((name, value))
    return tuple(sessions)


def _default_open_session_names(market: str) -> tuple[str, ...]:
    return ("regularmarket",)


def _normalized_session_names(values: tuple[str, ...]) -> frozenset[str]:
    return frozenset(str(value or "").strip().lower() for value in values if str(value or "").strip())


def _session_payloads(
    sessions: tuple[tuple[str, Mapping[str, Any]], ...]
) -> tuple[Mapping[str, Any], ...]:
    rows: list[Mapping[str, Any]] = []
    for name, session in sessions:
        start = _parse_datetime(session.get("startTime"))
        end = _parse_datetime(session.get("endTime"))
        rows.append(
            {
                "name": name,
                "label": _session_label(name),
                "start": start.isoformat() if start is not None else None,
                "end": end.isoformat() if end is not None else None,
            }
        )
    return tuple(rows)


def _next_session_payload(sessions: tuple[Mapping[str, Any], ...]) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc)
    future: list[Mapping[str, Any]] = []
    for session in sessions:
        start_raw = session.get("start")
        start = _parse_datetime(start_raw)
        if start is not None and start > now:
            future.append(session)
    if not future:
        return None
    return dict(
        sorted(
            future,
            key=lambda item: _parse_datetime(item.get("start")) or datetime.max.replace(tzinfo=timezone.utc),
        )[0]
    )


def _session_label(name: str) -> str:
    labels = {
        "dayMarket": "데이마켓",
        "preMarket": "프리마켓",
        "regularMarket": "정규장",
        "afterMarket": "애프터마켓",
    }
    return labels.get(name, name)


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return _aware_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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
