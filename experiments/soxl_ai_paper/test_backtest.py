import pytest

import backtest as B


def bar(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


def make(days):
    sigs, px = [], {"SOXL": {}, "SOXS": {}, "SOXX": {}}
    for i, (state, soxl) in enumerate(days):
        d = f"2026-01-{i + 1:02d}"
        sigs.append({"date": d, "ftd_state": state, "ftd_quality": 0, "top_score": 10})
        px["SOXL"][d] = bar(*soxl)
        px["SOXS"][d] = bar(10, 10, 10, 10)
        px["SOXX"][d] = bar(100, 100, 100, 100)
    return sigs, px


def test_signal_executes_next_open_not_same_day():
    # day0 신호 OK → day1 시가 100에 매수. day0 가격 변화는 반영되면 안 됨
    sigs, px = make([("NO_SIGNAL", (50, 200, 50, 200)), ("NO_SIGNAL", (100, 110, 100, 110)),
                     ("CORRECTION", (110, 110, 110, 110))])
    r = B.simulate("FTD 필터", sigs, px, None)
    eq_day1 = r["curve"][0][1]
    assert eq_day1 == pytest.approx(1.1 * (1 - B.COMMISSION) / (1 + B.SLIPPAGE), rel=1e-6)


def test_stop_gap_fills_at_open():
    sigs, px = make([("NO_SIGNAL", (100, 100, 100, 100)), ("NO_SIGNAL", (100, 100, 100, 100)),
                     ("NO_SIGNAL", (80, 85, 75, 84))])
    r = B.simulate("FTD 필터", sigs, px, 0.10)
    # day2 시가 80 (손절가 ~90 아래로 갭) → 80에 체결
    assert r["curve"][-1][1] == pytest.approx(80 / (100 * (1 + B.SLIPPAGE)) * (1 - B.STOP_SLIPPAGE)
                                              * (1 - B.COMMISSION) ** 2, rel=1e-6)


def test_clean_spikes_removes_one_day_glitch():
    rows = [{"date": str(i), "open": 1, "high": 1, "low": 1, "close": c} for i, c in
            enumerate([10, 10.5, 1000, 10.4, 10.6])]
    assert [r["close"] for r in B.clean_spikes(rows)] == [10, 10.5, 10.4, 10.6]


def test_stock_rotation_buys_top_momentum_next_open():
    import stocks_backtest as S
    dates = [f"d{i:03d}" for i in range(140)]
    mk = lambda f: [{"date": d, "open": f(i), "high": f(i), "low": f(i), "close": f(i), "volume": 1}  # noqa: E731
                    for i, d in enumerate(dates)]
    data = {"UP": mk(lambda i: 100 + i), "FLAT": mk(lambda i: 100.0), "DOWN": mk(lambda i: 200 - i)}
    px = S.Prices(data)
    r = S.run_strategy(px, dates, ["UP", "FLAT", "DOWN"], None, top_n=1)
    # 점수 계산이 가능한 첫날(126) 종가에 UP 선택 → 127 시가 매수, 이후 UP 가격을 따라감
    assert r["curve"][-1] == pytest.approx(239 / 227 * (1 - S.COST), rel=1e-6)
