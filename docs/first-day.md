# 프로젝트 첫날 체크리스트

키트를 처음 붙이는 날 할 일을 순서대로 적었다. 항목마다 확인 명령이나 확인 방법이 있고, 확인이 통과해야 다음 항목으로
넘어간다. 명령의 자세한 옵션과 배경은 [설치 가이드](install.md)에 있고, 여기서는 순서와 확인만 다룬다.

명령은 clone한 키트 디렉터리에서 실행하고 대상 저장소 경로를 인자로 준다(설치 가이드 1장). 예시의 대상 경로는
`../my-project`다.

GitLab 프로젝트를 기준으로 썼다. GitHub 저장소는 1~4번과 8번의 로컬 `harness judge`만 해당한다. CI 조각과 MR 본문 lint는
GitLab 전용이고 GitHub용 workflow는 키트가 만들지 않는다(설치 가이드 5장).

## 체크리스트

| # | 할 일 | 확인 | 담당 |
| --- | --- | --- | --- |
| 1 | 루트 생성: `init`에 플랫폼·트래커·이슈 키·브랜치를 준다 | `check`가 드리프트 없음 | 키트 담당 |
| 2 | 영역 생성: 영역마다 `--area`와 프로필 또는 `--verify-cmd` | 영역 `AGENTS.md` 생성, `check` 드리프트 없음, 영역 검증 명령이 저장소 루트에서 통과 | 영역 담당 |
| 3 | hooks 인터프리터 확인(Windows) | 보호 경로 편집이 거부된다 | 팀원 각자 |
| 4 | 첫 커밋: 생성 파일과 `.claude/`를 함께 커밋 | `git status`가 깨끗하고 `.gitignore`에 `/plans/` | 키트 담당 |
| 5 | CI 조각 include: `.gitlab-ci.yml`에 키트 커밋 SHA로 고정 | 테스트 MR에서 `harness-*` job이 생성된다 | 키트 담당 |
| 6 | 보호 브랜치와 병합 조건 | 기본 브랜치 직접 Push 거부, 파이프라인 실패 MR 병합 불가 | 팀장(Maintainer) |
| 7 | CI 변수 등록 | 변수 이름만 확인한다. 값은 어디에도 붙여 넣지 않는다 | 팀장(Maintainer) |
| 8 | 첫 MR로 판정과 본문 lint 확인 | `harness judge` 결과와 MR lint 결과가 같다 | 키트 담당 |
| 9 | 측정 예약 파이프라인 | 스케줄 수동 실행에서 `metrics.json`·`report.html` 아티팩트 생성 | 팀장(Maintainer) |

### 1. 루트 생성

```bash
python bin/harness.py init ../my-project --project-name my-project --platform gitlab \
  --tracker jira --issue-prefix ABC123 --default-branch main --integration-branch main
python bin/harness.py check ../my-project
```

- `--issue-prefix`는 Jira 프로젝트 키다. 브랜치 이름(`feat/ABC123-52-...`), MR 템플릿 예시, 플랜 파일 키가 모두 이 값을 쓴다.
  나중에 키가 바뀌면 `harness.json`을 고치고 `init --force`로 다시 생성한다(아래 "첫 소비자에서 걸린 점")
- 통합 브랜치를 따로 두지 않으면 `--integration-branch`를 기본 브랜치와 같게 준다

### 2. 영역 생성

```bash
python bin/harness.py init ../my-project --area backend --profile spring-java --var base_package=com.acme.app
python bin/harness.py check ../my-project
```

- 프로필이 없는 스택은 `--verify-cmd`를 하나 이상 준다. 검증 명령은 저장소 루트에서 실행된다(`cd backend && ...`)
- 프로필이 출력한 `안내:`(컨벤션 테스트 의존성)는 키트가 빌드 파일을 고치지 않으므로 영역 담당이 직접 추가한다
- 확인: `harness.json`의 `areas[].verify` 명령을 저장소 루트에서 한 번씩 실행해 통과시킨다

### 3. hooks 인터프리터 (Windows)

```powershell
where.exe python3
python3 --version
```

- `WindowsApps\python3.exe`가 나오고 버전이 출력되지 않으면 hooks가 조용히 꺼진 상태다.
  `harness.json`에 `"hooks": {"python": "py"}`를 넣고 `init --force`로 `.claude/settings.json`을 다시 만든다(설치 가이드 7장)
- 확인: Claude Code 세션에서 lock 파일 같은 보호 경로를 편집하게 해 보고 거부되는지 본다. `check`로는 hooks 동작을 알 수 없다

### 4. 첫 커밋

```bash
cd ../my-project
git status --short
git add .
git commit -m "chore: ai-dev-harness 적용"
```

- `git add .` 전에 `git status`를 본다. `.claude/settings.local.json`(개인 권한·설정)이 보이면 먼저 `.gitignore`에 넣는다.
  `init`은 `.gitignore`에 `/plans/`만 추가한다

- `.claude/settings.json`과 `.claude/hooks/`는 커밋한다. 개인 설정 `.claude/settings.local.json`은 커밋하지 않는다
- `docs/README.md`·`AGENTS.md` 같은 생성 파일에 프로젝트 내용을 직접 쓰지 않는다. 다음 `init --force`가 덮어쓴다.
  프로젝트 문서 목록은 생성 대상이 아닌 별도 문서에 둔다. 시드 파일(`docs/git-convention.md`, `docs/adr/README.md`)은
  프로젝트가 채워도 된다

### 5. CI 조각

```yaml
include:
  - remote: https://raw.githubusercontent.com/kiddo-psh/ai-dev-harness/<full-commit-sha>/core/ci/gitlab/mr-lint.yml
  - remote: https://raw.githubusercontent.com/kiddo-psh/ai-dev-harness/<full-commit-sha>/core/ci/gitlab/secret-detection.yml
  - remote: https://raw.githubusercontent.com/kiddo-psh/ai-dev-harness/<full-commit-sha>/core/ci/gitlab/sast.yml
  - remote: https://raw.githubusercontent.com/kiddo-psh/ai-dev-harness/<full-commit-sha>/core/ci/gitlab/metrics.yml
```

- 브랜치 이름이 아니라 전체 커밋 SHA로 고정한다. `<full-commit-sha>`는 `init`에 쓴 키트 커밋과 같게 맞춘다
  (`.harness/` 복사본과 조각이 같은 버전이어야 한다)
- `dependency-audit.yml`은 lockfile이 없는 `build.gradle(.kts)`·`package.json`을 실패로 본다. lockfile을 먼저 커밋하고 넣는다.
  `image-scan.yml`은 이미지를 만드는 job이 생긴 뒤 넣는다
- 필요 조건: Docker executor runner, Docker Hub·raw.githubusercontent.com 접근, sast 기본 규칙(`p/default`)을 받을 semgrep.dev 접근,
  mr-lint는 GitLab 16.7 이상(`core/ci/README.md` "필요 조건"). 하나라도 없으면 해당 job이 경고가 아니라 실패한다
- 확인: 아무 MR이나 열어 파이프라인에 `harness-mr-lint`·`harness-secret-detection`·`harness-sast`가 생기는지 본다.
  `.gitlab-ci.yml` 변경은 판정이 엄격이므로 이 MR 자체가 플랜·분리 리뷰를 거친다

### 6. 보호 브랜치와 병합 조건

GitLab 프로젝트 설정에서 사람이 한다.

- Settings → Repository → Protected branches: 기본 브랜치의 Push를 No one(또는 Maintainer)으로, Merge를 Developer 이상으로
- Settings → Merge requests: "Pipelines must succeed"를 켠다. 그래야 MR lint·Secret 탐지 실패가 병합을 막는다
- 확인: 기본 브랜치로 직접 `git push`가 거부되고, 실패한 파이프라인의 MR에 Merge 버튼이 비활성인지 본다

### 7. CI 변수

Settings → CI/CD → Variables에 등록한다. 값은 채팅·이슈·커밋에 붙여 넣지 않는다([Secret 규칙](secret-environment-variables.md)).

| 변수 | 필요할 때 | 설정 |
| --- | --- | --- |
| `HARNESS_COMMENT_TOKEN` | MR lint 판정 댓글, 2700자 넘는 MR 본문(엄격 MR은 대개 넘는다) | Masked, Reporter 역할 전용 프로젝트 액세스 토큰. 댓글까지 쓰려면 `api`, 본문 읽기만이면 `read_api`. MR을 올리는 사람이 읽을 수 있는 토큰이라 다른 용도로 쓰지 않는다 |
| `HARNESS_METRICS_TOKEN` | 측정 예약 파이프라인(필수) | Masked, `read_api`(Reporter 이상) 프로젝트 액세스 토큰 |
| `HARNESS_METRICS_SINCE` | 측정 시작일 고정 | 프로젝트 시작일 `YYYY-MM-DD` |
| `HARNESS_NOTIFY_WEBHOOK_URL` 또는 `HARNESS_MATTERMOST_*` | 주간 요약 발송 | Masked. Mattermost 계정 모드는 봇 계정 |

- Protected 변수는 보호 브랜치 파이프라인에서만 읽힌다. 예약 파이프라인의 대상 브랜치(기본 브랜치)가 보호 브랜치인지 확인한다
- 확인: 변수 목록에 이름과 Masked 표시가 있는지 본다

### 8. 첫 MR로 판정과 본문 lint 확인

```bash
python bin/harness.py judge ../my-project --base origin/main
```

- MR 본문은 템플릿을 채운다. `## 판정` 첫 낱말(lite·standard·strict), `Closes <KEY>`, 검증 세 줄, 영향 범위 체크
- 확인: `harness-mr-lint` job 로그의 "본문 판정·변경 파일 판정"이 로컬 `judge` 결과와 같다. 다르면 높은 쪽이 적용되고
  불일치로 기록된다(측정 지표 4)

### 9. 측정 예약 파이프라인

- Build → Pipeline schedules → New schedule. 월요일 09:00 KST면 timezone Asia/Seoul, cron `0 9 * * 1`, 대상은 기본 브랜치
- 만든 뒤 Run(▶)으로 한 번 돌린다
- 확인: `harness-metrics` job이 성공하고 아티팩트에 `metrics.json`·`report.html`이 있다. 발송 변수가 있으면 채널에
  요약이 오고 마지막 줄의 리포트 링크가 열린다
- 자세한 변수와 끄는 법은 [`core/ci/README.md`](../core/ci/README.md) "측정 예약 파이프라인"

## 첫 소비자에서 걸린 점

첫 소비자(꾸기, SSAFY GitLab) 적용에서 실제로 걸린 점이다. 같은 실수를 피하려고 남긴다.

- **이슈 키 변경**: GitLab 이전과 함께 Jira 키가 바뀌었는데 `harness.json`의 `issue_prefix`가 이전 값으로 남아 MR 템플릿·
  문서 예시와 플랜 파일 키가 어긋났다. 키가 바뀌면 `issue_prefix`를 고치고 `init --force`, 시드 파일의 예시는 직접 고친다
- **생성 파일 직접 수정**: 팀이 `docs/README.md`(생성 파일)에 프로젝트 문서 목록을 채워 넣어, `init --force`가 그 내용을
  지우려 했다. 수정분을 되돌리면 `check`가 그 파일을 계속 불일치로 보고한다. 4번의 규칙대로 프로젝트 문서 목록은 별도 문서에 둔다
- **CI 조각 미연결**: 키트 파일은 들어갔지만 `.gitlab-ci.yml`이 없어 MR lint·보안 검사가 한 번도 돌지 않았다. 5번 확인을 건너뛰지 않는다
