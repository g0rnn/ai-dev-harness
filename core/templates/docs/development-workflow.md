# {{tracker_name}}-{{platform}} 개발 흐름

## 1. 전체 흐름

```text
{{issue_noun}} 확인
  → {{integration_branch}} 최신화
  → {{issue_key}}가 포함된 작업 브랜치 생성
  → 구현 및 커밋
  → 테스트와 자체 검토
  → 최신 {{integration_branch}} 반영 및 충돌 확인
  → {{integration_branch}} 대상 {{pr_noun}} 생성
  → 코드 리뷰 및 Approve
  → Squash Merge 및 Source Branch 삭제
  → {{issue_noun}} 완료
```

브랜치, 커밋, {{pr_noun}} 이름의 상세 형식은 [Git 컨벤션](./git-convention.md)을 따른다.

## 2. 작업 시작

1. {{issue_noun}}의 목적과 범위를 확인한다.
2. 담당자와 현재 상태를 확인하고 작업을 시작할 때 `진행 중`으로 변경한다.
3. 로컬 저장소에 커밋되지 않은 다른 작업이 있는지 확인한다.
4. `{{integration_branch}}`으로 이동해 원격 변경을 Fast-forward로 반영한다.
5. {{issue_key}}가 포함된 작업 브랜치를 생성한다.

```bash
git switch {{integration_branch}}
git pull --ff-only origin {{integration_branch}}
git switch -c <type>/<KEY>-<description>
```

- 다른 사람이나 AI가 만든 로컬 변경을 임의로 삭제하거나 덮어쓰지 않는다.
- 진행 중인 변경이 있으면 먼저 해당 작업을 정리하거나 별도 worktree를 사용한다.
- API 계약, DB Schema, 공통 설정처럼 영향 범위가 큰 사항은 구현 전에 팀과 합의한다.

## 3. 구현과 커밋

- {{issue_noun}}의 목적을 벗어나는 변경을 같은 브랜치에 섞지 않는다.
- 작업 중 별도 목적의 변경이 필요해지면 새 {{issue_noun}}과 브랜치로 분리한다.
- 커밋은 리뷰 가능한 논리 단위로 작성한다.
- 비밀키, 토큰, 실제 개인정보 및 로컬 전용 설정은 커밋하지 않는다.
- 공통 파일을 변경하면 {{pr_noun}} 본문에 영향 범위를 명시한다.

## 4. {{pr_noun}} 전 확인

1. 변경 파일과 diff를 직접 확인한다.
2. 영역별 테스트, 정적 검사 또는 수동 검증을 수행한다.
3. 최신 `origin/{{integration_branch}}`을 rebase로 반영하고 충돌 여부를 확인한다.
4. 불필요한 디버깅 코드와 생성 파일이 포함되지 않았는지 확인한다.
5. {{pr_noun}}이 하나의 목적을 가지며 [Git 컨벤션의 크기 기준](./git-convention.md)을 충족하는지 확인한다.
6. 작업한 worktree에서 `python .harness/summary.py events`를 실행해 출력을 {{pr_noun}} 본문 `## hooks 기록` 절에 붙인다.
   hooks가 이 브랜치에서 막은 편집과 종료 검증 실패 수이며, 측정 수집기가 병합된 본문에서 읽는다.
7. 같은 worktree에서 `python .harness/summary.py agent`를 실행해 출력을 {{pr_noun}} 본문 `## 에이전트 기록` 절에 붙인다.
   이 브랜치를 작업한 세션 수·응답 수·작업 시간·토큰·추정 비용·기록 없는 커밋이며, 측정 수집기가 도구별 결과를 비교한다.
   Codex 세션은 Codex 로그(`CODEX_HOME`)에서 자동으로 센다. 기록을 남기지 않는 도구로 작업한 몫만 도구 줄에 손으로 더한다.
   리뷰 파일 7장(처리)을 채웠으면 `python .harness/summary.py review <리뷰 파일>` 출력 두 줄을 `## 리뷰 결과`에 붙인다.
   6·7은 {{pr_noun}}을 만드는 쪽(AI 도구 포함)이 만들 때 한다. 키트 없이 저장소 안에서 실행하고, 기록은 작업한 PC에만 있으므로
   다른 PC에서 만들지 않는다. 진행 중인 응답은 출력에 빠지므로 커밋을 더 push할 때마다 다시 실행해 본문을 고친다
   (`gh pr edit --body-file`, `glab mr update` 등). 병합하는 사람은 절이 오래됐으면 작성자에게 다시 만들어 달라고 요청한다.

```bash
git fetch origin
git rebase origin/{{integration_branch}}
```

이미 원격에 Push한 브랜치를 rebase했다면 팀원과 공유 상태를 확인한 뒤
`git push --force-with-lease`를 사용한다. 단순 `--force`는 사용하지 않는다.

테스트를 수행하지 못한 경우 숨기지 않고 {{pr_noun}} 본문에 사유와 미검증 범위를 작성한다.

## 5. 작업 {{pr_noun}} 생성

- Source: 작업 브랜치
- Target: `{{integration_branch}}`
- Template: `Default.md`
- Squash commits: 활성화
- Delete source branch: 활성화

{{pr_noun}} 제목과 본문에는 {{issue_key}} 및 `Closes <KEY>`를 포함한다.

화면 변경이 있으면 스크린샷을 첨부하고, 큰 {{pr_noun}}은 리뷰 순서와 분리하지 못한 이유를 설명한다.

## 6. 리뷰와 병합

- 작성자는 주요 설계 의도와 검증 결과를 {{pr_noun}}에 설명한다.
- 리뷰어는 요구사항 충족, 회귀 위험, 테스트, 보안 및 유지보수성을 확인한다.
- 작성자는 본인의 {{pr_noun}}을 직접 승인하지 않는다.
- 최소 한 명의 Approve를 받은 후 병합한다.
- 리뷰 반영 중 범위가 크게 늘어나면 후속 {{issue_noun}}으로 분리한다.

## 7. {{workflow_release_title}}

{{workflow_release_body}}

## 8. 병합 후 로컬 정리

```bash
git switch {{integration_branch}}
git pull --ff-only origin {{integration_branch}}
git branch -d <merged-branch>
git fetch --prune
```

- 병합된 작업 브랜치에 후속 변경을 계속 추가하지 않는다.
- 추가 작업은 새 {{issue_noun}}과 새 브랜치에서 시작한다.
- 강제 삭제가 필요한 브랜치는 병합 여부와 보존할 변경을 먼저 확인한다.
