import pytest

import spy_rebound_ledger as S


class Prov:
    def __init__(self, closes, quotes):
        self.closes, self.quotes = closes, quotes

    def history(self, s, refresh=False):
        return list(self.closes.get(s, []))

    def save_history(self, s, rows):
        self.closes[s] = rows

    def close_on(self, s, d):
        q = self.quotes.get((s, d))
        return q["close"] if q else None

    def quote_on(self, s, d):
        return self.quotes.get((s, d))


def q(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


def setup(tmp_path, vix=30.0):
    days = [f"d{i:04d}" for i in range(230)]
    a = [50 + i * 0.2 for i in range(228)] + [90.0, 84.0]
    closes = {"AAA": list(zip(days, a)), "SPY": list(zip(days, [100.0] * 230))}
    quotes = {("SPY", days[-1]): q(100, 100, 100, 100), ("^VIX", days[-1]): q(vix, vix, vix, vix)}
    S.cmd_init(tmp_path, days[-1], 1_341_500, 1341.5)  # $1000
    prov = Prov(closes, quotes)
    S.cmd_run(tmp_path, days[-1], prov=prov, symbols=["AAA"])
    return closes, quotes, prov


def test_starts_fully_in_spy_and_signals(tmp_path):
    closes, quotes, prov = setup(tmp_path)
    st = S.load_state(tmp_path)
    assert st["cash_usd"] == pytest.approx(0, abs=1e-6)
    assert st["spy"]["shares"] * 100 == pytest.approx(1000 * (1 - S.COMM), rel=1e-6)
    assert st["pending_buys"][0]["symbol"] == "AAA"
    assert st["pending_buys"][0]["amount"] == pytest.approx(st["spy"]["shares"] * 100 / 2, rel=1e-6)


def test_fill_funds_from_spy_and_unfilled_returns(tmp_path):
    closes, quotes, prov = setup(tmp_path)
    d1 = "d0230"
    quotes[("SPY", d1)] = q(99, 100, 98, 99)
    quotes[("^VIX", d1)] = q(20, 20, 20, 20)
    quotes[("AAA", d1)] = q(85, 86, 82, 83)  # touches 82.32
    closes["AAA"].append((d1, 83.0)); closes["SPY"].append((d1, 99.0))
    S.cmd_run(tmp_path, d1, prov=prov, symbols=["AAA"])
    st = S.load_state(tmp_path)
    assert "AAA" in st["positions"]
    assert st["cash_usd"] == pytest.approx(0, abs=1e-6)  # leftovers parked back in SPY
    assert st["positions"]["AAA"]["cost"] < 500


def test_unfilled_order_goes_back_to_spy(tmp_path):
    closes, quotes, prov = setup(tmp_path)
    d1 = "d0230"
    quotes[("SPY", d1)] = q(99, 100, 98, 99)
    quotes[("^VIX", d1)] = q(20, 20, 20, 20)
    quotes[("AAA", d1)] = q(85, 87, 83, 86)  # no touch
    closes["AAA"].append((d1, 86.0)); closes["SPY"].append((d1, 99.0))
    S.cmd_run(tmp_path, d1, prov=prov, symbols=["AAA"])
    st = S.load_state(tmp_path)
    assert st["positions"] == {}
    sides = [r.split(",")[1:3] for r in (tmp_path / "trades.csv").read_text().splitlines()[1:]]
    assert ["SPY", "SELL"] in sides and sides.count(["SPY", "BUY"]) == 2


def test_tax_estimate():
    st = {"realized": {"2026": 3_000_000}}
    assert S.est_tax(st, "2026") == pytest.approx(500_000 * 0.22)
    assert S.est_tax({"realized": {"2026": 1_000_000}}, "2026") == 0
