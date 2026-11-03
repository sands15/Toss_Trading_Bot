from decimal import Decimal
from pathlib import Path

import pytest

import ledger as L

BASE = {"SOXL": Decimal("40"), "SOXS": Decimal("10"), "SOXX": Decimal("250")}


def px(**kw):
    out = dict(BASE)
    out.update({k: Decimal(str(v)) for k, v in kw.items()})
    return out


def settle(tmp, date, o=None, h=None, l=None, c=None):
    o, c = o or px(), c or px()
    h = h or {s: max(o[s], c[s]) for s in L.SYMBOLS}
    l = l or {s: min(o[s], c[s]) for s in L.SYMBOLS}
    return L.cmd_settle(tmp, date, o, h, l, c, None)


@pytest.fixture
def tmp(tmp_path: Path) -> Path:
    L.cmd_init(tmp_path, "2026-11-02", Decimal("10000000"), Decimal("1400"))
    return tmp_path


def trades(tmp):
    return (tmp / "trades.csv").read_text(encoding="utf-8").splitlines()[1:]


def test_buy_hold_switch(tmp):
    L.cmd_decide(tmp, "2026-11-02", "SOXL", Decimal("0.5"), "t")
    s = settle(tmp, "2026-11-02", c=px(SOXL=42))
    assert s["position"]["symbol"] == "SOXL"
    assert Decimal(s["position"]["stop_price"]) == pytest.approx(Decimal("40.02") * Decimal("0.9"), abs=Decimal("0.001"))
    s = settle(tmp, "2026-11-03", o=px(SOXL=42), c=px(SOXL=43))
    assert s["position"]["days_held"] == 2
    L.cmd_decide(tmp, "2026-11-04", "SOXS", Decimal("0.3"), "flip")
    s = settle(tmp, "2026-11-04", o=px(SOXL=43))
    assert s["position"]["symbol"] == "SOXS"
    assert len(trades(tmp)) == 3


def test_rules_enforced(tmp):
    for args in [("SOXL", "0.8", "0.1", "x"), ("TQQQ", "0.2", "0.1", "x"),
                 ("SOXL", "0.2", "0.1", " "), ("SOXL", "0.2", "0.2", "x"),
                 ("SOXL", "0.2", "0.01", "x")]:
        with pytest.raises(L.LedgerError):
            L.cmd_decide(tmp, "2026-11-02", args[0], Decimal(args[1]), args[3], Decimal(args[2]))


def test_stop_order_hits_intraday(tmp):
    L.cmd_decide(tmp, "2026-11-02", "SOXL", Decimal("0.5"), "x")
    settle(tmp, "2026-11-02")
    # 다음 날 장중 저가 34 → 손절가(약 36.02)에서 체결, 종가 회복은 무관
    s = settle(tmp, "2026-11-03", o=px(SOXL=39), l={"SOXL": Decimal("34"), "SOXS": Decimal("10")},
               c=px(SOXL=41))
    assert s["position"] is None and s["cooldown"]
    sell = trades(tmp)[-1].split(",")
    assert sell[1] == "SELL" and Decimal(sell[4]) < Decimal("36.02") and Decimal(sell[4]) > Decimal("35.8")
    with pytest.raises(L.LedgerError):
        L.cmd_decide(tmp, "2026-11-04", "SOXL", Decimal("0.5"), "again")
    L.cmd_decide(tmp, "2026-11-04", "CASH", Decimal("0"), "rest")
    s = settle(tmp, "2026-11-04")
    assert s["cooldown"] is None
    L.cmd_decide(tmp, "2026-11-05", "SOXL", Decimal("0.5"), "back")


def test_stop_gap_down_fills_at_open(tmp):
    L.cmd_decide(tmp, "2026-11-02", "SOXL", Decimal("0.5"), "x")
    settle(tmp, "2026-11-02")
    settle(tmp, "2026-11-03", o=px(SOXL=30), c=px(SOXL=31))
    sell = trades(tmp)[-1].split(",")
    assert Decimal(sell[4]) < Decimal("30")


def test_entry_day_stop(tmp):
    L.cmd_decide(tmp, "2026-11-02", "SOXL", Decimal("0.5"), "x")
    s = settle(tmp, "2026-11-02", l={"SOXL": Decimal("35"), "SOXS": Decimal("10")}, c=px(SOXL=38))
    assert s["position"] is None


def test_intraday_exit(tmp):
    with pytest.raises(L.LedgerError):
        L.cmd_intraday(tmp, "2026-11-02", Decimal("39"), "01:07 KST", "src", "nothing held")
    L.cmd_decide(tmp, "2026-11-02", "SOXL", Decimal("0.5"), "x")
    L.cmd_intraday(tmp, "2026-11-02", Decimal("38"), "01:07 KST", "https://x", "악재")
    s = settle(tmp, "2026-11-02", l={"SOXL": Decimal("37.5"), "SOXS": Decimal("10")}, c=px(SOXL=37.8))
    assert s["position"] is None and s["intraday"] is None
    assert "장중 점검" in trades(tmp)[-1]


def test_intraday_but_stop_hit_is_conservative(tmp):
    L.cmd_decide(tmp, "2026-11-02", "SOXL", Decimal("0.5"), "x")
    L.cmd_intraday(tmp, "2026-11-02", Decimal("39"), "01:07 KST", "https://x", "악재")
    settle(tmp, "2026-11-02", l={"SOXL": Decimal("35"), "SOXS": Decimal("10")})
    assert "손절 조건" in trades(tmp)[-1]


def test_stop_can_only_tighten(tmp):
    L.cmd_decide(tmp, "2026-11-02", "SOXL", Decimal("0.5"), "x", Decimal("0.05"))
    settle(tmp, "2026-11-02")
    with pytest.raises(L.LedgerError):
        L.cmd_decide(tmp, "2026-11-03", "SOXL", Decimal("0.5"), "loosen", Decimal("0.10"))
    L.cmd_decide(tmp, "2026-11-03", "SOXL", Decimal("0.5"), "tighten", Decimal("0.03"))
    s = settle(tmp, "2026-11-03")
    assert Decimal(s["position"]["stop_pct"]) == Decimal("0.03")


def test_max_hold_days(tmp):
    L.cmd_decide(tmp, "2026-11-02", "SOXL", Decimal("0.5"), "x")
    for d in ("02", "03", "04", "05", "06"):
        s = settle(tmp, f"2026-11-{d}")
    assert s["forced_exit"].startswith("max_hold")
    s = settle(tmp, "2026-11-09")
    assert s["position"] is None


def test_bad_ohlc_and_double_settle(tmp):
    with pytest.raises(L.LedgerError):
        settle(tmp, "2026-11-02", l={"SOXL": Decimal("41"), "SOXS": Decimal("10")})
    settle(tmp, "2026-11-02")
    with pytest.raises(L.LedgerError):
        settle(tmp, "2026-11-02")


def test_gate_blocks_after_big_day_loss(tmp):
    L.cmd_decide(tmp, "2026-11-02", "SOXL", Decimal("0.5"), "x")
    # SOXL -9% close, stop not hit (stop ~36.02): account ~ -4.5%
    settle(tmp, "2026-11-02", c=px(SOXL=36.5))
    g = L.discipline_gate(tmp, L.load_state(tmp))
    assert g["level"] == "FAIL"
    with pytest.raises(L.LedgerError):
        L.cmd_decide(tmp, "2026-11-03", "SOXS", Decimal("0.2"), "revenge")
    L.cmd_decide(tmp, "2026-11-03", "CASH", Decimal("0"), "rest")


def test_gate_no_size_up_after_loss(tmp):
    L.cmd_decide(tmp, "2026-11-02", "SOXL", Decimal("0.2"), "x")
    settle(tmp, "2026-11-02")
    L.cmd_decide(tmp, "2026-11-03", "CASH", Decimal("0"), "exit")
    settle(tmp, "2026-11-03", o=px(SOXL=39.5))  # small losing exit
    st = L.load_state(tmp)
    assert Decimal(st["closed"][-1]["pnl_usd"]) < 0
    with pytest.raises(L.LedgerError):
        L.cmd_decide(tmp, "2026-11-04", "SOXL", Decimal("0.4"), "double down")
    L.cmd_decide(tmp, "2026-11-04", "SOXL", Decimal("0.2"), "same size ok")


def test_gate_loss_streak_then_rest(tmp):
    for d1, d2 in (("02", "03"), ("04", "05")):
        L.cmd_decide(tmp, f"2026-11-{d1}", "SOXL", Decimal("0.1"), "x")
        settle(tmp, f"2026-11-{d1}")
        L.cmd_decide(tmp, f"2026-11-{d2}", "CASH", Decimal("0"), "exit")
        settle(tmp, f"2026-11-{d2}", o=px(SOXL=39.8))
    assert L.discipline_gate(tmp, L.load_state(tmp))["level"] == "FAIL"
    L.cmd_decide(tmp, "2026-11-06", "CASH", Decimal("0"), "rest")
    settle(tmp, "2026-11-06")
    assert L.discipline_gate(tmp, L.load_state(tmp), "SOXL", Decimal("0.1"))["level"] != "FAIL"


def test_gate_pass_fresh(tmp):
    g = L.discipline_gate(tmp, L.load_state(tmp), "SOXL", Decimal("0.5"))
    assert g["level"] == "PASS"
