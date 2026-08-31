from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import turtle_runtime.paper_status as paper_status
from turtle_runtime.paper_status import (
    MAX_STATUS_BYTES,
    PaperStatusError,
    PaperStatusWriter,
    derive_paper_status_path,
    read_paper_status,
)


NOW = datetime(2026, 8, 31, 13, 30, tzinfo=timezone.utc)
RELEASE_SHA = "a" * 40
BOOT_HASH = "b" * 64


def _month(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "run_id": "must-not-leak",
        "simulation_account_key": "must-not-leak",
        "status": "ACTIVE",
        "start_date": "2026-08-31",
        "end_date": "2026-09-30",
        "initial_cash": "10000",
        "current_cash": "9990",
        "final_equity": "9990",
        "net_pnl": "-10",
        "return_fraction": "-0.001",
        "trades": 2,
        "wins": 1,
        "losses": 1,
        "win_rate": "0.5",
        "total_fees": "2",
        "max_drawdown": "12",
        "max_drawdown_fraction": "0.0012",
        "no_entry_sessions": 1,
        "no_candidate_sessions": 1,
        "invalid_sessions": 0,
        "unresolved_positions": 0,
        "waiting_plans": 0,
        "coverage_expected": 23,
        "coverage_covered": 2,
        "coverage_missing": 21,
        "journal_policy": {"private_path": "/must/not/leak"},
    }
    value.update(overrides)
    return value


def _day(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "run_id": "must-not-leak",
        "plan_id": "must-not-leak",
        "session_date": "2026-09-01",
        "symbol": "AAPL",
        "status": "CLOSED",
        "net_pnl": "-10",
        "fees": "2",
        "cash_start": "10000",
        "cash_end": "9990",
        "data_gaps": 0,
        "entry_price": "must-not-leak",
    }
    value.update(overrides)
    return value


def _cohort_month(**overrides: object) -> dict[str, object]:
    value = _month(
        current_cash="9990",
        final_equity="9990",
        net_pnl="-10",
        return_fraction="-0.001",
        trades=2,
        no_candidate_sessions=1,
    )
    value.update(
        {
            "simulation_lanes": 2,
            "lanes": {
                "A": _month(
                    status="ACTIVE",
                    current_cash="5000",
                    final_equity="5000",
                    net_pnl="0",
                    return_fraction="0",
                    trades=1,
                    no_candidate_sessions=1,
                ),
                "B": _month(
                    status="ACTIVE",
                    current_cash="4990",
                    final_equity="4990",
                    net_pnl="-10",
                    return_fraction="-0.002",
                    trades=1,
                    no_candidate_sessions=0,
                ),
            },
            "sessions": [
                {
                    "session_date": "2026-08-31",
                    "covered": True,
                    "distinct_symbols": True,
                    "lanes": {
                        "A": _day(
                            session_date="2026-08-31",
                            symbol=None,
                            status="NO_CANDIDATE",
                            net_pnl="0",
                            fees="0",
                            cash_start=None,
                            cash_end=None,
                        ),
                        "B": _day(
                            session_date="2026-08-31",
                            symbol="MSFT",
                            status="CLOSED",
                            net_pnl="-10",
                            cash_start="5000",
                            cash_end="4990",
                        ),
                    },
                },
                {
                    "session_date": "2026-09-01",
                    "covered": True,
                    "distinct_symbols": True,
                    "lanes": {
                        "A": _day(
                            session_date="2026-09-01",
                            symbol="AAPL",
                            status="CLOSED",
                            net_pnl="0",
                            cash_start="5000",
                            cash_end="5000",
                        ),
                        "B": _day(
                            session_date="2026-09-01",
                            symbol="MSFT",
                            status="NO_ENTRY",
                            net_pnl="0",
                            cash_start="4990",
                            cash_end="4990",
                        ),
                    },
                },
            ],
        }
    )
    value.update(overrides)
    return value


def _writer(tmp_path: Path, *, now: datetime = NOW, release: str = RELEASE_SHA, boot: str = BOOT_HASH) -> PaperStatusWriter:
    envelope = (tmp_path / "approval" / "approval-envelope.json").resolve()
    return PaperStatusWriter(
        derive_paper_status_path(envelope),
        release_sha=release,
        boot_id_hash=boot,
        clock=lambda: now,
    )


def _read(path: Path, *, now: datetime = NOW, release: str = RELEASE_SHA, boot: str = BOOT_HASH, max_age: float = 130):
    return read_paper_status(
        path,
        expected_release_sha=release,
        expected_boot_id_hash=boot,
        clock=lambda: now,
        max_age_seconds=max_age,
    )


def _rewrite(path: Path, mutate) -> None:
    payload = json.loads(path.read_text(encoding="ascii"))
    mutate(payload)
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="ascii")
    if os.name != "nt":
        path.chmod(0o600)


def test_derive_status_path_accepts_only_absolute_approval_envelope(tmp_path: Path) -> None:
    envelope = (tmp_path / "approval-envelope.json").resolve()
    assert derive_paper_status_path(envelope) == envelope.with_name("paper-status.json")
    for invalid in (Path("approval-envelope.json"), tmp_path / "wrong.json"):
        with pytest.raises(PaperStatusError, match="paper_status_configuration_invalid"):
            derive_paper_status_path(invalid)


def test_writer_publishes_exact_safe_schema_atomically_and_reader_accepts_it(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    writer.write(_month(), planner_ready=True, blocker_codes=[], latest_day=_day())

    payload = _read(writer.path)
    assert set(payload) == {
        "schema_version", "release_sha", "boot_id_hash", "mode", "live_order_submission",
        "updated_at", "planner_ready", "blocker_codes", "run_status", "start_date",
        "end_date", "initial_cash_usd", "current_cash_usd", "final_equity_usd",
        "realized_pnl_usd", "return_fraction", "trade_count", "wins", "losses",
        "win_rate", "total_fees_usd", "max_drawdown_usd", "max_drawdown_fraction",
        "no_entry_count", "invalid_result_count", "unresolved_position_count",
        "waiting_plan_count", "coverage_expected_count", "coverage_covered_count",
        "coverage_missing_count", "no_candidate_count", "latest_day",
    }
    assert payload["schema_version"] == 2
    assert payload["mode"] == "shadow"
    assert payload["live_order_submission"] is False
    assert payload["current_cash_usd"] == "9990"
    assert payload["no_candidate_count"] == 1
    assert payload["latest_day"] == {
        "session_date": "2026-09-01",
        "symbol": "AAPL",
        "status": "CLOSED",
        "net_pnl_usd": "-10",
        "fees_usd": "2",
        "cash_start_usd": "10000",
        "cash_end_usd": "9990",
        "data_gap_count": 0,
    }
    raw = writer.path.read_text(encoding="ascii")
    assert "must-not-leak" not in raw and "/must/not/leak" not in raw
    assert len(raw.encode("ascii")) <= MAX_STATUS_BYTES
    assert list(writer.path.parent.glob(".*.tmp")) == []
    if os.name != "nt":
        assert stat.S_IMODE(writer.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(writer.path.parent.stat().st_mode) & 0o077 == 0


def test_nullable_final_equity_latest_day_and_blockers_round_trip(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    writer.write(
        _month(status="OPEN", final_equity=None, return_fraction=None, unresolved_positions=1),
        planner_ready=False,
        blocker_codes=["paper_position_open"],
    )
    payload = _read(writer.path)
    assert payload["final_equity_usd"] is None
    assert payload["return_fraction"] is None
    assert payload["latest_day"] is None
    assert payload["blocker_codes"] == ["paper_position_open"]


def test_two_lane_writer_publishes_safe_lane_summaries_and_distinct_sessions(
    tmp_path: Path,
) -> None:
    writer = _writer(tmp_path)
    writer.write(_cohort_month(), planner_ready=True, blocker_codes=[])

    payload = _read(writer.path)

    assert payload["schema_version"] == 3
    assert payload["simulation_lanes"] == 2
    assert payload["distinct_trading_session_count"] == 2
    assert set(payload["lanes"]) == {"A", "B"}
    assert payload["lanes"]["A"]["current_cash_usd"] == "5000"
    assert payload["lanes"]["B"]["realized_pnl_usd"] == "-10"
    assert payload["lanes"]["A"]["latest_day"]["symbol"] == "AAPL"
    assert payload["lanes"]["B"]["latest_day"]["status"] == "NO_ENTRY"
    raw = writer.path.read_text(encoding="ascii")
    assert "must-not-leak" not in raw and "plan_id" not in raw


def test_two_lane_writer_rejects_duplicate_symbols_and_inconsistent_totals(
    tmp_path: Path,
) -> None:
    duplicate = _cohort_month()
    duplicate["sessions"][0]["lanes"]["B"]["symbol"] = "AAPL"
    duplicate["sessions"][0]["lanes"]["A"] = _day(
        session_date="2026-08-31",
        symbol="AAPL",
        status="CLOSED",
    )
    with pytest.raises(PaperStatusError, match="paper_status_value_invalid"):
        _writer(tmp_path).write(duplicate, planner_ready=True, blocker_codes=[])

    inconsistent = _cohort_month()
    inconsistent["lanes"]["B"]["current_cash"] = "4989"
    with pytest.raises(PaperStatusError, match="paper_status_value_invalid"):
        _writer(tmp_path).write(inconsistent, planner_ready=True, blocker_codes=[])


def test_writer_normalizes_blockers_without_leaking_human_configuration(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    writer.write(
        _month(),
        planner_ready=False,
        blocker_codes=["z_code", "contains private /Users/alice/path", "a_code", "z_code"],
    )
    payload = _read(writer.path)
    assert payload["blocker_codes"] == [
        "a_code",
        "planner_configuration_blocked",
        "z_code",
    ]
    assert "/Users/alice/path" not in writer.path.read_text(encoding="ascii")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.__setitem__("extra", True),
        lambda value: value.pop("current_cash_usd"),
        lambda value: value.__setitem__("current_cash_usd", "9990.00"),
        lambda value: value.__setitem__("trade_count", True),
        lambda value: value.__setitem__("mode", "live"),
        lambda value: value.__setitem__("live_order_submission", True),
        lambda value: value.__setitem__("coverage_missing_count", 20),
        lambda value: value.__setitem__("no_candidate_count", 3),
        lambda value: value["latest_day"].__setitem__("plan_id", "leak"),
    ],
)
def test_reader_rejects_extra_missing_type_invariant_and_nested_keys(tmp_path: Path, mutate) -> None:
    writer = _writer(tmp_path)
    writer.write(_month(), planner_ready=True, blocker_codes=[], latest_day=_day())
    _rewrite(writer.path, mutate)
    with pytest.raises(PaperStatusError, match="paper_status_invalid"):
        _read(writer.path)


def test_reader_rejects_duplicates_nonfinite_oversize_and_symlink(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    writer.write(_month(), planner_ready=True, blocker_codes=[])

    writer.path.write_bytes(b'{"schema_version":2,"schema_version":2}')
    with pytest.raises(PaperStatusError, match="paper_status_invalid"):
        _read(writer.path)
    writer.path.write_bytes(b'{"value":NaN}')
    with pytest.raises(PaperStatusError, match="paper_status_invalid"):
        _read(writer.path)
    writer.path.write_bytes(b"x" * (MAX_STATUS_BYTES + 1))
    with pytest.raises(PaperStatusError, match="paper_status_invalid"):
        _read(writer.path)

    writer.path.unlink()
    target = tmp_path / "elsewhere.json"
    target.write_text("untouched", encoding="ascii")
    try:
        writer.path.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(PaperStatusError, match="paper_status_invalid"):
        _read(writer.path)
    with pytest.raises(PaperStatusError, match="paper_status_path_invalid"):
        writer.write(_month(), planner_ready=True, blocker_codes=[])
    assert target.read_text(encoding="ascii") == "untouched"


def test_writer_removes_temporary_file_when_atomic_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = _writer(tmp_path)

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(paper_status.os, "replace", fail_replace)
    with pytest.raises(PaperStatusError, match="paper_status_write_failed"):
        writer.write(_month(), planner_ready=True, blocker_codes=[])
    assert not writer.path.exists()
    assert list(writer.path.parent.glob(".*.tmp")) == []


def test_reader_binds_release_boot_and_freshness(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    writer.write(_month(), planner_ready=True, blocker_codes=[])
    with pytest.raises(PaperStatusError, match="paper_status_release_mismatch"):
        _read(writer.path, release="c" * 40)
    with pytest.raises(PaperStatusError, match="paper_status_boot_mismatch"):
        _read(writer.path, boot="d" * 64)
    with pytest.raises(PaperStatusError, match="paper_status_stale"):
        _read(writer.path, now=NOW + timedelta(seconds=131))
    with pytest.raises(PaperStatusError, match="paper_status_stale"):
        _read(writer.path, now=NOW - timedelta(seconds=6))
    assert _read(writer.path, now=NOW + timedelta(seconds=130))["run_status"] == "ACTIVE"
    assert _read(writer.path, now=NOW - timedelta(seconds=5))["run_status"] == "ACTIVE"


@pytest.mark.parametrize(
    "month,ready,blockers,day",
    [
        (_month(initial_cash="10000.0"), True, [], None),
        (_month(status="RUNNING"), True, [], None),
        (_month(wins=2, losses=1, trades=2), True, [], None),
        (_month(), True, ["blocked"], None),
        (_month(), True, [], _day(symbol="aapl")),
        (_month(), True, [], _day(session_date="2026-10-01")),
        (
            _month(no_candidate_sessions=0),
            True,
            [],
            _day(status="NO_CANDIDATE", symbol=None),
        ),
    ],
)
def test_writer_rejects_noncanonical_or_inconsistent_public_values(
    tmp_path: Path,
    month: dict[str, object],
    ready: bool,
    blockers: list[str],
    day: dict[str, object] | None,
) -> None:
    with pytest.raises(PaperStatusError, match="paper_status_value_invalid"):
        _writer(tmp_path).write(month, planner_ready=ready, blocker_codes=blockers, latest_day=day)


@pytest.mark.parametrize("status", ["NO_CANDIDATE", "MARKET_CLOSED"])
def test_non_trading_latest_day_has_no_symbol(tmp_path: Path, status: str) -> None:
    writer = _writer(tmp_path)
    writer.write(
        _month(),
        planner_ready=True,
        blocker_codes=[],
        latest_day=_day(
            status=status,
            symbol=None,
            net_pnl="0",
            fees="0",
            cash_start=None,
            cash_end=None,
        ),
    )

    latest = _read(writer.path)["latest_day"]
    assert latest["status"] == status
    assert latest["symbol"] is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and mode contract")
def test_reader_requires_exact_owner_only_file_and_private_parent(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    writer.write(_month(), planner_ready=True, blocker_codes=[])
    writer.path.chmod(0o640)
    with pytest.raises(PaperStatusError, match="paper_status_permissions_invalid"):
        _read(writer.path)
    writer.path.chmod(0o600)
    writer.path.parent.chmod(0o750)
    with pytest.raises(PaperStatusError, match="paper_status_permissions_invalid"):
        _read(writer.path)
