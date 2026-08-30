# Toss Trading Bot

## 실거래 페이지 상태

대시보드에는 `실거래` 탭이 추가되어 있습니다. 이 페이지는 토스 API 인증,
계좌 연결, 전략/런타임 모드, 종목 후보, 시장/시세 상태, 미체결 주문,
이벤트 로그, 실주문 제출 엔진 연결 여부를 한 화면에서 체크합니다.

중요: 현재 페이지는 실거래 전환을 위한 운영 준비도 화면이며, 실제 매수/매도
주문은 제출하지 않습니다. 토스 주문 API 요청/응답 계약 검증, 중복 주문 방지,
kill switch, 주문 한도, shadow 검증이 끝나기 전까지
`can_submit_live_orders`는 `false`로 유지됩니다.

토스증권 Open API 기반의 매매 전략 연구·검증 봇입니다.

처음에는 오리지널 터틀 트레이딩 규칙을 구현하는 프로젝트로 시작했고,
현재는 미국주식 상대강도 모멘텀 전략, 생존편향 방지용 point-in-time
유니버스, 토스 계좌·시세 read-only 연동, paper trading, shadow 검증까지
포함합니다.

`shadow` 모드는 실제 토스 read-only 계좌·시세 데이터를 사용하지만, 체결은
가상으로만 기록합니다. 일반·조건 주문 API adapter는 계약 검증용으로 존재하지만,
새 장전 단타 기능은 **실제 주문 runtime에 연결하지 않은 상태**입니다.

운영 목표는 macOS에서 24시간 구동하는 것이지만, 전략·백테스트·API 클라이언트·
초기 세팅 스크립트·테스트는 Windows에서도 실행되도록 유지합니다.

## 핵심 원칙

전략 규칙이 API 편의성보다 우선입니다. 구현상 규칙을 그대로 보존할 수 없으면
반드시 명시적인 예외로 표시하고, 승인 전까지 live 주문으로 넘어가지 않습니다.

## 현재 방향

- 기본 운영 환경: `launchd`로 관리되는 macOS 데몬
- 절전 방지: Amphetamine 또는 동등한 macOS 전원 정책 사용
- 개발·테스트 환경: macOS와 Windows 모두 지원
- 전략 범위: 오리지널 터틀 + 미국주식 상대강도 모멘텀 + 장전 단타 shadow 계획기
- 브로커 연동: 토스증권 Open API를 어댑터 뒤에 둠
- 안전 모드: 백테스트, read-only 계좌 동기화, paper trading, shadow 검증
- 기존 코드베이스에는 legacy live 주문 경로가 존재하지만 배포 금지 상태이며,
  새 장전 단타 경로는 주문 runtime에 연결하지 않은 `shadow-only`
- AI는 설명 전용이며 종목 선택, 포지션 크기 변경, 주문 제출에 개입할 수 없음

## 현재 구현 상태

- `Decimal` 기반 캔들, 돈치안 채널, Turtle N, System 1/System 2 진입·청산·손절,
  skip state, 0.5N 피라미딩을 포함한 터틀 전략 코어
- rate-limit queue, 시장 데이터 캐시, 관심종목 빌더, 알림, read-only 상태 서버,
  런타임 셸, SQLite 상태 저장소
- CSV 기반 일봉 백테스트 엔진: 단일 종목·포트폴리오 루프, 터틀 리스크 기반
  수량 산정, 가상 체결, 수수료·슬리피지·세금 훅, 거래 내역, 자산 곡선,
  JSON 리포트, 감사 로그
- 미국주식 상대강도 모멘텀 백테스트: `SPY` 200일선 시장 필터, 126일 모멘텀
  점수, 최근 21일 제외, 75일선 이탈 청산, 토스 미국주식 비용 모델
- 생존편향을 줄이기 위한 point-in-time 유니버스 CSV 지원
- 토스 OpenAPI read-only 클라이언트: 토큰 발급, 시세, 시장 정보, 계좌, 보유
  종목, 주문 조회, 매수 가능 금액, 매도 가능 수량, 수수료 조회
- 토스 공식 OpenAPI 응답 구조인 `result` 래퍼와 배열형 응답 처리
- 로컬 포지션과 브로커 보유·미체결 주문을 비교하는 reconciliation
- paper runtime: 먼저 계좌 상태를 대조한 뒤 전략 신호를 평가하고, 실제 주문
  대신 주문 의도와 가상 체결을 기록
- shadow runtime: 실제 토스 read-only 데이터를 사용해 주문 의도와 가상 체결을
  기록하되, 계좌의 기존 보유 종목은 경고로 남기고 검증을 계속할 수 있음
- macOS 운영용 `launchd` plist 렌더링, 런타임 디렉터리 생성, 운영 점검 명령
- 장전 관심종목 생성과 postmarket 일일 리포트
- AI 요약 경계: 뉴스, 일일 리포트, 런타임 이벤트 설명만 가능하며 매매 의사결정에는
  관여하지 않음
- 장전 단타 계획기: Toss의 USD 현금 매수 가능액, 미국장 캘린더, 현재가·호가·수수료를
  strict 검증하고 진입/익절/손절/수량을 계좌·거래일당 한 번만 immutable 저장
- 자동 단타 selector: `MARKET_TRADING_AMOUNT / US / realtime` 상위 20개를 후보 소스로만
  사용하고, NASDAQ·NYSE·AMEX의 거래 가능 보통주 교집합에서 상위 5개를 순서대로 strict
  검사한다. 이 랭킹은 프리마켓 거래대금 측정값을 뜻하지 않는다. `/warnings`의 정확한 빈 배열,
  `adjusted=false`인 이전 완료 일봉 20개, 완료된 프리마켓 1분봉, fresh 현재가·호가를 요구하고
  최종 현재가·등락률과 계좌 flat·USD 현금을 다시 확인한다. 잠금 직전에는 warnings·계좌
  flat·USD 현금을 다시 조회해 계획을 재계산하고, lock 시각 기준 현재가·호가·현금·랭킹·
  warnings·account-check freshness를 재검증한 뒤에만 한 종목을 잠근다.
- 자동 선정 결과는 계좌·미국 거래일당 한 번만 저장되며 재시작해도 재선정하지 않는다. 뉴스와
  LLM은 선정에 영향을 주지 않고, 뉴스 worker와 WebSocket은 잠긴 그 종목만 사용한다.
- 단타 실행 안전장치: 기존 가상체결 runtime과 분리, 보유·미체결 주문이 있으면 차단,
  정규장 시작 뒤에는 `intraday_execution_engine_not_enabled`, live 설정은 config 단계에서 차단
- 선택 종목 뉴스 worker: 무결성 검증된 단타 plan의 종목 정확히 하나만 redacted JSON으로 받고,
  Finnhub REST 조회 → 선택형 localhost LLM 중립 요약 → 별도 뉴스 webhook으로 전달하되 승인·거래와
  동일한 단일 허용 Discord channel만 사용
- 뉴스 격리: 독립 `turtle_news` package·`news.sqlite3`·웹훅을 사용하며 Toss secret, 거래 DB,
  거래 모듈을 거부하고 LLM 실패·악성 출력은 출처 기반 fallback으로 처리
- 선정 종목 shadow WebSocket: 잠긴 `news-context.json`의 정확한 한 종목만
  `trade:us`·`orderbook:us`로 구독하고, 단일 ACK 뒤 읽기 전용 REST 현재가·호가를
  재동기화한다. simulation overlay에서는 intraday state DB에서 immutable plan을 조회하고
  별도 paper DB에 journal/ledger를 쓰지만, 계좌번호·개인 주문 채널·주문 API·order adapter는
  사용하지 않으며 출력의 `ready_for_live_entry`는 항상 `false`
- Discord 직접 승인 worker: 독립 `turtle_approval` package가 intents `0`의 outbound Gateway로
  단일 사용자·서버·채널과 화면에 표시된 전체 계획값을 묶어 검증하고, 계획 hash 확인 modal 뒤
  완성된 파일만 no-clobber publish하는 one-shot 영수증을 기록
- 승인 격리: bot token·원 nonce·계좌번호·주문 정보는 영수증에 없고, 현재 shadow recorder는
  영수증을 거래 DB나 주문 runtime으로 소비하지 않음. 별도 offline v2 consumer는 plan
  economics/hash/Discord identity/boot·writer fence/generation/만료/latch를 다시 검증하고 SQLite에서
  원자 소비하지만 production CLI·LaunchAgent에는 연결하지 않음. 같은 macOS 사용자(UID)의 악성 process를
  막는 OS sandbox는 아니므로 이 영수증은 향후 실주문 권한 증명으로 그대로 사용할 수 없음
- 단타 lifecycle offline core: fake broker/clock/stream에서 writer fencing, stable REST A→ACK→B,
  immutable request hash, 부분체결·취소 one-shot, exact OCO broker ID/economics, 승인 만료와 전량
  emergency/force exit를 검증함. production dispatch와 live credential 연결은 하드 차단됨

## 문서

- [개발일지](docs/development-log.md)
- [MacBook LLM 뉴스·Discord 운영 설계](docs/macbook-news-llm-design.md)
- [장전 가격 계획 기반 단타 브래킷 설계](docs/intraday-bracket-design.md)
- [초기 세팅](docs/setup.md)
- [터틀 규칙](docs/turtle-rules.md)
- [시스템 아키텍처](docs/architecture.md)
- [토스 API 계약](docs/toss-api-contract.md)
- [macOS 운영](docs/macos-operations.md)
- [구현 계획](docs/implementation-plan.md)
- [참고 프로젝트 노트](docs/reference-project-notes.md)

## 빠른 시작

Windows:

```powershell
.\ops\setup-local.ps1
```

macOS 또는 Linux:

```bash
bash ops/setup-local.sh
```

그 다음 `config/local.yaml`의 `toss.account_seq`를 채우고,
`TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET` 환경변수를 설정한 뒤 `--ops-check`를
실행하면 됩니다. 전체 첫 실행 흐름은 [초기 세팅](docs/setup.md)을 보면 됩니다.

장전 단타 계획기는 [fail-closed 템플릿](config/intraday.example.yaml)을 별도 local 파일로
복사해 모든 빈 값을 직접 정한 다음 아래 순서로 확인합니다. 템플릿 값은 종목이나 손익비를
추천하지 않으며, `shadow` 외 모드는 거부됩니다.

```bash
PYTHONPATH=src python -m turtle_bot --config config/intraday.local.yaml \
  --state-db state/intraday.sqlite3 --log-dir logs --ops-check
PYTHONPATH=src python -m turtle_bot --config config/intraday.local.yaml \
  --state-db state/intraday.sqlite3 --log-dir logs --shadow-service --once
```

계속 실행하려면 마지막 `--once`만 제거합니다. 계획 가능 창 전에는 대기하고, 계획과 Discord
알림 outbox를 같은 SQLite 트랜잭션에 한 번만 저장합니다. 웹훅이 없거나 전송이 실패하면
알림은 `PENDING`으로 남아 다음 실행에서 재시도됩니다. 현재 버전은 정규장 진입 주문을
제출하지 않습니다. `strategy.intraday.selection.mode=automatic`에서는 `runtime.symbols`를 비워
두며, 구현된 selector가 계획 종목을 정합니다. 한번 저장된 계좌·거래일 plan은 재시작 뒤에도
그 종목을 유지합니다.

Discord 직접 승인을 함께 검증하려면 intraday config에 checkout 밖의
`approval-envelope.json` 경로를 지정하고 `.[approval]` extra를 설치합니다. worker는 정확한
guild/channel/user allowlist와 별도 mode-0700 inbox를 요구하며, 승인 결과는 0600 JSON
영수증 하나뿐입니다. Keychain·LaunchAgent와 E2E 절차는 [macOS 운영](docs/macos-operations.md)을
따르며, 이 기능을 켜도 현재 버전에서 실주문은 활성화되지 않습니다.

선정된 단타 종목의 뉴스만 받으려면 intraday local config의 `news_context_path`와 뉴스 worker
config의 `context_path`를 같은 `news-context.json`으로 맞춥니다. 뉴스는 별도 webhook·자격증명을
쓰지만 승인·거래 알림과 동일한 단일 허용 channel로만 보냅니다. 비밀값은 JSON에 쓰지 않고
`FINNHUB_API_KEY`, `DISCORD_NEWS_WEBHOOK_URL`, `DISCORD_ALLOWED_CHANNEL_ID`, 선택형
`TURTLE_AI_API_KEY` 환경변수로만 전달합니다. worker는 webhook metadata의 channel ID가
allowlist와 다르면 기사 조회와 메시지 전송 전에 중단합니다.

```bash
cp config/news-digest.example.json config/news-digest.local.json
PYTHONPATH=src python -m turtle_news --config config/news-digest.local.json
```

이 명령은 WebSocket이 아니라 기본 15분 `launchd` 주기에 맞춘 one-shot REST polling입니다.
실시간·완전 수집을 보장하지 않으며 뉴스와 LLM은 종목·가격·수량·매매 허용 여부를 바꾸지
않습니다. Discord 전송은 정상 중복을 억제하지만 성공 응답 유실 시 한 번 중복될 수 있는
at-least-once 계약입니다. 상세 운영 절차는 [macOS 운영](docs/macos-operations.md)을 따릅니다.

선정 종목 시세·호가 shadow stream은 뉴스 polling과 별도 프로세스입니다. `stream` extra를
설치하고 planner가 계속 갱신하는 동일한 절대 `news-context.json`을 입력으로 사용합니다.
연결·재연결마다 OAuth → WebSocket 선언/단일 ACK → `/prices`·`/orderbook` 순서로 확인하고,
60초마다 JSON이 아닌 텍스트 `PING`을 보냅니다. `--once`는 ACK와 REST baseline만 검증한 뒤
소켓을 닫으며 주문을 만들지 않습니다.

```bash
.venv/bin/python -m pip install -e ".[stream]"
PYTHONPATH=src .venv/bin/python -m turtle_bot.toss_stream \
  --context /ABSOLUTE/PRIVATE/RUNTIME/news-context.json \
  --snapshot /ABSOLUTE/PRIVATE/RUNTIME/market-stream.json \
  --once
```

Mac 상시 실행에서는 credential을 셸에 직접 입력하지 말고 제공된 Keychain wrapper와
shadow 전용 LaunchAgent를 사용합니다. 설치·SSH 점검 절차는 [macOS 운영](docs/macos-operations.md)에
있습니다. 자동 selector는 구현되어 있지만 stream 자체는 선정 로직을 갖지 않으며 immutable
plan이 잠근 종목만 그대로 따릅니다. 현재 Toss REST 계약에는 미국 종목의 authoritative
halt/LULD 상태 필드가 없으므로 자동 계획과 stream은 계속 `shadow-only`이고 live 승격은
차단됩니다. 또한 Toss는 broker 계좌·시장 데이터와 로컬 SQLite 잠금을 하나의 원자 snapshot으로
제공하지 않습니다. 마지막 account/warning GET과 plan INSERT 사이에 발생하는 외부 수동 주문
race를 제거할 수 없으므로 이것도 별도 live 승격 blocker입니다.

## 한 달 intraday 전진 시뮬레이션

실제 돈을 넣기 전 단계로 미국 시장 session date `2026-08-31`~`2026-09-30` 양 끝을 포함하는
forward simulation을 구현했다. 초기값은 설정 가능한 **가상 USD 10,000**이다. planner는 매일
시장 calendar를 확인하고, 기간 안의 평일은 immutable plan 또는 `MARKET_CLOSED`로 coverage에
기록한다. 실제 holdings·buying power·계좌/주문 내역과 personal WebSocket은 차단되고 수량은 가상
cash에서만 정한다. 계좌 header가 필요한 commission schedule GET만 예외이며 모든 public market
read에는 account header를 보내지 않는다. 실제 주문 생성·정정·취소는 호출하지 않는다.
calendar의 `preMarket`와 `regularMarket` key가 둘 다 존재하면서 값도 둘 다 명시적 null일 때만
휴장이다. key 누락은 `intraday_calendar_malformed`, 한쪽만 null이면
`intraday_required_session_unavailable`로 fail-closed하며 `MARKET_CLOSED` coverage를 만들지 않는다.

planner와 stream은 서로 다른 private manifest를 사용한다. planner manifest만 수수료표 조회용
account sequence를 가지며, stream manifest의 account alias/sequence는 비어 있다. 두 manifest의
경제값·선정 규칙·기간·가상 자금·slippage는 같은 experiment SHA-256으로 잠기고, wrapper는
`run_id`·시작일·종료일·paper DB·hash를 시작 전과 config reload 때 다시 비교한다. account identity와
다른 filesystem path는 hash 입력이 아니지만, simulation의 resolved absolute `news_context_path`는
hash에 포함된다. 두 manifest는 동일한 plan DB·paper DB와 정확히 같은 context 절대 경로를 가리켜야
한다. planner config는 runtime UID 소유의 symlink가 아닌 regular file이며 group/other 권한이 없는지
Keychain 접근 전에 검사한다. 배포 시 `account_seq`로 만든 별도
`TOSS_SHADOW_ACCOUNT_FINGERPRINT`를 잠그고 매 hot reload마다 다시 계산해 비교하므로 계좌 권한이
바뀌면 즉시 fail-closed한다. 실제 account sequence나 fingerprint는 stream·Git·공개 문서로 넘기지
않는다. 그 밖의 잠금값도 달라지면 새 설정으로 이어서 돌리지 않는다.

자동 selector가 고른 한 종목의 strict parser를 통과한 Toss public WebSocket `trade:us`·
`orderbook:us` frame을 별도 SQLite DB에 저장한다. DB는 WAL·`synchronous=FULL`이고 stream은
128번째 frame 또는 0.25초 기준을 넘긴 다음 receive-loop tick에서 batch commit하며
disconnect·정상 종료에도 flush한다. idle 때도 기본 1초 poll tick이 flush를 진행한다. 따라서
비정상 종료 직전에는 최대 127개 미저장 frame이 유실될 수 있다. stream은 context를 검증한 뒤
OAuth·socket 작업보다 먼저 `start`를 sink에 보내 `paper_stream_instances` marker를 commit하고,
`last_seen_at`을 최대 초당 한 번 갱신하며 orderly final flush 뒤에만 닫는다. planner는 확정 시 instance가
필수 entry-expiry/force-exit 경계를 실제로 덮었는지 검사하고, 경계 전에 시작한 최신 open marker가
quote TTL 안에서 fresh하면 확정을 미룬다. TTL을 넘긴 orphan은 `stream_liveness_expired`로 닫는다.
어떤 instance도 경계를 덮지 못했으면 `stream_coverage_incomplete`, 경계 증거는 있지만 open process가
그 뒤 liveness를 잃었으면 `stream_process_interrupted` gap을 남긴 뒤에만 확정한다. 새 stream은 이전
orphan을 `superseded_by_stream_restart`로 닫는다.

검증된 REST baseline 뒤 ACK된 선택 종목 `trade:us`·`orderbook:us` 각각이 quote TTL 안에 현재
generation의 fresh event를 주지 않으면 `trade_topic_silent` 또는 `orderbook_topic_silent` gap으로
민감 구간을 invalid 처리하고 연결을 끊어 재접속한다. disconnect·늦은 첫 frame과 frame-level 검증
오류도 민감 구간의 data gap이다. malformed frame 자체는 journal에 넣지 않으며 이 기록을 거래소의
gap-free tape라고 주장하지 않는다.

가상 진입·target·stop trigger는 해당 시점에 실제로 수신한 `trade` frame만 사용한다. orderbook이나
baseline에 carry-forward된 예전 체결가는 trigger가 아니며, 실제 trade timestamp도 진입 시작 또는
가상 진입시각 뒤여야 한다. 체결은 trigger observation과 다른 이후 수신 orderbook에서만 판정한다.
표시 top-of-book depth가 전체 정수 수량을 덮고 limit과 불리한 방향의 configurable slippage를
만족해야 한다. fee는 plan에 잠근 broker commission을 각 leg에 적용하고 configured
fixed round-trip cost의 절반을 각 leg에 더한다. 별도 규제 수수료·최소 수수료나 slippage drag
통계는 현재 계산하지 않는다. open 상태에서 gap이 생기면 다음 유효 bid로 limit 없이 전량 가상
청산하고 결과를 invalid로 분리한다. 정규장 종료까지 fresh book과 충분한 depth가 없으면
**미청산 가상 포지션**을 `UNRESOLVED`로 봉인하고 final equity/return을 미확정으로 두며 다음 plan을
차단한다. `UNRESOLVED`는 단순한 무진입·누락일이 아니라 진입 후 청산 체결을 만들 수 없었던 상태다.

daily Discord payload는 status, 수량, entry/exit 가격·시각·사유, gross/net P&L, 합산 fee,
시작/종료 cash, accepted event·journal frame·data-gap 수, first/last event와 clean metric 포함 여부를
보낸다. 기간 종료 payload는 summary status, initial/current cash, final equity, realized/clean P&L과
return, 거래·승·패·승률, 평균 승/패, expectancy, profit factor, fee, MDD, exit reason, no-entry,
invalid·unresolved·waiting 수, expected/covered/missing/holiday coverage와 journal 정책을 보낸다.
Discord 한 줄 알림은 그중 핵심 상태·자산·손익·거래·fee·MDD·무효/미해결/누락 수를 표시한다.
reconnect percentile, uptime, MAE/MFE, exposure, 종목 분포는 아직 지표가 아니다. paper engine은
Discord 승인 receipt를 기다리거나 소비하지 않는다.

월 요약은 모든 예상 평일이 plan 또는 `MARKET_CLOSED`로 덮여야만 coverage가 완성된다. 기간 종료
뒤 누락일이 있거나 plan이 하나도 없으면 `INCOMPLETE`이며 `COMPLETE`로 가장하지 않는다. `WAITING`,
`OPEN`, `UNRESOLVED`, `INVALID`, `BLOCKED`는 각각 더 구체적인 비정상/비종료 상태로 우선 보고된다.

Mac에서는 기존 exact-five non-live topology를 유지한다. planner·stream·approval·news 네 job이
redacted heartbeat를 원자 기록하고, 다섯 번째 watchdog job이 heartbeat/launchd 상태를 읽는다.
계획·가상 현금 sizing·일일/월간 보고는 planner에, event journal·causal fill·USD ledger 갱신은
selected-symbol stream에 포함해 여섯 번째 daemon을 만들지 않는다. heartbeat producer와 evaluator가
코드에 연결됐다는 사실은 Mac 설치·가동 증거가 아니다. stream LaunchAgent는
`news-context.json`의 `WatchPaths`로만 깨어나며 `RunAtLoad`·`StartInterval`·`KeepAlive`가 없어 plan
전에는 OAuth/WS 없이 idle이고, planner가 context를 갱신하면 다시 시작한다. planner는 locked state
DB의 sibling `stream-expectation.json`을 context export보다 먼저 owner-private atomic write한다. 이
표식은 symbol·account를 담지 않고 stream 필요 기간만 나타낸다. watchdog은
`TOSS_WATCHDOG_CONTEXT_PATH`와 `TOSS_WATCHDOG_EXPECTATION_PATH`를 함께 strict 검증한다. active
expectation인데 context가 없거나 삭제·export 실패 상태면 idle로 오인하지 않고
`STREAM_CONTEXT_INVALID`다. 두 표식이 정상 만료된 post-close에는 loaded-but-stopped WatchPaths job을
정상으로 본다. malformed expectation은 `STREAM_EXPECTATION_INVALID`다. planner는 regular close
이후 context를 다시 쓰지 않아 만료 직후 WatchPaths 재기동 churn을 만들지 않는다. 현재 상태는
**로컬 구현·회귀 검증 완료 / Mac exact-SHA 배포·실제 public WS·Discord smoke 대기 /
LIVE NO-GO**이며 한 달 계측은 아직 시작하지 않았다. 상세 계약은
[intraday 브래킷 설계](docs/intraday-bracket-design.md)와
[macOS 운영](docs/macos-operations.md)을 따른다.

## 최신 테스트 결과

2026-08-30 현재 checkout을 강제해 다시 확인한 결과:

```text
PYTHONPATH=src python -m pytest
```

전체 회귀와 계약 테스트는 전략 코어, 롱·숏 백테스트, point-in-time 유니버스 필터링,
스캔·모멘텀 백테스트, 토스 OpenAPI 요청·응답 호환성, 시장 캘린더 파싱,
paper runtime, shadow 검증, 단타 현금·가격 계산, 장전 데이터 fail-closed 경계,
조건주문 계약, immutable 계획 저장, 초기 설정 파싱, 리포트, 상태 저장소를 포함합니다.
자동 selector의 랭킹·거래 가능 보통주 교집합·상위 후보 strict 검사, 정확한 빈 warnings,
raw 일봉·완료된 프리마켓 1분봉, 최종 warnings·계좌·현금·현재가·호가 재검증, lock 시각
freshness와 재시작 무재선정도 포함합니다.
또한 선정 종목 context redaction·경합·조기폐장, 독립 뉴스 import 경계, Finnhub exact-symbol
필터, 뉴스 DB dedupe/lease/session 만료, 악성 LLM fallback과 Discord 재시도를 검증합니다.
승인 봉투/worker schema 호환, 전체 표시값 binding, 만료 단조성, 생성 no-clobber, exact Discord
allowlist, modal TOCTOU, 선 ACK, 원자적 receipt publish, symlink·파일 모드·중복·위험 환경변수
거부도 포함합니다. 외부 Discord 서버 ACL은 대상 channel 하나만 View/Send가 남도록 별도
감사를 통과했습니다. 승인 전용 exact-SHA macOS release의 Gateway 연결 smoke도 통과했으며,
2026-08-30 synthetic/no-trade 버튼·hash modal E2E에서 strict receipt 1개 생성, mode `0600`,
전체 binding, 민감정보 미포함, worker 재시작 뒤 receipt·Discord 요청 무증가를 확인했습니다.
임시 LaunchAgent와 synthetic runtime은 검증 후 제거했습니다. 승인 영수증 소비자와 실주문
연결은 여전히 없습니다. stream 계약 테스트는 단일 종목·두 토픽 선언, exact ACK,
wrong-topic·binary·oversize·중복-key 거부, nullable/stale/regressed timestamp, 연결·REST 실패
재동기화, 텍스트 PING/pong timeout, mode `0600` redacted snapshot, 실제 read-only client의
OAuth·`GET /prices`·`GET /orderbook` 외 요청 0건을 검증합니다. 실제 Mac/Toss WebSocket 외부
smoke와 자동 selector의 실제 Toss/Mac shadow smoke, 미국장 shadow soak는 아직 수행하지
않았습니다.

## 주요 검증 수치

아래 수치는 로컬 `reports/backtest/` 리포트에서 확인한 결과입니다. 초기 자금은
모두 `$100,000`이고, 전략은 `SPY` 200일선 시장 필터 + S&P 500 상대강도
모멘텀 기준입니다.

현재 선택한 기본 모멘텀 설정은 `126일 모멘텀 / 최근 21일 제외 / 75일선 청산 /
최대 5종목 / 일 2개 진입 / 종목당 10% / 현금 보유 50%`입니다. 아래 첫 표는 과거 baseline
비교용 수치이고, 운영 후보 판단은 그 다음 PIT 검증 표를 우선합니다.

| 구간 | 비용 모델 | 최종 자산 | 총수익률 | 추정 CAGR | MDD | 거래 수 | 승률 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2015-01-02 ~ 2026-06-12 | 수수료 없음 | $1,789,990.66 | +1689.99% | 28.74% | 41.35% | 393 | 40.20% |
| 2015-01-02 ~ 2026-06-12 | 토스 비용 반영 | $1,635,546.77 | +1535.55% | 27.72% | 41.68% | 385 | 37.92% |
| 2024-01-02 ~ 2026-06-12 | 수수료 없음 | $592,310.93 | +492.31% | 107.53% | 38.48% | 88 | 48.86% |
| 2024-01-02 ~ 2026-06-12 | 토스 비용 반영 | $571,420.17 | +471.42% | 104.49% | 41.90% | 90 | 41.11% |

위 2024~2026 수치는 현재 보유한 S&P 500 프록시 종목군 기준입니다. 공개
historical S&P 500 구성종목 데이터(`fja05680/sp500`)를 우리 PIT CSV 포맷으로
변환해 다시 계산하면 결과가 크게 낮아집니다. 이 PIT 계산은 2015~2023 데이터를
지표 워밍업으로만 사용하고, 2024-01-02부터 매매를 허용했습니다.

| 구간 | 유니버스 | 비용 모델 | 최종 자산 | 총수익률 | 최저 누적수익률 | MDD | 거래 수 | 승률 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2024-01-02 ~ 2026-06-12 | 무료 PIT + 로컬 가격 교집합 | 수수료 없음 | $285,630.19 | +185.63% | -0.67% | 27.68% | 84 | 46.43% |
| 2024-01-02 ~ 2026-06-12 | 무료 PIT + 로컬 가격 교집합 | 토스 비용 반영 | $259,485.72 | +159.49% | -0.85% | 28.97% | 86 | 43.02% |

현재 선택한 `126/21/75/5/2/10%/현금50%` 설정은 같은 무료 PIT 조건에서 기존 baseline보다
4개 검증 구간 모두 수익률이 높고 MDD가 낮았습니다.

| 구간 | 기존 baseline 수익률 / MDD | 현재 선택 설정 수익률 / MDD |
| --- | ---: | ---: |
| 2015-01-02 ~ 2017-12-29 | +15.22% / 15.69% | +17.28% / 10.03% |
| 2018-01-02 ~ 2020-12-31 | -2.20% / 33.21% | +7.70% / 15.06% |
| 2021-01-04 ~ 2023-12-29 | +8.46% / 27.33% | +17.65% / 13.49% |
| 2024-01-02 ~ 2026-06-12 | +159.49% / 28.97% | +174.09% / 13.59% |

무료 PIT 적용 시 2024-01-02 기준 S&P 500 멤버 503개 중 로컬 가격 데이터가
있는 종목은 452개였고, 전체 기간 평균으로는 약 473개였습니다. 따라서 이 결과는
생존편향을 줄인 보수적 재검증이지만, 상폐·티커변경 종목 가격 데이터가 완전히
채워진 CRSP/Norgate급 검증은 아닙니다.

토스 비용 모델은 미국주식 매수 수수료 0.1%, 매도 수수료 0.1%, SEC Fee
0.00206%, 최소 수수료 $0.01을 반영합니다.

2024-01-02 ~ 2026-06-12 토스 비용 반영 결과에 한국 거주자 해외주식 양도세를
거칠게 추정하면 다음과 같습니다. 환율은 `$1 = 1,350원`, 기본공제는 연 250만원,
세율은 22%로 가정했습니다.

| 과세 방식 | 추정 세금 | 세후 최종 자산 | 세후 수익률 |
| --- | ---: | ---: | ---: |
| 연도별 실현손익만 과세 | 약 $28,218.62 / 약 3,809만원 | $543,201.55 | +443.20% |
| 2026-06-12 보유분 전량 청산 가정 | 약 $102,490.22 / 약 1억 3,836만원 | $468,929.96 | +368.93% |

주의할 점도 큽니다. 현재 장기 리포트는 현 S&P 500 구성종목 기반 프록시라
완전한 point-in-time 유니버스가 없으면 생존편향이 남습니다. 또한 메인 백테스트는
토스 수수료와 SEC Fee는 반영하지만 슬리피지, 스프레드, 환전 스프레드, 실제
세무 신고 로직은 아직 정식 엔진 옵션으로 넣지 않았습니다. 따라서 위 숫자는
실거래 보장값이 아니라 전략 연구와 shadow 검증을 위한 기준값입니다.

## 공식 참고 링크

- Toss Open API LLM guide: <https://developers.tossinvest.com/llms.txt>
- Toss OpenAPI JSON: <https://openapi.tossinvest.com/openapi-docs/latest/openapi.json>
- Toss AsyncAPI JSON: <https://openapi.tossinvest.com/openapi-docs/latest/asyncapi.json>
- Original Turtle Rules PDF: <https://www.tradingwithrayner.com/wp-content/uploads/2014/11/OriginalTurtleRules.pdf>
