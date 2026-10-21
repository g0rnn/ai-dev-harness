# 키트 기록 자동 전송과 Codex 비용·토큰 — 리뷰

2026-10-21. 표준 W1. 구현 세션 자체 점검 후 별도 Codex 세션이 코드·diff·플랜·검증 기록을 독립으로 대조했다. 기준 `origin/main` 4765389. 독립 리뷰는 테스트나 서버 쿼리를 재실행하지 않았다.

## 1. 읽기 범위

| # | 변경 단위 | 파일 | 참조한 계약 절 |
| --- | --- | --- | --- |
| U1 | 종료 hook·생성본 | core/hooks/session-end.py, .claude/hooks/session-end.py | hooks README SessionEnd, AGENTS.md 3·6장 |
| U2 | 기존 전송 경계 | core/metrics/telemetry.py | push 잠금·성공 위치만 전진·허용 필드·Codex 워터마크 |
| U3 | 대시보드 | core/observability/grafana/dashboards/harness-agentops.json | observability README 키트 기록·패널 표, Grafana LogQL·표현식 문서 |
| U4 | 검증 | tests/test_telemetry.py, .github/scripts/observability_smoke.py | 플랜 T1~T6 |
| U5 | 사용자 문서 | hooks·metrics·observability README, docs/install.md | 설치 14.5절, 기존 OTLP/HTTP 계약 |

## 2. 테스트 실효성

| 플랜 # | 실제 테스트 | 대응 | 기대값 변경 기록 확인 |
| --- | --- | --- | --- |
| T1 | AutoPushTest.test_no_endpoint_or_missing_copy_does_not_spawn | 엔드포인트·복사본 누락 | 신규 |
| T2 | AutoPushTest.test_detached_without_shell_or_pipes, test_record_precedes_push_and_failures_stay_silent | 인자 배열·DEVNULL·인증 환경 상속·대기 없음·기록 순서 | 신규 |
| T3 | AutoPushTest.test_record_precedes_push_and_failures_stay_silent | 기록·프로세스 시작 예외를 따로 주입 | 신규 |
| T4 | AutoPushIntegrationTest 2개 | 실제 설치본, HTTP 500 후 재시도, Codex 중복 없음 | 신규 |
| T5 | FragmentConfigTest, CI 스모크 | 정적 원천·통화 검사 통과. 운영 Grafana에서 쿼리 88개·합계 검증 통과. CI의 합성 데이터 스모크는 PR에서 확인 | CI 예상값을 새 패널에 맞춰 추가 |
| T6 | 전체 단위 테스트, check --self | 663개 중 658개 통과·기존 심볼릭 링크 5개 환경 제약 건너뜀, check 통과 | 전체 발견 수와 모듈별 실행 수 일치 |

## 3. 발견 사항

독립 리뷰에서 수정이 필요한 재현 가능한 결함을 발견하지 못했다.

- SessionEnd 기록 순서, 프로세스 분리와 실패 경로를 기존 push 잠금·워터마크와 대조했다.
- 세션별 누적값, 기간 전 기준값 차감, USD 제한과 빈 원천 합산을 확인했다.
- CI 합성 입력의 비용 0.0067, 합계 1.2567, input 2400, output 190, 기간 내 Codex 사건 7개를 산식으로 확인했다.
- 환경변수·요금표·CLI 계약과 차단 로직은 유지된다.

## 4. 공격 시도 기록

W1이라 해당 없음. 실패 경로는 T1~T4의 실제 전송·예외 주입으로 확인했다.

## 5. 단위 간 정합성

- 계약: 환경변수·CLI·로그 필드·요금표 그대로. 비용 합계는 USD Codex 기록만 더하며 Claude Code 키트 기록은 제외한다.
- 트랜잭션 경계: 종료 기록 후 자식 시작. 기존 push가 공용 Git 디렉터리 잠금과 성공 위치를 관리한다.
- 오류 코드: SessionEnd는 항상 0, 표준 입출력 없음. push의 부분 거절·실패 처리 정책 그대로.
- Secret: 인자 배열과 변경 diff에 실값 없음. 인증은 환경 상속, 자식 출력 DEVNULL.
- 한계: 기준값이 보관되지 않았거나 전송이 늦으면 기간 귀속은 근사. Codex만 사용하는 PC에는 자동 트리거가 없다.

## 6. 측정 칸

| 항목 | 값 |
| --- | --- |
| 읽은 기준 문서 절 수 | 8 |
| 열린 조건부 관점 (동시성·트랜잭션 / 인가) | 동시성·트랜잭션 / 인가 |
| 발견 사항 수 (참고 제외 / 참고) | 0 / 0 |
| 리뷰 세션 모델 | Codex(구현과 분리된 W1) |
| PR 리뷰에서 뒤늦게 발견된 누락 (병합 후 기입) | 병합 전 |

## 7. 처리 (작성자가 반영 뒤 채운다)

플랜 승인: 해당 없음(표준). 사용자 2026-10-21 배포·커밋·Push·PR 생성·조건 충족 시 병합 승인.

발견 사항이 없어 처리할 행은 없다. 운영 대시보드 쿼리·합계는 검증했고, CI 합성 입력 스모크는 PR 결과를 확인한다.

| # | 결함 | 심각도 | 발견 | 처리 | 근거 |
| --- | --- | --- | --- | --- | --- |
