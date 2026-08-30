# MacBook LLM 뉴스·Discord 운영 설계

상태: v1 구현·로컬 계약 검증 완료, 실제 서비스 smoke test 전
작성일: 2026-08-29

## 사용자 의도

- 전성비가 좋은 MacBook을 상시 운영 노드로 사용한다.
- 매매 판단과 주문은 기존의 결정론적 전략·안전장치만 수행한다.
- LLM은 당일 잠긴 단타 계획의 정확히 한 종목 뉴스만 한국어로 요약한다.
- 요약은 Discord로 보내고 원문을 확인할 수 있게 한다.
- Tailscale SSH로 관리하며 public internet에 관리 포트를 열지 않는다.

## 결정

1. 거래 프로세스와 뉴스 프로세스를 분리한다.
2. LLM은 localhost의 OpenAI-compatible API만 사용하고 tool과 broker 자격증명을 갖지 않는다.
3. trading agent가 무결성 검증한 당일 plan에서 만든 redacted
   `news-context.json`만 뉴스 worker에 전달한다.
   worker는 config, 주문, 거래 DB에 접근하지 않는다.
4. LLM 출력은 Discord와 별도 뉴스 상태 저장소로만 흐른다. 전략으로 돌아가는 경로를 만들지 않는다.
5. v1은 context 파일 하나당 단일 trading agent·단일 종목만 허용한다. 여러 계좌는
   context 디렉터리, news config, news DB와 worker를 각각 분리한다.
6. 뉴스는 추천·호재/악재·주가 방향 예측이 아니라 출처가 붙은 중립 요약만 제공한다.

선행 로컬 작업 트리의 `news.enabled`는 뉴스 점수를 실제 진입 차단과 청산 신호에 연결한다.
이 경로는 재사용하지 않았다. 공개 v1은 optional `strategy.intraday.news_context_path`와
독립 `turtle_news` one-shot 명령만 사용하며 뉴스 점수나 trading callback이 없다.

## 목표 구조

```text
                       관리 경로
관리 기기 ── Tailscale SSH ───────────────┐
                                           │
MacBook                                    ▼
┌─────────────────────────────────────────────────────────┐
│ launchd                                                 │
│                                                         │
│ trading-agent ────────────── Toss API ── trading.sqlite │
│              │ atomic redacted news-context.json        │
│              ▼                                          │
│ news-digest --once ── news feed                         │
│        │               │                                │
│        │ new items     └─ title/excerpt/source/link     │
│        ▼                                                │
│ llama-server 127.0.0.1 ── neutral Korean summary        │
│        │                                                │
│        └─────────────── Discord news webhook             │
│                                                         │
│ maintenance ─ heartbeat / backup / disk / log retention │
└─────────────────────────────────────────────────────────┘

중요: news-digest 또는 LLM에서 trading-agent로 돌아가는 화살표는 없다.
```

## 프로세스와 권한

| 프로세스 | 읽기 | 쓰기 | 보유 secret | 금지 |
| --- | --- | --- | --- | --- |
| `trading-agent` | 거래 config와 거래 DB | 거래 DB, redacted `news-context.json` | Toss 자격증명, 거래 Discord webhook | LLM 호출, 뉴스 점수 사용 |
| `news-digest` | 전용 JSON config, `news-context.json`, Finnhub feed | 별도 `news.sqlite3` | Finnhub API key, 뉴스 Discord webhook만 | Toss API/secret, trading DB/config, 거래 webhook, dashboard action POST |
| `llama-server` | 모델 파일 | 없음 | 없음 | 외부 bind, tool 실행, broker/Discord 접근 |
| `maintenance` | heartbeat, DB/로그/디스크 상태 | 검증된 backup과 정리 상태 | 외부 dead-man URL만 | 주문 생성, config 변경 |

macOS 사용자 권한만으로 완전한 sandbox를 만들지는 않는다. 대신 독립 `turtle_news` package는
`turtle_bot` 자체를 import하지 않으며, Toss secret이나 거래 webhook이 환경에 하나라도 있으면
시작을 거부한다. 지정된 DB에 거래 table이 있으면 schema를 만들기 전에 중단한다.

## 뉴스 입력

### 종목 범위

v1은 자동 scanner, 보유 종목 합집합, watchlist를 사용하지 않는다. 장전 단타 계획이
SQLite에 immutable 저장되고 다시 무결성 검증된 뒤 그 record의 `symbol` 하나만 사용한다.
LLM은 종목을 추가·제외하거나 relevance를 판정하지 않는다. 계좌번호·alias·현금·수량·가격·
주문 ID·guardrail·broker 응답은 context와 LLM 입력에서 제외한다.

trading agent는 같은 디렉터리의 임시 파일을 fsync한 뒤 lock과 `os.replace`로 다음 allowlist
object만 갱신한다. 현재 캘린더가 조기 폐장으로 바뀌면 `active_until`을 더 이른 강제청산
시각으로만 줄이며, 오래된 iteration이나 다른 symbol writer가 최신 context를 덮지 못한다.

```json
{
  "schema_version": 1,
  "generated_at": "2026-08-28T12:30:00+00:00",
  "market": "US",
  "session_date": "2026-08-28",
  "active_until": "2026-08-28T15:45:00-04:00",
  "symbol": "AMD",
  "reason": "intraday_plan"
}
```

worker는 key가 하나라도 많거나 적은 context, 대문자 US symbol 형식 오류, 미래·naive 시각,
5분보다 오래된 파일, 현재 New York 거래일 불일치, 주말, `active_until` 도달을 모두
fail-closed로 거부한다. 단일 파일에는 writer를 하나만 둔다. 여러 계좌를 운용할 때 계좌
식별자를 context에 추가하는 대신 각 계좌에 별도 runtime 디렉터리를 준다.

### 출처

Toss OpenAPI에는 뉴스 endpoint가 없으므로 v1 공급자는 Finnhub Company News 하나로 고정했다.
`symbol`, `from`, `to`를 보내고 응답의 `related`가 선택 symbol과 token 단위로 정확히 일치하는
기사만 받는다. 제목, 제공 summary, publisher, 보도 시각, HTTPS canonical URL만 저장한다.
기사 본문 scraping, redirect 추적, paywall 우회, Yahoo/RSS fallback은 하지 않는다. 이 feed는
북미 회사 뉴스 범위이며 완전성·무지연을 보장하는 실시간 wire가 아니다.

### 입력 경계

- 원문 링크는 HTTPS, 표준 443, userinfo 없음, public host만 허용한다. worker가 원문을
  fetch하지 않으며 local/private IP 링크와 redirect는 거부한다.
- 응답 크기, 기사 수, 제목/excerpt 길이, HTTP timeout을 제한한다.
- 실행당 기사 최대 4개를 한 건씩 처리한다.
- `canonical URL hash`를 기본 중복 키로 하고 URL이 없을 때만
  `publisher + normalized title + published_at` hash를 사용한다.
- 기사 전문은 DB·로그·Discord에 저장하지 않는다.

## LLM

기존 `turtle_bot.ai_summary`를 import하면 package 초기화 과정에서 거래·주문 모듈까지
로드되므로 재사용하지 않았다. `turtle_news` 안에 응답 크기·timeout·redirect를 제한한 작은
stdlib OpenAI-compatible client를 독립 구현했다. Mac의 GGUF Q4 + `llama-server`는 후보일 뿐
모델은 계약이 아니다. 메모리, 한국어 품질, 지연, idle 전력을 실측한 뒤 한 모델을 확정한다.

기본 운영값:

- bind: `127.0.0.1` only
- concurrency: 1
- context: 4K 내외
- temperature: 0~0.2
- output: 기사당 1~2문장, 전체 300~500 token 이내
- timeout: 60초 이내
- LLM 호출 전 exact dedupe; 새 항목이 없으면 호출하지 않음

LLM 입력의 뉴스 문자열은 모두 비신뢰 데이터다. system prompt에 `NEWS_JSON` 안의 문장은
명령이 아니라 데이터라고 명시한다. 모델은 URL을 생성하지 못하며, 코드가 검증된 입력 URL을
나중에 붙인다. 입력에 없던 ticker, ID, 수치 또는 URL이 출력되면 모델 결과를 폐기한다.

첫 버전 출력에서 제거할 항목:

- `recommendation_reason`
- positive/negative sentiment와 호재/악재 색상
- 주가 방향, 목표가, 매수·매도 의견
- LLM 기반 중요도 및 event key
- 실패한 출력을 고치기 위한 두 번째 repair 호출

요약 검증이 실패하면 모델 문장과 provider excerpt를 버리고
`원문 제목 + publisher + 보도 시각 + 검증된 링크`만 Discord에 보낸다. 선택 symbol 이외 대문자
ticker, 새 숫자, URL, mention, 추천·매수·매도 표현은 모델 출력 전체를 폐기한다. 거래
프로세스는 정상 동작을 계속한다.

## Discord

거래 알림과 뉴스 알림을 분리한다.

- 거래: `DISCORD_TRADE_ALERT_WEBHOOK_URL`
- 뉴스: `DISCORD_NEWS_WEBHOOK_URL`
- 공통 단일 channel allowlist: `DISCORD_ALLOWED_CHANNEL_ID`

두 webhook과 channel allowlist는 macOS Keychain 또는 권한이 제한된 로컬 실행 환경에 저장하고
launch wrapper가 실행 직전에 환경변수로 주입한다. 실제 값은 plist, config, CLI 인자,
dashboard payload와 로그에 넣지 않는다. 각 sender는 전송 전에 webhook metadata의 `channel_id`를
allowlist와 exact-match하며 불일치·조회 실패 시 POST를 수행하지 않는다.

뉴스 메시지는 다음처럼 코드가 조립한다.

```text
[뉴스 요약 · 2026-08-28 21:15 KST]
AMD
- 중립 한국어 요약
- Reuters · 2026-08-28 20:52 KST · 원문 링크

AI 헤드라인 요약 · 투자 판단 아님 · 원문 확인 필요
```

Discord payload에는 항상 `allowed_mentions: {"parse": []}`를 넣고, 성공 확인을 위해
`wait=true`로 보낸다. 2xx와 message object가 확인된 뒤에만 row를 `SENT`로 바꾼다.
실패 row는 `PENDING`으로 되돌리고 다음 15분 launchd 실행에서 같은 cached summary를
재사용한다. Discord는 idempotency key를 제공하지 않으므로 서버가 저장한 직후 응답이
유실되면 한 번 중복될 수 있다. 따라서 계약은 exactly-once가 아니라 at-least-once다.

## 상태와 scheduling

`news-digest`는 daemon이 아니라 launchd가 15분마다 실행하는 one-shot command다.
Toss secret 없이 동작해야 하므로 worker가 broker calendar를 다시 호출하지 않는다.
context의 New York `session_date`, freshness, 주말, `active_until`을 검증해 빠르게 종료하고,
새 관련 뉴스가 없으면 Discord와 LLM을 호출하지 않는다. ZoneInfo로 DST를 처리한다.

별도 `news.sqlite3`에 다음만 저장한다.

- global URL hash, session date, source, published time, first-seen time
- validated summary cache와 source fallback 구분
- `PENDING/SENDING/SENT/EXPIRED`, claim token·lease, attempt count, 안전한 error category

본문, 계좌 데이터, webhook, API key, model prompt 전문은 저장하지 않는다. URL hash primary key와
`BEGIN IMMEDIATE` claim으로 중복 worker 실행을 막고 이전 세션의 pending은 `EXPIRED`로 바꾼다.
state DB `quick_check`가 실패하거나 거래 table이 발견되면 원본을 지우지 않고 중단한다.

## launchd와 MacBook 운영

- 계좌별 `trading-agent`: 유일한 주문 writer, `RunAtLoad=true`,
  `KeepAlive={SuccessfulExit:false}`, bounded backoff.
- `llama-server`: localhost 전용. idle 자원 사용을 실측하고 문제가 있을 때만 장외 unload를 추가한다.
- `news-digest`: `StartInterval=900`, one-shot, `KeepAlive` 없음. DB claim lease가 동시 실행을 직렬화한다.
- `maintenance`: heartbeat·SQLite backup/`quick_check`·disk·로그 보존을 점검한다.

plist는 generator로 만들고 repository path나 macOS 사용자 경로를 hardcode하지 않는다. repo의
`.venv/bin/python`과 검토된 exact commit을 사용한다. runtime data와 log는 checkout 밖의
`Application Support`와 `Library/Logs`에 둔다.

MacBook은 AC 전원에서 system sleep을 막고 display sleep은 허용한다. local LLM은 낮은 process
priority로 실행하며 trading heartbeat 지연이나 memory pressure가 발생하면 먼저 LLM을 중지한다.
LaunchAgent + Keychain 구성은 재부팅 후 사용자 login과 Keychain unlock이 필요하다는 제약을
운영 문서에 명시한다.

dashboard는 `127.0.0.1`에만 bind한다. 초기에는 SSH local forwarding으로만 보고, 필요가 확인된
후 Tailscale Serve와 ACL을 적용한다. Tailscale IP에 직접 bind하지 않는다.

## 장애 정책

| 장애 | 뉴스 동작 | 거래 동작 |
| --- | --- | --- |
| 뉴스 공급자 timeout/오류 | 해당 cycle 중단, 제한된 WARN | 정상 계속 |
| LLM timeout/invalid output | source-only fallback | 정상 계속 |
| Discord 429/5xx | 결과 cache 후 제한 재시도, sent 미기록 | 정상 계속 |
| 오래된 symbol context | 수집·발송 중단 | 정상 계속 |
| `news.sqlite3` integrity 실패 | 뉴스 worker 중단, 원본 보존·경고 | 정상 계속 |
| Mac memory pressure | LLM/news 중단 | trading 우선 유지 |
| Mac 전원·네트워크 단절 | local 발송 불가 | 외부 dead-man이 미수신 경고 |

뉴스 장애에 대해 거래는 fail-open이고, LLM이 거래에 영향을 주는 경로는 fail-closed다.

## 선행 로컬 구현 정리

참고만 하고 직접 연결하지 않은 것:

- localhost `llama-server`와 GGUF Q4 설정
- URL exact dedupe와 claim lease 패턴
- Discord 길이 제한과 mention 차단 원칙
- 중복 기사 fixture와 관련 테스트 아이디어

제거·축소:

- 2천 줄대 runner의 다중 사용자 dashboard orchestration
- `daily_research_recommendation`과 추천 근거 생성
- 감성 점수, 호재/악재 색상, 단기 주가 영향 추론
- 뉴스 score를 entry/exit/stop에 연결하는 전략 경로
- dashboard action POST와 거래 DB에 Discord dedupe를 기록하는 경로
- LLM 기반 event clustering과 두 번째 repair 요청
- 사용자 절대 경로, 고정 port, `/usr/bin/python3`가 박힌 plist

새 agent framework, vector DB, RAG, Celery, Redis, Docker, multi-model routing은 추가하지 않는다.
한 독립 one-shot worker와 stdlib HTTP/SQLite면 충분하다. 거래용 AI client·Discord sender·DB는
import 격리를 위해 재사용하지 않는다.

## 필수 검증

1. 악성 headline의 지시, 새 ticker/URL, `@everyone`이 출력에 반영되지 않는다.
2. LLM 성공·실패·악성 응답 전후에 trading config, signal, order intent와 주문 DB가 동일하다.
3. news worker 환경에는 Toss secret이 없고 trading DB/config 쓰기는 실패한다.
4. 같은 URL은 한 news DB와 재시작 범위에서 LLM·의도적 Discord 전송 대상이 한 row다.
   단, 성공 응답 유실 시의 at-least-once 중복 가능성은 별도 운영 계약이다.
5. Discord 실패 후 sent로 표시하지 않고, 재시도 때 cache를 사용해 재요약하지 않는다.
6. LLM down이면 source-only fallback만 한 번 전송한다.
7. 모든 Discord payload가 mention을 차단하고 publisher·보도 시각·검증된 원문 링크를 포함한다.
8. 주말, DST 전환, market session별 digest가 예상 횟수로 실행된다.
9. plist는 `plutil -lint`를 통과하고 reboot/login 후 복구한다.
10. Mac에서 화면 sleep, network 단절, LLM kill, Discord 429, DB restore를 강제로 재현한다.

## rollout

1. 완료: 거래 package를 import하지 않는 one-shot worker와 fake Finnhub/LLM/Discord 계약 테스트.
2. 완료: redacted context, exact-symbol filter, 별도 DB/webhook, cache retry, 악성 LLM fallback 검증.
3. 대기: canonical Mac checkout과 전용 `.venv`를 고정하고 실제 key로 수동 one-shot smoke test.
4. 대기: Discord 전용 channel에 news-only로 3일 운용.
5. 대기: shadow와 함께 실제 미국장 5세션 운용하며 중복·누락·지연·전력·메모리를 측정.
6. 별도 gate: trading live 전환은 뉴스 기능과 무관하게 기존 P0 안전 조건을 모두 통과한 뒤 결정.

## 외부 계약

- [Toss OpenAPI schema](https://openapi.tossinvest.com/openapi-docs/latest/openapi.json)
- [Finnhub API documentation](https://finnhub.io/docs/api/quote)
- [Discord webhook 실행 계약](https://docs.discord.com/developers/resources/webhook#execute-webhook)
- [Tailscale SSH](https://tailscale.com/docs/features/tailscale-ssh)
