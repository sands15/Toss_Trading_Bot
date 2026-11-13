"""20일 돌파 손절·목표 장부 (사용자가 고른 종목, 가상 장부, 실제 주문 없음).

사용자가 정한 진입가·손절가·목표가·수량을 그대로 기록하고, 매 거래일 장 마감 후 CBOE 일봉으로
손절·목표 도달만 기계적으로 처리한다. 새 종목은 사용자가 `add`로 넣을 때만 들어온다.

매 거래일(정산일 D) 처리 — D 이전에 들어간 보유 종목만
1) 시가 ≤ 손절가 → 시가에 매도 (갭 하락)
2) 시가 ≥ 목표가 → 시가에 매도 (갭 상승)
3) 저가 ≤ 손절가 → 손절가에 매도 (같은 날 고가가 목표가에 닿아도 보수적으로 손절)
4) 고가 ≥ 목표가 → 목표가에 매도
비용: 매수·매도 각 수수료 0.1% + 체결 손해 0.05%. 세금·환전 미반영. 쉬는 현금은 이자 없음.
비교: 시작일 종가에 시작 자금 전액 SPY 보유.

사용:
  python3 breakout_ledger.py init --start 2026-11-13 --fx 1336.7
  python3 breakout_ledger.py add --date 2026-11-13 --symbol PG --entry 169.80 --stop 163.35 --target 182.70 --qty 5
  python3 breakout_ledger.py run --date 2026-11-16 [--fx 1340]
  python3 breakout_ledger.py status
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import martingale_ledger as ML

HERE = Path(__file__).resolve().parent
DATA = HERE / "records" / "breakout"
COST = 0.0015  # 수수료 0.1% + 체결 손해 0.05%
DAILY_FIELDS = ["date", "positions", "cash_usd", "equity_usd", "fx", "equity_krw", "return_pct",
                "spy_hold_usd", "events"]
TRADE_FIELDS = ["date", "symbol", "side", "price", "shares", "amount_usd", "fee_usd", "pnl_usd", "r_multiple",
                "reason"]


class BreakoutError(Exception):
    pass


def load_state(d: Path) -> dict:
    f = d / "state.json"
    if not f.exists():
        raise BreakoutError("돌파 장부가 없습니다. 먼저 init을 실행하세요.")
    return json.loads(f.read_text(encoding="utf-8"))


def save_state(d: Path, st: dict) -> None:
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "state.tmp"
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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
        raise BreakoutError("이미 돌파 장부가 있습니다.")
    cap = round(krw / fx, 2)
    st = {"start": start, "start_krw": krw, "start_fx": fx, "capital_usd": cap, "cash_usd": cap,
          "positions": [], "last_date": None, "last_fx": fx, "spy_start": None, "realized_usd": 0.0}
    save_state(d, st)
    return st


def cmd_add(d: Path, date: str, sym: str, entry: float, stop: float, target: float, qty: int,
            setup: str = "20일 돌파", prov=None, check: bool = True) -> str:
    st = load_state(d)
    sym = sym.upper()
    if not stop < entry < target:
        raise BreakoutError("손절가 < 진입가 < 목표가 여야 합니다.")
    if qty <= 0:
        raise BreakoutError("수량은 1 이상이어야 합니다.")
    if any(p["symbol"] == sym for p in st["positions"]):
        raise BreakoutError(f"{sym}은 이미 보유 중입니다.")
    if st["last_date"] and date < st["last_date"]:
        raise BreakoutError(f"이미 {st['last_date']}까지 정산했습니다. 그 이전 날짜로는 넣을 수 없습니다.")
    if check:
        prov = prov or ML.CboePrices()
        q = prov.quote_on(sym, date)
        if q is None:
            raise BreakoutError(f"{sym} {date} 확정 시세를 확인하지 못했습니다.")
        if not q["low"] <= entry <= q["high"]:
            raise BreakoutError(f"{sym} 진입가 {entry}가 {date} 거래 범위({q['low']}~{q['high']}) 밖입니다.")
    amt = entry * qty
    fee = amt * COST
    if amt + fee > st["cash_usd"] + 1e-9:
        raise BreakoutError(f"현금 부족: 필요 ${amt + fee:.2f}, 보유 ${st['cash_usd']:.2f}")
    st["cash_usd"] -= amt + fee
    st["positions"].append({"symbol": sym, "setup": setup, "entry_date": date, "entry": entry, "stop": stop,
                            "target": target, "shares": qty, "entry_fee": round(fee, 4), "last_close": entry})
    save_state(d, st)
    append(d / "trades.csv", TRADE_FIELDS, {"date": date, "symbol": sym, "side": "BUY", "price": entry,
           "shares": qty, "amount_usd": round(amt, 2), "fee_usd": round(fee, 2), "pnl_usd": "", "r_multiple": "",
           "reason": f"{setup} 진입, 손절 {stop}, 목표 {target}"})
    return f"{sym} {qty}주 @ {entry} (손절 {stop}, 목표 {target}, 손절 시 약 −${(entry - stop) * qty:.2f})"


def exit_price(p: dict, q: dict) -> tuple[float, str] | None:
    if q["open"] <= p["stop"]:
        return q["open"], "갭 하락 시가 손절"
    if q["open"] >= p["target"]:
        return q["open"], "갭 상승 시가 목표"
    if q["low"] <= p["stop"]:
        return p["stop"], "손절"
    if q["high"] >= p["target"]:
        return p["target"], "목표 도달"
    return None


def cmd_run(d: Path, date: str, fx: float | None = None, prov=None) -> str:
    st = load_state(d)
    if st["last_date"] and date <= st["last_date"]:
        raise BreakoutError(f"{date}는 이미 정산했습니다(마지막 {st['last_date']}).")
    fx = fx or st["last_fx"]
    prov = prov or ML.CboePrices()
    events, keep = [], []
    for p in st["positions"]:
        if p["entry_date"] >= date:
            keep.append(p)
            continue
        q = prov.quote_on(p["symbol"], date)
        if q is None:
            raise BreakoutError(f"{p['symbol']} {date} 확정 시세 없음 — 정산 중단(나중에 다시 실행)")
        hit = exit_price(p, q)
        if hit is None:
            p["last_close"] = q["close"]
            keep.append(p)
            continue
        px, why = hit
        amt = px * p["shares"]
        fee = amt * COST
        st["cash_usd"] += amt - fee
        pnl = amt - fee - (p["entry"] * p["shares"] + p["entry_fee"])
        r = (px - p["entry"]) / (p["entry"] - p["stop"])
        st["realized_usd"] += pnl
        events.append(f"{p['symbol']} {why} @ {px:.2f} ({pnl:+.2f}$, {r:+.2f}R)")
        append(d / "trades.csv", TRADE_FIELDS, {"date": date, "symbol": p["symbol"], "side": "SELL", "price": px,
               "shares": p["shares"], "amount_usd": round(amt, 2), "fee_usd": round(fee, 2),
               "pnl_usd": round(pnl, 2), "r_multiple": f"{r:.2f}", "reason": why})
    st["positions"] = keep
    if st["spy_start"] is None:
        spy0 = prov.close_on("SPY", st["start"])
        if spy0 is None:
            raise BreakoutError("SPY 시작일 종가를 확인하지 못했습니다.")
        st["spy_start"] = spy0
    spy = prov.close_on("SPY", date)
    if spy is None:
        raise BreakoutError(f"SPY {date} 확정 종가 없음 — 정산 중단")
    eq = st["cash_usd"] + sum(p["last_close"] * p["shares"] for p in keep)
    cap = st["capital_usd"]
    spy_hold = cap * (1 - COST) * spy / st["spy_start"]
    append(d / "daily.csv", DAILY_FIELDS, {"date": date, "positions": len(keep), "cash_usd": round(st["cash_usd"], 2),
           "equity_usd": round(eq, 2), "fx": fx, "equity_krw": round(eq * fx),
           "return_pct": f"{(eq / cap - 1) * 100:.2f}", "spy_hold_usd": round(spy_hold, 2),
           "events": "; ".join(events)})
    st["last_date"], st["last_fx"] = date, fx
    save_state(d, st)
    return f"{date}: 청산 {len(events)}건{(' — ' + '; '.join(events)) if events else ''}, 보유 {len(keep)}종목, 평가액 ${eq:.2f}"


def cmd_status(d: Path) -> str:
    st = load_state(d)
    cap = st["capital_usd"]
    lines = [f"20일 돌파 장부: 시작 {st['start']} {int(st['start_krw']):,}원 = ${cap}",
             f"마지막 정산: {st['last_date'] or '-'}  현금 ${st['cash_usd']:.2f}, 실현 손익 ${st['realized_usd']:+.2f}"]
    eq = st["cash_usd"]
    for p in st["positions"]:
        val = p["last_close"] * p["shares"]
        eq += val
        lines.append(f"  {p['symbol']} {p['shares']}주 @ {p['entry']} → {p['last_close']} "
                     f"({(p['last_close'] / p['entry'] - 1) * 100:+.2f}%) | 손절 {p['stop']} / 목표 {p['target']}")
    lines.append(f"평가액 ${eq:.2f} ({eq * st['last_fx']:,.0f}원, {(eq / cap - 1) * 100:+.2f}%)")
    f = d / "daily.csv"
    if f.exists():
        rows = list(csv.DictReader(f.open(encoding="utf-8")))
        if rows:
            lines.append(f"SPY 보유 ${rows[-1]['spy_hold_usd']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-dir", default=str(DATA))
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("init")
    a.add_argument("--start", required=True)
    a.add_argument("--krw", type=float, default=10_000_000)
    a.add_argument("--fx", type=float, required=True)
    a = sub.add_parser("add")
    a.add_argument("--date", required=True)
    a.add_argument("--symbol", required=True)
    a.add_argument("--entry", type=float, required=True)
    a.add_argument("--stop", type=float, required=True)
    a.add_argument("--target", type=float, required=True)
    a.add_argument("--qty", type=int, required=True)
    a.add_argument("--setup", default="20일 돌파")
    a = sub.add_parser("run")
    a.add_argument("--date", required=True)
    a.add_argument("--fx", type=float)
    sub.add_parser("status")
    args = p.parse_args(argv)
    d = Path(args.data_dir)
    try:
        if args.cmd == "init":
            cmd_init(d, args.start, args.krw, args.fx)
        elif args.cmd == "add":
            print(cmd_add(d, args.date, args.symbol, args.entry, args.stop, args.target, args.qty, args.setup))
        elif args.cmd == "run":
            print(cmd_run(d, args.date, args.fx))
        print(cmd_status(d))
    except BreakoutError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
