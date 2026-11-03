"""규칙 기반 SOXL/SOXS/현금 전략 백테스트 (CBOE 일봉 + tradermonty 감지기).

공정성 원칙
- t일 종가까지의 데이터만으로 신호를 계산하고, t+1일 시가에 매매한다(미래 데이터 사용 금지).
- 손절은 진입가 대비 -N% 가격에 조건주문이 걸린 것으로 보고, 장중 저가가 닿으면 체결
  (시가부터 아래로 갭이면 시가). 손절 체결은 0.3% 불리하게, 일반 매매는 0.05% 슬리피지 + 수수료 0.1%.
- AI 뉴스 판단은 재현할 수 없어 테스트하지 않는다(과거 뉴스 시점 복원 불가, 사후 지식 편향).

한계
- 시장 폭(breadth) 구성요소는 데이터가 없어 제외(감지기 기본 처리: 데이터 없음).
- 주도 ETF 바스켓은 원본 기본 바스켓 고정(원본의 동적 선택은 생략). 상장 전 ETF는 빠진다.
- CBOE 일봉의 하루짜리 오류값(전후로 급변 후 원복)은 제거한다.
- 레버리지 ETF 비용(운용보수 등)은 가격에 이미 반영돼 있다. 세금·환전 비용은 반영하지 않는다.

사용:
  python3 backtest.py                       # 2015-01-01 ~ 최신, 결과를 표로 출력
  python3 backtest.py --start 2020-01-01 --out backtest_results.md
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENDOR = HERE / "vendor" / "tradermonty"
sys.path[:0] = [str(VENDOR), str(VENDOR / "ftd-detector"), str(VENDOR / "market-top-detector")]

from cboe_fmp_client import CboeFMPClient  # noqa: E402
from post_ftd_monitor import assess_post_ftd_health  # noqa: E402
from rally_tracker import get_market_state  # noqa: E402
from calculators.breadth_calculator import calculate_breadth_divergence  # noqa: E402
from calculators.defensive_rotation_calculator import DEFENSIVE_ETFS, OFFENSIVE_ETFS, calculate_defensive_rotation  # noqa: E402
from calculators.distribution_day_calculator import calculate_distribution_days  # noqa: E402
from calculators.index_technical_calculator import calculate_index_technical  # noqa: E402
from calculators.leading_stock_calculator import DEFAULT_LEADING_ETFS, calculate_leading_stock_health  # noqa: E402
from calculators.sentiment_calculator import calculate_sentiment  # noqa: E402
from scorer import calculate_composite_score  # noqa: E402

COMMISSION = 0.001
SLIPPAGE = 0.0005
STOP_SLIPPAGE = 0.003
LONG_OK_STATES = {"NO_SIGNAL", "FTD_CONFIRMED"}
BEAR_STATES = {"CORRECTION", "RALLY_FAILED", "FTD_INVALIDATED"}


# ----------------------------------------------------------------------------- data
def clean_spikes(rows: list[dict]) -> list[dict]:
    """하루만 튀었다가 원래대로 돌아오는 오류값 제거."""
    out = []
    for i, r in enumerate(rows):
        if 0 < i < len(rows) - 1:
            p, n = rows[i - 1]["close"], rows[i + 1]["close"]
            if p > 0 and r["close"] > 0:
                up, back = r["close"] / p, n / r["close"]
                if (up > 2.5 or up < 0.4) and (back > 2.5 or back < 0.4) and 0.6 < n / p < 1.6:
                    continue
        if r["close"] > 0 and r["low"] > 0:
            out.append(r)
    return out


def load_all(symbols: list[str], cache_dir: Path) -> dict[str, list[dict]]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    client = CboeFMPClient()
    data = {}
    for s in symbols:
        f = cache_dir / f"{s.replace('^', '_')}.json"
        if f.exists():
            rows = json.loads(f.read_text())
        else:
            rows = client._rows_oldest_first(s)
            f.write_text(json.dumps(rows))
        data[s] = clean_spikes(rows)
    return data


class PIT:
    """특정 날짜까지만 보이는 데이터 뷰 (most-recent-first, FMP 형식)."""

    def __init__(self, data: dict[str, list[dict]]):
        self.data = data
        self.idx = {s: {r["date"]: i for i, r in enumerate(rows)} for s, rows in data.items()}

    def hist(self, sym: str, date: str, days: int) -> list[dict]:
        rows = self.data.get(sym, [])
        i = self.idx[sym].get(date)
        if i is None:  # 그 날짜가 없으면 그 이전 마지막 행까지
            lo = [k for k, r in enumerate(rows) if r["date"] <= date]
            if not lo:
                return []
            i = lo[-1]
        out = rows[max(0, i - days + 1): i + 1][::-1]
        return [dict(r, adjClose=r["close"]) for r in out]

    def quote(self, sym: str, date: str) -> dict | None:
        h = self.hist(sym, date, 252)
        if not h:
            return None
        vols = [r["volume"] for r in h[:50] if r["volume"]]
        return {"symbol": sym, "price": h[0]["close"], "yearHigh": max(r["high"] for r in h),
                "yearLow": min(r["low"] for r in h), "volume": h[0]["volume"],
                "avgVolume": sum(vols) / len(vols) if vols else None,
                "previousClose": h[1]["close"] if len(h) > 1 else None}


def signals_for(pit: PIT, date: str) -> dict:
    sp, qqq = pit.hist("^GSPC", date, 260), pit.hist("QQQ", date, 260)
    ms = get_market_state(sp[:80], qqq[:80])
    ms = assess_post_ftd_health(ms, list(reversed(sp[:80])), list(reversed(qqq[:80])))
    c1 = calculate_distribution_days(sp, qqq)
    lead = [e for e in DEFAULT_LEADING_ETFS if pit.hist(e, date, 60)]
    c2 = calculate_leading_stock_health({e: pit.quote(e, date) for e in lead},
                                        {e: pit.hist(e, date, 60) for e in lead}, etf_list=lead)
    sectors = {e: pit.hist(e, date, 50) for e in set(DEFENSIVE_ETFS + OFFENSIVE_ETFS) if e != "QQQ"}
    sectors = {k: v for k, v in sectors.items() if len(v) >= 21}
    sectors["QQQ"] = qqq[:50]
    c3 = calculate_defensive_rotation(sectors)
    spq = pit.quote("^GSPC", date)
    dist = (spq["price"] - spq["yearHigh"]) / spq["yearHigh"] * 100 if spq and spq["yearHigh"] else 0
    c4 = calculate_breadth_divergence(breadth_200dma=None, breadth_50dma=None, index_distance_from_high_pct=dist)
    c5 = calculate_index_technical(sp, qqq, sp500_quote=spq, nasdaq_quote=pit.quote("QQQ", date))
    vix, vix3m = pit.quote("^VIX", date), pit.quote("^VIX3M", date)
    term = None
    if vix and vix3m and vix3m["price"]:
        ratio = vix["price"] / vix3m["price"]
        term = ("steep_contango" if ratio < 0.85 else "contango" if ratio < 0.95
                else "flat" if ratio <= 1.05 else "backwardation")
    c6 = calculate_sentiment(vix_level=vix["price"] if vix else None, put_call_ratio=None,
                             vix_term_structure=term, margin_debt_yoy_pct=None)
    comps = {"distribution_days": c1, "leading_stocks": c2, "defensive_rotation": c3,
             "breadth_divergence": c4, "index_technical": c5, "sentiment": c6}
    composite = calculate_composite_score({k: v["score"] for k, v in comps.items()},
                                          {k: v.get("data_available", True) for k, v in comps.items()})
    return {"date": date, "ftd_state": ms["combined_state"],
            "ftd_quality": ms.get("quality_score", {}).get("total_score", 0),
            "top_score": composite["composite_score"]}


# ------------------------------------------------------------------------- strategy
def target(name: str, s: dict) -> str:
    long_ok = s["ftd_state"] in LONG_OK_STATES
    top_ok = s["top_score"] < 61
    if name == "SOXL 단순 보유":
        return "SOXL"
    if name == "SOXX 단순 보유":
        return "SOXX"
    if name == "FTD 필터":
        return "SOXL" if long_ok else "CASH"
    if name == "고점위험 필터":
        return "SOXL" if top_ok else "CASH"
    if name == "결합 필터":
        return "SOXL" if long_ok and top_ok else "CASH"
    if name == "결합 + SOXS":
        if long_ok and top_ok:
            return "SOXL"
        if s["ftd_state"] in BEAR_STATES and not top_ok:
            return "SOXS"
        return "CASH"
    raise ValueError(name)


def simulate(name: str, sigs: list[dict], px: dict[str, dict[str, dict]], stop_pct: float | None) -> dict:
    """sigs[i] 신호 → dates[i+1] 시가에 실행."""
    eq, cash, sym, shares, entry_stop = 1.0, 1.0, "CASH", 0.0, None
    curve, trades, cooldown = [], [], False
    entry_val = 0.0
    for i in range(len(sigs) - 1):
        d = sigs[i + 1]["date"]
        want = target(name, sigs[i])
        if cooldown and want != "CASH":
            want = "CASH"
        cooldown = False
        bars = {k: px[k].get(d) for k in ("SOXL", "SOXS", "SOXX")}
        if any(bars[k] is None for k in ("SOXL", "SOXS", "SOXX")):
            curve.append((d, curve[-1][1] if curve else 1.0))
            continue
        # 시가 매매
        if want != sym:
            if sym != "CASH":
                fill = bars[sym]["open"] * (1 - SLIPPAGE)
                cash += shares * fill * (1 - COMMISSION)
                trades.append(cash - entry_val)
                shares, sym = 0.0, "CASH"
            if want != "CASH":
                fill = bars[want]["open"] * (1 + SLIPPAGE)
                entry_val = cash
                shares = cash * (1 - COMMISSION) / fill
                cash, sym = 0.0, want
                entry_stop = fill * (1 - stop_pct) if stop_pct else None
        # 장중 손절
        if sym != "CASH" and entry_stop is not None:
            b = bars[sym]
            if b["open"] <= entry_stop or b["low"] <= entry_stop:
                fill = min(b["open"], entry_stop) * (1 - STOP_SLIPPAGE)
                cash += shares * fill * (1 - COMMISSION)
                trades.append(cash - entry_val)
                shares, sym, entry_stop, cooldown = 0.0, "CASH", None, True
        eq = cash + (shares * bars[sym]["close"] if sym != "CASH" else 0.0)
        curve.append((d, eq))
    if sym != "CASH":
        trades.append(eq - entry_val)
    return {"name": name, "stop": stop_pct, "curve": curve, "trades": trades,
            "exposure": None}


def metrics(res: dict, sigs: list[dict]) -> dict:
    curve = res["curve"]
    if not curve:
        return {}
    vals = [v for _, v in curve]
    peak, mdd = vals[0], 0.0
    for v in vals:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    years = len(vals) / 252
    total = vals[-1] - 1
    cagr = vals[-1] ** (1 / years) - 1 if years > 0 and vals[-1] > 0 else -1
    wins = [t for t in res["trades"] if t > 0]
    held = sum(1 for i in range(len(sigs) - 1) if target(res["name"], sigs[i]) != "CASH")
    return {"total": total, "cagr": cagr, "mdd": mdd, "trades": len(res["trades"]),
            "win": len(wins) / len(res["trades"]) if res["trades"] else math.nan,
            "exposure": held / max(1, len(sigs) - 1)}


# ------------------------------------------------------------------------------ main
STRATS = ["SOXL 단순 보유", "SOXX 단순 보유", "FTD 필터", "고점위험 필터", "결합 필터", "결합 + SOXS"]


def run(start: str, end: str | None, cache_dir: Path) -> tuple[list[dict], list[dict]]:
    syms = sorted(set(["SOXL", "SOXS", "SOXX", "^GSPC", "QQQ", "SPY", "^VIX", "^VIX3M"]
                      + DEFAULT_LEADING_ETFS + DEFENSIVE_ETFS + OFFENSIVE_ETFS))
    data = load_all(syms, cache_dir)
    pit = PIT(data)
    px = {k: {r["date"]: r for r in data[k]} for k in ("SOXL", "SOXS", "SOXX")}
    dates = [r["date"] for r in data["SOXL"] if r["date"] >= start and (end is None or r["date"] <= end)
             and r["date"] in px["SOXS"] and r["date"] in px["SOXX"]]
    sigs = []
    for k, d in enumerate(dates):
        sigs.append(signals_for(pit, d))
        if k % 250 == 0:
            print(f"  신호 계산 {d} ({k}/{len(dates)})", file=sys.stderr)
    rows = []
    for name in STRATS:
        for stop in ((None,) if "단순 보유" in name else (None, 0.10)):
            res = simulate(name, sigs, px, stop)
            rows.append({"name": name, "stop": stop, **metrics(res, sigs), "final": res["curve"][-1][1]})
    return sigs, rows


def table(rows: list[dict], title: str) -> str:
    out = [f"### {title}", "", "| 전략 | 손절 | 총수익률 | 연평균 | 최대낙폭 | 시장노출 | 매매수 | 승률 |",
           "|---|---|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        out.append(f"| {r['name']} | {'-10%' if r['stop'] else '없음'} | {r['total']:+.0%} | {r['cagr']:+.1%} | "
                   f"{r['mdd']:.0%} | {r['exposure']:.0%} | {r['trades']} | "
                   f"{'' if math.isnan(r['win']) else format(r['win'], '.0%')} |")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", default="2015-01-01")
    p.add_argument("--end")
    p.add_argument("--periods", default="2015-01-01:2026-12-31,2018-01-01:2019-12-31,2020-01-01:2021-12-31,"
                                        "2022-01-01:2022-12-31,2023-01-01:2024-12-31,2025-01-01:2026-12-31")
    p.add_argument("--cache-dir", default=os.path.expanduser("~/.cache/soxl_paper_cboe"))
    p.add_argument("--out")
    args = p.parse_args(argv)
    sigs, _ = run(args.start, args.end, Path(args.cache_dir))
    parts = [f"# 규칙 전략 백테스트 ({sigs[0]['date']} ~ {sigs[-1]['date']})", "",
             "t일 종가 신호 → t+1일 시가 매매, 수수료 0.1% + 슬리피지 0.05%, 손절 체결 0.3% 불리. 100% 노출 기준.", ""]
    cache = Path(args.cache_dir)
    data = {k: clean_spikes(json.loads((cache / f"{k}.json").read_text())) for k in ("SOXL", "SOXS", "SOXX")}
    px = {k: {r["date"]: r for r in rows} for k, rows in data.items()}
    for spec in args.periods.split(","):
        a, b = spec.split(":")
        sub = [s for s in sigs if a <= s["date"] <= b]
        if len(sub) < 30:
            continue
        rows = []
        for name in STRATS:
            for stop in ((None,) if "단순 보유" in name else (None, 0.10)):
                res = simulate(name, sub, px, stop)
                rows.append({"name": name, "stop": stop, **metrics(res, sub)})
        parts += [table(rows, f"{sub[0]['date']} ~ {sub[-1]['date']}"), ""]
    from collections import Counter
    st = Counter(s["ftd_state"] for s in sigs)
    parts += ["### 신호 분포", "", "FTD 상태: " + ", ".join(f"{k} {v}일" for k, v in st.most_common()),
              f"고점위험 61점 이상(Red 이상): {sum(1 for s in sigs if s['top_score'] >= 61)}일 / {len(sigs)}일", ""]
    text = "\n".join(parts)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
