"""SPY + 공포장 반등 규칙봇 (가상 장부, AI 판단 없음, 실제 주문 없음).

평소에는 전액 SPY를 들고 있다가, 공포장 반등 신호가 오면 SPY를 팔아 급락 종목을 사고,
나오면 다시 SPY로 돌아간다. 반등 규칙은 panic_ledger.py 와 같고 동시 2종목(평가액의 1/2씩)이다.
scratchpad 백테스트(동시 3종목, 2015~2026): 연 +19.8%, 최대낙폭 −30% (SPY 연 +13.3%, −34%). 세금 미반영.

정산일 D 장 마감 후 처리
1) 어제 신호의 매수 주문: D 시가에 주문 금액만큼 SPY 매도(수수료 0.1% + 체결 손해 0.05%)
   → D 저가 ≤ 지정가면 min(D 시가, 지정가)에 체결. 안 닿으면 그 돈으로 D 종가에 SPY 재매수
2) 보유 종목 매도: 손절(매수가 −10%) → 보유 10거래일 초과 시가 매도 → 5일선 지정가.
   판 돈은 D 종가에 SPY 재매수
3) 보유 종목 종가 > 5일선이면 다음 날 5일선 지정가 매도, 10거래일 지나면 다음 날 시가 매도 예약
4) VIX 종가 ≥ 25 이고 빈자리가 있으면 S&P 500 중 종가 ≥ $5, 200일선 위, RSI(2) < 5 종목을
   RSI 낮은 순으로 → 다음 날 종가 × 0.98 매수 지정가 (1종목 = 평가액 / 2)
5) 세금 추정: 매도할 때 실현 손익(SPY는 평균단가)을 연도별로 더해 원화로 환산,
   250만원 초과분의 22%를 '추정 양도세'로 표시(실제 신고와 다를 수 있음)

가격은 martingale_ledger 의 CBOE 캐시를 같이 쓴다(정산 5·6단계 다음에 실행).

사용:
  python3 spy_rebound_ledger.py init --start 2026-10-01 --fx 1341.5
  python3 spy_rebound_ledger.py run --date 2026-11-05 [--fx 1345]
  python3 spy_rebound_ledger.py status
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import martingale_ledger as ML
from panic_ledger import ENTRY_DISC, MAX_HOLD, MIN_PRICE, RSI_MAX, STOP_PCT, VIX_MIN, indicators

HERE = Path(__file__).resolve().parent
DATA = HERE / "records" / "spy_rebound"
SLOTS = 2
COMM, SLIP = 0.001, 0.0005
TAX_RATE, TAX_FREE_KRW = 0.22, 2_500_000
DAILY_FIELDS = ["date", "vix", "spy_close", "spy_shares", "positions", "cash_usd", "equity_usd", "fx", "equity_krw",
                "return_pct", "spy_hold_usd", "realized_ytd_krw", "est_tax_ytd_krw", "events"]
TRADE_FIELDS = ["date", "symbol", "side", "price", "shares", "amount_usd", "realized_usd", "reason"]


class SRError(Exception):
    pass


def load_state(d: Path) -> dict:
    f = d / "state.json"
    if not f.exists():
        raise SRError("SPY+반등 장부가 없습니다. 먼저 init을 실행하세요.")
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


def cmd_init(d: Path, start: str, krw: float, fx: float) -> dict:
    if (d / "state.json").exists():
        raise SRError("이미 SPY+반등 장부가 있습니다.")
    cap = round(krw / fx, 2)
    st = {"start": start, "start_krw": krw, "start_fx": fx, "capital_usd": cap, "cash_usd": cap,
          "spy": {"shares": 0.0, "cost": 0.0}, "positions": {}, "pending_buys": [], "last_date": None,
          "last_fx": fx, "spy_start": None, "realized": {},
          "params": {"slots": SLOTS, "rsi_max": RSI_MAX, "vix_min": VIX_MIN, "entry_disc": ENTRY_DISC,
                     "max_hold": MAX_HOLD, "stop_pct": STOP_PCT, "comm": COMM, "slip": SLIP}}
    save_state(d, st)
    return st


def _book(st: dict, d: Path, date: str, fx: float, sym: str, side: str, px: float, sh: float, amt: float,
          realized: float, reason: str) -> None:
    yr = date[:4]
    st["realized"][yr] = st["realized"].get(yr, 0.0) + realized * fx
    append(d / "trades.csv", TRADE_FIELDS, {"date": date, "symbol": sym, "side": side, "price": round(px, 4),
           "shares": round(sh, 6), "amount_usd": round(amt, 2), "realized_usd": round(realized, 2), "reason": reason})


def spy_buy(st, d, date, fx, px, usd, reason):
    if usd <= 0.01:
        return
    sh = usd * (1 - COMM) / px
    st["spy"]["shares"] += sh
    st["spy"]["cost"] += usd
    st["cash_usd"] -= usd
    _book(st, d, date, fx, "SPY", "BUY", px, sh, usd, 0.0, reason)


def spy_sell(st, d, date, fx, px, usd, reason) -> float:
    """usd 만큼(세후 수수료 전 금액 기준) SPY 매도, 받은 현금 반환."""
    s = st["spy"]
    if s["shares"] <= 0:
        return 0.0
    sh = min(s["shares"], usd / px)
    avg = s["cost"] / s["shares"]
    proceeds = sh * px * (1 - COMM)
    realized = proceeds - avg * sh
    s["shares"] -= sh
    s["cost"] -= avg * sh
    st["cash_usd"] += proceeds
    _book(st, d, date, fx, "SPY", "SELL", px, sh, proceeds, realized, reason)
    return proceeds


def est_tax(st: dict, year: str) -> float:
    return max(0.0, st["realized"].get(year, 0.0) - TAX_FREE_KRW) * TAX_RATE


def cmd_run(d: Path, date: str, fx: float | None = None, prov=None, symbols: list[str] | None = None) -> str:
    st = load_state(d)
    if date < st["start"]:
        raise SRError(f"시작일({st['start']}) 이전 날짜입니다.")
    if st["last_date"] and date <= st["last_date"]:
        raise SRError(f"{date}은(는) 이미 처리했거나 이전 날짜입니다.")
    prov = prov or ML.CboePrices()
    fx = fx or st["last_fx"]
    prev = st["last_date"]
    spyq = prov.quote_on("SPY", date)
    vixq = prov.quote_on("^VIX", date)
    if not spyq or not vixq:
        raise SRError(f"{date} SPY 또는 VIX 확정 시세를 확인하지 못했습니다.")
    if st["spy_start"] is None:
        st["spy_start"] = spyq["close"]
        spy_buy(st, d, date, fx, spyq["close"], st["cash_usd"], "start")
    events: list[str] = []
    pos = st["positions"]

    # 1) 매수 주문: 시가에 SPY 매도 → 지정가 체결 확인 → 미체결분은 종가에 SPY 재매수
    for o in st["pending_buys"]:
        s = o["symbol"]
        q = prov.quote_on(s, date)
        if not q or s in pos or len(pos) >= SLOTS:
            continue
        got = spy_sell(st, d, date, fx, spyq["open"] * (1 - SLIP), o["amount"], f"fund {s}")
        if q["low"] <= o["limit"] and got > 1:
            px = min(q["open"], o["limit"])
            sh = got * (1 - COMM) / px
            st["cash_usd"] -= got
            pos[s] = {"shares": sh, "cost": got, "entry": px, "opened": date, "days": 0, "sell_limit": None,
                      "sell_open": False, "last": q["close"]}
            _book(st, d, date, fx, s, "BUY", px, sh, got, 0.0, f"limit {o['limit']:.2f}")
            events.append(f"BUY {s}@{px:.2f}")
        else:
            events.append(f"미체결 {s} → SPY 재매수")
    st["pending_buys"] = []

    # 2) 보유 종목 매도
    for s in sorted(pos):
        p = pos[s]
        q = prov.quote_on(s, date)
        if not q:
            continue
        if p["opened"] != date:
            p["days"] += 1
        p["last"] = q["close"]
        stop = p["entry"] * (1 - STOP_PCT)
        px = reason = None
        if p["opened"] != date and q["low"] <= stop:
            px, reason = min(q["open"], stop) * (1 - SLIP), f"stop {stop:.2f}"
        elif p["sell_open"]:
            px, reason = q["open"] * (1 - SLIP), f"max_hold {MAX_HOLD}d"
        elif p["sell_limit"] is not None and q["high"] >= p["sell_limit"]:
            px, reason = max(q["open"], p["sell_limit"]), f"sma5 limit {p['sell_limit']:.2f}"
        if px is not None:
            proceeds = p["shares"] * px * (1 - COMM)
            st["cash_usd"] += proceeds
            _book(st, d, date, fx, s, "SELL", px, p["shares"], proceeds, proceeds - p["cost"], reason)
            events.append(f"SELL {s}@{px:.2f} ({proceeds / p['cost'] - 1:+.1%}, {reason.split()[0]})")
            del pos[s]

    # 남은 현금은 종가에 SPY로
    spy_buy(st, d, date, fx, spyq["close"] * (1 + SLIP), st["cash_usd"], "park cash")

    # 3) 매도 예약
    for s, p in pos.items():
        ser = ML.series_for(prov, s, date, prev)
        ind = indicators(ser) if ser and ser[-1][0] == date else None
        if ind and (p["sell_limit"] is not None or ind["close"] > ind["sma5"]):
            p["sell_limit"] = round(ind["sma5"], 4)
        if p["days"] >= MAX_HOLD:
            p["sell_open"] = True

    # 4) 신호
    held = sum(p["shares"] * p["last"] for p in pos.values())
    eq = st["cash_usd"] + st["spy"]["shares"] * spyq["close"] + held
    free = SLOTS - len(pos)
    vix = vixq["close"]
    if vix >= VIX_MIN and free > 0:
        cands = []
        for s in symbols if symbols is not None else ML.universe():
            if s in pos:
                continue
            ser = ML.series_for(prov, s, date, prev)
            if not ser or ser[-1][0] != date:
                continue
            ind = indicators(ser)
            if ind and ind["close"] >= MIN_PRICE and ind["close"] > ind["sma200"] and ind["rsi2"] < RSI_MAX:
                cands.append((ind["rsi2"], s, ind["close"]))
        for rsi, s, c in sorted(cands)[:free]:
            st["pending_buys"].append({"symbol": s, "limit": round(c * (1 - ENTRY_DISC), 4), "amount": eq / SLOTS,
                                       "rsi2": round(rsi, 2), "signal_date": date})

    cap = st["capital_usd"]
    yr = date[:4]
    append(d / "daily.csv", DAILY_FIELDS, {
        "date": date, "vix": vix, "spy_close": spyq["close"], "spy_shares": round(st["spy"]["shares"], 6),
        "positions": len(pos), "cash_usd": round(st["cash_usd"], 2), "equity_usd": round(eq, 2), "fx": fx,
        "equity_krw": round(eq * fx), "return_pct": f"{(eq / cap - 1) * 100:.2f}",
        "spy_hold_usd": round(cap * (1 - COMM) * spyq["close"] / st["spy_start"], 2),
        "realized_ytd_krw": round(st["realized"].get(yr, 0.0)), "est_tax_ytd_krw": round(est_tax(st, yr)),
        "events": "; ".join(events)})
    st["last_date"], st["last_fx"] = date, fx
    save_state(d, st)
    return f"{date}: VIX {vix:.2f}, 보유 {list(pos) or '없음'}, 평가액 ${eq:.2f} ({eq / cap - 1:+.2%})" + (
        f" | {'; '.join(events)}" if events else "")


def orders_tonight(st: dict) -> list[str]:
    """다음 정규장에 걸 주문 목록(사람이 실제로 따라 할 때 참고용)."""
    out = []
    for o in st["pending_buys"]:
        out.append(f"SPY ${o['amount']:.0f}어치 시가 매도 → {o['symbol']} 매수 지정가 ${o['limit']:.2f} "
                   f"(미체결이면 종가에 SPY 재매수)")
    for s, p in st["positions"].items():
        line = f"{s}: 손절 조건주문 ${p['entry'] * (1 - STOP_PCT):.2f}"
        if p.get("sell_open"):
            line += ", 시가 매도(보유 기간 초과)"
        elif p.get("sell_limit"):
            line += f", 매도 지정가 ${p['sell_limit']:.2f}"
        out.append(line + " → 팔리면 SPY 재매수")
    return out


def cmd_status(d: Path) -> str:
    st = load_state(d)
    lines = [f"SPY+반등 규칙봇: 시작 {st['start']} {int(st['start_krw']):,}원 = ${st['capital_usd']}",
             f"마지막 처리: {st['last_date'] or '-'}  SPY {st['spy']['shares']:.4f}주, 보유 {list(st['positions']) or '없음'}"]
    f = d / "daily.csv"
    if f.exists():
        rows = list(csv.DictReader(f.open(encoding="utf-8")))
        if rows:
            r = rows[-1]
            lines.append(f"평가액 ${r['equity_usd']} ({int(float(r['equity_krw'])):,}원, {r['return_pct']}%) | "
                         f"SPY 보유 ${r['spy_hold_usd']} | 올해 실현 {int(float(r['realized_ytd_krw'])):,}원, "
                         f"추정 양도세 {int(float(r['est_tax_ytd_krw'])):,}원")
    o = orders_tonight(st)
    lines.append("다음 장 주문: " + (" / ".join(o) if o else "없음 (SPY 보유 유지)"))
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
    except SRError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
