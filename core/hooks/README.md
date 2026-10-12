# core/hooks

AI 코딩 세션(Claude Code)에 거는 하드 가드레일. `init`이 대상 저장소의 `.claude/`에 설치하고
`check`가 드리프트를 검사한다. 스크립트는 표준 라이브러리만 쓴다.

| 원본 | 대상 | 역할 |
| --- | --- | --- |
| `settings.template.json` | `.claude/settings.json` | hook 등록. `{{hook_python}}`만 치환한다 |
| `protect-paths.py` | `.claude/hooks/` | PreToolUse. 보호 경로를 고치려는 `Edit`·`Write`·`MultiEdit`·`NotebookEdit`를 거부하거나 사용자 확인으로 돌린다 |
| `stop-verify.py` | `.claude/hooks/` | Stop. 응답을 끝내기 전에 바뀐 영역의 검증 명령을 실행한다 |
| `harness_common.py` | `.claude/hooks/` | 공통 코드. 키트의 `bin/harness.py`도 이 파일로 `hooks` 설정을 검사한다 |

`.claude/settings.json`은 키트가 생성하는 파일이다. 손으로 고치면 `check`가 불일치로 보고한다.
팀이나 개인 설정은 `.claude/settings.local.json` 또는 사용자 설정에 둔다.

## 설정 (`harness.json`)

```json
{
  "hooks": {
    "protected_paths": [{"pattern": "docs/api/**", "mode": "block", "reason": "API 계약"}],
    "stop_verify": ["npm test"],
    "stop_timeout_sec": 300,
    "python": "python3"
  }
}
```

모든 키는 생략할 수 있다. hook은 실행할 때마다 이 파일을 읽으므로 규칙을 바꿔도 `init`을 다시 할 필요가 없다.
`python`을 바꿀 때만 `init --force`로 `settings.json`을 다시 만든다.

- `protected_paths`: 지정하면 기본 목록을 대체한다. `mode`는 `block`(거부) 또는 `ask`(사용자 확인).
  `pattern`은 gitignore 방식이다. `/`로 시작하거나 중간에 있으면 루트 기준, 아니면 모든 깊이에서 맞춘다.
  앞의 `./`는 무시하고, 끝의 `/`는 그 아래 전체다. 디렉터리에 맞는 패턴은 그 아래 파일에도 맞는다.
  `**`는 0개 이상의 디렉터리다. 대소문자는 구분하지 않는다. 여러 규칙이 맞으면 `block`이 이긴다.
- 기본 목록: `block` lock 파일(`package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `gradle.lockfile`,
  `poetry.lock`, `uv.lock`, `Cargo.lock`, `go.sum`). `ask` CI 정의(`.gitlab-ci.yml`, `/.github/workflows/`),
  마이그레이션(`**/db/migration/`, `migrations/`), `.env.example`.
- 항상 추가(설정으로 끌 수 없다): 자기 보호 `/.claude/settings.json`, `/.claude/settings.local.json`,
  `/.claude/hooks/`는 `block`. `/harness.json`과 `areas[].docs`에 적은 계약 문서는 `ask`.
  프로젝트 밖의 사용자 설정 `~/.claude/settings.json`·`settings.local.json`도 `ask`.
- `stop_verify`: 영역(`areas[].dir`) 밖의 파일이 바뀌었을 때 실행할 명령. 영역 안의 변경은 그 영역의
  `areas[].verify`를 실행한다. 파일을 수정하지 않는 명령만 적는다.
- `stop_timeout_sec`: 명령별 시간 제한(1~840초, 기본 300). 모든 명령을 합친 예산도 840초다.
- `python`: hook을 실행할 명령 이름(기본 `python3`, 기존 Windows launcher `py -3`도 허용). 셸 메타문자·경로·임의 인자는 거절한다. Windows에서는 `python3`이 Microsoft Store 별칭인 경우가
  많아 hooks가 조용히 실패한다(종료 코드 49). `python`이나 `py`로 바꾼다.

## 동작

- 보호 경로 검사는 프로젝트 루트(`$CLAUDE_PROJECT_DIR`) 밖의 파일은 사용자 설정 외에는 보지 않는다.
  `..`, 역슬래시, Windows 확장 경로(`\\?\`)는 해석한 뒤 맞춘다. 그 밖의 UNC 경로(`\\host\share`)는
  프로젝트 소속을 확인할 수 없어 `ask`로 돌린다. 심볼릭 링크는 링크를 따라가기 전의 경로와 실제 경로를 둘 다 맞추고
  더 강한 결정을 따른다. 보호 대상 이름이 링크여도, 링크된 디렉터리를 거쳐 보호 파일에 닿아도 걸린다.
  자기 보호 경로는 실제 위치와도 비교한다. `.claude`나 `.claude/hooks`가 링크이면 그 대상 경로로 직접 써도 걸리고,
  보호 파일의 하드링크를 다른 이름으로 써도 걸린다. 그 밖의 규칙에는 하드링크 판정을 하지 않는다.
- 종료 검증은 작업 트리에 변경이 없으면 아무것도 하지 않는다. 프로젝트가 git 최상위의 하위 디렉터리면 프로젝트 안의
  변경만 본다. 직전 확인과 변경 상태가 같으면 다시 실행하지 않는다. 중첩 저장소·하위 모듈은 그 저장소의 `HEAD`와
  변경 파일만 해시하고(ignore된 파일은 읽지 않는다), 해시 계산도 840초 예산 안에 넣는다.
- 종료 검증이 실패하면 한 번 종료를 보류하고 실패한 명령과 출력 끝 40줄을 모델에 넘긴다. 모델이 고친 뒤에도
  실패하거나 아무것도 바꾸지 않았으면 다시 보류하지 않고 사용자에게 한 번 알린다. 세션 시작 전부터 있던 변경이
  실패하면 첫 응답에서 한 번 보류될 수 있다.
- 설정 파일을 읽지 못하면 막지 않고(fail-open) "꺼진 상태"라고 알린다. 끌 수 없는 규칙은 이때도 유지한다.
- 판정(`block`·`ask`)과 종료 검증 실패는 `.git/harness/events.jsonl`(worktree마다 따로)에 한 줄씩 남는다.
  기록에는 시각(`ts`)과 그때의 브랜치(`branch`, detached HEAD면 `null`)가 붙는다. 외부로 보내지 않는다.
  MR을 만들기 전에 `harness events summary`가 현재 브랜치의 기록을 MR 본문 `## hooks 기록` 절로 요약하고, 측정 수집기가
  병합 MR 본문에서 그 수를 읽는다(지표 5, [`core/metrics/README.md`](../metrics/README.md) "hooks 기록").

## 알려진 우회

hooks는 실수를 막는 장치이지 보안 경계가 아니다. 최종 방어선은 CI다.

- **Bash로 쓰는 경로는 보지 않는다.** `sed -i`, 리다이렉션(`>`), `git checkout -- <파일>`, 패키지 관리자 명령은
  보호 경로를 바꿀 수 있다. 명령 문자열을 추측해 막으면 오탐과 미탐이 모두 커서 1차에서는 다루지 않는다.
- `check`는 `.claude/hooks/`에 추가된 파일이나 디렉터리를 드리프트로 보고한다. 다만 검사 실행 전에
  임의 코드를 실행한 경우까지 막는 보안 경계는 아니다.
- 사용자 설정(`~/.claude/settings.json`)의 `disableAllHooks`는 확인 창을 거치면 바뀐다.
- hook 스크립트 파일이 없으면 Python이 종료 코드 2를 내서 모든 편집이 거부된다. `init --force`로 다시 설치한다.
- Claude Code 외의 도구(Codex, IDE 확장 등)에는 적용되지 않는다.
- 사람이 편집기로 고치는 파일은 막지 않는다.

## 임시로 끄는 법

사람이 판단해 끈다. 끈 사실과 이유는 `friction` 이슈로 남긴다(로드맵 1장 "팀 수용").

- 전체: `.claude/settings.local.json`에 `{"disableAllHooks": true}`. 이 파일은 개인 설정이라 커밋하지 않는다.
- 보호 경로 하나: `harness.json`의 `hooks.protected_paths`를 조정한다(`ask` 확인 후 반영).
- 종료 검증만: 느린 명령을 빠른 명령(예: 단위 테스트만)으로 바꾸거나 `hooks.stop_verify`를 지운다.
  영역의 `verify`는 필수이므로 영역 단위로는 끌 수 없다.

## 키트 자기 적용

키트 저장소에도 같은 hooks가 설치돼 있다(M1-7). `harness.json`의 `hooks`에서 `python`은 `python`(Windows),
`stop_verify`는 단위 테스트와 `check --self`다. 이 디렉터리를 고치면 `python bin/harness.py init --self`로
`.claude/`를 다시 생성한다. 생성하지 않으면 `check --self`가 불일치로 보고한다.
그래서 키트에서는 원본(`core/hooks/`, 매니페스트, `bin/harness.py`)을 고친 뒤 `init --self`를 돌리면 `.claude/hooks/`
자기 보호를 거치지 않고 hook을 바꿀 수 있다. 원본 변경은 엄격 판정이라 리뷰로 통제한다.

## 다음 단계

- 판정을 hook에 연결(#22). 판정 함수 `judge`·`changed_files`는 이미 `harness_common.py`에 있고 CLI
  `harness judge`가 같은 코드를 쓴다(M2-1). 지금은 어떤 hook도 판정 결과로 막거나 묻지 않는다
- 플랜·리뷰 파일 lint, 엄격 판정 플랜 승인에 따른 `block` 해제(M2-3)
- 세션 시작 시점 기준선(SessionStart)으로 기존 변경을 종료 검증에서 빼기
