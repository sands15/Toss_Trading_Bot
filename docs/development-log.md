# 개발일지

## 2026-08-30 — 단타 non-live core 구현·안전 경계 재감사

결과 표시는 **`NON_LIVE_CORE_IMPLEMENTED / LIVE_NO_GO`**로 고정했다. production CLI와 다섯 개
shadow LaunchAgent 템플릿은 주문 lifecycle을 생성하지 않으며, 이번 검증에서 Toss mutation,
실계좌 personal WebSocket, Discord 실전송, SSH·Mac 배포를 실행하지 않았다.

- `intraday_live.py`에 dependency-injected 상태기를 구현했다. 시작·재연결은 stable REST
  A→fresh exact ACK→REST B를 먼저 요구하고, SQLite writer lease/fence/version과 immutable
  request hash를 socket 직전에 다시 확인한다. 일반 create UNKNOWN recovery는 bounded one-shot,
  OCO create/cancel과 entry cancel은 reservation one-shot이다.
- 누적 fill 기반 owned 수량, partial-fill 뒤 잔량 cancel, exact broker order ID/client ID,
  OCO parent/leg economics와 triggered SELL 수량을 검사한다. 보호주문 만료 시 경쟁 SELL과 소유
  수량이 정확하고 정규장 주문 가능 상태일 때만 당일 소유 잔량 전체 emergency exit를 예약한다.
- SQLite schema v5에 승인 만료를 추가했다. v2 consumer는 immutable plan의 수량·가격·위험·시간,
  plan/envelope/receipt hash, Discord identity, boot/fence/generation, expiry와 disable/loss latch를
  검증한 뒤 `BEGIN IMMEDIATE` 안에서 한 번만 소비한다. READY/ENTRY reserve/send gate도 승인 fence,
  만료와 latch를 반복 확인한다.
- action event는 state→run pointer→role/side 행렬을 reservation과 send gate에서 확인한다. legacy
  event API로 intraday cancel ACK를 위조할 수 없고, ACK는 prior reservation·현재 fence·root broker
  ID를 원자 검증한다. stale writer는 예약 뒤에도 외부 cancel/delete를 호출할 수 없다.
- 일반·조건 주문 파서는 필수 ID·상태·누적 fill을 strict 처리하며, structured idempotency conflict는
  human message와 무관하게 보존한다. `confirmHighValueOrder=true` adapter는 runtime 생성 단계에서
  거부한다. trade/news Discord sender는 매 POST 직전에 대상 channel metadata를 다시 확인한다.
- macOS용 exact-five shadow plist/wrapper, standalone watchdog evaluator와 OS-level no-egress를
  요구하는 Mac gate가 추가됐다. 다만 이는 설치·가동 증거가 아니라 checked-in template이다.
- exact EXPIRED OCO는 같은 tick에서 계좌를 다시 대조한 뒤 전량 emergency exit를 한 번만
  예약한다. 그 POST 결과가 UNKNOWN이어도 같은 terminal parent의 ID·경제값·취소된 두 leg와
  시장·halt·보유·매도가능·경쟁 SELL 부재를 다시 확인한 경우에만 같은 idempotency key로 bounded
  recovery 한 번을 허용한다. 살아 있는 ENTRY와 EXIT/OCO가 공존하면 종료·보호로 오판하지 않고
  `RECOVERY_REQUIRED`로 격리한다.

Windows 비실거래 검증은 전체 **`693 passed, 3 skipped`**, 환경을 지운 PowerShell no-live gate는
**`456 passed, 2 skipped`**였다. `compileall`, `pip check`, `git diff --check`도 통과했다. skip 3개 중
2개는 Windows가 지원하지 않는 fd-relative `openat` 시험이고, 나머지 환경 계약 시험은 독립 gate
runner에서 실행됐다. 구현된 fake-adapter/non-live 범위의 최종 read-only 안전성 재감사에서는 새
concrete P0/P1을 찾지 못했다. Mac 전용 OS egress gate와 실제 설치 검사는 아직 실행하지 않았다.

남은 release blocker는 protection/exit SLO를 소비하는 durable urgent outbox와 triggered stop-limit
cancel→competition-cleared→전량 MARKET escalation, 모든 11개 kill point와 최소 100개 replay,
실제 v1/v2/v3 DB 복제 fixture, watchdog Discord sender,
hash-pinned lock/offline wheelhouse, clean exact-SHA Mac에서 zsh/plutil/dummy-Keychain/UID/no-egress 및
설치된 process/listener 감사다. authoritative 미국 halt/LULD source와 외부 deadman도 정해지지 않았다.
따라서 live writer 연결, arm, pilot은 계속 금지한다. 상세 근거는
`docs/intraday-bracket-design.md`, `docs/toss-api-contract.md`, `docs/macos-operations.md`에 둔다.

## 2026-08-29 — MacBook 원격 접속·배포 상태 실사

Tailscale 사설망 위에서 macOS 내장 OpenSSH와 작업 전용 공개키로 원격 접속을
검증했다. Homebrew의 CLI 전용 `tailscaled`는 네트워크 데몬으로 유지하되, 내장
Tailscale SSH 서버는 인증 뒤 macOS 사용자 셸을 시작하지 못하고 종료 코드 1로
끝나 비활성화했다. 비밀번호는 공유하거나 저장하지 않았다.

운영 노드는 Apple Silicon M3 Pro, 36GB RAM으로 local LLM과 trading 서비스를
함께 실행할 여유가 있다. AC 전원에서는 system sleep이 꺼져 있었지만 배터리
프로필은 짧은 sleep을 허용해 점검 중 연결이 반복적으로 끊겼다. 상시 운영 전
전원 연결, 덮개 상태, 재부팅 뒤 `tailscaled`/OpenSSH/launchd 복구를 실제로
검증해야 한다.

소스 정본은 아직 확정하지 않았다.

- Mac `main`은 Windows 기준점의 직계 후손으로 16개 커밋 앞서지만, 대규모
  미커밋 source/test/ops 변경과 runtime 산출물이 함께 있다.
- Windows 작업트리에는 Mac에 없는 shadow-only intraday 계획기와 격리형
  `turtle_news`가 있으며 전체 `372 passed`, `git diff --check`를 재확인했다.
- 양쪽이 동시에 수정한 `operations.py`, `state_store.py`, `config.py`,
  `notifier.py` 등을 어느 한쪽으로 덮어쓰거나 `git add -A`, `stash -u`로 묶지
  않는다.
- 다음 정본은 Mac의 최신 committed 기준점을 별도 clean checkout에 복원하고,
  Windows의 독립 파일부터 이식한 뒤 공유 파일을 수동 통합해 만드는 새 검증
  커밋이다.

Mac 운영 실사에서는 local LLM, gateway, 복수 dashboard 컨테이너가 실행 중이고
배포 이미지의 핵심 코드 해시가 dirty 작업트리와 일치함을 확인했다. dashboard는
실주문 경로를 포함하며 현재 운영 설정은 live 진입이 가능한 형태이므로 Mac에서
외부 호출 가능 테스트를 실행하지 않았다. 안전한 통합본에서는 기본값을
`shadow`, emergency stop 활성, 당일 수동 arm/승인 필수로 되돌리고 뉴스 결과가
전략 입력으로 들어가는 기존 research 경로를 제거하거나 구조적으로 격리한 뒤에만
배포한다.

최종 상태 스냅샷에서는 직접 주문 프로세스와 dashboard 컨테이너가 실행 중이지
않았지만, gateway는 로그인 후 live 컨테이너와 주문 루프를 만들 수 있는 상태였다.
적어도 한 운영 SQLite DB는 health job에서 `database disk image is malformed`로
판정됐으므로 원본에 migration이나 repair를 직접 수행하지 않는다. live control
plane을 먼저 disarm하고, DB 파일과 WAL/SHM을 일관된 복제본으로 보존한 뒤 SQLite
backup/recover 결과와 주문·포지션 원장을 대조해야 한다. 복구가 끝날 때까지 해당
DB를 주문 상태의 source of truth로 사용하지 않는다.

운영 파일 권한, 반복 실패한 refresh/news 작업, user LaunchAgent의 로그인 전 복구
불가도 후속 P1로 남겼다. 설치된 plist 문법은 모두 유효했지만 이는 서비스 정상
동작이나 재부팅 복구를 증명하지 않는다.

이번 실사는 파일, 서비스, 주문 상태를 변경하지 않은 읽기 전용 점검이었다.

### SSH 안정성 단기 표본과 남은 판정

AC 전원 상태에서 `caffeinate` 없이 native OpenSSH 세션을 4분 유지하며 10초 간격
heartbeat 24회를 모두 확인했고, 세션을 완전히 닫은 뒤 별도 one-shot SSH 재접속도
8/8 성공했다. 재접속 지연은 204–319ms였고 모든 명령이 종료 코드 0을 반환했다.
따라서 현재 AC 표본에서는 Tailscale 경로, 공개키 인증, native OpenSSH가 끊기지 않았다.
추가 one-shot 조회도 성공했고, `pmset` 전원 로그에는 앞선 배터리·덮개 닫힘 구간의 sleep이
기록돼 있었지만 AC 연결 뒤 점검 구간에는 새 sleep이 없었다.

다만 이 결과는 영구 무중단을 증명하지 않는다. 지속 세션 자체가 sleep 조건에 영향을
줄 수 있고 이전 단절은 배터리 방전 상태의 짧은 sleep 정책과 함께 관측됐다. 운영 승격
전에는 다음을 별도 gate로 통과해야 한다.

1. persistent tty 없이 5분 완전 유휴 후 30초 간격 one-shot 접속 10/10
2. AC 전원 1시간 soak에서 one-shot 접속 60/60, `tailscaled`/`sshd` 재시작 0회
3. live disarm·열린 주문 0·DB backup 뒤 계획 재부팅 시험
4. FileVault 해제 전/후와 사용자 로그인 전/후를 나눠 복구 시간 기록

### 상세 구현 결정

단타 live 실행부는 새 프레임워크를 만들지 않고 기존 장전 계획기, 거래 원장,
pre-trade safety, conditional adapter를 재사용한다. 신규 production 모듈은
`toss_stream.py`(선정 종목 시세·호가와 계좌 주문 이벤트 수신)와
`intraday_live.py`(복구 우선 상태기계) 두 개로 제한한다. 기존 동기 runtime을
asyncio로 전환하지 않고 동기 WebSocket client에 bounded reconnect와 REST full resync를
붙인다. 장전 승인 종목의 시세·호가만 구독하고 계좌 주문 이벤트는 별도 구독한다.

공식 Toss 계약에서 계좌 목록, 현금 매수 가능액, 보유·매도 가능 수량, 수수료, 시세·호가·
캔들, 상·하한가, 미국 장 세션, 일반·조건주문 상태를 조회할 수 있음을 재확인했다. 이 값들은
사용자 입력에서 제거하고 매 계획·주문·복구 직전에 API로 갱신한다. 사용자 정책은 선정 방식,
최대 투입액, 최대 손실, 가격 계획 승인 방식, OCO 실패 시 비상청산 권한, 일일 arm으로
최소화한다. 미국 종목의 공식 상·하한가가 `null`인 것은 정상일 수 있으므로 전략 익절·손절과
혼동하지 않는다.

사용자는 자동 종목 선정, API 현금에 적용하는 사용자 설정 비율, 시스템 가격 계산 후 승인,
재부팅 뒤 수동 로그인·재승인, 선정 종목의 새 기사별 Discord 알림을 선택했다. 별도 daily arm은
plan hash 승인과 같은 동작으로 합쳐 용어와 상태를 늘리지 않는다. 손실 한도는 미정이므로 첫
pilot에 배정현금의 0.25%를 보수적 제안값으로 기록했으며, 1주 위험이 이를 넘으면 진입하지
않는다. OCO(One Cancels the Other)를 보호 SLO 안에 확정하지 못하면, 늦게 생성된 OCO의
부재·취소와 경쟁 SELL 부재를 먼저 확인한 뒤 당일 해당 plan의 확정 BUY에서 확정 SELL을 뺀
잔여 전 수량을 자동 비상청산하기로 확정했다. 기존·수동·다른 전략 보유분은 포함하지 않는다.

계획 승인은 Discord 직접 승인(B 방식)으로 변경했다. incoming webhook을 양방향으로 확장하지
않고 전용 private bot이 Mac에서 outbound Gateway로 버튼 interaction만 받는다. OAuth 설치는
서버 기본 권한 `0`으로 하고 지정 channel overwrite에서만
`VIEW_CHANNEL | SEND_MESSAGES = 3072`를 허용한다. Gateway intents는 0으로 제한하고 privileged intent,
관리자·메시지/역할/웹훅 관리·slash command 권한은 주지 않는다. allowlist 사용자·서버·채널,
plan hash, 일회용 nonce, 만료시각을 확인한 이중 확인만 one-shot 승인 영수증 생성을 허용한다.
현재 worker는 `PLANNED -> APPROVED` 전이, arm, 주문을 수행하지 않는다. 승인 worker에는 Toss
자격증명·주문 API·trading DB 쓰기를 주지 않고 redacted plan과 one-shot approval inbox만
허용한다. 뉴스 worker와 뉴스 webhook은 계속 출력 전용이다.

당시 Discord Developer Portal의 기존 application을 `주식 거래 승인 봇`으로 이름 변경하고 실제
설정을 확인했다. Public Bot과 User Install을 끄고 Guild Install만 유지했으며, Default Install
Link는 `None`, Presence·Server Members·Message Content intent는 모두 OFF다. 아직 어떤 Discord
서버에도 설치하지 않았고 bot token은 재설정하거나 노출하지 않았다. 따라서 현재 완료 범위는
application provisioning이었다. 이후 설치·Keychain 상태는 아래 최신 항목에서 정정한다.

모든 승인·거래·뉴스 알림은 운영자가 지정한 단일 Discord channel만 대상으로 한다. 실제 channel
ID는 공개 문서나 저장소에 기록하지 않고 Mac의 로컬 실행 환경에만 주입한다. sender는 호출자가
channel을 바꿀 수 있는 인자를 제공하지 않으며, 설정값과 다른 channel의 발송·interaction은
fail-closed로 거부한다. 설치 뒤 Discord 권한도 해당 channel에만 허용해 코드 allowlist와 서버
권한을 이중으로 적용한다.

현재 거래 `DiscordTradeNotifier`와 독립 뉴스 worker에도 같은 단일 channel gate를 구현했다.
`DISCORD_ALLOWED_CHANNEL_ID`가 없거나 잘못된 형식이면 notifier를 비활성화하고, 각 webhook의
Discord metadata를 GET으로 먼저 확인해 `channel_id`가 다르거나 확인할 수 없으면 메시지 POST
전에 fail-closed한다. Dashboard configured 판정과 multi-user gateway 환경 전달도 webhook과
channel 설정을 함께 요구한다. 실제 Discord endpoint를 호출하지 않는 회귀 테스트에서 잘못된
channel은 POST 0회임을 확인했고, 전체 `376 passed`, `compileall`, `pip check`, `git diff --check`를
통과했다. 실제 webhook·bot token·서버 channel ACL을 사용한 E2E는 아직 수행하지 않았다.

구현 순서는 dirty Mac과 dirty Windows를 직접 합치지 않고 clean integration checkout을
먼저 만든 뒤, live submission fencing/UNKNOWN 복구, strict intraday config와 immutable
plan, WebSocket transport, BUY→OCO runtime, 고장 주입, shadow soak, 1주 pilot 순서로
고정했다. 뉴스 worker는 계속 별도 프로세스·DB·webhook을 사용하고 LLM 출력은 주문,
수량, 손절·익절, 승인 상태에 영향을 줄 수 없다. 세부 상태 전이와 합격 기준은
[장전 가격 계획 기반 단타 브래킷 설계](intraday-bracket-design.md#11-live-구현-작업-계획)에 기록했다.

## 2026-08-28 — 실거래 전 운영·안전성 감사

### 결론

현재 GitHub `main`(`0ef73d3`)은 **실거래 배포 금지, shadow-only** 상태로 판정했다.
실주문 코드가 존재하지만 문서·상태 소유권·주문 복구·원격 제어 경계가 그 위험에
맞게 닫혀 있지 않다. 아래 P0를 모두 해결하고 실패 훈련을 통과하기 전에는 live
모드를 켜지 않는다.

### 검토 기준과 검증 결과

- GitHub `main`에서 `PYTHONPATH=src python -m pytest -q`: `250 passed`.
- `python -m compileall -q src ops`, `python -m pip check`: 통과.
- 기본 인터프리터로 실행하면 다른 editable checkout을 import해 `15 failed, 235 passed`가
  발생했다. 테스트가 현재 저장소를 강제로 import하도록 설정하고 전용 가상환경을 써야 한다.
- 별도의 로컬 작업 트리는 GitHub보다 16개 커밋 앞서고 대규모 미커밋 변경을 포함하며,
  그 작업 트리 자체의 테스트는 `512 passed`였다. 그러나 재현 가능한 공개 릴리스가 아니므로
  GitHub `main`도, 해당 미정리 작업 트리도 그대로 배포 원본으로 삼지 않는다.
- 앞선 작업 트리에는 live submission lock, 원자적 idempotency 예약, 빈 allowlist 차단 등 일부
  개선이 이미 있으므로 버리지 말고 선별 통합한다. 무인증 dashboard, 전략 상태 초기화,
  broker 409 분류 등 남은 차단 문제는 그 작업 트리에서도 별도로 해결해야 한다.
- 원격 Mac은 점검 시 오프라인이어서 SSH 접속, 전원 정책, launchd 설치 상태는 확인하지 못했다.

### P0 — live 전환 차단 조건

1. **단일 원본 확정:** 앞선 로컬 작업을 작은 검토 가능 커밋으로 정리하고 GitHub와 통합한 뒤,
   깨끗한 checkout에서 전체 검증을 다시 수행한다. 미커밋 작업 트리를 덮어쓰지 않는다.
2. **주문 소유자 단일화:** config가 live로 바뀐 기존 `--paper-service`와 대시보드 live 스레드가
   동시에 돌 수 있다. 프로세스 간 단일 실행 lock, DB의 원자적 idempotency 예약 및 주문 제출
   lock으로 한 계좌·한 전략의 live writer를 하나로 제한한다.
3. **전략 상태 영속화:** heartbeat마다 `PaperTradingRuntime`과 `StrategyState`가 다시 만들어져
   Turtle System 1의 이전 승자 skip 상태가 사라진다. 상태를 DB에 저장하고 재시작 회귀를 추가한다.
4. **포지션 소유권 보존:** broker 보유분을 로컬 전략 포지션에 먼저 덮어쓰면 불일치가 가려지고
   수동 보유분이 자동 청산 대상이 될 수 있다. broker truth, 전략 소유분, 수동/외부 보유분을
   분리하고 불일치 시 거래를 차단한다. 재구성 과정에서 pyramid unit 수가 1로 축소되지 않게 한다.
5. **원격 제어 인증:** Tailscale 주소에 직접 bind하는 무인증 HTTP 대시보드가 live 설정 및 주문
   동작 POST를 받는다. backend는 localhost에만 bind하고 Tailscale Serve ACL/identity 또는 SSH
   port forwarding 뒤에 두며, Origin/CSRF·요청 크기·감사 로그를 적용한다.
6. **불확정 주문 복구:** 네트워크 timeout/`URLError`와 Toss `409 request-in-progress`를 실패가 아닌
   UNKNOWN으로 보존한다. broker order id가 없어도 `clientOrderId`로 조회·대조할 수 있어야 하며,
   해결 전에는 같은 intent의 재주문을 막는다. Toss의 `clientOrderId` idempotency 보장 시간도
   로컬 운영 계약에 명시한다.
7. **한도 원장 불변화:** 일일 주문 수·금액을 상태 동기화로 바뀌는 현재 주문 row가 아니라
   append-only submit event에서 합산한다. 취소·체결 동기화가 주문 한도를 되돌리지 못하게 한다.
8. **운영 복구성:** WAL DB를 파일 하나만 ZIP으로 복사하지 말고 SQLite backup API와 무결성 검사를
   사용한다. launchd는 비정상 일반 종료도 재시작하도록 구성하고, Keychain 기반 secret 주입,
   외부 stale-heartbeat 알림, 로그/DB 보존 정책을 추가한다.

추가 안전 결함으로 빈 `allowed_symbols`의 fail-open, 서비스 시작 직후 첫 iteration의 즉시 재실행,
`PARTIALLY_FILLED` 상태명 오타, 느슨한 bool/숫자 config 파싱을 함께 수정한다.

주요 코드 근거는 다음과 같다. 이 위치는 기준 커밋 `0ef73d3`에 대한 것이다.

| 위험 | 근거 |
| --- | --- |
| 무인증 dashboard의 live mutation | `ops/run-dashboard-macos.command:45-50,95-100`, `src/turtle_bot/health.py:6476-6506,6565-6619` |
| 복수 live 실행 경로와 비원자적 idempotency | `src/turtle_bot/operations.py:321-397,1380-1412`, `src/turtle_bot/state_store.py:209-218,771-778` |
| 매 iteration 전략 상태 초기화 | `src/turtle_bot/operations.py:1632-1668`, `src/turtle_bot/paper_runtime.py:347` |
| 비교 전 position mutation 및 unit 축약 | `src/turtle_bot/position_sync.py:320-348,375-417`, `src/turtle_bot/strategy.py:163-191` |
| timeout과 broker id 없는 주문 복구 누락 | `src/turtle_bot/live_execution.py:140-176`, `src/turtle_bot/toss_client.py:123-142`, `src/turtle_bot/operations.py:2265-2275` |
| Toss 409를 terminal failure로 분류 | `src/turtle_bot/toss_live_adapter.py:262-267`, `src/turtle_bot/live_execution.py:155-170` |
| 취소·동기화로 사라지는 일일 한도 | `src/turtle_bot/state_store.py:845-871`, `src/turtle_bot/live_execution.py:178-195,226-259` |
| WAL과 일치하지 않는 파일 단독 backup | `src/turtle_bot/state_store.py:39-52`, `src/turtle_bot/operations.py:1043-1072` |

### P1 — 검증과 운영 품질

- 백테스트의 신호가 난 당일 종가 체결을 next-open 또는 명시적인 MOC 모델로 바꾸고,
  slippage·spread·환전·티커 변경/상장폐지 가격을 반영한다. 고정한 파라미터로 별도 held-out 및
  walk-forward 검증을 수행하며 README의 큰 과거 수익률을 live 기대값으로 사용하지 않는다.
- README와 운영 문서의 “실주문 미구현/read-only” 설명을 실제 주문 생성·정정·취소 코드와 맞춘다.
- 의존성을 lock하고 CI가 전용 환경에서 현재 `src`를 import하는지 검증한다.
- macOS 점검 명령에 Toss 인증/네트워크, DB integrity·쓰기 권한, 시간대와 장 상태를 포함한다.
- 업데이트는 시장 개장·미결 주문·백업·rollback gate를 통과한 경우에만 수행한다.
- buying-power 조회 실패와 산출할 수 없는 주문 금액은 fail-closed로 막고, 실계좌 equity 기반 sizing,
  일일 실현·미실현 손실 및 peak drawdown의 durable halt를 추가한다.
- live에서는 429 fallback candle에도 최대 age를 강제하고 stale 표식을 blocker로 전달한다.
- STOP/EXIT를 관측 현재가의 일반 LIMIT으로만 내지 말고 IOC/marketable limit 재가격 또는 지원되는
  조건부 주문과 watchdog를 사용해 보호 주문의 미체결을 감시한다.
- iteration 예외를 서비스 loop 내부에서 격리·보고한다. dashboard process만 살아 있고 trading
  thread가 영구 종료되는 상태를 외부 heartbeat로 탐지한다.

### 단순화 후보 (`ponytail-audit`)

- `delete:` 단일 사용자·단일 계좌 운영이 확정되면 다중 사용자 Docker gateway와 관련 테스트·launcher를 제거한다. 약 `-3,583줄`, Docker Desktop 의존성 `-1`.
- `shrink:` 별도 SPA가 필요하지 않다면 4천 줄대 인라인 dashboard를 작은 상태·제어 화면으로 축소한다. 약 `-2,500줄`.
- `delete:` 완전히 미참조인 `_legacy_dashboard_html`을 제거한다. `-1,270줄`.
- `delete:` 거의 쓰지 않는 299줄 package barrel export를 버전만 남기고 직접 module import로 바꾼다. 약 `-290줄`.
- `shrink:` 단일 종목 `BacktestEngine.run()`을 검증 후 `run_portfolio()` 위임으로 바꾼다. 약 `-240줄`.
- `delete:` runtime에서 호출되지 않는 Toss API/orderbook cache, 과거 runtime scaffold, 옛 `broker_orders` API는 필요가 생길 때 다시 추가한다. 약 `-525줄`.
- `yagni:` 운영 호출이 없는 `RateLimitQueue`의 priority queue API를 제거하고 rate-limit pause 상태만 남긴다. 약 `-100줄`.
- `shrink:` YAML과 CLI의 중복 설정 및 사용하지 않는 예제 config를 하나의 설정 원본으로 합친다. 약 `-110줄`.
- `stdlib:` 쓰기 가능한 설정을 JSON으로 통일할 수 있다면 PyYAML 대신 표준 `json`을 써 필수 의존성 `-1`을 검토한다.

`net:` 최대 약 `-8,700줄`, `-2 deps` 가능. 이 수치는 단일 사용자 gateway 제거와 dashboard
범위 축소가 확정될 때의 상한이며, 기능 의도를 확인한 뒤 항목별로 적용한다.

### MacBook 운영 목표

한 사용자·한 계좌 기준으로는 native Python + launchd를 기본 경로로 삼는다. dashboard는
localhost 전용으로 두고 Tailscale Serve 또는 SSH tunnel을 접속 경계로 사용한다. 자격 증명은
Keychain에서 launch wrapper로 주입하고 plist나 저장소에 기록하지 않는다.

배포 순서는 깨끗한 canonical checkout → ops check → read-only → paper → shadow soak → 소액 live
순서다. live 전에는 중복 실행, 주문 응답 timeout, broker 409, 재부팅, DB 복원, 장중 네트워크 단절을
각각 강제로 재현하고 “중복 주문 없음·불확정 주문 재제출 없음·자동 안전 정지”를 확인한다.

### 공식 계약 참고

- [Toss Open API 가이드](https://developers.tossinvest.com/docs)
- [Toss OpenAPI schema](https://openapi.tossinvest.com/openapi-docs/latest/openapi.json)
- [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve)
- [Tailscale SSH](https://tailscale.com/docs/features/tailscale-ssh)

## 2026-08-28 — MacBook LLM 뉴스·Discord 설계

사용자 운영 의도를 “MacBook 상시 실행 + LLM 뉴스 요약 + Discord 전달”로 확정하고
[상세 설계](macbook-news-llm-design.md)를 작성했다.

선행 로컬 작업 트리에는 local Gemma/llama-server, 긴급·일일 launchd 작업, 뉴스 dedupe와
Discord embed 구현이 이미 있으며 관련 선택 테스트 `71 passed`를 확인했다. 다만 이 구현은
뉴스 감성 점수를 전략의 entry/exit/stop에 연결하고, dashboard action POST와 거래 DB에 알림
상태를 기록하므로 그대로 채택하지 않는다.

결정:

- 계좌별 trading agent와 별도 news-digest/LLM 프로세스를 둔다.
- trading agent가 redacted `news-context.json`을 atomic export하고, news worker에는 Toss 자격증명과
  거래 DB path·쓰기 권한을 주지 않는다.
- `news.enabled` 전략 경로를 제거 또는 live에서 강제 비활성화하고 `news_digest.enabled`와 분리한다.
- 기존 runner의 수집·exact dedupe·실행 lock·Discord formatter·local llama-server만 선별 재사용한다.
- 추천·감성·호재/악재·주가 영향 추론·LLM event clustering·dashboard POST는 제거한다.
- Discord에는 중립 요약, publisher, 보도 시각, 검증된 원문 링크와 “투자 판단 아님”을 표시한다.

다음 checkpoint는 canonical source 통합 후 뉴스 one-shot worker를 dry-run으로 축소 구현하는 것이다.

## 2026-08-28 — 장전 가격 계획 기반 단타 브래킷 설계

사용자의 “장 시작 전에 상한·하한을 정하고 단타” 요구를 미국 주식의 고정 상·하한가가
아니라 진입선·익절선·손절선과 위험 예산을 잠그는 intraday bracket으로 정의했다.
[상세 설계](intraday-bracket-design.md)를 작성하고 1단계 `shadow-only` 장전 계획기를 구현했다.

공식 Toss OpenAPI `1.2.14`와 WebSocket AsyncAPI `1.2.2`를 다시 확인해 다음을 결정했다.

- 미국 종목의 `price-limits`는 상·하한가가 `null`이며 LULD는 별도 동적 밴드다.
- Toss OTO는 BUY 뒤 SELL 하나만 제공하고, OCO는 SELL/SELL 두 조건만 제공한다. 따라서 BUY의
  실제 체결 수량을 확인한 뒤 OCO를 별도로 생성한다.
- OCO는 LIMIT만 가능하므로 손절도 stop-limit이다. 갭·거래정지 재개 때 미체결될 수 있어
  `triggeredOrderId` watchdog와 제한된 재가격·비상청산이 필요하다.
- 해외 조건주문은 모든 거래 가능 세션에서 발동하므로 장전에는 계획만 저장하고, v1 진입은
  정규장 WebSocket 조건에서 일반 지정가로 실행한다. 신규 진입은 Mac/stream 장애 시 fail-closed다.
- BUY 체결부터 OCO `WATCHING` 확인까지를 `OPEN_UNPROTECTED`로 명시하고, OCO가 확정되지 않으면
  비상청산·당일 신규 진입 중지로 전환한다.
- live pilot은 롱, 1종목, 1주, 하루 1회로 제한한다. 수량 확대 전에 partial fill과 OCO resize의
  보호 공백을 별도로 검증한다.
- 뉴스 LLM과 Discord는 기존 data-diode를 유지하며 거래 가격·수량·허용 여부를 바꾸지 않는다.

구현 결과:

- `strategy.kind=intraday`는 기존 paper/shadow 가상체결 엔진보다 먼저 분기한다. `shadow`, 미국장,
  1종목, live 비활성 조건이 아니면 broker 호출 전에 차단한다.
- Toss `USD cashBuyingPower`만 현금 원본으로 쓰고, 전체 보유·열린 일반 주문·열린 조건주문이
  있으면 계획을 만들지 않는다.
- `/prices`와 `/orderbook`의 timestamp·통화·신선도·상호 skew·호가 잔량·스프레드·last-mid
  괴리를 검사한 뒤 `max(lastPrice, bestAsk)`를 보수적 기준가로 쓴다.
- 비율 비용과 고정 최소비용을 포함한 왕복 예상비용을 현금에서 먼저 예약하고
  risk/notional/quantity 한도를 모두 적용한다. 미국
  호가단위 반올림 뒤 최소 R:R를 다시 검사한다.
- raw accountSeq는 저장하지 않고 해시 key를 사용한다. SQLite의 계좌·미국 거래일 unique 제약과
  canonical payload hash로 최초 계획 한 건만 immutable 저장한다. 설정 변경으로 당일 계획을
  덮어쓰지 못한다.
- 공식 조건주문 SINGLE/OCO/OTO adapter와 UNKNOWN 결과 분류를 계약 테스트로 추가했지만,
  intraday runtime에서는 OPEN 목록 GET만 사용한다. 생성·수정·삭제는 live에 연결하지 않았다.
- Discord 계획·차단 알림은 `SHADOW/실주문 없음`을 명시하고 account key와 raw market payload를
  내보내지 않으며 mention parsing을 끈다. 계획과 알림 outbox를 같은 SQLite 트랜잭션에
  저장하고, 전송 실패·재시작 때 claim lease로 다시 처리한다. 외부 차단 사유는 고정 공개문구,
  내부 진단은 예외 타입과 HTTP status만 남겨 예외 문자열 속 token이 유출되지 않게 했다.
- 선택형 Decimal 오타·bool·NaN/Infinity를 `0`으로 바꾸던 fail-open 파싱과, 일반 주문
  `hasNext` 누락·타입 오류를 빈 계좌로 보던 경계를 fail-closed로 수정했다.
- 2026-08-29 최종 재검증에서 현재 checkout을 강제한
  `PYTHONPATH=src python -m pytest` 전체 회귀는 `347 passed`다.

이 기능은 기존 live P0가 해소되고 canonical source가 정리되기 전까지 계속 `shadow-only`다.
다음 checkpoint는 전용 Mac `.venv`와 import path를 고정한 뒤 장전 계획을 여러 거래일 수집하고,
공식 WebSocket reconnect/REST resync와 BUY→OCO 보호 상태기계를 별도 단계로 구현하는 것이다.

## 2026-08-29 — 선정 단타 종목 전용 뉴스·LLM·Discord v1

사용자 요구를 “종목별 WebSocket”과 “뉴스 알림”으로 분리했다. WebSocket은 향후
가격·체결·주문 상태용이고, 이번 구현은 장전 계획에서 확정된 한 종목만 15분 one-shot
REST polling으로 수집하는 정보 경로다. 뉴스 결과는 전략·주문으로 돌아가지 않는다.

구현 결과:

- intraday plan을 SQLite에서 저장·무결성 검증한 existing/new/race-winner 세 경로가
  `schema_version/generated_at/market/session_date/active_until/symbol/reason`만 담은
  `news-context.json`을 atomic export한다. 계좌·현금·수량·가격·주문 정보는 제외했다.
- context writer는 nonblocking file lock과 단조성 검사를 사용한다. 오래된 iteration이 최신
  조기폐장 시각을 늘리거나, 같은 경로의 다른 symbol writer가 덮어쓰지 못한다. 파일·진단 DB
  실패는 best-effort WARN일 뿐 trading health와 immutable plan을 바꾸지 않는다.
- `turtle_bot` eager import가 거래 모듈을 함께 로드하므로 기존 AI/notifier를 재사용하지 않고
  stdlib-only 독립 package `turtle_news`를 만들었다. import 회귀 테스트는 거래 package가
  `sys.modules`에 생기지 않음을 확인한다.
- Finnhub Company News에 선택 symbol을 명시하고 `related` exact token, 24시간 age, HTTPS public
  URL을 다시 검사한다. 응답·문자열·기사 수·timeout·redirect를 제한하고 본문 scraping은 없다.
- worker 환경에 Toss secret이나 거래 webhook이 있으면 시작을 거부한다. 뉴스 DB는 read-only
  immutable preflight와 recovery sidecar 거부 후에만 writable open하며 거래 table, integrity,
  schema 충돌을 fail-closed로 처리한다.
- 별도 `news.sqlite3`는 URL hash dedupe, session date, `PENDING/SENDING/SENT/EXPIRED`, claim lease,
  3회 attempt cap, validated summary cache와 안전한 error code만 저장한다. 이전 거래일 pending은
  다음 plan으로 넘어가지 않는다.
- LLM은 loopback OpenAI-compatible endpoint만 허용한다. 악성 prompt, 선택 symbol 외 대문자
  ticker, 새 숫자, URL, mention, 매수·매도·추천 문구가 나오면 결과와 provider excerpt를 버리고
  제목·publisher·시각·검증된 링크만 Discord에 보낸다.
- Discord는 뉴스 전용 webhook, `wait=true`, `allowed_mentions.parse=[]`를 사용하고 확인된 2xx
  뒤에만 `SENT`로 바꾼다. 실패는 cached summary로 다음 launchd 주기에 재시도한다. Discord가
  저장한 직후 응답이 유실되면 중복될 수 있으므로 전달 계약은 at-least-once다.
- 매 기사 전과 Discord 직전에 context freshness·session·`active_until`을 재검사하고 claim lease를
  갱신한다. 장 종료나 trading heartbeat 중단 뒤에는 새 뉴스가 전송되지 않는다.

검증:

- `PYTHONPATH=src python -m pytest`: `372 passed`.
- focused tests는 context redaction/조기폐장/경합/진단 실패, import 격리, stale·주말 context,
  exact symbol, 위험 환경, 거래 DB 무변경, dedupe/lease/session/attempt cap, 악성 LLM fallback,
  Discord retry cache와 장 종료 직전 재검사를 포함한다.
- 실제 Finnhub key, 뉴스 Discord webhook, Mac local LLM을 사용한 외부 smoke test와 launchd
  재부팅 검증은 아직 수행하지 않았다.
- 현재 Windows의 unscoped system Python은 여전히 다른 checkout의 `turtle_bot`을 import하고
  `turtle_news`를 찾지 못한다. 따라서 검증은 `PYTHONPATH=src`로 현재 checkout을 강제했고,
  Mac 배포는 exact `.venv` import path gate를 통과하기 전까지 금지한다.

운영 전 gate는 Mac의 canonical checkout·전용 `.venv`, 계좌별 checkout 밖 runtime 경로,
Finnhub 계약·quota, 단일 허용 Discord channel과 뉴스 전용 webhook, local LLM model/RAM, 15분·24시간·4건
기본값, at-least-once 중복 수용을 확정하는 것이다. 단타 주문 실행은 계속 shadow-only이며
WebSocket reconnect/REST resync와 BUY→OCO 보호 상태기계는 별도 후속 단계다.

## 2026-08-29 — Discord 승인 봇 자격증명 검증 실패와 안전 복구

- 최초 macOS 로그인 키체인 항목은 metadata만 존재했으며, 로그인 GUI LaunchAgent의 비노출
  probe에서 저장값 길이가 `10`으로 확인돼 발급 토큰이 정상 저장되지 않았음을 확정했다. 이
  항목은 폐기 대상으로 분류했고 approval worker에서는 사용하지 않는다.
- 기존 항목을 갱신하는 1회용 Aqua LaunchAgent는 `security` 종료 코드 `45`로 실패했다. helper는
  실패를 성공으로 처리하지 않았고 임시 token 원본을 즉시 제거했으며, 원문은 브라우저 출력,
  저장소, `.env`, plist, 로그, 개발일지에 기록하지 않았다.
- 별도 전용 service `TossTradingBot.DiscordApprovalToken`에 새 항목을 생성해 전송 원본과
  byte-for-byte 일치를 검증했다. 이어 별도의 새 Aqua LaunchAgent에서 무프롬프트 재조회도
  `keychain-read-ok`로 통과했다. Windows/Mac token 원본과 두 1회용 helper·plist·status 파일은
  모두 제거했고 복구 감시는 중지했다. SSH Background 세션 조회 차단은 정상적인 세션 경계다.
- 이 시점에는 실제 approval worker와 거래 실행을 계속 비활성으로 두고 서버 설치/ACL,
  사용자·서버·채널 단일 allowlist, interaction E2E, clean exact-SHA Mac 배포를 남은 gate로
  기록했다. 다음 최신 항목이 설치·ACL의 후속 결과를 정정한다.

## 2026-08-29 — 격리 승인 worker·Discord 단일 채널 ACL 검증

- immutable intraday plan에서 승인용 redacted envelope를 checkout 밖 경로로 atomic export한다.
  실제 계좌 식별자·bot token은 포함하지 않고, existing/new/race-winner와 재시작에서 같은
  plan hash·nonce를 유지하며 만료를 뒤로 늘리지 않는다. 최초 생성은 no-clobber, 갱신은 현재
  envelope를 직전에 재검증하고, 표시값 drift 복구는 nonce를 회전한다. export 실패는 거래 계획이나
  DB를 바꾸지 않는 진단 실패다.
- 독립 `turtle_approval` package를 추가했다. Discord Gateway intents는 `0`이고 exact
  user/guild/channel과 화면에 표시된 전체 plan값/hash/nonce/expiry를 interaction마다 다시 검증한다.
  modal 제출은 먼저 ephemeral ACK를 보내고, temp write·fsync 뒤 hard-link no-clobber publish한
  mode `0600` one-shot 영수증만 만든다. 거래 package·SQLite·
  Toss 자격증명·subprocess·주문 API에는 접근하지 않으며 거절 경로와 inbox 소비자는 아직 없다.
- macOS 로그인 Keychain의 새 전용 항목은 생성 원본과 byte-for-byte 일치했고, 별도의 fresh Aqua
  LaunchAgent에서도 다시 읽혔다. application 설치, 대상 guild/channel, 운영자 소유 관계도
  token을 출력하지 않는 read-only API 감사로 일치함을 확인했다.
- 최초 서버 유효 권한 감사에서는 category 상속 때문에 대상 외 7개 channel/category에도
  View/Send가 남아 있었다. 비대상 채팅·음성 category와 동기화되지 않은 channel에 bot role
  View/Send 거부를 추가한 뒤 전체 11개 channel/category를 재계산했다. 최종 결과는 대상
  View/Send만 true, 그 외 View 0·Send 0, Administrator/Manage Channels/Manage Roles 모두 false다.
  실제 ID와 token, 임시 감사 script·plist·출력은 저장소와 Mac에서 제거했다.
- wrapper는 인자를 거부하고 `zsh -f` + `env -i` allowlist clean child 안에서만 Keychain token을
  읽으며 Python `-I` source gate를 적용한다. plist 로그는 존재가 보장된 mode `0700` runtime root에
  둔다. 같은 macOS UID의 다른 process를 막는 OS sandbox는 아니므로 현재 unsigned receipt는
  live capability가 아니며, 별도 identity/인증 출처와 consumer 재검증 전까지 거래 연결을 금지한다.
- 현재 checkout을 강제한 `PYTHONPATH=src python -m pytest`는 `442 passed`였고 `compileall`,
  `pip check`, `git diff --check`, macOS wrapper `zsh -n`, plist `plutil -lint`, 비밀정보 패턴
  scan을 다시 통과했다. 승인 전용 clean branch에서는 45개 승인 테스트, Python `-I` exact-source
  import, `pip check`, `git fsck`, clean status를 통과한 별도 exact-SHA commit을 만들었고 원격에는
  push하지 않았다. Mac 첫 Gateway 연결은 Python.org 런타임의 기본 CA 경로 부재로
  `ClientConnectorCertificateError`를 재현했고, TLS 검증을 끄지 않고 Apple 관리
  `/etc/ssl/cert.pem`을 clean wrapper에 명시한 뒤 연결을 확인했다. Discord
  서버 ACL은 이 로컬 suite와 별도의 외부 운영 gate로 검증했다.
- 아래 2026-08-30 항목이 clean exact-SHA release 설치와 Gateway 연결 결과를 이어서 정정한다.
  기존 Mac checkout·venv·DB에는 pull/reset/clean/editable install을 하지 않으며, 실거래와 새
  단타 주문 경로는 계속 비활성이다.

## 2026-08-30 — 승인 전용 exact-SHA Mac release·synthetic interaction E2E

- dirty/diverged 운영 checkout과 분리한 clean worktree에서 승인 worker에 필요한 7개 파일만
  `a660c9679f48cc08dd5e2f5142ed3f7c57ec195d`로 고정했다. 원격에는 push하지 않았고, 검증한
  git bundle로 Mac의 불변 release 디렉터리에 복원해 exact HEAD·clean status·`git fsck`를
  확인했다. Python 3.12 전용 venv에서 45개 승인 테스트, isolated exact-source import,
  `compileall`, `pip check`, wrapper `zsh -n`, plist `plutil -lint`를 통과했다.
- Python.org 런타임의 기본 CA 파일 부재로 Discord TLS 연결이 실패하는 현상을 재현했다. 인증서
  검증을 끄지 않고 wrapper의 clean environment에 읽기 가능한 Apple 관리
  `/etc/ssl/cert.pem`을 명시한 새 exact commit으로 교체한 뒤 Gateway 연결을 확인했다.
- 주문 권한·Toss 자격증명·거래 DB·inbox 소비자가 없는 synthetic/no-trade LaunchAgent만 임시로
  연결하고 허용된 사용자 세션에서 최신 요청의 승인 버튼과 hash 끝 8자리 modal을 제출했다.
  Discord ephemeral 응답은 worker가 영수증만 기록하고 주문을 제출하지 않음을 확인했다.
- inbox에는 strict validator를 통과한 receipt가 정확히 1개 생성됐다. 일반 파일·현재 UID·mode
  `0600`·크기 제한, `APPROVE`, plan/hash/전체 interaction binding, nonce SHA-256, 만료 범위를
  재검증했고 원 nonce·가격/수량 필드·계좌 별칭·자격증명 필드는 없었다. worker를 강제 재시작한
  뒤에도 stderr hash가 같고 receipt는 1개였으며, fresh 요청의 Discord DOM match 수가 재시작 전후
  동일해 추가 게시가 없음을 확인했다.
- 정확한 E2E label·plist·runtime parent/basename을 검증한 뒤 agent를 bootout하고 임시 plist와
  synthetic envelope/receipt/runtime을 삭제했다. 이 삭제는 복구하지 않으며, 검증된 불변 release는
  clean 상태로 보존했다. 과거와 이번 synthetic Discord 메시지는 운영 기록과 구분되지만 사용자의
  별도 삭제 승인 없이 원격에서 제거하지 않았다.
- 전체 checkout 회귀는 `442 passed`이고 `compileall`, `pip check`, `git diff --check`를 다시
  통과했다. 같은 macOS UID 경계가 해결되지 않았으므로 receipt는 계속 shadow 감사 기록일 뿐
  live 권한이 아니며, 기존 Mac checkout·venv·DB와 실주문 runtime은 건드리지 않았다.

## 2026-08-30 — 선정 종목 전용 Toss WebSocket shadow v1

공식 Toss AsyncAPI `1.2.2` 원본을 다시 확인했다. 2026-08-30 UTF-8 응답 SHA-256은
`130251057fd9535a3e276099f9166b445f8c51f505f30540758e4b209231282e`다. 구독은 action 세 개나
ACK 세 개가 아니라, request ID와 `trade:us`·`orderbook:us` 한 종목을 담은 full-replace 배열
하나와 단일 `type=subscriptions` ACK다. 현재 shadow에는 `personal:order`를 포함하지 않는다.

구현 결과:

- `turtle_bot.toss_stream`을 기존 60초 planner와 분리된 동기 process로 추가했다. immutable plan이
  redacted `news-context.json`에 잠근 한 종목만 읽으며 두 번째 symbol 설정 경로를 만들지 않는다.
  자동 모드는 `runtime.symbols`를 비워 두며, 구현된 selector가 계좌·미국 거래일당 잠근 plan을
  stream이 그대로 따른다.
- process는 Keychain wrapper가 전달한 client ID/secret을 즉시 환경에서 제거하고
  `TossClient(account_seq=None)`만 만든다. 연결마다 fresh OAuth, WSS handshake, 정확한 선언/ACK,
  read-only `/prices`·`/orderbook` baseline 순서로 확인한다. 거래 DB, 계좌번호, 개인 주문 topic,
  live/conditional adapter 호출은 없다.
- trade/orderbook frame은 text·64KiB·duplicate-key 없는 JSON, exact topic, USD, decimal,
  timezone-aware timestamp, broker/receive freshness, REST baseline 이후 시각을 검증한다. 동일
  timestamp는 sequence가 없으므로 허용하고 역행만 unusable로 만든다. binary·다른 symbol·malformed
  frame은 연결을 폐기한다.
- 공식 schema에서 nullable인 timestamp와 empty/zero/crossed/sort 불확정 호가는 protocol 오류로
  재연결 storm을 만들지 않고 `verified=false`/`shadow_usable=false`로 유지해 다음 frame 또는
  30초 REST resync를 기다린다. REST와 stream 사이 cursor·sequence가 없으므로 gap-free replay나
  entry crossing 복구를 주장하지 않는다.
- Toss 계약의 순수 텍스트 `PING`을 60초마다 보내고 JSON pong을 15초 안에 요구한다. periodic REST는
  outstanding pong이 없을 때 먼저 끝낸 뒤 ping deadline을 시작한다. 연결 실패는 1·2·4초 지수
  backoff, jitter, 30초 cap을 쓰며 한 연결이 verified baseline과 fresh trade/book까지 관측하면
  연속 실패 수를 reset한다. 총 재연결 수는 별도 보존한다. 고빈도 symbol에서도 context 파일을
  frame마다 읽지 않고 monotonic 1초 cadence로 재검증하며 메모리의 `active_until`은 매 loop 확인한다.
- `market-stream.json`은 시작 전에 이전 usable 값을 tombstone으로 무효화하고 이후 temp write,
  fsync, atomic replace, mode `0600`으로 redacted snapshot만 게시한다. `shadow_usable`과 함께
  broker/receive 만료로 계산한 `valid_until`을 내보내며 `ready_for_live_entry`와
  `live_order_submission`은 항상 `false`다. SIGKILL 뒤 소비자는 현재 시각과 `valid_until`을 반드시
  다시 비교해야 한다.
- `.[stream]` extra에 `websockets>=15,<16`을 고정했다. Mac wrapper는 argument-free `env -i`,
  exact-source import, Apple CA bundle, 기존 gateway Keychain layout을 사용하고 account/DB/Discord/
  live 설정을 전달하지 않는다. shadow 전용 Aqua LaunchAgent template은 비밀이 아닌 context,
  snapshot, Keychain slug만 가진다. Aqua 로그인 뒤 Tailscale SSH로 상태 확인·kickstart할 수 있다.
- `python -m turtle_bot.toss_stream`은 Python 규칙상 현재 eager `turtle_bot.__init__`도 실행한다.
  따라서 검증된 경계는 order adapter를 instantiate/call하지 않고 broker write가 0건이라는 것이며,
  별도 macOS UID나 서버측 market-data 전용 scope와 같은 OS/credential sandbox를 뜻하지 않는다.

검증:

- focused stream/ops fault tests는 exact declaration/ACK, ACK timeout, wrong topic,
  binary/oversize/duplicate JSON, nullable·stale·future·same/regressed timestamp, REST 이전 queued frame,
  broker-vs-receive TTL, empty/zero book, reconnect·healthy reset, REST 실패/null baseline, text PING,
  late/missing pong, 16초 slow REST overlap, context symbol/revision/expiry lock, 시작 tombstone,
  100-frame burst context I/O bound, mode `0600` redaction, clean Keychain/plist boundary를 포함한다.
- 전체 `PYTHONPATH=src python -m pytest` 회귀와 `compileall`, `pip check`,
  `git diff --check`도 통과했다. 실제 read-only `TossClient` transport 검증에서 요청은 OAuth POST와
  `/prices`, `/orderbook` GET뿐이고 `X-Tossinvest-Account`, JSON order body, 주문 endpoint는 0건이다.
- 실제 Toss credential을 사용한 Mac WSS handshake와 외부 REST baseline, 네트워크 재접속 주입,
  5–10 미국장 session shadow soak는 아직 수행하지 않았다. 새 code는 기존 승인 전용 Mac release나
  dirty 운영 checkout에 배포하지 않았고, live entry/OCO/자동청산은 계속 비활성이다.

## 2026-08-30 — 자동 intraday 종목 selector shadow v1

운영자가 `runtime.symbols`에 종목을 넣어야 했던 장전 경로를 `selection.mode=automatic`에서
실제 read-only Toss 데이터로 한 종목을 고르는 경로로 연결했다. 이 구현은 계획 생성까지만 하며
주문 runtime에는 연결하지 않은 `shadow-only`다.

구현·결정:

- `MARKET_TRADING_AMOUNT / US / realtime` 상위 20개는 후보 소스로만 사용한다. 이 응답의
  `tradingAmount`를 프리마켓 거래대금이라고 부르거나 그렇게 해석하지 않는다.
- 랭킹 후보를 NASDAQ·NYSE·AMEX의 `ACTIVE / STOCK / commonShare=true` strict universe와 교차한 뒤
  랭킹 순서상 상위 5개만 검사한다. 상세 종목 응답과 `/warnings`의 정확한 빈 배열 `[]`,
  `adjusted=false`인 이전 완료 raw 일봉 20개, 완전히 끝난 프리마켓 1분봉을 요구한다.
- 후보 확정 직전에 전체 계좌 flat과 USD 현금을 다시 확인하고 fresh 현재가·호가를 읽는다.
  랭킹 timestamp를 다시 검사하며, fresh 현재가와 랭킹 기준가로 최종 가격·등락률 범위를 재계산한
  뒤에만 임시 가격 계획을 만든다. 그 다음 최종 후보 warnings, 계좌 flat, USD 현금을 한 번 더
  읽고 마지막 현금값으로 계획을 다시 계산한다. DB `lock_at` 기준 price·orderbook·cash·ranking·
  warning-check·account-check freshness도 재검증한다.
- 계좌·미국 거래일 unique lock이 먼저 생성한 immutable plan을 승자로 정한다. 같은 process의 다음
  iteration과 재시작은 저장된 symbol·설정·guardrail을 검증해 재사용하며 selector를 다시 돌리지 않는다.
- 뉴스와 LLM은 선정 입력이 아니며 후보 점수·가격·수량을 바꿀 수 없다. 잠긴 plan의 한 symbol만
  redacted news context로 내보내고, 뉴스 worker와 `toss_stream`이 그 symbol만 사용한다.
- 현재 Toss REST OpenAPI에는 authoritative 미국 halt/LULD 상태 필드가 없다. 빈 warnings와 미국
  `price-limits=null`은 거래 가능 증명이 아니므로 live 승격을 명시적으로 차단한다.
- 마지막 warnings/account GET과 SQLite INSERT는 원자 transaction이 아니다. Toss가 broker
  계좌·시장 데이터와 로컬 DB를 아우르는 원자 snapshot을 제공하지 않으므로 그 사이 외부 수동
  주문 race를 제거할 수 없다. broker-side reservation 또는 동등한 배타 제어 전에는 이 residual
  race도 live 승격 blocker다.

내부 계약·회귀 검증은 exact ranking과 universe 교집합, 상위 후보 제한, malformed/비어 있지 않은
warnings, raw 일봉·완료된 프리마켓 봉, stale current market, 최종 warnings·계좌·현금·가격·등락률과
lock 시각 freshness 재검사, 경합·재시작 무재선정을 포함한다. 실제 Toss 응답과 Mac 실행을 함께
확인하는 자동 selector 외부 shadow smoke는 아직 수행하지 않았다.

## 2026-08-30 — 남은 intraday live runtime·Mac 운영 설계 확정

현재 selector·불변 장전 계획·redacted 승인 봉투/영수증·선정 종목 시세/뉴스 shadow 이후에 남은
실거래 경계를 코드와 공식 계약 기준으로 다시 감사했다. 결론은 계속 **live NO-GO**다. 현재
`operations.py`는 intraday를 shadow로만 dispatch하고, `state_store.py`에는 live run·writer fence·
owned/protected 수량이 없으며, 승인 receipt consumer와 BUY→OCO 실행 엔진도 없다.

공식 Toss OpenAPI `1.2.14`와 AsyncAPI `1.2.2`를 재검증했다. 일반 order create의
`clientOrderId` 멱등 창은 10분이지만 조건 create 문서는 같은 시간창을 명시하지 않는다. 주문
목록·상세·personal order WS에는 client ID 검색/echo가 없다. 정정·취소에는 멱등키가 없고 일반
cancel 성공은 operation order ID를 반환하지만 conditional DELETE 성공은 exact 204/no body다.
REST 상태는 누적 `filledQuantity`가 권위이며 canceled/rejected/replaced 주문도 이미 fill을 가질 수
있다. OCO는 같은 수량의 SELL/SELL LIMIT 두 leg이고 해외 조건은 모든 거래 가능 session에서
감시되며 stop leg도 stop-limit이다. 원자적 BUY→OCO 3-leg bracket, WS replay/sequence, authoritative
미국 halt/LULD 상태는 없다.

Ponytail 원칙으로 새 service/event bus/table 묶음을 만들지 않고 거래 production module을
`intraday_live.py` 하나로 제한했다. 거래 package를 import하지 않는 standalone watchdog 한 파일은
권한 격리를 위한 예외다. 기존 plan/hash/outbox, conditional adapter, stream transport와
market parser, calendar·Toss read client는 재사용한다. 기존 `LiveOrderOrchestrator`의 단발 cancel
overwrite, `TossPositionSync(sync_live_positions=True)`의 broker holding 자동 채택, 모든 intent에
emergency stop·일일 건수를 적용하는 `PreTradeSafety`는 intraday lifecycle에 직접 재사용하지 않는다.

DB에는 `intraday_runs` 하나와 기존 주문 원장의 최소 migration만 둔다. 모든 상태 전이는 CAS와
writer fence를 사용하고, network 전에 immutable client ID·canonical request hash·첫 시도·recovery
deadline·`submit_started`를 같은 transaction에 예약한다. 현재 `order_intents` upsert와
`has_unresolved` 뒤 insert의 TOCTOU는 제거하고 `(account_key, client_order_id)`를 unique로 만든다.
상태는 `PLANNED→APPROVED→RECONCILING→READY_TO_ENTER→ENTRY_*→OPEN_UNPROTECTED→
PROTECTION_*→PROTECTED→EXIT_*→CLOSED`이며 시작·재연결은 항상 broker 대조가 신호보다 먼저다.

일반-order UNKNOWN create만 저장한 동일 body/client ID를 10분보다 짧은 로컬 8분 deadline 안에서
주문 identity 복구 목적으로 최대 한 번 재호출한다. 조건 create UNKNOWN은 공식 시간창 확인 전
자동 재POST 0이다. 정정·취소 결과가 불명확하면 재호출하지 않고 원주문과 operation ID chain을 REST로
대조한다. personal WS는 빠른 알림일 뿐이며 live writer 한 연결에서 잠긴 한 종목의 trade/orderbook과
계좌 personal order만 full-replace 구독한다. 순서는 REST snapshot A→exact ACK→REST snapshot B이고,
personal frame은 projection에 대입하지 않고 REST 재조회만 trigger한다. 별도 shadow stream은 live
동안 중지한다.

첫 live pilot은 하루 1진입·정수 1주·재진입 없음이다. 첫 partial fill에 보호 clock을 시작하고 잔량
취소를 한 번 요청한 뒤 terminal 누적 fill만 owned quantity로 확정한다. OCO는 그 수량으로 한 번
생성하고 exact `WATCHING`이며 `protected_qty==owned_qty`일 때만 보호로 인정한다. OCO 생성 실패,
stop-limit 미체결, force-exit에서는 exact conditional DELETE 204와 post-ACK stable snapshot으로
OCO competition-cleared, 경쟁 SELL 상태, sellable quantity를 먼저 확인한다.
사용자가 선택한 자동 비상청산은 정규장 안에서 당일 plan의 `누적 BUY fill-누적 SELL fill` 잔여 전
수량을 MARKET SELL하는 정책으로 plan hash에 포함한다. 이는 가격·손실 상한·거래정지 중 체결을
보장하지 않는다. 취소/주문 상태가 불명확하면 과매도를 피하기 위해 두 번째 SELL을 만들지 않고
긴급 알림과 `RECOVERY_REQUIRED`를 유지한다.

하루 1회 pilot에는 별도 범용 daily-loss 설정을 늘리지 않는다. `risk_budget`은
`cashBuyingPower * risk_fraction`으로 계산해 계획당·일일 신규 위험 한도를 겸하며 초기 권장치는
allocated cash의 0.25%다.
gap과 stop-limit 미체결로 실제 손실은 이를 넘을 수 있다. durable kill switch와 loss fuse는
`ENTRY`만 막고 entry cancel, OCO, protective/force/emergency exit와 reconciliation은 계속 허용한다.

Mac live release에서는 현재 dashboard, unauthenticated/CSRF 없는 health action POST, multi-user
gateway와 Docker dashboard를 전부 제외한다. exact SHA·hash-locked dependency의 root-owned
read-only release를 사용하고 runtime DB/log/config/mailbox는 밖에 둔다. 승인과 거래는 별도 macOS
UID·Keychain으로 분리하고 cross-UID read-only mailbox의 owner/mode/link/schema와 live purpose,
boot/writer/approval generation을 consumer가 검증한다. 같은 UID shadow receipt는 live capability가
아니다. local watchdog 외에 Mac 전원·인터넷 전체 장애를 감지할 두 번째 상시 노드 deadman도
주문 권한 없이 같은 단일 Discord channel로만 경보해야 한다. trade/news/approval/watchdog는 매
전송마다 원격 channel을 다시 확인하며 첫 성공 뒤 영구 cache하지 않는다.

남은 P0 외부 결정은 live 전용 계좌와 당일 수동 주문 금지, 현금 사용 fraction, 초기 risk·시간값,
trader/approver/watchdog 세 macOS identity 운영, 별도 상시 deadman, authoritative 미국 halt/LULD
source다. 이 중 하나라도
없으면 live flag를 열지 않는다. 구현 순서는 state migration/fence→일반 주문 계약 교정→startup
reconciliation→live approval consume·entry→fill ownership·OCO·전량 exit→combined WS→role-aware
fuse/alerts→exact-SHA fault/shadow soak→실시간 감독 아래 1주 pilot이다. 상세 상태·시험·승격 기준은
`docs/intraday-bracket-design.md`, 공식 API 제약은 `docs/toss-api-contract.md`, Mac 경계는
`docs/macos-operations.md`에 기록했다.

## 2026-08-30 — 실계좌 test 제외 비실거래 구현 명세 확정

사용자 요청에 따라 실제 돈·소액·1주 주문, 실계좌 OCO·취소·비상/시간청산 시험을 현재 범위에서
완전히 제외했다. 당시 그 직전까지의 목표 완료명을 `NON_LIVE_IMPLEMENTATION_COMPLETE / LIVE_NO_GO`로
정의하고, replay·fake REST/WS·crash injection·synthetic Discord 승인·Mac exact-SHA shadow만으로
합격할 수 있는 명세를 `docs/intraday-bracket-design.md` 13절에 추가했다. 이 결과를
`LIVE_READY`, `PILOT_READY`, `SAFE_TO_TRADE`로 자동 승격하는 경로는 두지 않는다.

Ponytail 원칙으로 거래 production module은 향후 `intraday_live.py` 하나, 새 DB table은
`intraday_runs` 하나로 제한했다. 거래 package를 import하지 않는 standalone watchdog만 권한 격리
예외다. 기존 `idempotency_key`를 Toss `clientOrderId` 정본으로 재사용하고,
기존 intent/execution/event 원장을 v4 migration으로 강화한다. intraday create intent는 immutable
INSERT와 partial unique index를 사용한다. 일반 create는 최초 1회와 저장한 동일 body/client ID의
identity recovery 최대 1회, OCO create는 최초 1회와 자동 recovery 0회다. cancel/delete는 멱등키가
없으므로 reservation 뒤 한 번만 호출하고 결과 유실 시 REST로만 대조한다.

상태 전이는 writer lease/fence, broker-sync fence, version CAS와 append-only event를 같은 transaction에
묶는다. 한 runtime tick은 broker mutation을 최대 하나만 수행한다. process 시작·writer 교체·WS gap은
항상 `RECONCILING`으로 가며 REST A→exact WS ACK→REST B stable snapshot 전에 signal을 평가하지 않는다.
ownership은 root order별 최대 누적 BUY fill에서 triggered/force/emergency SELL fill을 빼 계산하며
negative·감소·초과·미등록 보유/주문을 자동 보정하거나 채택하지 않는다. `PROTECTED`는 OCO의
top-level quantity와 exact group/leg field 전부 및 `protected_qty==owned_qty>0`, `CLOSED`는
holdings/sellable 0, known 일반 주문 exact terminal, active conditional/leg-created SELL 부재를 함께
증명해야 한다.

승인 v2는 immutable shadow plan과 모든 가격·수량·위험, 두 SLO, 전량 MARKET 비상정책, boot ID hash,
writer fence, approval generation, nonce를 canonical hash로 묶는다. consumer는 cross-UID mailbox의
regular-file/owner/group/mode/link/schema와 exact Discord identity/channel/hash/expiry를 검사한 뒤 stable
broker snapshot과 한 번의 SQLite CAS로만 소비한다. 현재 same-UID shadow receipt는 live capability로
승격하지 않는다.

Mac 비실거래 topology는 shadow planner, 선택 종목 public stream, shadow approval recorder, news
one-shot, trading-unprivileged watchdog 다섯 job만 허용한다. 현재 `--shadow-service`가 paper와 같은 실행
함수를 쓰는 허점을 명시하고, 매 config reload의 strict shadow flags 검사와 exact Toss origin,
GET path allowlist, no-redirect transport tripwire를 필수 수정으로 정했다. live writer/receipt consumer,
dashboard/gateway/Docker dashboard/health action/`tailscale serve`는 release에 포함하지 않는다.

자동 합격은 v1/v2/v3→v4 migration, 모든 허용·금지 전이, 두 writer/consumer 경합, 11개 kill point,
partial fill·OCO/exit UNKNOWN, 최소 100개 replay signal, 뉴스/LLM data-diode, Mac dummy UID/mailbox/
watchdog을 포함한다. 모든 suite는 parent secret scrub과 process-level no-egress 아래 실제 Toss order
HTTP·실계좌 personal WS·자동 Discord interaction 0을 증명하고 fake mutation과 real transport call을
따로 집계한다. 상세 명세와 완료표는 프로젝트
문서에 두고, 공개 개발일지에는 이 결정과 source reference만 남긴다.

## 2026-08-30 — 한 달 실제 시세 intraday 전진 시뮬레이션 로컬 구현

실계좌 시험 대신 미국 시장 session date `2026-08-31`~`2026-09-30` 양 끝을 포함하는 한 달
forward simulation을 먼저 수행하기로 했다. 시작 자금은 설정 가능한 simulation 전용 가상
`USD 10,000`을 기본으로 하며, 실제 holdings·buying power·주문 API를 sizing이나 원장에 사용하지
않는다. 계좌/주문 내역과 personal WebSocket도 차단하고 Toss 수수료표 조회만 account-header
read-only 예외로 허용한다. 실제 주문 생성·정정·취소는 없다.

calendar coverage는 `today` 안에 `preMarket`와 `regularMarket` key가 모두 있고 둘 다 명시적 null인
경우만 휴장으로 인정한다. key 하나라도 누락되면 `intraday_calendar_malformed`, 한쪽만 null이면
`intraday_required_session_unavailable`이며 holiday row를 쓰지 않는다. 불완전한 broker payload를
휴장으로 오인해 한 달 coverage를 채우지 않는다.

planner manifest와 account-free stream manifest를 별도 private directory에 둔다. stream 쪽
account alias/sequence는 비워 두되 OAuth client credential은 public market API 인증을 위해
Keychain에서만 읽는다. 두 manifest는 경제값·선정·시간·기간·가상자금·slippage와 resolved absolute
`news_context_path`가 동일해야 하며 그 전체를 experiment SHA-256으로 잠근다. account identity와
다른 filesystem path는 hash에서 제외한다. 두 wrapper는 expected run ID, 양 끝 날짜, paper DB와
experiment hash를 config 및 DB lock과 대조하므로 중간 설정 변경은 이어서 실행되지 않는다. planner
wrapper는 Keychain 전에 config가 runtime UID 소유의 non-symlink regular file이고 group/other mode가
0인지 검사한다. 설치 plist의 `TOSS_SHADOW_ACCOUNT_FINGERPRINT`는 account sequence의 private
SHA-256 binding이며 planner가 매 hot reload마다 다시 계산해 비교한다. actual sequence와 fingerprint는
stream manifest/environment에 전달하지 않고 공개 evidence에도 남기지 않는다.

자동 선정된 하루 한 종목의 strict parser를 통과한 public trade/orderbook WebSocket frame과 usable
snapshot을 저장하고, 별도 USD cash ledger가 subsequent changed book·표시 depth·불리한 slippage로
causal full fill을 판정한다. SQLite는 WAL·`synchronous=FULL`이고 128번째 frame 또는 0.25초 기준을
넘긴 다음 periodic receive-loop tick에서 batch commit한다. idle connection도 기본 1초 poll tick으로
flush한다. disconnect·정상 종료에는 flush하지만 비정상 종료 시 최대 127개 pending frame이 유실될
수 있다. context 검증 뒤 OAuth/socket보다 먼저 들어오는 `start` event에서 durable
`paper_stream_instances` marker를 만들고, `last_seen_at`을 최대 초당 한 번 갱신하며 정상 final flush
뒤에만 닫는다. planner는 entry-expiry/force-exit 경계를 instance가 덮었는지 검사하고, 경계 전에
시작한 최신 open marker가 quote TTL 안에서 fresh하면 session finalize를 미룬다. TTL을 넘긴 orphan은
`stream_liveness_expired`로 닫는다. 경계 coverage가 없으면 `stream_coverage_incomplete`, coverage가
있지만 open process가 그 뒤 liveness를 잃었으면 `stream_process_interrupted` gap을 기록한 뒤
finalize한다. 새 stream은 이전 orphan을 `superseded_by_stream_restart`로 닫는다.

검증된 REST baseline 뒤 ACK된 선택 종목 trade/orderbook topic 각각은 quote TTL 안에 현재 generation의
fresh event를 내야 한다. 한쪽이 silent하면 `trade_topic_silent` 또는 `orderbook_topic_silent` gap으로
민감 구간을 invalid 처리하고 연결을 끊어 재접속한다. 늦은 첫 frame·disconnect·frame 검증 오류도
민감 구간에서는 invalid이며 malformed frame 자체를 저장하지 않는다. 이 DB는 gap-free exchange
tape가 아니다.

진입·target·stop trigger는 그 시점에 실제 수신한 trade frame만 사용한다. orderbook/baseline에
carry-forward된 last trade는 trigger가 아니며, trade의 broker timestamp가 진입 시작 또는 virtual
entry 이전이면 거부한다. trigger 뒤 별도 orderbook fill이라는 causal 경계는 그대로 유지한다.

fee는 plan에 잠근 broker commission의 leg별 적용과 configured fixed round-trip cost의 반씩만
모델링한다. 최소·규제 수수료의 별도 계산과 MAE/MFE, exposure, uptime/reconnect percentile,
slippage drag 통계는 구현 범위가 아니다. gap 전 position은 다음 fresh·충분한 depth의 bid로
가상 청산한 뒤 clean 성과에서 제외한다. 진입한 가상 포지션이 정규장 종료에도 체결 청산되지
않았을 때만 `UNRESOLVED`이며 final equity/return을 미확정으로 두고 이후 plan을 차단한다. 무진입
session이나 단순 coverage 누락을 `UNRESOLVED`라고 부르지 않는다.

기간의 모든 평일은 plan 또는 idempotent `MARKET_CLOSED` row로 coverage를 남긴다. 종료 뒤
예상일 누락 또는 zero-plan이면 `INCOMPLETE`이고, `WAITING`/`OPEN`/`UNRESOLVED`/`INVALID`/`BLOCKED`는
더 구체적인 상태로 우선 표시한다. 월 Discord payload에는 status, initial/current/final equity,
realized/clean P&L과 return, win/loss·승률·평균·expectancy·profit factor, fee/MDD/exit reason,
no-entry·invalid·unresolved·waiting, expected/covered/missing/holiday coverage와 journal/gap 지표가
들어간다. 표시 문자열은 핵심 수치만 축약한다.

Mac exact-five non-live topology는 유지한다. simulator를 planner와 selected-symbol stream에 내장하고
별도 daemon을 늘리지 않는다. planner·stream·approval·news 네 job은 redacted heartbeat producer를
갖고, 다섯 번째 watchdog은 네 heartbeat와 launchd 상태를 평가한다. planner heartbeat만 DB
`quick_check`, stream heartbeat만 ACK/baseline freshness를 싣는다. watchdog은 상태 변화 JSON을
출력하지만 Discord sender는 아직 배포 계약에 연결되지 않았다. stream LaunchAgent는
`news-context.json` `WatchPaths`만 사용하고 `RunAtLoad`·`StartInterval`·`KeepAlive`를 두지 않아 plan
전에는 idle이다. planner가 context를 생성·갱신할 때만 launchd가 실행/재실행하며 regular close
이후에는 context를 다시 쓰지 않아 만료 직후 재기동 churn을 만들지 않는다. stream은
immutable plan 조회를 위해 intraday state DB를 열고 journal·cash ledger는 별도 paper DB에 쓴다.
accountSeq, personal topic과 order adapter는 받지 않는다. paper 가격 계획에는 Discord 승인을 요구하지 않고,
daily/month-end payload만 기존 단일 허용 channel로 알린다. 현재 상태는 로컬 구현·회귀 검증 완료,
Mac exact-SHA 설치·실제 public WS journal·Discord smoke 대기이며 run은 아직 시작하지 않았다.

planner는 locked state DB sibling에 symbol·account 없는 owner-private
`stream-expectation.json`을 context보다 먼저 atomic write한다. expectation 실패는 strict blocker이고,
이후 context export가 실패하거나 파일이 삭제돼도 active expectation이 남는다. watchdog plist/wrapper는
같은 runtime directory의 `TOSS_WATCHDOG_CONTEXT_PATH`와 `TOSS_WATCHDOG_EXPECTATION_PATH`를 모두 받는다.
둘이 현재 뉴욕 session에서 active일 때만 stream heartbeat/process/ACK/baseline을 필수로 검사하고,
active expectation과 missing/idle/invalid context 조합은 `STREAM_CONTEXT_INVALID`로 fail-closed한다.
둘 다 정상 만료된 뒤 loaded WatchPaths job이 멈춘 것은 healthy idle이며 malformed expectation은
`STREAM_EXPECTATION_INVALID`다.
상세 데이터·체결·지표·합격 계약은
`docs/intraday-bracket-design.md`, 설치 경계는 `docs/macos-operations.md`가 정본이다.

## 2026-08-31 — macOS LaunchAgent clean handoff 보정

exact-SHA Mac 설치 후 planner가 Python preflight 전에 `zsh: parameter not set`으로 종료되는 것을
실제 Aqua LaunchAgent에서 확인했다. 원인은 중첩 `zsh -c` source가 single quote에 의해 여러 argv로
분할된 것이었다. planner preflight는 secret 없는 `env -i -> Python` 직접 실행으로 바꾸고,
planner/stream runtime은 non-secret internal 값만 clean `zsh -s`에 전달한 뒤 quoted heredoc source로
실행하도록 통일했다. inner shell이 internal 환경을 local로 복사해 unset한 다음 Keychain을 읽고,
credential은 shell local에서 export한 뒤 local을 비우고 Python을 직접 exec한다. 따라서 client
credential은 `/usr/bin/env` 또는 Python argv, plist와 로그에 들어가지 않으며 Python의 기존 pop
경계와 exact-SHA/config/simulation/account lock도 유지된다.

Mac synthetic release에서 두 wrapper 전체를 실행해 preflight 인자 전달, runtime Keychain lookup
도달, stdout 비움, `parameter not set` 부재, 없는 합성 Keychain 항목의 안전한 exit 69를 확인했다.
Windows 전체 회귀와 non-live gate도 다시 통과했다. 이 기록 시점에는 실패한 planner를 unload한
상태이며, 새 SHA 설치와 실제 Aqua 재시작·heartbeat·public market handshake 검증 전까지 run 시작을
주장하지 않는다.

## 2026-08-31 — zsh Keychain account 경계 수정 및 한 달 simulation 시작

Aqua LaunchAgent 진단에서 Keychain credential 자체는 일반 환경·clean environment·nested zsh 모두
비대화식 조회에 성공했지만, 실제 wrapper와 동일한 lookup만 실패했다. 원인은 zsh가
`$keychain_slug:toss_client_*`의 `:t`를 path modifier로 해석해 account 문자열을 변형한 것이었다.
planner와 stream wrapper를 `${keychain_slug}:toss_client_*`로 고치고, unbraced parameter 뒤 colon
suffix가 다시 들어오지 못하게 회귀 검사를 추가했다.

Windows 전체 회귀와 compile/import/dependency 검사를 통과했고, Mac exact-SHA에서는 OS network deny와
민감 환경 scrub이 포함된 non-live release gate `525 passed`, `pip check`, 두 wrapper의
`/bin/zsh -n`을 통과했다. 범용 Mac 전체 pytest에서 나온 5건은 release venv에 의도적으로 설치하지 않은
data용 `requests` 1건과 실제 Mac 기본 Keychain backend를 file backend로 가정한 gateway test 4건으로,
이번 planner/stream release gate와 분리해 판정했다.

설치돼 있던 legacy plist는 `/bin/zsh`와 script를 나눈 2-entry `ProgramArguments` 구조였으므로, 단순히
첫 항목만 바꾸면 새 script가 두 번 전달돼 exit 70이 발생했다. 저장소 manifest 정본과 동일한 단일
executable 배열로 두 plist를 원자 교체하고 release
`8bc17c199bdcc9125db7d0f063945e048b8e12c7`를 Aqua session에서 시작했다.

planner는 한 interval 뒤에도 heartbeat `OK`, mode `shadow`, `live_order_submission=false`, DB
`quick_check=ok`를 유지했다. plan/paper SQLite를 독립 read-only connection으로 다시 검사했고,
paper run은 `2026-08-31`~`2026-09-30` 양 끝을 포함한 기간, initial/current cash `USD 10,000`,
allocation `0.90`, risk `0.00225`, target `0.012`, stop `0.006`, blocker 없음으로 잠겼다. broker order,
order intent, execution order table은 모두 0건이다. 아직 selected-symbol context와 plan이 없는 장 전
상태라 stream은 heartbeat 없이 healthy idle이며, context가 생길 때만 해당 한 종목의 public
`trade:us`·`orderbook:us`를 구독한다. 진단용 LaunchAgent와 임시 probe/helper/status 파일은 검증 후
삭제했다. 상세 데이터·체결 계약은 `docs/intraday-bracket-design.md`, 설치·운영 경계는
`docs/macos-operations.md`가 정본이다.

## 2026-08-31 — Discord `/현황` 모의투자 조회 배포

- 기존 exact-five topology를 유지한 채 planner가 매 반복 뒤 승인 envelope와 같은 private directory에
  `paper-status.json`을 `0600` atomic replace하도록 추가했다. 입력은 기존 공개 월간 payload와 최신
  일일 payload만 explicit allowlist하며 account key, plan ID/hash, 전체 일자 배열, 경로, broker/raw
  frame은 기록하지 않는다. `mode=shadow`, `live_order_submission=false`, exact release SHA, 현재 boot
  hash, owner/private mode, exact schema와 130초 freshness가 모두 맞아야 읽을 수 있다.
- 기존 intents-0 approval Gateway에 guild-scoped `/현황` 하나를 등록했다. callback은 status 파일을
  읽기 전에 exact user/guild/channel을 검사하고 불일치하면 defer·응답·파일 read를 모두 생략한다.
  허용 context의 응답만 ephemeral이며 mentions를 비활성화했다. approval process에는 trading SQLite
  경로나 Toss credential, `turtle_bot` import를 추가하지 않았다.
- Windows 전체 회귀와 scrubbed non-live gate `546 passed, 5 skipped`를 통과했다. Mac exact-SHA
  `b9221a0b0285e30c078fb9d71ecc6cf4752321d3`에서는 network-denied non-live gate `551 passed`,
  `pip check`, compileall, zsh syntax와 plist lint를 통과했다. planner·selected-symbol stream·approval을
  같은 SHA로 전환한 뒤 planner heartbeat `OK`, approval heartbeat `IDLE`, stream healthy idle,
  paper run `ACTIVE`, 실주문 false를 재검증했다.
- Discord API read-back에서 guild command `/현황`은 정확히 1개이고 global command는 0개였다. 설정에
  사용한 bot token과 Discord ID는 출력·로그·저장소에 남기지 않았고 1회용 Aqua setup/verification
  helper와 결과 파일은 검증 후 제거했다. 실제 사용자 interaction 전송은 수행하지 않았으며, 사용자
  계정에서 지정 채널의 `/현황`을 한 번 호출하는 UI smoke만 남아 있다.

## 2026-08-31 — 자동선정 무후보 coverage와 `/현황` schema v2 배포

자동선정 결과가 비어도 첫 시도에서 하루를 끝내지 않는다. 실제 service interval을 기준으로 다음
반복이 계획 마감에 닿거나 넘어가는 마지막 유효 시도에서만, 모든 정상 전략 조건을 통과한 종목이
없으면 그 날짜를 immutable `NO_CANDIDATE`로 기록한다. 그 전의 빈 결과는 재시도하며 이후 후보가
생기면 정상 계획을 잠근다. daily/premarket candle 부족·stale·future와 quote/orderbook 데이터
품질 오류는 다른 후보를 계속 검사하되, 끝까지 계획이 없으면 첫 구체 오류를 다시 발생시키고
coverage를 만들지 않는다.

paper DB에는 run/date unique `paper_no_candidate_sessions`를 additive하게 추가했다. 계획·휴장과
상호 배타적이고 재시작 시 같은 날짜 기록은 idempotent하다. 월 coverage에는 관망일을 포함하지만,
실제 plan이 한 건도 없는 기간은 계속 `INCOMPLETE`라서 전략 표본 0개를 성공으로 가장하지 않는다.
공개 `paper-status.json`은 schema version 2로 올려 no-candidate count와 최신 covered day를 싣고,
`/현황`은 최신 관망일을 `조건 충족 종목 없음`으로 표시한다. 최종 관망은 runtime event와 `/현황`에
남지만 별도 Discord 관망 알림은 아직 없다. 사용자는 이전 schema의 `/현황` 실제 호출이 정상이라고
확인했고, 이번 schema v2는 writer-reader-renderer 회귀와 Mac의 fresh status artifact로 검증했다.

로컬 전체 회귀는 `816 passed, 6 skipped`, Windows non-live gate는 `573 passed, 5 skipped`, compileall과
dependency check도 통과했다. exact commit `535efda7feed7f677e58e917bf688e3a848710fb`를 Mac의 side-by-side
release로 설치했고 Mac network-denied non-live gate `578 passed`, dependency·wrapper syntax·Git
무결성 검사를 통과했다. 활성 계획·context·expectation이 모두 없는 계획 창 전 안전 시점에 planner,
stream, approval 세 job을 함께 정지하고 두 SQLite DB를 online backup API로 백업·`quick_check`한 뒤
같은 SHA로 전환했다. planner heartbeat `OK`, approval `IDLE`, stream healthy idle, status schema 2,
두 DB `quick_check=ok`, 가상현금 연속성, plan·broker order·intent·execution 0건과
`live_order_submission=false`를 재확인했다.

첫 전환에서는 설치 plist의 `ProgramArguments` 길이를 별도로 검증하지 않아 두 번째 인수가 남았고,
세 wrapper가 의도대로 `EX_SOFTWARE`로 fail-closed했다. DB·계획·주문 변경은 0건이었다. 세 job을 다시
내린 뒤 각 plist를 단일 wrapper 인수로 고치고 길이 1·mode 0600·lint를 검증해 재시작했다. 이후
배포 절차는 array item 치환 성공을 가정하지 않고 최종 인수 개수를 반드시 검사한다. 이 시점에도
설치된 topology는 planner·stream·approval `3/5`이며 news와 watchdog은 미설치다. 따라서
exact-five 완료나 live 준비 완료로 기록하지 않으며 상태는 계속 `LIVE_NO_GO`다.

## 2026-08-31 — OAuth gzip 진단, 토큰 재사용과 planner 안전 정지

미국 장전 plan window 첫 자동선정에서 planner가 `intraday_read_or_integrity_failure`로 막혔다. 본문,
credential, 계정값을 출력하지 않는 1회용 Aqua LaunchAgent probe로 traceback basename/line, route,
HTTP status와 allowlisted header만 확인했다. 실제 원인은 `POST /oauth2/token`의
`401 + Content-Encoding: gzip` 오류 본문을 기존 urllib transport가 압축 해제 없이 UTF-8 decode해
원래 상태를 `UnicodeDecodeError`로 가린 것이었다. gzip을 풀어 JSON error code만 다시 확인한 결과는
`invalid_client`였다. 임시 job/script/result와 bundle은 모두 제거했다.

transport는 identity/gzip을 strict 처리하고 JSON bytes를 직접 파싱한다. 비 JSON HTTP 오류는 원문
없이 generic code와 HTTP status만 보존하며, unsupported encoding·깨진 gzip·성공 응답의 invalid JSON은
본문을 담지 않는 전용 예외로 fail-closed한다. 서비스 loop는 config/shadow lock을 매번 다시 검사하되
설정이 같은 동안 `TossClient`와 access token을 메모리에서 재사용한다. credential/account/base URL 또는
transport guard가 바뀌면 client를 폐기한다. `401 invalid_client`는 같은 process에서 network 재시도하지
않아 매분 OAuth를 두드리지 않으며, 안전한 error code만 내부 진단에 남긴다. 이는 한 client당 유효 token
하나이고 재발급이 이전 token을 무효화한다는 공식 계약에도 맞춘다.

Windows 전체 회귀는 `825 passed, 6 skipped`, scrubbed non-live gate는 `582 passed, 5 skipped`였고,
Mac exact-SHA `e18c19fe67ae084ec0b5a6ddd36af89372e0e253` gate는 network-denied
`587 passed`, `pip check`, compileall, exact import, clean Git/fsck를 통과했다. 이 release는
side-by-side로만 준비했다. 기존 planner는 plan 0건, paper plan 0건, DB `quick_check=ok`를 확인하고
unload했다. approval은 기존 release에서 실행 중이고 stream은 no-context idle이다. Toss Open API
client ID/secret 재발급 또는 client 활성화와 비노출 one-shot 성공 전에는 plist를 새 SHA로 전환하거나
planner를 재시작하지 않는다. 따라서 현재 한 달 run은 정상 시작/완료로 간주하지 않으며 누락을
합성하지 않는다.

다음 병렬 실험은 현재 run을 중간 분할하지 않고 새 cohort에서 고정 두 레인, 총 가상현금 50:50,
서로 다른 symbol, 레인 간 이체 없음, 한 WebSocket의 네 exact topic, 두 sub-run ledger로 시작한다.
두 plan/cohort를 한 transaction으로 잠그고 레인별 결과와 합산 portfolio, distinct session을 따로
보고한다. 세부 계약은 `docs/intraday-bracket-design.md` 14.6절을 정본으로 둔다.
