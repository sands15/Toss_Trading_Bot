# 장전 가격 계획 기반 단타 브래킷 설계

상태: **`NON_LIVE_CORE_IMPLEMENTED / LIVE_NO_GO`**. 자동 selector·장전 계획기·승인 v2
검증/원자 소비·선정 종목 public WebSocket/news shadow와 dependency-injected intraday lifecycle
core가 구현돼 있다. production CLI·LaunchAgent는 실행 core를 생성하지 않으며 실제 주문·실계좌
live test는 수행하지 않았다. exhaustive replay/SLO·durable urgent alert/stop-limit escalation,
watchdog Discord delivery와 clean exact-SHA Mac release 증거가 남아 있으므로
`NON_LIVE_IMPLEMENTATION_COMPLETE`나 live 가능 판정으로 해석하지 않는다.

이 문서는 미국 주식 정규장을 대상으로, 장 시작 전에 진입·익절·손절 가격과
위험 한도를 잠근 뒤 장중에 한 번의 단타를 실행하는 최소 안전 설계를 정의한다.
수익성을 보장하거나 특정 가격을 추천하는 문서가 아니다.

## 1. 용어와 범위

미국 주식에는 한국 시장과 같은 고정 일일 상한가·하한가가 없다. Toss의
`GET /api/v1/price-limits`도 미국 종목에 대해 `upperLimitPrice`와
`lowerLimitPrice`를 `null`로 반환한다. 미국 시장의 LULD 가격 밴드와 거래정지는
별도로 존재하지만, 이를 사용자가 지정하는 고정 익절·손절가로 취급하지 않는다. 현재 확인한
REST 계약에는 미국 종목의 authoritative halt/LULD 상태 필드도 없다. 빈 warnings나 null인
price-limits를 “거래정지 아님”의 증거로 사용할 수 없으므로 이 공백이 해소되기 전에는 live로
승격하지 않는다.

이 기능에서 사용하는 뜻은 다음과 같다.

- 상한선: `target_trigger`, 익절 조건이 발동하는 가격
- 하한선: `stop_trigger`, 손절 조건이 발동하는 가격
- 진입선: `entry_trigger`, 매수 진입을 검토하는 가격
- 추격 제한선: `entry_limit`, 이 가격보다 비싸면 진입하지 않는 상한

두 가격만으로는 진입 가격과 최대 손실을 통제할 수 없으므로 장전 계획은 최소한
진입선·추격 제한선·익절선·손절선·손절 지정가를 별도로 가진다.

첫 버전의 범위는 다음으로 고정한다.

- 미국 주식, 정규장, 롱 전용
- 계좌 전체에서 동시 포지션 1개
- 하루 신규 진입 1회
- 자동 선정된 단일 allowlist와 Discord에서 명시적으로 승인된 장전 가격 계획
- 물타기, 피라미딩, 숏, 재진입, 트레일링 스톱, 프리마켓·애프터마켓 진입 없음
- 뉴스 LLM은 설명·알림 전용이며 종목·가격·수량·거래 허용 여부를 바꾸지 않음

이번 비실거래 작업에서 `live test 제외`의 뜻은 다음과 같이 고정한다.

- 실제 Toss 계좌에 일반·조건주문 생성, 정정, 취소를 한 건도 보내지 않는다.
- 실제 돈·소액·1주 pilot, 실제 OCO 생성, 실계좌 emergency/force exit 시험은 하지 않는다.
- 과거 데이터 replay, 임시 SQLite, fake REST/WebSocket, 장애주입, synthetic Discord 승인,
  Mac exact-SHA 패키징, 실제 시장의 읽기 전용 shadow 관측은 범위에 포함한다.
- 주문 lifecycle 코드는 fake transport로 끝까지 실행하되 CLI·LaunchAgent·운영 config에서는
  broker mutation 경로를 계속 닫아 둔다.
- 이후 실제 주문 검증은 별도 사용자 승인과 별도 작업으로만 열 수 있다. 이 문서의 비실거래
  합격 결과만으로 `live_execution_enabled`를 바꾸지 않는다.

production에 연결된 거래 범위는 **장전 계획 생성과 그 결과의 승인·뉴스·시세 shadow 관찰까지**다.
`strategy.kind=intraday`는 기존 `PaperTradingRuntime`보다 먼저 별도 분기로 빠지며, Toss 읽기 API,
SQLite 계획 INSERT와 redacted context·알림 외에는 거래 동작을 하지 않는다. 정규장이 시작되면
`intraday_execution_engine_not_enabled`로 차단되고 주문 제출 함수에는 도달하지 않는다.
별도 `intraday_live.py` core는 fake broker/clock/stream으로만 상태 전이, writer fence, immutable
request, 승인 만료, 부분체결, OCO 식별과 전량 exit를 검증한다. 코드가 존재한다는 사실은 production
dispatch 권한을 뜻하지 않는다.

## 2. Toss 공식 계약이 만드는 제약

2026-08-28 기준 Toss REST OpenAPI `1.2.14`와 WebSocket AsyncAPI `1.2.2`를
계약 원본으로 사용한다.

### 조건주문

- `OCO`는 **One Cancels the Other**의 약자다. 익절·손절 두 개의 `SELL` 조건을 동시에
  감시하고 하나가 발동하면 다른 하나를 취소한다.
  `first.triggerPrice > 현재가 > second.triggerPrice`여야 한다.
- `OTO`는 `BUY first` 체결 뒤 `SELL second` 하나만 감시한다. `BUY -> OCO`를
  중첩하는 3-leg bracket은 지원하지 않는다.
- `OCO`와 `OTO`는 모두 `LIMIT`만 지원한다. 따라서 OCO의 손절 leg도
  stop-limit이며 갭 하락이나 거래정지 재개 시 체결 가격 또는 체결 자체가 보장되지 않는다.
- 해외 주식 조건주문은 거래 가능한 모든 세션에서 감시·발동한다. 정규장 전용 기능은
  장전에는 로컬 계획만 저장하고 정규장 확인 뒤에만 주문을 만든다.
- OCO/OTO는 계좌·종목당 그룹 주문 1개만 허용한다. Toss 앱에서 수동 등록한 주문도
  조회 결과에 포함되므로 기존 그룹 주문이 있으면 봇이 덮어쓰지 않고 해당 종목을 차단한다.
- 조건주문 수정은 기존 주문 취소 후 새 주문 생성으로 동작하며 ID도 바뀐다. 수정 중
  보호 공백이 생길 수 있으므로 v1에는 trailing/빈번한 OCO resize를 넣지 않는다.
- OCO create에는 `expireDate`가 필수다. plan에서 정한 literal `YYYY-MM-DD`를 body/hash에 잠그며,
  상세 응답에서 이 optional property가 빠지거나 cutoff/timezone 의미를 추정해야 하면 보호 확인에
  실패한다.
- 생성 응답의 필수 `conditionalOrderId`와 로컬에 미리 저장한 `clientOrderId`를 같은 로컬
  트랜잭션에 연결한다. 응답 echo는 optional/nullable이며, 값이 있을 때만 로컬 값과 exact 비교한다.
  목록·상세 응답만으로는 로컬 멱등키를 복원할 수 없다.
- 일반 주문 create의 10분 멱등 창과 달리 조건주문 create 문서는 그 창을 명시하지 않는다.
  Toss 확인 전에는 결과 불명 OCO create를 자동 재POST하지 않는 것이 live P0 조건이다.

### WebSocket

현재 구현된 shadow 연결은 다음 두 시세 채널만 사용한다.

- `trade:us:{symbol}`: 진입 가격 관찰
- `orderbook:us:{symbol}`: 스프레드와 시장성 검증

`personal:order:{accountSeq}`는 부분·전량 체결, 취소, 거부를 복구하는 미래 live runtime에서만
추가한다. 현재 shadow process에는 계좌번호도 전달하지 않는다.

시세 채널은 lossy이므로 누적 거래량을 재구성하거나 모든 틱을 받았다고 가정하지 않는다.
personal 주문 채널은 연결 session 안에서는 `LOSSLESS`지만 연결이 끊긴 gap에는 replay cursor나
재전송이 없으므로 재연결 직후 REST의 일반 주문, 조건주문, 보유수량을 다시 대조한다. 60초 간격
keepalive와 지수 백오프 재연결을 적용한다.

## 3. 장전 계획

장전 계획은 수정하지 않는 불변 레코드다. 가격이나 날짜를 바꾸면 새 `plan_id`와
새 승인 해시를 만든다. 현재 shadow-only Discord 앱은 계획 메시지의 일회용 승인 버튼과
plan hash 재확인 후 mode `0600` 영수증 하나만 새로 쓴다. DB 상태를 바꾸거나 arm·주문을
수행하지 않으며, 거절 버튼·거절 영수증·inbox 소비자도 아직 없다. 향후 live runtime만
영수증의 인증된 출처와 모든 broker/risk gate를 다시 검증한 뒤 `PLANNED -> APPROVED` CAS를
수행할 수 있다. 현재 같은 UID의 mode `0600` 파일은 그 자체로 인증된 출처가 아니다.

```yaml
plan_id: generated-id
mode: shadow
market: US
symbol: AAPL
session_date_et: YYYY-MM-DD

entry_trigger: DECIMAL
entry_limit: DECIMAL
target_trigger: DECIMAL
target_limit: DECIMAL
stop_trigger: DECIMAL
stop_limit: DECIMAL

risk_budget_usd: DECIMAL
max_notional_usd: DECIMAL
max_quantity: 1

entry_start: regular+00:05
entry_expiry: regular+01:00
force_exit_at: regular-00:15
regular_session_only: true
```

위 시간은 초기 검증용 운영값의 예시이며 수익 최적화값이 아니다. 실제 시각은 KST를
하드코딩하지 않고 Toss 미국장 캘린더의 `regularMarket`을 기준으로 계산한다.

필수 가격 관계는 다음과 같다.

```text
stop_limit <= stop_trigger < entry_trigger <= entry_limit
entry_limit < target_limit <= target_trigger
```

보수적인 계획 손실과 수량은 다음처럼 계산한다.

```text
variable_cost_per_share = target_limit * estimated_round_trip_cost_fraction
estimated_round_trip_cost = variable_cost_per_share * quantity + estimated_fixed_round_trip_cost
cash_required_per_share = entry_limit + variable_cost_per_share
risk_per_share = entry_limit - stop_limit + variable_cost_per_share
reward_per_share = target_limit - entry_limit - variable_cost_per_share

risk_budget = cashBuyingPower * risk_fraction
allocated_cash = cashBuyingPower * cash_allocation_fraction
qty_by_risk = floor((risk_budget - estimated_fixed_round_trip_cost) / risk_per_share)
qty_by_cash = floor((allocated_cash - estimated_fixed_round_trip_cost) / cash_required_per_share)
qty_by_notional = floor(max_notional_usd / entry_limit)
quantity = min(qty_by_risk, qty_by_cash, qty_by_notional, max_quantity)

planned_risk = (entry_limit - stop_limit) * quantity + estimated_round_trip_cost
planned_reward = (target_limit - entry_limit) * quantity - estimated_round_trip_cost
reward_risk_ratio = planned_reward / planned_risk
```

`estimated_round_trip_cost_fraction`은 활성 Toss 미국 수수료율의 두 배 이상이어야 한다.
비율로 표현되지 않는 최소 규제비용 등은 USD 단위 `estimated_fixed_round_trip_cost`로 별도
예약한다. 두 값에는 수수료 외 규제비용·오차 버퍼를 보수적으로 포함한다. 계획 저장 직전
`cash_reserved <= allocated_cash`, `planned_risk <= risk_budget`, `entry_notional <= max_notional`,
반올림 후 `reward_risk_ratio >= minimum_reward_risk_ratio`를 다시 확인한다.

갭과 LULD 거래정지 때문에 실제 손실은 이 계산을 넘을 수 있다. 이 값은 보장이 아니라
주문 전 위험 예약값이다. `quantity < 1`, buying power 불명, 가격 단위 불일치, 보상/위험비
미달, 기존 포지션·미해결 주문 존재 중 하나라도 해당하면 계획을 승인하지 않는다.

### 현재 장전 계획기 입력 검증 순서

1. `shadow`, 미국장, 뉴욕 timezone, universe/watchlist 비활성, live 이중 차단을 검사한다.
   자동 모드는 `runtime.symbols`가 비어 있어야 하고, 수동 모드만 정확히 한 종목을 요구한다.
2. Toss 미국장 `today.date`, `preMarket`, `regularMarket`을 읽고 휴장·단축장·DST 시간을 그대로 쓰며,
   `regular_open-plan_lead_minutes`부터 `regular_open-minimum_plan_lead_minutes`까지만 허용한다.
3. 전체 계좌가 flat인지 확인한다. 보유수량, 열린 일반 주문, 열린 조건주문이 하나라도 있으면
   중단하고, `USD cashBuyingPower`의 필수 필드·통화·유한 양수값과 활성 미국 수수료율을 검증한다.
4. 자동 모드는 `MARKET_TRADING_AMOUNT / US / realtime` 상위 20개를 후보 소스로 읽어 exact rank,
   symbol, USD, timestamp freshness와 설정된 가격·거래대금·등락률 범위를 검사한다. 이 필드의
   `tradingAmount`는 프리마켓 거래대금으로 해석하거나 기록하지 않는다.
5. NASDAQ·NYSE·AMEX 각각의 `ACTIVE / STOCK / commonShare=true` 목록을 strict 파싱하고 랭킹
   후보와 교집합을 만든 뒤, 랭킹 순서상 상위 5개만 다음 검사의 대상으로 삼는다.
6. 후보 상세가 요청한 종목과 정확히 일치하고 USD·ACTIVE·보통주·허용 거래소인지 확인한다.
   `/stocks/{symbol}/warnings`는 정확한 빈 배열 `[]`이어야 한다. 유효하지만 비어 있지 않으면
   해당 후보를 건너뛰고, malformed면 selector 전체를 fail-closed로 중단한다.
7. `adjusted=false`로 오늘 이전의 완료된 raw 일봉 20개를 요구하고 양수 거래량, 최신성,
   평균 일 거래가치와 평균 일중 범위 한도를 검사한다.
8. `adjusted=false` 프리마켓 1분봉을 페이지로 모으되 현재 시각까지 완전히 끝난 봉만 사용한다.
   최소 봉 수, 최신 완료 시각, 양수 누적 거래량, 프리마켓 범위 한도를 통과해야 한다.
9. 후보 확정 직전에 전체 계좌가 여전히 flat인지 다시 확인하고 USD `cashBuyingPower`를 다시 읽는다.
10. 해당 종목의 fresh `/prices`와 `/orderbook`을 다시 읽어 timezone 포함 timestamp, USD, age,
    상호 skew, 양수 잔량, `bid < ask`, 최대 스프레드와 last-mid 괴리를 검사한다.
11. 랭킹 timestamp가 아직 fresh인지 재검증하고, fresh 현재가와 랭킹 기준가로 최종 가격·등락률
    범위를 다시 계산한다. 그 뒤 보수적 기준가 `max(lastPrice, bestAsk)`로 가격·수량·R:R를 계산한다.
12. 최종 후보의 `/warnings`를 다시 조회해 여전히 정확한 빈 배열인지 확인한 뒤 전체 계좌 flat과
    USD `cashBuyingPower`를 다시 조회한다. warning 상태가 바뀌었거나 계좌가 깨끗하지 않으면
    잠그지 않고, 마지막 현금값으로 가격·수량·R:R 계획을 다시 계산한다.
13. DB `lock_at` 기준으로 최종 price·orderbook·cash·ranking·warning-check·account-check timestamp가
    각각의 freshness 한도 안인지 다시 검증한다. raw accountSeq 대신 해시된 account key로 계좌·미국
    거래일당 단일 INSERT를 시도하고 계획과
    공개용 Discord 알림을 한 SQLite 트랜잭션에 넣는다. claim lease를 얻은 프로세스만
    `SHADOW/실주문 없음` 메시지를 보내며, 실패·재시작 시 `PENDING` outbox만 다시 처리한다.

계좌·거래일 plan이 한번 잠기면 같은 프로세스의 다음 iteration이나 재시작에서도 자동 selector를
다시 실행하지 않는다. 저장된 symbol과 설정·guardrail 무결성을 확인한 뒤 그 plan을 그대로 쓴다.
뉴스와 LLM은 후보 점수·선택·가격·수량에 영향을 주지 않으며, plan이 잠근 한 종목만 뉴스 context와
WebSocket context로 내보낸다.

이 재검증으로 오래된 snapshot 사용 창은 제한하지만 원자성을 만들지는 못한다. Toss는 broker
계좌 상태·시장 데이터와 로컬 SQLite INSERT를 하나의 원자 snapshot/transaction으로 제공하지
않는다. 따라서 마지막 warnings/account GET 직후 외부에서 수동 주문이 제출되면 plan lock과
경합할 수 있다. shadow에서는 이 residual race를 기록 대상으로 두고, live에서는 전용 계좌·당일
수동/타 writer 금지와 계좌별 단일 writer fence를 동등한 배타 운영 경계로 채택한다. 이 규칙을
보장할 수 없으면 승격 blocker로 유지한다.

Discord webhook은 원격 멱등키를 제공하지 않으므로, Discord가 메시지를 받은 직후 로컬
`SENT` 기록 전에 프로세스가 죽는 극단적 구간에는 중복 알림이 가능하다. outbox의 보장은
유실 방지(at-least-once)이며 거래 실행의 정확성에는 영향을 주지 않는다. 웹훅 미설정도
계획을 실주문으로 승격하지 않으며 알림만 `PENDING`으로 유지한다.

## 4. 향후 live 실행 흐름 — offline core만 구현, production 연결 금지

```text
장전 계획 생성·승인
        |
        v
정규장 + 진입 시작 시각 확인
        | 세션/시세/호가/계좌 불명확
        +--------------------------> SKIPPED 또는 RECOVERY_REQUIRED
        |
        v
WebSocket에서 아래→위 entry_trigger 통과 확인
        | 현재가 > entry_limit
        +--------------------------> SKIPPED(reason=NO_CHASE)
        |
        v
entry_limit 지정가 BUY 1회 제출
        |
        v
체결 확인 ---- 미체결 만료 ----> 취소 확인 -> CANCELLED
        |
        v
OPEN_UNPROTECTED
        | 현재가가 이미 target/stop 밖 또는 OCO 거절·불명
        +--------------------------> EMERGENCY_EXIT / RECOVERY_REQUIRED
        |
        v
실제 체결 수량으로 OCO SELL/SELL 생성·WATCHING 확인
        |
        v
PROTECTED -> target 또는 stop 발동 -> 생성된 일반 SELL 체결 감시
        |                                      |
        | force_exit_at                        | stop-limit 미체결
        v                                      v
OCO 취소 확인 후 시간청산              제한된 재가격/비상청산
        \______________________________________/
                         |
                         v
                       CLOSED
```

진입은 로컬 WebSocket 조건으로 시작하고 Toss의 `SINGLE BUY` 조건주문은 v1에서 사용하지
않는다. 해외 조건주문은 세션 선택이 없어 Mac 장애 시 진입 만료 뒤 프리·애프터마켓에서
고아 진입이 발동할 수 있기 때문이다. Mac이나 WebSocket이 끊기면 신규 진입을 놓치는 쪽으로
실패한다.

## 5. 체결과 보호주문

Toss는 매수와 양쪽 청산을 하나의 원자적 bracket으로 제공하지 않는다. 따라서 BUY 체결과
OCO의 `WATCHING` 확인 사이에는 필연적인 `OPEN_UNPROTECTED` 구간이 있다.

v1 live pilot은 1주로 제한해 부분체결 수량 변경을 제거한다. 수량을 늘리는 단계에서는 다음
절차를 먼저 검증한다.

1. 첫 `PARTIAL_FILL`에 진입 잔량 취소를 요청한다.
2. 취소 또는 전량 체결의 terminal 상태를 REST로 확인한다.
3. authoritative 누적 `filledQuantity`를 받을 때마다 현재 exposure/`owned_qty`를 단조 갱신한다.
4. entry가 terminal일 때 그 최종 누적값을 진입 총량으로 고정하고 해당 수량으로 OCO를 한 번 생성한다.
5. `protected_qty == owned_qty`가 확인되기 전에는 다른 진입을 금지한다.

OCO 생성 시 현재가가 이미 익절선 위나 손절선 아래라면 `condition-already-met`가 발생할 수
있다. 이 경우 OCO를 반복 제출하지 않고 보유수량을 재확인한 뒤 직접 비상청산하고 당일 신규
진입을 중단한다. timeout, connection reset, 409, 429, 5xx, 응답 ID 누락은 실패로 확정하지
않고 `UNKNOWN`으로 보존한 뒤 read-only 대조한다. 조건주문 멱등키의 시간창이 공식적으로
확인되기 전에는 같은 body/key라도 자동 재POST하지 않으며, ID를 권위 있게 복구하지 못하면
OCO와 별도 SELL 양쪽을 모두 막고 `RECOVERY_REQUIRED`로 둔다.

손절 leg는 다음처럼 stop-limit으로 만든다.

```text
second.triggerPrice = stop_trigger
second.orderPrice = stop_limit  # trigger보다 낮은 제한된 체결 여유
```

하단 leg가 발동하면 `triggeredOrderId`의 일반 SELL 주문을 추적한다. 정해진 보호 SLO 안에
체결되지 않으면 현재 매수호가와 sellable quantity를 다시 확인해 제한적으로 재가격하거나
비상청산한다. 거래정지·호가 소실 상태에서는 체결을 가정하지 않고 신규 진입 중지와 긴급
Discord 알림을 유지한다.

## 6. 영속 상태와 불변조건

최소 상태기계는 다음과 같다.

```text
PLANNED -> APPROVED -> RECONCILING -> READY_TO_ENTER
        -> ENTRY_SUBMITTING -> ENTRY_WORKING -> OPEN_UNPROTECTED
        -> PROTECTED -> EXITING -> CLOSED
```

종결·복구 상태는 `SKIPPED`, `CANCELLED`, `RECOVERY_REQUIRED`다. market halt 같은 flat 차단은
별도 state를 늘리지 않고 `SKIPPED`의 allowlisted `reason_code`로 기록한다.

반드시 지키는 불변조건:

- `(account, session_date, strategy, symbol)`당 active plan 1개
- `(account, clientOrderId)`를 DB에서 원자적으로 선점
- 계좌별 live 주문 writer 1개
- broker order ID가 없어도 UNKNOWN intent가 있으면 신규 진입 금지
- 봇이 매도할 수 있는 수량은 자기 체결 원장으로 증명한 `owned_qty`뿐
- 수동 또는 다른 전략 보유분은 자동 채택·매도하지 않음
- `protected_qty == owned_qty`; 불일치 시 신규 진입 금지
- 위험감소 주문(`ENTRY_CANCEL`, `PROTECTIVE_EXIT`, `EMERGENCY_EXIT`)은 신규 진입용
  주문 건수·손실 퓨즈 때문에 차단하지 않되, 소유수량·sellable quantity 검증은 유지
- 재시작 시 전략 평가보다 일반 주문·조건주문·체결·보유수량 대조를 먼저 수행

기존 실행 원장을 버리고 새 주문 원장을 만들지 않는다. 기존 원장에 주문 역할과 계획 연결을
추가하고, `intraday_plans`와 조건주문 ID 매핑만 최소로 저장한다. 상태 변경 근거는 append-only
event로 남긴다.

## 7. 장중 위험 제한

- 일일 실현손익 + 보수적 미실현손익 + 비용을 거래일별로 계산한다.
- 일일 손실 한도, 연속 실패 한도, UNKNOWN 주문, stale quote, buying power 불명 중 하나면
  신규 진입만 중단한다. 기존 OCO 유지·진입취소·포지션 청산은 계속 허용한다.
- 스프레드는 절대값과 `risk_per_share` 대비 비율을 함께 검사한다.
- 진입 주문은 짧은 만료 후 취소하며 시장가 추격을 하지 않는다.
- 같은 종목의 수동 보유, 열린 일반 주문, OCO/OTO가 하나라도 있으면 진입하지 않는다.
- 정규장 종료 전 `force_exit_at`에 잔여 포지션을 정리하고 익일 보유하지 않는다.
- 미국 LULD 거래정지, 갭, stop-limit 미체결을 정상적인 실패 시나리오로 테스트한다.

Tailscale 주소는 SSH 관리 경로일 뿐 Toss API의 허용 IP가 아니다. Toss REST와 WebSocket은
MacBook의 공인 egress IP를 보므로 WTS의 허용 IP와 실제 egress가 일치해야 한다.

## 8. 뉴스 LLM과 Discord

뉴스 worker는 기존 data-diode 경계를 그대로 유지한다. Discord 승인 앱은 뉴스 webhook과
별도 프로세스·토큰으로 운용하는 유일한 제한형 입력 경로다. 두 프로세스의 자격증명은
분리하지만 모든 승인·거래·뉴스 출력은 동일한 하나의 허용 channel로만 보낸다.

- 거래 프로세스가 redacted symbol context만 내보낸다.
- 뉴스 worker에는 Toss 자격증명, 거래 DB 쓰기, 계획 승인·수정 권한이 없다.
- LLM이 만든 감성·호재/악재·가격·추천은 거래 상태기계 입력으로 사용하지 않는다.
- 뉴스 webhook은 중립 뉴스 요약만 출력한다. 승인 앱은 `PLAN` 메시지의 일회용 승인 버튼만
  처리하며 주문 제출, 설정 변경, emergency stop 해제 권한이 없다.

## 9. 현재 저장소 구현 범위

2026-08-30 현재 완료된 항목:

- `intraday.py`: Decimal 현금·위험 수량, 미국 호가단위, 비용 예약, 최소 R:R, 결정적 ID
- `operations.py`: 기존 runtime과 분리된 장전 read-only 분기, 자동 selector와 strict Toss 응답
  검증. `MARKET_TRADING_AMOUNT / US / realtime` 상위 20개와 NASDAQ·NYSE·AMEX 거래 가능
  보통주 교집합의 상위 5개에 대해 warnings `[]`, raw 이전 일봉 20개, 완료된 프리마켓 1분봉,
  fresh 현재가·호가, 최종 warnings·계좌·현금·가격·등락률과 DB lock 시각 freshness를
  재검증하고 한 종목을 불변 잠금
- `state_store.py`: hash 검증을 포함한 immutable `intraday_plans`, 계좌·거래일 unique lock,
  계획과 원자 저장되는 claim-lease 기반 notification outbox
- `toss_conditional.py`: 공식 SINGLE/OCO/OTO create/list/get/modify/delete 계약 adapter
- `notifier.py`: 민감 계좌키를 제외한 plan/blocked Discord 알림과 mention 차단
- `operations.py`: immutable plan에서 승인 worker 전용 redacted envelope를 mode `0600`으로
  atomic export하고, 최초 생성 no-clobber·갱신 직전 재검증·만료 단조성·표시값 drift 시 nonce
  회전을 적용
- `turtle_approval`: Discord intents `0`, exact user/guild/channel, hash modal, 만료·TOCTOU 재검증,
  전체 표시값 binding과 temp-write/fsync/no-clobber publish의 mode `0600` one-shot receipt만
  제공하는 거래 코드·DB·Toss 비의존 worker
- `toss_stream.py`: immutable plan에서 파생된 context의 한 종목만 `trade:us`·`orderbook:us`로
  구독하고 exact ACK, REST baseline, 재연결·재동기화를 수행하는 read-only shadow stream
- `config/intraday.example.yaml`: 모든 경제값을 명시해야 하는 fail-closed 템플릿

아직 구현하지 않은 항목:

- BUY 체결 후 OCO 보호, `OPEN_UNPROTECTED` watchdog, 비상청산 상태기계
- 1분봉 shadow 체결 시뮬레이션과 당일 P&L/loss fuse
- 실주문 ownership 원장과 원자적 live idempotency 예약
- live `personal:order` 구독과 일반·조건주문·보유수량 reconciliation 상태기계
- authoritative 미국 halt/LULD 상태 source와 그 fail-closed live gate
- 마지막 broker GET과 SQLite plan lock 사이 외부 수동 주문 race를 줄이는 전용 계좌·당일
  수동/타 writer 금지와 계좌별 단일 writer fence

따라서 조건주문 adapter는 계약 테스트용 기반일 뿐 현재 `intraday` 서비스에서 호출하는 것은
목록 조회뿐이다. 생성·수정·삭제 메서드는 live runtime에 연결되어 있지 않다.

새 전략 프레임워크, 범용 scanner, 별도 dashboard, 임의 명령 Discord bot은 만들지 않는다.
자동 선정은 구현된 기존 Toss universe/ranking/candle/시세 read-only 함수로 장전 후보 한 종목만
잠그는 경계로 제한한다. 현재 Discord 앱은 해당 plan의 승인 영수증만 만들고, 향후 소비자가
별도 검증 뒤 승인 상태 전이를 담당한다. 현재 Toss REST에는 authoritative 미국 halt/LULD 필드가
없으므로 자동 선정이 성공해도 live 승격 조건을 충족하지 않는다. broker 계좌·시장·로컬 DB의
원자 snapshot도 없으므로 최종 재검증 뒤 외부 수동 주문 race 역시 live 승격 blocker다.

## 10. 검증과 승격

현재 1단계 구현은 현재 checkout을 명시한 `PYTHONPATH=src` 회귀 테스트와 `compileall`,
`pip check`, `git diff --check`를 통과했다. 시스템 Python이 다른 editable
checkout을 import하는 상태도 확인했으므로 Mac 배포 전에는
[macOS source gate](macos-operations.md#intraday-shadow-planner-source-gate)를 반드시 통과해야 한다.
자동 selector의 실제 Toss 응답과 Mac 실행을 함께 검증하는 외부 shadow smoke는 아직 남아 있다.

### 현재 자동화된 계약·단위 테스트

- OCO/OTO/SINGLE payload와 미국 호가 단위
- 숫자 오타·YAML bool·NaN을 0으로 바꾸지 않는 fail-closed 설정 파싱
- 조건주문 DELETE 204, 수정 후 새 ID, 종목당 그룹 주문 충돌
- `condition-already-met`, 409, 429, timeout, 잘린/비정상 응답, 5xx의 UNKNOWN 대조
- Discord 실패 후 재시작 전송, 동시 outbox claim, 예외 문자열 secret canary 비노출
- 자동 selector의 exact ranking·거래 가능 보통주 교집합·상위 후보 제한, warnings `[]`, raw 일봉,
  완료된 프리마켓 1분봉, fresh 현재가·호가, 최종 warnings·flat·현금·가격·등락률과 lock 시각
  freshness 재검증, 재시작 무재선정
- 승인 envelope/worker schema, redaction, stable nonce, expiry 단조성, exact allowlist,
  전체 표시값 modal TOCTOU, 선 ACK, temp-write/fsync/no-clobber receipt, symlink·중복·위험
  환경변수 거부

### 향후 실행 엔진 필수 테스트

- live personal WebSocket의 session-lossless 전달, reconnect gap 미재생과 일반·조건주문·보유 REST 대조
- 진입 제한이 protective/emergency exit를 막지 않는지
- 수동 보유분을 전략 수량으로 채택하거나 매도하지 않는지

### 전략·고장 주입

- 같은 1분봉에서 target과 stop이 모두 닿으면 stop 우선
- 갭이 stop-limit 아래로 발생, LULD 거래정지, stale quote
- BUY ACK 뒤 Mac 강제종료, OCO 생성 전 네트워크 단절
- OCO 생성 응답 유실, stop 발동 뒤 생성 주문 미체결
- 장 종료, DST 전환, 휴장·단축장
- 두 프로세스가 같은 계획을 동시에 제출

### 장기 승격 순서

1. P0 source/주문 안전 결함 수정과 깨끗한 canonical checkout 검증
2. 과거 1분봉 백테스트와 실제 WebSocket `shadow` 기록
3. 최소 100개 신호에서 중복 진입·세션 오류·stale 진입 0건 확인
4. paper fault test에서 재시작 후 포지션·주문·OCO 대조 확인
5. 수동 승인, allowlist 1종목, 1주, 하루 1회 live pilot
6. 보호되지 않은 포지션 0건과 종료 전 잔여 포지션 0건을 확인한 뒤에만 수량 확대

이번 작업은 1–4의 비실거래 증거까지만 다루며 5–6은 실행하지 않는다. 현재 P0 감사 결과와 위
미구현 항목이 해소되기 전에는 이 기능을 실주문 runtime에 연결하지 않는다.

## 11. 실거래 엔진 구현 설계 — 실제 주문 시험 제외

이 절은 구현 순서와 각 단계의 완료 조건이다. 목표는 기능 수를 늘리는 것이 아니라
`중복 매수 0`, `전략이 소유하지 않은 수량 매도 0`, `세션 밖 진입 0`,
`UNKNOWN 상태의 무근거 재제출 0`을 fake broker와 replay에서 증명하는 최소 runtime이다.
현재 작업은 실행 엔진의 상태·요청을 구현하되 실제 Toss mutation endpoint에는 연결하지 않는다.
세부 비실거래 완료 계약은 13절이 우선한다.

### 11.1 구현 착수 전 P0

다음 항목 중 하나라도 충족되지 않으면 코드가 있어도 live flag를 열지 않는다.

1. Mac 최신 committed SHA를 기준으로 clean integration checkout을 만든다.
2. Mac과 Windows의 dirty source/test/ops 변경은 allowlist snapshot으로 각각 보존한다.
   runtime DB, WAL/SHM, model, log, backup, local config, secret은 포함하지 않는다.
3. `operations.py`, `state_store.py`, `config.py`, `notifier.py`는 어느 한쪽을 선택하지
   않고 함수·migration 단위로 수동 통합한다.
4. gateway와 모든 주문 writer를 disarm하고 live config를 `false`, emergency stop을
   `true`로 고정한다. 이 변경은 운영 상태 변경 승인을 받은 maintenance window에서만 한다.
5. 손상 판정된 운영 SQLite 원본을 직접 열어 migration/repair하지 않는다. 서비스 정지 뒤
   SQLite backup API 또는 recovery 복제본을 만들고, 원본과 복제본을 주문·포지션 REST
   snapshot과 대조한다.
6. canonical checkout에서 exact-path import, 전체 테스트, `compileall`, `pip check`,
   `git diff --check`, DB `quick_check`, plist lint를 통과한다.
7. 계좌별 주문 writer가 정확히 1개이고 dashboard/gateway가 별도 writer를 만들 수 없는지
   process/container/launchd 수준에서 확인한다.
8. `ops/run-dashboard-macos.command`, multi-user gateway, Docker dashboard와
   `health.py`의 상태변경 POST action은 live intraday release에서 제외한다. 현재 구현은 인증된
   read-only 관리면이 아니므로 loopback이나 Tailscale 뒤에 있다는 이유로 주문 capability를 주지
   않는다.
9. live 전용 계좌를 사용하고 그 거래일에는 Toss 앱·다른 전략·다른 process의 수동 주문을 금지한다.
   Toss는 계좌 상태와 로컬 DB를 원자적으로 예약하지 않으므로 이 배타 운영 규칙을 받아들일 수
   없으면 live를 열지 않는다. 알 수 없는 외부 주문·보유 변화가 보이면 자동 채택·매도하지 않고
   `RECOVERY_REQUIRED`로 고정한다.
10. authoritative 미국 halt/LULD source를 선택하고 stale·불일치·provider 장애를 fail-closed로
    처리하거나, 이 조건을 포기하지 않는 한 Toss API 단독 live를 계속 차단한다. 빈 warnings와
    `price-limits=null`, 주문 거절 코드는 사전 halt 확인 수단이 아니다.
11. exact SHA 이름의 side-by-side release와 hash-pinned dependency lock을 만들고 source·venv는
    root 소유 read-only, config·DB·log·Keychain은 release 밖에 둔다. plist는 mutable checkout이나
    `/current` symlink가 아니라 검증한 exact SHA 절대경로를 가리킨다.
12. approval, news, trade, watchdog의 설정·원격 webhook metadata가 모두 운영자가 정한 같은 한
    channel인지 매 전송에 다시 확인한다. 비실거래 구현의 trade notifier도 첫 성공을 영구 cache하지 않지만 live
    전에 제거한다. webhook이 다른 channel로 이동하면 전송하지 않는다.
13. local watchdog 외에 Mac 전원·인터넷 전체 장애를 감지할 별도 상시 노드의 deadman을 둔다.
    둘 다 주문 권한 없이 같은 한 channel에만 경보한다. 외부 deadman이 없으면 Mac 전체 장애 알림을
    보장할 수 없으므로 live 운영 위험으로 명시하고 pilot을 열지 않는다.
14. DB backup은 서비스 상태를 대조한 뒤 SQLite backup API로 만들고 복제본 `quick_check`와 hash를
    검증한다. WAL/SHM을 제외한 파일 zip은 rollback 자료로 쓰지 않는다. flat·owned order 0이
    확인되기 전에는 binary나 DB를 rollback하지 않는다.

### 11.2 최소 코드 경계

기존 코드를 재사용한다.

| 책임 | 사용 코드 | 변경 원칙 |
| --- | --- | --- |
| 현금·위험·가격·수량 계산 | `intraday.py` | 순수 Decimal 계산 유지, 네트워크 금지 |
| 일반 주문 transport | `TossLiveBrokerAdapter` | payload/parse만 재사용, lifecycle orchestration은 `intraday_live.py`가 담당 |
| OCO 계약 | `toss_conditional.py` | create/list/get/delete 계약을 runtime에서 호출 |
| pre-trade 제한 | 기존 `PreTradeSafety` | 신규 진입과 위험감소 주문 정책을 분리 |
| 휴장·단축장·DST | 기존 market calendar | 고정 UTC 시각으로 대체하지 않음 |
| 계획·outbox | 기존 `SQLiteStateStore` | canonical migration과 CAS 상태만 추가 |
| 거래 알림 | 기존 notifier | 운영 상태 전송과 계획 승인 메시지 생성 |

기존 `LiveOrderOrchestrator`는 단발 submit/cancel 원장이지 partial fill·OCO·재시작 lifecycle이
아니며, cancel 응답의 새 order ID로 원주문 mapping을 덮을 수 있어 intraday 상태기계로 직접 쓰지
않는다. `TossPositionSync(sync_live_positions=True)`도 broker-only holding을 기존 momentum position으로
자동 채택하므로 ownership 판정에 쓰지 않는다. strict read-only broker parser만 참고하고, malformed
holding/order item을 조용히 건너뛰지 않는 intraday 전용 대조를 둔다. 미국 modify는 price-only이므로
현재 `ModifyOrderRequest.quantity`는 제거하고 원주문·정정·취소가 반환한 새 operation ID chain을
append-only event로 보존한다.

현재 `toss_stream.py`까지 구현됐으므로 남은 신규 production 파일은 하나만 추가한다.

- `src/turtle_bot/intraday_live.py`: 저장 계획 로드, broker reconciliation, 상태 전이,
  진입·보호·시간청산 orchestration만 담당한다.

`operations.py`에는 `strategy.kind == intraday` dispatch와 dependency wiring만 둔다.
새 base class, event bus, plugin framework, 범용 scanner, dashboard, Discord slash-command
bot은 추가하지 않는다. 버튼 전용 승인 worker 한 개만 별도 capability로 둔다.

live에서는 별도 shadow stream process를 동시에 사용하지 않는다. `intraday_live.py`가 기존
`toss_stream.py`의 transport·frame validator를 재사용해 한 WebSocket에서 잠긴 한 종목의
`trade:us`·`orderbook:us`와 계좌 전체 `personal:order`를 함께 선언한다. 계좌 topic은 주문 상태를
받기 위한 것이지 두 번째 종목 선정 경로가 아니다. 전략 계산과 주문 상태의 권위는 WebSocket이
아니라 REST 재대조와 SQLite 원장이다.

### 11.3 설정 계약

경제적 위험값은 암묵적 기본값을 두지 않고 반드시 YAML에 명시한다.

현재 config 이름을 유지하고 live 구현 때 아래 필드만 추가한다. 같은 뜻의 두 번째 설정 계층은
만들지 않는다.

```yaml
toss:
  live_enabled: false

runtime:
  mode: shadow
  market: US
  timezone: America/New_York
  symbols: []                 # automatic selector owns this

strategy:
  kind: intraday
  intraday:
    selection:
      mode: automatic
    cash_allocation_fraction: null
    risk_fraction: null
    max_notional: null
    max_quantity: 1
    entry_start_minutes_after_open: null
    entry_expiry_minutes_after_open: null
    force_exit_minutes_before_close: null
    protection_slo_seconds: null       # planned field; shadow p99로 확정
    exit_fill_slo_seconds: null         # planned field; shadow p99로 확정
    emergency_market_fallback: null    # planned explicit consent
    live_execution_enabled: false

live:
  emergency_stop: true
  allowed_symbols: []         # intraday에서는 두 번째 정본으로 쓰지 않음; plan symbol만 effective allowlist
```

하루 한 번만 신규 진입하므로 v1에는 별도 `max_daily_loss_usd`를 추가하지 않는다. 승인 계획의
`risk_budget = cashBuyingPower * risk_fraction`이 계획당·일일 신규 위험 한도를 겸하고, 첫 제출
뒤에는 재진입을 영구 금지한다. 실제 손실은 갭과 stop-limit 미체결로 계획값을 넘을 수 있다.
그 경우 loss fuse는 새 BUY만 막을 뿐 이미 생긴 손실을 제한한다고 표현하지 않는다.

다음 값은 사용자 입력이나 장기 config로 받지 않고 각 계획·제출·복구 시점에 Toss API에서
새로 조회한다.

- 계좌 목록과 계좌 유형; 계좌가 하나면 자동 선택하고 둘 이상일 때만 운영자가 한 번 선택
- USD 현금 기반 매수 가능 금액, 보유종목, 종목별 매도 가능 수량
- 현재 계좌의 미국시장 수수료율과 KRW/USD 환율
- 종목 기본정보·상장 상태·warnings, 현재가, 호가, 최근 체결, 1분봉·일봉. 현재 REST에서
  authoritative 미국 halt/LULD 상태는 조회할 수 없음
- 거래소가 제공하는 당일 상·하한가, 미국 휴장·정규장·단축장 세션 시각
- 열린/종료 일반 주문과 체결 상세, 열린/종료 조건주문과 발동 주문
- 서버가 응답 헤더로 알리는 현재 rate limit과 retry 시각

API 조회값이 없거나 stale·불일치하면 사용자에게 값을 대신 입력받지 않고 해당 계획 또는
진입을 중단한다. 특히 미국 주식의 공식 `price-limits`는 상·하한가가 `null`일 수 있으며,
이는 전략의 익절·손절 가격이 아니다. 익절·손절은 별도의 전략 정책과 위험 한도에서 계산한다.

따라서 사용자가 결정할 trading 정책은 선정 방식, 주문당 최대 USD 투입액, 계획당 최대 USD
손실, 가격 계획을 직접 입력할지 시스템 제안 후 승인할지, 봇 소유수량의 OCO 실패 시 자동
비상청산 허용 여부, 매일 수동 arm 여부로 축소한다. 하루 1진입 pilot에서는 거래당 손실과
일일 손실 설정을 따로 두지 않고 하나의 `max_loss_usd`로 사용한다.

2026-08-29 사용자 선택과 보수적 해석:

- 종목은 시스템이 자동 선정한다. 미국 거래 가능 보통주에서 유동성·spread·1분/일봉 변동성과
  정확한 빈 warnings를 검사해 한 종목만 제안하고, 뉴스/LLM 점수는 선정에 쓰지 않는다. 현재
  REST에는 authoritative 미국 halt/LULD 상태가 없으므로 이를 검사했다고 주장하지 않으며 live
  승격을 차단한다.
- 고정 USD 투입액 대신 `cash_allocation_fraction`을 사용한다. 실제 현금 한도는
  `cashBuyingPower * cash_allocation_fraction`이며, 이 금액을 강제로 모두 주문하지 않고
  손절거리 기반 risk 수량과의 최솟값만 사용한다. 1주 pilot 상한은 그대로 유지한다.
- 가격은 시스템이 계산하고 사용자가 plan hash를 승인한다. 이 승인이 당일 신규 BUY 권한인
  `arm`과 동일하므로 사용자에게 별도의 arm 절차나 용어를 추가하지 않는다.
- 재부팅 후에는 자동 신규 진입을 재개하지 않는다. FileVault 로그인 뒤 broker 대조를 완료하고
  새 계획을 다시 승인한다. 기존 broker OCO 감시·복구는 신규 진입 승인과 분리한다.
- 뉴스는 잠긴 선정 종목의 새 기사마다 개별 Discord 항목을 보낸다. provider ID/URL로 중복을
  줄이되 Discord 결과 유실 구간의 at-least-once 중복 가능성은 유지한다. run당 전송 상한은 drop이
  아니라 rate-control이며 먼저 모든 검증 기사를 news DB에 `PENDING`으로 넣고 같은 session/24시간
  window 안에서 oldest-first로 다음 run이 이어 보낸다. 여기서 “새 기사”는 provider가 실제 반환하고
  exact-symbol 검사를 통과한 항목이며 인터넷 전체의 실시간·완전 수집을 보장한다는 뜻은 아니다.
- OCO를 정해진 보호 SLO 안에 확정하지 못하면 긴급 알림과 함께 당일 해당 plan으로 매수해
  아직 남아 있는 전 수량을 자동 비상청산한다. 일부 수량이나 최초 1주만 파는 것이 아니다.
  청산 수량은 `당일 plan의 REST 확정 BUY 체결 합계 - 같은 plan의 확정 SELL 체결 합계`로만
  계산하며 기존 보유분·수동 매수분·다른 전략 수량은 포함하지 않는다.

손실 한도를 모르는 초기값은 수익 규칙이 아니라 pilot 안전 상한으로 잡는다. 업계 교육에서
거래당 계좌의 0.5–2% 수준을 예시로 들지만, 첫 자동화 pilot은 그보다 낮은
`cashBuyingPower * cash_allocation_fraction * 0.25%`를 제안한다. 하루 한 번만 진입하므로 이
값이 계획당·일일 손실 상한을 겸한다. 비용을 포함한 1주의 손절 위험이 이 상한을 넘으면 해당
종목을 건너뛰며, shadow 결과만으로 자동 상향하지 않는다.

`null`, 누락, YAML bool 오해, NaN/Infinity, 음수, 1 초과 fraction, 시각 역전,
timezone 없는 timestamp는 시작 전에 오류로 종료한다. 첫 live pilot은 미국 주식,
장전 자동 선정 뒤 잠긴 1종목, 정수 1주, 하루 1진입, 익일 보유 금지로 하드 제한한다.
위험 비율, 현금 사용 비율, 최대 일손실, 진입·강제청산 시각, 보호 SLO는 사용자가
승인하기 전까지 live config를 생성하지 않는다.

### 11.4 선정 종목 전용 WebSocket

현재 `toss_stream.py`는 planner가 `news-context.json`에 잠근 정확한 한 종목의 시세·호가만
별도 동기 process에서 구독한다. 자동 모드는 `runtime.symbols`를 비워 두고 구현된 selector가
immutable plan의 종목을 정한다. 재시작해도 같은 계좌·거래일 plan을 다시 선정하지 않으며,
stream과 뉴스 worker는 이 잠긴 context 외에 두 번째 종목 설정 경로를 만들지 않는다.

```text
[{"id":"shadow-..."},
 {"type":"trade:us","codes":["{selected_symbol}"]},
 {"type":"orderbook:us","codes":["{selected_symbol}"]}]
```

구독 배열 하나가 기존 구독 전체를 교체하는 full-replace 선언이다. 서버의 단일
`type=subscriptions` ACK에서 request ID, 정확한 두 full topic set, 빈 `rejected`를 확인한다.
ACK 전에 data가 오거나 일부/추가 topic, 다른 symbol, 개인 주문 topic이 있으면 연결을 폐기한다.
AsyncAPI `1.2.2`와 2026-08-30 원본 SHA-256은 코드·계약 문서에 고정했다. watchlist 전체,
미선정 종목, 뉴스, `personal:order`는 이 연결에서 구독하지 않는다.

현재 runtime이 동기식이므로 `websockets.sync.client.connect`를 사용한다. 연결에는
고정 `open_timeout`, `close_timeout`, `max_size=64KiB`, `max_queue=16`을 두고 무제한 buffer를
금지한다. 라이브러리 자동 ping은 끄고 Toss 계약의 순수 텍스트 `PING`을 60초마다 보내며
15초 안에 JSON pong이 없으면 연결을 폐기한다. reconnect는 1·2·4초 지수 backoff와 20%
jitter, 30초 cap, process당 8회 한도를 사용한다. LaunchAgent는 비정상 종료만 다시 시작한다.

아래 중 하나면 즉시 신규 진입을 막고 `RECONCILING`으로 전이한다.

- 연결 종료, pong timeout, server shutdown
- ACK 누락·거절, 예상하지 않은 symbol/channel
- binary, JSON/schema 오류, duplicate key, 64KiB 초과
- timestamp 역행·stale·future, USD 불일치, 빈·정렬 오류·교차 호가

연결·ACK 뒤 `/prices`, `/orderbook`을 exact symbol/USD/positive non-crossed book으로 검증하고
재연결마다 반복한다. 공식 REST 계약상 timestamp는 optional/null이므로 값이 없거나 stale이면
연결을 끊는 schema 오류로 만들지 않고 baseline을 `verified=false`로 유지해 주기적으로 다시
조회한다. 이 동안 `shadow_usable=false`다. 공식 계약에는 REST와 stream을 잇는 cursor·sequence·
원자 경계가 없으므로 gap-free replay를 주장하지 않는다. `market-stream.json`의
`shadow_usable`은 broker/receive timestamp와 `valid_until`을 모두 만족하는 관측 진단일 뿐이고
`ready_for_live_entry=false`는 항상 유지한다. 누적 거래량을
자체 복원하거나 유실 메시지를 추정하지 않는다. 일반 주문·조건주문·보유·buying power 대조와
`personal:order`는 미래 live 상태기계에서 별도로 구현하기 전까지 이 process의 범위가 아니다.

live 전환 시 shadow process를 중지하고 동일 validator를 live runtime 안에서 사용한다. 한 번의
full-replace 선언은 정확히 다음 세 topic만 포함한다.

```text
trade:us:{selected_symbol}
orderbook:us:{selected_symbol}
personal:order:{accountSeq}
```

계정당 동시 WebSocket 연결은 최대 2개이고 새 연결이 가장 오래된 연결을 종료할 수 있으므로,
legacy·shadow 연결이 남아 있으면 live startup을 실패시킨다. personal event의 `PARTIAL_FILL/FILL`
등은 REST 재조회 사유로만 명시적으로 매핑한다. personal frame에는 sequence/replay/event timestamp가
없고 `orderedAt`은 주문 생성시각일 뿐이므로 frame 수량으로 projection을 직접 갱신하지 않는다.
각 frame은 broker state를 dirty로 만들고 exact order detail과 account snapshot을 다시 읽게 한다.

시작과 재연결 순서는 `REST snapshot -> connect/ACK -> REST snapshot -> 안정 projection 공개`다. WS에는
replay cursor, sequence, 최초 snapshot, `clientOrderId`가 없으므로 두 snapshot 사이의 무손실을
주장하지 않는다. 두 결과가 불일치하거나 snapshot 도중 mutation이 진행 중이면
`RECONCILING`을 유지하고 신규 진입 신호를 버린다.

### 11.5 영속 상태기계

상태는 메모리가 아니라 SQLite에 먼저 기록한다.

```text
PLANNED -> APPROVED -> RECONCILING -> READY_TO_ENTER
  -> ENTRY_SUBMITTING -> ENTRY_UNKNOWN | ENTRY_WORKING
  -> ENTRY_CANCELING -> OPEN_UNPROTECTED
  -> PROTECTION_SUBMITTING -> PROTECTION_UNKNOWN | PROTECTED
  -> EXIT_CANCELING_PROTECTION -> EXIT_SUBMITTING -> EXIT_UNKNOWN | EXIT_WORKING
  -> CLOSED
```

종결·차단 상태는 `SKIPPED`, `CANCELLED`, `RECOVERY_REQUIRED`다. 프로세스는
항상 `RECONCILING`에서 시작하고 broker 대조가 끝나기 전에는 전략 신호를 읽지 않는다.
사용자 관점의 `arm`은 Discord 계획 승인 한 번뿐이다. `READY_TO_ENTER`는 별도 사용자 동작이 아니라
그 승인과 현재 broker gate를 runtime이 다시 확인했다는 내부 상태다.

새 표는 `intraday_runs` 하나뿐이다. 여기에는 `plan_id`, `approved_envelope_sha256`, `state`,
`version`, writer/sync fence, entry·protection·active-exit intent pointer, triggered exit order ID,
`owned_qty`, `protected_qty`, 평균 진입가, 보호 공백 시작 시각, loss-fuse 시각, 마지막 broker
sync 시각, redacted halt reason만 저장한다. 일반 주문 상세와 append-only transition 근거는
기존 `order_intents`, `execution_orders`, `execution_events` 원장을 그대로 사용한다.

기존 원장에는 13.3절 migration으로 account/plan/role, exact request JSON·hash, 첫 시도와 recovery
deadline, 누적 fill projection을 추가한다. 기존 `idempotency_key`가 Toss `clientOrderId`의 정본이다.
local create role은 `ENTRY`, `PROTECTION`, `FORCE_EXIT`, `EMERGENCY_EXIT`뿐이다. cancel과 broker가
만든 protective exit는 기존 intent의 append-only event로 남긴다. intraday 경로는 현재 mutable
upsert를 호출하지 않고 immutable `INSERT`와 partial unique index를 사용한다. 조건주문도
`PROTECTION` intent로 같은 원장에 넣고 별도 OCO 원장을 만들지 않는다.

모든 전이는 다음 compare-and-swap을 만족해야 한다.

```text
UPDATE intraday_runs
SET state=:next, version=version+1, ...
WHERE plan_id=:plan_id AND state=:expected AND version=:version
```

영향 row가 1이 아니면 두 번째 writer 또는 stale process로 간주하고 즉시 fence한다.
DB transaction 안에서 다음 상태와 idempotency/client order ID를 먼저 예약한 뒤에만 네트워크
요청을 보낸다. HTTP timeout, connection reset, 408/425, 구조화된 `request-in-progress` 409, 429,
5xx, 응답 ID 누락은 실패가 아니라 `*_UNKNOWN`이다. `idempotency-key-conflict`나 same key/different
body는 UNKNOWN이 아니라 로컬 identity 위반이므로 즉시 fence+`RECOVERY_REQUIRED`다. UNKNOWN에서는
같은 요청을 blind retry하지 않는다.

일반 order create의 broker 멱등 창은 10분뿐이고 주문 조회·WS에는 client ID가 없다. 따라서
일반-order UNKNOWN create는 `first_attempt_at`부터 8분인 로컬 deadline 안에서만, 저장된 canonical
body와 동일한 semantic payload·동일 client ID로 제한된 identity-recovery 재호출을 허용한다. 재호출 전에
REST holdings, OPEN 전체, 해당 거래일 CLOSED 전체 page, 조건주문을 대조하고, 재호출이 주문 ID를
돌려주면 그 주문을 채택한다. ENTRY recovery POST는 정규장·entry expiry·현재 fence/approval·halt
CLEAR·fresh quote/book/spread·`last <= entry_limit`가 모두 계속 유효할 때만 가능하다. 하나라도
무효면 read-only 대조와 경보만 계속하고 `RECOVERY_REQUIRED`로 남긴다. 이미 broker에서 ID가
발견된 경우에만 그 기존 주문의 미체결 잔량을 추적·취소한다. 8분이 지나거나 request hash를
재현할 수 없으면 자동 재호출하지 않고
`RECOVERY_REQUIRED`로 남긴다. 정정·취소에는 멱등키가 없으므로 불명확한 결과를 반복 호출하지
않고 원주문과 반환된 작업 order ID chain을 REST로 대조한다. 조건주문 create는 같은 10분 창이
공식 명시되지 않았으므로 UNKNOWN에서 자동 재호출하지 않는다.

### 11.6 장전 계획과 승인

계획 생성 순서는 다음으로 고정한다.

1. 미국 정규장 거래일·단축장 여부와 장 시각을 calendar로 계산한다.
2. 구현된 automatic selector가 잠근 symbol을 읽고 allowlist가 정확히 그 한 종목인지 검사한다.
3. 일반 주문, 조건주문, 보유수량을 조회한다. 해당 symbol에 하나라도 있으면 계획하지 않는다.
4. USD `cashBuyingPower`의 통화, timestamp, finite 양수 여부를 검증한다.
5. 보수적 진입 기준가 `max(last, bestAsk)`와 수수료·slippage reserve를 계산한다.
6. `risk_qty`, `cash_qty`, `notional_qty`, `max_quantity`의 최솟값을 정수 내림한다.
7. entry, target, stop trigger, stop limit을 미국 tick에 맞춰 불리한 방향으로 보수 반올림한다.
8. `stop < entry <= entry_limit < target`, 최소 R:R, 최대 spread/괴리를 재검증한다.
9. 민감정보를 제외한 canonical payload hash와 계좌 hash로 immutable plan을 1회 INSERT한다.
10. 전용 Discord 앱이 redacted plan과 hash 뒷자리를 버튼 메시지로 보낸다. allowlist 사용자가
    버튼을 누르고 hash를 재확인하면 한 번만 승인한다. 승인 후 경제값 수정은 금지하고 변경이
    필요하면 기존 계획을 취소한 뒤 새 계획을 만들고 다시 승인한다.

수량식은 기존 구현을 유지한다.

```text
risk_budget = cash * risk_fraction
risk_per_share = entry_limit - stop_limit + per_share_cost_reserve
risk_qty = floor((risk_budget - fixed_cost_reserve) / risk_per_share)
allocated_cash = cash * cash_allocation_fraction
cash_qty = floor((allocated_cash - fixed_cost_reserve) / cash_required_per_share)
quantity = min(risk_qty, cash_qty, floor(max_notional / entry_limit), max_quantity)
```

수량이 1 미만, 비용 반영 후 손실 한도 초과, 현금 snapshot stale, R:R 미달이면 `SKIPPED`다.
현금이 많아도 첫 pilot의 `max_quantity: 1`을 자동으로 늘리지 않는다.

### 11.7 진입 제출

`READY_TO_ENTER` 이후에도 다음 조건을 매 tick마다 모두 확인한다.

- 정규장이고 `entry_start <= now < entry_expire`
- quote/orderbook/personal-order stream이 모두 정상이며 REST resync 이후 변경 없음
- last, bid, ask의 timestamp·currency·spread·last-mid 괴리가 허용 범위 이내
- 계좌, loss fuse, emergency stop, writer lease, plan hash가 유효
- 해당 symbol의 수동 보유·일반 주문·조건주문이 없음

진입 신호는 직전 유효 last가 trigger 아래이고 현재 유효 last가 trigger 이상인 실제
`below -> above` crossing만 인정한다. 시작 시 이미 trigger 위이거나 현재가가 entry limit을
넘으면 `state=SKIPPED`, `reason_code=NO_CHASE`다.

네트워크 전 DB에 `ENTRY_SUBMITTING`, 결정적 `clientOrderId`, canonical request hash와 recovery
deadline을 원자 예약한다. 일반 `LIMIT + DAY` BUY를 제출한다. ACK가 명확하면
`ENTRY_WORKING`, 결과가 불명하면 `ENTRY_UNKNOWN`으로 저장하고 일반 주문·누적 체결·보유수량을
조회해 다음 중 하나로 대조한다.

- 저장된 broker order ID 하나: 해당 주문만 채택
- order ID가 없고 로컬 recovery deadline 전: 정규장·entry window·승인/fence·halt·fresh
  quote/book/spread·entry limit가 모두 여전히 유효할 때만 저장된 동일 body/client ID로 identity
  recovery를 한 번 수행한다. 하나라도 무효면 POST하지 않는다.
- 주문 없음, 체결 없음, 보유 증가 없음이 확인됐지만 deadline 경과: 실패로 단정하거나 재제출하지
  않고 `RECOVERY_REQUIRED`
- 체결 또는 보유 증가: 봇 원장과 일치할 때만 소유수량으로 기록
- 둘 이상 또는 수동 개입과 구분 불가: `RECOVERY_REQUIRED`, 당일 진입 중단

`entry_expire`까지 미체결이면 취소를 한 번 요청하고 terminal `CANCELLED/REJECTED/FILLED`를
REST로 확인한다. 취소 응답이 불명하면 새 주문을 내지 않고 recovery를 계속한다.

누적 `filledQuantity > 0`이고 잔량도 남은 첫 순간에 보호 SLO를 시작하고
`ENTRY_CANCELING`으로 전이해 잔량 취소를 한 번 요청한다. 미국 주문은 수량 정정을 지원하지
않으므로 잔량 축소 정정을 사용하지 않는다. authoritative 누적 fill을 받을 때마다 현재 exposure와
`owned_qty`를 즉시 단조 갱신하고, 원주문이 terminal일 때 그 최종값을 entry 총량으로 고정한다.
잔량 상태가 불명확한 동안 별도
SELL을 내면 이후 BUY fill과 경합할 수 있으므로 자동으로 추측하지 않는다. 이 공백을 줄이기 위해
첫 live pilot은 1주로 고정하며, 다주 수량은 partial-fill kill-point가 통과한 뒤에만 연다.

### 11.8 체결 직후 OCO 보호

BUY 체결을 개인 주문 이벤트 하나로 확정하지 않는다. 일반 주문, 체결, 보유수량을 대조해
전략 소유수량을 확정한다. 1주 pilot에서 예상하지 않은 fractional/초과 체결 또는 수동 보유
혼합이 보이면 `RECOVERY_REQUIRED`다.

확정 즉시 `OPEN_UNPROTECTED` 시각을 영속화하고 실제 체결수량으로 `SELL/SELL` OCO를
한 번 생성한다. OCO ID와 `WATCHING` 상태를 REST에서 확인했을 때만 `PROTECTED`다.
다음 조건이면 새로운 body/client ID로 OCO를 반복 제출하지 않는다.

- timeout, `request-in-progress` 409, 429/5xx, connection reset, 응답 ID 누락
- 현재가가 이미 target 이상 또는 stop 이하
- 동일 symbol/수량/가격 후보가 0개 또는 2개 이상

결과 불명 시 조건주문 목록을 조회하되 목록·상세에는 client ID와 leg side가 없으므로 symbol,
type, top-level quantity/orderType, 두 가격, 생성 시간 매칭만으로 ownership을 확정하지 않는다.
조건주문 멱등 시간창이 공식 확인되기 전에는 저장된 동일 body/client ID도 자동 재POST하지 않는다.
로컬에 이미 영속된 exact broker ID가 없다면 candidate 0/1/2 모두 진단 정보일 뿐이며 OCO 또는 별도
SELL을 만들지 않고 `RECOVERY_REQUIRED`로 간다. ID가 모호한 상태에서 목록 부재나 sellable만으로
비상청산을 시작하지 않는다. `OPEN_UNPROTECTED`가 설정된 보호 SLO를 넘거나 가격이 보호 band 밖이면
당일 신규 진입을 영구 중단하고 최고 우선순위 수동 복구 경보를 유지한다.

비상청산 전에는 늦게 생성된 OCO와 이중 매도가 생기지 않도록 OCO create가 명확한 비접수 reject였거나
known ID의 exact DELETE 204가 영속된 뒤 post-ACK stable snapshot에서 competition-cleared임을 증명하고,
경쟁 SELL 부재, 보유·매도 가능 수량을 REST로 확인한다. 그 뒤 위 식으로 계산한
`remaining_owned_qty_today` 전량을 청산 대상으로 제출하고, 부분 체결이면 같은 주문의 남은
수량을 추적해 0이 될 때까지 관리한다. 첫 pilot은 정수 주문만 만들지만 같은 plan의 실제 체결로
증명된 잔여수량은 전부 청산 대상이다. 거래정지·호가 소실로 체결이 불가능한 경우에는 완료를
가정하지 않고 긴급 알림과 watchdog을 유지한다.

사용자가 선택한 자동 비상청산은 정규장 안에서 broker 상태가 명확할 때
`remaining_owned_qty_today` 전량의 `SELL + MARKET`을 한 번 제출하는 정책으로 계획 hash에
포함한다. 이는 청산 가능성을 우선하며 체결 가격이나 계획 손실 상한을 보장하지 않는다. 주문
결과가 불명하면 정규장·holding/owned/request/sellable exact와 OCO/경쟁 SELL 부재가 모두 계속
유효할 때만 동일 create identity recovery를 한 번 수행한다. 하나라도 불명확하면 과매도 위험 때문에
두 번째 SELL을 만들지 않는다. 거래정지, 정규장 종료,
sellable quantity 불명에서는 자동청산을 완료할 수 없으므로 거짓 `CLOSED` 대신 지속 긴급 알림과
`RECOVERY_REQUIRED`를 유지한다.

stop leg가 발동하면 `triggeredOrderId` 일반 SELL의 누적 fill을 추적한다. stop-limit이 SLO 안에
체결되지 않으면 그 주문의 미체결 잔량 취소를 한 번 요청하고 terminal을 확인한 뒤, 남은 전략
소유수량 전부에 같은 비상 MARKET 정책을 적용한다. 취소가 불명확하면 이중 SELL을 제출하지
않는다. LULD/halt 중에는 체결을 보장할 방법이 없다는 사실을 운영 알림에 그대로 표시한다.

### 11.9 시간청산과 이중 매도 방지

`force_exit_at`에는 OCO가 살아 있는 채로 별도 SELL을 내지 않는다.

1. 조건주문 상태를 조회한다.
2. 이미 leg가 발동했으면 그 `triggeredOrderId`를 끝까지 추적한다.
3. 아직 `WATCHING`이면 OCO 취소를 한 번 요청한다.
4. exact DELETE 204를 영속화하고 그 뒤 stable snapshot으로 active group·triggered/경쟁 SELL 부재와
   sellable quantity를 확인한다.
5. 그 뒤에만 계획에서 승인한 정규장 `MARKET SELL`을 남은 전략 소유수량 전부에 제출한다.
6. 체결·보유 0을 대조한 후 `CLOSED`로 전이한다.

취소 불명, sellable quantity 불명, 장중 거래정지에서는 이중 매도 위험 때문에 새 SELL을
추가하지 않고 긴급 알림과 수동 복구로 남긴다. 봇이 매도하는 최대 수량은 로컬 체결 원장과
broker history 양쪽이 증명한 `owned_qty`다.

### 11.10 재시작 대조표

| broker 관측 | 로컬 판정 | 자동 동작 |
| --- | --- | --- |
| 포지션 0, 열린 주문/OCO 0, submit 0 | 미진입 | restart 전 receipt를 폐기하고 새 승인 generation 대기 |
| BUY open, 포지션 0 | `ENTRY_WORKING` | 기존 주문만 추적, 새 BUY 금지 |
| 전략 소유 포지션 > 0, OCO 0 | `OPEN_UNPROTECTED` | 즉시 보호 시도 또는 비상청산 후 halt |
| 전략 소유 포지션과 정확히 매칭되는 OCO 1 | `PROTECTED` | 기존 OCO 추적 |
| 전략 소유 포지션 0, 전략 OCO 1 | stale OCO | 취소 확인 후 halt |
| 수동/타 전략 포지션, 복수 후보, 원장 불일치 | `RECOVERY_REQUIRED` | 채택·매도·신규 진입 모두 금지 |
| UNKNOWN create와 broker 결과 불명 | UNKNOWN 유지 | deadline 안 exact identity recovery, 이후 자동 재호출 금지 |

writer lease가 만료돼도 새 process는 즉시 주문하지 않는다. fence token을 증가시키고 위
대조표를 완료한 뒤에만 ownership을 얻는다.

재시작 전 이미 entry fill이 있으면 새 Discord 승인을 기다리지 않고 보호·청산만 자동 재개한다.
반대로 아직 submit이 0이면 이전 receipt를 재사용하지 않는다. Mac boot ID와 writer generation을
바꾸고 같은 immutable plan에서 새 nonce의 승인 envelope를 내보내 사용자가 다시 승인해야만
`READY_TO_ENTER`가 된다. 어떠한 재시작도 당일 두 번째 entry count를 0으로 되돌리지 않는다.

### 11.11 일일 퓨즈와 관측성

신규 entry 건수는 mutable 현재 주문 row가 아니라 append-only `ENTRY submit_started` event로
계산한다. 취소했다고 건수를 돌려주지 않는다. 하루 1회 pilot에서는 계획의 risk budget이 일일
신규 위험 한도를 겸한다. 실현손익, 보수적 미실현손익, 이미 발생한 수수료와 예상 청산비용은
관측·경보값으로 계속 계산하지만, stop-limit gap 때문에 실제 손실의 상한이라고 부르지 않는다.

신규 진입 차단 퓨즈:

- 승인 risk budget 소진 또는 하루 entry submit 횟수 도달
- 연속 제출/대조 실패, UNKNOWN 미해결
- stale/비정상 시세, stream 재연결 한도 초과
- 보호 SLO 초과, 원장 불일치, DB quick-check/쓰기 오류
- emergency stop, 다른 writer, 계획 hash 불일치

이 퓨즈는 신규 진입만 막는다. 기존 BUY 취소, OCO 생성·유지, 보호/비상청산 같은 위험감소
동작은 계속 허용하되 ownership과 sellable quantity 검증은 생략하지 않는다.

현재 `PreTradeSafety`는 emergency stop과 일일 주문 건수를 모든 BUY/SELL에 공통 적용하므로 그대로
재사용하면 청산까지 막을 수 있다. live 전에는 action role 기준으로 나눠 `ENTRY`만
`entry_enabled`, approval, daily count, loss fuse의 영향을 받게 한다.
`ENTRY_CANCEL`, `PROTECTION`, `PROTECTIVE_EXIT`, `FORCE_EXIT`, `EMERGENCY_EXIT`는 live entry
kill switch가 켜져도 계속 허용하되, writer fence·strategy ownership·sellable quantity·세션과
UNKNOWN 중복 방지는 항상 검사한다. 운영 kill switch도 process kill이 아니라 SQLite의 durable
`entry_disabled_at/reason`을 먼저 기록해 새 BUY만 멈추는 방식이다.

필수 metric/event는 `stream_connected`, `last_event_age`, `reconnect_count`, `broker_resync_age`,
`run_state`, `entry_unknown_age`, `owned_qty`, `protected_qty`, `unprotected_ms`,
`daily_submit_count`, `daily_loss`, `writer_fence`다. 로그와 Discord에는 accountSeq, token,
webhook, 주문 원문, 예외의 secret-bearing URL을 남기지 않는다.

live runtime 자체가 죽으면 스스로 알릴 수 없으므로 별도 `toss-watchdog` UID의 trading-unprivileged
watchdog LaunchAgent가 group-readable heartbeat 파일의 age와 `launchctl` 상태만 읽는다. 이 process에는
Toss/approval/news credential, 거래 DB 접근, 주문 adapter를 주지 않고 watchdog 전용 alert-only
Discord credential만 주어 stale·restart-loop를 같은 단일 Discord channel로 알린다.
정상 heartbeat는 channel을 도배하지 않고 시작·상태변경·stale·복구·일마감만 전송한다.

### 11.12 Discord 승인과 뉴스·LLM 격리

승인 방식은 Discord 직접 승인(B 방식)으로 고정한다. 기존 incoming webhook은 상호작용을 받을
수 없으므로 별도 Discord application/bot이 Mac에서 outbound Gateway 연결을 유지한다. 공개 HTTP
endpoint나 port forwarding은 열지 않는다.

Discord bot role의 서버 기본 권한은 `0`으로 설치한다. 운영자가 지정한 단일 target channel의
permission overwrite에서만 다음 두 개를 허용한다.

```text
VIEW_CHANNEL  = 0x0400
SEND_MESSAGES = 0x0800
permissions   = 3072
```

다른 모든 category/channel에는 bot role의 `VIEW_CHANNEL`과 `SEND_MESSAGES`를 명시적으로
거부하고, sender와 interaction handler도 local allowlist의 exact channel ID가 아니면
fail-closed한다. 즉 `3072`는 서버 전역 OAuth 권한이 아니라 target channel의 allow 값이다.
OAuth 값과 target overwrite만 보고 완료로 판정하지 않는다. category·role·member 상속을 모두
적용한 guild 전체 유효 권한 감사에서 target View/Send만 true, 그 외 channel/category의
View/Send 수가 모두 0, Administrator/Manage Channels/Manage Roles가 모두 false여야 한다.
2026-08-29 외부 감사는 이 조건을 통과했으며 private ID와 token은 결과에 기록하지 않았다.
2026-08-30에는 주문 capability가 없는 exact-SHA Mac release로 synthetic button/modal 승인,
strict mode-0600 receipt, 재시작 뒤 receipt·Discord 요청 중복 억제까지 통과했다. 검증 후 임시
LaunchAgent와 synthetic runtime을 제거했으며 receipt consumer와 live 주문 연결은 추가하지 않았다.

OAuth2 Guild Install은 `bot` scope와 `permissions=0`만 사용하고 slash command는 등록하지 않는다. Gateway
`INTERACTION_CREATE`는 privileged intent가 필요하지 않으므로 intents는 0으로 두고 Presence,
Server Members, Message Content intent를 모두 끈다. `ADMINISTRATOR`, `MANAGE_MESSAGES`,
`MANAGE_CHANNELS`, `MANAGE_ROLES`, `MANAGE_WEBHOOKS`, `READ_MESSAGE_HISTORY`, `EMBED_LINKS`,
`MENTION_EVERYONE`, `USE_APPLICATION_COMMANDS`는 요청하지 않는다. 계획은 plain text와 button으로
보내므로 embed 권한도 필요 없다.

승인 interaction은 exact `discord_user_id`, `guild_id`, `channel_id`, `plan_id`, plan hash,
일회용 nonce, 만료시각과 화면에 표시된 계정 별칭·종목·현금·수량·모든 가격·위험값을 묶어
검증한다. 첫 버튼은 hash 뒷자리 확인 modal만 열고 modal 제출이 성공해야 mode `0600`의
one-shot receipt를 완성된 temp 파일에서 no-clobber publish한다. 같은 interaction/nonce 재사용,
다른 사용자·채널·서버, 만료·변경된 plan은 거부한다. 현재 shadow-only 버전은 이
receipt를 소비하거나 SQLite 상태를 바꾸지 않는다. 후속 거래 runtime만 모든 broker/risk gate를
다시 통과한 뒤 SQLite CAS로 `PLANNED -> APPROVED`를 수행할 수 있다.

승인 worker에는 Toss client ID/secret, 계좌번호, trading DB 쓰기, shell/subprocess, 주문 API를
주지 않는다. macOS Keychain의 전용 Discord bot token과 redacted approval envelope 읽기,
one-shot approval inbox 쓰기만 허용한다. 현재 release에는 inbox 소비자가 없다. 향후 거래
runtime만 immutable plan, 현재 계좌·시세·주문·위험 gate를 다시 검증하고 자기 DB의 상태를
CAS로 바꿀 수 있다. 현재 worker가 끊기면 영수증이 생기지 않을 뿐이며 주문이나 OCO는 시작되지
않는다. 기존 OCO 추적·비상청산 지속은 실행 엔진 구현 뒤에 별도로 증명할 미래 요구사항이다.

package/import 분리는 오작동과 우발적 권한 혼합을 줄이지만 OS 보안 경계는 아니다. 같은 macOS
UID의 다른 process는 mode `0600` 파일과 사용자 Keychain에 접근하거나 영수증을 위조할 수 있다.
따라서 현재 영수증은 사람의 shadow 승인 기록일 뿐 live capability가 아니다. live inbox 소비자를
추가하기 전에는 별도 OS identity 또는 코드서명/하드웨어 기반 키처럼 같은 UID가 복제할 수 없는
인증 출처를 설계하고, consumer가 immutable DB plan·현재 broker 상태·당일 위험 한도·만료를 다시
검증해야 한다. 같은 UID Keychain의 HMAC 하나만 추가하는 것은 이 경계를 해결하지 못한다.

선택한 live 경계는 별도 macOS standard user 두 개다. `toss-trader`만 Toss Keychain, trading DB,
live runtime을 소유하고 `toss-approver`만 Discord bot Keychain과 approval worker를 소유한다.
root가 미리 만든 non-writable anchor 아래 두 mailbox를 사용한다.

```text
approval-v2/      owner=root,         group=wheel,                  dir 0755
approval-outbox/  owner=toss-trader,  group=toss-approver-readers, dir 0750, file 0640
approval-inbox/   owner=toss-approver, group=toss-trader-readers,   dir 0750, file 0640
```

각 process는 상대 mailbox를 읽을 수만 있고 상대 Keychain·DB·쓰기 디렉터리에는 접근하지 못한다.
현재 same-UID `0600` primitive는 group bit와 다른 owner를 거부하므로 재사용하지 않는다. v2는
expected UID/GID/mode와 모든 ancestor의 non-writable 상태, local filesystem을 확인하는 별도 작은
fd-relative primitive다. directory fd에서 `openat`+`O_NOFOLLOW|O_CREAT|O_EXCL`, `fstat`,
`fchown`/`fchmod`, file+directory `fsync`를 사용하며 path 재해석과 symlink mount를 허용하지 않는다.
v1은 `statfs`가 local APFS이고 ownership-ignore flag가 없을 때만 통과한다. 다른 filesystem은 별도
threat review 없이는 allowlist에 추가하지 않는다.
consumer는 receipt file descriptor의 regular-file, no-symlink, owner UID, group, mode, size, link count와
schema를 확인하고, plan ID/hash, 표시된 모든 경제값, nonce, approval generation, Discord
user/guild/channel, expiry를 constant-time 비교한다. 그 뒤 현재 broker·시장·risk gate를 다시 읽고
SQLite CAS로 receipt를 한 번만 소비한다. root/admin과 해당 OS identity 자체의 compromise는 이 로컬
경계가 막는 위협으로 주장하지 않는다. trader/approver 두 사용자 session과 Keychain 복구를 운영할 수 없다면 같은
UID receipt를 live 권한으로 승격하지 않는다.

현재 worker가 허용하는 `mode=shadow` envelope/receipt는 그대로 live에 재사용하지 않는다. 새 schema는
`purpose=INTRADAY_LIVE_ENTRY`, account alias, boot ID hash, writer generation, approval generation,
emergency MARKET 정책과 모든 SLO를 plan hash에 묶는다. 승인 메시지가 중복 게시될 수 있어도 같은
plan/generation receipt의 SQLite consume와 `ENTRY submit_started`는 정확히 한 번이어야 한다.

현재 거래 planner는 approval worker용 redacted envelope와 뉴스 worker용 단일-symbol context를
각각 atomic export한다. `turtle_news`는 15분 one-shot, 별도 SQLite, 별도 Discord webhook,
localhost LLM으로 실행하되 webhook은 승인·거래와 동일한 단일 허용 channel을 가리켜야 한다.
뉴스 process가 Toss secret 환경변수나 trading DB 경로를 받으면 시작을 거부한다.

뉴스 수집 실패, LLM timeout, 잘못된 JSON, 악성 기사, Discord 중복·실패 전후에 plan hash,
state, quantity, entry/target/stop, 주문 intent가 byte-for-byte 동일해야 한다. 뉴스 점수나 LLM
의견으로 매수 취소·허용·수량 변경을 하지 않는다.

### 11.13 테스트 계획

테스트는 기본적으로 네트워크를 차단하고 fake clock, fake WebSocket, fake REST transport,
임시 SQLite를 사용한다. `config/local.yaml`, 실제 Keychain, 실제 Discord/Toss endpoint를
읽는 테스트는 기본 suite에 넣지 않는다.

1. **계획 계산**: 현금 경계, 비용, tick 반올림, 최소 R:R, NaN/Infinity, 0주, plan hash.
2. **stream**: ACK 순서·누락·거절, malformed/oversize, queue overflow, stale timestamp,
   silence timeout, reconnect backoff, 다른 symbol/account 주입, REST resync 실패.
3. **진입**: 아래→위 crossing, 시작부터 위, limit 초과, stale 호가, spread, 휴장·단축장·DST,
   ACK loss, 409/429/5xx, 8분/10분 멱등 경계, 동일 key+다른 body 거부, CLOSED 다중 page,
   취소 응답 유실, 두 runtime 동시 제출.
4. **보호**: BUY 직후 crash, OCO 전 network loss, OCO 응답 유실, 후보 0/1/2개,
   목록·WS에 client ID 부재, 가격이 이미 band 밖, stop 발동 뒤 미체결·취소 불명,
   target/stop 동시 도달 시 보수적 stop 우선.
5. **ownership**: 수동 보유, 다른 전략 주문, 초과/fractional 체결, sellable 불일치,
   stale OCO, 이중 SELL 시도 차단.
6. **재시작**: 위 상태 각각을 저장한 직후 process kill, WAL 포함 재개, lease 탈취,
   corrupt DB clone, migration rollback, UNKNOWN 장기화.
7. **위험**: 일손실 경계, 제출 건수 불변, 퓨즈 뒤 신규 BUY 0건, 퓨즈 중 보호 EXIT 허용.
8. **뉴스 격리**: LLM 성공/실패/악성 출력에서 거래 DB·signal·intent 불변, Toss secret 주입 거부.
9. **capability**: approver/trader UID·Keychain·mailbox 교차 접근 거부, receipt owner/mode/link 교란,
   dashboard/gateway/health POST가 live release에 없고 별도 watchdog에 주문 capability가 없는지.

필수 자동 합격 조건:

- 중복 entry 제출 0건
- 전략 미소유 수량 SELL 0건
- stale/세션 밖 entry 0건
- UNKNOWN 뒤 무근거 재제출 0건
- 모든 재시작 fixture에서 broker 대조 전 전략 평가 0회
- `protected_qty != owned_qty` 상태에서 신규 진입 0건
- 설정된 SLO를 넘긴 무알림 `OPEN_UNPROTECTED` 0건
- shadow mode에서 broker write method 호출 0건
- entry kill switch 이후 신규 BUY 0건이면서 기존 position의 OCO/force/emergency exit는 계속 동작
- create UNKNOWN의 local recovery deadline 이후 동일 client ID POST 0건
- live release process/venv/import graph에 dashboard·gateway action entrypoint 0건

### 11.14 단계별 비실거래 검증과 이후 경계

| 단계 | 이번 범위 | 합격 조건 |
| --- | --- | --- |
| NL0 | clean integration, migration 복제본, 모든 live flag hard-off | 운영 DB·secret을 읽지 않고 전체 정적 gate 통과 |
| NL1 | 순수 계획·상태 전이 단위시험 | 모든 허용/금지 전이와 수량 불변조건 통과 |
| NL2 | fake REST/WS 계약시험 | 실제 host 접속 0, canonical request·strict parse 통과 |
| NL3 | crash/timeout/429/WS gap 장애주입 | 모든 kill point에서 재시작 후 중복 BUY/SELL 0 |
| NL4 | 과거 데이터 replay | 최소 100개 신호에서 세션·추격·위험 불변조건 위반 0 |
| NL5 | Mac synthetic approval·exact-SHA 패키지 | 주문 자격증명 없는 fixture로 UID/mailbox/restart 검증 |
| NL6 | 실제 시장 read-only shadow | 선택 종목 시세·뉴스만 관측, broker mutation 0 |

다음 항목은 **이번 범위가 아니며 실행하지 않는다**.

- 실계좌에 1주를 넣는 live pilot
- 실제 OCO 생성·취소·발동 또는 MARKET emergency/force exit
- 실거래를 반복해 SLO나 손익을 검증하는 soak
- 현금 사용 비율 확대 또는 자동 수량 확대

NL0–NL6 합격은 `non-live implementation complete`일 뿐 `live ready`가 아니다. 이후 live 검증을
시작하려면 별도 작업에서 미해결 P0, 실제 계좌 배타성, halt/LULD source, trader/approver/watchdog
세 macOS identity,
외부 deadman을 다시 확인하고 사용자의 명시적 승인을 받아야 한다.

Mac 운영 gate는 NL5 전에 통과한다. 단기 확인 결과는 AC 전원 4분 heartbeat 24/24와 독립 재접속
8/8 성공이지만, persistent tty 없는 10분 idle, 1시간 SSH soak, FileVault 이후 필요한 세 사용자 로그인
복구 시간 측정은 남아 있다. 이 시험도 주문 자격증명과 live plist 없이 수행한다. 예기치 않은
재부팅 후 FileVault 해제와 사용자 로그인이 없으면 user LaunchAgent의 무인 복구를 보장할 수
없다는 사실은 그대로다.

비실거래 범위에서는 approver/trader 두 UID의 mailbox와 watchdog을 dummy secret·fixture heartbeat로만
검증한다. 실제 Toss 주문 자격증명을 simulation job에 주지 않고 live plist도 설치하지 않는다.
dashboard/gateway/health action process가 release에 없는지는 정적으로 검사한다. 실제 포지션이나
OCO가 전제되는 rollback drill은 이후 별도 live 검증 범위다.

### 11.15 구현 커밋 순서

1. clean integration 기준·LF/executable mode·dependency lock을 별도 patch로 확정한다.
2. `--shadow-service` exact-mode preflight와 OAuth 외 non-GET transport tripwire를 먼저 구현해 이후
   작업 동안에도 실제 mutation 경로가 닫혀 있음을 증명한다.
3. v4 migration, immutable reservation, writer/sync fence, monotonic execution projection과 two-writer
   경합 tests를 추가한다.
4. 일반 주문 strict execution parser, CLOSED full pagination, 공식 status spelling과
   create/cancel UNKNOWN 분류를 adapter focused tests로 고친다.
5. `intraday_live.py`의 `recover()`와 stable broker snapshot만 구현하고 모든 저장 상태 restart
   fixture에서 signal/mutation 0을 증명한다.
6. approval v2 canonical wrapper, cross-UID synthetic receipt consumer와 CAS replay tests를 추가하되
   production entrypoint에는 consumer를 배선하지 않는다.
7. entry crossing·reservation·create/identity recovery·partial-fill cancel을 구현하고 각 network
   경계 crash tests를 붙인다.
8. OCO create/verify, 필수 `expireDate`, `OPEN_UNPROTECTED`, protection SLO, triggered stop 추적을
   구현한다. accepted-before-timeout과 candidate ambiguity에서는 자동 conditional 재POST 0을
   검사한다.
9. exact conditional DELETE 204 영속화→post-ACK stable snapshot→잔여 전량 emergency/force exit와
   이중 SELL 방지, role-aware entry fuse를 구현한다. 응답 유실 때 별도 SELL 0을 검사한다.
10. fake 3-topic WebSocket, REST A→ACK→REST B, queue overflow/reconnect와 metric/outbox/watchdog을
    연결한다. 실제 shadow job은 public 두 topic만 유지한다.
11. news data-diode·매-send channel 확인, Mac 다섯-job exact-SHA simulation release와 read-only shadow
    soak를 검증한다. 실제 주문 plist·personal account stream·1주 pilot은 제외한다.

각 커밋은 해당 focused test를 포함하고 기능 변경과 대규모 EOL 정리를 섞지 않는다. 마지막
release는 dirty checkout을 직접 실행하지 않고 검증된 exact commit의 전용 venv와 불변 release
디렉터리에서 시작한다. rollback은 비실거래 복제 DB에서 이전 exact SHA와 pre-migration SQLite
backup으로 연습하며 DB reverse migration은 하지 않는다. 운영 DB·실계좌 주문을 쓰는 rollback은
이번 범위 밖이다.

## 12. 남은 결정과 권장 초기값

Toss API로 다시 읽을 수 있는 계좌, 현금, 수수료, 보유·주문, 거래일·장 시각, 현재가·호가,
종목 metadata는 사용자 입력으로 받지 않는다. 아래는 API가 정할 수 없는 정책만 남긴 목록이다.

| 정책 | 설계값 | 상태 |
| --- | --- | --- |
| 종목 | automatic selector가 한 종목 잠금 | 사용자 확정 |
| 현금 사용 | `cashBuyingPower * cash_allocation_fraction` 전체를 cash cap으로 사용하되 risk 수량을 넘겨 강제 소진하지 않음 | fraction 숫자만 필요 |
| 계획/arm | 시스템 가격·수량 제안 후 Discord hash 승인 한 번 | 사용자 확정 |
| 하루 신규 진입 | 1회, 재진입 없음 | 안전 기본값 고정 |
| 첫 live 수량 | 정수 1주 | 승격 gate 고정 |
| 초기 계획 손실 | `allocated_cash * 0.25%`; 현재 config에서는 `risk_fraction = cash_allocation_fraction * 0.0025` | 권장 pilot 값, 승인 필요 |
| 진입 창 | 정규장 +5분부터 +60분 전까지 | 권장 pilot 값, shadow 측정 후 승인 |
| 강제청산 | 정규장 종료 15분 전 | 권장 pilot 값, shadow 측정 후 승인 |
| OCO 실패 | 당일 plan의 남은 전략 소유수량 전부 정규장 MARKET SELL | 사용자 확정; 가격 보장 없음 명시 |
| stop-limit 미체결 | triggered order 취소 확인 뒤 남은 전략 소유수량 전부 같은 MARKET 정책 | 자동 전량청산 결정의 필수 귀결 |
| 계좌 배타성 | live 전용 계좌, 당일 Toss 앱·다른 전략·수동 주문 금지 | live 필수 동의 |
| 승인 보안 | `toss-trader`와 `toss-approver` 별도 macOS UID | live 필수 동의·설정 필요 |
| 재부팅 | FileVault 해제·사용자 로그인·broker 대조·Discord 재승인, 자동 로그인 금지 | 사용자 운영 조건 반영 |
| 뉴스 | 잠긴 종목의 새 기사마다 LLM 요약, 동일 단일 Discord channel만 | 사용자 확정 |
| 장애 감시 | 제3 `toss-watchdog` UID local watcher + 별도 상시 노드 deadman, 둘 다 주문 권한 없음 | watchdog UID/전용 alert token과 두 번째 상시 노드 필요 |
| halt/LULD | Toss 단독으로 확인 불가하므로 external authoritative source 없이는 live 금지 | 미해결 P0 |

quote/orderbook TTL, stream silence, OCO 보호와 exit fill SLO는 감으로 정하지 않는다. 실제 Mac에서
5–10개 미국장 shadow session의 p99 지연을 측정하고 `max(안전 최솟값, 3 * p99)`로 제안하되,
config 상한을 넘으면 값을 늘려 진입하는 대신 그 날을 skip한다. 이 값과 external halt source,
현금 사용 fraction, 전용 계좌·trader/approver/watchdog 세 macOS identity 동의가 확정되기 전에는
live config를 만들지 않는다.

가장 안전한 현재 진행 경로는 `state migration/consumer/runtime 구현 -> fault test -> Mac shadow
soak -> 1주 live pilot -> 명시적 수량 확대`다. `cash_allocation_fraction`이 크더라도 1주 pilot과
partial-fill fault gate를 건너뛰어 한 번에 전체 현금 규모로 승격하지 않는다.

## 13. 비실거래 구현 명세와 완료 정의

이 절은 11절을 실제 작업 단위로 내린 명세다. 서로 충돌하면 이 절의 `실제 주문 0` 경계가
우선한다. 구현자가 임의로 live 연결을 먼저 열거나, 테스트 편의를 위해 안전 전이를 생략하지
못하도록 저장·전이·호출·검증 순서를 고정한다.

### 13.1 산출물과 금지선

비실거래 구현 완료 때 존재해야 하는 산출물은 다음뿐이다.

| 산출물 | 책임 | 금지 |
| --- | --- | --- |
| `intraday_live.py` | fake broker에서도 동일하게 쓰는 recovery·entry·protection·exit 상태기계 | CLI에서 실제 Toss mutation으로 dispatch |
| `state_store.py` migration | run CAS, writer fence, immutable intent, append-only transition | 별도 DB·범용 event bus·두 번째 order ledger |
| 기존 Toss adapter 보강 | 공식 request/response strict parse, UNKNOWN 분류 | 실제 endpoint를 쓰는 integration test |
| 기존 approval worker v2 보강 | cross-UID fd-relative mailbox, synthetic receipt | 같은 UID receipt를 live capability로 승격 |
| standalone shadow watchdog | redacted heartbeat와 launchd 상태만 검사 | `turtle_bot` eager import, Toss/거래 DB/order code |
| strict shadow entrypoint | planner·selected-symbol stream·news·승인 기록만 실행 | generic live config 수용, receipt 소비 |
| tests/fixtures | fake REST/WS/clock, replay, kill-point, synthetic approval | 실제 계좌·실제 주문 자격증명 사용 |
| Mac simulation release spec | exact SHA, read-only source, dummy mailbox, watchdog | live writer/consumer plist 설치 |

현재 adapter를 그대로 live-safe라고 간주하지 않는다. `toss_live_adapter.query_order()`가 missing
`execution`/`filledQuantity`를 0으로 대체하거나 초과 fill에서 잔량을 0으로 clamp하는 동작은 제거하고,
strict finite cumulative quantity·`averageFilledPrice`·requested quantity 검증 실패로 분류해야 한다.
또한 현재 terminal code set의 `idempotency-key-conflict`는 제거해 writer identity 위반
`RECOVERY_REQUIRED`로 올린다. 이 두 focused regression test가 먼저 실패(red)하고 수정 뒤 통과하기
전에는 lifecycle runtime을 배선하지 않는다.

현재 `--shadow-service`는 `--paper-service`와 같은 `run_paper_service()`를 호출하고 그 함수는 매
iteration config를 다시 읽으므로 이름이나 startup 1회 검사만으로 shadow를 보장하지 못한다. 최소
수정은 새 service 계층을 만드는 것이 아니라 기존 함수에 `expected_mode="shadow"`를 넘기는 것이다.
startup뿐 아니라 **새로 읽은 config마다**, 그 config에서 DB 경로·Keychain·client를 해석하거나
network를 열기 전에 아래를 모두 다시 검사한다. reload가 실패하거나 어느 값이라도 바뀌면 그
iteration을 skip하지 말고 process를 종료한다.

```text
strategy.kind == "intraday"
runtime.mode == "shadow"
toss.live_enabled is false
strategy.intraday.live_execution_enabled is false
live.emergency_stop is true
live.allowed_symbols == []
toss.base_url == "https://openapi.tossinvest.com"
```

여기에 기존 transport를 감싸는 작은 read-only tripwire 하나를 `toss_client.py`에 둔다. 정확한
`POST /oauth2/token`과 아래 shadow GET allowlist만 통과시키고, 일반·조건주문
create/modify/cancel을 포함한 그 외
`POST`, `PUT`, `PATCH`, `DELETE`는 delegate 호출 전 거부한다. shadow planner와 stream만 이 wrapper를
사용한다. production에서는 URL을 한 parser로 한 번만 canonicalize하고 scheme=`https`, ASCII exact
host=`openapi.tossinvest.com`, port 없음 또는 443, no user-info/fragment를 강제한다. OAuth POST는 exact
path `/oauth2/token`, empty query만 허용한다. GET도 같은 origin과 absolute normalized API path만
허용하고 backslash, encoded slash/dot, duplicate authority 같은 parser ambiguity는 거부한다.
`urllib`의 자동 redirect를 끈 no-redirect opener를 실제 delegate로 사용하고 모든 30x를 오류로
반환해야 한다. wrapper만 검사한 뒤 기본 opener가 Authorization이나 OAuth form을 다른 origin으로
따라가게 두면 안 된다. fake lifecycle 시험은 production CLI가 아니라 테스트에서 adapter에 fake
transport를 직접 주입한다. 이렇게 해야 config reload·origin·redirect·dispatch 중 하나가 잘못돼도
실제 주문 요청이나 자격증명 유출로 이어지지 않는다.

```text
/api/v1/market-calendar/US
/api/v1/rankings
/api/v1/stocks
/api/v1/stocks/all
/api/v1/stocks/{strict-symbol}/warnings
/api/v1/candles
/api/v1/prices
/api/v1/orderbook
/api/v1/accounts
/api/v1/holdings
/api/v1/orders
/api/v1/conditional-orders
/api/v1/buying-power
/api/v1/commissions
```

각 path의 query key/value도 기존 strict request builder의 exact schema와 일치해야 하며 duplicate/unknown
query key를 거부한다. OAuth form은 exact key set `grant_type`, `client_id`, `client_secret`와
`grant_type=client_credentials`만 허용한다. allowlist 확대는 새 코드·계약시험 없이는 config로 할 수
없다.

거래 package의 production module 추가는 `intraday_live.py` 하나로 제한한다. 단, eager
`turtle_bot.__init__`를 import하면 거래 권한 격리가 깨지는 watchdog은 standard-library-only standalone
파일 하나와 argument-free wrapper/plist를 별도 security boundary로 둔다. approval v2는 기존 worker에
구현한다. 새 broker interface, repository layer, message broker, generic workflow engine, dashboard,
slash command, 별도 상태 DB는 만들지 않는다. 기존 concrete adapter와 `TossTransport`,
`SQLiteStateStore`, calendar, notifier를 생성자 인자로 그대로 받는다.

### 13.2 최소 runtime 표면

`intraday_live.py`의 public 표면은 다음 세 개면 충분하다.

```python
class IntradayLiveRuntime:
    def recover(self) -> None: ...
    def on_stream_frame(self, frame: object) -> None: ...
    def tick(self) -> None: ...

def canonical_order_request(body: Mapping[str, object]) -> tuple[bytes, str]: ...
def remaining_owned_quantity(snapshot: BrokerSnapshot, plan_id: str) -> Decimal: ...
```

별도 base class나 callback registry는 만들지 않는다. `IntradayLiveRuntime`은 기존 store, Toss read
client, 일반 주문 adapter, 조건주문 adapter, stream validator, notifier, aware clock을 직접 받는다.
테스트는 같은 concrete adapter 아래의 fake transport와 fake clock을 넣는다. production CLI는 이번
범위에서 이 class를 생성하지 않는다.

`recover()`는 process 시작·writer 교체·WS 재연결 뒤 한 번 실행하며 broker 대조가 끝날 때까지
signal을 읽지 않는다. `on_stream_frame()`은 strict validation 뒤 cumulative observation만 갱신하고
주문을 직접 만들지 않는다. `tick()`만 저장 상태와 broker snapshot을 보고 한 번에 최대 하나의
상태 전이 또는 하나의 예약된 mutation을 수행한다. 함수 한 번 안에서 두 종류의 SELL을 연달아
만들지 않는다.

한 tick의 순서는 고정한다.

1. DB `quick_check` 상태, writer fence·lease, boot ID, hard entry-disable을 확인한다.
2. 이전 tick의 미완료 reservation/UNKNOWN을 먼저 처리한다.
3. 필요 상태에서만 strict broker snapshot을 읽고 DB ownership과 대조한다.
4. stream 신선도·calendar·halt guard를 확인한다.
5. 허용 전이를 `BEGIN IMMEDIATE` 안에서 CAS하고 intent/event/outbox를 함께 기록한다.
6. commit 성공 뒤에만 예약된 네트워크 mutation 하나를 호출한다.
7. 응답을 두 번째 transaction에 기록한다. 기록 실패 시 성공을 추정하지 않고 재시작 reconciliation로
   넘긴다.
8. 알림은 상태 transaction에 넣은 outbox를 별도로 drain한다.

`tick()`의 어떤 예외도 `ENTRY_SUBMITTING` 예약을 되돌리거나 submit count를 감소시키지 않는다.
`BaseException`까지 무리하게 삼키지 않고, DB에 기록 가능한 allowlisted error code만 남긴 뒤 process를
종료해 launchd recovery를 유도한다. token, URL query, accountSeq, request body, raw broker payload는
exception/log/outbox에 넣지 않는다.

deadline은 aware UTC로 영속화하고 한 process 안의 elapsed time은 monotonic clock으로 잰다. restart 뒤
wall clock이 `unprotected_since`, first-attempt, approval 또는 market timestamp보다 뒤로 이동했거나 OS
time-sync가 불량하면 duration을 0으로 재설정하지 않는다. 신규 entry는 막고 protection/exit SLO는
이미 만료한 것으로 보수 판정해 즉시 broker 대조·경보로 간다. DST는 calendar 변환에만 영향을 주며
지속시간 계산에는 쓰지 않는다.

### 13.3 SQLite migration과 소유권 원장

기존 schema에 한 번 적용하는 다음 migration을 사용한다. legacy row를 깨지 않도록 추가 column은
nullable로 두되, intraday reservation method가 모두 채우도록 검사한다. 기존 `idempotency_key`를 Toss
`clientOrderId`의 정본으로 사용해 같은 값을 담는 중복 column은 만들지 않는다.

```sql
BEGIN IMMEDIATE;

ALTER TABLE order_intents ADD COLUMN account_key TEXT;
ALTER TABLE order_intents ADD COLUMN plan_id TEXT;
ALTER TABLE order_intents ADD COLUMN order_role TEXT;
ALTER TABLE order_intents ADD COLUMN request_hash TEXT;
ALTER TABLE order_intents ADD COLUMN request_json TEXT;
ALTER TABLE order_intents ADD COLUMN first_attempt_at TEXT;
ALTER TABLE order_intents ADD COLUMN recovery_deadline_at TEXT;
ALTER TABLE order_intents ADD COLUMN reserved_at TEXT;
ALTER TABLE order_intents ADD COLUMN send_by TEXT;
ALTER TABLE order_intents ADD COLUMN reserved_writer_fence INTEGER;
ALTER TABLE order_intents ADD COLUMN reserved_run_version INTEGER;

ALTER TABLE execution_orders ADD COLUMN filled_quantity TEXT NOT NULL DEFAULT '0';
ALTER TABLE execution_orders ADD COLUMN remaining_quantity TEXT;
ALTER TABLE execution_orders ADD COLUMN average_fill_price TEXT;
ALTER TABLE execution_orders ADD COLUMN last_broker_observed_at TEXT;

ALTER TABLE execution_events ADD COLUMN plan_id TEXT;
ALTER TABLE execution_events ADD COLUMN run_version INTEGER;
ALTER TABLE execution_events ADD COLUMN writer_fence INTEGER;

CREATE UNIQUE INDEX ux_order_intents_account_client
ON order_intents(account_key, idempotency_key)
WHERE plan_id IS NOT NULL AND account_key IS NOT NULL;

CREATE UNIQUE INDEX ux_intraday_one_entry
ON order_intents(plan_id) WHERE order_role = 'ENTRY';

CREATE UNIQUE INDEX ux_intraday_one_protection
ON order_intents(plan_id) WHERE order_role = 'PROTECTION';

CREATE UNIQUE INDEX ux_intraday_one_local_exit
ON order_intents(plan_id)
WHERE order_role IN ('FORCE_EXIT','EMERGENCY_EXIT');

CREATE UNIQUE INDEX ux_intraday_event_plan_version
ON execution_events(plan_id, run_version)
WHERE plan_id IS NOT NULL AND run_version IS NOT NULL;

CREATE UNIQUE INDEX ux_intraday_one_shot_event
ON execution_events(intent_id, event_type)
WHERE event_type IN (
  'create_send_reserved',
  'identity_recovery_send_reserved',
  'entry_cancel_send_reserved',
  'conditional_cancel_send_reserved',
  'conditional_cancel_acknowledged',
  'triggered_sell_cancel_send_reserved'
);

CREATE TABLE intraday_runs (
  plan_id TEXT PRIMARY KEY
    REFERENCES intraday_plans(plan_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  state TEXT NOT NULL CHECK (state IN (
    'PLANNED','APPROVED','RECONCILING','READY_TO_ENTER',
    'ENTRY_SUBMITTING','ENTRY_UNKNOWN','ENTRY_WORKING','ENTRY_CANCELING',
    'OPEN_UNPROTECTED','PROTECTION_SUBMITTING','PROTECTION_UNKNOWN','PROTECTED',
    'EXIT_CANCELING_PROTECTION','EXIT_SUBMITTING','EXIT_UNKNOWN','EXIT_WORKING',
    'CLOSED','SKIPPED','CANCELLED','RECOVERY_REQUIRED'
  )),
  version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
  writer_id TEXT,
  writer_fence INTEGER NOT NULL DEFAULT 0 CHECK (writer_fence >= 0),
  writer_lease_until TEXT,
  broker_sync_fence INTEGER NOT NULL DEFAULT -1
    CHECK (broker_sync_fence >= -1 AND broker_sync_fence <= writer_fence),
  boot_id_hash TEXT CHECK (boot_id_hash IS NULL OR length(boot_id_hash) = 64),
  approval_generation INTEGER NOT NULL DEFAULT 0 CHECK (approval_generation >= 0),
  approved_envelope_sha256 TEXT CHECK (
    approved_envelope_sha256 IS NULL OR length(approved_envelope_sha256) = 64
  ),
  approval_receipt_sha256 TEXT CHECK (
    approval_receipt_sha256 IS NULL OR length(approval_receipt_sha256) = 64
  ),
  approval_interaction_id TEXT UNIQUE,
  approved_at TEXT,
  approved_writer_fence INTEGER CHECK (
    approved_writer_fence IS NULL OR approved_writer_fence >= 0
  ),
  entry_disabled_at TEXT,
  entry_disabled_reason TEXT,
  entry_submit_count INTEGER NOT NULL DEFAULT 0 CHECK (entry_submit_count IN (0,1)),
  entry_intent_id TEXT UNIQUE REFERENCES order_intents(intent_id),
  protection_intent_id TEXT UNIQUE REFERENCES order_intents(intent_id),
  active_exit_intent_id TEXT UNIQUE REFERENCES order_intents(intent_id),
  triggered_exit_order_id TEXT UNIQUE,
  owned_qty TEXT NOT NULL DEFAULT '0',
  protected_qty TEXT NOT NULL DEFAULT '0',
  average_entry_price TEXT,
  unprotected_since TEXT,
  loss_fuse_at TEXT,
  last_broker_sync_at TEXT,
  last_stream_sync_at TEXT,
  reason_code TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK (
    (writer_id IS NULL AND writer_lease_until IS NULL)
    OR
    (writer_id IS NOT NULL AND writer_lease_until IS NOT NULL)
  ),
  CHECK (
    (approved_envelope_sha256 IS NULL AND approval_receipt_sha256 IS NULL
      AND approval_interaction_id IS NULL
      AND approved_at IS NULL AND approved_writer_fence IS NULL)
    OR
    (approved_envelope_sha256 IS NOT NULL AND approval_receipt_sha256 IS NOT NULL
      AND approval_interaction_id IS NOT NULL
      AND approved_at IS NOT NULL AND approved_writer_fence IS NOT NULL
      AND boot_id_hash IS NOT NULL)
  ),
  CHECK (
    (entry_submit_count = 0 AND entry_intent_id IS NULL)
    OR
    (entry_submit_count = 1 AND entry_intent_id IS NOT NULL)
  ),
  CHECK (
    state <> 'PROTECTED'
    OR (owned_qty <> '0' AND owned_qty = protected_qty)
  ),
  CHECK (
    state NOT IN ('PROTECTION_SUBMITTING','PROTECTION_UNKNOWN','PROTECTED')
    OR protection_intent_id IS NOT NULL
  ),
  CHECK (
    state <> 'OPEN_UNPROTECTED'
    OR (owned_qty <> '0' AND unprotected_since IS NOT NULL)
  ),
  CHECK (
    state NOT IN ('EXIT_SUBMITTING','EXIT_UNKNOWN')
    OR active_exit_intent_id IS NOT NULL
  ),
  CHECK (
    state <> 'EXIT_WORKING'
    OR active_exit_intent_id IS NOT NULL OR triggered_exit_order_id IS NOT NULL
  ),
  CHECK (
    approved_writer_fence IS NULL OR approved_writer_fence <= writer_fence
  ),
  CHECK (
    state NOT IN ('CLOSED','SKIPPED','CANCELLED')
    OR (owned_qty = '0' AND protected_qty = '0')
  )
);

CREATE UNIQUE INDEX ux_intraday_receipt_once
ON intraday_runs(approval_receipt_sha256)
WHERE approval_receipt_sha256 IS NOT NULL;

INSERT INTO schema_migrations(version, applied_at) VALUES (4, :applied_at);
COMMIT;
```

migration은 새 임시 DB, 기존 v1/v2/v3 fixture 복제본, 이미 v4인 DB의 경우를 검사한다. 적용 전
`PRAGMA foreign_keys=ON`, 적용 뒤 `PRAGMA foreign_key_check`와 `PRAGMA quick_check`를 통과해야 한다.
운영 DB에는 이번 작업에서 적용하지 않는다. 실패한 migration을 역 SQL로 되돌리지 않고 pre-migration
backup 복제본을 버리고 다시 만든다.

SQLite CHECK가 표현하지 못하는 pointer 의미는 reservation transaction이 검사한다. entry/protection/
active-exit intent의 `plan_id`와 `account_key`는 run의 immutable plan과 같아야 하고 role은 각 pointer와
정확히 맞아야 한다. 다른 plan/account/role row를 pointer로 연결하려는 시도는 CAS 전에 거부한다.

intraday 전용 `reserve_order_intent()`는 기존 mutable upsert를 호출하지 않고 `INSERT`만 한다.
local create의 `order_role`은 `ENTRY`, `PROTECTION`, `FORCE_EXIT`, `EMERGENCY_EXIT` 중 하나다.
`PROTECTION`은 `first_attempt_at`은 기록하되 `recovery_deadline_at=NULL`이며
`identity_recovery_send_reserved` event를 만들 수 없다. 나머지 일반 order role만 local 8분 deadline을
가질 수 있다.
entry/OCO cancel과 broker가 만든 `triggeredOrderId`는 새 create intent가 아니라 기존 intent의
append-only event다. create 요청은 위 partial unique index가 허용하는 한 번만 예약한다. 같은
`intent_id`나 `(account_key, idempotency_key)`가 이미 있으면 canonical request hash가 같아도 새
submit을 허용하지 않고 기존 row를 읽어 recovery한다. hash가 다르면 즉시 writer를 fence하고
`RECOVERY_REQUIRED`다.

reservation 전에 final wire `clientOrderId`를 1–36자의 `[A-Za-z0-9_-]`로 완성해 기존
`idempotency_key`에 저장한다. intraday adapter는 이 값을 sanitize·truncate·rehash하지 않고,
기존 `_client_order_id(value)` 결과가 입력과 byte-for-byte 같지 않으면 네트워크 전에 거부한다.
따라서 DB unique key와 실제 전송 ID가 항상 같다.

실제 전송 body는 다음 canonical JSON을 `request_json`에 그대로 저장한다.

```python
json.dumps(
    body,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=True,
    allow_nan=False,
)
```

`request_hash`는 문자열 이어붙이기가 아니라 exact key set
`{account_key, plan_id, order_role, method, path, body}`의 canonical JSON SHA-256이다. 최초 call과
identity recovery 모두 현재 plan·시세로 body를 다시 계산하지 않고 저장된 `request_json`을 decode한
동일 key order의 mapping과 같은 transport serializer를 사용한다. 저장 compact JSON 자체가 현재
transport의 wire byte라고 주장하지 않고, 두 호출의 serializer output이 같은지는 fake transport에서
별도로 비교한다. 모든 Decimal은 exponent 없는 canonical string으로 정규화해
`1`, `1.0`, `01`, `1E+0`가 서로 다른 요청처럼 저장되지 않게 한다.

정정·취소가 반환하는 operation order ID는 `execution_events.payload`에
`root_order_id`, `operation`, `operation_order_id`로 append-only 기록한다. 별도 chain table이나
mutable broker ID overwrite를 만들지 않는다. transition transaction은 run CAS, 정확히 한
`execution_events(plan_id, run_version, writer_fence)` row, 필요한 intent, notification outbox를 함께
commit한다.

ENTRY intent가 생기기 전 run event의 기존 NOT NULL `intent_id`에는 `run:{plan_id}` sentinel을 넣는다.
한 run version에는 event row가 정확히 하나이며 그 event type 자체가 transition과 reservation을
함께 표현한다. 별도 generic transition event를 하나 더 쓰지 않는다. run state/owned projection을
바꾸지 않는 raw observation event는 `run_version=NULL`로 두고, projection을 바꾸면 반드시 CAS로
version을 올려 event 하나를 쓴다.

SQLite runtime 연결은 `foreign_keys=ON`, `journal_mode=WAL`, `synchronous=FULL`,
`busy_timeout=5000`으로 고정한다. writer 획득·renewal·모든 mutation 예약은 `BEGIN IMMEDIATE`다.
새 writer가 만료 lease를 인수할 때 `writer_fence`를 증가시키고 `broker_sync_fence=-1`로 만든다.
stable broker 대조를 끝낸 transaction만 `broker_sync_fence=writer_fence`를 기록할 수 있다.
SQLite에서 비교하는 모든 UTC timestamp는 `timespec='microseconds'`와 `+00:00` offset의 같은 길이
canonical text로 저장하며 `Z`, local offset, naive value를 섞지 않는다.

v1 constants는 HTTP timeout 15초, writer heartbeat 5초, lease 45초다. mutation reservation 직전
lease 잔여가 30초 미만이면 먼저 renew하고 CAS한다. broker snapshot 중에는 각 GET 사이에서만
renew하며 하나의 HTTP call 중에는 thread를 추가해 lease를 갱신하지 않는다. sleep·process pause로
lease가 만료되면 해당 writer는 다음 DB write/mutation을 포기하고 새 writer의 reconciliation에 맡긴다.

reservation은 `reserved_writer_fence`, `reserved_run_version`, `reserved_at`, `send_by`를 함께 저장한다.
`send_by`는 ENTRY의 경우 `min(entry_expiry, reserved_at + 5초)`, protection/exit는
`reserved_at + 5초`다. mutation executor는 transport가 socket을 열기 직전 같은 DB에서 fence,
run version/state, request hash, `now <= send_by`, lease 잔여 30초 이상을 다시 확인한다. ENTRY는
정규장·approval·halt·fresh quote/book/spread·entry limit도 다시 확인한다. 하나라도 실패하면 delegate
call 0으로 `RECONCILING`에 넘긴다. 이 check와 transport call 사이에는 await, sleep, callback, queue
handoff를 두지 않는다.

모든 send는 `broker_sync_fence=writer_fence`와 `last_broker_sync_at` age 5초 이하를 요구한다.
PROTECTION은 current `owned_qty`가 request quantity와 같아야 하고, local MARKET exit는 정규장,
OCO competition-cleared·경쟁 SELL exact terminal,
`remaining_owned_qty == request quantity == sellable_qty`가 마지막 snapshot과 DB projection에서 계속
같아야 한다.

모든 상태 CAS는 최소 다음 predicate를 포함한다.

```sql
UPDATE intraday_runs
SET state = :next_state,
    version = version + 1,
    updated_at = :now
WHERE plan_id = :plan_id
  AND state = :expected_state
  AND version = :expected_version
  AND writer_id = :writer_id
  AND writer_fence = :writer_fence
  AND writer_lease_until > :now;
```

영향 row가 1이 아니면 다시 읽어 억지로 이어가지 않고 현재 process를 fence한다. stale writer의
늦은 HTTP 응답도 fence가 다르면 DB에 반영하지 않는다. `reserve_create` transaction은 plan hash,
lease, sync fence, expected state를 검사하고 intent·`execution_orders` projection·
`create_send_reserved` event·run pointer·outbox·CAS를 한 번에 commit한다. `ENTRY`일 때만
`entry_submit_count=1`을 같은 transaction에서 latch한다.

실제 `_migrate_v4()`는 `schema_migrations.version=4`가 있으면 DDL 전체를 skip한다. 현재
`initialize_schema()` transaction 안에서 다시 `BEGIN`하지 않고, 기존 base schema transaction이
끝난 뒤 자기 `BEGIN IMMEDIATE` 하나로 실행한다. version row는 모든 ALTER/index/table 생성이 성공한
마지막 statement에 기록한다. 따라서 문서의 literal SQL을 이미 v4인 DB에 다시 실행하는 방식으로
idempotency를 흉내 내지 않는다.

수량은 DB에서 부동소수점 연산하지 않는다. strict finite nonnegative `Decimal` string으로 읽고,
다음 식을 broker의 각 root order별 **최대 누적** `filledQuantity`에 적용한다. 같은 WS/REST snapshot을
다시 읽어도 더하지 않는다.

```text
owned_qty = cumulative ENTRY BUY fill
            - cumulative triggered protective SELL fill
            - cumulative FORCE_EXIT SELL fill
            - cumulative EMERGENCY_EXIT SELL fill
remaining_owned_qty = owned_qty
```

`owned_qty < 0`이면 0으로 clamp하지 않는다. locally owned fill보다 broker holding이 작음,
알려지지 않은 BUY/SELL, 다른 종목
holding, fractional/초과 fill, state와 맞지 않는 open order는 자동 보정하지 않고
`RECOVERY_REQUIRED`다. OCO leg에는 수량이나 side field가 없으므로 이를 만들어 읽지 않는다.
`protected_qty`는 known group의 `conditionalOrderId`, `type=OCO`, `status=WATCHING`, exact symbol,
`market=US`, top-level `quantity=remaining_owned_qty`, `orderType=LIMIT`을 확인하고, `first/second`가
각각 `type=STOP`, `status=WATCHING`, plan의 exact trigger/order price, null `triggeredOrderId`이며
응답 `expireDate`가 request와 일치할 때만 그 top-level quantity다. 필수 검증값이 없거나 다르면 0이
아니라 보호 미확정이다. create ACK로 conditional ID가 이미 로컬에 영속돼 있으면 그 known ID를
한 번 DELETE하고 204/stable proof 뒤 exit를 검토하며, ID 자체가 없거나 후보로만 추정한 경우에는
건드리지 않고 `RECOVERY_REQUIRED`다.

### 13.4 허용 상태 전이표

재시작 상태를 보존하기 위한 `resume_state`는 추가하지 않는다. 모든 reservation과 broker ID가
원장에 있으므로 process 시작 시 먼저 `RECONCILING`으로 간 뒤 stable snapshot으로 아래 상태를
다시 결정한다. 이 표에 없는 state pair는 모두 오류이며 CAS 전에 거부한다.

| 현재 | 사건과 필수 조건 | 다음 | 허용 mutation |
| --- | --- | --- | --- |
| 없음 | immutable plan 검증 | `PLANNED` | 없음 |
| `PLANNED` | 현재 plan/fence/generation receipt 소비 | `APPROVED` | 없음 |
| `APPROVED` | writer 획득, sync fence 무효화 | `RECONCILING` | read-only |
| `APPROVED`부터 `EXIT_WORKING`까지의 비종결 상태 | process 시작·writer 교체·WS gap | `RECONCILING` | read-only |
| `RECONCILING` | submit 0, 계좌 flat, 주문 0, 승인 유효 | `READY_TO_ENTER` | 없음 |
| `RECONCILING` | submit 0, 승인 세대 불일치 | `PLANNED` | 없음 |
| `RECONCILING` | entry cancel reservation 존재, root BUY nonterminal | `ENTRY_CANCELING` | cancel 재호출 없음 |
| `RECONCILING` | conditional cancel reservation 존재, OCO nonterminal | `EXIT_CANCELING_PROTECTION` | cancel 재호출 없음 |
| `RECONCILING` | submit 1, ENTRY terminal, fill 0 | `CANCELLED` | 없음 |
| `RECONCILING` | local/triggered EXIT exact terminal, flat, active conditional/leg SELL 0 | `CLOSED` | 없음 |
| `RECONCILING` | local/triggered EXIT terminal, owned > 0 | `RECOVERY_REQUIRED` | 새 SELL 금지 |
| `RECONCILING` | 알려진 BUY open | `ENTRY_WORKING` | 없음 |
| `RECONCILING` | ENTRY create identity 불명 | `ENTRY_UNKNOWN` | gate 충족 시 일반-order recovery 1회 |
| `RECONCILING` | owned > 0, OCO/SELL 0 | `OPEN_UNPROTECTED` | 보호 또는 emergency 예약 |
| `RECONCILING` | owned > 0, protection intent가 명확한 비접수 reject, OCO/SELL 0 | `OPEN_UNPROTECTED` | OCO 재생성 금지, emergency만 검토 |
| `RECONCILING` | 13.3의 모든 group/leg field가 맞는 known OCO exact `WATCHING` | `PROTECTED` | 없음 |
| `RECONCILING` | known triggered/local exit 존재 | `EXIT_WORKING` | 없음 |
| `RECONCILING` | flat인데 known OCO/SELL 잔존 | `EXIT_CANCELING_PROTECTION` | cancel 예약 1회 |
| `RECONCILING` | 미등록 보유·주문 또는 후보 복수 | `RECOVERY_REQUIRED` | 없음 |
| `READY_TO_ENTER` | entry 창 만료 또는 영구 gate 실패 | `SKIPPED` | 없음 |
| `READY_TO_ENTER` | fresh below→above crossing과 모든 gate | `ENTRY_SUBMITTING` | LIMIT BUY create 1회 |
| `ENTRY_SUBMITTING` | exact ACK/order ID | `ENTRY_WORKING` | 없음 |
| `ENTRY_SUBMITTING` | timeout/reset/409/429/5xx/ID 누락 | `ENTRY_UNKNOWN` | gate 충족 시 일반-order recovery 1회 |
| `ENTRY_SUBMITTING` | 명확 reject, 누적 fill 0 | `CANCELLED` | 없음 |
| `ENTRY_UNKNOWN` | identity와 ID 복구 | 관측에 따라 `ENTRY_WORKING`, `ENTRY_CANCELING`, `OPEN_UNPROTECTED` | 추가 create 없음 |
| `ENTRY_UNKNOWN` | 후보 0, deadline 전, recovery 미사용, 정규장·entry/approval/fence/halt/quote/book/spread/limit 모두 유효 | `ENTRY_UNKNOWN` | 저장 body/client ID recovery 1회 |
| `ENTRY_UNKNOWN` | 8분 경과·hash 재현 불가·후보 복수 | `RECOVERY_REQUIRED` | POST 금지 |
| `ENTRY_WORKING` | fill 0이고 entry 만료 | `ENTRY_CANCELING` | cancel 1회 |
| `ENTRY_WORKING` | 누적 fill > 0, 잔량 > 0 | `ENTRY_CANCELING` | cancel 1회 |
| `ENTRY_WORKING` | terminal, fill > 0 | `OPEN_UNPROTECTED` | 보호 예약 |
| `ENTRY_WORKING` | terminal, fill 0 | `CANCELLED` | 없음 |
| `ENTRY_CANCELING` | terminal, owned > 0 | `OPEN_UNPROTECTED` | 보호 예약 |
| `ENTRY_CANCELING` | terminal, owned = 0 | `CANCELLED` | 없음 |
| `ENTRY_CANCELING` | cancel 결과 불명 | 유지 또는 `RECOVERY_REQUIRED` | 재-cancel 금지 |
| `OPEN_UNPROTECTED` | OCO create 예약 | `PROTECTION_SUBMITTING` | OCO create 1회 |
| `OPEN_UNPROTECTED` | 보호 불가, OCO/SELL 부재·sellable exact | `EXIT_SUBMITTING` | emergency MARKET 1회 |
| `PROTECTION_SUBMITTING` | ID와 GET `WATCHING` 확인 | `PROTECTED` | 없음 |
| `PROTECTION_SUBMITTING` | locally persisted ID의 schema-valid detail이 plan과 불일치, trigger 없음 | `EXIT_CANCELING_PROTECTION` | known OCO DELETE 1회 |
| `PROTECTION_SUBMITTING` | known ID detail `PAUSED`, trigger 없음 | `EXIT_CANCELING_PROTECTION` | known OCO DELETE 1회 |
| `PROTECTION_SUBMITTING` | known ID detail `ORDERING/ORDERED` | `PROTECTION_UNKNOWN` | bounded read-only, 별도 SELL 0 |
| `PROTECTION_SUBMITTING` | known ID에서 `triggeredOrderId` 확인 | `EXIT_WORKING` | 해당 일반 SELL만 추적 |
| `PROTECTION_SUBMITTING` | 결과 불명 | `PROTECTION_UNKNOWN` | read-only, conditional 재POST 0 |
| `PROTECTION_UNKNOWN` | 이미 로컬에 영속된 ID의 exact detail로 `WATCHING` 복구 | `PROTECTED` | 추가 OCO 없음 |
| `PROTECTION_UNKNOWN` | `triggeredOrderId` 확인 | `EXIT_WORKING` | 없음 |
| `PROTECTION_UNKNOWN` | known ID가 `PAUSED` | `EXIT_CANCELING_PROTECTION` | 미예약이면 DELETE 1회 |
| `PROTECTION_UNKNOWN` | known ID가 `ORDERING/ORDERED`이고 bounded snapshot 안 | 유지 | read-only |
| `PROTECTION_UNKNOWN` | known ID가 `ORDERING/ORDERED`로 30초/3 pair 초과 | `RECOVERY_REQUIRED` | DELETE·SELL 금지 |
| `PROTECTION_UNKNOWN` | exact `EXPIRED`, trigger 0, post-expiry stable owned/sellable | `EXIT_SUBMITTING` | emergency MARKET 1회 |
| `PROTECTION_UNKNOWN` | `COMPLETED`인데 trigger 0/2 또는 상태 조합 불가 | `RECOVERY_REQUIRED` | SELL 금지 |
| `PROTECTION_UNKNOWN` | 응답 ID 없음 또는 목록 후보만 존재 | `RECOVERY_REQUIRED` | conditional POST·SELL 모두 금지 |
| `PROTECTION_UNKNOWN` | ID/부재 모호 | `RECOVERY_REQUIRED` | SELL 금지 |
| `PROTECTED` | target/stop `triggeredOrderId` 확인 | `EXIT_WORKING` | 없음 |
| `PROTECTED` | group `ORDERING/ORDERED`, trigger ID 아직 없음 | `PROTECTION_UNKNOWN` | bounded read-only, 별도 SELL 0 |
| `PROTECTED` | group `PAUSED`, trigger 없음 | `EXIT_CANCELING_PROTECTION` | OCO DELETE 1회 |
| `PROTECTED` | exact `EXPIRED`, trigger 0, post-expiry stable owned/sellable | `EXIT_SUBMITTING` | emergency MARKET 1회 |
| `PROTECTED` | `COMPLETED`인데 trigger 0/2 또는 조합 불가 | `RECOVERY_REQUIRED` | SELL 금지 |
| `PROTECTED` | force-exit 시각, 아직 `WATCHING` | `EXIT_CANCELING_PROTECTION` | OCO cancel 1회 |
| `PROTECTED` | flat, known protection inactive, 경쟁 SELL 없음 | `CLOSED` | 없음 |
| `EXIT_CANCELING_PROTECTION` | 어느 leg든 `triggeredOrderId` 또는 그 일반 SELL 발견 | `EXIT_WORKING` | 별도 SELL 없음 |
| `EXIT_CANCELING_PROTECTION` | exact DELETE 204 영속 + post-ACK stable snapshot에서 protection inactive·경쟁 SELL 없음, owned > 0, sellable exact | `EXIT_SUBMITTING` | force/emergency MARKET 1회 |
| `EXIT_CANCELING_PROTECTION` | flat, known protection inactive, 경쟁 주문 0 | `CLOSED` | 없음 |
| `EXIT_CANCELING_PROTECTION` | DELETE 응답 불명 또는 204 미영속 | `RECOVERY_REQUIRED` | 재-DELETE·별도 SELL 금지 |
| `EXIT_SUBMITTING` | exact ACK/order ID | `EXIT_WORKING` | 없음 |
| `EXIT_SUBMITTING` | 결과 불명 | `EXIT_UNKNOWN` | gate 충족 시 일반-order recovery 1회 |
| `EXIT_UNKNOWN` | exact ID/fill 발견 | `EXIT_WORKING` 또는 `CLOSED` | 추가 SELL 없음 |
| `EXIT_UNKNOWN` | 후보 0, deadline 전, recovery 미사용, 정규장·OCO/SELL 0·holding/owned/request/sellable exact | `EXIT_UNKNOWN` | 저장 MARKET body/client ID recovery 1회 |
| `EXIT_UNKNOWN` | 경쟁 SELL·ownership·sellable 중 하나라도 불명 | `RECOVERY_REQUIRED` | recovery POST도 금지 |
| `EXIT_WORKING` | 일부 fill, 잔량 > 0 | `EXIT_WORKING` | 새 SELL 금지 |
| `EXIT_WORKING` | flat, known 일반 exit exact terminal, active conditional/leg SELL 0 | `CLOSED` | 없음 |
| `EXIT_WORKING` | terminal인데 owned > 0 | `RECOVERY_REQUIRED` | 두 번째 SELL 금지 |
| `RECOVERY_REQUIRED` | 운영자 명시 복구 + 새 stable snapshot | `RECONCILING` | 먼저 read-only |
| 종결 상태 | 어떤 사건이든 | 그대로 | 없음 |

`RECONCILING` 분기는 표 순서가 아니라 다음 명시적 우선순위로 하나만 선택한다: 미등록/모호 상태,
기존 cancel reservation, active exit, terminal exit, protection terminal/UNKNOWN, active OCO, active ENTRY,
terminal ENTRY, 마지막으로 submit 0 flat. cancel reservation을 발견한 restart가 `ENTRY_WORKING`이나
`PROTECTED`로 되돌아가 one-shot cancel 사실을 잃어서는 안 된다.

`RECOVERY_REQUIRED`, `SKIPPED`, `CANCELLED`, `CLOSED`는 restart만으로 `RECONCILING`에 들어가지
않는다. 첫 상태는 운영자의 명시 복구 CAS가 필요하고 나머지는 종결 상태다. flat에서 halt·세션·
데이터 문제로 진입하지 않은 경우는 `SKIPPED`와 allowlisted reason을 사용한다. 포지션이 남은
장애는 원래 보호/exit 상태 또는 `RECOVERY_REQUIRED`에 두고 entry-disable만 latch한다.
`CLOSED` 역시 holdings 0, sellable 0, known 일반 주문이 exact terminal이고 known OCO가 active
group 상태가 아니며 leg-created SELL이 없음을 모두 읽었을 때만 허용한다. top-level
`CANCELED`라는 존재하지 않는 값을 기다리거나 CLOSED-list membership만으로 종료를 추정하지 않는다.

### 13.5 broker snapshot과 reconciliation

Toss read API는 계좌 전체의 원자 snapshot을 제공하지 않는다. 따라서 한 번의 GET 묶음을 사실로
가정하지 않고 다음 `A -> WS ACK -> B` 순서를 사용한다.

1. SQLite plan/run/intent/event를 한 transaction에서 읽고 expected IDs와 hash를 고정한다.
2. account-wide holdings를 strict parse한다. plan symbol 외 보유는 0이어야 한다.
3. account-wide 일반 `OPEN` 전체를 읽는다.
4. 거래일 `CLOSED`를 `limit=100`으로 cursor가 끝날 때까지 읽는다. `from/to`는 ET session date를
   그대로 넣지 않고 plan의 regular open/close를 KST로 변환한 두 calendar date를 양 끝 포함으로
   사용한다. 미국 정규장은 KST 자정을 넘을 수 있기 때문이다. cursor 반복, item 중복·변형,
   20 page 초과는 모두 실패다.
5. account-wide 조건주문 `OPEN`과 `CLOSED`를 각각 `limit=100`으로 끝까지 읽어 session item을 locally
   filter하고, known 조건주문 detail을 읽는다. 현재 adapter에는 조건주문 history의 `from/to`가 없으므로
   첫 page만 보고 끝내지 않는다. `hasNext=true`인데 next cursor가 없거나 cursor가 반복되거나
   20 page를 넘으면 실패한다. 같은 conditional ID가 두 set에 나타나면 normalized item이 같아도
   detail을 다시 읽고 한 건으로 합치며, 값이 다르면 snapshot을 폐기한다.
6. locally known 모든 root/operation/triggered order detail을 읽는다.
7. owned > 0 또는 SELL을 검토하는 상태에서 plan symbol의 sellable quantity를 읽는다.
8. 진입 전 상태에서만 USD buying power와 session을 덮는 현재 미국 수수료율을 읽는다. 응답에 broker
   timestamp가 없으므로 local receive monotonic/UTC 시각을 붙인다.
9. 선택 종목 trade/orderbook과 account personal-order topic을 정확히 한 WebSocket declaration으로
   구독하고 exact ACK를 확인한다. 이번 작업의 실제 Mac shadow job은 personal topic을 열지 않으며,
   이 단계는 fake frame으로만 검증한다.
10. 2–8을 B snapshot으로 반복하고 normalized fingerprint를 비교한다.

일반 주문의 `PARTIAL_FILLED`는 OPEN/CLOSED 양쪽 schema에 올 수 있으므로 list membership을 terminal
판정으로 쓰지 않는다. 같은 order ID가 두 set에 나타나면 normalized item이 같아도 detail을 다시
읽고 한 건으로 합치며, 값이 다르면 snapshot을 폐기한다.

normalized fingerprint에는 holding symbol/quantity, 일반 order ID/status/side/quantity/누적 fill,
조건주문의 ID/type/status/symbol/market/top-level quantity/orderType/expireDate, 양 leg의
type/status/triggerPrice/orderPrice/triggeredOrderId, sellable, buying power, active commission이 포함된다.
timestamp와 응답 순서처럼 경제 상태가 아닌 field만 제외한다. A/B가 다르거나 queued personal frame이
하나라도 있으면 그 frame을 projection에 적용하지 않고 dirty trigger로만 소비한 뒤 REST를 다시 읽는다.
전체 30초와 세 번의 snapshot pair를 넘기지 않는다. 안정화되지 않으면 `RECONCILING`+entry-disabled 또는
`RECOVERY_REQUIRED`다. polling 횟수를 늘려 일치할 때까지 버티지 않는다.

read-only GET은 rate-limit group별로 순차 실행한다. 429에서는 `Retry-After`가 30초 전체 deadline
안에 들어올 때만 한 번 기다리고, 그렇지 않으면 대조 실패다. malformed item을 건너뛰거나 unknown
enum을 terminal로 추정하지 않는다. REST `execution.filledQuantity`와 정확한
`averageFilledPrice`를 누적 projection의 권위로 쓰며, personal WS event는 빠른 재조회 trigger일
뿐이다. `orderedAt`이나 receive order로 REST와 frame 사이의 인과 순서를 만들어내지 않는다.

state별 수량 대조는 다음처럼 다르다.

- SELL reservation 전: `holding_qty == owned_qty == sellable_qty`.
- OCO `WATCHING`: `holding_qty == owned_qty`; broker가 조건주문 수량을 sellable에서 어떻게 예약하는지
  계약되지 않았으므로 sellable은 finite `0..owned_qty`만 허용하고 equality를 가정하지 않는다.
- triggered/local SELL open: `holding_qty == owned_qty`; sellable은 open SELL 잔량과 충돌하지 않는지
  strict order set으로 대조한다.
- 새 MARKET SELL 직전: 13.6의 OCO competition-cleared proof와 모든 경쟁 SELL exact terminal 뒤
  `sellable_qty == remaining_owned_qty`를 반드시 다시 확인한다.

조건주문 group/leg 상태는 OPEN/CLOSED membership이 아니라 다음 표로 해석한다.

| exact detail | owned > 0일 때 판정/동작 |
| --- | --- |
| group `WATCHING`, 양 leg `WATCHING`, null triggered IDs, 모든 identity/가격/만료 검증 일치 | 유일한 `PROTECTED` |
| group `PAUSED`, triggered IDs null | 미보호; entry-disable, known ID DELETE 1회 후 204 proof 대기 |
| group `ORDERING` 또는 `ORDERED` | broker SELL 생성 중/완료 가능; bounded REST 재조회만, 별도 DELETE/SELL 0 |
| group `COMPLETED`, triggered ID 1개 | 그 일반 SELL detail/fill만 `EXIT_WORKING`으로 추적 |
| group `COMPLETED`, triggered ID 0개 또는 2개 | 모순/미완전 snapshot; `RECOVERY_REQUIRED`, 별도 SELL 0 |
| group `EXPIRED`, triggered ID 1개 | 생성된 일반 SELL만 추적 |
| group `EXPIRED`, triggered ID 0개 | post-expiry stable A/B, 경쟁 SELL 0, owned/sellable exact일 때만 자연 competition-cleared |
| unknown group/leg enum, required field 누락, 상태 조합 불가 | `RECOVERY_REQUIRED` |

`ORDERING/ORDERED`가 snapshot 30초/3 pair 안에 triggered ID 또는 안정 terminal detail로 수렴하지
않아도 `COMPLETED`나 취소로 추정하지 않는다. flat이면 holdings 0과 실제 일반 SELL fills를 먼저
설명할 수 있어야 한다.

dedicated account의 수동 주문 금지는 이 A/B 절차로도 제거되지 않는 외부 race의 운영 경계다.
미등록 보유·주문을 발견하면 비슷한 종목·가격·시간이라는 이유로 채택하거나 매도하지 않는다.

`READY_TO_ENTER` 직전 B snapshot의 USD `cashBuyingPower`가 plan의 `cash_reserved`보다 작거나, active
commission으로 계산한 보수적 왕복비용이 plan reserve보다 크면 그 날을 `SKIPPED`로 끝낸다. 현금이
늘어도 수량을 다시 계산해 키우지 않고 immutable plan quantity를 유지한다. latest quote로 entry
가격을 다시 계산하지 않으며 crossing/entry-limit gate만 다시 적용한다.

### 13.6 broker mutation 프로토콜과 crash 의미

일반 order create(ENTRY/FORCE_EXIT/EMERGENCY_EXIT)는 intent마다 `최초 1회 + identity recovery 최대
1회`다. 조건 OCO create는 공식 문서가 일반 주문과 같은 10분 창을 명시하지 않으므로 `최초 1회 +
자동 recovery 0회`다. 그 보장에 대한 Toss의 서면/계약 확인 없이는 이 값을 늘리지 않는다.

1. `reserve_create`가 canonical body, client ID, hash, `first_attempt_at`, 일반 주문만 8분 local deadline,
   `create_send_reserved`와 `*_SUBMITTING`을 commit한다.
2. mutation executor가 socket 직전 fence/version/state/hash/send-by/lease와 action-specific market·broker
   gate를 다시 확인한다. 실패하면 POST 0이다.
3. 검증을 통과한 경우에만 저장된 body로 POST를 한 번 호출한다.
4. 일반·조건 create는 exact HTTP 200과 각각 required `orderId`/`conditionalOrderId`가 있어야
   성공이다. locally reserved client ID가 정본이며 optional/nullable echo가 있으면 exact 일치도
   검사해 immutable projection에 기록한다.
5. 명확한 validation reject와 fill 0만 terminal reject로 처리한다.
6. timeout, connection reset, truncated JSON, 408/425, 구조화된 `request-in-progress` 409, 429, 5xx,
   success code인데 ID가 없거나 다른 2xx는 `*_UNKNOWN`이다. `idempotency-key-conflict`/same
   key-different body 422는 writer identity
   위반으로 fence+`RECOVERY_REQUIRED`이며 recovery 대상이 아니다.
7. 일반-order UNKNOWN에서는 먼저 13.5 snapshot을 수행한다. locally known ID를 찾지 못했고 deadline
   전이며 `identity_recovery_send_reserved`가 없을 때만 같은 body/client ID POST를 한 번 더 호출한다.
   OCO UNKNOWN은 read-only 대조만 하고 자동 POST를 호출하지 않는다.
8. recovery도 별도 reservation·socket 직전 gate를 모두 통과해야 한다. 두 번째 응답도 불명하거나
   8분이 지났으면 더 이상 POST하지 않는다.

recovery POST는 최초 request가 실제로 전송되지 않았다면 새 주문을 만들 수 있다. 따라서 일반
ENTRY는 현재 entry gate가 전부 유효할 때, 일반 EXIT는 정규장·holding/owned/request/sellable exact와
경쟁 OCO/SELL 부재가 유효할 때만 호출한다. gate가 무효면 read-only 대조로 ID가 나타나는지만
확인하고 새 POST는 하지 않는다. 일반 create의 Toss 10분 broker window 뒤에는 같은 client ID도 새
주문이 될 수 있으므로 local 8분 deadline 뒤 자동 call은 무조건 금지한다. PROTECTION은 이 recovery
분기에 들어오지 않는다.

cancel/conditional delete에는 멱등키가 없다. target 종류별로 `entry_cancel_send_reserved`,
`conditional_cancel_send_reserved`, `triggered_sell_cancel_send_reserved`를 정확히 한 번 INSERT하고
commit 뒤 호출한다. triggered SELL event의 `intent_id`는 `triggered:{order_id}` sentinel이다. OCO group
cancel과 이후 발동 일반 SELL cancel은 서로 다른 event identity이므로 unique index가 둘째 동작을
잘못 막지 않는다. 일반 order cancel만 exact HTTP 200과 `result.orderId`를 operation ID로 append한다.
conditional DELETE 성공은 exact HTTP 204/no body이고 operation ID를 만들지 않으며
`conditional_cancel_acknowledged` event로 그 사실만 영속화한다. timeout/409/429/5xx/응답 누락 뒤에는
같은 cancel/DELETE를 다시 호출하지 않고 root/operation/OPEN/CLOSED/detail을 읽는다. 특히 DELETE
응답 유실 뒤 목록 부재·404를 취소 성공으로 추정하지 않는다. v1은 일반 modify와 OCO modify를 전혀
사용하지 않는다.

crash 지점의 의미는 고정한다.

| crash 위치 | 재시작 판정 |
| --- | --- |
| reservation commit 전 | broker call 0; 이전 상태 유지 |
| commit 후 POST 전 | `*_SUBMITTING`; 새 intent 금지, reconciliation |
| 일반 POST 후 ACK 저장 전 | `*_UNKNOWN`; exact identity recovery 규칙 |
| OCO POST 후 ACK 저장 전 | `PROTECTION_UNKNOWN`; read-only 대조 뒤 `RECOVERY_REQUIRED`, 재POST 0 |
| ACK 저장 후 state CAS 전 | immutable root ID projection으로 상태 재구성 |
| cancel 예약 후 call 전 | 재-cancel 금지; read-only 대조 |
| 일반 cancel call 후 응답 저장 전 | root/operation chain 대조, 두 번째 cancel 금지 |
| conditional DELETE 후 204 저장 전 | 취소 불명; 재-DELETE·별도 SELL 금지 |
| writer 교체 뒤 늦은 응답 | fence 불일치로 DB 반영 0 |
| DB lock/IO/quick-check 실패 | entry-disable latch, broker mutation 금지, 긴급 경보 |

OCO에 top-level `CANCELED`를 기대하지 않는다. force/emergency SELL 전 `competition-cleared` 증거는
(a) exact 204 event가 현재 conditional ID/fence에 영속돼 있고, (b) 그 event 뒤 stable A/B에서 해당
ID가 OPEN에 없으며 detail/list 어디에도 `WATCHING/PAUSED/ORDERING/ORDERED`가 없고, (c) 양 leg의
`triggeredOrderId`와 account-wide 경쟁 SELL이 없고, (d) ownership/sellable이 exact인 경우다. 자연
`EXPIRED`도 exact detail과 같은 stable snapshot이 no-trigger/no-SELL을 증명할 때만 별도 SELL을
검토한다. `COMPLETED`는 단독으로 cancellation 증거가 아니다.

DELETE 응답이 불명하면 이후 목록 부재만으로 (a)를 대체할 수 없어 emergency/force SELL로 넘어가지
않는다. 이 때문에 일부 장애에서는 자동청산보다 과매도 방지가 우선되어 `RECOVERY_REQUIRED`가
남는다. 반대로 competition-cleared와 ownership·sellable·정규장 상태가 모두 명확하면 사용자
결정대로 당일 plan 잔여 전 수량을 한 번의 MARKET SELL intent로 처리한다. 가격·체결·손실 상한은
보장하지 않는다.

### 13.7 approval v2와 receipt 소비 순서

현재 `intraday_plans.mode='shadow'` row는 경제값을 잠그는 정본으로 그대로 둔다. live 목적을 표현하려고
그 row를 UPDATE하거나 mode CHECK를 느슨하게 하지 않는다. 대신 shadow plan 전체와 아직 plan에 없는
SLO·비상정책을 다음 strict envelope에 묶고 그 canonical hash를 `approved_envelope_sha256`로 사용한다.
이 schema는 이번 범위에서 synthetic fixture로만 소비한다.
`plan_hash`는 오직 FK로 연결된 `intraday_plans.plan_hash`이고 두 값을 서로 대신 비교하지 않는다.

```json
{
  "schema_version": 2,
  "purpose": "INTRADAY_LIVE_ENTRY",
  "plan_id": "<immutable plan id>",
  "plan_hash": "<64 hex>",
  "account_alias": "<non-secret display alias>",
  "session_date": "YYYY-MM-DD",
  "symbol": "<locked symbol>",
  "quantity": "<canonical integer string>",
  "entry_trigger": "<decimal>",
  "entry_limit": "<decimal>",
  "target_trigger": "<decimal>",
  "target_limit": "<decimal>",
  "stop_trigger": "<decimal>",
  "stop_limit": "<decimal>",
  "cash_reserved": "<decimal>",
  "planned_risk": "<decimal>",
  "planned_reward": "<decimal>",
  "entry_start": "<aware ISO timestamp>",
  "entry_expiry": "<aware ISO timestamp>",
  "force_exit_at": "<aware ISO timestamp>",
  "protection_slo_seconds": "<positive integer>",
  "exit_fill_slo_seconds": "<positive integer>",
  "emergency_exit": {
    "policy": "MARKET_ALL_REMAINING_OWNED",
    "regular_session_only": true,
    "price_not_guaranteed": true
  },
  "boot_id_hash": "<64 hex>",
  "writer_fence": "<nonnegative integer>",
  "approval_generation": "<positive integer>",
  "nonce": "<single-use random value>",
  "issued_at": "<aware ISO timestamp>",
  "expires_at": "<aware ISO timestamp>",
  "interaction_binding": "<64 hex>"
}
```

`interaction_binding`은 자신을 제외한 위 exact key set의 canonical JSON SHA-256이다. 임의 extension
key, duplicate JSON key, unknown purpose/schema, exponent/NaN/Infinity, timezone 없는 시각은 거부한다.
`schema_version`, 두 SLO, `writer_fence`, `approval_generation`은 JSON integer이고, quantity와 모든
경제값은 exponent 없는 Decimal string이다. ID/hash/nonce/purpose/decision/timestamp는 string이며
boolean field는 JSON boolean만 허용한다. placeholder가 따옴표 안에 보이는 위 예시는 표시용일 뿐
parser가 숫자 문자열을 허용한다는 뜻이 아니다.

모든 `*_sha256`의 byte 정의는 하나다. exact key set을 `sort_keys=True`, `separators=(",", ":")`,
`ensure_ascii=True`, `allow_nan=False`로 JSON encode한 **UTF-8, BOM/newline 없는 bytes**를 SHA-256한다.
`envelope_sha256`는 envelope 전체 canonical bytes, `interaction_binding`은 그 field만 제외한 canonical
bytes, receipt hash는 receipt 전체 canonical bytes다. 파일 원문·pretty JSON·dict insertion order를
hash하지 않는다.

macOS `boot_id_hash`는 standard-library `sysctlbyname("kern.bootsessionuuid")`로 읽은 값을 whitespace
trim한 뒤 canonical lowercase UUID 형식으로 재직렬화하고,
`SHA256(b"macos-boot-v1\0" + uuid_ascii)`로 만든다. command output, hostname, uptime, process start
time을 대신 쓰지 않는다. 이 값을 읽거나 형식을 검증하지 못하면 승인과 신규 entry를 차단한다.
Discord 화면에는 account alias, symbol, quantity, 여섯 가격, cash/risk/reward, 세 시간, 두 SLO,
MARKET 전량청산과 가격 비보장, plan hash suffix가 모두 보여야 한다.

승인 worker가 쓰는 receipt exact key set은 다음이다.

```json
{
  "schema_version": 2,
  "purpose": "INTRADAY_LIVE_ENTRY",
  "decision": "APPROVE",
  "plan_id": "<same>",
  "plan_hash": "<same>",
  "interaction_binding": "<same>",
  "approval_generation": "<same>",
  "writer_fence": "<same>",
  "boot_id_hash": "<same>",
  "nonce_sha256": "<64 hex>",
  "discord_guild_id": "<allowlisted>",
  "discord_channel_id": "<allowlisted>",
  "discord_user_id": "<allowlisted>",
  "interaction_id": "<single-use>",
  "decided_at": "<aware ISO timestamp>",
  "expires_at": "<aware ISO timestamp>",
  "envelope_sha256": "<64 hex>"
}
```

consumer 검증 순서는 바꾸지 않는다.

1. root-owned anchor와 mailbox를 fd-relative로 열고 local filesystem, every ancestor/dir의 expected
   UID/GID/mode와 non-writable 상태를 확인한다. `O_NOFOLLOW`로 receipt를 열고 `fstat`으로 regular
   file, expected approver UID/GID, exact mode `0640`, 최대 32 KiB를 검사한다. `nlink=1`만 consume하고,
   `nlink=2`는 아래 publish recovery 동안 bounded pending이지 승인도 영구-invalid도 아니다.
2. strict duplicate-key rejecting JSON parser로 exact key set과 type을 검사한다.
3. envelope/plan/interaction/nonce hash를 constant-time 비교한다.
4. current boot ID hash, writer fence, approval generation, session/entry expiry를 비교한다.
5. exact Discord guild/channel/user와 아직 소비되지 않은 interaction ID를 검사한다.
6. DB의 immutable plan payload를 다시 canonicalize해 모든 경제값을 비교한다.
7. stable broker snapshot, entry-disable, daily submit 0, current risk/market/halt gate를 다시 검사한다.
8. 한 `BEGIN IMMEDIATE` transaction에서 `PLANNED -> APPROVED` CAS와 receipt hash,
   `approval_interaction_id`를 한 번만 기록한다. 두 값은 각각 UNIQUE여서 다른 bytes로 같은 interaction을
   재사용하는 경합도 DB가 거부한다.
9. trader는 approver-owned inbox의 file을 unlink·rename할 권한을 받지 않는다. file이 남아 있어도
   unique receipt hash와 run CAS가 consumed 정본이므로 두 번째 consume는 실패한다.

별도 `toss-trader`/`toss-approver` UID와 cross-UID mailbox ownership이 없으면 이 v2 receipt도 live
capability로 인정하지 않는다. same-UID shadow worker가 만든 기존 v1 receipt를 schema upgrade로
자동 변환하지 않는다. pre-entry reboot/writer fence 변경은 approval generation을 올리고 기존
receipt를 무효화한다. fill이 이미 있으면 승인 없이 보호·청산만 계속한다.

v2 publisher의 no-clobber 방식은 temp inode를 final name에 hard-link한 뒤 temp link를 지우는 기존
패턴을 유지한다. `link` 직후 crash하면 `nlink=2`가 남을 수 있으므로 approver 시작 때 자기 mailbox의
allowlisted temp 이름만 검사한다. final과 temp가 같은 regular inode·owner/group/mode/size/hash이고
final directory entry가 durable할 때만 temp를 unlink하고 directory를 fsync한다. consumer는 최대
30초/3회 동안 `APPROVAL_PUBLISH_PENDING`으로 기다린 뒤에도 nlink가 1이 아니면 consume 0+경보로
남긴다; trader가 상대 directory를 cleanup하지 않는다. `after_link_before_temp_unlink` kill test와
악성 다른-inode temp/hardlink, remote filesystem, symlink, wrong owner/group/mode negative test를
필수로 둔다.

### 13.8 설정·시장·WebSocket의 fail-closed 계약

기존 `IntradayConfig`에는 세 값만 추가한다.

```yaml
strategy:
  intraday:
    protection_slo_seconds: null
    exit_fill_slo_seconds: null
    emergency_market_fallback: null
    live_execution_enabled: false
```

두 SLO는 positive integer, emergency policy는 명시적 boolean이어야 한다. shadow plan은 null을
허용하지만 approval v2/live policy 생성은 하나라도 null이면 실패한다. 일반 create의 8분 recovery
deadline·최대 1회, 조건 create recovery 0회, cancel/DELETE 최대 1회, snapshot 30초/3 pair는 config로
늘릴 수 없는 safety constant다.
별도 daily-loss, generic retry, replay mode, 두 번째 symbol 설정은 추가하지 않는다.

automatic selection에서는 `runtime.symbols=[]`를 유지하고 immutable plan symbol 하나가 effective
allowlist다. `live.allowed_symbols`를 동적으로 덮어 두 번째 정본으로 만들지 않는다. checked-in config는
계속 `runtime.mode=shadow`, `toss.live_enabled=false`, `live_execution_enabled=false`,
`live.emergency_stop=true`, `live.allowed_symbols=[]`여야 한다. 실제 현금 fraction, risk, 가격 fraction,
SLO 값은 사용자가 승인할 live config가 아니므로 이번 작업에서 임의 기본값으로 채우지 않는다.

미국 halt/LULD 입력은 다음 normalized record만 상태기계가 받는다.

```text
symbol, status=CLEAR|HALTED|UNKNOWN, observed_at, valid_until,
source_name, source_event_id, schema_version
```

`CLEAR`이고 exact symbol이며 `observed_at <= now <= valid_until`인 경우만 entry gate를 통과한다.
현재 Toss에는 authoritative source가 없고 외부 provider도 확정되지 않았으므로 production record는
항상 `UNKNOWN`이며 entry를 차단한다. 비실거래 시험은 세 상태 fixture로 parser·staleness를 검증한다.
provider 선택·자격증명·실데이터 시험은 이후 별도 P0다.

halt `UNKNOWN/HALTED`는 신규 ENTRY만 막는다. 이미 생긴 position의 BUY cancel, OCO 확인·취소,
ownership 대조와 broker가 접수 가능한 risk-reducing SELL을 자동으로 막는 공통 safety flag로 쓰지
않는다. 다만 체결 불가 가능성과 open risk를 긴급 알림에 표시하고 flat을 추정하지 않는다.

미래 실행 연결은 한 WebSocket의 full-replace declaration에 정확히 다음 세 topic만 둔다.

```text
trade:us:{plan.symbol}
orderbook:us:{plan.symbol}
personal:order:{accountSeq}
```

request ID, `subscribed` exact set, 빈 `rejected`를 확인하기 전에는 frame을 적용하지 않는다. ACK 전 data,
다른 symbol/account, unknown topic, binary, duplicate JSON key, 64 KiB 초과, queue overflow, timestamp
역행·future는 연결을 폐기하고 entry-disable 뒤 `RECONCILING`으로 간다. application frame을 버려
최신값만 남기는 최적화는 personal order에 사용하지 않는다. bounded queue가 가득 차면 전체 연결을
닫고 REST로 대조한다.

personal enum은 `PARTIAL_FILL`을 REST `PARTIAL_FILLED`에 명시적으로 매핑한다. 코드·로그·UI에 남은
비공식 `PARTIALLY_FILLED`는 새 runtime과 parser에서 허용하지 않는다. 다만 WS frame의
`filledQuantity`도 projection에 대입하지 않고 해당 order의 REST detail 재조회만 trigger한다. 누적
수량과 평균가는 REST의 exact `execution.filledQuantity`와 `averageFilledPrice`로 확정한다. 첫 연결과
모든 재연결 순서는 13.5의 REST A→ACK→REST B다.

이번 Mac shadow release는 기존 두 market topic만 실제로 열며 accountSeq, personal topic, trading DB,
order adapter를 받지 않는다. 3-topic 경로는 recorded/fake frame으로만 검증한다. shadow process와
미래 live writer를 동시에 띄우는 문제는 이번에는 live writer 자체를 설치하지 않아 제거한다.

### 13.9 fake broker·장애주입·replay 시험

추가 mock framework는 쓰지 않는다. `tests/test_intraday_live.py` 안의 `ScriptedBroker` 하나가
append-only call log, queued normalized snapshots, client-ID별 일반/OCO map, operation별 queued fault를
가진다. adapter 계약시험은 기존 `FakeTransport`를 재사용한다.

fake outcome은 최소 다음을 지원한다.

- exact ACK와 deterministic 4xx reject
- broker 내부에는 생성됐지만 caller는 timeout을 받은 `accepted_then_timeout`
- 409, 429, 500, connection reset, malformed JSON, missing ID
- cancel/OCO accepted 뒤 응답 유실
- stale/malformed read snapshot과 cursor 반복
- 일반 create는 10분 안 same key/body가 same ID, same key/different body가 conflict, 10분 뒤 new order 가능
- 조건 create는 second call 자체를 test failure로 만들고 공식 확인 전 recovery 0을 강제

kill point는 reservation과 외부 부작용 사이를 모두 덮는다.

```text
after_reservation_before_http
after_broker_accept_before_persist
after_unknown_persist
after_partial_fill_before_cancel
after_entry_terminal_before_owned_persist
after_owned_persist_before_oco
after_oco_accept_before_persist
after_oco_watching_before_protected_persist
after_oco_cancel_before_exit
after_exit_accept_before_persist
after_closed_before_final_persist
```

각 kill point에서 SQLite connection을 실제로 닫고 새 runtime/writer fence로 reopen한다. 정상 실행과
모든 crash-index 실행의 최종 DB projection·fake broker call identity를 비교한다. 중복 frame도
반복 주입한다. thread sleep에 의존하지 않고 fake aware clock을 앞으로 이동한다.

시험 묶음은 다음을 모두 포함한다.

1. v1/v2/v3→v4 migration, DDL 중간 실패 rollback, 재실행, foreign key/quick check.
2. 모든 허용 state transition과 표에 없는 모든 직접 전이 거부.
3. 두 SQLite connection의 writer·ENTRY reservation 경합에서 승자·fake create 각 1개.
4. entry crossing/no-chase, stale quote/book, spread, 휴장·단축장·DST, force-exit 경계.
5. 일반 create ACK loss, 409/429/5xx, 8분 직전 recovery, 8분 이후 create 0.
6. partial fill 뒤 cancel 처리 중 추가 fill, terminal-with-fill, cumulative event replay.
7. OCO candidate 0/1/2, accepted-then-timeout에서 conditional recovery POST 0, 모든
   `WATCHING/PAUSED/ORDERING/ORDERED/COMPLETED/EXPIRED` group·leg 조합, triggered stop-limit 미체결.
8. OCO DELETE 응답 불명 때 MARKET SELL 0, exact 204 영속+post-ACK stable competition-cleared 뒤
   잔여 전량 SELL 1개, DELETE 경계에서 leg가 trigger되면 해당 일반 SELL만 추적.
9. 수동/타 전략 holding·order, fractional/초과 fill, sellable mismatch, stale OCO, unknown enum.
10. restart의 첫 broker action이 snapshot이며 그 전 signal 평가·mutation 0.
11. entry-disable/loss fuse 뒤 BUY 0이면서 cancel/protection/exit는 ownership 아래 계속 진행.
12. approval symlink/owner/group/mode/local-fs/schema/hash/generation/interaction replay/concurrent consume
    거부와 `after_link_before_temp_unlink` bounded recovery.
13. LLM 성공·timeout·악성 기사 전후 plan/run/intent byte-for-byte 불변.
14. 최소 100개 replay signal과 모든 crash-index에서 아래 공통 invariant 검증.

공통 invariant는 test helper 하나로 매 step 뒤 검사한다.

```text
logical ENTRY create <= 1
general-order identity recovery <= 1 and exact same client ID/body
conditional identity recovery = 0
local exit intent <= 1
confirmed cumulative SELL <= confirmed cumulative BUY
entry terminal 전 locally created SELL = 0
OCO competition-cleared proof가 없으면 separate local SELL = 0
PROTECTED => exact known WATCHING OCO group/leg fields and protected_qty == owned_qty > 0
CLOSED => owned/holding/sellable = 0, every known general order exact terminal,
          no active conditional group, no leg-created SELL
broker_sync_fence != writer_fence => signal evaluation = 0 and mutation = 0
stale writer response persisted = 0
```

### 13.10 실제 주문 호출 0의 기계적 증명

“fake를 썼다”는 설명만으로 합격시키지 않는다. 다음 다섯 겹을 자동 검사한다.

1. `operations.py`가 read-only 조건주문 adapter를 만드는 것은 허용하지만, 이번 범위에서
   `IntradayLiveRuntime`을 production dispatch하거나 adapter mutation method/delegate를 호출하는
   경로는 0이어야 한다. intraday + live 관련 flag 조합은 Toss client, Keychain, DB open보다 먼저
   실패한다.
2. `--shadow-service`는 13.1의 exact hard flags를 방금 reload한 config마다 강제하고 OAuth 외
   non-GET을 막는 read-only transport tripwire를 반드시 사용한다. config hot-swap 뒤에도 mutation
   delegate call은 0이다.
3. NL1–NL5 runner는 interpreter를 시작하기 전에 Toss/Discord/account 관련 환경변수를 allowlist 방식으로
   제거하고, OS sandbox/container/firewall 수준에서 loopback 외 outbound egress를 deny한다. 모든
   subprocess도 같은 scrubbed env와 network namespace/policy를 상속하며 정책을 풀 권한이 없다.
4. 그 위에서 autouse test guard가 `UrllibTossTransport.request`, `urllib` opener/default WebSocket
   connector, DNS와 외부 `socket.create_connection`을 호출 즉시 실패시킨다. loopback은 별도 표시된
   health test에만 허용한다. monkeypatch와 자체 counter는 보조 방어이지 실제 호출 0의 단독 증거가
   아니다.
5. fake transport URL은 IANA 비라우팅 `.invalid`만 허용하고 suite 종료 시
   `real_http_calls == 0`, `real_ws_connections == 0`을 assert한다. fake adapter POST는 별도
   `fake_mutation_calls`로 집계해 state-machine 기대값과 비교한다.

tripwire focused test는 method/path matrix뿐 아니라 wrong scheme/host/port, user-info, query/fragment,
encoded slash/dot/backslash, OAuth form의 누락·추가 key, config hot-swap과 301/302/303/307/308을 모두
거부한다. 실제 no-redirect opener가 redirect target을 호출하지 않았고 delegate call count가 0인지
확인한다. fake origin 허용은 production config가 아니라 dependency-injected test transport 안에만 있다.

이미 별도 사용자 승인으로 수행된 Discord synthetic button/modal E2E 기록은 보존하지만 자동 suite가
외부 Discord를 호출했다는 뜻은 아니다. 이번 gate는 recorded/fake Discord로 재현하고 외부 E2E를
자동 반복하지 않는다.

정적 gate도 함께 둔다.

- checked-in YAML의 네 hard-off 값과 빈 `live.allowed_symbols`
- checked-in launchd template에 intraday live runtime·receipt consumer·주문 자격증명 0
- simulation import path에서 default HTTP transport 생성 0
- `config/local.yaml`, 운영 DB, Keychain을 여는 test 0
- dashboard/gateway/health action entrypoint가 simulation release manifest에 0
- order 관련 production endpoint string은 adapter 계약 모듈 외 새 파일에 중복 0

Mac read-only shadow가 끝난 뒤에는 private DB를 읽기 전용으로 열어 `quick_check=ok`와 intraday
simulation 전용 DB의 broker/order/event row count를 확인한다. 이 DB count만으로 외부 call 0을
증명하지 않고, transport tripwire audit와 process manifest를 함께 증거로 남긴다. private path,
account data, raw response는 보고서에 넣지 않는다.

### 13.11 Mac 비실거래 배포 topology

Mac에는 아래 다섯 job만 허용한다. live writer와 receipt consumer는 구현 파일이 있어도 설치하지
않는다.

| job | 읽기 | 쓰기/외부 호출 | 가지면 안 되는 것 |
| --- | --- | --- | --- |
| intraday shadow planner | owner-only config, Toss commission/public read API, shadow DB | plan/context/envelope/outbox/heartbeat | order mutation transport |
| selected-symbol stream | redacted context, OAuth, immutable plan 조회용 intraday state DB | 한 symbol market WS, snapshot/heartbeat, 별도 paper journal/ledger DB | accountSeq, personal topic, order adapter |
| Discord approval shadow | redacted envelope, bot Keychain | one-shot shadow receipt | Toss secret, receipt consumer |
| news one-shot | selected context, news/LLM secret | own dedupe DB, 한 channel 알림 | Toss secret, trading DB |
| shadow watchdog | redacted heartbeat, launchd state | 상태변경 JSON stdout | Toss/approval/news secret, trading DB, order code, network sender |

planner/news/watchdog용 argument-free wrapper와 plist를 추가하고 기존 approval/stream wrapper도 같은
기준으로 맞춘다. 모든 wrapper는 `zsh -f`, zero args, `env -i` allowlist, `umask 077`,
`ulimit -c 0`, `python -I`, exact-SHA import 확인, secret capture 직후 Python env pop을 지킨다. core
limit은 Keychain을 읽기 전 outer wrapper에서 0으로 설정하고 Python도 시작 즉시 `RLIMIT_CORE=0`을
확인/재설정해 child에 상속한다. source·venv는 exact SHA 이름의 root-owned read-only directory이며
모든 ancestor도 runtime UID가 rename/replace할 수 없어야 한다. root-owned directory는 0755,
wrapper/executable은 0555 또는 0755, source는 0444/0555로 runtime이 traverse/execute할 수 있게 한다;
root-owned wrapper에 0700을 쓰지 않는다. config·DB·log·mailbox는 release 밖의 명시적 owner/group/mode
경로다. plist는 mutable checkout이나 `/current` symlink를 가리키지 않는다.

dependency는 lockfile의 exact version+hash와 offline wheelhouse manifest hash로 고정하고 clean Mac
release가 network 없이 설치되는지 검증한다. editable install, 범위 version만 적힌 `pyproject.toml`,
사용자 site-packages가 하나라도 개입하면 exact-SHA gate는 실패다.

simulation release에는 dashboard, multi-user gateway, Docker dashboard, generic bot plist,
`tailscale serve`, health mutation action을 포함하지 않는다. Tailscale SSH는 상태 확인과 maintenance
전용이고 SSH session이 끊겨도 launchd job은 지속해야 한다. private host address나 account/channel
identifier는 release manifest·로그·개발일지에 기록하지 않는다.

planner·stream·approval·news 네 wrapper는 component별 directory의 `heartbeat.json`을 mode 0600으로
atomic publish하고, exact-five의 다섯 번째 watchdog은 네 heartbeat와 launchd 상태를 평가한다.
별도 watchdog UID/group-readable heartbeat topology는 아직 Mac permission gate를 통과하지 않았으므로
현재 template만으로 주장하지 않는다. 다음 redacted field만 허용한다.

```text
schema_version, release_sha, boot_id_hash, component, launchd_label,
mode=shadow, live_order_submission=false, updated_at,
status_code, stream_ack_ok, baseline_fresh, db_quick_check=ok|fail|not_applicable
```

watchdog은 heartbeat stale/schema/hard-off, launchd process state, stream ACK/baseline, planner DB
quick-check 상태와 recovery를 평가해 변화가 있을 때만 redacted JSON을 stdout에 쓴다. planner는 locked
state DB sibling의 owner-private `stream-expectation.json`을 context export보다 먼저 atomic write한다.
watchdog은 `TOSS_WATCHDOG_CONTEXT_PATH`와 `TOSS_WATCHDOG_EXPECTATION_PATH`로 같은 private directory의
expectation/context를 strict parse한다. active expectation과 missing/deleted/idle/invalid context는
`STREAM_CONTEXT_INVALID`, active context와 absent/expired expectation 또는 malformed expectation은
`STREAM_EXPECTATION_INVALID`다. 둘이 fresh current-session active일 때만 stream
process·heartbeat·ACK·baseline이 필수이고, pre-plan 부재 또는 둘 다 정상 만료된 뒤에는 loaded
WatchPaths job의 stopped 상태를 healthy idle로 본다. 현재 watchdog은
Discord나 다른 network sender를 갖지 않는다. DB `quick_check`는 planner가 자기 DB를 읽어 heartbeat에
결과만 쓰며 watchdog은 DB를 열지 않는다. trade/news/approval sender는 **각 POST 직전** 원격 webhook/channel metadata와
guild를 다시 조회해 설정의 같은 단일 channel인지 확인하며 첫 성공을 영구 cache하지 않는다. 한
news run에서 여러 기사를 보내도 매 기사마다 반복한다. mention은 계속 차단한다. bot/webhook에는
Manage 권한을 주지 않지만 metadata GET과 POST 사이 guild admin이 channel/webhook을 바꾸는 원격
TOCTOU는 로컬 코드가 제거할 수 없는 trust boundary로 기록하고, mismatch 발견 즉시 이후 전송을
중단한다.

별도 macOS trader/approver/watchdog UID와 cross-UID mailbox/heartbeat는 dummy secret과 synthetic
receipt로 시험한다. 각 login Keychain 복구와 mailbox negative access를 검증하되 실제 Toss order
credential은 trader simulation job에도 넣지 않는다. FileVault는 끄지 않으며 reboot 뒤 필요한
사용자 로그인 전에는 shadow job이 정상 복구되지 않을 수 있는 RTO를 측정한다. 외부 deadman은 별도
노드에서 Mac reachability만 보고 같은 channel에 알리며 주문 권한이 없다.

NL5 release gate는 최소 다음을 확인한다.

```text
exact committed SHA and clean tree
root-owned/non-writable source, venv, every ancestor; traversable wrapper mode
hash-locked offline wheelhouse install, manifest hash, and pip check
python -I exact import
full pytest, compileall, git diff --check
plist lint, zsh syntax, wrapper/child RLIMIT_CORE=0
only five allowlisted launchd jobs
no dashboard/gateway/live writer/consumer process or listener
hard-off initial/reload config, malicious-origin/no-redirect tripwire
dummy UID/mailbox/hardlink-crash/restart/watchdog heartbeat permission tests
Discord metadata resolver-per-send test with cached result forbidden
```

### 13.12 완료 판정표

다음은 모두 자동 증거가 있어야 하며 하나라도 실패하면 이번 비실거래 구현도 미완료다.

| 영역 | 합격 기준 |
| --- | --- |
| schema | v1/v2/v3 복제본 보존, v4 migration/재실행/rollback fixture, quick/foreign-key check 통과 |
| concurrency | 두 writer·두 receipt consumer 경합에서 승자 1, stale fence write 0 |
| entry | 모든 replay/crash에서 logical BUY 최대 1, stale·세션 밖·no-chase BUY 0 |
| UNKNOWN | 일반 create recovery 최대 1/exact body-key/8분 뒤 POST 0, 조건 create recovery 0, cancel 재호출 0 |
| ownership | 미등록 holding/order 채택 0, 누적 SELL이 confirmed BUY를 넘는 경우 0 |
| protection | owned를 알기 전 OCO 0, `PROTECTED` false-positive 0, SLO 초과 무알림 0 |
| exit | OCO competition-cleared/경쟁 SELL exact terminal 전 separate SELL 0, 허용 시 잔여 전량 intent 정확히 1 |
| restart | 모든 상태에서 첫 동작 snapshot, broker 대조 전 signal/mutation 0 |
| approval | wrong identity/channel/hash/generation/file metadata/receipt hash/interaction ID replay consume 0; nlink crash recovery bounded |
| isolation | 뉴스·LLM·watchdog가 plan/run/intent를 변경한 경우 0 |
| network | process-level egress deny 아래 실제 Toss order HTTP/실계좌 personal WS/자동 suite Discord interaction 0 |
| release | live plist·receipt consumer·dashboard/gateway 0, hard-off reload/no-redirect tripwire와 UID/heartbeat/core/dependency gates 통과 |
| regression | focused와 전체 pytest, compileall, pip check, diff check 전부 성공 |

완료 보고서에는 exact git SHA, dependency manifest hash, test count, fixture count, mutation tripwire 결과,
migration schema version, Mac release manifest hash만 넣는다. secret, private ID, account/holding/order,
raw log·screenshot은 넣지 않는다.

이 단계가 끝나도 다음은 의도적으로 미완료다.

- 실제 Toss 일반·조건 주문의 end-to-end 동작과 실계좌 recovery
- 실제 OCO 보호 공백·stop-limit·MARKET exit의 체결 특성
- 실제 account personal WS와 REST 사이의 장중 gap
- authoritative halt/LULD provider 선정·장애 특성
- 실제 계좌 배타성 위반 race와 수동 개입 절차
- 실제 돈 기준 SLO·현금 fraction·risk 값의 승인
- 1주 pilot, 반복 pilot, 수량 확대

현재 checkout의 결과 label은 정확히 `NON_LIVE_CORE_IMPLEMENTED / LIVE_NO_GO`다. 위 완료표 중
SLO 초과 durable alert, triggered stop-limit 취소→경쟁 제거→전량 MARKET escalation, 11개 kill
point와 최소 100개 replay, 실제 v1/v2/v3 복제 fixture, Mac OS-level no-egress·dummy Keychain·UID,
heartbeat cross-UID permission/Discord watchdog, dependency lock/wheelhouse 및 설치된 exact-five manifest 증거가
남아 있다. 이들이 모두 자동 증거로 닫힌 뒤에만 `NON_LIVE_IMPLEMENTATION_COMPLETE`를 검토한다. 이를
`LIVE_READY`, `PILOT_READY`, `SAFE_TO_TRADE`로 바꾸는 자동 규칙은 만들지 않는다. 이후 live 시험은
사용자가 별도로 요청하고 P0를 다시 승인하기 전까지 영구적으로 범위 밖이다.

## 14. 한 달 실제 시세 forward simulation 계약

### 14.1 기간·자본·권한

관측 기간은 `America/New_York` session date 기준 `2026-08-31`부터 `2026-09-30`까지 양 끝을
포함한다. `run_id`와 두 날짜, 초기 cash, slippage·freshness 설정은 config hash로 첫 DB 생성 때
잠기며 같은 `run_id`로 바꿀 수 없다. 별도 experiment SHA-256은 strategy kind, 관련 runtime 설정,
모든 단타 경제·위험·선정·시간 입력, simulation ID/기간/초기 cash/slippage와 resolved absolute
`news_context_path`를 canonical JSON으로 묶는다. account identity와 다른 filesystem path는
의도적으로 제외한다. wrapper는 expected run ID, 두
날짜, paper DB와 experiment hash를 시작 전과 planner config reload 때 다시 비교한다.

planner용 private manifest와 stream용 account-free manifest는 서로 다른 directory에 같은 basename으로
둔다. stream manifest의 account alias/sequence는 비어 있지만 두 manifest의 hash 대상 값은 같고,
동일한 plan DB·paper DB·resolved absolute context 경로를 가리켜야 한다. simulation config는 이
context가 absolute `news-context.json`으로 resolve되지 않으면 거부한다. planner는 당일 market calendar를 조회해
기간 안의 평일마다 immutable plan, idempotent `MARKET_CLOSED`, 또는 마지막 유효 자동선정 반복의
`NO_CANDIDATE` row를 기록한다. 조기 후보 0건은 재시도하고 daily/premarket candle 부족·stale·future
및 quote/orderbook data-quality 실패는 coverage를 만들지 않는다. 날짜 범위·
오름차순·하루 한 plan은 DB 등록 시 강제한다. 초기 자본 기본값은 simulation-only `USD 10,000`이며
확정된 entry/exit cash event만 다음 plan의 available cash를 바꾼다.

planner config에는 account sequence가 있으므로 wrapper는 Keychain 접근 전에 `lstat`으로 runtime UID
소유 non-symlink regular file인지, group/other permission bit가 0인지 검사한다. installed plist는 실제
sequence 대신 별도 64-hex `TOSS_SHADOW_ACCOUNT_FINGERPRINT`를 pin한다. planner는 config hot reload마다
account sequence fingerprint를 다시 계산해 같은지 확인한다. 실제 sequence와 fingerprint는 stream
manifest/environment, Git, 공개 report에 넣지 않는다.

calendar의 `preMarket`와 `regularMarket` key는 둘 다 명시적으로 존재해야 한다. 둘 다 null인 경우만
holiday이며 `MARKET_CLOSED`를 기록한다. key 누락은 `intraday_calendar_malformed`, 한쪽만 null이면
`intraday_required_session_unavailable`로 차단하고 holiday coverage를 만들지 않는다.

가상 원장은 별도 SQLite의 `paper_runs.current_cash`, `paper_cash_ledger`의 `INITIAL/ENTRY/EXIT`,
plan별 quantity·entry/exit 가격·fee·realized P&L로 구성된다. reserved cash, 별도 position lot,
unrealized P&L, FX와 세금 원장은 현재 없다. selector와 planner 수량은 가상 current cash와 기존
cash allocation/risk 규칙만 사용한다. `SimulationReadOnlyTossTransport`는 holdings, buying power,
account/order history, personal WebSocket과 모든 mutation을 차단한다. account header는 commission
schedule GET에만 허용하고 public market GET에는 거부한다. stream은 account sequence를 받지 않는다.
조회된 broker commission fraction은 immutable plan snapshot에 고정한다.

공용 planner는 redacted shadow approval envelope를 계속 만들 수 있지만 paper engine은 Discord
receipt를 기다리거나 소비하지 않는다. selector, risk/cash 비율과 entry/target/stop/force-exit
계산만 deterministic plan으로 잠근다. 어떤 paper 결과도 live flag, order adapter 또는 receipt
consumer를 활성화할 수 없다.

### 14.2 실제 데이터 journal과 유효성

자동 selector가 그 session에 잠근 정확한 한 symbol만 `trade:us`와 `orderbook:us`로 구독한다.
strict WebSocket parser가 받아들인 뒤 sink의 schema/source/USD/freshness/시간 역행 검사를 통과한
개별 frame은 `market_frames`에 event kind, broker event time, receive time과 trade 가격·수량 또는
top-of-book 가격·수량으로 저장한다. warm-up 중 `shadow_usable=false`여도 해당 개별 source frame이
유효하면 저장한다. trade와 book이 모두 fresh하고 usable인 payload만 generation, 두 timestamp,
top-of-book과 처리 action을 포함한 `market_observations`가 되어 체결 평가에 들어간다.

malformed/wrong-topic/stale/regressed frame 자체나 parser exception 원문은 journal row로 저장하지
않는다. 연결이 끊기면 pending batch를 먼저 flush하고 entry/open 민감 구간에 data-gap row를 남긴다.
이 가격은 clean fill에 쓰지 않는다. secret, account ID, private host/channel identifier와 OAuth 값도
journal·report에 없다.

DB는 SQLite WAL과 `synchronous=FULL`을 강제한다. frame은 메모리 queue 순서를 보존하며 128번째
frame에서 자동 flush하고, stream sink는 monotonic 0.25초 기준을 넘긴 다음 periodic receive-loop
tick에서도 flush한다. 기본 idle receive poll은 1초이며 disconnect·정상 close도 명시적으로 flush한다.
따라서 commit 사이 pending tail은 최대 127개이고 SIGKILL·전원 장애에서는 그 frame 내용이 유실될
수 있다.

stream은 context 검증 뒤 OAuth/socket 작업보다 먼저 `start`를 sink로 보낸다. sink는 이때
`paper_stream_instances` marker를 commit하고, `last_seen_at`을 최대 초당 한 번 갱신하며 정상 close의
pending-tail flush 뒤에만 marker를 닫는다. planner finalize는 모든 instance의
`started_at`~`ended_at 또는 last_seen_at`이 entry-expiry/force-exit 경계를 덮는지 검사한다. 경계 전에
시작한 최신 open marker가 quote TTL 안에서 fresh하면 확정을 미룬다. TTL을 넘기면 모든 orphan
marker를 `stream_liveness_expired`로 닫는다. 경계를 덮은 instance가 없으면
`stream_coverage_incomplete`, 경계 coverage가 있지만 open process가 그 뒤 liveness를 잃었으면
`stream_process_interrupted` gap을 기록한 다음에만 확정한다. 새 stream 시작은 이전 orphan을
`superseded_by_stream_restart`로 닫는다. 기존 journal이 있는 민감 구간 재시작은 별도로
`stream_process_restarted`, entry 시작 뒤 quote TTL보다 늦은 최초 frame은 `stream_started_late`다.

verified REST baseline 뒤 ACK된 선택 종목 trade/orderbook topic 각각은 quote TTL 안에 현재 generation의
fresh event를 내야 한다. 실패하면 `trade_topic_silent` 또는 `orderbook_topic_silent` gap으로 민감
구간 day를 invalid 처리하고 socket을 disconnect/reconnect한다. 이 검출은 유실된 frame 내용을
복원하지 않으며 journal은 실제 선택 종목 수신 event 표본이지 gap-free consolidated tape가 아니다.

### 14.3 causal 가상 체결 규칙

한 session의 신규 BUY는 최대 하나이고 재진입하지 않는다. 진입·target·stop trigger는 실제 수신한
trade frame에만 반응한다. orderbook/REST baseline에 carry-forward된 과거 trade 값은 trigger가 아니고,
broker trade timestamp가 entry start 또는 virtual entry 시각 이전이면 거부한다. signal을 발생시킨
row에서 즉시 체결하거나 나중에 본 가격을 과거에 대입하지 않는다. trigger observation ID를 저장한
뒤 그와 다른 book hash를 가진 이후 accepted orderbook만 fill 후보로 삼는다.

1. BUY는 best ask와 설정한 adverse slippage를 더한 가격이 entry limit 이하이고 표시 ask depth가
   전체 정수 수량 이상일 때만 전량 체결한다.
2. target SELL은 trigger 뒤의 best bid에서 adverse slippage를 뺀 가격이 target limit 이상이고
   표시 bid depth가 보유 전량 이상일 때만 전량 체결한다.
3. stop-limit SELL도 stop trigger 뒤의 다음 유효 book을 사용한다. slippage 반영 bid가 stop limit
   아래이거나 depth가 부족하면 체결을 만들지 않고 force-exit 경로까지 미체결로 유지한다.
4. force-exit 시각 뒤 stream 경로는 다음 accepted orderbook을 사용한다. 60초 planner의
   `finalize_session` 경로는 이미 journal에 있는 latest observation이 quote TTL 안이면 그 fresh
   book도 사용할 수 있다. 두 경로 모두 limit은 무시하지만 full bid depth와 adverse slippage는
   유지한다. 진입한 가상 포지션을 정규장 종료에도 닫지 못하면 임의 last/close를 쓰지 않고
   `UNRESOLVED`로 봉인해 final equity/return을 미확정으로 두고 후속 plan을 차단한다. 무진입이나
   coverage 누락은 `UNRESOLVED`가 아니다.

부분체결을 낙관적으로 합성하지 않는다. 표시 top depth가 전량에 부족하면 해당 book에서는 no-fill로
남기며 다음 유효 book을 기다린다. slippage는 BUY에서 `ask * (1+s)`를 tick 올림, SELL에서
`bid * (1-s)`를 tick 내림한다. fee는 제공된 active broker commission이 있으면 그것을, 없으면
immutable plan commission snapshot을 leg별 notional에 적용하고 configured fixed round-trip cost의
절반을 각 leg에 더한다. 별도 SEC/regulatory/minimum fee 규칙은 구현하지 않았다. planning용
round-trip buffer를 fee에 한 번 더 더하지 않으며 slippage drag도 별도 통계로 계산하지 않는다.

disconnect/restart gap이 entry 전이면 plan을 즉시 `INVALID`로 만들고 진입하지 않는다. OPEN에서
gap이 생기면 다음 accepted orderbook의 full bid depth로 limit 없이 전량 exit하고, 실제 계산된
cash/P&L은 보존하되 clean metric에서는 제외한다. 해당 exit도 정규장 종료까지 불가능하면
그 진입된 가상 포지션은 `UNRESOLVED`다. 모든 돈 계산은 `Decimal`이고 cash가 음수가 되거나 plan
전체 수량을 funding하지 못하면 transaction을 실패시킨다.

### 14.4 일일·월간 결과

내부 daily summary의 현재 field는 session/symbol/status/quantity, entry/exit time·price·fee·reason,
gross/net realized P&L, cash before/after, clean metric 포함 여부, accepted observation·journaled frame·
data-gap count, first/last event time과 entry/exit fee source다. `CLOSED`와 `NO_ENTRY`만 clean으로
포함하고 `INVALID`, `OPEN`, `UNRESOLVED`는 제외한다. MAE/MFE, 보유 시간, daily return, exposure,
uptime/reconnect/freshness percentile과 별도 slippage drag field는 없다.

자동 선정에서 계획 마감 전의 후보 0건은 확정하지 않고 다음 planner 반복에서 다시 평가한다. 다음
반복이 마감 뒤가 되는 마지막 유효 시도에서도 모든 정상 전략 threshold를 통과한 후보가 없을 때만
그 날짜를 immutable `NO_CANDIDATE`로 기록한다. daily/premarket candle 부족·stale·future 또는
quote/orderbook data-quality 실패는 다른 후보 검사를 계속하되, 끝까지 plan이 없으면 첫 구체 오류를
다시 발생시키며 coverage를 만들지 않는다. 평균 거래대금·변동폭·거래량 같은 정상 threshold 탈락은
`NO_CANDIDATE` 판단에 포함한다.

내부 month summary는 initial/current/final cash, realized/clean P&L과 return, cash 및 closed-equity
drawdown, total fee, plan/clean trade/invalid/unresolved/no-entry 수, wins/losses, win rate, average
win/loss, profit factor, expectancy, exit-reason count, accepted observation/journaled frame/data-gap 수를
계산한다. coverage에는 기간의 모든 평일 expected/covered/missing 목록과 수, planned 목록,
`MARKET_CLOSED` 및 `NO_CANDIDATE` 목록이 포함되고 month summary는 no-candidate count를 별도로
계산한다. 선택 종목 분포, exposure, journal coverage percentage와
reconnect/stale 통계는 계산하지 않는다. 미청산 `OPEN` 또는 `UNRESOLVED`가 있으면 final equity와
return은 null이다.

Discord daily payload는 status, quantity, entry/exit price·time·reason, gross/net P&L, 합산 fee,
cash before/after, accepted observation·journal frame·gap count, first/last event, fee source와 clean 포함
여부를 공개한다. end date 뒤 status-keyed run payload는 status, initial/current cash, final equity,
realized/clean P&L·return, trade/win/loss·win rate·average win/loss·expectancy·profit factor, fee/MDD,
exit reason, no-entry·no-candidate·invalid·unresolved·waiting,
expected/covered/missing/market-closed/no-candidate coverage,
journal/gap 수와 fee/journal policy를 공개한다. Discord 표시 문자열은 이 payload에서 핵심 수치만
축약한다. `COMPLETE`가 아닌 run report는 warn level이다. fill/invalid alert와 두 report는 simulation
DB outbox에서 main notification outbox로 idempotent하게 전달되며 Discord 실패 시 main outbox가
재시도한다. 이 과정은 journal/ledger commit을 rollback하거나 fill을 다시 만들지 않는다. news
article 알림 계약은 별개다.

month status는 구체적인 미종료/오류 상태를 우선한다: `UNRESOLVED`, `OPEN`, `WAITING`, `INVALID`,
`BLOCKED`, 기간 중 `ACTIVE` 순이다. 기간 종료 뒤 plan, `MARKET_CLOSED`, `NO_CANDIDATE`로 덮이지
않은 평일이 있거나 실제 plan이 하나도 없으면 `INCOMPLETE`다. 모든 coverage가 있고 최소 한 plan이 있으며 위
상태가 없을 때만 `COMPLETE`다.

### 14.5 합격 gate와 배포 상태

현재 로컬 구현은 아래 계약을 자동 검증했다. Mac에서 계측을 시작하려면 같은 계약의 exact-SHA
smoke와 실제 public feed 확인을 추가로 통과해야 한다.

| 영역 | 합격 기준 |
| --- | --- |
| authority | simulation sizing의 holdings/buying power/account-order read 0, personal topic·mutation 0; account header는 commission GET에만 있고 public GET에는 0 |
| causality | trigger와 같은 observation/book hash fill 0; subsequent changed orderbook과 replay idempotency 검증 |
| depth/cost | 전량 visible top depth, limit, adverse slippage, plan commission+fixed half-cost를 만족하지 않은 fill 0 |
| ledger | immutable run config+experiment hash, `INITIAL/ENTRY/EXIT` unique cash event, negative cash·중복 fill 0 |
| daily limit | session당 신규 BUY 최대 1, 재진입 0, 정상 day의 종료 position 0 |
| invalid data | entry 전 gap은 INVALID, OPEN gap exit는 clean 제외, close 미해결은 UNRESOLVED·후속 plan 차단, fabricated close 0 |
| durability | SQLite WAL/FULL, periodic tick·128-frame flush·127-frame 최대 tail, disconnect flush, boundary coverage·TTL liveness/orphan finalization, restart·topic-silence gap과 replay 검증 |
| reports | daily/month internal summary, coverage·INCOMPLETE와 축약 durable notification, Discord 재시도 시 fill/ledger 중복 0 |
| period | inclusive date validation과 날짜 오름차순 plan; 평일별 plan·`MARKET_CLOSED`·최종 유효 시도의 `NO_CANDIDATE`, data-quality coverage 0, missing/zero-plan은 `INCOMPLETE`, 과거 합성 0 |
| release | planner/account-free stream 별도 manifest와 동일 experiment lock, 기존 exact-five 유지, simulator용 여섯 번째 daemon 0, live writer/consumer/dashboard 0 |

Mac의 기존 다섯 job 중 planner가 virtual cash sizing·plan·daily/month report를 맡고 selected-symbol
stream이 validated journal·fill evaluator·USD ledger를 맡는다. 승인 recorder/news/watchdog의 권한은
변하지 않으며 paper engine은 승인 receipt를 소비하지 않는다. 즉 simulator를 기존 두 process에
내장하고 여섯 번째 service를 만들지 않는다. planner·stream·approval·news는 redacted heartbeat를
publish하고 다섯 번째 watchdog은 네 heartbeat와 launchd 상태를 평가한다. 이 wiring은 checked-in
구현이며 Mac 설치·권한·지속 가동 증거는 아니다. stream plist는 `news-context.json` `WatchPaths`만
사용하고 `RunAtLoad`·`StartInterval`·`KeepAlive`가 없으므로 plan 전에는 idle이다. planner는 locked
state DB sibling `stream-expectation.json`을 context보다 먼저 쓰고, regular close 전 context
생성·갱신만 실행/재실행 trigger로 만든다. close 이후에는 context를 다시 쓰지 않는다. watchdog은
두 path를 받아 active expectation과 context가 일치할 때만 stream을 required로 평가하며 정상
post-close idle은 healthy로 처리한다. watchdog의 Discord sender는
아직 연결되지 않았다.

owner-private `/현황` artifact는 schema version 2이며 no-candidate count와 최신 covered day를
공개한다. 최신 날짜가 `NO_CANDIDATE` 또는 `MARKET_CLOSED`여도 해당 daily summary가 표시된다.
최종 `NO_CANDIDATE`는 durable runtime event와 `/현황`에는 남지만 별도 Discord 알림 outbox에는
아직 넣지 않았다.

현재 상태는 정확히 **LOCAL_IMPLEMENTATION_VERIFIED / MAC_RELEASE_STAGED_AUTH_BLOCKED / LIVE_NO_GO**다.
Mac에는 non-live gate를 통과한 수정 release가 side-by-side로 준비됐지만, 기존 Toss OAuth client가
`401 invalid_client`로 거부돼 planner를 plan 0건 상태에서 내렸다. 새 자격증명을 비노출 one-shot으로
검증한 뒤에만 세 installed job을 같은 SHA로 전환한다. 실제 public Toss WS baseline·journal smoke와
redacted Discord daily/final smoke가 끝나기 전에는 정상 run을 시작했다고 주장하지 않는다. 시작이 늦으면
과거 누락 frame, plan, 휴장 또는 fill을 합성하지 않고 실제 plan이 생긴 날짜부터
`2026-09-30`까지만 관측한다. 누락 평일은 coverage의 missing 목록에 남고 종료 상태는
`INCOMPLETE`다. 이 결과만으로 live 승격하지 않는다.

### 14.6 다음 실험의 2레인 병렬 가상체결

현재 단일 레인 run의 기간·초기 현금·experiment hash는 중간에 바꾸지 않는다. 2레인은 새 실험에서만
고정 `A/B` 두 개로 시작하며 총 가상현금은 최초 한 번 `50:50`으로 나눈다. 레인 간 이체·재조정은
금지하고 각 레인의 allocation/risk 규칙을 자기 현금에 적용한다. 같은 날 두 레인은 반드시 서로 다른
symbol이어야 하며, 한 종목만 적격이면 다른 레인의 현금을 빌리지 않고 나머지를
`NO_CANDIDATE`로 남긴다.

selector는 한 ranking snapshot에서 상위 두 적격 종목을 고르고 두 plan과 날짜별 cohort manifest를
한 SQLite transaction으로 잠근다. manifest는 각 레인의 `PLAN`, `NO_CANDIDATE`,
`MARKET_CLOSED`와 plan identity를 보유하며 `symbol_a != symbol_b`를 DB 제약으로 강제한다. A만 저장된
반쪽 cohort는 허용하지 않는다. data-quality 실패는 해당 레인의 coverage를 만들지 않으며, cohort
coverage는 두 레인이 모두 확정됐을 때만 완료다. 현재 paper schema의 하루 한 plan 제약을 우회하려고
한 run에 두 plan을 넣지 않고 `cohort-...-a`, `cohort-...-b` 두 sub-run과 별도 cash ledger를 사용한다.

stream은 새 daemon이나 두 번째 socket을 추가하지 않는다. 한 process·한 WebSocket에서 두 종목의
`trade:us`와 `orderbook:us`, 총 네 topic을 exact ACK한 뒤 immutable `topic -> lane` 표로 한 frame을
정확히 한 레인에만 전달한다. OAuth·연결·ACK·process·paper DB write 실패는 cohort 공통 장애이고,
식별 가능한 한 symbol의 stale/malformed/silence는 그 레인만 invalid 처리한다. reconnect generation과
REST baseline, queue, stream instance identity는 레인별로 분리한다.

월 보고서는 A, B, 합산 portfolio를 따로 표시하고 총 거래 수와 distinct trading session 수를 함께
낸다. 같은 날의 두 거래는 같은 market regime와 ranking snapshot을 공유하므로 독립 표본 두 개로
간주하지 않는다. 첫 구현 범위는 고정 2레인·50:50·단일 socket뿐이며 동적 N레인, 성과 기반 현금 이동,
다중 socket은 실제 병목 증거가 생기기 전에는 추가하지 않는다.

## 공식 참고

- [Toss Open API 가이드](https://developers.tossinvest.com/docs)
- [Toss REST OpenAPI](https://openapi.tossinvest.com/openapi-docs/latest/openapi.json)
- [Toss WebSocket AsyncAPI](https://openapi.tossinvest.com/openapi-docs/latest/asyncapi.json)
- [Toss 연동 개요와 rate limit](https://openapi.tossinvest.com/openapi-docs/overview.md)
- [websockets 동기 client와 keepalive 설정](https://websockets.readthedocs.io/en/stable/reference/sync/client.html)
- [FINRA LULD Plan](https://www.finra.org/filing-reporting/trf/limit-uplimit-down-luld-plan)
