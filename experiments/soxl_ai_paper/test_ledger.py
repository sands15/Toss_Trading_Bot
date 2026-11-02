from decimal import Decimal
from pathlib import Path

import pytest

import ledger as L

P = {"SOXL": Decimal("40"), "SOXS": Decimal("10"), "SOXX": Decimal("250")}


def prices(**kw):
    out = dict(P)
    out.update({k: Decimal(str(v)) for k, v in kw.items()})
    return out


def test_buy_hold_switch_and_costs(tmp_path: Path):
    L.cmd_init(tmp_path, "2026-11-02", Decimal("10000000"), Decimal("1400"))
    L.cmd_decide(tmp_path, "2026-11-02", "SOXL", Decimal("0.5"), "test")
    s = L.cmd_settle(tmp_path, "2026-11-02", prices(), prices(SOXL=42), None)
    assert s["position"]["symbol"] == "SOXL"
    assert Decimal(s["cash_usd"]) == pytest.approx(Decimal("7142.86") / 2, abs=Decimal("0.05"))
    # 판단 없으면 그대로 보유
    s = L.cmd_settle(tmp_path, "2026-11-03", prices(SOXL=42), prices(SOXL=43), None)
    assert s["position"]["days_held"] == 2
    L.cmd_decide(tmp_path, "2026-11-04", "SOXS", Decimal("0.3"), "flip")
    s = L.cmd_settle(tmp_path, "2026-11-04", prices(SOXL=43), prices(), None)
    assert s["position"]["symbol"] == "SOXS"
    trades = (tmp_path / "trades.csv").read_text().splitlines()
    assert len(trades) == 4  # header + buy + sell + buy


def test_rules_enforced(tmp_path: Path):
    L.cmd_init(tmp_path, "2026-11-02", Decimal("10000000"), Decimal("1400"))
    with pytest.raises(L.LedgerError):
        L.cmd_decide(tmp_path, "2026-11-02", "SOXL", Decimal("0.8"), "too big")
    with pytest.raises(L.LedgerError):
        L.cmd_decide(tmp_path, "2026-11-02", "TQQQ", Decimal("0.2"), "bad symbol")
    with pytest.raises(L.LedgerError):
        L.cmd_decide(tmp_path, "2026-11-02", "SOXL", Decimal("0.2"), " ")


def test_stop_loss_forces_cash(tmp_path: Path):
    L.cmd_init(tmp_path, "2026-11-02", Decimal("10000000"), Decimal("1400"))
    L.cmd_decide(tmp_path, "2026-11-02", "SOXL", Decimal("0.5"), "x")
    s = L.cmd_settle(tmp_path, "2026-11-02", prices(), prices(SOXL=35), None)
    assert s["forced_exit"].startswith("stop_loss")
    with pytest.raises(L.LedgerError):
        L.cmd_decide(tmp_path, "2026-11-03", "SOXL", Decimal("0.5"), "again")
    s = L.cmd_settle(tmp_path, "2026-11-03", prices(SOXL=35), prices(SOXL=36), None)
    assert s["position"] is None and s["forced_exit"] is None


def test_max_hold_days(tmp_path: Path):
    L.cmd_init(tmp_path, "2026-11-02", Decimal("10000000"), Decimal("1400"))
    L.cmd_decide(tmp_path, "2026-11-02", "SOXL", Decimal("0.5"), "x")
    for d in ("02", "03", "04", "05", "06"):
        s = L.cmd_settle(tmp_path, f"2026-11-{d}", prices(), prices(), None)
    assert s["forced_exit"].startswith("max_hold")


def test_no_double_settle(tmp_path: Path):
    L.cmd_init(tmp_path, "2026-11-02", Decimal("10000000"), Decimal("1400"))
    L.cmd_settle(tmp_path, "2026-11-02", prices(), prices(), None)
    with pytest.raises(L.LedgerError):
        L.cmd_settle(tmp_path, "2026-11-02", prices(), prices(), None)
