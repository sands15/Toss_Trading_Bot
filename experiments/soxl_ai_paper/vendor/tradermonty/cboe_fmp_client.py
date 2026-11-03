"""CBOE 지연 시세로 tradermonty 감지기의 FMPClient 인터페이스를 흉내 낸다 (API 키 불필요).

원본 FMPClient(Financial Modeling Prep)와 같은 메서드 이름과 응답 형식을 돌려준다.
감지기 코드는 고치지 않고, 각 스킬 폴더의 fmp_client.py 가 이 클래스를 FMPClient 로 내보낸다.

차이점(원본 대비):
- 지수(^GSPC 등)는 CBOE 일봉에 거래량이 0이다. ^GSPC 의 거래량은 같은 날 SPY 거래량으로 대신한다.
- ETF 일봉은 하루 늦게 올라올 수 있다. 일봉의 마지막 날짜가 시세(quote)의 거래일보다 이르면
  시세의 시가·고가·저가·종가·거래량으로 그날 행을 덧붙인다.
- 장중에는 마지막 행이 미완성 일봉일 수 있다(지연 15분).
- 국채 금리(get_treasury_rates)는 제공하지 않는다.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from typing import Optional

QUOTE_URL = "https://cdn-api.cboe.com/api/global/delayed_quotes/quotes/{}.json"
HIST_URL = "https://cdn-api.cboe.com/api/global/delayed_quotes/charts/historical/{}.json"
VOLUME_PROXY = {"^GSPC": "SPY", "^IXIC": "QQQ", "^NDX": "QQQ", "^DJI": "DIA", "^RUT": "IWM"}


INDEX_ALIAS = {"^GSPC": "_SPX", "^NDX": "_NDX", "^RUT": "_RUT", "^DJI": "_DJX"}


def cboe_symbol(symbol: str) -> str:
    if symbol in INDEX_ALIAS:
        return INDEX_ALIAS[symbol]
    return "_" + symbol[1:] if symbol.startswith("^") else symbol


def _get_json(url: str, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


class CboeFMPClient:
    def __init__(self, api_key: Optional[str] = None, **_kwargs) -> None:
        self.cache: dict = {}
        self.api_calls_made = 0
        self.rate_limit_reached = False

    # ------------------------------------------------------------------ raw
    def _fetch(self, kind: str, symbol: str) -> Optional[dict]:
        key = (kind, symbol)
        if key in self.cache:
            return self.cache[key]
        url = (QUOTE_URL if kind == "quote" else HIST_URL).format(cboe_symbol(symbol))
        try:
            self.api_calls_made += 1
            data = _get_json(url)
        except Exception as exc:  # noqa: BLE001 - 감지기는 None 을 '데이터 없음'으로 처리한다
            print(f"WARN: CBOE {kind} {symbol} 실패: {exc}", file=sys.stderr)
            data = None
        self.cache[key] = data
        return data

    def _raw_quote(self, symbol: str) -> Optional[dict]:
        data = self._fetch("quote", symbol)
        return data.get("data") if data else None

    def _rows_oldest_first(self, symbol: str) -> list[dict]:
        data = self._fetch("hist", symbol)
        rows = []
        for r in (data or {}).get("data", []) or []:
            try:
                rows.append({
                    "date": r["date"],
                    "open": float(r["open"]),
                    "high": float(r["high"]),
                    "low": float(r["low"]),
                    "close": float(r["close"]),
                    "volume": float(r.get("volume") or 0),
                })
            except (KeyError, TypeError, ValueError):
                continue
        q = self._raw_quote(symbol)
        if q and q.get("last_trade_time") and q.get("close"):
            qdate = q["last_trade_time"][:10]
            if not rows or rows[-1]["date"] < qdate:
                rows.append({"date": qdate, "open": float(q["open"]), "high": float(q["high"]),
                             "low": float(q["low"]), "close": float(q["close"]),
                             "volume": float(q.get("volume") or 0)})
        proxy = VOLUME_PROXY.get(symbol)
        window = rows[-60:]
        if proxy and window and sum(1 for r in window if r["volume"] == 0) >= len(window) // 2:
            vol = {r["date"]: r["volume"] for r in self._rows_oldest_first(proxy)}
            for r in rows:
                r["volume"] = vol.get(r["date"], 0.0)
        return rows

    # ------------------------------------------------------------ FMP shape
    def get_historical_prices(self, symbol: str, days: int = 365) -> Optional[dict]:
        rows = self._rows_oldest_first(symbol)
        if not rows:
            return None
        recent = rows[-days:][::-1]  # most-recent-first (FMP 형식)
        for r in recent:
            r["adjClose"] = r["close"]
        return {"symbol": symbol, "historical": recent}

    def get_quote(self, symbols: str) -> Optional[list[dict]]:
        out = []
        for sym in [s.strip() for s in symbols.split(",") if s.strip()]:
            q = self._raw_quote(sym)
            if not q:
                continue
            rows = self._rows_oldest_first(sym)
            last_year = rows[-252:]
            vols = [r["volume"] for r in rows[-50:] if r["volume"]]
            out.append({
                "symbol": sym,
                "price": float(q.get("current_price") or q.get("close") or 0),
                "open": q.get("open"),
                "dayHigh": q.get("high"),
                "dayLow": q.get("low"),
                "previousClose": q.get("prev_day_close"),
                "change": q.get("price_change"),
                "changesPercentage": q.get("price_change_percent"),
                "volume": q.get("volume"),
                "avgVolume": sum(vols) / len(vols) if vols else None,
                "yearHigh": max(r["high"] for r in last_year) if last_year else None,
                "yearLow": min(r["low"] for r in last_year) if last_year else None,
            })
        return out or None

    def get_batch_quotes(self, symbols: list[str]) -> dict[str, dict]:
        res = {}
        for s in symbols:
            q = self.get_quote(s)
            if q:
                res[s] = q[0]
        return res

    def get_batch_historical(self, symbols: list[str], days: int = 50) -> dict[str, list[dict]]:
        res = {}
        for s in symbols:
            d = self.get_historical_prices(s, days=days)
            if d and d.get("historical"):
                res[s] = d["historical"]
        return res

    @staticmethod
    def calculate_sma(prices: list[float], period: int) -> float:
        if len(prices) < period:
            return sum(prices) / len(prices)
        return sum(prices[:period]) / period

    @staticmethod
    def calculate_ema(prices: list[float], period: int) -> float:
        if len(prices) < period:
            return sum(prices) / len(prices)
        rev = prices[::-1]
        ema = sum(rev[:period]) / period
        k = 2 / (period + 1)
        for p in rev[period:]:
            ema = p * k + ema * (1 - k)
        return ema

    def get_vix_term_structure(self) -> Optional[dict]:
        v, v3 = self.get_quote("^VIX"), self.get_quote("^VIX3M")
        if not v or not v3 or not v3[0]["price"]:
            return None
        ratio = v[0]["price"] / v3[0]["price"]
        cls = ("steep_contango" if ratio < 0.85 else "contango" if ratio < 0.95
               else "flat" if ratio <= 1.05 else "backwardation")
        return {"vix": round(v[0]["price"], 2), "vix3m": round(v3[0]["price"], 2),
                "ratio": round(ratio, 3), "classification": cls}

    def get_treasury_rates(self, days: int = 600) -> Optional[list[dict]]:
        return None

    def get_api_stats(self) -> dict:
        return {"cache_entries": len(self.cache), "api_calls_made": self.api_calls_made,
                "rate_limit_reached": False, "source": "CBOE delayed quotes"}
