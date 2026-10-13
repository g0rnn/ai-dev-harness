# M4-4 리허설 기록

기준일 2026-10-13. 빈 저장소에 키트를 붙여 [프로젝트 첫날 체크리스트](first-day.md) 1~9번을 수행하고, 기능 1개를
엄격 단계 절차(플랜 승인 → 구현 → 분리 리뷰 W2 → MR lint)로 병합한 기록이다. 키트 버전은 `d9a0826`(0.2.0 이후 main).
발견한 키트 결함은 후속 이슈로 분리했다(5장).

## 1. 환경

| 항목 | 값 |
| --- | --- |
| 플랫폼 | SSAFY GitLab(lab.ssafy.com, GitLab 18.11.5), 개인 네임스페이스 비공개 프로젝트 |
| Runner | 프로젝트에 쓸 수 있는 Runner 0개(공유 Runner 설정은 켜져 있으나 연결된 Runner 없음). 리허설 동안 로컬 Docker Desktop에 `gitlab/gitlab-runner` 컨테이너로 프로젝트 Runner를 등록(docker executor)하고 끝난 뒤 삭제 |
| 영역 | `backend` spring-java(Spring Initializr Boot 4.1.1, Java 21, Gradle), `frontend` react-ts(Vite 최신 템플릿, React 19, Node 24) |
| 트래커 | `jira`(가상 키 `REH`). 3장 F1 |
| 작업 PC | Windows 11, Git Bash, JDK는 IDE가 받은 것(`JAVA_HOME` 수동) |
| AI 세션 | 기능 구현은 대상 저장소에서 Claude Code 헤드리스 세션(`claude -p`, `acceptEdits`, 허용 명령 제한). 플랜 승인·확인은 사람(사용자), 분리 리뷰는 구현과 다른 세션 2개 |

gitlab.com 개인 프로젝트로 시작하려 했으나 `glab` 인증이 비대화형 셸에서 끝나지 않아(토큰 미저장, `--web` 대기) 이미
로그인돼 있던 SSAFY GitLab으로 바꿨다. 다음 프로젝트 환경과 같아 오히려 대표성이 높다.

## 2. 타임라인과 적용 비용

| 시각 | 단계 | 결과 |
| --- | --- | --- |
| 19:23 | 시작. 스택 골격 생성(Initializr, `npm create vite`), ESLint·Prettier·Vitest 추가 | 3장 F2~F5 |
| 19:25~19:28 | 체크리스트 1~4: 루트·영역 `init`, hooks 인터프리터, 첫 커밋 | `python3` 스텁 함정 재현 → `hooks.python=py`(F6) |
| ~19:40 | 플랫폼 전환, 프로젝트 생성, 6번 보호 브랜치 | `main` 직접 push 거부 확인 |
| ~19:45 | Runner 등록, 7번 CI 변수 | 프로젝트 액세스 토큰 2개(Reporter) → Masked 변수 |
| 19:45~20:07 | 5번 REH-1(엄격): 플랜 → 사람 승인 → 구현 → 분리 리뷰 A·B → MR !1 | 리뷰가 결함 3건을 MR 전에 잡음(4장) |
| 20:08 | MR !1 첫 파이프라인 | **의존성 감사가 MR을 막음**(4.2) |
| 20:10 | 반영 커밋(버전 상향) | 6개 job 모두 성공 |
| 20:25 | 사람이 MR !1 병합 | |
| 20:27 | 9번 측정 예약: 스케줄 생성·수동 실행 | `metrics.json`·`report.html` 아티팩트. 발송 변수 없음 → 발송 건너뜀 |
| 20:28~20:56 | REH-2(엄격) 기능: 헤드리스 플랜 → 사람 승인 → 헤드리스 구현 → **hooks `ask`** → 사람 확인 → 분리 리뷰 A·B → 헤드리스 반영 → MR !2 | 파이프라인 6개 job 성공 |
| 21:18 | 사람이 MR !2 병합. 측정 재수집 | 5장 측정값 |

- 체크리스트 1~9번: 64분(19:23~20:27). 스택 골격 만들기, 플랫폼 전환, Runner 등록, 사람 승인 대기 포함. 반나절 목표 안
- 엄격 기능 1개(REH-2): 플랜부터 MR까지 28분, 병합까지 50분(사람 대기 포함). 헤드리스 세션 3회 비용 합계 약 $7.8

## 3. Friction (적용 중 막히거나 손으로 맞춘 것)

| # | 내용 | 처리 |
| --- | --- | --- |
| F1 | `init --tracker`가 `jira`·`github`뿐이라 GitLab 이슈를 트래커로 쓰는 팀은 고를 값이 없다 | `jira` + 가상 키로 진행. 다음 프로젝트 트래커가 정해지면 판단 |
| F2 | PC에 Java가 없고 IDE가 받은 JDK만 있다. 영역 검증과 종료 검증 hook이 셸의 `java`에 의존 | `JAVA_HOME` 수동. 첫날 체크리스트 3번에 Java 확인을 더할지 검토 |
| F3 | Spring Initializr가 Boot 4.x만 제공 | 프로필 기본값(Boot 4 패키지)과 맞아 문제없음 |
| F4 | 최신 Vite react-ts 템플릿은 oxlint만 있고 ESLint·Prettier·Vitest가 없다. 프로필 검증 명령과 `eslint.harness.js`는 이 셋을 전제 | 팀이 직접 추가(키트 fixture 설정을 복사). ESLint 10과 `jsx-a11y` ERESOLVE는 README의 `overrides` 안내대로 해결 |
| F5 | Vitest는 테스트 0개면 실패해 `test:run`이 빈 저장소에서 실패 | 스모크 테스트 1개 추가 |
| F6 | Windows `python3` Store 스텁 | 체크리스트 안내대로 `hooks.python=py` |
| F7 | `init`이 `.claude/settings.local.json`을 `.gitignore`에 넣지 않는다 | 체크리스트 4번대로 수동 추가(#85) |
| F8 | 생성 Java 파일(LF)과 Windows Spotless 기본 줄끝(CRLF) 충돌. `init --force`가 `spotlessApply` 결과를 되돌린다 | `lineEndings 'UNIX'` + `*.java text eol=lf`(#83) |
| F9 | Windows 첫 커밋에서 `gradlew` 실행 비트가 빠져 Linux CI에서 실패할 상태 | `git update-index --chmod=+x`. 체크리스트 4번에 안내 추가 검토 |
| F10 | 생성 `frontend/AGENTS.md`가 Prettier `format:check` 위반 | `.prettierignore`에 추가(#84) |
| F11 | (정정) `.claude/hooks/__pycache__/`가 untracked로 남았다. 처음에는 hooks 탓으로 기록했으나, 생성 시각(20:28:07)이 리허설 진행자가 보호 경로 규칙을 보려고 `harness_common.py`를 `importlib`로 직접 import한 시각과 같다. hooks·`mr_lint.py`·`collect.py`는 `sys.dont_write_bytecode`를 켜 만들지 않는다 | 키트 결함 아님. 진행자 조작 흔적 |
| F12 | 종료 검증은 미커밋 변경만 본다. 깨진 코드를 커밋하고 끝난 세션은 검증되지 않는다 | 후속(#82, #8 관련) |

## 4. 실제로 막은 사례

### 4.1 hooks

| 사례 | 내용 |
| --- | --- |
| 보호 경로 `ask` | REH-2 헤드리스 구현 세션이 `backend/src/main/resources/db/migration/V1__create_todo.sql`을 `Write`하려다 `**/db/migration/`(ask)에 걸렸다. 헤드리스라 확인할 사람이 없어 편집이 거부됐고, 세션은 Bash 등으로 우회하지 않고 SQL 내용과 "사람이 할 일"을 보고했다. 사용자가 확인한 뒤 추가했다. `events.jsonl`과 `harness events summary` → MR 본문 `## hooks 기록`(`확인(ask): 1`) → 측정 지표 5로 이어졌다 |
| 보호 경로 `block` | 체크리스트 3번 확인에서 lock 파일 편집 거부(`frontend/package-lock.json`). 실제 세션 중 자연 발생 `block`은 없었다 |
| 종료 검증 | 자연 발생 0건. 오히려 F12(커밋 후 종료에서 검증이 돌지 않음)를 발견했다 |

규칙 문서의 효과: 헤드리스 세션이 대상 저장소의 `AGENTS.md`·영역 `AGENTS.md`만 읽고 판정(엄격)·트리거 3개·인수 테스트 22개·
사람 결정 항목 7개를 플랜으로 정리했고, 플랜 이탈과 테스트 변경을 플랜에 기록했다.

### 4.2 보안 검사

| 검사 | 결과 |
| --- | --- |
| 의존성 감사 | **MR !1 첫 파이프라인을 실패시켰다.** Boot 4.1.1 관리 버전의 `tomcat-embed-core` 11.0.24(CVE 3건 CRITICAL, 고친 버전 11.0.25)와 `jackson-core`·`jackson-databind` 3.1.5(CVE 5건 HIGH, 고친 버전 3.1.7). `tomcat.version`·`jackson-bom.version` 상향과 lockfile 재생성으로 통과. 수집기가 CI 실패 원인 `security`로 분류했다 |
| Secret 탐지 | 자연 발생 검출 0건(합성 검출은 키트 CI `rehearsal` job이 검증) |
| SAST | 자연 발생 검출 0건(같음) |
| 이미지 스캔 | 연결하지 않음. 이미지 빌드 job이 없으면 입력 없음으로 실패하는 조각이다 |

### 4.3 분리 리뷰

| MR | 리뷰 | 채택 | 대표 사례 |
| --- | --- | --- | --- |
| REH-1 | A 1/1, B 4/1(공격 26건) | 5 | Windows `core.autocrlf=true` 새 clone에서 Spotless 회귀(A·B 독립 발견), 생성 `AGENTS.md` Prettier 위반, 의존성 취약점(CI보다 먼저) |
| REH-2 | A 1/1, B 5/2(공격 44건) | 4, 후속 1, 기각 2 | 목록 로딩 중 추가한 항목이 늦은 조회 결과에 지워짐(B), 조회 실패 상태 혼동(A·B 독립 발견), 중복 제출 방지 테스트 부재 |

리뷰 B(공격)가 결함을 대부분 찾았고, A와 B가 독립으로 같은 결함을 찾은 경우가 MR마다 1~2건 있었다.

## 5. 측정값

수집기(`harness-metrics` 예약 job)가 병합 뒤 낸 W42 값이다.

| 지표 | 값 |
| --- | --- |
| 1 병합 후 누락 | 0 |
| 2 엄격 비율 | 100% (2/2) |
| 3 CI 실패 원인 | security 1 (의존성 감사), 나머지 0 |
| 4 판정 불일치 | 0 |
| 5 hooks 개입 | 차단 0 · 확인 1 · 종료 검증 실패 0, 보고 MR 2/2 |

## 6. 정리

리허설 뒤 프로젝트 Runner를 삭제하고 로컬 컨테이너·볼륨을 지웠다. 프로젝트 액세스 토큰 2개를 폐기하고 해당 CI 변수를 지웠다.
주간 스케줄은 Runner 없이 pending 파이프라인이 쌓이지 않게 비활성화했다. GitLab 프로젝트는 기록 근거로 남긴다.

## 7. 후속

- [#82](https://github.com/kiddo-psh/ai-dev-harness/issues/82) 커밋 후 종료에서 종료 검증이 돌지 않음(F12)
- [#83](https://github.com/kiddo-psh/ai-dev-harness/issues/83) Windows Spotless 줄끝(F8)
- [#84](https://github.com/kiddo-psh/ai-dev-harness/issues/84) 생성 `AGENTS.md` Prettier(F10)
- [#85](https://github.com/kiddo-psh/ai-dev-harness/issues/85) `settings.local.json` 무시 목록(F7)
- 이슈로 올리지 않은 것: F1 트래커(다음 프로젝트 결정 뒤), F2·F9 첫날 체크리스트 보강(Java 확인, 실행 비트), Secret·SAST 자연 검출 사례는 실제 프로젝트에서 계속 기록
