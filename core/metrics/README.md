# core/metrics

키트의 효과를 숫자로 보이는 수집기·리포트·주간 요약 발송. 로드맵 M2-6(CI 실패 분류)과 M4-1·M4-6·M4-2·M4-7에서 채웠다.

수집하는 지표 (roadmap 1장). 원천은 모두 플랫폼 API다(MR lint 아티팩트는 30일에 만료되고 `plans/`는 커밋하지 않는다).
지표 5는 각자의 worktree에만 있는 hooks 기록을 MR 본문에 옮겨 적은 값을 API로 읽는다.

| # | 지표 | 출처 (`collect.py`) |
| --- | --- | --- |
| 1 | 병합 후 발견된 누락 수 | `escaped-defect` 라벨 이슈·MR 본문의 `원인: !<MR>`(GitLab)·`원인: #<PR>`(GitHub) → 원인 MR의 병합 주 |
| 2 | 엄격 단계 MR 비율 | 기간 내 병합 MR의 유효 판정(본문 `## 판정`과 변경 파일 재판정 중 높은 쪽)이 엄격인 수 / 병합 MR 수 |
| 3 | CI 실패 원인 분류 | 기간 내 실패 job과 trace → `classify_ci.py` 범주 6개 |
| 4 | 판정 스크립트와 사람 판정의 불일치 수 | 본문 판정 ≠ 변경 파일 재판정인 병합 MR 수(MR lint `tier.mismatch`와 같은 정의) |
| 5 | hooks 개입 수와 보고 MR 수 | 병합 MR 본문 `## hooks 기록`의 차단·확인·종료 검증 실패 수 합계, 절을 채운 MR 수 / 집계 MR 수(아래 "hooks 기록") |

네 스크립트(`collect.py`·`report.py`·`notify.py`·`classify_ci.py`)는 표준 라이브러리만 쓰고, `harness init`이 GitLab 대상 저장소의
`.harness/metrics/`에 그대로 복사한다(`check`가 드리프트를 본다). 소비자 CI에서는 예약 파이프라인 조각 `core/ci/gitlab/metrics.yml`이
주 1회 수집 → 리포트 → 발송을 실행하고 아티팩트를 게시한다([`core/ci/README.md`](../ci/README.md) "측정 예약 파이프라인").

## 수집기 (`collect.py`, `harness metrics collect`)

기간 내 병합 MR·결함 이슈·실패 CI job을 플랫폼 API로 읽어 ISO 주별 지표와 원자료를 JSON 한 파일로 쓴다.
표준 라이브러리만 쓰며, 소비자 CI에서 `python collect.py ...`로 단독 실행할 수 있다(같은 디렉터리의 `classify_ci.py`가 필요하다).

```bash
python bin/harness.py metrics collect --platform gitlab --since 2026-09-01 [--until 2026-10-01] --out metrics.json
python core/metrics/collect.py --platform github --since 2026-09-01 --security-jobs secret-detection,sast
```

| 옵션 | 기본 | 설명 |
| --- | --- | --- |
| `--platform` | (필수) | `gitlab` 또는 `github` |
| `--since` / `--until` | (필수) / 지금 | `YYYY-MM-DD`. `--since`는 포함, `--until`은 제외. `--utc-offset` 기준 0시 |
| `--out` | `metrics.json` | 출력 파일. 임시 파일에 쓴 뒤 교체한다(실패하면 기존 파일을 남긴다) |
| `--api-url` | GitLab `CI_API_V4_URL`, GitHub `GITHUB_API_URL` 또는 `https://api.github.com` | https만 허용 |
| `--project` | GitLab `CI_PROJECT_ID`, GitHub `GITHUB_REPOSITORY` | GitLab은 ID 또는 `group/project`, GitHub는 `owner/repo` |
| `--utc-offset` | `+09:00` | 주 경계 시간대. `zoneinfo`는 Windows에 tzdata가 없어 쓰지 않는다(KST는 서머타임이 없다) |
| `--defect-label` | `escaped-defect` | 지표 1 라벨 |
| `--security-jobs` | 없음 | GitHub 보안 검사 job 이름(쉼표 구분). `harness-` 접두를 붙여 `security`로 분류한다 |
| `--common` / `--config` | `.claude/hooks/harness_common.py` / `harness.json` | 재판정에 쓰는 판정 코드와 규칙(저장소 루트 기준) |
| `--mr-lint` | 키트 `core/ci/mr-lint/mr_lint.py`, 없으면 `.harness/mr-lint/mr_lint.py` | 판정 정의(`lint_body`)를 가져올 MR lint |

종료 코드: 성공 0, 입력·설정·API 오류 2. 오류는 HTTP 상태와 URL 경로만 보인다.

### 토큰

토큰은 환경 변수로만 받는다(CI 변수는 masked·protected). 출력 파일·오류 메시지에 쓰지 않고, 다른 호스트(또는 https → http)로
가는 리다이렉트·다음 페이지 요청에는 인증 헤더를 붙이지 않는다(GitHub 로그는 저장소 서버로 리다이렉트된다).

| 플랫폼 | 환경 변수 | 필요한 범위 |
| --- | --- | --- |
| GitLab | `HARNESS_METRICS_TOKEN`(필수, `PRIVATE-TOKEN` 헤더) | 프로젝트 액세스 토큰 `read_api`(Reporter 이상). CI job 토큰은 MR·파이프라인 목록 API를 읽지 못해 쓰지 않는다 |
| GitHub | `HARNESS_METRICS_TOKEN`, 없으면 `GITHUB_TOKEN`(`Authorization: Bearer`) | fine-grained 토큰 `contents:read`·`pull-requests:read`·`issues:read`·`actions:read`. workflow에서는 `permissions`에 같은 범위를 준다 |

### 수집 방식

- 병합 MR: GitLab `merge_requests?state=merged&updated_after=`(병합 시각 필터는 버전마다 달라 갱신 시각으로 받고 `merged_at`으로 거른다),
  GitHub `pulls?state=closed&sort=updated`(기간 시작보다 오래 갱신된 PR이 나오면 멈춘다). 페이지는 `Link: rel="next"`를 따른다(100개씩).
- 판정: 변경 파일(GitLab `merge_requests/:iid/diffs`, GitHub `pulls/:n/files`의 옛·새 경로)을 `harness_common.judge`로 다시 판정하고
  `mr_lint.lint_body(본문, 재판정)`의 `tier`를 그대로 쓴다. 통합 MR(`integration_branch` → `default_branch`)은 MR lint처럼 검사 대상이
  아니라 원자료에 `integration: true`로만 남기고 지표에 세지 않는다.
- **재판정 기준 주의**: 재판정은 수집 시점의 `harness.json` 규칙을 쓴다. MR이 병합될 때의 규칙과 다르면 그때 MR lint가 낸 판정과
  달라질 수 있다(규칙을 바꾼 주 전후 비교에 유의한다).
- 실패 job: GitLab은 `updated_after`(기간 시작)로 받은 파이프라인 → 실패 job(`scope[]=failed`, 재시도 포함) → trace. 기간 뒤에 끝났거나
  재시도한 파이프라인도 받아 job `created_at`으로 거른다(`updated_before`로 자르면 그 job이 빠진다). GitHub는 기간 내 workflow run(결론이
  실패·시간 초과·취소이거나 다시 실행한 run) → job(`filter=all`) → 로그. GitHub job은 위 어댑터와 같은 변환을 한다. trace는 끝 1 MiB만 읽어 분류하고, 만료(404·410)면 메타데이터만으로 분류한다. 주는 job `created_at`(GitHub는 run `created_at`).
  GitHub는 필터를 준 run 목록을 1,000개까지만 돌려준다.

### 병합 후 결함 라벨 규칙 (지표 1)

병합 뒤 발견한 누락은 이슈(또는 수정 MR)를 만들고 라벨 `escaped-defect`를 붙인 뒤 본문에 원인 MR을 한 줄로 적는다.
이슈와 수정 MR 중 하나에만 라벨을 붙인다(둘 다 붙이면 두 번 센다).

```text
원인: !12      (GitLab MR 번호)
원인: #12      (GitHub PR 번호)
```

- 원인 MR의 **병합 주**에 센다. 원인 MR이 기간 밖에 병합됐으면 세지 않는다(`status: out_of_window`).
- 원인 줄이 없으면 이슈 생성 주의 `unlinked_defects`로 따로 센다(`status: unlinked`). 주석·코드 블록 안의 원인 줄은 읽지 않는다.
- 결함은 생성 시각이 기간 안(`--since` 이상 `--until` 미만)인 것만 센다. 기간 뒤에 만든 결함은 원자료에 `status: out_of_window`로만
  남아 같은 창을 다시 수집해도 결과가 같다. 병합 뒤 늦게 발견되는 결함을 놓치지 않으려면 주간 수집은 `--until`을 생략(지금)하고
  `--since`를 프로젝트 시작으로 고정하거나 충분히 길게 둔다. 원인 MR이 병합되지 않았거나 없으면 `status: cause_not_merged`.

### hooks 기록 (지표 5)

hooks는 보호 경로 판정(`block`·`ask`)과 종료 검증 실패를 각 worktree의 `.git/harness/events.jsonl`에 남긴다
([`core/hooks/README.md`](../hooks/README.md)). 이 파일은 개인 PC에만 있으므로 MR을 만들기 전에 작업한 worktree에서
요약을 만들어 MR 본문에 붙이고, 수집기는 병합 MR 본문에서 그 수를 읽는다.
수집기는 병합된 본문만 읽으므로, 리뷰를 반영한 뒤 병합 직전에 다시 실행해 절을 바꾼다(MR을 연 뒤의 개입이 빠지지 않게).

```bash
python bin/harness.py events summary ../my-project            # 현재 브랜치. 키트 자신은 --self
python bin/harness.py events summary ../my-project --branch feat/ABC-12
```

출력(그대로 MR 본문의 `## hooks 기록` 절을 바꾼다. 템플릿에 절이 있다):

```text
## hooks 기록

- 차단(block): 2
- 확인(ask): 1
- 종료 검증 실패: 0
- 규칙: `**/db/migration/` ask 1; `package-lock.json` block 2
```

- 기록 중 `branch`가 현재(또는 `--branch`) 브랜치인 줄만 센다. `branch` 필드가 없는 옛 기록과 읽을 수 없는 줄은 빼고 그 수를
  표준 오류에 알린다. 기록이 없으면 세 수를 0으로 낸다. detached HEAD면 `--branch`가 필요하다(종료 2)
- 종료 검증 실패는 `stop-verify`의 `fail` 기록 수다(보류 뒤 다시 실패한 기록 포함)
- 수집기는 주석·코드 블록을 지운 `## hooks 기록` 절에서 `차단(block): N`·`확인(ask): N`·`종료 검증 실패: N` 세 줄을 모두 찾아야
  보고로 본다(목록 기호·전각 쌍점 허용, 같은 줄이 또 나오면 처음 값). 절이 없거나, 템플릿의 "기록 없음"이거나, 세 줄 중 하나라도
  없으면 미보고다. 수는 6자리(999999)까지만 읽고 그보다 긴 값은 미보고로 본다(본문은 신뢰할 수 없는 입력). 규칙 줄은 읽지 않는다
- 미보고 MR 수는 hooks를 끄고 작업했거나 절차를 건너뛴 MR의 대리 지표다. hooks를 끈 사실 자체는 기록이 없어 알 수 없다
- 본문에 손으로 쓴 수도 그대로 센다. 요약은 자기 신고이며 검증하지 않는다
- 통합 MR은 다른 지표처럼 읽지 않는다. MR lint는 이 절을 검사하지 않는다(필수가 아니다)

### 출력 (`version` 1)

```json
{"version": 1, "platform": "gitlab", "project": "123",
 "window": {"since": "2026-09-21T00:00:00+09:00", "until": "2026-10-05T00:00:00+09:00", "utc_offset": "+09:00"},
 "generated_at": "2026-10-05T09:00:00+09:00",
 "weeks": [{"week": "2026-W39", "start": "2026-09-21", "merged_mrs": 3, "strict": 1, "mismatch": 1,
            "escaped_defects": 1, "unlinked_defects": 0,
            "ci_failures": {"test": 1, "format": 0, "infra": 0, "timeout": 0, "security": 0, "unclassified": 0},
            "hooks": {"block": 2, "ask": 1, "verify_fail": 0, "reported_mrs": 1, "unreported_mrs": 2}}],
 "mrs": [{"number": 11, "title": "...", "url": "...", "merged_at": "...", "week": "2026-W39",
          "source_branch": "feat/11", "target_branch": "develop", "integration": false, "files": 3,
          "paths": ["docs/guide.md", "secure/token.py", "src/token.py"],
          "tier": {"body": "lite", "judge": "strict", "effective": "strict", "mismatch": true}, "mismatch_higher": "judge",
          "hooks": {"block": 2, "ask": 1, "verify_fail": 0}}],
 "defects": [{"kind": "issue", "number": 40, "title": "...", "url": "...", "created_at": "...", "cause": 12,
              "cause_merged_at": "...", "week": "2026-W39", "status": "linked"}],
 "jobs": [{"id": 501, "name": "backend-test", "stage": "test", "failure_reason": "script_failure", "category": "test",
           "rules": ["test.pytest"], "pipeline": 100, "url": "...", "created_at": "...", "week": "2026-W39"}]}
```

- `weeks`는 기간과 겹치는 모든 ISO 주를 빈 주까지 0으로 낸다. `ci_failures`는 범주 6개를 항상 모두 둔다.
- 엄격 비율은 `strict / merged_mrs`(리포트가 계산한다). `mismatch_higher`는 불일치일 때 높은 쪽(`body`·`judge`).
- `hooks`(M4-7에서 추가, `version`은 1 그대로)는 주마다 항상 있고, MR에서는 미보고·통합 MR이면 `null`이다.
  `reported_mrs + unreported_mrs = merged_mrs`. 리포트·발송은 `hooks`가 없는 옛 JSON도 0으로 렌더한다.
- 원자료는 번호·URL·제목·시각·주·판정·범주와 MR 변경 파일 경로(`paths`)·hooks 수만 담는다. MR·이슈 본문과 로그 원문은 넣지 않는다.
- `paths`는 재판정에 쓴 옛·새 경로다(통합 MR에는 없다). `harness metrics cases`가 이 경로로 판정 eval 사례 후보를 만든다
  ([`docs/install.md`](../../docs/install.md) 3.4). `paths` 이전에 수집한 파일은 사례 후보에서 빠진다.

테스트는 `tests/test_metrics_collect.py`, API 응답 fixture는 `tests/fixtures/metrics/`(합성 데이터, 실제 호스트·Secret 없음)에 둔다.

## 리포트 (`report.py`, `harness metrics report`)

수집기가 쓴 `metrics.json`(`version` 1)을 읽어 5개 지표의 주별 추세와 이번 주 요약, 원자료 표를 정적 HTML 한 파일로 쓴다.
표준 라이브러리만 쓰고 다른 키트 모듈을 import하지 않아 소비자 CI에서 `python report.py ...`로 단독 실행할 수 있다(네트워크 없음).

```bash
python bin/harness.py metrics report --in metrics.json --out report.html
python core/metrics/report.py --in metrics.json --out report.html --title "냠냠코치 측정 리포트"
```

| 옵션 | 기본 | 설명 |
| --- | --- | --- |
| `--in` | `metrics.json` | 수집기 출력(`version` 1). 50 MB 상한 |
| `--out` | `report.html` | 출력 HTML. 임시 파일에 쓴 뒤 교체한다(실패하면 기존 파일을 남긴다) |
| `--title` | `<project> 측정 리포트` | 문서 제목 |

내용

- 머리: 제목, 프로젝트·플랫폼·기간·생성 시각·주 수(시각은 `window.utc_offset` 기준)
- 이번 주 요약: 마지막 주의 5개 지표와 직전 주 대비 증감(`+1`·`-2`·`±0`, 비율은 `%p`, 주가 하나면 `-`). 마지막 주 시작일부터
  7일이 기간 끝(`window.until`)보다 뒤면 "(진행 중)"을 붙인다. 엄격 비율은 병합 MR이 없는 주면 `-`. 지표 5는 차단·확인·종료 검증
  실패의 합(hooks 개입 수)과 참고 줄 "hooks 기록 보고 MR"(`보고/집계`, 증감 없음)
- 그래프 5개(인라인 SVG)와 바로 뒤에 같은 값의 표: 지표 1 막대(병합 후 누락·원인 미연결), 지표 2 선(엄격 비율 %,
  점마다 `strict/merged_mrs`), 지표 3 누적 막대(범주 6개 고정 순서·고정 색·범례), 지표 4 막대(판정 불일치),
  지표 5 나란한 막대(차단·확인·종료 검증 실패, 표에 보고 MR). x축은 ISO 주와 시작일, y축은 0부터(최소 1). 값 라벨을 그려
  색만으로 구분하지 않는다
- 원자료: `<details>`로 접은 MR(번호·제목·병합 시각·주·본문/재판정/유효 판정·불일치·hooks 기록·통합 여부)·결함·job 표.
  번호·id는 링크. hooks 기록은 `차단 N·확인 N·검증 실패 N`, 미보고, 통합 MR·옛 JSON이면 `-`
- `weeks`가 비면 그래프 대신 "데이터 없음"

제약과 보안

- 외부 스크립트·CDN·폰트·이미지 없음(`<script>` 없음, 인라인 `<style>`만). 사내망·오프라인에서 열린다
- XHTML 호환(모든 태그를 닫고 void 요소는 self-closing). 테스트가 `xml.etree`로 파싱해 구조를 검사한다
- 입력 문자열은 모두 `html.escape(quote=True)`. 링크는 URL 스킴이 `http`·`https`일 때만 `<a href>`, 아니면 텍스트만
- 숫자 필드가 없거나 숫자가 아니면 0, 문자열 필드가 아니면 빈 값. `mrs`·`defects`·`jobs`·`window`는 없어도 렌더한다

종료 코드: 성공 0, 오류 2(입력 파일 없음·JSON 아님·최상위가 객체가 아님·`version`이 1이 아님·`weeks`가 목록이 아님·출력 실패).
오류 메시지에는 파일 경로만 넣고 입력 내용은 옮기지 않는다.

CI 아티팩트 게시(`expire_in` 90일·`expose_as`)와 주간 요약 메시지의 리포트 링크(`$CI_JOB_URL/artifacts/file/report.html`)는
예약 파이프라인 조각 `core/ci/gitlab/metrics.yml`이 한다(`core/ci/README.md` "측정 예약 파이프라인").

테스트는 `tests/test_metrics_report.py`, 입력 fixture는 `tests/fixtures/metrics/report-sample.json`(합성 데이터, 실제 호스트 없음).
수집기 fixture로 `collect`를 돌린 결과를 그대로 렌더하는 계약 테스트가 두 스키마를 맞춘다.

## 발송 (`notify.py`, `harness metrics notify`)

`metrics.json`(`version` 1)의 마지막 주 5개 지표와 직전 주 대비 증감, 리포트 링크를 평문 Markdown 한 메시지로 만들어 보낸다.
요약 값은 같은 디렉터리의 `report.py`를 import해 리포트의 "이번 주 요약"과 같은 계산을 쓴다(둘이 다른 숫자를 보이지 않는다).
feelm `scripts/jira/notify_mattermost.py`의 발송부(`MattermostClient`)를 이관했고 Jira 조회는 가져오지 않았다(백로그).

```bash
python bin/harness.py metrics notify --in metrics.json --dry-run                     # 본문만 표준 출력
python core/metrics/notify.py --in metrics.json --report-url https://gitlab.example/group/demo/-/jobs/77/artifacts/file/report.html
```

| 옵션 | 기본 | 설명 |
| --- | --- | --- |
| `--in` | `metrics.json` | 수집기 출력(`version` 1). 리포트와 같은 입력 검사 |
| `--mode` | `auto` | `webhook`·`mattermost`·`auto`. auto는 `HARNESS_NOTIFY_WEBHOOK_URL`이 있으면 webhook, 없고 `HARNESS_MATTERMOST_URL`이 있으면 mattermost, 둘 다 없으면 종료 2에 변수 이름 안내 |
| `--report-url` | 없음 | 본문 마지막 줄 `리포트: <URL>`. http·https만 받는다. 조각은 `$CI_JOB_URL/artifacts/file/report.html`을 준다 |
| `--dry-run` | | 본문만 표준 출력에 쓰고 네트워크·설정 검사 없이 종료 0 |

### 전송 방식과 변수

설정은 환경 변수로만 받는다(CI 변수는 masked). 값은 로그·오류 메시지·출력에 쓰지 않는다.

| 모드 | 변수 | 동작 |
| --- | --- | --- |
| `webhook` | `HARNESS_NOTIFY_WEBHOOK_URL` | `POST {"text": 본문}`(`Content-Type: application/json`). Slack·Mattermost incoming webhook 공통 |
| `mattermost` | `HARNESS_MATTERMOST_URL`, `HARNESS_MATTERMOST_LOGIN_ID`, `HARNESS_MATTERMOST_PASSWORD`, `HARNESS_MATTERMOST_CHANNEL_ID`(또는 `HARNESS_MATTERMOST_TEAM` + `HARNESS_MATTERMOST_CHANNEL`) | `POST /api/v4/users/login` → (채널 ID가 없으면 `GET /api/v4/teams/name/<team>` → `GET /api/v4/teams/<id>/channels/name/<channel>`) → `POST /api/v4/posts` → `POST /api/v4/users/logout`(실패해도 경고만). incoming webhook·PAT이 꺼진 서버(SSAFY Mattermost)용. 비밀번호가 CI 변수에 들어가므로 봇 계정을 권장한다 |

### 본문

평문 Markdown(제목 줄, `- ` 목록, 맨 URL). Slack mrkdwn과 Mattermost Markdown이 다르게 그리는 강조·머리글 기호는 쓰지 않는다.
4,000자 상한(넘으면 본문을 자르고 리포트 줄은 남긴다).

```text
group/demo 주간 측정 요약 2026-W40 (진행 중)
- 1 병합 후 누락 수: 1 (지난주 1, ±0)
- 2 엄격 비율: 100% (1/1) (지난주 33% (1/3), +67%p)
- 3 CI 실패 수: 2 (지난주 4, -2)
- 4 판정 불일치 수: 0 (지난주 1, -1)
- 5 hooks 개입 수: 1 (지난주 4, -3), 보고 MR 1/1
리포트: https://gitlab.example/group/demo/-/jobs/77/artifacts/file/report.html
```

- 지표 줄은 "값 (지난주 값, 증감)". 증감은 리포트와 같이 `+1`·`-2`·`±0`, 비율은 `%p`, 주가 하나면 `-`. 엄격 비율은 `%`와
  `strict/merged`이고 병합 MR이 없는 주면 `-`. 지표 5 줄 끝에 이번 주 보고 MR(`보고/집계`)을 붙인다
- "(진행 중)"은 리포트와 같은 기준(마지막 주 시작일부터 7일이 `window.until`보다 뒤)
- `weeks`가 비면 제목과 "데이터 없음" 한 줄. `--report-url`이 없으면 리포트 줄을 생략한다

### 보안

- URL은 https만 받는다(webhook URL 경로의 토큰·비밀번호를 평문으로 보내지 않는다)
- 리다이렉트를 따르지 않는다. 3xx도 오류다(POST를 다른 주소로 다시 보내지 않는다)
- 오류 메시지는 HTTP 상태와 **호스트**만 남긴다(`HTTP 500 hooks.example`). webhook URL 경로·비밀번호·세션 토큰·응답 본문은
  어디에도 쓰지 않는다. 설정 오류는 변수 이름만 안내한다
- 종료 코드: 성공 0, 입력·설정·전송 오류 2. Mattermost 로그아웃 실패는 경고만 내고 0

테스트는 `tests/test_metrics_notify.py`(가짜 전송으로 webhook·Mattermost 호출 순서·헤더·본문, 조각 구조, 매니페스트 복사).

## CI 실패 원인 분류 (`classify_ci.py`)

이미 끝난 파이프라인의 job 메타데이터와 trace 파일을 읽어 실패 원인 범주를 정하는 순수 분류기다.
네트워크로 수집하지 않는다. 수집은 아래 어댑터 명령으로 파일을 만든 뒤 넘긴다.
M4-1 수집기(`collect.py`)도 이 모듈을 import해 API로 받은 job·trace를 넘긴다.

```bash
python bin/harness.py classify-ci --jobs jobs.json --trace-dir traces/ --out ci-failures.jsonl
```

입력

- `--jobs`: GitLab job API 형태의 job 객체 목록(`id`·`name`·`stage`·`status`·`failure_reason`). 단일 객체,
  한 줄에 객체 하나(JSONL), 페이지별 배열을 이어 붙인 출력도 읽는다. `id`(정수)와 `status`는 필수다.
- `--trace-dir`: `<job_id>.log` 파일이 있는 디렉터리. 파일이 없는 job은 메타데이터만으로 분류한다.
- `status`가 `failed`인 job만 분류한다. 성공·취소·건너뜀 등은 산출에서 빼고 개수만 stderr에 적는다.

산출 (`--out` 생략 시 표준 출력): job 하나당 JSON 한 줄. 집계는 하지 않는다(M4-1).

```json
{"job_id": 12, "name": "backend-test", "stage": "test", "failure_reason": "script_failure", "category": "format",
 "matches": [{"rule": "test.gradle", "category": "test", "line": 5, "count": 4},
             {"rule": "format.spotless", "category": "format", "line": 7, "count": 3}]}
```

`category`는 `test`·`format`·`infra`·`timeout`·`security`·`unclassified` 중 하나다. `matches`에는 판정에 쓰지
않은 규칙까지 일치한 규칙을 모두 남긴다(`line`은 첫 일치 줄 번호, 메타데이터 규칙이면 `null`, `count`는 일치한 줄 수).

판정 순서 — 앞 단계에서 정해지면 뒤 단계는 `matches`에만 남는다.

1. `failure_reason`: `job_execution_timeout`·`stuck_or_timeout_failure` → `timeout`, `runner_system_failure`·`api_failure` 등
   runner·플랫폼 오류 → `infra`. `script_failure`·`unknown_failure`는 원인을 정하지 않는다.
2. 구조화 표식: trace의 `<NAME>_RESULT={"category": "..."}` JSON 한 줄. 지금 키트 CI 조각은 이 표식을 출력하지 않는다.
3. job 이름: `harness-`로 시작하면(키트 보안 검사 job) `security`
4. 로그 정규식: GitLab 실행 시간 초과 문구 → 인프라(DNS·네트워크·이미지 pull·OOM·디스크·runner) → 포맷(Spotless·Prettier·
   ESLint·Black/Ruff·Checkstyle/ktlint) → 테스트(Gradle·Maven·Jest/Vitest·pytest·unittest·Go)

규칙은 `classify_ci.py`의 `REASON_CATEGORIES`·`LOG_RULES`에 코드로 둔다. 규칙을 바꾸면
`tests/test_classify_ci.py`에 합성 로그 fixture(`tests/fixtures/classify_ci/`)를 함께 추가한다.
fixture에는 Secret·실제 호스트명·IP·프로젝트명을 넣지 않는다.

### 수집 어댑터 (참고 명령)

분류기는 아래 명령의 출력 파일만 읽는다. 명령은 저장소 디렉터리에서 실행하며, 토큰은 각 CLI의 로그인 상태를 쓴다.

GitLab (`glab`, `:id`는 현재 저장소로 바뀐다)

```bash
glab api --paginate "projects/:id/pipelines/<pipeline_id>/jobs?per_page=100" > jobs.json
mkdir -p traces
for id in <실패한 job id ...>; do
  glab api "projects/:id/jobs/$id/trace" > "traces/$id.log"
done
```

GitHub Actions (`gh`, `failure_reason`·`stage`가 없어 로그와 job 이름으로만 분류된다).
시간 초과(`timed_out`)는 실패로 넣고 GitLab의 `job_execution_timeout` 사유로 바꾼다. 빼면 시간 초과 job이 제외 수로만 잡힌다.
보안 job은 `harness-*` 이름으로 알아보므로, 키트 저장소처럼 보안 검사 job 이름이 다르면(`security.yml`의
`secret-detection`·`sast`) 아래처럼 `harness-` 접두를 붙인다. 다른 저장소는 `$security`에 그 저장소의 보안 job 이름을 적는다.

```bash
gh api --paginate "repos/{owner}/{repo}/actions/runs/<run_id>/jobs" \
  --jq '["secret-detection", "sast"] as $security | .jobs[] | {id, stage: null,
          name: (if (.name | IN($security[])) then "harness-" + .name else .name end),
          failure_reason: (if .conclusion == "timed_out" then "job_execution_timeout" else null end),
          status: (if .conclusion == "failure" or .conclusion == "timed_out" then "failed"
                   else (.conclusion // .status) end)}' > jobs.json
mkdir -p traces
for id in <실패한 job id ...>; do
  gh api "repos/{owner}/{repo}/actions/jobs/$id/logs" > "traces/$id.log"
done
```
