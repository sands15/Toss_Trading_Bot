import pytest

import breakout_ledger as B


class Prov:
    def __init__(self, quotes):
        self.quotes = quotes

    def quote_on(self, s, d):
        return self.quotes.get((s, d))

    def close_on(self, s, d):
        q = self.quotes.get((s, d))
        return q["close"] if q else None


def q(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


def setup(tmp_path, quotes):
    B.cmd_init(tmp_path, "d0", 10_000_000, 1000.0)
    prov = Prov(quotes)
    B.cmd_add(tmp_path, "d0", "AAA", 100.0, 95.0, 110.0, 10, prov=prov)
    return prov


def base():
    return {("AAA", "d0"): q(99, 101, 98, 100), ("SPY", "d0"): q(500, 501, 499, 500)}


def test_hold_then_target(tmp_path):
    qs = base() | {("AAA", "d1"): q(100, 104, 99, 103), ("SPY", "d1"): q(500, 502, 499, 501),
                   ("AAA", "d2"): q(104, 111, 103, 109), ("SPY", "d2"): q(501, 503, 500, 502)}
    prov = setup(tmp_path, qs)
    B.cmd_run(tmp_path, "d1", prov=prov)
    st = B.load_state(tmp_path)
    assert st["positions"][0]["last_close"] == 103
    out = B.cmd_run(tmp_path, "d2", prov=prov)
    st = B.load_state(tmp_path)
    assert st["positions"] == [] and "목표 도달" in out and "+2.00R" in out


def test_stop_wins_when_both_hit(tmp_path):
    qs = base() | {("AAA", "d1"): q(100, 112, 94, 105), ("SPY", "d1"): q(500, 502, 499, 501)}
    prov = setup(tmp_path, qs)
    out = B.cmd_run(tmp_path, "d1", prov=prov)
    assert "손절 @ 95.00" in out and "-1.00R" in out


def test_gap_down_fills_at_open(tmp_path):
    qs = base() | {("AAA", "d1"): q(90, 92, 88, 91), ("SPY", "d1"): q(500, 502, 499, 501)}
    prov = setup(tmp_path, qs)
    out = B.cmd_run(tmp_path, "d1", prov=prov)
    assert "갭 하락 시가 손절 @ 90.00" in out and "-2.00R" in out


def test_add_rejects_bad_levels_and_out_of_range(tmp_path):
    B.cmd_init(tmp_path, "d0", 10_000_000, 1000.0)
    prov = Prov(base())
    with pytest.raises(B.BreakoutError):
        B.cmd_add(tmp_path, "d0", "AAA", 100.0, 101.0, 110.0, 1, prov=prov)
    with pytest.raises(B.BreakoutError):
        B.cmd_add(tmp_path, "d0", "AAA", 120.0, 95.0, 130.0, 1, prov=prov)


def test_no_rerun_same_date(tmp_path):
    qs = base() | {("AAA", "d1"): q(100, 104, 99, 103), ("SPY", "d1"): q(500, 502, 499, 501)}
    prov = setup(tmp_path, qs)
    B.cmd_run(tmp_path, "d1", prov=prov)
    with pytest.raises(B.BreakoutError):
        B.cmd_run(tmp_path, "d1", prov=prov)
