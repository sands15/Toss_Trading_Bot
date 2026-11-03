import prices as P


def q(last, stamp, **kw):
    d = {"open": 1, "high": 2, "low": 0.5, "close": 1.5, "last_trade_time": last, "timestamp_utc": stamp}
    d.update(kw)
    return d


def test_session_final_after_close():
    assert P.session_info(q("2026-11-02T15:59:59", "2026-11-03 01:59:09")) == ("2026-11-02", True)


def test_session_not_final_right_after_close():
    # 16:05 ET = 21:05 UTC
    assert P.session_info(q("2026-11-02T15:59:59", "2026-11-02 21:05:00"))[1] is False


def test_session_intraday_not_final():
    assert P.session_info(q("2026-11-02T11:00:00", "2026-11-02 16:15:00"))[1] is False


def test_settle_args_format():
    info = {"session_date": "2026-11-02", "quotes": {
        "SOXL": q("", "", open=79.44, high=81.07, low=67.5, close=68.17),
        "SOXS": q("", "", open=50.38, high=58.53, low=49.31, close=58.09),
        "SOXX": q("", "", open=476.62, close=453.65)}}
    out = P.settle_args(info)
    assert out.startswith("--date 2026-11-02 --open SOXL=79.44,SOXS=50.38,SOXX=476.62")
    assert "--low SOXL=67.5,SOXS=49.31" in out
