import pytest

import martingale_ledger as M


class FakeProv:
    def __init__(self, series):
        self.series = series  # sym -> list[(date, close)]

    def history(self, sym, refresh=False):
        return list(self.series.get(sym, []))

    def save_history(self, sym, rows):
        self.series[sym] = list(rows)

    def close_on(self, sym, date):
        return None


def flat(n, price, start=0):
    return [(f"d{start + i:04d}", float(price)) for i in range(n)]


def make(prices_a, spy_up=True):
    base = flat(220, 100)
    spy = flat(220, 100) + [(f"d{220 + i:04d}", 101.0 if spy_up else 50.0) for i in range(len(prices_a))]
    a = base + [(f"d{220 + i:04d}", float(p)) for i, p in enumerate(prices_a)]
    return FakeProv({"SPY": spy, "AAA": a})


def test_start_add_and_take_profit(tmp_path):
    prov = make([101, 95.9, 91.0, 96.0])
    M.cmd_init(tmp_path, "d0220", 1_341_500, 1341.5)  # $1000
    M.cmd_run(tmp_path, "d0220", prov=prov, symbols=["AAA"])
    st = M.load_state(tmp_path)
    assert st["positions"]["AAA"]["cost"] == pytest.approx(1.0)  # 0.1% of $1000
    M.cmd_run(tmp_path, "d0221", prov=prov, symbols=["AAA"])   # 95.9 <= 101*0.95 → add $2 (trend: 95.9 < MA≈100 → blocked)
    st = M.load_state(tmp_path)
    assert st["positions"]["AAA"]["cost"] == pytest.approx(1.0)  # below own 200DMA → no add
    assert st["positions"]["AAA"]["last_buy"] == 95.9


def test_take_profit_closes_cycle(tmp_path):
    prov = make([101, 104])
    M.cmd_init(tmp_path, "d0220", 1_341_500, 1341.5)
    M.cmd_run(tmp_path, "d0220", prov=prov, symbols=["AAA"])
    M.cmd_run(tmp_path, "d0221", prov=prov, symbols=["AAA"])   # 104 >= avg*1.02 → sell, then restart same day
    rows = (tmp_path / "trades.csv").read_text().splitlines()
    assert any(",SELL," in r for r in rows)
    st = M.load_state(tmp_path)
    assert st["positions"]["AAA"]["opened"] == "d0221"


def test_market_filter_blocks_new_cycles(tmp_path):
    prov = make([101], spy_up=False)
    M.cmd_init(tmp_path, "d0220", 1_341_500, 1341.5)
    M.cmd_run(tmp_path, "d0220", prov=prov, symbols=["AAA"])
    assert M.load_state(tmp_path)["positions"] == {}


def test_add_when_trend_ok(tmp_path):
    # rising history so 200DMA is low; drop 5% still above MA → add with doubling
    base = [(f"d{i:04d}", 50.0 + i * 0.1) for i in range(220)]  # last ≈ 71.9
    a = base + [("d0220", 80.0), ("d0221", 75.9)]
    spy = flat(222, 100)
    prov = FakeProv({"SPY": spy, "AAA": a})
    M.cmd_init(tmp_path, "d0220", 1_341_500, 1341.5)
    M.cmd_run(tmp_path, "d0220", prov=prov, symbols=["AAA"])
    M.cmd_run(tmp_path, "d0221", prov=prov, symbols=["AAA"])
    p = M.load_state(tmp_path)["positions"]["AAA"]
    assert p["cost"] == pytest.approx(3.0) and p["next_amt"] == pytest.approx(4.0)


def test_rejects_old_or_missing_spy(tmp_path):
    prov = make([101])
    M.cmd_init(tmp_path, "d0220", 1_341_500, 1341.5)
    M.cmd_run(tmp_path, "d0220", prov=prov, symbols=["AAA"])
    with pytest.raises(M.MartError):
        M.cmd_run(tmp_path, "d0220", prov=prov, symbols=["AAA"])
    with pytest.raises(M.MartError):
        M.cmd_run(tmp_path, "d0999", prov=prov, symbols=["AAA"])
