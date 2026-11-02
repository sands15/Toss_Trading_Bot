"""SOXL/SOXS AI 가상투자 장부 (paper only).

이 스크립트는 실제 주문을 내지 않고, 토스 API나 turtle_bot 패키지도 사용하지 않는다.
AI(예약 세션)가 판단을 기록하면 이 장부가 규칙을 강제하고 가상 체결을 계산한다.

명령:
  init      --date D --krw 10000000 --fx 1390.5
  decide    --date D --action SOXL|SOXS|CASH [--weight 0.5] [--stop-pct 0.10] --reason "..."
  intraday  --date D --price 41.2 --time "01:07 KST" --source URL --reason "..."
  settle    --date D --open SOXL=..,SOXS=..,SOXX=.. --high SOXL=..,SOXS=..
            --low SOXL=..,SOXS=.. --close SOXL=..,SOXS=..,SOXX=.. [--fx 1390.5]
  status

하루 처리 순서(settle):
  1) 시가: 대기 중 판단(또는 강제 청산)대로 매도/매수
  2) 시가가 이미 손절가 이하(갭 하락)면 시가에 손절
  3) 장중: 저가가 손절가 이하면 손절가에 손절(조건 주문 체결).
     장중 점검 청산이 기록돼 있어도 저가가 손절가에 닿았다면 보수적으로 손절가 체결로 본다.
  4) 장중 점검 청산 기록이 있으면 그 가격에 청산
  5) 종가 평가, 최대 보유일 점검
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
STOP_PCT_DEFAULT = Decimal("0.10")  # 진입가 대비 손절 폭 기본값
STOP_PCT_MIN = Decimal("0.03")
STOP_PCT_MAX = Decimal("0.10")
MAX_HOLD_DAYS = 5
COMMISSION = Decimal("0.001")  # 매수/매도 각각 0.1%
SLIPPAGE = Decimal("0.0005")  # 시장가 체결 시 0.05% 불리하게
STOP_SLIPPAGE = Decimal("0.003")  # 손절 조건 주문 체결 시 0.3% 불리하게

DAILY_FIELDS = [
    "date", "position", "shares", "close", "stop_price", "cash_usd", "equity_usd", "fx",
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
    state = json.loads(path.read_text(encoding="utf-8"))
    state.setdefault("cooldown", None)
    state.setdefault("intraday", None)
    return state


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


def held_after_open(state: dict) -> str:
    """오늘 시가 체결 후 들고 있을 상품."""
    if state["forced_exit"]:
        return "CASH"
    if state["pending"]:
        return state["pending"]["action"]
    return state["position"]["symbol"] if state["position"] else "CASH"


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
        "cooldown": None,
        "intraday": None,
        "benchmarks": None,
        "last_settled": None,
        "last_fx": str(fx),
    }
    save_state(data_dir, state)
    return state


def cmd_decide(data_dir: Path, date: str, action: str, weight: Decimal, reason: str,
               stop_pct: Decimal = STOP_PCT_DEFAULT) -> dict:
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
    if not (STOP_PCT_MIN <= stop_pct <= STOP_PCT_MAX):
        raise LedgerError(f"손절 폭은 {STOP_PCT_MIN}~{STOP_PCT_MAX} 사이여야 합니다.")
    blocked = state["forced_exit"] or state["cooldown"]
    if blocked and action != "CASH":
        raise LedgerError(f"직전 강제 청산/손절({blocked}) 다음 거래일은 현금만 허용됩니다.")
    pos = state["position"]
    if pos and action == pos["symbol"] and stop_pct != D(pos.get("stop_pct", STOP_PCT_DEFAULT)):
        # 보유 유지 시 손절가는 위로만 올릴 수 있다 (손절 완화 금지)
        new_stop = D(pos["entry_price"]) * (1 - stop_pct)
        if new_stop < D(pos["stop_price"]):
            raise LedgerError("보유 중에는 손절가를 낮출 수 없습니다.")
    state["pending"] = {"date": date, "action": action, "weight": str(weight),
                        "stop_pct": str(stop_pct), "reason": reason}
    save_state(data_dir, state)
    return state


def cmd_intraday(data_dir: Path, date: str, price: Decimal, time: str, source: str,
                 reason: str) -> dict:
    state = load_state(data_dir)
    if state["last_settled"] and date <= state["last_settled"]:
        raise LedgerError("이미 정산된 날짜입니다.")
    if not (reason.strip() and source.strip() and time.strip()):
        raise LedgerError("시각(time), 출처(source), 이유(reason)는 필수입니다.")
    if price <= 0:
        raise LedgerError("가격은 0보다 커야 합니다.")
    symbol = held_after_open(state)
    if symbol == "CASH":
        raise LedgerError("오늘은 보유 종목이 없어 장중 청산할 것이 없습니다.")
    state["intraday"] = {"date": date, "symbol": symbol, "price": str(price), "time": time,
                         "source": source, "reason": reason}
    save_state(data_dir, state)
    return state


def _sell(state: dict, fill: Decimal, date: str, reason: str, trades: list) -> None:
    pos = state["position"]
    shares = D(pos["shares"])
    gross = shares * fill
    fee = money(gross * COMMISSION)
    state["cash_usd"] = str(money(D(state["cash_usd"]) + gross - fee))
    trades.append({"date": date, "side": "SELL", "symbol": pos["symbol"], "shares": str(shares),
                   "price": str(qty(fill)), "fee_usd": str(fee), "reason": reason})
    state["position"] = None


def _buy(state: dict, symbol: str, price_open: Decimal, budget: Decimal, stop_pct: Decimal,
         date: str, reason: str, trades: list) -> None:
    fill = price_open * (1 + SLIPPAGE)
    shares = qty(budget / (fill * (1 + COMMISSION)))
    if shares <= 0:
        return
    gross = shares * fill
    fee = money(gross * COMMISSION)
    state["cash_usd"] = str(money(D(state["cash_usd"]) - gross - fee))
    state["position"] = {"symbol": symbol, "shares": str(shares), "entry_price": str(qty(fill)),
                         "entry_date": date, "days_held": 0, "stop_pct": str(stop_pct),
                         "stop_price": str(qty(fill * (1 - stop_pct)))}
    trades.append({"date": date, "side": "BUY", "symbol": symbol, "shares": str(shares),
                   "price": str(qty(fill)), "fee_usd": str(fee), "reason": reason})


def cmd_settle(data_dir: Path, date: str, opens: dict, highs: dict, lows: dict, closes: dict,
               fx: Decimal | None) -> dict:
    state = load_state(data_dir)
    if state["last_settled"] and date <= state["last_settled"]:
        raise LedgerError(f"{date}은(는) 이미 정산되었거나 이전 날짜입니다.")
    for s in SYMBOLS:
        if not (lows[s] <= min(opens[s], closes[s]) and highs[s] >= max(opens[s], closes[s])):
            raise LedgerError(f"{s} 가격이 맞지 않습니다: 저가 ≤ 시가·종가 ≤ 고가 여야 합니다.")
    fx = fx or D(state["last_fx"])
    trades: list[dict] = []
    events: list[str] = []
    state["cooldown"] = None  # 손절 다음 거래일 제한은 이 날 판단으로 이미 적용됨
    intraday = state["intraday"] if state["intraday"] and state["intraday"]["date"] == date else None
    state["intraday"] = None

    # 0) 벤치마크: 첫 정산일 시가에 전액 매수 후 보유
    if state["benchmarks"] is None:
        start = D(state["cash_usd"])
        state["benchmarks"] = {s: str(qty(start / opens[s])) for s in BENCHMARKS}

    # 1) 시가 체결
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
                _sell(state, opens[pos["symbol"]] * (1 - SLIPPAGE), date, pending["reason"], trades)
            if action in SYMBOLS:
                budget = D(state["cash_usd"]) * D(pending["weight"])
                _buy(state, action, opens[action], budget,
                     D(pending.get("stop_pct", STOP_PCT_DEFAULT)), date, pending["reason"], trades)
        elif pos:
            events.append(f"hold:{held}")
            new_pct = D(pending.get("stop_pct", pos.get("stop_pct", STOP_PCT_DEFAULT)))
            new_stop = qty(D(pos["entry_price"]) * (1 - new_pct))
            if new_stop > D(pos["stop_price"]):
                pos["stop_pct"], pos["stop_price"] = str(new_pct), str(new_stop)
                events.append(f"stop_raised:{new_stop}")
        else:
            events.append("hold:CASH")
    state["pending"] = None

    # 2~4) 손절 조건 주문과 장중 점검 청산
    pos = state["position"]
    if pos:
        sym = pos["symbol"]
        stop = D(pos["stop_price"])
        if opens[sym] <= stop and pos["entry_date"] != date:
            _sell(state, opens[sym] * (1 - STOP_SLIPPAGE), date, f"갭 하락 손절 (손절가 {stop})", trades)
            events.append("stop_gap")
            state["cooldown"] = f"stop_gap {date}"
        elif lows[sym] <= stop:
            _sell(state, stop * (1 - STOP_SLIPPAGE), date, f"손절 조건 주문 체결 (손절가 {stop})", trades)
            events.append("stop_hit")
            state["cooldown"] = f"stop_hit {date}"
        elif intraday and intraday["symbol"] == sym:
            price = min(max(D(intraday["price"]), lows[sym]), highs[sym])
            if price != D(intraday["price"]):
                events.append("intraday_price_clamped")
            _sell(state, price * (1 - SLIPPAGE), date,
                  f"장중 점검 청산 {intraday['time']}: {intraday['reason']} ({intraday['source']})",
                  trades)
            events.append("intraday_exit")

    # 5) 종가 평가, 최대 보유일 점검
    pos = state["position"]
    close = Decimal("0")
    shares = Decimal("0")
    if pos:
        pos["days_held"] += 1
        close = closes[pos["symbol"]]
        shares = D(pos["shares"])
        if pos["days_held"] >= MAX_HOLD_DAYS:
            state["forced_exit"] = f"max_hold {pos['days_held']}d"
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
        "stop_price": pos["stop_price"] if pos else "",
        "cash_usd": state["cash_usd"],
        "equity_usd": str(equity),
        "fx": str(fx),
        "equity_krw": str((equity * fx).quantize(Decimal("1"), rounding=ROUND_HALF_UP)),
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
        f"보유: {pos['symbol']} {pos['shares']}주 @ {pos['entry_price']} "
        f"(손절가 {pos['stop_price']}, {pos['days_held']}일째)" if pos else "보유: 현금"
    )
    lines.append(f"오늘 시가 이후 보유 예정: {held_after_open(state)}")
    if state["pending"]:
        p = state["pending"]
        lines.append(f"대기 중 판단: {p['action']} 비중 {p['weight']} 손절폭 {p['stop_pct']} - {p['reason']}")
    if state["forced_exit"]:
        lines.append(f"다음 시가 강제 청산 예정: {state['forced_exit']}")
    if state["cooldown"]:
        lines.append(f"손절 후 현금 대기: {state['cooldown']}")
    if state["intraday"]:
        i = state["intraday"]
        lines.append(f"장중 청산 기록: {i['symbol']} @ {i['price']} {i['time']} - {i['reason']}")
    daily = data_dir / "daily.csv"
    if daily.exists():
        rows = list(csv.DictReader(daily.open(encoding="utf-8")))
        if rows:
            r = rows[-1]
            lines.append(
                f"평가액: ${r['equity_usd']} ({int(D(r['equity_krw'])):,}원, {r['return_pct']}%)  "
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
    p.add_argument("--stop-pct", default=str(STOP_PCT_DEFAULT))
    p.add_argument("--reason", required=True)
    p = sub.add_parser("intraday")
    p.add_argument("--date", required=True)
    p.add_argument("--price", required=True)
    p.add_argument("--time", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--reason", required=True)
    p = sub.add_parser("settle")
    p.add_argument("--date", required=True)
    p.add_argument("--open", required=True)
    p.add_argument("--high", required=True)
    p.add_argument("--low", required=True)
    p.add_argument("--close", required=True)
    p.add_argument("--fx")
    sub.add_parser("status")
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir)
    try:
        if args.cmd == "init":
            cmd_init(data_dir, args.date, D(args.krw), D(args.fx))
        elif args.cmd == "decide":
            cmd_decide(data_dir, args.date, args.action, D(args.weight), args.reason, D(args.stop_pct))
        elif args.cmd == "intraday":
            cmd_intraday(data_dir, args.date, D(args.price), args.time, args.source, args.reason)
        elif args.cmd == "settle":
            need = SYMBOLS + ("SOXX",)
            cmd_settle(data_dir, args.date, parse_prices(args.open, need),
                       parse_prices(args.high, SYMBOLS), parse_prices(args.low, SYMBOLS),
                       parse_prices(args.close, need), D(args.fx) if args.fx else None)
        print(cmd_status(data_dir))
    except LedgerError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
