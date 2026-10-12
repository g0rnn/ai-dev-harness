# 개발 계획

기준일 2026-09-29. 이 문서는 키트 완성(2단계)까지의 목표, 마일스톤, 작업 목록과 범위 밖을 정의한다.
마일스톤이 끝날 때마다 완료 기준 충족 여부와 측정값을 이 문서에 추기한다.

## 1. 목표

한 문장: **새 저장소를 만든 날부터 AI 산출물이 표준을 벗어나면 기계가 막고, 그 효과를 숫자로 보이는 키트.**

| 목표 | 측정값 | 기준 |
| --- | --- | --- |
| 적용 비용 | 빈 저장소에 키트를 붙여 첫 MR을 엄격 단계 절차까지 통과시키는 시간 | 반나절 이내 |
| 방어 효과 | 병합 후 뒤늦게 발견된 누락 수, 보안 검사가 잡은 건수 | 이전 프로젝트 대비 감소, 검사 4종 모두 실제 검출 사례 1건 이상 |
| 팀 수용 | 절차를 건너뛴 MR 비율, 팀원이 hooks를 끈 횟수 | 0에 가깝게 |

측정 항목은 1주차에 자리를 잡는다. 플랫폼 API(병합 MR·이슈·CI job)에서 다음 넷을 뽑는다(M4-1 `harness metrics collect`, `core/metrics/README.md`).

1. 병합 후 발견된 누락 수 (`escaped-defect` 라벨 이슈·MR의 원인 MR 병합 주. 리뷰 템플릿 6장 측정 칸은 보조)
2. 엄격 단계 MR 비율 (MR 본문 `## 판정`과 변경 파일 재판정 중 높은 쪽)
3. CI 실패 원인 분류 (테스트 실패 · 포맷 · 인프라 · 타임아웃 · 보안 · 미분류)
4. 판정 스크립트와 사람 판정의 불일치 수 (본문 판정 ≠ 재판정)

## 2. 단계

| 단계 | 기간 | 산출물 | 완료 기준 |
| --- | --- | --- | --- |
| 1. 착수판 | 2주 | 템플릿 묶음, hooks, 보안 검사 4종 CI | 빈 저장소에 붙여 첫 MR이 엄격 단계 절차까지 통과 |
| 2. 키트 완성 | 다음 프로젝트 기간 중 6~8주 | 1단계 + 판정·리뷰 자동화, Scaffold, 측정 수집기, 가이드와 리허설 기록 | 실제 프로젝트 한 개에서 전 기간 사용, 전후 측정값 확보 |
| 3. 플랫폼 | 별도 프로젝트 | 포털, 승인·이력, 운영 관리, 거버넌스 RAG | 이 계획의 범위 밖. 2단계 결과가 좋을 때 별도 계획으로 올린다 |

## 3. 마일스톤과 작업 목록

각 마일스톤은 그 시점에 다음 프로젝트에 붙여도 쓸 수 있는 상태여야 한다. 작업은 GitHub 이슈로
만들고 이 목록의 번호를 이슈 제목에 붙인다.

### M1. 착수판 (1~2주차)

완료 기준: `init`으로 빈 저장소에 템플릿·hooks·보안 CI가 들어가고, 첫 MR이 엄격 단계 절차까지 통과한다.

- [x] M1-1 저장소 초기화, 구조, 라이선스, 자기 설정(`harness.json`)
- [x] M1-2 feelm 규칙 문서 이관·일반화 → `core/templates/` (이관 대장 `docs/migration-ledger.md`)
- [x] M1-3 `harness.py init/check`, 자기 적용(`--self`), 드리프트 검사, 단위 테스트, GitHub Actions 리허설
- [x] M1-4 영역 `AGENTS.md` 템플릿 (`core/templates/AREA-AGENTS.md`): 3단계 판정표·트리거·플랜/구현/리뷰 절차·W1/W2 편성을 스택 중립으로 정리. `init --area <dir> --verify-cmd ...`로 생성
- [x] M1-5 hooks 1차: `.claude/settings.json` 생성, 보호 경로(계약 문서·마이그레이션·CI 정의·lock 파일) 수정 시 차단 또는 경고, 세션 종료 시 지정 검증 명령 실행 (#5, 후속 #8)
- [x] M1-6 보안 검사 CI 조각 4종 (`core/ci/gitlab/`): Secret 탐지(gitleaks), 의존성 감사(npm audit · OWASP dependency-check 또는 Gradle 대안), SAST(semgrep), 컨테이너 이미지 스캔(trivy). 각각 `include: remote:`로 끌어 쓸 수 있는 단독 YAML
- [x] M1-7 키트 저장소에 M1-5·M1-6을 자기 적용 (GitHub Actions에는 같은 도구를 직접 호출) (#16)
- [x] M1-8 리허설 확장: 리허설 대상에 hooks 설치와 보안 검사 실행까지 포함 (#18)
- [x] M1-9 설치 가이드 초안 (`docs/install.md`): 새 저장소·기존 저장소·끄는 법, Windows의 `hooks.python` 설정(`python3`이 Microsoft Store 별칭이면 hooks가 조용히 꺼진다)
- [x] M1-10 적용 전 계약 리뷰(엄격): 다음 프로젝트에 붙이기 전에 소비자가 생기면 바꾸기 비싼 표면을 한 번에 본다. `harness.json` 스키마(`areas` 포함), CLI 인자, 자리표시자 이름, 판정 식별자(`lite`·`standard`·`strict`), 기본 트리거·리뷰 관점을 생성 시점에 설정으로 굳힐지(#1 리뷰 F2), `harness_version` 올림 정책(#1 리뷰 F5), `hooks` 스키마와 매니페스트 `base`·`render` 필드, 자리표시자 `hook_python`, 기존 `.claude/settings.json` 병합 여부(#5 Q3), `check`가 관리 디렉터리(`.claude/hooks/` 등)의 여분 파일도 보고할지(#5 리뷰 R18), CI 조각의 소비자 표면(job 이름 `harness-*`, 변수 `HARNESS_SCAN_IMAGE`·`HARNESS_SCAN_ARCHIVE`·`HARNESS_SEMGREP_CONFIG`·`HARNESS_AUDIT_SKIP_DIRS`, 조각 경로, 태그 참조 방식, #12), Jenkins 지원 시 공통 검사 정책과 CI별 구현의 경계 (#20)

### M2. 판정과 리뷰 (3~4주차)

완료 기준: diff 기반 3단계 자동 판정이 MR 댓글로 붙고, MR 본문 필수 절이 비면 CI가 실패한다.
판정 댓글은 토큰 변수(`HARNESS_COMMENT_TOKEN`, 키트는 `GITHUB_TOKEN`)가 있을 때만 붙고, 없으면 job 로그와 `mr-lint.json`
아티팩트로 남긴다. `plans/`는 커밋하지 않아 CI가 볼 수 없으므로 플랜·리뷰 검사의 CI 쪽은 MR 본문 측정 칸 검사다.

- [x] M2-1 판정 스크립트 `harness judge`: 변경 파일 목록과 경로 규칙(`harness.json`의 `areas[].trigger_paths`·최상위 `judge`, 문장 트리거는 사람 확인)으로 경량·표준·엄격(`lite`·`standard`·`strict`)을 산출. 사람 판정과 다르면 기록(측정 4번, M2-2) (#27)
- [x] M2-2 MR 본문 lint CI 조각: `Closes`, 검증 절(방법·결과·미검증), 영향 범위 체크박스 중 하나 이상 필수. 엄격 판정이면 플랜 요약·리뷰 결과 절 필수. diff에 `plans/` 경로가 있거나 `.gitignore`에 `/plans/`가 없으면 실패(영역 `AGENTS.md` 5장의 `[ci]` 규칙) (#37)
- [x] M2-3 플랜·리뷰 파일 lint `harness lint-plans`: 템플릿 절 누락, 측정 칸 빈칸, 인수 테스트 표 0행 검출. 로컬 CLI이고 hooks 연결은 후속(#22). `plans/`는 커밋하지 않아 CI가 볼 수 없으므로 CI 쪽은 M2-2 MR 본문의 측정 칸 검사가 대신한다 (#38)
- [x] ~~M2-4 Claude MR 리뷰 CI 조각 이관~~ 철회: feelm에서 효과가 낮아 거의 쓰지 않던 기능이라 키트에서 제거 (#32 이관, #43 제거)
- [x] M2-5 리뷰 프롬프트 단일화: 로컬 W1/W2 리뷰 관점과 CI Claude 리뷰 시스템 프롬프트를 한 파일(`core/templates/review-perspectives.md`)에서 생성. CI 절과 `review_perspectives_ci`는 M2-4 철회 때 함께 제거 (#29, #43)
- [x] M2-6 CI 실패 원인 분류기 1차: job 로그를 규칙으로 분류해 JSON 산출 (측정 3번) (#28)

### M3. Scaffold (5~7주차)

완료 기준: 세 프로필(웹 백엔드 `spring-java`, 웹 프론트 `react-ts`, Android 폰+워치 `android-kotlin`)에서 도메인·화면 생성
명령이 있고, 생성물이 컨벤션 테스트와 CI를 통과한다. 모바일은 이전 프로젝트 근거가 없어 규칙을 처음 정하므로 1주를 더 둔다.

- [x] M3-1 프로필 구조 확정: `profiles/<name>/{profile.json,scaffold/,conventions/}`, `init --area --profile`, `harness scaffold`, 계층 규칙 설정(`layers`·`allow`)과 고정 규칙 끄기(`disabled_rules`) (#47)
- [x] M3-2 `spring-java` 프로필: 도메인(패키지·Controller·Service·Repository·DTO·테스트 뼈대) 생성, 컨벤션 테스트(계층 의존 방향, 트랜잭션 경계 규칙). 예외 처리는 공통 예외 사용 예시만 두고(J-4) 테스트는 후속 (#49)
- [x] M3-3 `react-ts` 프로필: 화면(라우트·페이지·API 클라이언트·테스트 뼈대) 생성, 컨벤션 테스트(공용 컴포넌트 경계, 접근성 lint) (#50)
- [x] M3-4 `android-kotlin` 프로필(폰 + Wear OS): M3-4a 기본값·고정 규칙·최소 템플릿, M3-4b 냠냠코치 아키텍처 ADR에 맞춘 Hilt 템플릿(화면·Repository·UseCase)과 컨벤션 전용 프로필 `android-konsist`(고정 규칙 8개·feature 계층 설정) (#51, #59)
- [x] M3-5 리허설 확장: 프로필별로 생성 → 컨벤션 테스트 → 영역 검증 명령 실행(키트 `ci`의 `profiles` job, 위반 시나리오 포함) (#56)

### M4. 측정과 가이드 (7~8주차)

완료 기준: 주간 요약이 발송되고 HTML 리포트가 게시되며(요약에 리포트 링크), 설치·해제 가이드와 리허설 기록, 전후 측정값 표 1개가 있다.

- [x] M4-1 측정 수집기 `harness metrics`: 플랫폼 API(병합 MR·이슈·CI job)에서 4개 지표 추출 → JSON (#65)
- [x] M4-2 주간 요약 발송: feelm의 Jira 일일 요약 발송 코드를 이관해 Mattermost·Slack webhook으로 전송 (#69, `harness metrics notify`와 GitLab 예약 파이프라인 조각 `core/ci/gitlab/metrics.yml`)
- [x] M4-3 설치·해제 가이드 완성, 프로젝트 첫날 체크리스트 (#71, `docs/first-day.md`, 설치 가이드 9·10장)
- [ ] M4-4 리허설 기록: 빈 저장소 부트스트랩 → 기능 1개 엄격 단계 통과 → hooks와 보안 검사가 실제로 막은 사례 기록
- [ ] M4-5 전후 비교표: 이전 프로젝트(feelm) 수치와 키트 적용 프로젝트 수치
- [x] M4-6 주간 HTML 리포트: `harness metrics`가 JSON과 함께 정적 HTML 한 장(4개 지표의 주별 추세 — 사후 누락 수, 엄격 비율, CI 실패 원인 분포, 판정 불일치)을 생성해 CI 아티팩트나 Pages로 게시한다. 서버 없이 표준 라이브러리로 만들고, 주간 요약 메시지에 링크를 단다. 주 단위 집계는 M4-1 JSON에 둔다(M4-1 의존). 상시 대시보드(Grafana 등)는 범위 밖. 렌더러·`metrics report` 명령은 #67, 아티팩트 게시(90일)와 요약 메시지의 리포트 링크는 M4-2 조각(#69). Pages 게시는 SSAFY 지원 확인 뒤 선택
- [x] M4-8 Claude Code OpenTelemetry 선택 가이드: 설치 가이드 14절. 켜는 위치(저장소 설정은 무시되므로 사용자·managed 설정), 키트 지표와 겹치는 신호(`hook_registered`로 hooks를 끈 세션), 내용 기록 변수를 끈 채로 두는 규칙, 끄는 법. 수집기 구성은 범위 밖 (#76)

## 4. 백로그 (마일스톤 미배정)

- Jira 업무 생성 자동화(feelm `scripts/jira/`) 이관. 다음 프로젝트가 Jira를 쓰면 M2에 편입
- 문서 신선도 검사: 문서의 명령·경로가 저장소에 존재하는지 검사하는 lint
- 배포 승격 단계(staging), 수동 승인 job, 배포 후 스모크 테스트 CI 조각
- `harness update`: 키트 새 버전과 대상 저장소의 diff를 보여 주고 선택 반영
- 엄격 단계 완화·강화 규칙 자동 제안 (엄격 MR 5건 연속 누락 0 → 완화 제안)

## 5. 범위 밖

포털 UI, SSO, 모니터링, 거버넌스 RAG, 비활성 서비스 Lifecycle. 회사 인프라 없이 만들면 흉내가 되므로
2단계 결과를 본 뒤 3단계 계획에서 다룬다.

## 6. 원칙

- **일반화를 미리 하지 않는다.** 두 번째 적용 전에 추상화하지 않는다. 다음 프로젝트 스택 하나만 겨냥한다.
- **기본은 가볍게, 엄격은 트리거로.** 경량·표준 경로는 hooks와 CI만 조용히 돈다. 플랜 승인과 분리 리뷰는 판정이 엄격일 때만 켠다.
- **측정을 첫 기능으로.** 숫자로 못 보이면 취미다.
- **키트가 키트를 쓴다.** 만들어진 즉시 이 저장소에 적용하고, 불편은 이슈로 남긴다.
