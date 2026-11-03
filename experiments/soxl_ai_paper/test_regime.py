import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "vendor" / "tradermonty"))

import cboe_fmp_client as C  # noqa: E402
import regime as R  # noqa: E402


def fake_get_json(url, timeout=20.0):
    sym = url.rsplit("/", 1)[-1].removesuffix(".json")
    if "/quotes/" in url:
        return {"timestamp": "2026-11-03 01:00:00", "data": {
            "current_price": 110.0, "close": 110.0, "open": 100.0, "high": 111.0, "low": 99.0,
            "prev_day_close": 100.0, "volume": 5000, "last_trade_time": "2026-11-02T15:59:59"}}
    vol = "0.0" if sym.startswith("_") else 1000
    rows = [{"date": f"2026-10-{d:02d}", "open": 100, "high": 105, "low": 95, "close": 100 + d,
             "volume": vol} for d in range(1, 31)]
    return {"data": rows}


def test_client_shapes(monkeypatch):
    monkeypatch.setattr(C, "_get_json", fake_get_json)
    c = C.CboeFMPClient()
    h = c.get_historical_prices("QQQ", days=10)["historical"]
    assert len(h) == 10 and h[0]["date"] == "2026-11-02"  # quote 행이 덧붙고 최신이 앞
    assert h[0]["close"] == 110.0 and h[1]["date"] == "2026-10-30"
    spx = c.get_historical_prices("^GSPC", days=5)["historical"]
    assert spx[1]["volume"] == 1000  # SPY 거래량으로 대체
    q = c.get_quote("^GSPC")[0]
    assert q["price"] == 110.0 and q["yearHigh"] == 111.0
    assert C.cboe_symbol("^GSPC") == "_SPX" and C.cboe_symbol("^VIX3M") == "_VIX3M"


def test_summarize_text():
    ftd = {"market_state": {"combined_state": "CORRECTION"},
           "sp500": {"correction_depth_pct": -3.5}, "nasdaq": {"correction_depth_pct": -10.3},
           "quality_score": {"signal": "No FTD", "total_score": 0, "exposure_range": "0-25%", "guidance": "x"}}
    top = {"composite": {"composite_score": 61.8, "zone": "Red", "risk_budget": "40-55%",
                         "strongest_warning": {"label": "Distribution Day Count"}, "component_scores": {}}}
    text = R.to_text(R.summarize(ftd, top))
    assert "CORRECTION" in text and "61.8" in text and "Distribution Day Count" in text
    assert "실패" in R.to_text(R.summarize(None, None))
