"""Paper portfolio in KRW. Fractional US shares, Toss-like cost model."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from . import prices

START_KRW = 5_000_000
COMMISSION = 0.001   # 토스증권 미국주식 수수료 0.1%
FX_SPREAD = 0.0025   # 환전 스프레드 가정 0.25% (편도)
BENCHMARK = "SPY"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Portfolio:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / "data" / "portfolio.json"
        self.state = json.loads(self.path.read_text()) if self.path.exists() else None

    # ---- lifecycle -------------------------------------------------
    def init(self) -> None:
        if self.state is not None:
            raise SystemExit("portfolio already initialised")
        fx = prices.usdkrw()
        spy = prices.quote_price(BENCHMARK)
        usd = START_KRW / (fx * (1 + FX_SPREAD))
        bench_shares = usd * (1 - COMMISSION) / spy
        self.state = {
            "inception": _now(),
            "start_krw": START_KRW,
            "cash_krw": START_KRW,
            "holdings": {},
            "benchmark": {"symbol": BENCHMARK, "shares": bench_shares, "fx": fx, "price": spy},
        }
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state, indent=1, ensure_ascii=False))

    # ---- trading ----------------------------------------------------
    def _log(self, row: dict) -> None:
        f = self.root / "data" / "trades.csv"
        new = not f.exists()
        with f.open("a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(row))
            if new:
                w.writeheader()
            w.writerow(row)

    def buy(self, symbol: str, krw: float, reason: str) -> dict:
        symbol = symbol.upper()
        krw = min(krw, self.state["cash_krw"])
        if krw < 10_000:
            raise SystemExit(f"not enough cash ({self.state['cash_krw']:.0f} KRW)")
        fx, px = prices.usdkrw(), prices.quote_price(symbol)
        usd = krw / (fx * (1 + FX_SPREAD))
        shares = usd * (1 - COMMISSION) / px
        h = self.state["holdings"].setdefault(symbol, {"shares": 0.0, "cost_krw": 0.0, "thesis": reason})
        h["shares"] += shares
        h["cost_krw"] += krw
        self.state["cash_krw"] -= krw
        self.save()
        row = {"time": _now(), "side": "BUY", "symbol": symbol, "shares": round(shares, 6), "price_usd": px, "usdkrw": fx, "krw": round(krw), "reason": reason}
        self._log(row)
        return row

    def sell(self, symbol: str, fraction: float, reason: str) -> dict:
        symbol = symbol.upper()
        h = self.state["holdings"].get(symbol)
        if not h:
            raise SystemExit(f"no position in {symbol}")
        fraction = max(0.0, min(1.0, fraction))
        fx, px = prices.usdkrw(), prices.quote_price(symbol)
        shares = h["shares"] * fraction
        krw = shares * px * (1 - COMMISSION) * fx * (1 - FX_SPREAD)
        cost_part = h["cost_krw"] * fraction
        h["shares"] -= shares
        h["cost_krw"] -= cost_part
        if fraction >= 0.999:
            del self.state["holdings"][symbol]
        self.state["cash_krw"] += krw
        self.save()
        row = {"time": _now(), "side": "SELL", "symbol": symbol, "shares": round(shares, 6), "price_usd": px, "usdkrw": fx, "krw": round(krw), "reason": f"{reason} (실현손익 {krw - cost_part:+,.0f}원)"}
        self._log(row)
        return row

    # ---- valuation ----------------------------------------------------
    def valuation(self) -> dict:
        fx = prices.usdkrw()
        rows, total = [], self.state["cash_krw"]
        for sym, h in self.state["holdings"].items():
            px = prices.quote_price(sym)
            val = h["shares"] * px * fx
            total += val
            rows.append({"symbol": sym, "shares": h["shares"], "price_usd": px, "value_krw": val, "cost_krw": h["cost_krw"], "pnl_pct": (val / h["cost_krw"] - 1) * 100 if h["cost_krw"] else 0.0})
        b = self.state["benchmark"]
        bench = b["shares"] * prices.quote_price(b["symbol"]) * fx
        start = self.state["start_krw"]
        return {
            "time": _now(), "usdkrw": fx, "cash_krw": self.state["cash_krw"], "total_krw": total,
            "return_pct": (total / start - 1) * 100, "bench_krw": bench, "bench_return_pct": (bench / start - 1) * 100,
            "positions": sorted(rows, key=lambda r: -r["value_krw"]),
        }

    def record_nav(self, v: dict) -> None:
        f = self.root / "data" / "nav.csv"
        new = not f.exists()
        with f.open("a", newline="") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["time", "total_krw", "return_pct", "spy_krw", "spy_return_pct", "usdkrw"])
            w.writerow([v["time"], round(v["total_krw"]), round(v["return_pct"], 3), round(v["bench_krw"]), round(v["bench_return_pct"], 3), v["usdkrw"]])


def render(v: dict) -> str:
    lines = [
        f"## 포트폴리오 ({v['time'][:16]} UTC, USD/KRW {v['usdkrw']:,.2f})", "",
        f"- 평가금액: **{v['total_krw']:,.0f}원** ({v['return_pct']:+.2f}%)",
        f"- 벤치마크(SPY 전액 매수): {v['bench_krw']:,.0f}원 ({v['bench_return_pct']:+.2f}%)",
        f"- 초과수익: **{v['return_pct'] - v['bench_return_pct']:+.2f}%p**",
        f"- 현금: {v['cash_krw']:,.0f}원 ({v['cash_krw'] / v['total_krw'] * 100:.1f}%)", "",
        "| 종목 | 수량 | 현재가($) | 평가액(원) | 비중 | 손익 |", "|---|---:|---:|---:|---:|---:|",
    ]
    for r in v["positions"]:
        lines.append(f"| {r['symbol']} | {r['shares']:.4f} | {r['price_usd']:,.2f} | {r['value_krw']:,.0f} | {r['value_krw'] / v['total_krw'] * 100:.1f}% | {r['pnl_pct']:+.2f}% |")
    return "\n".join(lines) + "\n"
