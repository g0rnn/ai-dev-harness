# core/ci

대상 저장소가 `include: remote:`로 끌어 쓰는 GitLab CI 조각(ADR-0003). 보안 검사 4종(M1-6),
MR 본문 lint(M2-2), 측정 예약 파이프라인(M4-2)이 있다.

```text
core/ci/gitlab/
  secret-detection.yml   gitleaks
  dependency-audit.yml   trivy fs (npm package-lock.json, gradle.lockfile)
  sast.yml               semgrep
  image-scan.yml         trivy image
  mr-lint.yml            MR 본문 필수 절 검사 (M2-2, 보안 검사 아님)
  verify.yml             변경 영역 검증 명령 실행 (M5-5, 보안 검사 아님)
  metrics.yml            측정 수집 → HTML 리포트 → 주간 요약 발송 예약 파이프라인 (M4-2, 보안 검사 아님)
core/ci/mr-lint/         MR 본문 lint 모듈 원본. init이 GitLab 대상 저장소 .harness/mr-lint/에 복사한다
```

측정 스크립트 원본은 `core/metrics/`에 있고 init이 GitLab 대상 저장소 `.harness/metrics/`에 복사한다.

각 조각은 단독으로 동작한다. 보안 검사와 MR 본문 lint는 MR 파이프라인에서만, `metrics.yml`은 예약 파이프라인에서만
실행된다. 필요한 변수와 조건은 파일 머리 주석에 있다.

## 사용법

게시된 전체 커밋 SHA로 고정해 참조한다. 쓰는 조각만 넣는다. 릴리스 태그가 게시되면 변경되지 않는 태그도
사용할 수 있다. 0.3.0부터 GitHub 릴리스 태그(`v0.3.0`)를 게시한다. 태그는 옮기지 않지만, 바뀌지 않음을 보장하려면 커밋 SHA로 고정한다.

```yaml
include:
  - remote: https://raw.githubusercontent.com/kiddo-psh/ai-dev-harness/<full-commit-sha>/core/ci/gitlab/secret-detection.yml
  - remote: https://raw.githubusercontent.com/kiddo-psh/ai-dev-harness/<full-commit-sha>/core/ci/gitlab/dependency-audit.yml
  - remote: https://raw.githubusercontent.com/kiddo-psh/ai-dev-harness/<full-commit-sha>/core/ci/gitlab/sast.yml
  - remote: https://raw.githubusercontent.com/kiddo-psh/ai-dev-harness/<full-commit-sha>/core/ci/gitlab/image-scan.yml
  - remote: https://raw.githubusercontent.com/kiddo-psh/ai-dev-harness/<full-commit-sha>/core/ci/gitlab/mr-lint.yml
  - remote: https://raw.githubusercontent.com/kiddo-psh/ai-dev-harness/<full-commit-sha>/core/ci/gitlab/verify.yml
  - remote: https://raw.githubusercontent.com/kiddo-psh/ai-dev-harness/<full-commit-sha>/core/ci/gitlab/metrics.yml

variables:
  HARNESS_SCAN_IMAGE: $CI_REGISTRY_IMAGE:$CI_COMMIT_SHA   # image-scan을 쓸 때

harness-image-scan:
  needs: [build-image]   # 이미지를 올리는 로컬 job
```

GitLab Container Registry가 없는 프로젝트는 빌드한 이미지를 Docker 형식 tar 아티팩트로 넘긴다.
`build-image`는 Docker 사용 권한이 있는 runner에서 실행하는 소비자 job 예시다.

```yaml
stages: [build, test]

variables:
  HARNESS_SCAN_ARCHIVE: image.tar

build-image:
  stage: build
  script:
    - docker build -t "harness-build:$CI_COMMIT_SHA" .
    - docker save "harness-build:$CI_COMMIT_SHA" -o image.tar
  artifacts:
    access: developer
    paths: [image.tar]

harness-image-scan:
  needs:
    - job: build-image
      artifacts: true
```

`HARNESS_SCAN_IMAGE`와 `HARNESS_SCAN_ARCHIVE`는 둘 중 하나만 지정한다. tar 파일이 없거나 비어 있으면
job이 실패한다. tar 아티팩트는 크기가 클 수 있으므로 프로젝트의 아티팩트 용량 제한을 확인한다.

GitLab 17.9 이상이면 `integrity: sha256-<base64>`를 함께 적어 받은 파일이 바뀌지 않았는지 확인할 수 있다.

job 이름은 `harness-`로 시작한다. `stage`(기본 `test`), `needs`, `image` 등은 같은 이름의 job을 로컬
`.gitlab-ci.yml`에 다시 적어 덮어쓴다. 로컬에 `stages`를 정의했다면 `test`를 넣거나 `stage`를 덮어쓴다.

## 정책

| 조각 | 실패 조건 | 예외 |
| --- | --- | --- |
| `secret-detection` | MR 커밋에서 Secret 발견(뒤 커밋에서 지웠어도, merge 커밋에서 넣었어도) | `.gitleaksignore`(Fingerprint), `.gitleaks.toml`(`[extend] useDefault = true` 필수) |
| `dependency-audit` | 운영 의존성의 High·Critical이면서 고친 버전이 있는 취약점. lockfile이 없는 `package.json`·`build.gradle(.kts)`, trivy가 읽지 못한 lockfile | `.trivyignore`, `HARNESS_AUDIT_SKIP_DIRS` |
| `image-scan` | 이미지의 High·Critical이면서 고친 버전이 있는 취약점. 이미지 참조·tar 입력이 모두 없거나 둘 다 지정됨, tar 파일 누락·빈 파일 | `.trivyignore` |
| `sast` | 경고만(노란색). 이 MR이 새로 만든 발견만 보고한다. merged results에서는 대상 브랜치 현재 커밋을 기준으로 삼는다. 측정(M4) 뒤 차단으로 올린다 | `# nosemgrep: <규칙 ID>`, `.semgrepignore` |

- **도구 오류는 실패다.** DB·규칙 다운로드 실패, 설정 오류, MR 변수 없음, 얕은 clone, 기준 커밋 없음이 모두
  해당한다. 검사가 돌지 않았는데 통과로 보이지 않게 한다. semgrep도 발견만 경고로 허용한다. MR이 파일을 바꿨는데
  검사한 파일이 0개면 경고한다.
- **dev·test 의존성은 막지 않는다**(npm devDependencies, Gradle test 구성). 운영에서 돌지 않는 코드로 MR을 막으면
  검사를 끄게 된다. 측정(M4) 때 다시 본다.
- **MR이 검사 설정을 바꿀 수 없다.** 저장소의 `trivy.yaml`은 읽지 않는다. `.gitleaks.toml`이 기본 규칙을 끄면
  실패한다. `HARNESS_AUDIT_SKIP_DIRS`는 glob·`.`·`..`·절대 경로를 받지 않는다.
- **예외는 리뷰로 통제한다.** 이 MR이 예외 파일(`.gitleaksignore`, `.gitleaks.toml`, `.trivyignore`,
  `.semgrepignore`, `trivy.yaml`)이나 `HARNESS_SEMGREP_CONFIG`가 가리키는 저장소 규칙 파일을 바꿨으면
  각 job 로그 맨 앞에 경고가 나온다. 실패로 만들지는 않는다.
  예외 파일을 CODEOWNERS에 올려 보안 담당 승인을 받게 한다.

  ```text
  /.gitleaksignore  @<보안 담당>
  /.gitleaks.toml   @<보안 담당>
  /.trivyignore     @<보안 담당>
  /.semgrepignore   @<보안 담당>
  ```

- Secret이 실제로 커밋됐다면 예외로 넘기지 말고 폐기·교체한다(`docs/secret-environment-variables.md`).
  `.trivyignore`에는 CVE마다 사유와 재검토 시점을 주석으로 남긴다.
- 로그와 리포트에는 Secret 값을 남기지 않는다. gitleaks는 값을 가리고, semgrep은 규칙 ID·경로·줄 번호만 남긴다.

## 필요 조건

- Docker executor runner
- 외부 접근:
  - raw.githubusercontent.com: 조각 파일
  - Docker Hub: 도구 이미지
  - mirror.gcr.io 또는 ghcr.io: trivy DB
  - semgrep.dev: 레지스트리 규칙을 쓸 때
- 폐쇄망: 조각은 `include: project:`로 미러링하고, trivy는 `TRIVY_DB_REPOSITORY`로 DB 미러를 지정한다.
  semgrep은 `HARNESS_SEMGREP_CONFIG`에 저장소 안의 규칙 파일을 준다.
- `image-scan`: 이미지 참조 방식에는 레지스트리 접근이 필요하다. `$CI_REGISTRY` 아래 이미지는 job 토큰을
  쓴다. 다른 레지스트리는 `TRIVY_USERNAME`·`TRIVY_PASSWORD`를 보호·마스킹 변수로 준다. tar 방식에는
  빌드 job이 만든 Docker 형식 이미지 아티팩트와 이를 받는 `needs`가 필요하다.

## 알려진 한계

- 커밋 메시지와 바이너리 파일의 Secret은 보지 않는다.
- npm workspaces는 상위 `package.json`에 `"workspaces"`가 있는지만 본다. 그 목록이 이 디렉터리를 포함하는지는 보지 않는다.
- trivy DB 캐시(`.trivycache/`)는 MR 파이프라인끼리 공유한다. CI 정의를 바꿀 수 있는 MR은 캐시도 바꿀 수 있으므로
  이 조각이 막는 경계 밖이다.
- 사용자가 준 `TRIVY_USERNAME`·`TRIVY_PASSWORD`는 이미지 레지스트리와 묶이지 않는다. 그 레지스트리의 이미지에만 쓴다.
- semgrep 레지스트리 규칙(`p/default`)은 버전이 고정되지 않는다. 고정이 필요하면 저장소 안의 규칙 파일을 쓴다.
- 이미지 리포트에는 이미지의 환경 변수와 빌드 이력이 들어 있다. `artifacts:access: developer`로 Developer 이상만
  받는다(GitLab 16.11 이상). 이미지에 Secret을 굽지 않는다.

## 리포트

도구별 JSON 리포트(`gitleaks-report.json`은 값을 가린다, `trivy-dependency.json`, `trivy-image.json`,
`semgrep-report.json`은 규칙 ID·경로·줄 번호만)를 30일 동안 아티팩트로 남긴다. 측정 수집기(M4-1)가 검출 건수를 여기서 읽는다.

## 끄는 법

사람이 판단해 끈다. 끈 사실과 이유는 `friction` 이슈로 남긴다.

- 조각 전체: `include`에서 뺀다.
- 한 job: 같은 이름의 job을 로컬에 다시 적고 `rules: [{when: never}]`로 덮어쓴다.
- 오탐 하나: 위 표의 예외 방식을 쓴다.

## MR 본문 lint

`mr-lint.yml`의 `harness-mr-lint` job은 MR 본문과 변경 파일을 검사하고 실패하면 job이 실패한다(`allow_failure` 없음).
보안 검사가 아니라 절차 검사다. MR 작성자가 CI 정의나 검사 코드를 바꾸면 우회할 수 있고, 그 변경은 리뷰와 `harness check`로 본다.

| 검사 | 실패 조건 |
| --- | --- |
| 업무 참조 | `Closes` 또는 `Refs` 뒤에 참조가 있는 줄이 없다(대소문자 무시) |
| `## 검증` | 방법·결과·검증하지 못한 것(미검증) 줄이 없거나 값이 비었다. 값은 같은 줄 또는 들여쓴 다음 줄 |
| `## 영향 범위` | 체크한 항목(`[x]`)이 없다. 영향이 없으면 "해당 없음"을 체크한다 |
| `## 판정` | 첫 내용 줄의 첫 낱말이 `lite`·`standard`·`strict`(경량·표준·엄격)가 아니다 |
| 엄격 | 적용 판정이 엄격인데 `## 플랜 요약`·`## 리뷰 결과`가 없거나 비었거나 "해당 없음"이다. `## 리뷰 결과` 측정 칸(리뷰 템플릿 6장 다섯 항목)이 빠졌거나 빈칸이 있다 |
| `plans/` | MR이 루트 `plans/` 아래 파일을 추가·수정했다(삭제는 허용). `.gitignore`에 `/plans/` 줄이 없다 |
| 보호 경로 | MR이 보호 경로(hooks와 같은 규칙: lock 파일·CI 정의·DB 마이그레이션·`.env.example`·`harness.json`·`.claude/settings*.json`·`.claude/hooks/`·`.harness/`·영역 계약 문서, `hooks.protected_paths`로 바꾼 목록)를 추가·수정·삭제했는데 `## 보호 경로 확인`에 체크한 항목(`[x]`)이 없다(M5-5) |

- **판정은 높은 쪽.** 변경 파일(`CI_MERGE_REQUEST_DIFF_BASE_SHA`와 소스 커밋의 merge-base 이후)로 `harness judge`와 같은
  판정을 다시 계산해 본문 판정과 높은 쪽을 적용한다. 다르면 불일치로 로그·`mr-lint.json`에 남긴다(측정 4번). 불일치만으로는
  실패하지 않지만 엄격이 적용되면 엄격 검사가 붙는다. "사람 확인 필요" 트리거 문장도 로그에 나온다.
- **본문 출처.** `CI_MERGE_REQUEST_DESCRIPTION`(GitLab 16.7 이상). 잘렸을 때만 API로 읽는다(아래). HTML 주석(템플릿 안내문)은
  지우고 펜스 코드 블록(```·~~~) 안의 줄은 검사에 쓰지 않는다. 4칸 들여쓴 코드 블록은 목록 항목의 이어 쓰기와 가르지 않고
  본문으로 읽는다(검증 절의 들여쓴 값을 살리기 위해서다).
- **본문만 고치면 다시 돌지 않는다.** 본문을 고친 뒤 MR의 Pipelines 탭에서 새 파이프라인을 실행한다.
- **2700자 제한.** GitLab은 이 변수를 2700자에서 자른다. 잘렸으면(`CI_MERGE_REQUEST_DESCRIPTION_IS_TRUNCATED`) 잘린 본문으로
  판정하지 않는다. `HARNESS_COMMENT_TOKEN`이 있으면 같은 CI의 API(`CI_API_V4_URL`, https만)에서
  `GET /projects/:id/merge_requests/:iid`로 실행 시점의 전체 본문을 읽어 검사한다(잘리지 않았으면 호출하지 않는다).
  토큰은 리다이렉트로 넘기지 않고 출력하지 않으며, 응답은 5 MB까지만 읽는다. 토큰이 없거나 읽지 못하면(HTTP 오류, 형식 오류,
  다른 MR의 응답) 실패(종료 2)하고 로그에는 HTTP 코드나 오류 종류만 남는다. 토큰을 쓰지 않으면 템플릿 안내 주석을 지우거나
  긴 내용을 링크로 옮긴다.
- **검사 코드.** `harness init`이 GitLab 대상 저장소에 넣은 `.harness/mr-lint/mr_lint.py`를 `python3 -I -B`로 실행하고, 판정에는
  `.claude/hooks/harness_common.py`와 `harness.json`을 쓴다. 셋 중 하나라도 없으면 실패한다. 이미지는 git이 든
  `python:3.12.15-bookworm`(digest 고정)이다.
- **검사할 수 없으면 실패다.** MR 변수 없음, 기준·소스 커밋 없음(얕은 clone이면 `GIT_DEPTH: "0"` 확인), 설정 오류는 종료 코드 2.
- **판정 댓글(선택).** CI 변수 `HARNESS_COMMENT_TOKEN`이 있으면 결과를 표식(`<!-- harness-mr-lint -->`) 댓글 하나로 남기고
  다시 돌면 같은 댓글을 고친다(토큰 사용자의 댓글만). 댓글에는 판정·불일치·실패 항목만 싣고 본문이나 경로를 옮기지 않는다.
  게시에 실패하면 경고만 내고 lint 결과는 그대로다. MR 파이프라인은 작성자 코드와 같이 돌아 이 토큰은 MR을 올릴 수 있는
  사람이 읽을 수 있다. 쓰려면 Reporter 역할의 전용 프로젝트 액세스 토큰을 Masked로 등록한다(MR 파이프라인에서
  읽어야 하므로 Protected로 두면 보통 비어 있다). 범위는 댓글까지 쓰면 `api`, 잘린 본문 읽기만 쓰면 `read_api`다(`read_api`면
  댓글은 경고로 끝난다). 이 노출을 받아들일 수 없으면 등록하지 않는다.
  키트 자기 적용(GitHub)은 같은 이유로 검사 job(토큰 없음)과 댓글 job(기준 커밋 코드만 토큰 사용)을 나눴다(아래 "키트 자기 적용").
- **통합 MR은 건너뛴다.** `Release.md` 템플릿에는 업무 참조·판정 절이 없다. 소스 브랜치가 `harness.json`의
  `integration_branch`, 대상이 `default_branch`이고(둘이 다를 때만) 같은 프로젝트의 MR이면(`CI_MERGE_REQUEST_SOURCE_PROJECT_ID`
  = `CI_MERGE_REQUEST_PROJECT_ID`) 본문·변경 파일을 보지 않고 "건너뜀(통과)"으로 끝낸다(종료 0, 로그 한 줄,
  `mr-lint.json`의 `"result": "skipped"`). fork에서 같은 이름의 브랜치로 올린 MR은 검사한다. 브랜치 값은 MR이 고친
  `harness.json`이 아니라 대상 브랜치 최신 커밋(`CI_MERGE_REQUEST_TARGET_BRANCH_SHA`, merged results 파이프라인에서만 있고
  없으면 diff 기준 커밋 `CI_MERGE_REQUEST_DIFF_BASE_SHA`)의 값으로 본다. 같은 MR에서 값을 바꿔 자기 검사를
  끄지 못하게 하려는 것이고, 기준 커밋의 값을 읽지 못하면 건너뛰지 않는다. 판정 댓글은 남기지 않으므로 대상을 바꾸기 전에 달린
  이전 판정 댓글은 그대로 남는다. rules를 로컬에서 덮어쓸 필요가 없다.

- 리포트: `mr-lint.json`(결과 `pass`·`fail`·`error`·`skipped`, 실패 항목, 본문·변경 파일·적용 판정과 불일치, 파일별 판정, 사람 확인 목록, 댓글 상태)을 30일 보관한다.
  측정 수집기(M4-1)가 판정 불일치를 여기서 읽는다.

### 보호 경로 확인 (M5-5)

hooks는 Claude Code에만 걸린다. Codex 같은 다른 도구나 hooks를 끈 세션의 변경은 MR 단계에서 같은 규칙으로 잡는다.

- 규칙은 hook과 같은 코드(`harness_common.rules_for`·`decide`)이고, 설정은 **대상 쪽 기준 커밋**의 `harness.json`을 쓴다.
  MR이 같은 MR에서 `protected_paths`를 비워 검사를 끌 수 없다(기준 커밋에 설정이 없으면 MR 쪽 설정). 그 설정이 hook과 같은
  검사를 통과하지 못하면 끌 수 없는 규칙과 기본 목록으로 검사하고 경고를 남긴다(설정을 고치는 MR을 막지 않는다)
- `.harness/`(이 검사 코드)와 `harness.json`은 끌 수 없는 규칙이다. 다만 MR이 CI 정의를 바꿔 job을 빼는 것은 막지 못한다
  (보호 경로 확인이 요구될 뿐이다). 최종 방어선은 보호 브랜치의 승인 규칙이다
- GitHub 소비자에게는 MR lint·변경 영역 검증 조각이 설치되지 않는다(GitLab 조각). 템플릿 절은 같지만 강제는 GitLab에서만 된다
- hook은 lock 파일을 AI에게 막지만(`block`), 사람이 의존성을 갱신하는 정상 작업이 있어 lint는 `block`·`ask` 모두 사람
  확인 표시로 통과시킨다. 체크는 자기 신고다. 누가 체크했는지는 플랫폼의 승인 규칙(코드 소유자·필수 승인)으로 통제한다
- 실패 메시지는 보호 경로와 규칙 사유를 8개까지 보여 준다

## 변경 영역 검증 (`verify.yml`, M5-5)

MR이 바꾼 파일이 속한 영역의 검증 명령을 CI에서 실행한다. 종료 검증 hook과 같은 선택(`harness_common.commands_for`)이라
hooks가 없는 도구로 만든 MR도 같은 검증을 거친다. 검사 코드는 `init`이 GitLab 대상에 넣는 `.harness/verify/verify_changed.py`다.

- `HARNESS_VERIFY_IMAGE`(필수): 영역 도구(JDK·Node 등)와 python3가 든 이미지. 준비 단계는 팀이 `before_script`로 덧붙인다
- 기준은 `CI_MERGE_REQUEST_DIFF_BASE_SHA`(대상 쪽 기준 커밋)다. fork MR에서도 fork의 같은 이름 브랜치가 아니라 대상 프로젝트
  기준과 비교한다(#100 리뷰 B F2). 그 커밋을 가져오지 못하면 실패한다
- 변경 파일은 기준과의 merge-base 이후 커밋이다(`GIT_DEPTH: "0"`). 실행할 명령이 없으면 통과, 하나라도 실패하면 실패
- 로컬에서 같은 검사: `python .harness/verify/verify_changed.py --base origin/<대상 브랜치>`

## 측정 예약 파이프라인

`metrics.yml`의 `harness-metrics` job은 **예약 파이프라인**(`$CI_PIPELINE_SOURCE == "schedule"`)에서 주 1회 돌며 측정 수집기 →
주간 HTML 리포트 → 주간 요약 발송을 한 job에서 차례로 실행하고 `metrics.json`·`report.html`을 아티팩트로 90일 게시한다.
보안 검사가 아니라 측정이다(판정 표준). MR 파이프라인에서는 돌지 않고, Run pipeline(web)으로 만든 파이프라인에서는 수동 job으로
시험할 수 있다. 실행 코드는 `harness init`이 GitLab 대상 저장소에 넣은 `.harness/metrics/{collect,report,notify,classify_ci}.py`다
(`python3 -I -B`로 실행, `harness check`가 드리프트를 본다). 재판정에는 `.harness/mr-lint/mr_lint.py`·`.claude/hooks/harness_common.py`·
`harness.json`을 쓴다. 이 파일이 없는 기존 소비자는 `harness init <target> --force`로 받는다(그 전엔 `check`가 "없음"으로 알린다).
지표 정의와 각 스크립트의 옵션은 [`core/metrics/README.md`](../metrics/README.md)에 있다.

### 변수

CI/CD Variables에 등록한다(`docs/secret-environment-variables.md` 7장). 예약 파이프라인은 대상 브랜치가 보호 브랜치일 때만 Protected
변수를 읽으므로 기본 브랜치를 보호 브랜치로 두거나 Protected를 끈다. 발송 변수가 하나도 없으면 수집·리포트만 하고 발송은
로그 한 줄로 건너뛴다. 키트는 어떤 값도 저장하지 않는다.

| 변수 | 필수 | 설명 |
| --- | --- | --- |
| `HARNESS_METRICS_TOKEN` | 필수 (Masked) | 프로젝트 액세스 토큰 `read_api`(Reporter 이상). `collect`가 `PRIVATE-TOKEN` 헤더로만 쓴다. CI job 토큰은 MR·파이프라인 목록 API를 읽지 못한다 |
| `HARNESS_METRICS_SINCE` | 선택 | 수집 시작일 `YYYY-MM-DD`. 없으면 8주 전. 병합 뒤 늦게 발견되는 결함을 놓치지 않으려면 프로젝트 시작일로 고정한다 |
| `HARNESS_METRICS_UTC_OFFSET` | 선택 | 주 경계 시간대(기본 `+09:00`) |
| `HARNESS_NOTIFY_WEBHOOK_URL` | 선택 (Masked) | incoming webhook URL(Slack·Mattermost 공통, `POST {"text": 본문}`). 있으면 webhook으로 보낸다 |
| `HARNESS_MATTERMOST_URL` | 선택 | Mattermost 서버 https 주소. webhook 변수가 없고 이 변수가 있으면 계정 로그인으로 보낸다(incoming webhook이 꺼진 서버용) |
| `HARNESS_MATTERMOST_LOGIN_ID` | 선택 | 로그인 ID. 비밀번호가 CI 변수에 들어가므로 개인 계정 대신 **봇 계정**을 권장한다 |
| `HARNESS_MATTERMOST_PASSWORD` | 선택 (Masked) | 비밀번호 |
| `HARNESS_MATTERMOST_CHANNEL_ID` | 선택 | 채널 ID. 없으면 아래 팀·채널 이름으로 찾는다 |
| `HARNESS_MATTERMOST_TEAM` | 선택 | 팀 이름(URL의 팀 slug). `HARNESS_MATTERMOST_CHANNEL`과 함께 쓴다 |
| `HARNESS_MATTERMOST_CHANNEL` | 선택 | 채널 이름(URL의 채널 slug) |

### 스케줄 만들기

스케줄은 사람이 GitLab UI에서 만든다(키트는 만들지 않는다). 프로젝트 → Build → Pipeline schedules → New schedule.
월요일 09:00 KST면 Cron timezone을 UTC로 두고 `0 0 * * 1`, 또는 timezone을 Asia/Seoul로 고르고 `0 9 * * 1`. 대상 브랜치는
기본 브랜치. 만든 뒤 Run(▶)으로 바로 한 번 돌려 변수·토큰을 확인한다. 수동 시험은 Build → Pipelines → Run pipeline으로 만든
파이프라인(`web`)에서 `harness-metrics`를 실행한다(수동 job이라 자동으로 돌지 않는다).

### 아티팩트와 리포트 링크

- `metrics.json`(주별 지표·원자료, 토큰·본문 원문 없음)과 `report.html`(정적 HTML 한 파일)을 90일 보관한다(`when: always`,
  실패해도 만들어진 파일은 남는다).
- `expose_as: 'harness metrics report'`로 파이프라인·MR 화면에 링크가 보인다. 주간 요약 메시지의 리포트 링크는
  `$CI_JOB_URL/artifacts/file/report.html`이다(아티팩트 접근 권한이 있는 구성원만 연다).
- 수집 창은 `HARNESS_METRICS_SINCE`(없으면 8주 전)부터 실행 시각까지라 마지막 주는 "(진행 중)"으로 표시된다.
  GitLab Pages 게시는 범위 밖이다(SSAFY 지원 확인 뒤 선택).

### 끄는 법

- 잠시: Pipeline schedules에서 스케줄을 비활성화한다(변수와 코드는 그대로).
- 완전히: `include`에서 `metrics.yml`을 빼고 스케줄을 지운다. 같은 이름의 job을 로컬에 `rules: [{when: never}]`로 덮어써도 된다.
- 발송만: 발송 변수(`HARNESS_NOTIFY_WEBHOOK_URL`, `HARNESS_MATTERMOST_*`)를 지우면 수집·리포트만 남는다.

### 한계

- 예약 파이프라인은 대상 브랜치(보통 기본 브랜치)의 `.gitlab-ci.yml`과 `.harness/metrics/`를 쓴다. 다른 브랜치의 변경은 반영되지 않는다.
- 재판정은 수집 시점의 `harness.json` 규칙이다. 규칙을 바꾼 주 전후 비교에 유의한다(`core/metrics/README.md`).
- 메시지는 평문 Markdown 한 통이다(Slack 블록·첨부 서식 없음). Jira 조회·일일 요약은 백로그다.
- `stage`(기본 `test`)·`image`·`timeout`(15분)은 같은 이름의 job을 로컬에 다시 적어 덮어쓴다. 로컬에 `stages`를 정의했다면
  `test`를 넣거나 `stage`를 덮어쓴다.

## 키트 자기 적용

키트 저장소의 PR에서는 `.github/workflows/security.yml`이 `secret-detection`과 `sast` 조각을 GitHub Actions로 실행한다(M1-7).
`.github/scripts/run_fragment.py`가 조각에서 이미지·job 변수·스크립트·`allow_failure: exit_codes`를 꺼내 `docker run`으로
돌리고, GitLab MR 변수는 PR merge 커밋에서 만든다(첫 부모 = 대상, 둘째 부모 = PR head, 그 merge-base = 기준).
셸은 GitLab Docker executor처럼 bash가 있으면 `bash -eo pipefail`이다. 조각의 형식은 이 러너가 읽는 모양을 지킨다.
최상위 job 하나, 알려진 키(`stage`·`image`·`variables`·`cache`·`rules`·`interruptible`·`allow_failure`·`script`·`artifacts`),
`script:` 아래 `- |` 블록 하나, 변수 값은 큰따옴표 또는 맨 값(`$` 없음)이다. 모르는 모양은 실패한다(스크립트 일부만 돌려 통과하지 않는다).
PR이 workflow·러너·조각을 바꿨으면 job 로그에 경고가 나온다.
키트에는 lockfile과 이미지가 없어 의존성 감사·이미지 스캔은 리허설(M1-8)에서 돌린다.

MR 본문 lint는 `ci` workflow의 `mr-lint` job이 같은 모듈 원본(`core/ci/mr-lint/mr_lint.py`)을 실행한다. 본문은 이벤트 파일의
`pull_request.body`, 변경 범위는 `pull_request.base.sha`·`head.sha`다. PR 본문만 고쳐도 다시 돌도록 `pull_request` 타입에
`edited`를 넣었다(같은 workflow의 다른 job도 함께 돈다). `pull_request` 이벤트만 쓴다(`pull_request_target`은 거부).
PR이 바꾼 검사 코드(`mr_lint.py`, `harness_common.py`)가 쓰기 토큰과 함께 돌지 않게 두 job으로 나눈다. `mr-lint`는 PR 커밋을
`contents: read` 권한·토큰 없이 `--no-comment`로 검사하고(이 job의 종료 코드가 PR 체크다) `mr-lint.json`을 아티팩트로 올린다.
`mr-lint-comment`는 `pull-requests: write` 권한으로 기준 커밋(`pull_request.base.sha`)을 checkout해 그 원본의
`--post-report`로 아티팩트를 읽고, 결과·판정 값·불일치 여부·실패 항목(한 줄 300자, 30개까지, 줄바꿈·HTML·멘션·코드 울타리
무력화)만 검증해 `GITHUB_TOKEN`으로 판정 댓글을 남긴다(결과가 `skipped`이면 남기지 않는다). 이 job은 실패하지 않는다.
통합 PR 건너뜀은 `pull_request.head.ref`·`base.ref`와 `head.repo.full_name` = `base.repo.full_name`으로 판단한다(키트는
`integration_branch`와 `default_branch`가 모두 `main`이라 건너뛰지 않는다). fork PR처럼 쓰기 권한이 없거나 기준 커밋에
모듈이 없으면(이 job을 처음 들이는 PR) 경고만 낸다. `security` workflow와 `run_fragment.py`는 이 job과 관계없다.

`ci.yml`의 `rehearsal` job은 `.github/scripts/rehearse.py`로 `init` 대상에 fixture를 만들어 조각 4종을 모두 실행한다.
검출 시나리오(지운 Secret, lodash 4.17.20 CVE-2021-23337, log4j-core 2.14.1 CVE-2021-44228, alpine 3.10 CVE-2021-36159,
SAST 발견, lockfile 누락, 이미지 입력 누락)는 종료 코드와 리포트의 기대 ID를 함께 본다. 도구 오류로 실패한 것을
기대대로 실패한 것으로 읽지 않기 위해서다. 가짜 Secret과 취약 lockfile은 실행 중에 만들고 키트에 커밋하지 않는다.
외부 레지스트리와 DB에 기대므로 필수 체크로 두지 않는다. 로컬에서는 `python .github/scripts/rehearse.py security`로 돌린다.

## 갱신

도구 버전은 태그와 digest로 고정한다(보안 도구 자체가 공급망 경로다). 버전을 올리는 것도 보안 검사
변경이라 엄격 판정이다(`docs/contributing.md`).
