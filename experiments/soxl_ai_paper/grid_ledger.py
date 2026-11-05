"""SOXX 변동성 맞춤 그리드 가상 장부 (규칙 기반, AI 판단 없음, paper only).

규칙 (stocks/regime 백테스트의 vgrid와 같음)
- 시작일 종가에 시작 자금의 INVEST(기본 50%)로 SOXX 매수, 기준가 = 그 종가. 나머지는 현금.
- 매 거래일 종가 p, 간격 step = K × ATR(14) (그날까지의 True Range 14일 평균)
  - p ≥ 기준가 + step 이고 보유 중 → 1칸(시작 자금의 UNIT, 기본 10%) 만큼 매도, 기준가 = p
  - p ≤ 기준가 − step 이고 현금 있음 → 1칸 매수, 기준가 = p
  - 그 외 p > 기준가 → 기준가 = max(기준가, p − step)  (트레일링: 오르면 기준가도 따라 올림)
- 체결은 종가, 비용 수수료 0.1% + 슬리피지 0.05%. 세금·환전 비용 미반영.
- 가격: CBOE 일봉(키 불필요). 종가가 확정된 날만 처리하고, 빠진 날은 다음 실행 때 순서대로 따라잡는다.

사용:
  python3 grid_ledger.py init --start 2026-11-05 --fx 1341.5
  python3 grid_ledger.py run [--fx 1341.5]     # 처리 안 된 확정 거래일을 모두 정산
  python3 grid_ledger.py status
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "vendor" / "tradermonty"))

DATA = HERE / "records" / "grid"
SYMBOL = "SOXX"
INVEST, UNIT, K, ATR_DAYS = 0.5, 0.1, 1.0, 14
COST = 0.0015
ET = ZoneInfo("America/New_York")
DAILY_FIELDS = ["date", "close", "atr", "step", "ref", "action", "shares", "cash_usd", "equity_usd", "fx",
                "equity_krw", "return_pct", "soxx_hold_100_usd", "soxx_hold_50_usd"]
TRADE_FIELDS = ["date", "side", "shares", "price", "value_usd", "fee_usd", "ref_before", "step"]


class GridError(Exception):
    pass


def load_state(d: Path) -> dict:
    f = d / "state.json"
    if not f.exists():
        raise GridError("그리드 장부가 없습니다. 먼저 init을 실행하세요.")
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
        raise GridError("이미 그리드 장부가 있습니다.")
    capital = round(krw / fx, 2)
    st = {"symbol": SYMBOL, "start": start, "start_krw": krw, "start_fx": fx, "capital_usd": capital,
          "invest": INVEST, "unit": UNIT, "k": K, "cash_usd": capital, "shares": 0.0, "ref": None,
          "last_date": None, "start_close": None, "last_fx": fx}
    save_state(d, st)
    return st


def final_rows(fetch=None) -> list[dict]:
    """CBOE 일봉(오래된 순). 종가가 확정되지 않은 마지막 행은 뺀다."""
    if fetch is not None:
        return fetch()
    from cboe_fmp_client import CboeFMPClient

    from prices import session_info
    c = CboeFMPClient()
    rows = c._rows_oldest_first(SYMBOL)
    q = c._raw_quote(SYMBOL)
    if q and rows and q.get("last_trade_time"):
        qd, final = session_info({"last_trade_time": q["last_trade_time"],
                                  "timestamp_utc": c.cache[("quote", SYMBOL)]["timestamp"]})
        if rows[-1]["date"] == qd and not final:
            rows = rows[:-1]
    today_et = datetime.now(ET).date().isoformat()
    return [r for r in rows if r["date"] <= today_et]


def atr_at(rows: list[dict], i: int) -> float:
    trs = []
    for j in range(max(1, i - ATR_DAYS + 1), i + 1):
        h, lo, pc = rows[j]["high"], rows[j]["low"], rows[j - 1]["close"]
        trs.append(max(h, pc) - min(lo, pc))
    return sum(trs) / len(trs)


def cmd_run(d: Path, fx: float | None = None, fetch=None) -> list[str]:
    st = load_state(d)
    fx = fx or st["last_fx"]
    rows = final_rows(fetch)
    idx = {r["date"]: i for i, r in enumerate(rows)}
    todo = [r["date"] for r in rows if r["date"] >= st["start"] and (st["last_date"] is None or r["date"] > st["last_date"])]
    done = []
    cap = st["capital_usd"]
    unit_usd = cap * st["unit"]
    for date in todo:
        i = idx[date]
        p = rows[i]["close"]
        atr = atr_at(rows, i)
        step = st["k"] * atr
        action, ref_before = "hold", st["ref"]
        if st["ref"] is None:  # 첫날: INVEST 만큼 매수
            amt = cap * st["invest"]
            st["shares"] += amt * (1 - COST) / p
            st["cash_usd"] -= amt
            st["ref"], st["start_close"] = p, p
            action = "init_buy"
            append(d / "trades.csv", TRADE_FIELDS, {"date": date, "side": "BUY", "shares": round(amt * (1 - COST) / p, 6),
                   "price": p, "value_usd": round(amt, 2), "fee_usd": round(amt * COST, 2), "ref_before": "", "step": round(step, 4)})
        elif p >= st["ref"] + step and st["shares"] > 0:
            q = min(st["shares"], unit_usd / p)
            st["shares"] -= q
            st["cash_usd"] += q * p * (1 - COST)
            st["ref"] = p
            action = "sell"
            append(d / "trades.csv", TRADE_FIELDS, {"date": date, "side": "SELL", "shares": round(q, 6), "price": p,
                   "value_usd": round(q * p, 2), "fee_usd": round(q * p * COST, 2), "ref_before": round(ref_before, 4), "step": round(step, 4)})
        elif p <= st["ref"] - step and st["cash_usd"] > 0.01:
            amt = min(unit_usd, st["cash_usd"])
            st["shares"] += amt * (1 - COST) / p
            st["cash_usd"] -= amt
            st["ref"] = p
            action = "buy"
            append(d / "trades.csv", TRADE_FIELDS, {"date": date, "side": "BUY", "shares": round(amt * (1 - COST) / p, 6),
                   "price": p, "value_usd": round(amt, 2), "fee_usd": round(amt * COST, 2), "ref_before": round(ref_before, 4), "step": round(step, 4)})
        elif p > st["ref"]:
            new_ref = max(st["ref"], p - step)
            if new_ref != st["ref"]:
                action = "trail"
            st["ref"] = new_ref
        eq = st["cash_usd"] + st["shares"] * p
        hold100 = cap * (1 - COST) * p / st["start_close"]
        hold50 = cap * (1 - st["invest"]) + cap * st["invest"] * (1 - COST) * p / st["start_close"]
        append(d / "daily.csv", DAILY_FIELDS, {
            "date": date, "close": p, "atr": round(atr, 4), "step": round(step, 4), "ref": round(st["ref"], 4),
            "action": action, "shares": round(st["shares"], 6), "cash_usd": round(st["cash_usd"], 2),
            "equity_usd": round(eq, 2), "fx": fx, "equity_krw": round(eq * fx),
            "return_pct": f"{(eq / cap - 1) * 100:.2f}", "soxx_hold_100_usd": round(hold100, 2),
            "soxx_hold_50_usd": round(hold50, 2)})
        st["last_date"] = date
        done.append(f"{date} {action} close={p} step={step:.2f} ref={st['ref']:.2f} equity=${eq:.2f}")
    st["last_fx"] = fx
    save_state(d, st)
    return done


def cmd_status(d: Path) -> str:
    st = load_state(d)
    lines = [f"SOXX 변동성 그리드: 시작 {st['start']} {int(st['start_krw']):,}원 = ${st['capital_usd']}",
             f"마지막 처리: {st['last_date'] or '-'}  보유 {st['shares']:.4f}주, 현금 ${st['cash_usd']:.2f}, 기준가 {st['ref']}"]
    f = d / "daily.csv"
    if f.exists():
        rows = list(csv.DictReader(f.open(encoding="utf-8")))
        if rows:
            r = rows[-1]
            lines.append(f"평가액 ${r['equity_usd']} ({int(float(r['equity_krw'])):,}원, {r['return_pct']}%) | "
                         f"SOXX 100% 보유 ${r['soxx_hold_100_usd']} / 50% 보유 ${r['soxx_hold_50_usd']}")
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
    a.add_argument("--fx", type=float)
    sub.add_parser("status")
    args = p.parse_args(argv)
    d = Path(args.data_dir)
    try:
        if args.cmd == "init":
            cmd_init(d, args.start, args.krw, args.fx)
        elif args.cmd == "run":
            for line in cmd_run(d, args.fx) or ["처리할 확정 거래일 없음"]:
                print(line)
        print(cmd_status(d))
    except GridError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
