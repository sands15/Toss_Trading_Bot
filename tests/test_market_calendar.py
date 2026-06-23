from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Mapping

from turtle_bot.market_calendar import (
    MarketCalendarConfig,
    MarketCalendarGate,
    parse_market_session,
)


class FakeCalendarClient:
    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str | None]] = []

    def get_market_calendar(
        self,
        market: str,
        *,
        date: str | None = None,
    ) -> Mapping[str, Any]:
        self.calls.append((market, date))
        return self.payload


def test_parse_market_session_from_boolean_open_flag() -> None:
    session = parse_market_session(
        {"isOpen": True},
        market="kr",
        session_date=date(2026, 1, 2),
    )

    assert session.market == "KR"
    assert session.is_open is True
    assert session.known is True
    assert session.blocker is None


def test_parse_market_session_blocks_closed_status() -> None:
    session = parse_market_session(
        {"marketStatus": "HOLIDAY"},
        market="KR",
        session_date=date(2026, 1, 2),
    )

    assert session.is_open is False
    assert session.known is True
    assert session.blocker == "market_session_not_open:holiday"


def test_parse_market_session_unknown_when_payload_has_no_state() -> None:
    session = parse_market_session(
        {"date": "2026-01-02"},
        market="KR",
        session_date=date(2026, 1, 2),
    )

    assert session.is_open is False
    assert session.known is False
    assert session.blocker == "market_calendar_unknown"


def test_parse_official_us_market_calendar_regular_session() -> None:
    session = parse_market_session(
        {
            "today": {
                "date": "2026-01-02",
                "regularMarket": {
                    "startTime": "2026-01-02T14:30:00+00:00",
                    "endTime": "2026-01-02T21:00:00+00:00",
                },
            }
        },
        market="US",
        session_date=date(2026, 1, 2),
        now=datetime(2026, 1, 2, 15, tzinfo=timezone.utc),
    )

    assert session.is_open is True
    assert session.known is True
    assert session.status == "REGULARMARKET"
    assert session.blocker is None


def test_parse_official_us_market_calendar_closed_outside_regular_session() -> None:
    session = parse_market_session(
        {
            "today": {
                "date": "2026-01-02",
                "regularMarket": {
                    "startTime": "2026-01-02T14:30:00+00:00",
                    "endTime": "2026-01-02T21:00:00+00:00",
                },
            }
        },
        market="US",
        session_date=date(2026, 1, 2),
        now=datetime(2026, 1, 2, 22, tzinfo=timezone.utc),
    )

    assert session.is_open is False
    assert session.known is True
    assert session.blocker == "market_session_not_open:closed"


def test_parse_official_us_day_market_is_displayed_but_not_tradable_by_default() -> None:
    session = parse_market_session(
        {
            "today": {
                "date": "2026-01-02",
                "dayMarket": {
                    "startTime": "2026-01-02T00:00:00+00:00",
                    "endTime": "2026-01-02T08:00:00+00:00",
                },
                "regularMarket": {
                    "startTime": "2026-01-02T14:30:00+00:00",
                    "endTime": "2026-01-02T21:00:00+00:00",
                },
            }
        },
        market="US",
        session_date=date(2026, 1, 2),
        now=datetime(2026, 1, 2, 1, tzinfo=timezone.utc),
    )

    assert session.is_open is False
    assert session.status == "DAYMARKET"
    assert session.blocker == "market_session_not_open:daymarket"
    assert [item["name"] for item in session.as_payload()["sessions"]] == [
        "dayMarket",
        "regularMarket",
    ]


def test_parse_official_us_day_market_can_be_enabled() -> None:
    session = parse_market_session(
        {
            "today": {
                "date": "2026-01-02",
                "dayMarket": {
                    "startTime": "2026-01-02T00:00:00+00:00",
                    "endTime": "2026-01-02T08:00:00+00:00",
                },
                "regularMarket": {
                    "startTime": "2026-01-02T14:30:00+00:00",
                    "endTime": "2026-01-02T21:00:00+00:00",
                },
            }
        },
        market="US",
        session_date=date(2026, 1, 2),
        now=datetime(2026, 1, 2, 1, tzinfo=timezone.utc),
        open_session_names=("dayMarket", "regularMarket"),
    )

    assert session.is_open is True
    assert session.status == "DAYMARKET"
    assert session.blocker is None
    assert session.as_payload()["tradable_sessions"] == ["dayMarket", "regularMarket"]


def test_parse_official_kr_market_calendar_holiday() -> None:
    session = parse_market_session(
        {"today": {"date": "2026-01-02", "integrated": None}},
        market="KR",
        session_date=date(2026, 1, 2),
        now=datetime(2026, 1, 2, 1, tzinfo=timezone.utc),
    )

    assert session.is_open is False
    assert session.known is True
    assert session.blocker == "market_session_not_open:holiday"


def test_market_calendar_gate_uses_local_session_date() -> None:
    client = FakeCalendarClient({"status": "OPEN"})
    gate = MarketCalendarGate(
        client=client,
        config=MarketCalendarConfig(market="KR", timezone_name="Asia/Seoul"),
        now=lambda: datetime(2026, 1, 1, 16, tzinfo=timezone.utc),
    )

    session = gate.current_session()

    assert session.session_date == date(2026, 1, 2)
    assert session.is_open is True
    assert client.calls == [("KR", "2026-01-02")]
