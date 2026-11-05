import pytest

import panic_ledger as P


class Prov:
    def __init__(self, closes, quotes):
        self.closes, self.quotes = closes, quotes  # closes: sym -> [(d,c)], quotes: (sym,d) -> ohlc

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


def build(vix0=30.0):
    days = [f"d{i:04d}" for i in range(230)]
    # AAA: long uptrend, then two sharp down days → RSI2 very low, still above 200DMA
    a = [50 + i * 0.2 for i in range(228)] + [90.0, 84.0]
    spy = [100.0] * 230
    closes = {"AAA": list(zip(days, a)), "SPY": list(zip(days, spy))}
    quotes = {("^VIX", days[-1]): q(vix0, vix0, vix0, vix0)}
    return days, closes, quotes


def test_signal_then_limit_fill_then_exit(tmp_path):
    days, closes, quotes = build()
    P.cmd_init(tmp_path, days[-1], 1_341_500, 1341.5)  # $1000
    prov = Prov(closes, quotes)
    P.cmd_run(tmp_path, days[-1], prov=prov, symbols=["AAA"])
    st = P.load_state(tmp_path)
    assert st["pending_buys"][0]["symbol"] == "AAA"
    assert st["pending_buys"][0]["limit"] == pytest.approx(84 * 0.98)
    # next day: low touches limit → filled at limit (open above)
    d1 = "d0230"
    closes["AAA"].append((d1, 83.0)); closes["SPY"].append((d1, 100.0))
    quotes[("AAA", d1)] = q(85.0, 86.0, 82.0, 83.0)
    quotes[("^VIX", d1)] = q(20, 20, 20, 20)
    P.cmd_run(tmp_path, d1, prov=prov, symbols=["AAA"])
    st = P.load_state(tmp_path)
    assert st["positions"]["AAA"]["entry"] == pytest.approx(84 * 0.98)
    assert st["positions"]["AAA"]["sell_limit"] is None  # 83 < sma5 → no sell yet
    # day 2: close above sma5 → sell limit set at sma5
    d2 = "d0231"
    closes["AAA"].append((d2, 95.0)); closes["SPY"].append((d2, 100.0))
    quotes[("AAA", d2)] = q(84.0, 96.0, 83.5, 95.0)
    quotes[("^VIX", d2)] = q(20, 20, 20, 20)
    P.cmd_run(tmp_path, d2, prov=prov, symbols=["AAA"])
    lim = P.load_state(tmp_path)["positions"]["AAA"]["sell_limit"]
    assert lim == pytest.approx(sum(c for _, c in closes["AAA"][-5:]) / 5, rel=1e-4)
    # day 3: high reaches limit → sold
    d3 = "d0232"
    closes["AAA"].append((d3, 96.0)); closes["SPY"].append((d3, 100.0))
    quotes[("AAA", d3)] = q(94.0, 97.0, 93.0, 96.0)
    quotes[("^VIX", d3)] = q(20, 20, 20, 20)
    P.cmd_run(tmp_path, d3, prov=prov, symbols=["AAA"])
    st = P.load_state(tmp_path)
    assert st["positions"] == {}
    assert ",SELL," in (tmp_path / "trades.csv").read_text()


def test_no_signal_when_vix_low(tmp_path):
    days, closes, quotes = build(vix0=18.0)
    P.cmd_init(tmp_path, days[-1], 1_341_500, 1341.5)
    P.cmd_run(tmp_path, days[-1], prov=Prov(closes, quotes), symbols=["AAA"])
    assert P.load_state(tmp_path)["pending_buys"] == []


def test_unfilled_limit_is_cancelled(tmp_path):
    days, closes, quotes = build()
    P.cmd_init(tmp_path, days[-1], 1_341_500, 1341.5)
    prov = Prov(closes, quotes)
    P.cmd_run(tmp_path, days[-1], prov=prov, symbols=["AAA"])
    d1 = "d0230"
    closes["AAA"].append((d1, 86.0)); closes["SPY"].append((d1, 100.0))
    quotes[("AAA", d1)] = q(85.0, 87.0, 83.0, 86.0)  # low 83 > limit 82.32
    quotes[("^VIX", d1)] = q(20, 20, 20, 20)
    P.cmd_run(tmp_path, d1, prov=prov, symbols=["AAA"])
    st = P.load_state(tmp_path)
    assert st["positions"] == {} and st["pending_buys"] == []


def _hold_setup(tmp_path):
    days, closes, quotes = build()
    P.cmd_init(tmp_path, days[-1], 1_341_500, 1341.5)
    prov = Prov(closes, quotes)
    P.cmd_run(tmp_path, days[-1], prov=prov, symbols=["AAA"])
    d1 = "d0230"
    closes["AAA"].append((d1, 80.0)); closes["SPY"].append((d1, 100.0))
    quotes[("AAA", d1)] = q(85.0, 85.0, 79.0, 80.0)
    quotes[("^VIX", d1)] = q(20, 20, 20, 20)
    P.cmd_run(tmp_path, d1, prov=prov, symbols=["AAA"])
    return closes, quotes, prov


def test_stop_loss(tmp_path):
    closes, quotes, prov = _hold_setup(tmp_path)  # entry 82.32
    d2 = "d0231"
    closes["AAA"].append((d2, 72.0)); closes["SPY"].append((d2, 100.0))
    quotes[("AAA", d2)] = q(78.0, 78.0, 70.0, 72.0)  # low below 74.09 stop
    quotes[("^VIX", d2)] = q(20, 20, 20, 20)
    P.cmd_run(tmp_path, d2, prov=prov, symbols=["AAA"])
    assert P.load_state(tmp_path)["positions"] == {}
    assert "stop" in (tmp_path / "trades.csv").read_text()


def test_max_hold_exit(tmp_path):
    closes, quotes, prov = _hold_setup(tmp_path)
    price = 80.0
    for i in range(11):
        d = f"d{231 + i:04d}"
        price -= 0.1  # slow drift down, never above sma5, never hits stop
        closes["AAA"].append((d, price)); closes["SPY"].append((d, 100.0))
        quotes[("AAA", d)] = q(price + 0.05, price + 0.06, price - 0.05, price)
        quotes[("^VIX", d)] = q(20, 20, 20, 20)
        P.cmd_run(tmp_path, d, prov=prov, symbols=["AAA"])
    assert P.load_state(tmp_path)["positions"] == {}
    assert "max_hold" in (tmp_path / "trades.csv").read_text()
