from __future__ import annotations

import argparse
from pathlib import Path

from . import news
from .portfolio import Portfolio, render

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m buffett")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("news", help="collect headlines")
    sub.add_parser("init", help="create 5,000,000 KRW paper portfolio")
    sub.add_parser("status", help="value portfolio, append nav, write REPORT.md")
    b = sub.add_parser("buy"); b.add_argument("symbol"); b.add_argument("krw", type=float); b.add_argument("reason")
    s = sub.add_parser("sell"); s.add_argument("symbol"); s.add_argument("fraction", type=float); s.add_argument("reason")
    a = p.parse_args()

    if a.cmd == "news":
        items, errors = news.collect()
        path = news.save(items, errors, ROOT)
        print(f"{len(items)} headlines -> {path.relative_to(ROOT)}" + (f" (failed: {list(errors)})" if errors else ""))
        return
    pf = Portfolio(ROOT)
    if a.cmd == "init":
        pf.init(); print("initialised")
    elif a.cmd == "buy":
        print(pf.buy(a.symbol, a.krw, a.reason))
    elif a.cmd == "sell":
        print(pf.sell(a.symbol, a.fraction, a.reason))
    elif a.cmd == "status":
        v = pf.valuation(); pf.record_nav(v)
        text = render(v)
        (ROOT / "REPORT.md").write_text("# 버핏 펀드 현황\n\n" + text)
        print(text)


if __name__ == "__main__":
    main()
