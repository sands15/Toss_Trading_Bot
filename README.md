# 버핏 펀드 (모의투자)

- 3시간마다 미국 증시 주요 뉴스를 수집해(`news/`) 버핏식 원칙(`STRATEGY.md`)으로 판단한다.
- 500만원 가상 포트폴리오를 운용하고, 성과는 SPY 전액 매수와 비교한다.
- 현황: [REPORT.md](REPORT.md) · 일지: [JOURNAL.md](JOURNAL.md) · 매매기록: [data/trades.csv](data/trades.csv) · 수익률 추이: [data/nav.csv](data/nav.csv)

외부 의존성 없이 Python 3.11 이상 표준 라이브러리만 쓴다. 시세는 Yahoo Finance 차트 API, 뉴스는 MarketWatch, WSJ, Nasdaq, Seeking Alpha, Investing.com의 RSS에서 가져온다.
