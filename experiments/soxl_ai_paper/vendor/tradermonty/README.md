# tradermonty 감지기 (CBOE 데이터 버전)

- 원본: https://github.com/tradermonty/claude-trading-skills (MIT, `LICENSE` 참고)
- 가져온 커밋: `d8e71ce2f0b069eb5b6fca352857c168c35e6c46` (2026-11-02)
- 가져온 스킬: `ftd-detector`, `market-top-detector`의 `scripts/` (테스트 제외)

## 원본과 다른 점

- 각 폴더의 `fmp_client.py`만 바꿨다. 원본은 Financial Modeling Prep(유료 API 키) 클라이언트이고,
  여기서는 `cboe_fmp_client.py`(CBOE 지연 시세, 키 불필요)를 같은 이름으로 내보낸다.
- 지수 거래량: CBOE 지수 일봉은 거래량이 0이라 ^GSPC 거래량은 SPY 거래량으로 대신한다.
- `market-top-detector`는 `--no-auto-breadth`로 실행한다(시장 폭 자동 수집에 pandas-market-calendars 필요).
  그래서 breadth_divergence 구성요소는 중립(50)으로 계산된다.
- 그 밖의 감지 로직 파일은 원본 그대로다. 실행은 상위 폴더의 `regime.py`로 한다.
