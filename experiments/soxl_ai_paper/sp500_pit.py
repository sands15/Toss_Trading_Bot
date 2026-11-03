"""본체 모멘텀 엔진(src/turtle_bot/momentum_backtest.py)용 데이터 준비: 과거 S&P 500 구성 종목 + CBOE 일봉.

입력: fja05680/sp500 (MIT) 의 "S&P 500 Historical Components & Changes (Updated).csv"
      (date, tickers 열. 구성 변경이 있던 날짜별 전체 구성 종목 목록)
출력:
  <out>/candles/<SYMBOL>.csv   timestamp,symbol,open,high,low,close,volume,currency,adjusted,source
  <out>/pit_universe.csv        as_of,symbol,included,... (매 거래일 × 그날 구성 종목)
  <out>/coverage.json           CBOE에서 못 받은 종목 목록 (상장폐지·인수·티커 변경 등)

한계: CBOE에 없는 상장폐지 종목은 빠진다(생존 편향이 일부 남음). 구성 종목표가 끝난 날 이후는
마지막 구성으로 이어 쓴다.

사용:
  python3 sp500_pit.py --components "<csv 경로>" --out ~/.cache/soxl_paper_sp500 --start 2014-01-01
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "vendor" / "tradermonty"))
from cboe_fmp_client import HIST_URL, _get_json, cboe_symbol  # noqa: E402

from backtest import clean_spikes  # noqa: E402


def load_components(path: Path) -> list[tuple[str, frozenset[str]]]:
    out = []
    with path.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            out.append((r["date"], frozenset(t.strip() for t in r["tickers"].split(",") if t.strip())))
    return sorted(out)


def fetch_hist(sym: str, cache: Path) -> list[dict] | None:
    f = cache / f"{sym.replace('/', '_')}.json"
    if f.exists():
        return json.loads(f.read_text()) or None
    try:
        raw = _get_json(HIST_URL.format(cboe_symbol(sym)))
        rows = [{"date": r["date"], "open": float(r["open"]), "high": float(r["high"]),
                 "low": float(r["low"]), "close": float(r["close"]), "volume": float(r.get("volume") or 0)}
                for r in raw.get("data", [])]
    except Exception:  # noqa: BLE001 - 없는 종목은 빈 결과로 기록
        rows = []
    f.write_text(json.dumps(rows))
    return rows or None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--components", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--start", default="2014-01-01", help="일봉 저장 시작일 (지표 계산용 여유 포함)")
    p.add_argument("--pit-start", default="2015-01-01", help="PIT 구성표 시작일")
    p.add_argument("--workers", type=int, default=4)
    args = p.parse_args(argv)
    out = Path(args.out).expanduser()
    cache, cdir = out / "raw", out / "candles"
    cache.mkdir(parents=True, exist_ok=True)
    cdir.mkdir(parents=True, exist_ok=True)

    comps = load_components(Path(args.components))
    comp_dates = [d for d, _ in comps]
    tickers = sorted({t for d, ts in comps if d >= args.start for t in ts}
                     | set(next(ts for d, ts in reversed(comps) if d <= args.start)) | {"SPY"})
    with ThreadPoolExecutor(args.workers) as ex:
        hists = dict(zip(tickers, ex.map(lambda s: fetch_hist(s, cache), tickers)))
    missing = sorted(s for s, h in hists.items() if not h)

    for s, h in hists.items():
        if not h:
            continue
        rows = [r for r in clean_spikes(h) if r["date"] >= args.start]
        with (cdir / f"{s.replace('/', '_')}.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["timestamp", "symbol", "open", "high", "low", "close", "volume", "currency", "adjusted", "source"])
            for r in rows:
                w.writerow([r["date"], s, r["open"], r["high"], r["low"], r["close"], r["volume"], "USD", "true", "cboe"])

    trading_days = [r["date"] for r in hists["SPY"] if r["date"] >= args.pit_start]
    with (out / "pit_universe.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["as_of", "symbol", "included", "reasons", "market", "instrument_type"])
        for d in trading_days:
            k = bisect.bisect_right(comp_dates, d) - 1
            for s in sorted(comps[k][1]):
                w.writerow([d, s, "true", "sp500_member", "US", "stock"])

    member_days = {}
    for d in trading_days:
        k = bisect.bisect_right(comp_dates, d) - 1
        for s in comps[k][1]:
            member_days[s] = member_days.get(s, 0) + 1
    covered = sum(v for s, v in member_days.items() if s not in missing)
    report = {"tickers": len(tickers), "missing": missing, "missing_count": len(missing),
              "member_day_coverage": round(covered / max(1, sum(member_days.values())), 4),
              "components_last_date": comp_dates[-1], "trading_days": len(trading_days)}
    (out / "coverage.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k != "missing"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
