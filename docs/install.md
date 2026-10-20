# 설치 가이드

이 문서는 ai-dev-harness를 대상 저장소에 붙이고, 확인하고, 끄고, 걷어내는 절차를 다룬다.
키트 자신에게 적용하는 절차(자기 적용)는 [README](../README.md)에 있다.

목표는 빈 저장소에 키트를 붙여 첫 병합 요청이 엄격 단계 절차까지 통과하는 데 반나절을 넘기지 않는
것이다([개발 계획](roadmap.md) 1장).

## 1. 준비물

- Python 3.12 이상. 키트는 표준 라이브러리만 쓰므로 설치할 의존성이 없다
- git
- 키트 저장소를 clone해 둔다. 대상 저장소와 별도 디렉터리에 둔다

```bash
git clone https://github.com/kiddo-psh/ai-dev-harness.git
cd ai-dev-harness
python bin/harness.py version
```

`init`은 clone한 키트 디렉터리에서 실행하고 대상 저장소 경로를 인자로 준다. 키트를 대상 저장소 안에
복사하지 않는다.

## 2. 무엇이 설치되는가

생성 목록은 `core/templates/manifest.json`이 정의한다. 플랫폼(`gitlab`·`github`)에 따라 병합 요청
템플릿의 경로가 달라진다.

| 대상 | 내용 |
| --- | --- |
| `AGENTS.md`, `CLAUDE.md` | AI 도구가 따르는 공통 절차 |
| `docs/README.md` | 문서 역할과 우선순위 |
| `docs/ai-collaboration.md` | AI 병렬 작업과 충돌 방지 |
| `docs/git-convention.md` | 브랜치·커밋·병합 요청 |
| `docs/development-workflow.md` | 업무 시작부터 병합까지 |
| `docs/secret-environment-variables.md` | Secret과 환경 변수 |
| `docs/templates/plan.md`, `docs/templates/review.md` | 플랜·리뷰 양식 |
| `docs/adr/README.md`, `docs/adr/0000-template.md` | 설계 결정 기록 |
| `.gitlab/merge_request_templates/` 또는 `.github/PULL_REQUEST_TEMPLATE.md` | 병합 요청 본문 |
| `.claude/settings.json`, `.claude/hooks/` | Claude Code hooks ([설명](../core/hooks/README.md)) |
| `harness.json` | 대상 저장소의 키트 설정. 이후 모든 명령이 이 파일을 읽는다 |
| `.harness/mr-lint/mr_lint.py` | `gitlab`만. MR 본문 lint 조각이 실행하는 검사 코드(5절) |
| `.harness/verify/verify_changed.py` | `gitlab`만. 변경 영역 검증 조각이 실행하는 검사 코드([설명](../core/ci/README.md)) |
| `.harness/summary.py`, `.harness/metrics/collect.py`·`classify_ci.py` | MR 본문 `## hooks 기록`·`## 에이전트 기록`·`## 리뷰 결과`를 키트 없이 만드는 요약 명령과 그것이 쓰는 코드([설명](../core/metrics/README.md) "에이전트 기록") |
| `.harness/metrics/` (`report.py`·`notify.py`) | `gitlab`만. 측정 예약 파이프라인 조각이 `collect.py`와 함께 실행하는 수집·리포트·발송 코드([설명](../core/ci/README.md)) |
| `.gitignore`의 `/plans/`·`/.claude/settings.local.json` 줄 | 생성 파일이 아니라 줄 추가다. 아래 참고 |

`docs/git-convention.md`(커밋 Scope 표·릴리스 태그 이름)와 `docs/adr/README.md`(ADR 목록)는 **시드 파일**이다. 처음 한 번
생성한 뒤에는 프로젝트가 채워 넣는 문서라 `init --force`가 덮어쓰지 않고 `check`도 내용을 비교하지 않는다(없으면 `없음`으로
보고하고 다시 만든다).

`init`은 대상의 `.gitignore`에 `/.claude/settings.local.json`·`/plans/` 줄이 없을 때만 각각 끝에 주석 한 줄과 함께
추가한다. 파일이 없으면 만들고, 있으면 기존 내용은 그대로 둔다(줄 끝 형식도 따른다). 개인 설정(`.claude/settings.local.json`)은
권한·hooks 끄기처럼 사람마다 다르므로 커밋하지 않는다. 플랜·리뷰 파일(`plans/`)도 커밋하지 않으며 MR 본문 lint가
`/plans/` 줄이 없거나 MR이 `plans/` 파일을 넣으면 실패한다. `.gitignore`는 `check`가 보지 않는다.

영역 `AGENTS.md`는 3절에서 따로 생성한다. GitLab CI 조각은 5절에서 연결한다.

## 3. 새 저장소에 붙이기

### 3.1 루트 생성

```bash
python bin/harness.py init ../my-project \
  --project-name my-project \
  --platform gitlab \
  --tracker jira \
  --issue-prefix ABC123 \
  --default-branch main \
  --integration-branch develop
```

| 인자 | 기본값 | 설명 |
| --- | --- | --- |
| `--project-name` | 대상 디렉터리 이름 | 문서에 들어갈 프로젝트 이름 |
| `--platform` | `gitlab` | `gitlab` 또는 `github`. 병합 요청 템플릿 경로와 MR·PR 용어를 정한다 |
| `--tracker` | `jira` | `jira` 또는 `github`. 업무 항목 용어와 이슈 키 예시를 정한다 |
| `--issue-prefix` | 없음 | Jira 프로젝트 키. **`--tracker jira`이면 필수다** |
| `--default-branch` | `main` | |
| `--integration-branch` | `develop` | 통합 브랜치를 쓰지 않으면 `main`과 같게 준다. 같으면 `docs/git-convention.md`·`docs/development-workflow.md`가 단일 브랜치(태그 릴리스) 내용으로 생성된다(`git-convention.md`는 시드 파일이라 처음 생성할 때만) |

대상 디렉터리가 없으면 만든다. 생성한 파일 목록이 출력된다.

### 3.2 영역 생성

영역은 담당자와 검증 명령이 갈리는 단위다(`backend`, `frontend` 등). 루트 `init` 다음에 실행한다.

```bash
python bin/harness.py init ../my-project --area backend \
  --verify-cmd "cd backend && ./gradlew test" \
  --verify-cmd "cd backend && ./gradlew ktlintCheck" \
  --area-doc "docs/api/openapi.yaml"
```

- `--verify-cmd`는 **하나 이상 필수**다(프로필을 쓰면 프로필 기본값으로 생략 가능, 3.6절). 반복해서 여러 개를 줄 수 있다. 파일을 고치지 않는 명령만 적는다
- 검증 명령은 **저장소 루트에서** 실행된다(hooks `stop-verify`). 영역 안의 빌드 도구는 `cd backend && …`, `npm --prefix frontend run …`처럼 루트 기준으로 쓴다
- `--trigger`, `--review-focus`는 생략하면 키트 기본값을 쓴다. 주면 기본값을 **대체**한다(추가가 아니다)
- `--area-doc`은 그 영역의 기준 문서다. 준 것만 적힌다. 여기 적은 경로는 hooks가 `ask`로 보호한다
- `--area`는 대상의 `harness.json`을 쓰므로 `--platform` 같은 루트 인자와 함께 쓸 수 없다
- 경로는 대상 저장소 기준 상대 경로다. `./web/`처럼 줘도 `web`으로 정규화된다. 절대 경로와 `..`는 거부한다
- 같은 `--area`를 다시 실행하면 `harness.json`의 해당 항목을 제자리에서 바꾼다. 다만 이미 있는
  `<영역>/AGENTS.md` 파일은 `--force` 없이 덮어쓰지 않는다. 생략한 trigger·review focus·문서는 이전 값을 유지한다

결과로 `<영역>/AGENTS.md`가 생기고 `harness.json`의 `areas`에 설정이 기록된다. 판정용 경로 규칙
`trigger_paths`·`test_paths`(아래 3.4)도 키트 기본값으로 함께 기록된다. 바꾸려면 `harness.json`을 직접 고친다.

### 3.3 첫 커밋

```bash
cd ../my-project
git add .
git commit -m "chore: ai-dev-harness 적용"
```

`.claude/settings.json`과 `.claude/hooks/`는 **커밋한다**. 팀 전체가 같은 가드레일을 쓰기 위해서다.
개인 설정은 `.claude/settings.local.json`에 두고 이 파일은 커밋하지 않는다.

### 3.4 판정 확인 (`harness judge`)

작업 브랜치의 변경 파일로 경량·표준·엄격(`lite`·`standard`·`strict`)을 산출한다. 기계 판정은 하한이다.
사람이 판정을 올릴 수는 있어도 내리지 않는다.

```bash
python bin/harness.py judge ../my-project                       # origin/<default_branch>와의 merge-base 이후
python bin/harness.py judge ../my-project --base origin/develop --json
git diff --name-only origin/main | python bin/harness.py judge ../my-project --files -
```

- 변경 파일은 merge-base 이후 커밋, 커밋하지 않은 수정, 추적 안 된 파일이다. 이름을 바꾼 파일은 옛 경로와 새 경로를 모두 본다
- 파일마다 `trigger_paths.strict` → `trigger_paths.standard` → 문서(`*.md`, 루트 `docs/`) → `test_paths` 순으로 맞춰
  보고, 어디에도 안 맞으면 표준이다. 전체 판정은 가장 높은 값이다. 문서·테스트만 바뀌었으면 경량이다
- 패턴은 gitignore 방식이고 영역 디렉터리 기준이다(`/src/auth/`는 `<영역>/src/auth/` 아래). 영역 밖 파일은
  최상위 `judge`의 `trigger_paths`·`test_paths`(저장소 루트 기준)를 쓴다. 기본값은 키 단위로 대체된다. `trigger_paths`를
  지정하지 않은 곳에는 공통 기본(lock 파일·CI 정의·DB 마이그레이션 → 엄격)을, `test_paths`를 지정하지 않은 곳에는 기본
  테스트 경로를 쓴다
- 최상위 `judge.triggers`는 영역 밖 파일이 하나라도 바뀌면 출력된다. 문서만 바꿔도 나오므로 문장은 조건을 담아 쓴다
- 영역의 `triggers`와 최상위 `judge.triggers` 문장(계약 불일치, 인가 등)은 경로로 판정할 수 없어 "사람 확인 필요"로만
  출력한다
- `--files` 목록은 UTF-8(BOM 허용) 한 줄에 경로 하나다. `git diff --name-only`가 따옴표로 감싼 비ASCII 경로도 풀어 읽는다
- 판정과 무관하게 종료 코드 0이다. 설정·git 오류는 2

판정 규칙을 바꾸기 전후에는 사람이 정답을 정한 사례 세트(eval)로 판정이 어떻게 달라지는지 본다.

```bash
python bin/harness.py metrics collect --platform gitlab --since 2026-09-01 --out metrics.json
python bin/harness.py metrics cases --in metrics.json --out cases.jsonl      # 병합 MR → 사례 후보
python bin/harness.py judge ../my-project --eval cases.jsonl [--json]
```

- 사례는 한 줄에 JSON 객체 하나다: `{"id": "mr-12", "paths": ["src/a.py"], "expected": "standard", "source": "<URL>", "note": "..."}`.
  `id`·`paths`·`expected`는 필수, `source`·`note`는 선택이다. `expected`가 `null`이면 정답 미확정으로 세기만 하고 비교하지 않는다
- `metrics cases`는 본문 판정과 재판정이 같은 MR은 그 판정을 정답으로, 다르거나 본문 판정이 없는 MR은 `expected: null`과
  메모로 낸다. 불일치 MR의 정답은 사람이 정해 `expected`에 적는다(판정 불일치 지표 4가 eval 세트로 돌아오는 경로).
  통합 MR과 `paths`가 없는 옛 수집 결과는 뺀다
- 출력은 일치율, 혼동 행렬(행 정답, 열 판정), 불일치 사례와 판정을 정한 파일·규칙이다. 불일치가 하나라도 있으면 종료 1,
  입력·설정 오류는 2(오류에는 파일과 줄 번호만 적는다)

### 3.5 플랜·리뷰 파일 검사 (`harness lint-plans`)

커밋하지 않는 `plans/`의 플랜·리뷰 파일이 양식을 채웠는지 로컬에서 검사한다. CI는 `plans/`를 볼 수 없으므로
CI 쪽 확인은 MR 본문 검사가 맡는다.

```bash
python bin/harness.py lint-plans --target ../my-project       # plans/ 전체
python bin/harness.py lint-plans ABC123-52 --target ../my-project   # 그 키의 플랜과 리뷰만
```

- 대상 파일: `<키>.md`는 플랜, `<키>-review*.md`(`-review.md`, `-review-A.md` 등)는 리뷰다. 키는 tracker가 github이면
  숫자, jira이면 `<issue_prefix>-<숫자>`다. `-mapping`·`-self-review` 파일과, 같은 키의
  `-review-A.md`·`-review-B.md` 중 하나라도 있을 때의 `-review.md`(엄격 단계 합본 대조표)는 제외하고, 그 밖의 이름은 무시한다
- 플랜 실패: 판정 줄이 없거나 `엄격 | 표준 | 경량`이 그대로 남음, 판정 값이 엄격·표준·경량(strict·standard·lite) 중 하나가 아님, 템플릿 절 누락, 3장 인수 테스트 표가 없거나 데이터 행 0,
  첫 칸 외 모두 빈 행(`| T1 | | | |`). 인수 테스트 표는 템플릿과 같은 머리 행으로 찾는다. 리뷰 실패: 템플릿 절 누락,
  6장 측정 칸 빈 값과 템플릿 측정 항목의 행 누락
- 경고: 템플릿의 `<…>` 자리표시자가 그대로 남음(인라인 코드·코드 블록 안은 보지 않는다)
- 절 목록과 자리표시자는 대상의 `docs/templates/plan.md`·`review.md`에서 읽고, 없으면 키트 원본을 대상 설정으로 렌더해
  쓴다. 절 제목은 공백과 끝의 괄호 주석을 빼고 비교한다. 표 구분줄은 `|---|`, `| :---: |` 등 GFM 변형을 모두 받는다
- 종료 코드: 실패 없음 0(경고만 있어도 0), 실패 1, 설정 오류·없는 키 2. 파일은 고치지 않는다

### 3.6 프로필 적용과 스캐폴드 (`--profile`, `harness scaffold`)

프로필(`profiles/<name>/`, [설명](../profiles/README.md))은 스택별 기본값 묶음이다. 영역을 만들 때 고른다.

```bash
python bin/harness.py init ../my-project --area backend --profile <name> --var base_package=com.acme.app
python bin/harness.py scaffold backend <kind> MovieReview --target ../my-project
```

- `--profile`은 `--area`와 함께 쓴다. 프로필의 `verify`·`triggers`·`review_focus`·`trigger_paths`·`test_paths`·`layers`·`allow`를
  영역 설정에 굳히고 `profile`·`vars`·`disabled_rules` 키를 남긴다. 명시한 `--verify-cmd`·`--trigger`·`--review-focus`가 우선한다
- 프로필의 검증 명령·안내에는 영역 경로 `{{area_dir}}`가 들어가 저장소 루트에서 실행할 수 있는 명령으로 굳는다. 그래서 프로필 영역 경로는 영문자·숫자·`._/-`만 쓰고 `-`로 시작하지 않는다(공백·`&`·`;`·옵션으로 읽히는 `-x` 거부)
- 이 규칙 전(#55 이전)에 프로필로 만든 영역은 `--force`로 다시 만들어도 굳힌 `verify`가 유지된다. `harness.json` 영역의 `verify`를 지우거나 `--verify-cmd`로 다시 주고 다시 만든다
- `--var 이름=값`은 프로필이 선언한 변수만 받는다. 기본값이 없는 변수는 필수이고, 값은 프로필의 형식(정규식)에 맞아야 한다
- 같은 영역을 `--force`로 다시 만들 때 `--profile`·`--var`를 생략하면 이전 값을 유지한다. 다른 프로필로 바꿀 수는 없다
  (영역 항목을 지우고 다시 만든다). 프로필 없이 만든 영역에 처음 프로필을 적용하면 기준 문서만 유지하고 나머지는 프로필
  기본값을 쓴다
- 변수를 바꿔 컨벤션 파일 경로가 달라지면(예: `base_package`) 이전 경로의 파일은 지우지 않고 `이전 컨벤션 파일`로 출력한다.
  확인 후 직접 지운다
- 프로필의 컨벤션 파일(아키텍처 테스트, lint 설정)이 영역 아래에 렌더된다. 손으로 고치지 않는다. `check`가 드리프트를 본다.
  계층 규칙은 `harness.json` 영역의 `layers`(계층 → 패턴 목록)·`allow`(계층 → 의존 가능한 계층)를 고친 뒤
  `init --area <dir> --force`로 다시 생성한다. 고정 규칙은 `disabled_rules`에 `"규칙 ID": "이유"`를 적어야 끌 수 있다.
  이 세 키의 변경은 엄격 트리거다. `harness.json`은 영역 밖이라 최상위 `judge.triggers`에도 같은 취지의 문장을 넣어
  `harness judge`·MR 본문 lint의 사람 확인으로 나오게 한다
- 컨벤션 테스트에 필요한 의존성은 `init`이 `안내:`로 출력한다. 키트는 소비자 빌드·lock 파일을 고치지 않으므로 직접 추가한다
- `scaffold <영역> <종류> <이름>`은 프로필 템플릿으로 파일을 만든다. 이름은 `MovieReview`·`movie-review`처럼 주고
  템플릿이 Pascal·camel·kebab·snake 형태를 쓴다. 목적지 파일이 하나라도 있으면 아무것도 만들지 않고 중단한다(`--force` 없음).
  생성물은 팀 소유이고 `check` 대상이 아니다
- 라우터·경로 상수 같은 공유 파일은 고치지 않고 붙일 코드 조각을 출력한다. 붙이는 위치는 사람이 정한다
- 프로젝트 저장소의 `.harness/templates/<profile>/<kind>/<파일>`이 있으면 프로필의 같은 이름 템플릿 대신 쓴다.
  아키텍처 ADR에 맞춰 생성물을 바꿀 때 쓴다

## 4. 기존 저장소에 붙이기

기존 파일을 말없이 덮어쓰지 않는다. 충돌하는 파일이 하나라도 있으면 **아무것도 쓰지 않고 중단**하고
목록을 보여 준다. 단 시드 파일(`docs/git-convention.md`, `docs/adr/README.md`)은 이미 있으면 충돌로 보지 않고 그 파일을
그대로 쓴다(덮어쓰지 않는다). 기존 저장소의 같은 이름 문서가 키트 내용과 다르면 직접 맞춘다.

```
오류: 이미 존재하는 파일이 있어 중단한다. 덮어쓰려면 --force 를 지정한다:
  AGENTS.md
  docs/README.md
```

권장 순서는 다음과 같다.

1. **먼저 깨끗한 작업 트리에서 시작한다.** 커밋하지 않은 변경이 있으면 커밋하거나 stash한다
2. 위 목록을 보고 각 파일을 어떻게 할지 정한다
   - 기존 내용이 없어도 되면 → `--force`
   - 기존 내용을 살려야 하면 → 생성 파일을 덮어쓰기 전에 별도 브랜치에서 내용을 검토한다. 특히 기존
     `.claude/settings.json` 병합은 아직 지원하지 않는다. 수동 병합 파일은 `check`에서 드리프트로 표시된다
3. `--force`로 실행한다. `harness.json`이 아직 없다면 필요한 루트 인자도 함께 준다

```bash
# 기존 harness.json이 있는 경우
python bin/harness.py init ../my-project --force

# 기존 harness.json이 없는 GitHub 저장소의 예
python bin/harness.py init ../my-project --force --platform github --tracker github
```

4. `git diff`로 무엇이 바뀌었는지 확인한 뒤 커밋한다

### 주의할 점

- **`--force`는 생성 대상 파일만 덮어쓴다.** `harness.json`도 다시 쓰지만 기존 파일을 읽어서 쓰므로
  `hooks`, `areas` 같은 설정은 보존된다. 바뀌는 것은 `harness_version`이 키트 버전으로 맞춰지는 것과
  들여쓰기·키 순서뿐이다
- **`harness.json`이 이미 있으면 `--platform`·`--tracker` 같은 루트 인자를 함께 쓸 수 없다.** 값을
  바꾸려면 `harness.json`을 직접 고치고 `init --force`로 다시 생성한다. `--config`와 루트 인자도 함께 쓸 수 없다
- **부분 적용은 지원하지 않는다.** 일부 파일만 생성하는 인자는 없다. 필요 없는 파일은 생성 후 지우면
  되지만, 그러면 `check`가 "없음"으로 보고한다(8절)

## 5. CI 조각

보안 검사 네 조각은 GitLab MR 파이프라인에서 사용할 수 있다([ADR-0003](adr/0003-gitlab-ci-remote-include.md)).

- **GitLab 전용이다.** 대상 저장소의 `.gitlab-ci.yml`이 키트의 조각을 `include: remote:`로 게시된 전체 커밋 SHA에 고정해 참조한다.
  파일은 복사하지 않는다
- **GitHub 저장소용 조각은 없다.** GitHub Actions에서는 같은 도구(gitleaks, trivy, semgrep)를 직접
  호출해야 한다. 키트 저장소 자신의 적용 예는 `.github/workflows/security.yml`이다
- 설정 방법·필요 조건·끄는 법은 [`core/ci/README.md`](../core/ci/README.md)를 따른다
- MR 본문 lint 조각(`mr-lint.yml`)은 MR 본문의 필수 절(업무 참조, 판정, 검증, 영향 범위, 엄격이면 플랜 요약·리뷰 결과)과
  `plans/` 규칙을 검사하고 실패하면 MR 파이프라인이 실패한다. `init`이 넣은 `.harness/mr-lint/mr_lint.py`,
  `.claude/hooks/harness_common.py`, `harness.json`을 커밋해 두어야 한다. GitLab 16.7 이상이 필요하고, 본문만 고치면
  다시 돌지 않으므로 새 파이프라인을 실행한다. 판정 댓글은 선택(`HARNESS_COMMENT_TOKEN`)이고, 같은 토큰이 있으면 2700자에서
  잘린 본문을 API로 다시 읽는다(없으면 잘린 MR은 실패). 통합 MR(`integration_branch` → `default_branch`, 같은 프로젝트)은
  건너뛴다(통과)([`core/ci/README.md`](../core/ci/README.md) "MR 본문 lint")

## 6. hooks 설정

hooks는 `init`이 설치하지만, 무엇을 보호하고 무엇을 검증할지는 `harness.json`의 `hooks` 절로 정한다.
이 절은 **생략할 수 있고**, 생략하면 기본 보호 경로만 적용되고 종료 검증은 영역 설정만 쓴다.

```json
{
  "hooks": {
    "protected_paths": [
      {"pattern": "docs/api/**", "mode": "block", "reason": "API 계약"}
    ],
    "stop_verify": ["python -m pytest -q"],
    "stop_timeout_sec": 300,
    "python": "python3"
  }
}
```

| 키 | 값 | 설명 |
| --- | --- | --- |
| `protected_paths` | 객체 목록 | 지정하면 기본 목록을 **대체**한다. `pattern` 필수, `mode`는 `block` 또는 `ask` 필수, `reason`은 선택 |
| `stop_verify` | 문자열 목록 | 영역 밖 파일이 바뀌었을 때 실행할 명령. 파일을 고치지 않는 명령만 적는다 |
| `stop_timeout_sec` | 1~840 정수 | 명령별 시간 제한. 기본 300. 전체 예산도 840초다 |
| `python` | 명령 이름 또는 `py -3` | hook을 실행할 인터프리터. 기본 `python3`. 셸 메타문자·경로·임의 인자는 허용하지 않는다 |

규칙과 기본 보호 목록, 우회 경로는 [`core/hooks/README.md`](../core/hooks/README.md)에 자세히 있다.

`protected_paths`·`stop_verify`·`stop_timeout_sec`는 hook이 실행할 때마다 다시 읽으므로 고친 뒤
`init`을 다시 할 필요가 없다. **`python`만 예외다** — `.claude/settings.json`에 값이 박히므로
`init --force`로 다시 생성해야 한다.

hooks가 막거나 확인한 편집과 종료 검증 실패는 worktree마다 `.git/harness/events.jsonl`에 남는다. 병합 요청을 만들기 전에
작업한 worktree에서 요약을 만들어 본문 `## hooks 기록` 절에 붙이면 측정 수집기가 지표 5로 센다
([`core/metrics/README.md`](../core/metrics/README.md) "hooks 기록").

```bash
python .harness/summary.py events                       # 대상 저장소의 작업한 worktree에서(키트 없이)
python bin/harness.py events summary ../my-project      # 같은 출력(키트에서)
```

`## 에이전트 기록`은 `python .harness/summary.py agent`, `## 리뷰 결과`의 두 줄은 `python .harness/summary.py review <리뷰 파일>`로
만든다. 생성 `AGENTS.md`(7절)와 개발 흐름 문서가 병합 요청을 만드는 쪽(AI 도구 포함)이 실행해 채우도록 정한다.

## 7. Windows의 `hooks.python`

Windows에서 hooks가 아무 일도 안 하는 것처럼 보이면 대부분 이 문제다.

`hooks.python`의 기본값은 `python3`인데, Windows에서 `python3`는 실제 Python이 아니라
**Microsoft Store 앱 실행 별칭**인 경우가 많다. 별칭이 가리키는 앱이 설치돼 있지 않으면 Store 페이지를
열고 종료 코드 49로 끝난다. hook이 실패해도 편집은 계속되므로 **보호가 조용히 꺼진 상태**가 된다.

### 확인

```powershell
where.exe python3
python3 --version
```

- `...\AppData\Local\Microsoft\WindowsApps\python3.exe` 가 나오면 별칭이다
- `python3 --version`이 버전을 출력하지 않거나 Store가 열리면 **그대로 쓰면 안 된다**
- 별칭이어도 버전이 정상 출력되면 동작은 한다. 다만 `python`과 **다른 인터프리터**일 수 있으므로
  두 명령의 버전을 비교해 둔다

### 조치

`harness.json`에 실제로 동작하는 명령을 지정하고 `settings.json`을 다시 만든다.

```json
{ "hooks": { "python": "py" } }
```

```bash
python bin/harness.py init ../my-project --force
```

`py`(Python Launcher)가 가장 안전하다. `python`도 되지만, PATH에서 Store 별칭이 먼저 잡히지 않는지
`where.exe python`으로 확인한다.

> 별칭 자체를 끄려면 **설정 → 앱 → 고급 앱 설정 → 앱 실행 별칭**에서 `python3.exe`를 끈다.
> 다만 이건 PC마다 해야 하므로, 팀에 공유되는 `harness.json`에 `python`을 지정하는 편이 확실하다.

## 8. 설치 확인

```bash
python bin/harness.py check ../my-project
```

- 드리프트가 없으면 검사한 파일 수를 출력하고 종료 코드 0
- 템플릿과 다르거나 없는 파일, `.claude/hooks/`의 여분 파일이 있으면 목록을 출력하고 종료 코드 1
- `harness.json`의 `harness_version`이 키트 버전과 달라도 종료 코드 1

생성된 파일은 손으로 고치지 않는다. 내용을 바꾸려면 키트의 `core/templates/`를 고치고 다시 생성한다.
저장소 고유 내용은 생성 대상이 아닌 별도 문서에 둔다.

hooks가 실제로 동작하는지는 `check`로 알 수 없다. Claude Code 세션에서 lock 파일 같은 보호 경로를
편집해 보고 거부되는지 확인한다.

## 9. 끄는 법

사람이 판단해 끈다. 끈 사실과 이유는 `friction` 이슈로 남긴다([개발 계획](roadmap.md) 1장 "팀 수용").

| 범위 | 방법 |
| --- | --- |
| hooks 전체 | `.claude/settings.local.json`에 `{"disableAllHooks": true}`. 개인 설정이라 커밋하지 않는다 |
| 보호 경로 하나 | `harness.json`의 `hooks.protected_paths`를 조정한다 |
| 종료 검증 | `hooks.stop_verify`를 지우거나 더 빠른 명령으로 바꾼다. 영역의 `verify`는 필수라 영역 단위로는 끌 수 없다 |
| CI 조각 하나 | `.gitlab-ci.yml`의 해당 `include`를 제거한다. [`core/ci/README.md`](../core/ci/README.md) |
| 측정 예약 파이프라인 | Build → Pipeline schedules에서 스케줄을 비활성화한다(변수·코드는 그대로). 발송만 끄려면 발송 변수(`HARNESS_NOTIFY_WEBHOOK_URL`, `HARNESS_MATTERMOST_*`)를 지운다. [`core/ci/README.md`](../core/ci/README.md) "측정 예약 파이프라인" |

## 10. 제거

```bash
cd ../my-project
rm -rf .claude/hooks .harness/metrics
rm .claude/settings.json harness.json .harness/summary.py
```

문서(`AGENTS.md`, `CLAUDE.md`, `docs/`)와 병합 요청 템플릿은 저장소의 내용이므로 지울지는 따로 판단한다.
시드 파일(`docs/git-convention.md`, `docs/adr/README.md`)은 생성 뒤 프로젝트가 채운 문서라 남기는 것이 기본이다.
CI 조각(5절)을 붙였다면 CI 정의에서 해당 `include`와 그 조각의 job 참조도 지운다.
GitLab이면 다음도 걷어낸다.

- `.harness/mr-lint/`, `.harness/verify/`, `.harness/metrics/`(키트가 복사한 실행 코드). `.harness/templates/`는 팀이 만든 템플릿이라 따로 판단한다
- 측정 예약 파이프라인: Build → Pipeline schedules의 스케줄을 지운다. `include`만 빼고 스케줄을 남기면 job 없는 파이프라인이 매주 실패한다
- CI 변수: Settings → CI/CD → Variables의 `HARNESS_*` 변수를 지우고, 그 변수에 넣은 프로젝트 액세스 토큰을 폐기한다
- 이미 만들어진 `metrics.json`·`report.html` 아티팩트를 지운다. 대부분 90일 뒤 만료되지만, GitLab 기본 설정(Settings → CI/CD →
  Artifacts의 "Keep artifacts from most recent successful jobs")은 브랜치의 마지막 성공 파이프라인 아티팩트를 `expire_in`과 무관하게
  남긴다. 마지막 예약 실행의 아티팩트는 Build → Artifacts에서 직접 지우거나 이 설정을 끈다

`.gitignore`의 `/plans/`·`/.claude/settings.local.json` 줄은 남겨 둬도 된다.

`.claude/settings.local.json`은 개인 설정이라 키트가 만들지 않았다. 지우지 않는다.

## 11. 문제 해결

| 증상 | 원인과 조치 |
| --- | --- |
| `설정 파일이 없다: .../harness.json` | 루트 `init`을 먼저 실행한다. `--area`는 루트 `init` 이후에만 쓴다 |
| `tracker가 jira이면 issue_prefix가 필요하다` | `--issue-prefix`를 주거나 `--tracker github`을 쓴다 |
| `--area 에는 --verify-cmd 가 하나 이상 필요하다` | 그 영역의 검증 명령을 하나 이상 준다 |
| `--area 는 대상의 harness.json 설정을 쓰므로 ... 함께 쓸 수 없다` | 영역 생성에서 루트 인자를 뺀다 |
| `이미 존재하는 파일이 있어 중단한다` | 4절 |
| `영역 dir은 정규화된 형태여야 한다` | `harness.json`의 `areas[].dir`을 손으로 고칠 때 난다. `./backend`나 `backend/`가 아니라 `backend`로 적는다. `--area`로 줄 때는 자동으로 정규화되므로 이 오류가 나지 않는다 |
| hooks가 아무 반응이 없다 | 7절. `.claude/settings.json`이 있는지, `hooks.python`이 동작하는지 확인한다 |
| 모든 편집이 거부된다 | hook 스크립트가 없으면 Python이 종료 코드 2를 내서 전부 거부된다. `init --force`로 다시 설치한다 |
| 콘솔에 한국어가 깨진다 | 명령 출력은 UTF-8로 맞춰 두었다. 그래도 깨지면 `chcp 65001` 후 다시 실행한다 |

## 12. 다음 단계

- 첫날 할 일과 확인 순서: [프로젝트 첫날 체크리스트](first-day.md)
- 판정 단계와 플랜·리뷰를 켜는 조건: [기여 규칙](contributing.md)
- hooks의 규칙·동작·알려진 우회: [`core/hooks/README.md`](../core/hooks/README.md)
- GitLab CI 조각: [`core/ci/README.md`](../core/ci/README.md)
- 설계 결정의 배경: [ADR](adr/README.md)
- 세션 단위 관측(선택): 14절 Claude Code 텔레메트리

## 13. 소비자 계약 (0.3.0)

`harness.json`의 루트 키는 `harness_version`, `project_name`, `platform`, `tracker`, `issue_prefix`,
`default_branch`, `integration_branch`, `related_docs`, `areas`, `hooks`, `judge`(선택, 3.4절), `pricing`(선택)이다. 알 수 없는
키나 타입이 틀린 값은 오류로 처리한다. `hooks`는 `protected_paths`·`stop_verify`·`stop_timeout_sec`·`python`을 가진다.
`pricing`은 추정 비용 단가를 덮어쓴다. `models`(모델 ID → `input`·`output`·`cache_write_5m`·`cache_write_1h`·`cache_read`의
백만 토큰당 단가)와, `models`와 함께만 쓰는 `currency`(영문 3자 또는 원·₩·$·€·¥·£)·`as_of`(기준일)를 가진다. `related_docs` 항목은 문자열 `label`·`path`만 가진다. `areas` 항목은
`dir`·`verify`와 선택 항목 `triggers`·`review_focus`·`docs`·`trigger_paths`·`test_paths`를 가진다.
`trigger_paths`는 `strict`·`standard` 키만 가진 객체이고 값은 패턴 목록이다(빈 목록 허용). `test_paths`는 패턴
목록이다. 영역을 새로 만들면 기본 `triggers`·`review_focus`·`trigger_paths`·`test_paths`를 설정 파일에 저장한다. 같은 영역을 `--force`로 다시 만들 때 생략한
선택 항목은 이전 값을 유지한다.
최상위 `judge`(선택)는 영역 밖 파일의 판정 규칙으로 `trigger_paths`·`test_paths`·`triggers`(사람 확인 문장)만 가진다.
영역 `verify`와 최상위 `hooks.stop_verify` 명령은 저장소 루트에서 실행한다. 프로필의 `verify`·`setup_notes`는 변수와 `area_dir`(정규화된 영역 경로)로 렌더하며, `area_dir`는 변수 이름으로 쓸 수 없다.
영역의 프로필 키(3.6절)는 `profile`(프로필 이름), `vars`(변수 이름 → 문자열), `layers`(계층 이름 → 패턴 목록),
`allow`(계층 이름 → `layers`에 있는 계층 이름 목록, 빈 목록 허용), `disabled_rules`(규칙 ID → 비어 있지 않은 이유)다.
`profile` 없이 나머지 키를 쓰면 오류다. CLI는 `init <target> --area <dir> --profile <name> [--var 이름=값 ..]`와
`scaffold <area> <kind> <name> [--var 이름=값 ..] [--target <dir>]`이고, 스캐폴드 템플릿 덮어쓰기 경로는
`.harness/templates/<profile>/<kind>/`다. 프로필 정의 파일 `profiles/<name>/profile.json`의 스키마와 템플릿 표식
(`harness:rule <id>` ~ `harness:end`)은 키트 작성자용 계약이다([profiles/README.md](../profiles/README.md)).

템플릿의 `{{name}}`은 키트가 가진 값으로 치환한다. 이름은 소문자와 밑줄만 사용한다. 지원 이름은 `project_name`, `platform`,
`pr_noun`, `pr_long`, `ci_variables`, `tracker_name`, `issue_noun`, `issue_key`,
`issue_key_example`, `branch_key_example`, `default_branch`, `integration_branch`, `related_docs`,
`hook_python`, `review_perspectives`, `git_branch_model`, `git_branch_table`, `git_release_section`,
`workflow_release_title`, `workflow_release_body`다. `review_perspectives`는 리뷰 관점 원본
(`core/templates/review-perspectives.md`)의 공통·로컬 절을 묶은 값이고, `git_*`·`workflow_*`는 브랜치 모델 조각
(`core/templates/docs/branch-model.md`)에서 `integration_branch == default_branch`(단일 브랜치)인지에 따라 고른 절이다. 영역 템플릿에는 `area_dir`, `area_docs`, `area_verify`, `area_triggers`,
`area_review_focus`가 추가된다. 알 수 없는 이름이나 잘못된 표기는 생성 오류다. 생성 파일 목록과 원본 경로는
`core/templates/manifest.json`이 정의하며, 이는 소비자 설정이 아닌 키트 작성자용 계약이다.
CLI 명령은 `init`, `check`, `scaffold`, `judge`(`--eval` 포함), `lint-plans`, `events summary`, `sessions summary`,
`agent summary`, `review summary`, `dashboard`, `telemetry push`, `classify-ci`, `metrics collect`·`report`·`notify`·`cases`,
`version`이다. 소비자가 수집기에 맞춰 쓰는 MR 본문 절은 `## hooks 기록`, `## 에이전트 기록`, `## 리뷰 결과`, `## 보호 경로 확인`이고,
라벨이 붙은 줄의 형식은 `core/metrics/README.md`와 `core/ci/README.md`가 정한다.
매니페스트의 `includes`는 원본 파일의 `##` 절을 골라 자리표시자 값으로 넣는다. `when`(`two-branch`·`trunk`)을 단
항목은 브랜치 모델별 변형이고, 같은 이름의 두 변형을 모두 둬야 한다. 원본을 고치면 생성 파일이 바뀌므로 `check`가 불일치로
보고한다(시드 파일 제외). `files` 항목의 `seed: true`는 시드 파일(`docs/git-convention.md`, `docs/adr/README.md`)로, 처음
한 번만 생성하고 `--force`가 덮어쓰지 않으며 `check`는 존재만 본다. 그래서 템플릿을 바꿔도 이미 만든 시드 파일에는
반영되지 않는다(필요하면 파일을 지우고 `init --force`로 다시 만든다). 항목의 `base`는 `templates`(기본)·`hooks`·`ci`(`core/ci/`)·`metrics`(`core/metrics/`) 중 하나다.

### 13.1 0.2.0에서 0.3.0으로 올리기

0.2.0 이후 M2~M5의 기능이 들어왔다(판정·lint, 프로필·스캐폴드, 측정·리포트, 세션 기록·추정 비용·실시간 대시보드, hooks 없는
도구의 보호 경로 확인). 설정 키는 추가만 했고 기존 키의 뜻은 바꾸지 않았다. 예외: 10월 2~3일의 키트(같은 0.2.0으로 기록됨)로
`claude_review` 키를 켰다면 그 기능은 없어졌다. `harness.json`의 `claude_review` 키와 `.harness/claude-review/`를 지우고,
`.gitlab-ci.yml`에서 `claude-review.yml` include와 그 템플릿을 쓰는 로컬 job(`harness-claude-auth-check`,
`harness-claude-review`)도 지운다. include만 0.3.0으로 올리면 없어진 조각을 가리켜 파이프라인 설정이 깨지고, 옛 고정값으로
두면 job이 지운 스크립트를 부른다.

1. 작업 브랜치를 만든다(생성 파일이 한꺼번에 바뀌므로 다른 작업과 섞지 않는다).
2. 키트를 0.3.0(`v0.3.0` 태그)으로 받고 `python bin/harness.py init <target> --force`를 실행한다. 루트 `init`이 `harness.json`에
   기록된 모든 영역의 `AGENTS.md`와 프로필 컨벤션 파일도 다시 만든다. `init --area`는 프로필 영역의 `setup_notes`를 다시 볼
   때만 쓴다. 시드 파일(`docs/git-convention.md`, `docs/adr/README.md`)은 덮어쓰지 않고, 없으면 만든다.
3. 다시 생성되는 주요 파일:
   - `.claude/settings.json`과 hooks: `SessionStart`·`SessionEnd` 등록, 새 `session-start.py`·`session-end.py`
   - MR 템플릿: `## hooks 기록`·`## 에이전트 기록`·`## 리뷰 결과`·`## 보호 경로 확인` 절
   - 리뷰 템플릿 7장(처리 표), 개발 흐름 문서
   - GitLab: `.harness/mr-lint/`, `.harness/verify/`, `.harness/metrics/`
   - `harness.json`의 `harness_version`이 `0.3.0`이 된다
4. 손으로 할 일:
   - `.gitignore`에 `.claude/settings.local.json`·`/plans/`가 없으면 `init`이 끝에 더한다. 결과를 확인한다
   - react-ts 프로필: 생성된 `AGENTS.md`를 `.prettierignore`에 넣는다(프로필 `setup_notes`)
   - spring-java 프로필(Windows): 줄끝 설정 안내를 따른다(프로필 `setup_notes`)
   - GitLab CI: 쓰는 조각의 `include` 고정 SHA(또는 `v0.3.0` 태그 커밋)를 올린다. 0.2.0에는 보안 검사 4종만 있었으므로
     새 조각은 따로 `include`에 더한다. MR 본문 lint `mr-lint.yml`(GitLab 16.7 이상, `GIT_DEPTH` 0)이 있어야 아래의 보호 경로
     확인이 동작한다. 변경 영역 검증 `verify.yml`(`HARNESS_VERIFY_IMAGE` 필요)과 측정 `metrics.yml`(예약 파이프라인)은 선택이다
5. 팀이 생성 파일을 고친 적이 있으면 `init --force`가 그 수정을 덮는다. `git diff`로 파일마다 나눠 처리한다.
   - 키트 장치(`.claude/hooks/`, `.claude/settings.json`, `.harness/`의 실행 코드): 다시 생성한 것을 받는다
   - 팀이 고친 문서·템플릿(`AGENTS.md`, `docs/`, MR·리뷰 템플릿, 프로필 컨벤션 테스트): 팀 파일을 되돌리고, 새 절(MR 템플릿의
     `## hooks 기록`·`## 에이전트 기록`·`## 보호 경로 확인`, 리뷰 템플릿 7장, 개발 흐름의 기록 절차)만 손으로 옮긴다
   - 팀이 옮겨 둔 시드 파일(예: `docs/git-convention.md`를 다른 경로로)은 새로 생긴 파일을 지운다
   이렇게 남긴 팀 수정은 `check`가 계속 불일치로 보고한다. 그 목록이 팀이 키트와 다르게 쓰는 파일이다.
6. `python bin/harness.py check <target>`으로 남은 "불일치"·"없음"이 5에서 남기거나 지우기로 한 파일뿐인지 확인하고 MR을 올린다. 이 MR은 `.claude/`·`harness.json`·CI 정의를 바꾸므로
   `## 보호 경로 확인`을 사람이 확인하고 체크한다.

새로 생기는 동작:

- `mr-lint.yml`을 include하면 MR lint가 보호 경로(lock 파일, CI 정의, 마이그레이션, `harness.json`, `.claude/`의 설정·hooks,
  `.harness/` 등)를 바꾼 MR에 `## 보호 경로 확인` 체크를 요구한다. hooks가 없는 도구(Codex 등)로 만든 MR도 같다
- hooks가 세션 시작·응답·종료를 `.git/harness/sessions.jsonl`에 기록하고 transcript에서 토큰 수를 센다(내용은 기록하지 않는다)
- 팀 대시보드(`core/observability/`)와 `telemetry push`는 켜지 않는다. 쓰려면 14.5절

## 14. (선택) Claude Code 텔레메트리

Claude Code는 OpenTelemetry로 세션·비용·도구 허용 여부·hook 실행 결과를 내보낼 수 있다. 키트는 이것을 켜지 않는다.
받을 수집기(OTel collector)와 저장소가 필요하다. 키트는 바로 띄울 수 있는 선택 조각(수집기·Prometheus·Loki·Grafana와
대시보드, [`core/observability/`](../core/observability/README.md))을 주지만 운영(호스팅·백업·알림 당번)은 팀이 맡는다(14.5).
수집기를 쓰는 팀이 키트 지표를 보완할 때 아래대로 켠다. 기준 문서는
[Claude Code 모니터링](https://code.claude.com/docs/en/monitoring-usage)이며, 변수와 이벤트는 버전마다 늘어나므로 켜기 전에 확인한다.

### 14.1 어디에 설정하는가

Claude Code는 저장소의 `.claude/settings.json`·`.claude/settings.local.json`에 둔 OTel exporter 변수를 무시한다.
그래서 키트가 생성하는 `.claude/settings.json`으로는 켤 수 없다. 다음 중 하나에 둔다.

- 각자의 셸 환경 변수 또는 `~/.claude/settings.json`의 `env`
- 조직이 배포하는 managed settings의 `env`(전원에게 같은 설정)

```json
{
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.example:4317",
    "OTEL_RESOURCE_ATTRIBUTES": "team.id=my-project"
  }
}
```

- 수집기 인증 헤더(`OTEL_EXPORTER_OTLP_HEADERS`)는 Secret이다. 저장소·문서·이슈에 쓰지 않는다([Secret 규칙](secret-environment-variables.md))
- 수집기 없이 동작만 보려면 `OTEL_METRICS_EXPORTER=console`, `OTEL_METRIC_EXPORT_INTERVAL=1000`
- 내보내기 주기 기본값은 지표 60초, 이벤트 5초다

### 14.2 키트 지표와 겹치는 것

| 보고 싶은 것 | 텔레메트리 | 키트 |
| --- | --- | --- |
| hooks가 막은 횟수 | `hook_execution_complete` 이벤트의 `num_blocking` | hooks 기록 `.git/harness/events.jsonl`(worktree별, [hooks](../core/hooks/README.md)) |
| hooks가 켜져 있는지 | 세션 시작 때 hook마다 `hook_registered` 이벤트. 키트 hook(`protect-paths`·`session-start`·`session-end`·`stop-verify`)의 등록이 없는 세션은 hooks가 꺼졌거나 설치되지 않은 세션으로 본다 | 세션 기록(`sessions.jsonl`)이 없는 커밋을 `harness sessions summary`가 "기록 없는 커밋"으로 센다(대리 신호, M5-1) |
| 세션 수·응답 수·작업 시간 | 세션·이벤트 | 세션 기록 `start`·`stop`·`end`(`harness sessions summary`) |
| 도구 사용을 누가 허용·거부했나 | `tool_decision` 이벤트의 `decision`(accept·reject)과 `source`(`config`·`hook`·`user_*`) | 없음 |
| 비용·토큰 | `claude_code.cost.usage`·`claude_code.token.usage` 지표 | 세션 기록의 transcript 누적 토큰과 단가로 계산한 추정 비용(`harness sessions summary`, M5-2). 수집기 없이 로컬에서 본다 |
| 변경 줄 수·커밋·PR 수 | `claude_code.lines_of_code.count`·`claude_code.commit.count`·`claude_code.pull_request.count` 지표 | 없음 |
| 병합 후 누락, 엄격 비율, CI 실패, 판정 불일치 | 없음 | `harness metrics`(플랫폼 API) |

텔레메트리는 세션 단위 사실(무엇이 실행되고 막혔나)을, 키트 지표는 병합 단위 결과(무엇이 새어 나갔나)를 본다.
특히 `hook_registered`는 로드맵 1장 "팀 수용"의 "hooks를 끈 횟수"를 직접 잴 수 있는 신호다. 키트의 hooks 기록은 hooks가 꺼지면 남지 않는다.

### 14.3 개인정보와 Secret

기본값은 내용을 보내지 않는다. 지표와 이벤트에는 코드·파일 내용이 들어가지 않고 프롬프트는 길이만 남는다.
다음 변수는 켜지 않는 것을 기본으로 하고, 켜려면 팀 합의와 수집기의 보관 승인을 먼저 받는다.

| 변수 | 켜면 추가되는 것 |
| --- | --- |
| `OTEL_LOG_USER_PROMPTS` | 프롬프트 원문 |
| `OTEL_LOG_ASSISTANT_RESPONSES` | 응답 원문. **지정하지 않으면 `OTEL_LOG_USER_PROMPTS`를 따른다** |
| `OTEL_LOG_TOOL_DETAILS` | Bash 명령, 파일 경로, 도구 입력, hook matcher |
| `OTEL_LOG_RAW_API_BODIES` | API 요청·응답 전체 |

프롬프트만 승인받았으면 `OTEL_LOG_ASSISTANT_RESPONSES=0`을 함께 둔다. 그러지 않으면 응답 원문(응답이 옮긴 코드·Secret 포함)도 나간다.
프롬프트와 명령에는 붙여 넣은 Secret이 섞일 수 있다. 켠 상태에서 Secret이 노출되면 Secret 규칙의 노출 대응 절차를 따른다.
OAuth로 로그인하면 사용자 이메일이 속성으로 붙는다(설정한 수집기로만 간다). 수집기 접근 권한을 팀 밖으로 넓히지 않는다.

### 14.4 끄기

설정한 곳에서 `CLAUDE_CODE_ENABLE_TELEMETRY`를 지운다. 저장소 쪽에서는 켤 수 없지만 끌 수는 있다.
`.claude/settings.json`의 `env`에 `OTEL_LOGS_EXPORTER`·`OTEL_METRICS_EXPORTER`를 `none`으로 두면 그 저장소에서는 보내지 않는다.
단 managed settings, `--settings` 파일, 실행 환경에서 같은 변수를 정했으면 그쪽이 우선한다.

### 14.5 팀 대시보드 (선택 조각, M5-8)

[`core/observability/`](../core/observability/README.md)의 `docker compose up` 한 번으로 수집기·Prometheus·Loki·Grafana와
"harness AgentOps" 대시보드(사람·도구별 세션, 시간당 비용, 도구 허용·거부 비율, hooks 개입, hooks 기록 없는 세션)가 뜬다.

- 위치: 처음에는 한 사람의 PC(`127.0.0.1`, 기본)에서 띄워 본다. 팀이 같이 보려면 사내망에서 접근할 수 있는 한 대에 띄우고
  Grafana만 `HARNESS_OBS_BIND`로 연다. 수집기는 인증하지 않으므로 `127.0.0.1`에 두고 같은 호스트의 인증 프록시(TLS)만 연다
- 비밀: `GRAFANA_ADMIN_PASSWORD`와 수집기 인증 헤더(`OTEL_EXPORTER_OTLP_HEADERS`)는 Secret이다. 셸·CI 변수로만 준다
- 키트 기록: hooks의 세션·개입 기록은 `harness telemetry push`가 보낸다(기본 꺼짐, 팀이 정한 주기로 실행). 내용과 transcript 경로는
  보내지 않는다
- 내용 기록 변수는 꺼진 채 유지한다(14.3). 보관은 Prometheus·Loki 모두 30일이다
- 끄기: `docker compose down`과 Claude Code 쪽 `CLAUDE_CODE_ENABLE_TELEMETRY` 제거. 키트의 다른 기능은 그대로 동작한다
