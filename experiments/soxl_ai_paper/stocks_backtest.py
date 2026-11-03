"""개별 주식(+ETF) 상대강도 모멘텀 순환 매매 백테스트.

- 대상(생존 편향 완화): 2014년 말 미국 시가총액 상위 34개 종목으로 고정(작성자 기억 기반 목록).
  그 뒤 부진하거나 쪼개진 종목(GE, IBM, XOM, INTC, T 등)도 그대로 포함한다.
  단, 2014년 이후 새로 커진 종목(NVDA, TSLA, AVGO 등)은 들어가지 않는다 — 의도된 것.
- "+ETF" 버전: 위 종목에 QQQ, SOXX, USD(반도체 2배), TQQQ(나스닥 3배), TLT(장기 국채)를 섞는다.
- 규칙: 21거래일마다 t일 종가 기준 점수 = close[t-21] / close[t-126] - 1 (최근 1개월 제외 6개월 상승률).
  양수인 상위 5개를 같은 비중으로 t+1일 시가에 산다. 시장 필터가 꺼지면 다음 날 시가에 전량 현금화,
  다시 켜지면 그날 순위로 재진입.
- 비용: 거래 금액의 0.1% 수수료 + 0.05% 슬리피지. 배당은 미반영(가격 수익률) — 모든 전략에 같은 조건.

사용: python3 stocks_backtest.py [--start 2015-01-01] [--out stocks_backtest_results.md]
"""

from __future__ import annotations

import argparse
import math
import os
from pathlib import Path

import backtest as B

UNIVERSE_2014 = ("AAPL MSFT XOM GOOGL BRK.B JNJ WFC GE WMT JPM PG META CVX VZ PFE KO ORCL T BAC MRK "
                 "INTC DIS CSCO AMZN GILD HD IBM PEP CMCSA C QCOM V MA AMGN").split()
PLUS_ETFS = ["QQQ", "SOXX", "USD", "TQQQ", "TLT"]
COST = B.COMMISSION + B.SLIPPAGE
TOP_N, REBAL, LOOK, SKIP = 5, 21, 126, 21


class Prices:
    def __init__(self, data: dict[str, list[dict]]):
        self.rows = data
        self.by_date = {s: {r["date"]: r for r in rows} for s, rows in data.items()}
        self.pos = {s: {r["date"]: i for i, r in enumerate(rows)} for s, rows in data.items()}

    def score(self, s: str, d: str) -> float | None:
        k = self.pos[s].get(d)
        if k is None or k < LOOK:
            return None
        rows = self.rows[s]
        a, b = rows[k - LOOK]["close"], rows[k - SKIP]["close"]
        return b / a - 1 if a > 0 else None


def run_strategy(px: Prices, dates: list[str], universe: list[str], filt, top_n: int = TOP_N,
                 phase: int = 0) -> dict:
    """filt(i) -> bool: dates[i] 종가 기준 시장 필터 on/off. None이면 필터 없음."""
    cash, shares = 1.0, {}
    target: list[str] = []
    curve, invested_days, turnover = [], 0, 0.0
    for i in range(len(dates) - 1):
        d, nd = dates[i], dates[i + 1]
        on = True if filt is None else filt(i)
        # t일 종가에 다음 목표 결정
        if not on:
            new_target = []
        elif (i + phase) % REBAL == 0 or not target:
            ranked = [(px.score(s, d), s) for s in universe]
            ranked = sorted([(v, s) for v, s in ranked if v is not None and v > 0], reverse=True)
            new_target = [s for _, s in ranked[:top_n]]
        else:
            new_target = target
        # t+1일 시가 체결 (목표가 바뀔 때만)
        if new_target != target:
            opens = {s: px.by_date[s].get(nd, {}).get("open") for s in set(shares) | set(new_target)}
            if all(opens.get(s) for s in set(shares) | set(new_target)):
                equity = cash + sum(q * opens[s] for s, q in shares.items())
                want = {s: equity / len(new_target) for s in new_target} if new_target else {}
                traded = sum(abs(want.get(s, 0) - shares.get(s, 0) * opens[s]) for s in set(shares) | set(want))
                equity -= traded * COST
                turnover += traded
                want = {s: equity / len(new_target) for s in new_target} if new_target else {}
                shares = {s: v / opens[s] for s, v in want.items()}
                cash = 0.0 if new_target else equity
                target = new_target
        closes = {s: px.by_date[s].get(nd, {}).get("close") for s in shares}
        eq = cash + sum(q * (closes[s] or 0) for s, q in shares.items())
        curve.append(eq)
        invested_days += 1 if shares else 0
    return {"curve": curve, "exposure": invested_days / max(1, len(curve)), "turnover": turnover}


def buy_hold(px: Prices, dates: list[str], names: list[str]) -> dict:
    names = [s for s in names if dates[1] in px.by_date[s]]
    alloc = 1.0 / len(names) * (1 - COST)
    sh = {s: alloc / px.by_date[s][dates[1]]["open"] for s in names}
    curve, last = [], {}
    for d in dates[1:]:
        for s in names:
            r = px.by_date[s].get(d)
            if r:
                last[s] = r["close"]
        curve.append(sum(q * last.get(s, 0) for s, q in sh.items()))
    return {"curve": curve, "exposure": 1.0, "turnover": 0.0}


def stats(res: dict) -> dict:
    v = res["curve"]
    peak, mdd = v[0], 0.0
    for x in v:
        peak = max(peak, x)
        mdd = min(mdd, x / peak - 1)
    yrs = len(v) / 252
    return {"total": v[-1] - 1, "cagr": v[-1] ** (1 / yrs) - 1 if v[-1] > 0 else -1, "mdd": mdd,
            "exposure": res["exposure"]}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", default="2015-01-01")
    p.add_argument("--cache-dir", default=os.path.expanduser("~/.cache/soxl_paper_cboe"))
    p.add_argument("--out")
    args = p.parse_args(argv)
    cache = Path(args.cache_dir)
    sigs, _ = B.run(args.start, None, cache)  # FTD·고점위험 신호 (그날 종가까지만 사용)
    data = B.load_all(UNIVERSE_2014 + PLUS_ETFS + ["SPY"], cache)
    px = Prices(data)
    dates = [s["date"] for s in sigs if s["date"] in px.by_date["SPY"]]
    sig_by = {s["date"]: s for s in sigs}
    spy = data["SPY"]
    spy_pos = {r["date"]: i for i, r in enumerate(spy)}

    def f_spy200(i):
        k = spy_pos[dates[i]]
        return k >= 200 and spy[k]["close"] > sum(r["close"] for r in spy[k - 199:k + 1]) / 200

    f_ftd = lambda i: sig_by[dates[i]]["ftd_state"] in B.LONG_OK_STATES  # noqa: E731
    f_top = lambda i: sig_by[dates[i]]["top_score"] < 61  # noqa: E731
    filters = [("필터 없음", None), ("SPY 200일선", f_spy200), ("FTD 필터", f_ftd), ("고점위험 필터", f_top)]

    lines = [f"# 개별 주식 모멘텀 순환 백테스트 ({dates[0]} ~ {dates[-1]})", "",
             "리밸런싱 날짜에 따라 결과가 크게 달라져서(운), 리밸런싱 시점을 21가지로 바꿔 돌린 **중간값**과"
             " 연평균의 최저~최고 범위를 함께 적는다.", "",
             f"21거래일마다 6개월(최근 1개월 제외) 상승률 상위 {TOP_N}개 동일 비중, t일 종가 신호 → t+1일 시가 체결,"
             " 비용 0.15%/거래, 배당 미반영.", "",
             "| 대상 | 전략 | 총수익률(중간값) | 연평균(중간값) | 연평균 범위 | 최대낙폭(중간값) | 시장노출 |",
             "|---|---|---:|---:|---:|---:|---:|"]

    def med(xs):
        xs = sorted(xs)
        return xs[len(xs) // 2]

    def row(label, strat, results):
        ms = [stats(r) for r in (results if isinstance(results, list) else [results])]
        c = [m["cagr"] for m in ms]
        rng = f"{min(c):+.1%} ~ {max(c):+.1%}" if len(ms) > 1 else "-"
        lines.append(f"| {label} | {strat} | {med([m['total'] for m in ms]):+.0%} | {med(c):+.1%} | {rng} | "
                     f"{med([m['mdd'] for m in ms]):.0%} | {med([m['exposure'] for m in ms]):.0%} |")

    for b in ("SPY", "QQQ", "SOXX"):
        row("벤치마크", f"{b} 단순 보유", buy_hold(px, dates, [b]))
    row("2014 상위 34종목", "동일 비중 단순 보유", buy_hold(px, dates, UNIVERSE_2014))
    for label, uni in (("2014 상위 34종목", UNIVERSE_2014), ("34종목 + ETF", UNIVERSE_2014 + PLUS_ETFS)):
        for fname, f in filters:
            row(label, f"모멘텀 상위 {TOP_N} · {fname}",
                [run_strategy(px, dates, uni, f, phase=ph) for ph in range(REBAL)])
    text = "\n".join(lines)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
