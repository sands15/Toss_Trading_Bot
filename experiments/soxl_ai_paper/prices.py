"""CBOE 지연 시세(15분)로 SOXL/SOXS/SOXX/VIX 가격을 받아온다 (API 키 불필요).

사용:
  python3 prices.py                 # 최근 정규장 시세 요약 (JSON)
  python3 prices.py --settle-args   # ledger.py settle 에 넣을 인자 출력
  python3 prices.py --expect-date 2026-11-02 --settle-args
                                    # 시세의 거래일이 기대 날짜와 다르면 오류(종료코드 2)

주의:
- 정규장 기준 시가/고가/저가/종가만 사용한다(프리·애프터·주간거래 제외).
- 지연 시세라 장 마감 직후에는 종가가 확정되지 않았을 수 있다.
  final=false 이면 정산에 쓰지 말 것.
- 과거 날짜 시세는 제공하지 않는다. 빠진 날은 웹에서 두 곳 이상 확인한다.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BASE = "https://cdn-api.cboe.com/api/global/delayed_quotes/quotes/{}.json"
SYMBOLS = {"SOXL": "SOXL", "SOXS": "SOXS", "SOXX": "SOXX", "VIX": "_VIX"}
ET = ZoneInfo("America/New_York")
FIELDS = ("open", "high", "low", "close", "current_price", "prev_day_close",
          "price_change_percent", "volume", "last_trade_time")


def fetch(symbol: str, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(BASE.format(symbol), headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.load(resp)
    data = payload["data"]
    out = {k: data.get(k) for k in FIELDS}
    out["timestamp_utc"] = payload.get("timestamp")
    return out


def session_info(quote: dict) -> tuple[str, bool]:
    """(거래일 YYYY-MM-DD, 종가 확정 여부)."""
    last = datetime.fromisoformat(quote["last_trade_time"]).replace(tzinfo=ET)
    stamp = datetime.fromisoformat(quote["timestamp_utc"]).replace(tzinfo=timezone.utc).astimezone(ET)
    close_time = last.replace(hour=16, minute=0, second=0)
    final = last.time() >= datetime.strptime("15:59", "%H:%M").time() and stamp >= close_time + timedelta(minutes=20)
    return last.date().isoformat(), final


def collect() -> dict:
    quotes = {name: fetch(code) for name, code in SYMBOLS.items()}
    date, final = session_info(quotes["SOXL"])
    for name in ("SOXS", "SOXX"):
        d2, f2 = session_info(quotes[name])
        if d2 != date:
            raise RuntimeError(f"{name} 거래일({d2})이 SOXL({date})과 다릅니다.")
        final = final and f2
    return {"session_date": date, "final": final, "source": "CBOE delayed quotes", "quotes": quotes}


def settle_args(info: dict) -> str:
    q = info["quotes"]
    fmt = lambda key, names: ",".join(f"{n}={q[n][key]}" for n in names)
    trio, duo = ("SOXL", "SOXS", "SOXX"), ("SOXL", "SOXS")
    return (f"--date {info['session_date']} --open {fmt('open', trio)} --high {fmt('high', duo)} "
            f"--low {fmt('low', duo)} --close {fmt('close', trio)}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--settle-args", action="store_true")
    p.add_argument("--expect-date")
    args = p.parse_args(argv)
    try:
        info = collect()
    except Exception as exc:  # 네트워크/형식 오류는 그대로 알린다
        print(f"오류: 시세 수집 실패 - {exc}", file=sys.stderr)
        return 1
    if args.expect_date and info["session_date"] != args.expect_date:
        print(f"오류: 시세 거래일 {info['session_date']} ≠ 기대 {args.expect_date}", file=sys.stderr)
        return 2
    if args.settle_args:
        if not info["final"]:
            print("오류: 종가가 아직 확정되지 않았습니다(final=false). 20분 뒤 다시 시도하세요.", file=sys.stderr)
            return 3
        print(settle_args(info))
    else:
        print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
