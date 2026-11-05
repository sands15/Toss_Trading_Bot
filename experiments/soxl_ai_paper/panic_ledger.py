"""공포장 급락 반등 규칙봇 (가상 장부, AI 판단 없음, 실제 주문 없음).

scratchpad 백테스트 "RSI<5, VIX≥25, 매수 지정가 −2%, 매도 5일선 지정가, 동시 3종목"과 같은 규칙.
2015~2020에서 고르고 2021~2026에서 확인: 거래당 순이익 +2.4% / +1.4%(토스 수수료 0.1% 반영),
12년 연 +8.7%(쉬는 현금 국채 이자 포함), 최대낙폭 −12%. 거래 수가 적다(연 9회 안팎).

매 거래일 장 마감 후(정산일 D) 처리 순서
1) 어제 건 매수 지정가: D 저가 ≤ 지정가 → min(D 시가, 지정가)에 체결. 안 닿으면 취소
2) 매도 지정가(5일선): D 고가 ≥ 지정가 → max(D 시가, 지정가)에 체결. 안 닿으면 유지
3) 쉬는 현금에 하루치 국채 이자(--cash-rate, 기본 연 3.8%)
4) 보유 종목 종가 > 5일선 → 다음 날 5일선 가격에 매도 지정가 (이미 걸려 있으면 가격 갱신)
5) VIX 종가 ≥ 25 이고 빈자리가 있으면: S&P 500(universe 파일) 중 종가 ≥ $5, 종가 > 200일선,
   RSI(2) < 5 인 종목을 RSI 낮은 순으로 → 다음 날 종가 × 0.98 매수 지정가 (1종목 = 평가액 / 3)
비용: 매수·매도 각 수수료 0.1%(지정가라 체결 손해 없음 가정). 세금·환전 미반영.

가격은 martingale_ledger 의 CBOE 가격 캐시를 같이 쓴다. 정산 단계에서 마틴게일 장부 다음에 실행하면
그날 시세를 다시 받지 않는다.

사용:
  python3 panic_ledger.py init --start 2026-11-05 --fx 1341.5
  python3 panic_ledger.py run --date 2026-11-05 [--fx 1345] [--cash-rate 0.038]
  python3 panic_ledger.py status
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import martingale_ledger as ML

HERE = Path(__file__).resolve().parent
DATA = HERE / "records" / "panic"
SLOTS, RSI_MAX, VIX_MIN, ENTRY_DISC, MIN_PRICE = 3, 5.0, 25.0, 0.02, 5.0
COMM = 0.001
DAILY_FIELDS = ["date", "vix", "positions", "pending_buys", "cash_usd", "equity_usd", "fx", "equity_krw",
                "return_pct", "spy_hold_usd", "events"]
TRADE_FIELDS = ["date", "symbol", "side", "price", "shares", "amount_usd", "pnl_pct", "reason"]


class PanicError(Exception):
    pass


def load_state(d: Path) -> dict:
    f = d / "state.json"
    if not f.exists():
        raise PanicError("반등 규칙봇 장부가 없습니다. 먼저 init을 실행하세요.")
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


def indicators(series: list[tuple[str, float]]) -> dict | None:
    """종가 기록(오래된 순, 마지막이 정산일) → sma5, sma200, rsi2."""
    if len(series) < 200:
        return None
    cl = [x[1] for x in series]
    ag = al = None
    for i in range(1, len(cl)):
        ch = cl[i] - cl[i - 1]
        g, lo = max(ch, 0.0), max(-ch, 0.0)
        ag, al = (g, lo) if ag is None else ((ag + g) / 2, (al + lo) / 2)
    rsi = 100.0 if not al else 100 - 100 / (1 + ag / al)
    return {"close": cl[-1], "sma5": sum(cl[-5:]) / 5, "sma200": sum(cl[-200:]) / 200, "rsi2": rsi}


def cmd_init(d: Path, start: str, krw: float, fx: float) -> dict:
    if (d / "state.json").exists():
        raise PanicError("이미 반등 규칙봇 장부가 있습니다.")
    cap = round(krw / fx, 2)
    st = {"start": start, "start_krw": krw, "start_fx": fx, "capital_usd": cap, "cash_usd": cap, "positions": {},
          "pending_buys": [], "last_date": None, "last_fx": fx, "spy_start": None,
          "params": {"slots": SLOTS, "rsi_max": RSI_MAX, "vix_min": VIX_MIN, "entry_disc": ENTRY_DISC, "comm": COMM}}
    save_state(d, st)
    return st


def cmd_run(d: Path, date: str, fx: float | None = None, cash_rate: float = 0.038, prov=None,
            symbols: list[str] | None = None) -> str:
    st = load_state(d)
    if date < st["start"]:
        raise PanicError(f"시작일({st['start']}) 이전 날짜입니다.")
    if st["last_date"] and date <= st["last_date"]:
        raise PanicError(f"{date}은(는) 이미 처리했거나 이전 날짜입니다.")
    prov = prov or ML.CboePrices()
    fx = fx or st["last_fx"]
    prev = st["last_date"]
    spy = ML.series_for(prov, "SPY", date, prev)
    if not spy or spy[-1][0] != date:
        raise PanicError(f"SPY {date} 종가를 확인하지 못했습니다(종가 미확정이거나 휴장).")
    vixq = prov.quote_on("^VIX", date)
    if vixq is None:
        raise PanicError(f"VIX {date} 종가를 확인하지 못했습니다.")
    if st["spy_start"] is None:
        st["spy_start"] = spy[-1][1]
    events: list[str] = []
    pos = st["positions"]

    # 1) 매수 지정가 체결
    for order in st["pending_buys"]:
        s, lim, amt = order["symbol"], order["limit"], order["amount"]
        q = prov.quote_on(s, date)
        if not q or s in pos or len(pos) >= SLOTS:
            continue
        if q["low"] <= lim:
            px = min(q["open"], lim)
            amt = min(amt, st["cash_usd"])
            if amt <= 1:
                continue
            sh = amt * (1 - COMM) / px
            st["cash_usd"] -= amt
            pos[s] = {"shares": sh, "cost": amt, "entry": px, "opened": date, "sell_limit": None, "last": q["close"]}
            append(d / "trades.csv", TRADE_FIELDS, {"date": date, "symbol": s, "side": "BUY", "price": round(px, 4),
                   "shares": round(sh, 6), "amount_usd": round(amt, 2), "pnl_pct": "", "reason": f"limit {lim:.2f}"})
            events.append(f"BUY {s}@{px:.2f}")
    st["pending_buys"] = []

    # 2) 매도 지정가 체결 + 종가 갱신
    for s in sorted(pos):
        p = pos[s]
        q = prov.quote_on(s, date)
        if not q:
            continue
        p["last"] = q["close"]
        if p["sell_limit"] is not None and q["high"] >= p["sell_limit"]:
            px = max(q["open"], p["sell_limit"])
            proceeds = p["shares"] * px * (1 - COMM)
            st["cash_usd"] += proceeds
            pnl = proceeds / p["cost"] - 1
            append(d / "trades.csv", TRADE_FIELDS, {"date": date, "symbol": s, "side": "SELL", "price": round(px, 4),
                   "shares": round(p["shares"], 6), "amount_usd": round(proceeds, 2), "pnl_pct": f"{pnl * 100:.2f}",
                   "reason": f"sma5 limit {p['sell_limit']:.2f}"})
            events.append(f"SELL {s}@{px:.2f} ({pnl:+.1%})")
            del pos[s]

    # 3) 현금 이자
    st["cash_usd"] *= 1 + cash_rate / 252

    # 4) 매도 지정가 설정/갱신
    for s, p in pos.items():
        ser = ML.series_for(prov, s, date, prev)
        ind = indicators(ser) if ser and ser[-1][0] == date else None
        if not ind:
            continue
        if p["sell_limit"] is not None or ind["close"] > ind["sma5"]:
            p["sell_limit"] = round(ind["sma5"], 4)

    # 5) 신규 매수 신호
    held = sum(p["shares"] * p["last"] for p in pos.values())
    eq = st["cash_usd"] + held
    free = SLOTS - len(pos)
    vix = vixq["close"]
    if vix >= VIX_MIN and free > 0:
        cands = []
        for s in symbols or ML.universe():
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
        if cands:
            events.append(f"signals {len(cands)} → orders {min(free, len(cands))}")

    cap = st["capital_usd"]
    append(d / "daily.csv", DAILY_FIELDS, {
        "date": date, "vix": vix, "positions": len(pos), "pending_buys": len(st["pending_buys"]),
        "cash_usd": round(st["cash_usd"], 2), "equity_usd": round(eq, 2), "fx": fx, "equity_krw": round(eq * fx),
        "return_pct": f"{(eq / cap - 1) * 100:.2f}",
        "spy_hold_usd": round(cap * (1 - COMM) * spy[-1][1] / st["spy_start"], 2), "events": "; ".join(events)})
    st["last_date"], st["last_fx"] = date, fx
    save_state(d, st)
    return (f"{date}: VIX {vix:.2f}{' (조건 ON)' if vix >= VIX_MIN else ''}, 보유 {len(pos)}종목, "
            f"내일 매수 주문 {len(st['pending_buys'])}건, 평가액 ${eq:.2f} ({eq / cap - 1:+.2%})"
            + (f" | {'; '.join(events)}" if events else ""))


def cmd_status(d: Path) -> str:
    st = load_state(d)
    lines = [f"공포장 반등 규칙봇: 시작 {st['start']} {int(st['start_krw']):,}원 = ${st['capital_usd']}",
             f"마지막 처리: {st['last_date'] or '-'}  보유 {', '.join(st['positions']) or '없음'}, "
             f"대기 매수 {len(st['pending_buys'])}건, 현금 ${st['cash_usd']:.2f}"]
    f = d / "daily.csv"
    if f.exists():
        rows = list(csv.DictReader(f.open(encoding="utf-8")))
        if rows:
            r = rows[-1]
            lines.append(f"평가액 ${r['equity_usd']} ({int(float(r['equity_krw'])):,}원, {r['return_pct']}%), "
                         f"VIX {r['vix']} | SPY 보유 ${r['spy_hold_usd']}")
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
    a.add_argument("--cash-rate", type=float, default=0.038)
    sub.add_parser("status")
    args = p.parse_args(argv)
    d = Path(args.data_dir)
    try:
        if args.cmd == "init":
            cmd_init(d, args.start, args.krw, args.fx)
        elif args.cmd == "run":
            print(cmd_run(d, args.date, args.fx, args.cash_rate))
        print(cmd_status(d))
    except PanicError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
