# Toss Trading Bot (코드네임: 이블린) ─ 동의 기반 실거래 아키텍처 구성안

> 2026-06-23 정정: 공개된 토스증권 Open API 문서와 사용자 사례 기준으로
> 개인 사용자의 실거래에 사업자/IP 등록이 보편적으로 필수라는 근거는 확인되지
> 않았다. live 실행의 정상 선결조건은 앱 ID/비밀키, OAuth 토큰, 계좌 헤더,
> 봇 자체 안전 가드이며, Toss 개발자센터 IP 등록을 요구하는 로컬 preflight는
> 두지 않는다.

## 1) 목적/전제
- 전제
  - 대상 사용자는 **본인 계정/타인 계정 API 키를 사용해 실거래**를 할 수 있다.
  - 수익 배분/과금 없이 “빌려주기/공동사용”이 목표이다.
  - 책임은 최종 실행자(=봇 운영자)가 1차이며, 동의자(계정 소유자)도 자신의 API 키 사용 범위를 인지해야 한다.
- 목표
  - 현재 구조에 맞춰 `동의 이력 + 계정 바인딩 + 실행 감사 + 정책 차단`을 강제해
    - 실거래 오남용 리스크를 줄이고
    - 분쟁 발생 시 추적 가능하게 만들고
    - 운영 상 허용/차단 정책을 일관되게 적용한다.

## 2) 현재 구조와의 정합성 요약
- 실거래 진입
  - `runtime.mode`(paper/shadow/live), `toss.live_enabled`, 긴급정지 플래그로 이미 실거래 경로가 분리되어 있다.
  - 실거래 submit은 `operations.py`와 `toss_live_adapter.py`에서 브로커 호출 전에 안전 가드가 선행된다.
- 안전 가드 존재
  - 미체결 주문, 시장 시간, 최소/최대 수량, 일일 주문/금액, 예외 상태 등을 체크한다.
- 약점(현재 미흡)
  - “동의(의사결정 주체)”와 “누가 어느 계정으로 어느 동의로 실행했는지”를 강제 인증/로그가 약하다.
  - 친구 계정 키 사용 시에도 실무적으로 남은 구멍: 키 회수·만료·동의 만료·비인가 실행의 추적성이 약함.

## 3) 제안 아키텍처 개요 (최소 구현 우선순위)
최종 목표는 4개 레이어 결합이다.

1. Consent Layer (동의/권한 레이어)
   - 실거래 시작 전에 동의 토큰이 있어야만 실행 가능.
   - 권한의 생명주기(발급, 만료, 폐기)를 중앙에서 관리.
   - 실행 트랜잭션마다 `consent_id`가 필수.

2. Account Binding Layer (계정 바인딩 레이어)
   - 어떤 실행이 어떤 Toss 계정 계열인지 운영적으로 고정.
   - `user_slug/account_slug` 기준으로 키를 주입하고 실행 컨텍스트에 주입된 값과 운영 로그를 강제 부착.

3. Guard/Policy Layer (정책 + 허용 조건)
   - 기존 `live_safety` 규칙 + 동의 기반 규칙을 OR가 아닌 AND로 결합.
   - 동의 미승인/만료/키 mismatch/위험 점수 초과는 즉시 live abort.
   - Toss가 실제로 네트워크 경로를 거절한 경우에도 live 선결조건이 아니라 장애 진단으로만 다룬다.

4. Audit & Forensics Layer (감사/사후조사 레이어)
   - 주문 시도/승인/실패/완료 모두 append-only 로그.
   - 추후 분쟁·점검 시 `누가, 언제, 어느 동의로, 어느 계정에서, 어떤 주문을` 즉시 재현 가능.

## 4) 핵심 데이터 모델

### 4-1) `ConsentRecord` (동의 레코드)
- `consent_id` (UUID)
- `granter_user_id` (동의한 사람, 계정 소유자)
- `granter_account_alias` (표시명)
- `toss_account_seq` (실제 계좌 식별자)
- `scope` (live_trading, order_max_count, order_max_notional, symbols, symbols_exclude)
- `risk_mode` (safe/pilot/limited/full)
- `allowed_operators` (운영자 리스트)
- `expires_at`
- `revoked_at`, `revoked_by`
- `signature_hash` (서명/동의 원문 해시)

### 4-2) `ConsentUsage` (실행 사용 이력)
- `usage_id`
- `consent_id`
- `operator_user_id` (실행자)
- `effective_account_seq`
- `runtime_mode` (live/shadow/paper)
- `requested_at`, `approved_at`, `completed_at`
- `decision` (allowed / rejected / revoked / expired)
- `policy_failures` (JSON)
- `request_meta` (ip, user-agent, client fingerprint)

### 4-3) `LiveExecutionLog` (실제 주문 감사)
- `order_intent_id`
- `consent_id`
- `operator_user_id`
- `granter_user_id`
- `account_seq`
- `symbol`, `side`, `qty`, `max_notional`, `result` (ACK/FAILED/CANCELLED/FILLED)
- `toss_raw_request_id`
- `raw_api_response`
- `block_reason`

### 4-4) `AccountBinding` (계정 바인딩)
- `user_slug`
- `account_alias`
- `toss_client_id_secret_ref`
- `account_seq`
- `status` (active/suspended/revoked)
- `last_health_check_at`
- `health` (api_key_valid, permission_ok)

## 5) 동의/실행 플로우 설계

### 5-1) 동의 등록 플로우
1. 동의자(친구) 계정 소유자가 `Consent` 등록 API 호출
2. 동의 범위(scope), 한도, 유효기간, 만료 동의 문구 저장
3. 동의 토큰(서면/문자/텍스트 동의) 원문 저장 + 해시 기록
4. 승인 상태 `active`로 전환

### 5-2) 실거래 시작 플로우
1. 운영자 UI에서 실행 버튼 클릭
2. 서버는 `consent_id + operator_user_id + selected_account_slug` 받음
3. Policy Check:
   - `toss.live_enabled == true`
   - `consent.active && !consent.expired && !consent.revoked`
   - `operator_user_id in allowed_operators`
   - `runtime.mode == live`
   - `account_binding.status == active`
   - Toss가 네트워크 경로를 거절한 이력이 있으면 VPN/프록시/클라우드 경로를 장애로 조사
4. 통과 시 Execution Context 생성 후 `operations`로 넘김
5. 주문 전/후 이벤트를 `LiveExecutionLog`에 append
6. 실패 시 즉시 감사 로그 + 메시지(원인코드 포함)

### 5-3) 종료/강제 정지 플로우
1. `revocation` 이벤트 수신 시 해당 계정/동의 토큰 기반 실거래 즉시 중단
2. `emergency_stop=true`로 하여 오탑/오작동 차단
3. 미체결 주문 정리 규칙 적용

## 6) 구성 변경(파일 기준) 제안
아래 파일/모듈은 기능 추가의 앵커. 이름은 신규 경로 기반 예시.

### 6-1) 신규 모듈
- `src/turtle_bot/consent/models.py`
  - dataclass 기반 `ConsentRecord`, `ConsentUsage`, `LiveExecutionLog`
- `src/turtle_bot/consent/service.py`
  - 동의 발급/해지/검증 API 로직
- `src/turtle_bot/consent/validator.py`
  - live 실행 전 체크 (만료, 허용자, scope, 범위)
- `src/turtle_bot/consent/storage.py`
  - 로컬 스토리지(JSON/YAML/SQLite 중 1개) 추상화
- `src/turtle_bot/ops/multi_user_gateway.py` 강화
  - 계정별 바인딩을 주입해서 실행 context 생성

### 6-2) 기존 파일 통합 포인트
- `src/turtle_bot/live_execution.py`
  - `resolve_runtime_context` 단계에서 동의 검증 호출 삽입
  - `LiveOrderOrchestrator` 호출 직전 `consent_id` 필수화
- `src/turtle_bot/operations.py`
  - `_submit_live_intents`에서 실행 전 policy guard 체인 강화
  - 실패 사유를 `block_reason`으로 남김
- `src/turtle_bot/toss_live_adapter.py`
  - 실행 실패 시 `error_code`, `error_message`, `network_path_diagnostic` 포함 구조화
- `src/turtle_bot/live_safety.py`
  - 기존 금액/수량/시점 규칙과 동의 규칙 AND 결합
- `src/turtle_bot/config.py`
  - `ConsentConfig`, `AccountBindingConfig`, `require_consent_for_live` 옵션 추가
- `src/turtle_bot/health.py`
  - 라이브 시작 가능 조건에 `consent_active`, `account_binding healthy` 항목 표시
- `tests/`
  - 동의 미존재/만료/해지 케이스
  - Toss 네트워크 거절 응답 진단/표시 케이스
  - 감사 로그 append 불변성 테스트

### 6-3) 운영 파일
- `config/local.example.yaml`
  - 동의 레이어 사용 옵션 on/off, 보관소 타입, 기본 감사 정책
- `docs/CONSENT_LIVE_RUNBOOK.md`
  - 친구 동의 체결, 만료 갱신, 키 교체, 사고 대응 절차

## 7) 보안/운영 정책 제안
- 키 관리
  - 계정 키는 사용자 단위 secret namespace로 분리.
  - 운영자가 수동으로 다른 키를 하드코드하지 못하게 UI/API에서 키 표시 제한.
- 동의 만료
  - 기본 만료 1~7일 권장.
  - 기간 도래 24시간 전 알림(옵션), 만료 후 자동 live_block.
- 네트워크 진단
  - Toss 개발자센터 IP 등록을 정상 실거래 준비 단계로 요구하지 않는다.
  - Toss가 실제로 IP/주소 거절 응답을 반환했을 때만 VPN, 프록시, 클라우드 경로 여부를 장애로 조사한다.
- 롤백/중지
  - 동의 해지/키 회수 시 실시간 자동 정지.
  - 계정 단위와 consent 단위를 동시에 검사.
- 사고 대응
  - 비정상 주문/권한 실패 3회 이상 발생 시 계정 자동 일시 정지.

## 8) API 제안 (최소 세트)

### 8-1) 동의 관리
- `POST /api/consent` : 동의 생성
- `GET /api/consent` : 운영자/동의자별 동의 목록
- `POST /api/consent/{id}/revoke` : 철회
- `POST /api/consent/validate` : 실행 직전 검증

### 8-2) 실행 컨텍스트
- `POST /api/live/pilot/prepare` : 실행 전 준비, consent 바인딩 및 정책 검사
- `POST /api/live/pilot/start` : 실행 트리거
- `GET /api/live/execution/{usage_id}` : 실행 이력 조회
- `GET /api/live/audit` : 실행 감사 조회(필터: consent/operator/time/symbol)

## 9) 우선 구현 로드맵 (총 3단계)

### Phase 0 (즉시, 1~2일)
1. 실행 전 consent_required 플래그 추가
2. consent_id 없으면 live 시작 금지
3. 로그에 consent/operator/account_seq 필수 기록
4. Toss 네트워크 거절 응답은 원문 코드/메시지를 기록하되 정상 준비 조건으로 승격하지 않음

### Phase 1 (기능 완성, 3~5일)
1. ConsentStorage + ConsentValidator 추가
2. multi_user_gateway 계정 바인딩 강제
3. 대시보드에 동의 상태 표시 및 만료 카운트다운

### Phase 2 (운영 안정화, 1주)
1. 감사 로그 조회 UI
2. 계정/동의 단위 자동 정지 정책
3. 테스트 시나리오 20~30개 추가
4. 장애 대응 runbook 탑재

## 10) 리스크 및 한계
- API 키/동의는 **계정 소유자-운영자 간 신뢰 관계**가 핵심.
- 기술적으로는 실거래 통제가 가능해도, 법적 책임 분리는 결국 운영 프로세스와 기록 증빙이 같이 가야 함.
- 특정 네트워크, VPN, 프록시, 클라우드 egress에서 Toss가 edge 차단을 할 수 있으므로,
  실패 시 컨테이너 내부 공개 IP와 요청 경로를 확인해야 한다.

## 11) 이번 단계 기준 결론
- 지금 구조는 “기술적 실거래 실행”은 준비돼 있으나,
  `동의 증빙·계정별 권한 경계·감사 추적`이 빠진 상태로 “친구와 공유 + 책임 분리”에 최적화되진 않음.
- 위 4개 레이어를 붙이면, 네가 원하는 방식(돈 안받고 빌려 쓰기, 동의 기반 운영)에 맞는 실거래 체계로 전환 가능.
- 다음 작업은 Phase 0부터 적용해 “동의 토큰 없으면 실거래 불가”를 가장 먼저 강제하는 것이 안전하다.
