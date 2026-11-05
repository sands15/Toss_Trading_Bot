"""S&P 500 종목별 추세 조건 마틴게일 가상 장부 (규칙 기반, AI 판단 없음, paper only).

규칙 (scratchpad 백테스트 "종목+시장(SPY) 200일선"과 같음)
- 종목마다 독립 사이클: 계좌 평가액의 BASE(0.1%)로 시작 → 직전 매수가 대비 DROP(5%) 하락마다
  직전 매수액의 MULT(2)배 추가 매수 → 평균 단가 대비 +TP(2%) 이상이면 전량 매도하고 사이클 종료.
- 추세 조건: 그 종목 종가 ≥ 자기 200일 이동평균 **그리고** SPY 종가 ≥ SPY 200일 이동평균일 때만
  새 사이클 시작과 추가 매수를 한다. 조건이 꺼져도 보유분은 익절 조건이 될 때까지 들고 간다.
- 체결은 그날 종가, 비용 매매마다 0.15%(수수료 0.1% + 슬리피지 0.05%). 세금·환전 미반영.
- 대상: universe/sp500_2026-08-18.txt 중 200일 이상의 CBOE 일봉이 있는 종목.

가격 처리
- 처음 실행 때 종목별 CBOE 일봉으로 200일 이동평균용 종가 기록을 만든다(캐시: ~/.cache/soxl_paper_mart).
- 매일은 종목별 CBOE 지연 시세 1건씩 받아 그날 종가를 붙인다. 기록에 하루 이상 빈 날이 있으면 그 종목은
  일봉을 다시 받는다. 시세의 거래일이 정산일과 다르거나 종가가 확정 전이면 그 종목은 그날 건너뛴다.

사용:
  python3 martingale_ledger.py init --start 2026-11-05 --fx 1341.5
  python3 martingale_ledger.py run --date 2026-11-05 [--fx 1345.0]
  python3 martingale_ledger.py status
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "vendor" / "tradermonty"))

DATA = HERE / "records" / "martingale"
UNIVERSE = HERE / "universe" / "sp500_2026-08-18.txt"
CACHE = Path(os.path.expanduser("~/.cache/soxl_paper_mart"))
BASE, DROP, MULT, TP, MA = 0.001, 0.05, 2.0, 0.02, 200
COST = 0.0015
MARKET = "SPY"
DAILY_FIELDS = ["date", "symbols_priced", "positions", "invested_pct", "cash_usd", "equity_usd", "fx", "equity_krw",
                "return_pct", "spy_hold_usd", "cycles_closed", "realized_usd", "buys", "market_ok"]
TRADE_FIELDS = ["date", "symbol", "side", "amount_usd", "price", "shares", "reason"]


class MartError(Exception):
    pass


# ----------------------------------------------------------------------------- io
def load_state(d: Path) -> dict:
    f = d / "state.json"
    if not f.exists():
        raise MartError("마틴게일 장부가 없습니다. 먼저 init을 실행하세요.")
    return json.loads(f.read_text(encoding="utf-8"))


def save_state(d: Path, st: dict) -> None:
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "state.tmp"
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(d / "state.json")


def append(path: Path, fields: list[str], row: dict) -> None:
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerow(row)


def universe() -> list[str]:
    return [x.strip() for x in UNIVERSE.read_text().splitlines() if x.strip() and not x.startswith("#")]


# -------------------------------------------------------------------------- prices
class CboePrices:
    """종목별 종가 기록(오래된 순)과 정산일 종가를 제공."""

    def __init__(self, cache: Path = CACHE, sleep: float = 0.15):
        from cboe_fmp_client import HIST_URL, QUOTE_URL, _get_json, cboe_symbol
        self._hist_url, self._quote_url, self._get, self._sym = HIST_URL, QUOTE_URL, _get_json, cboe_symbol
        self.cache, self.sleep = cache, sleep
        cache.mkdir(parents=True, exist_ok=True)

    def _fetch(self, url: str):
        for attempt in range(4):
            try:
                return self._get(url)
            except Exception as exc:  # noqa: BLE001
                if "429" in str(exc) and attempt < 3:
                    time.sleep(5 * (attempt + 1))
                    continue
                return None
            finally:
                time.sleep(self.sleep)

    def history(self, sym: str, refresh: bool = False) -> list[tuple[str, float]]:
        f = self.cache / f"{sym.replace('/', '_')}.json"
        if f.exists() and not refresh:
            return [tuple(x) for x in json.loads(f.read_text())]
        raw = self._fetch(self._hist_url.format(self._sym(sym))) or {}
        rows = [(r["date"], float(r["close"])) for r in raw.get("data", []) if r.get("close")]
        rows = rows[-(MA + 30):]
        f.write_text(json.dumps(rows))
        return rows

    def save_history(self, sym: str, rows: list[tuple[str, float]]) -> None:
        (self.cache / f"{sym.replace('/', '_')}.json").write_text(json.dumps(rows[-(MA + 30):]))

    def quote_on(self, sym: str, date: str) -> dict | None:
        """정산일의 확정 시가·고가·저가·종가. 같은 날 다시 부르면 캐시에서 읽는다(다른 장부와 공유)."""
        from prices import session_info
        qdir = self.cache / "quotes" / date
        f = qdir / f"{sym.replace('/', '_').replace('^', '_')}.json"
        if f.exists():
            return json.loads(f.read_text())
        raw = self._fetch(self._quote_url.format(self._sym(sym)))
        if not raw or not raw.get("data") or not raw["data"].get("last_trade_time"):
            return None
        q = raw["data"]
        qd, final = session_info({"last_trade_time": q["last_trade_time"], "timestamp_utc": raw.get("timestamp")})
        if qd != date or not final or not q.get("close"):
            return None
        out = {k: float(q[k]) for k in ("open", "high", "low", "close")}
        qdir.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(out))
        return out

    def close_on(self, sym: str, date: str) -> float | None:
        q = self.quote_on(sym, date)
        return q["close"] if q else None


def series_for(prov, sym: str, date: str, prev_date: str | None) -> list[tuple[str, float]] | None:
    """date 종가까지 포함한 종가 기록. 없으면 None."""
    hist = prov.history(sym)
    if hist and hist[-1][0] >= date:
        return [x for x in hist if x[0] <= date]
    if hist and prev_date and hist[-1][0] < prev_date:
        hist = prov.history(sym, refresh=True)  # 빈 날이 있으면 일봉 다시 받기
        if hist and hist[-1][0] >= date:
            return [x for x in hist if x[0] <= date]
    c = prov.close_on(sym, date)
    if c is None:
        return None
    hist = [x for x in hist if x[0] < date] + [(date, c)]
    prov.save_history(sym, hist)
    return hist


def above_ma(series: list[tuple[str, float]]) -> bool | None:
    if len(series) < MA:
        return None
    return series[-1][1] >= sum(x[1] for x in series[-MA:]) / MA


# ------------------------------------------------------------------------ commands
def cmd_init(d: Path, start: str, krw: float, fx: float) -> dict:
    if (d / "state.json").exists():
        raise MartError("이미 마틴게일 장부가 있습니다.")
    cap = round(krw / fx, 2)
    st = {"start": start, "start_krw": krw, "start_fx": fx, "capital_usd": cap, "cash_usd": cap,
          "positions": {}, "last_date": None, "last_fx": fx, "spy_start": None,
          "params": {"base": BASE, "drop": DROP, "mult": MULT, "tp": TP, "ma": MA, "cost": COST}}
    save_state(d, st)
    return st


def cmd_run(d: Path, date: str, fx: float | None = None, prov=None, symbols: list[str] | None = None) -> str:
    st = load_state(d)
    if date < st["start"]:
        raise MartError(f"시작일({st['start']}) 이전 날짜입니다.")
    if st["last_date"] and date <= st["last_date"]:
        raise MartError(f"{date}은(는) 이미 처리했거나 이전 날짜입니다.")
    prov = prov or CboePrices()
    fx = fx or st["last_fx"]
    prev = st["last_date"]
    spy = series_for(prov, MARKET, date, prev)
    if not spy or spy[-1][0] != date:
        raise MartError(f"{MARKET} {date} 종가를 확인하지 못했습니다(종가 미확정이거나 휴장).")
    market_ok = bool(above_ma(spy))
    if st["spy_start"] is None:
        st["spy_start"] = spy[-1][1]

    pos = st["positions"]
    syms = symbols or universe()
    closes: dict[str, float] = {}
    trend: dict[str, bool] = {}
    for s in sorted(set(syms) | set(pos)):
        ser = series_for(prov, s, date, prev)
        if not ser or ser[-1][0] != date:
            continue
        closes[s] = ser[-1][1]
        trend[s] = bool(above_ma(ser)) and market_ok

    # 평가액(그날 종가 기준, 시작 전) — 새 사이클 크기 계산용
    equity0 = st["cash_usd"] + sum(p["shares"] * closes.get(s, p["last_price"]) for s, p in pos.items())
    cycles, realized, buys = 0, 0.0, 0

    for s in sorted(pos):
        if s not in closes:
            continue
        p, c = pos[s], closes[s]
        p["last_price"] = c
        if c >= p["cost"] / p["shares"] * (1 + TP):
            proceeds = p["shares"] * c * (1 - COST)
            st["cash_usd"] += proceeds
            realized += proceeds - p["cost"]
            cycles += 1
            append(d / "trades.csv", TRADE_FIELDS, {"date": date, "symbol": s, "side": "SELL", "amount_usd": round(proceeds, 2),
                   "price": c, "shares": round(p["shares"], 6), "reason": "take_profit"})
            del pos[s]
            continue
        if c <= p["last_buy"] * (1 - DROP):
            if trend.get(s):
                amt = min(p["next_amt"], st["cash_usd"])
                if amt > 0.01:
                    p["shares"] += amt * (1 - COST) / c
                    p["cost"] += amt
                    st["cash_usd"] -= amt
                    p["next_amt"] *= MULT
                    buys += 1
                    append(d / "trades.csv", TRADE_FIELDS, {"date": date, "symbol": s, "side": "BUY", "amount_usd": round(amt, 2),
                           "price": c, "shares": round(amt * (1 - COST) / c, 6), "reason": "add"})
            p["last_buy"] = c

    for s in sorted(syms):
        if s in pos or s not in closes or not trend.get(s):
            continue
        amt = min(BASE * equity0, st["cash_usd"])
        if amt <= 0.01:
            break
        c = closes[s]
        pos[s] = {"shares": amt * (1 - COST) / c, "cost": amt, "next_amt": amt * MULT, "last_buy": c, "last_price": c,
                  "opened": date}
        st["cash_usd"] -= amt
        buys += 1
        append(d / "trades.csv", TRADE_FIELDS, {"date": date, "symbol": s, "side": "BUY", "amount_usd": round(amt, 2),
               "price": c, "shares": round(amt * (1 - COST) / c, 6), "reason": "start"})

    held = sum(p["shares"] * closes.get(s, p["last_price"]) for s, p in pos.items())
    eq = st["cash_usd"] + held
    cap = st["capital_usd"]
    append(d / "daily.csv", DAILY_FIELDS, {
        "date": date, "symbols_priced": len(closes), "positions": len(pos),
        "invested_pct": f"{held / eq * 100:.1f}", "cash_usd": round(st["cash_usd"], 2), "equity_usd": round(eq, 2),
        "fx": fx, "equity_krw": round(eq * fx), "return_pct": f"{(eq / cap - 1) * 100:.2f}",
        "spy_hold_usd": round(cap * (1 - COST) * spy[-1][1] / st["spy_start"], 2),
        "cycles_closed": cycles, "realized_usd": round(realized, 2), "buys": buys, "market_ok": market_ok})
    st["last_date"], st["last_fx"] = date, fx
    save_state(d, st)
    return (f"{date}: 시세 {len(closes)}종목, 익절 {cycles}회(${realized:.2f}), 매수 {buys}건, 보유 {len(pos)}종목, "
            f"주식 비중 {held / eq:.0%}, 시장 조건 {'ON' if market_ok else 'OFF'}")


def cmd_status(d: Path) -> str:
    st = load_state(d)
    lines = [f"추세 조건 마틴게일: 시작 {st['start']} {int(st['start_krw']):,}원 = ${st['capital_usd']}",
             f"마지막 처리: {st['last_date'] or '-'}  보유 {len(st['positions'])}종목, 현금 ${st['cash_usd']:.2f}"]
    f = d / "daily.csv"
    if f.exists():
        rows = list(csv.DictReader(f.open(encoding="utf-8")))
        if rows:
            r = rows[-1]
            lines.append(f"평가액 ${r['equity_usd']} ({int(float(r['equity_krw'])):,}원, {r['return_pct']}%), 주식 비중 "
                         f"{r['invested_pct']}% | SPY 보유 ${r['spy_hold_usd']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-dir", default=str(DATA))
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("init")
    a.add_argument("--start", required=True)
    a.add_argument("--krw", type=float, default=10_000_000)
    a.add_argument("--fx", type=float, required=True)
    a = sub.add_parser("run")
    a.add_argument("--date", required=True)
    a.add_argument("--fx", type=float)
    sub.add_parser("status")
    args = p.parse_args(argv)
    d = Path(args.data_dir)
    try:
        if args.cmd == "init":
            cmd_init(d, args.start, args.krw, args.fx)
        elif args.cmd == "run":
            print(cmd_run(d, args.date, args.fx))
        print(cmd_status(d))
    except MartError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
