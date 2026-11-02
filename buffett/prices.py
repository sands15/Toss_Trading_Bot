from __future__ import annotations

from urllib.parse import quote

from .net import get_json

CHART = "https://query2.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval=1d"


def quote_price(symbol: str) -> float:
    data = get_json(CHART.format(sym=quote(symbol), rng="5d"))
    meta = data["chart"]["result"][0]["meta"]
    price = meta.get("regularMarketPrice")
    if not price:
        raise RuntimeError(f"no price for {symbol}")
    return float(price)


def usdkrw() -> float:
    return quote_price("KRW=X")


def history(symbol: str, rng: str = "1y") -> list[tuple[int, float]]:
    data = get_json(CHART.format(sym=quote(symbol), rng=rng))
    res = data["chart"]["result"][0]
    closes = res["indicators"]["quote"][0]["close"]
    return [(t, c) for t, c in zip(res["timestamp"], closes) if c is not None]
