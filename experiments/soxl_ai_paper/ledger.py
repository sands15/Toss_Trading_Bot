"""SOXL/SOXS AI 가상투자 장부 (paper only).

이 스크립트는 실제 주문을 내지 않고, 토스 API나 turtle_bot 패키지도 사용하지 않는다.
AI(예약 세션)가 매일 판단을 기록하면 이 장부가 규칙을 강제하고 가상 체결을 계산한다.

명령:
  init    --date D --krw 10000000 --fx 1390.5
  decide  --date D --action SOXL|SOXS|CASH [--weight 0.5] --reason "..."
  settle  --date D --open SOXL=..,SOXS=..,SOXX=.. --close SOXL=..,SOXS=..,SOXX=.. [--fx 1390.5]
  status
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "records"

SYMBOLS = ("SOXL", "SOXS")
BENCHMARKS = ("SOXX", "SOXL")
ACTIONS = SYMBOLS + ("CASH",)

# 규칙 (README의 "규칙"과 같아야 한다)
MAX_WEIGHT = Decimal("0.5")
STOP_LOSS = Decimal("-0.10")
MAX_HOLD_DAYS = 5
COMMISSION = Decimal("0.001")  # 매수/매도 각각 0.1%
SLIPPAGE = Decimal("0.0005")  # 시가 대비 0.05% 불리하게 체결

DAILY_FIELDS = [
    "date", "position", "shares", "close", "cash_usd", "equity_usd", "fx",
    "equity_krw", "return_pct", "soxx_hold_usd", "soxl_hold_usd", "event",
]
TRADE_FIELDS = ["date", "side", "symbol", "shares", "price", "fee_usd", "reason"]


class LedgerError(Exception):
    pass


def D(value) -> Decimal:
    return Decimal(str(value))


def money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def qty(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def state_path(data_dir: Path) -> Path:
    return data_dir / "state.json"


def load_state(data_dir: Path) -> dict:
    path = state_path(data_dir)
    if not path.exists():
        raise LedgerError("장부가 없습니다. 먼저 init을 실행하세요.")
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(data_dir: Path, state: dict) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_path(data_dir).with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(state_path(data_dir))


def append_csv(path: Path, fields: list[str], row: dict) -> None:
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        if new:
            writer.writeheader()
        writer.writerow(row)


def parse_prices(text: str, needed: tuple[str, ...]) -> dict[str, Decimal]:
    prices: dict[str, Decimal] = {}
    for part in text.split(","):
        if not part.strip():
            continue
        sym, _, val = part.partition("=")
        prices[sym.strip().upper()] = D(val.strip())
    missing = [s for s in needed if s not in prices]
    if missing:
        raise LedgerError(f"가격 누락: {', '.join(missing)}")
    if any(p <= 0 for p in prices.values()):
        raise LedgerError("가격은 0보다 커야 합니다.")
    return prices


def cmd_init(data_dir: Path, date: str, krw: Decimal, fx: Decimal) -> dict:
    if state_path(data_dir).exists():
        raise LedgerError("이미 장부가 있습니다. 다시 시작하려면 records/ 를 직접 정리하세요.")
    cash = money(krw / fx)
    state = {
        "start_date": date,
        "start_krw": str(krw),
        "start_fx": str(fx),
        "start_usd": str(cash),
        "cash_usd": str(cash),
        "position": None,
        "pending": None,
        "forced_exit": None,
        "benchmarks": None,
        "last_settled": None,
        "last_fx": str(fx),
    }
    save_state(data_dir, state)
    return state


def cmd_decide(data_dir: Path, date: str, action: str, weight: Decimal, reason: str) -> dict:
    state = load_state(data_dir)
    action = action.upper()
    if action not in ACTIONS:
        raise LedgerError(f"action은 {ACTIONS} 중 하나여야 합니다.")
    if not reason.strip():
        raise LedgerError("판단 이유(reason)는 필수입니다.")
    if state["last_settled"] and date <= state["last_settled"]:
        raise LedgerError("이미 정산된 날짜에는 판단을 기록할 수 없습니다.")
    if action == "CASH":
        weight = Decimal("0")
    elif not (Decimal("0") < weight <= MAX_WEIGHT):
        raise LedgerError(f"비중은 0 초과 {MAX_WEIGHT} 이하여야 합니다.")
    if state["forced_exit"] and action != "CASH":
        raise LedgerError(
            f"강제 청산({state['forced_exit']}) 다음 거래일은 현금만 허용됩니다."
        )
    state["pending"] = {"date": date, "action": action, "weight": str(weight), "reason": reason}
    save_state(data_dir, state)
    return state


def _sell(state: dict, price_open: Decimal, date: str, reason: str, trades: list) -> None:
    pos = state["position"]
    shares = D(pos["shares"])
    fill = price_open * (1 - SLIPPAGE)
    gross = shares * fill
    fee = money(gross * COMMISSION)
    state["cash_usd"] = str(money(D(state["cash_usd"]) + gross - fee))
    trades.append({"date": date, "side": "SELL", "symbol": pos["symbol"], "shares": str(shares),
                   "price": str(qty(fill)), "fee_usd": str(fee), "reason": reason})
    state["position"] = None


def _buy(state: dict, symbol: str, price_open: Decimal, budget: Decimal, date: str,
         reason: str, trades: list) -> None:
    fill = price_open * (1 + SLIPPAGE)
    shares = qty(budget / (fill * (1 + COMMISSION)))
    if shares <= 0:
        return
    gross = shares * fill
    fee = money(gross * COMMISSION)
    state["cash_usd"] = str(money(D(state["cash_usd"]) - gross - fee))
    state["position"] = {"symbol": symbol, "shares": str(shares), "entry_price": str(qty(fill)),
                         "entry_date": date, "days_held": 0}
    trades.append({"date": date, "side": "BUY", "symbol": symbol, "shares": str(shares),
                   "price": str(qty(fill)), "fee_usd": str(fee), "reason": reason})


def cmd_settle(data_dir: Path, date: str, opens: dict, closes: dict, fx: Decimal | None) -> dict:
    state = load_state(data_dir)
    if state["last_settled"] and date <= state["last_settled"]:
        raise LedgerError(f"{date}은(는) 이미 정산되었거나 이전 날짜입니다.")
    fx = fx or D(state["last_fx"])
    trades: list[dict] = []
    events: list[str] = []

    # 1) 벤치마크: 첫 정산일 시가에 전액 매수 후 보유
    if state["benchmarks"] is None:
        start = D(state["cash_usd"])
        state["benchmarks"] = {s: str(qty(start / opens[s])) for s in BENCHMARKS}

    # 2) 시가 체결
    pending = state["pending"]
    if state["forced_exit"]:
        pending = {"action": "CASH", "weight": "0", "reason": f"강제 청산: {state['forced_exit']}"}
        events.append(f"forced_exit:{state['forced_exit']}")
        state["forced_exit"] = None
    pos = state["position"]
    if pending:
        action = pending["action"]
        held = pos["symbol"] if pos else "CASH"
        if action != held:
            if pos:
                _sell(state, opens[pos["symbol"]], date, pending["reason"], trades)
            if action in SYMBOLS:
                budget = D(state["cash_usd"]) * D(pending["weight"])
                _buy(state, action, opens[action], budget, date, pending["reason"], trades)
        else:
            events.append(f"hold:{held}")
    state["pending"] = None

    # 3) 종가 평가, 손절/보유기간 점검
    pos = state["position"]
    close = Decimal("0")
    shares = Decimal("0")
    if pos:
        pos["days_held"] += 1
        close = closes[pos["symbol"]]
        shares = D(pos["shares"])
        change = close / D(pos["entry_price"]) - 1
        if change <= STOP_LOSS:
            state["forced_exit"] = f"stop_loss {change:.2%}"
        elif pos["days_held"] >= MAX_HOLD_DAYS:
            state["forced_exit"] = f"max_hold {pos['days_held']}d"
        if state["forced_exit"]:
            events.append(f"flag:{state['forced_exit']}")
    equity = money(D(state["cash_usd"]) + shares * close)
    start_usd = D(state["start_usd"])
    bench = {s: money(D(state["benchmarks"][s]) * closes[s]) for s in BENCHMARKS}
    for t in trades:
        append_csv(data_dir / "trades.csv", TRADE_FIELDS, t)
    append_csv(data_dir / "daily.csv", DAILY_FIELDS, {
        "date": date,
        "position": pos["symbol"] if pos else "CASH",
        "shares": str(shares),
        "close": str(close),
        "cash_usd": state["cash_usd"],
        "equity_usd": str(equity),
        "fx": str(fx),
        "equity_krw": str(money(equity * fx).quantize(Decimal("1"))),
        "return_pct": f"{(equity / start_usd - 1) * 100:.2f}",
        "soxx_hold_usd": str(bench["SOXX"]),
        "soxl_hold_usd": str(bench["SOXL"]),
        "event": ";".join(events),
    })
    state["last_settled"] = date
    state["last_fx"] = str(fx)
    save_state(data_dir, state)
    return state


def cmd_status(data_dir: Path) -> str:
    state = load_state(data_dir)
    lines = [f"시작: {state['start_date']}  {int(D(state['start_krw'])):,}원 = ${state['start_usd']}"]
    lines.append(f"마지막 정산: {state['last_settled'] or '-'}  현금: ${state['cash_usd']}")
    pos = state["position"]
    lines.append(
        f"보유: {pos['symbol']} {pos['shares']}주 @ {pos['entry_price']} ({pos['days_held']}일째)"
        if pos else "보유: 현금"
    )
    if state["pending"]:
        p = state["pending"]
        lines.append(f"대기 중 판단: {p['action']} 비중 {p['weight']} - {p['reason']}")
    if state["forced_exit"]:
        lines.append(f"다음 시가 강제 청산 예정: {state['forced_exit']}")
    daily = data_dir / "daily.csv"
    if daily.exists():
        rows = list(csv.DictReader(daily.open(encoding="utf-8")))
        if rows:
            r = rows[-1]
            lines.append(
                f"평가액: ${r['equity_usd']} ({r['equity_krw']}원, {r['return_pct']}%)  "
                f"| SOXX 보유 ${r['soxx_hold_usd']}  SOXL 보유 ${r['soxl_hold_usd']}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", default=str(DATA))
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("--date", required=True)
    p.add_argument("--krw", default="10000000")
    p.add_argument("--fx", required=True)
    p = sub.add_parser("decide")
    p.add_argument("--date", required=True)
    p.add_argument("--action", required=True)
    p.add_argument("--weight", default="0")
    p.add_argument("--reason", required=True)
    p = sub.add_parser("settle")
    p.add_argument("--date", required=True)
    p.add_argument("--open", required=True)
    p.add_argument("--close", required=True)
    p.add_argument("--fx")
    sub.add_parser("status")
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir)
    try:
        if args.cmd == "init":
            cmd_init(data_dir, args.date, D(args.krw), D(args.fx))
        elif args.cmd == "decide":
            cmd_decide(data_dir, args.date, args.action, D(args.weight), args.reason)
        elif args.cmd == "settle":
            need = SYMBOLS + ("SOXX",)
            cmd_settle(data_dir, args.date, parse_prices(args.open, need),
                       parse_prices(args.close, need), D(args.fx) if args.fx else None)
        print(cmd_status(data_dir))
    except LedgerError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
