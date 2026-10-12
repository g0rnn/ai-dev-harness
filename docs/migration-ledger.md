# feelm에서 이관한 항목 대장

기준 커밋: feelm `origin/develop` 2026-09-29 (`2b677706`). 원본은 읽기만 했고 수정하지 않았다.
"걷어낸 것"은 프로젝트 고유 명사·경로·수치이며, 규칙의 뜻은 바꾸지 않았다. 이관 문서를 공개
저장소에 두는 것은 팀에 알린다(ADR-0001 후속).

## 이관 완료 (M1-2)

| 원본 (feelm) | 키트 위치 | 걷어낸 것 · 바꾼 것 |
| --- | --- | --- |
| `AGENTS.md` | `core/templates/AGENTS.md` | 관련 문서 링크를 `{{related_docs}}`로, Jira·MR 명사를 자리표시자로, 9장 Jira 자동화 절차를 일반 규칙으로 축소 |
| `CLAUDE.md` | `core/templates/CLAUDE.md` | 없음 |
| `docs/README.md` (문서 역할·우선순위) | `core/templates/docs/README.md` | 설계 문서 링크와 pipeline·backend 영역 명칭 제거, 협업 문서 목록을 키트가 생성하는 4개로 축소 |
| `docs/ai-collaboration.md` | `core/templates/docs/ai-collaboration.md` | worktree 예시의 프로젝트 키를 자리표시자로 |
| `docs/development-workflow.md` | `core/templates/docs/development-workflow.md` | Jira·GitLab·develop·main을 자리표시자로 |
| `docs/git-convention.md` | `core/templates/docs/git-convention.md` | 브랜치·커밋 예시의 프로젝트 키 제거, 커밋 Scope 표를 채워 넣는 빈 표로, 모노레포 디렉터리 절 제거, 개행 절 축약 |
| `docs/secret-environment-variables.md` | `core/templates/docs/secret-environment-variables.md` | GitLab 변수 절을 플랫폼 중립으로, 11장(리포트 이미지 서명 Secret) 제거, `infra/README.md` 참조 제거 |
| `docs/templates/plan.md` | `core/templates/docs/templates/plan.md` | "3장 판정"을 "판정"으로, Jira 칸을 트래커 자리표시자로 |
| `docs/templates/review.md` | `core/templates/docs/templates/review.md` | 리뷰어 B의 "공격 시도 기록" 표를 추가(feelm은 pipeline 템플릿에만 있었음) |
| `docs/adr/0000-template.md` | `core/templates/docs/adr/0000-template.md` | 관련 항목의 FR/API/ERD 표기를 일반 표현으로 |
| `docs/adr/README.md` | `core/templates/docs/adr/README.md` | feelm ADR 목록과 예시 제거, 규칙만 유지 |
| `.gitlab/merge_request_templates/Default.md` | `core/templates/merge-request/Default.md` | `Closes` 예시를 자리표시자로. GitHub에는 `.github/PULL_REQUEST_TEMPLATE.md`로 생성 |
| `.gitlab/merge_request_templates/Release.md` | `core/templates/merge-request/Release.md` | 없음 |
| `.gitignore`의 `/plans/`·`/*/plans/`·`.env` 규칙 | 키트 자신의 `.gitignore` | 소비자 저장소용 gitignore 조각은 M1-5에서 hooks와 함께 생성 |

## 이관 완료 (M1-4)

| 원본 (feelm) | 키트 위치 | 걷어낸 것 · 바꾼 것 |
| --- | --- | --- |
| `backend/AGENTS.md`, `pipeline/AGENTS.md` | `core/templates/AREA-AGENTS.md` (`init --area`로 생성) | 두 문서의 공통 뼈대(3단계 판정·트리거·플랜/구현/리뷰·W1/W2)만 스택 중립으로. 기준 문서·검증 명령·트리거·리뷰 관점은 `harness.json`의 `areas[]`에서 채움. 단계 이름을 경량·표준·엄격으로, "리뷰 게이트"를 "조건부 관점"으로. 경량 단계는 플랜 없이 시작 보고와 MR에 기록(pipeline 방식). 테스트 전용 변경은 트리거에서 제외. 완화·강화 규칙은 "검토 후보"로. 루트 `AGENTS.md`와 겹치는 규칙은 반복하지 않음. 규칙마다 강제 주체 `[hook]`·`[ci]`·`[사람]` 표시 |

## 이관 완료 (M4-2)

| 원본 (feelm) | 키트 위치 | 걷어낸 것 · 바꾼 것 |
| --- | --- | --- |
| `scripts/jira/notify_mattermost.py` | `core/metrics/notify.py` (`harness metrics notify`) | 발송부(`MattermostClient`의 login·팀/채널 조회·posts·logout, `api_request`, https 검사)만. Jira 조회·일일 작업 요약 본문·`.env` 읽기·이모지 서식 제거(Jira 조회는 백로그 그대로). 변수 이름을 `MATTERMOST_*`에서 `HARNESS_MATTERMOST_*`로, incoming webhook 모드(`HARNESS_NOTIFY_WEBHOOK_URL`)와 `--mode` 추가. 리다이렉트를 따르지 않고(3xx도 오류) 오류 메시지에 응답 본문 대신 HTTP 상태·호스트만 남긴다. 본문은 수집기 `metrics.json`의 주간 4개 지표 요약 |

## 이관 철회 (M2-4, #32 → #43)

feelm `infra/claude-review/`의 Claude MR 리뷰(수집·생성·게시·상태·인증 확인·네트워크 가드·샌드박스 점검 스크립트와 테스트,
systemd 유닛, `.gitlab/ci/common.yml`의 리뷰 job, README)를 #32에서 `core/ci/claude-review/`·`core/ci/gitlab/claude-review.yml`로
옮겼다가 #43에서 모두 제거했다. feelm에서 효과가 낮아 거의 쓰지 않던 기능이라서다(2026-10-03 사용자 결정). `harness.json`의
`claude_review` 블록, 리뷰 관점 원본의 CI 절과 `review_perspectives_ci`도 함께 없앴다. 이관 내용은 #33 커밋(`faccd28`)에 남아 있다.

## 이관 예정

| 원본 (feelm) | 키트 위치(예정) | 마일스톤 | 비고 |
| --- | --- | --- | --- |
| `frontend/AGENTS.md` | `profiles/react-ts/` 검증 명령 | M3-3 | Prettier 검사 규칙은 프로필의 verify 정의로 |
| `.gitlab/ci/*.yml`의 MR 검사 job 패턴 | `core/ci/gitlab/` | M1-6, M2-2 | 경로 필터·resource_group·interruptible 패턴만 참고. 배포 job은 이관하지 않음 |
| `scripts/jira/` (업무 생성 자동화) | 백로그 | — | 다음 프로젝트가 Jira를 쓰면 편입 |
| `docs/backend-convention.md` | `profiles/spring-java/` 컨벤션 테스트 | M3-2 | 문서가 아니라 검사로 옮긴다 |

## 이관하지 않는 것

- `docs/requirements.md`, `docs/api/`, `docs/erd.md`, ADR 본문: feelm의 설계 내용
- `infra/scripts/`의 배포·롤백·캐시 스크립트: 서버 구조에 종속
- `backend/scripts/`의 데이터 적재 스크립트: 도메인 종속
