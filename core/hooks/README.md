# core/hooks

AI 코딩 세션(Claude Code)에 거는 하드 가드레일. `init`이 대상 저장소의 `.claude/`에 설치하고
`check`가 드리프트를 검사한다. 스크립트는 표준 라이브러리만 쓴다.

| 원본 | 대상 | 역할 |
| --- | --- | --- |
| `settings.template.json` | `.claude/settings.json` | hook 등록. `{{hook_python}}`만 치환한다 |
| `protect-paths.py` | `.claude/hooks/` | PreToolUse. 보호 경로를 고치려는 `Edit`·`Write`·`MultiEdit`·`NotebookEdit`를 거부하거나 사용자 확인으로 돌린다 |
| `stop-verify.py` | `.claude/hooks/` | Stop. 응답을 끝내기 전에 바뀐 영역의 검증 명령을 실행한다 |
| `session-start.py` | `.claude/hooks/` | SessionStart. 세션 시작 `HEAD`를 종료 검증 기준선으로 남기고 세션 시작을 기록한다. 아무것도 출력하지 않는다 |
| `session-end.py` | `.claude/hooks/` | SessionEnd. 세션 종료를 기록하고, OTLP 엔드포인트가 있으면 키트 기록을 백그라운드 전송한다. 아무것도 출력하지 않고 항상 0으로 끝난다 |
| `harness_common.py` | `.claude/hooks/` | 공통 코드. 키트의 `bin/harness.py`도 이 파일로 `hooks` 설정을 검사한다 |

`.claude/settings.json`은 키트가 생성하는 파일이다. 손으로 고치면 `check`가 불일치로 보고한다.
팀이나 개인 설정은 `.claude/settings.local.json` 또는 사용자 설정에 둔다.

SessionEnd 자동 전송은 `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` 또는 `OTEL_EXPORTER_OTLP_ENDPOINT`가 설정된 PC에서만
설치된 `.harness/telemetry.py push`를 분리 실행한다. 세션 종료 기록을 먼저 남기고, 전송 결과를 기다리지 않는다.
인증 헤더는 같은 환경변수로 상속하며 출력하지 않는다. HTTP 전송 실패·다른 push와의 경합 시 남은 기록은 다음 SessionEnd에
다시 시도한다. 수집기가 일부 거절한 기록은 기존 push 정책대로 재전송하지 않는다. 설정·끄는 법과 Codex 수집 범위는
[`core/observability/README.md`](../observability/README.md#키트-기록과-codex-세션-telemetry-push)를 따른다.

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
  `/.claude/hooks/`는 `block`. `/harness.json`, `/.harness/`(CI 검사 코드)와 `areas[].docs`에 적은 계약 문서는 `ask`.
  프로젝트 밖의 사용자 설정 `~/.claude/settings.json`·`settings.local.json`도 `ask`.
- `stop_verify`: 영역(`areas[].dir`) 밖의 파일이 바뀌었을 때 실행할 명령. 영역 안의 변경은 그 영역의
  `areas[].verify`를 실행한다. 파일을 수정하지 않는 명령만 적는다. 명령은 저장소 루트에서 bash 문법으로 쓴다
  (`cd backend && ./gradlew build`). POSIX에서는 `/bin/sh`, Windows에서는 Git Bash로 실행한다(Claude Code가 hook을 실행하는 셸과
  같다). bash는 `CLAUDE_CODE_GIT_BASH_PATH` → git 설치 위치의 `bin/bash.exe` → PATH의 bash 순으로 찾고, 이름이 `bash.exe`인 파일만
  쓴다(WSL 실행기 `System32`·`WindowsApps`는 제외). 찾지 못하면 cmd로 실행하며 실패 출력에 설정 방법을 붙인다. 명령은 환경 변수로
  넘겨 `\` 같은 문자가 그대로 간다. 시간 초과에는 Windows 작업 개체로 셸과 후손(npm·`exec` 체인 포함)을 함께 끝낸다. cmd 문법
  (`%VAR%`, `gradlew.bat`)으로 쓴 명령은 bash 문법으로 바꾼다(#115).
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
- 종료 검증 대상은 작업 트리의 미커밋 변경과, 세션 시작 기준선 이후 커밋된 변경(이름 변경은 옛·새 경로 둘 다)이다.
  세션이 커밋하고 끝나도 검증이 돈다(#82). 기준선은 SessionStart가 `session_id`별 시작 `HEAD`를 `.git/harness/sessions.json`
  (worktree마다 따로)에 남긴 것이고, resume·compact는 기준선을 덮지 않는다. 30일 지나거나 200개를 넘는 항목은 지운다.
  기준선이 없으면(이 기능 전 설치, `session_id` 없음, git이 모르는 커밋) 미커밋 변경만 본다. 동시에 시작한 세션의 기록이
  경합으로 빠지면 그 세션도 같다. 비교는 종료 시점의 `HEAD`와 기준선 사이라서, 세션 중 다른 브랜치로 옮겨 가면 두 커밋의
  차이 전체가 대상이 된다. 한 번 커밋한 영역은 세션이 끝날 때까지 대상에 남아 상태가 바뀔 때마다 다시 검증된다(같은 상태면 캐시).
  둘 다 없으면 아무것도 하지 않는다. 프로젝트가 git 최상위의 하위 디렉터리면 프로젝트 안의
  변경만 본다. 직전 확인과 변경 상태가 같으면 다시 실행하지 않는다. 중첩 저장소·하위 모듈은 그 저장소의 `HEAD`와
  변경 파일만 해시하고(ignore된 파일은 읽지 않는다), 해시 계산도 840초 예산 안에 넣는다.
- 종료 검증이 실패하면 한 번 종료를 보류하고 실패한 명령과 출력 끝 40줄을 모델에 넘긴다. 모델이 고친 뒤에도
  실패하거나 아무것도 바꾸지 않았으면 다시 보류하지 않고 사용자에게 한 번 알린다. 세션 시작 전부터 있던 변경이
  실패하면 첫 응답에서 한 번 보류될 수 있다.
- 설정 파일을 읽지 못하면 막지 않고(fail-open) "꺼진 상태"라고 알린다. 끌 수 없는 규칙은 이때도 유지한다.
- 판정(`block`·`ask`)과 종료 검증 실패는 `.git/harness/events.jsonl`(worktree마다 따로)에 한 줄씩 남는다.
  기록에는 시각(`ts`)과 그때의 브랜치(`branch`, detached HEAD면 `null`)가 붙는다. 외부로 보내지 않는다.
  MR을 만들기 전에 `harness events summary`(대상 저장소에서는 `python .harness/summary.py events`)가 현재 브랜치의 기록을 MR 본문 `## hooks 기록` 절로 요약하고, 측정 수집기가
  병합 MR 본문에서 그 수를 읽는다(지표 5, [`core/metrics/README.md`](../metrics/README.md) "hooks 기록").

## 세션 기록 (`sessions.jsonl`, M5-1)

개입이 없어도 세션마다 무엇이 돌았는지 남긴다. `.git/harness/sessions.jsonl`(worktree마다 따로)에 사건마다 한 줄이다.
기준선 파일 `sessions.json`과 이름이 비슷하지만 다른 파일이다. 외부로 보내지 않고, 프롬프트·코드 내용은 쓰지 않는다.

| `event` | 남기는 hook | 필드(공통 `ts`·`branch`·`session`·`tool`·`head` 외) |
| --- | --- | --- |
| `start` | SessionStart(시작·resume·clear·compact마다) | `source`, 입력에 있으면 `model`·`transcript`(로컬 transcript 경로) |
| `stop` | Stop(응답이 끝날 때마다) | `verify`(`none` 검증 대상 없음·`pass`·`fail`·`cached-pass`·`cached-fail` 같은 상태 재사용·`error`), `files`(검증 대상 파일 수), `retry` |
| `end` | SessionEnd | `reason` |

- `stop`·`end`에는 transcript의 세션 누적 토큰 `usage`(`{모델: {input, output, cache_write_5m, cache_write_1h, cache_read}}`,
  M5-2)가 붙는다. 서브에이전트 transcript(`<transcript>/subagents/*.jsonl`)를 포함하고, 스트리밍으로 반복된 같은 메시지는
  한 번만 센다. 메인 transcript를 읽지 못하면 `usage` 대신 `usage_error`(`no_transcript`·`not_found`·`too_large`(200MB 초과)·
  `unreadable`·`error`(계산 중 예외))를 남긴다. 서브에이전트 파일만 크거나 읽지 못하면 그 파일을 빼고 센 `usage`와 함께
  `usage_error`(`too_large_subagent`·`unreadable_subagent`)를 남긴다. 매 응답마다 transcript 전체를 다시 읽는다(80MB에 약 0.2초).
  비용은 기록하지 않고 요약할 때 단가로 계산한다.
- `tool`은 지금 `claude-code`뿐이다. 다른 도구의 세션은 기록되지 않는다(MR 본문에 수동으로 적는다, #98).
- 기록은 최선 노력이다. 파일을 쓰지 못해도 hook의 판정·종료 코드·출력은 같다. 여러 hook과 세션이 동시에 써도
  줄이 섞이거나 사라지지 않도록 `<파일>.lock`으로 잠그고 쓴다(Windows의 append는 원자적이지 않다). `events.jsonl`도 같다.
- 파일은 추가만 한다(응답 1만 번에 약 2MB). 지우려면 `.git/harness/sessions.jsonl`을 삭제한다.
- `harness sessions summary [<target>|--self] [--branch <name>] [--base <ref>]`: 현재 브랜치의 세션 수(도구별), 응답 수,
  작업 시간, 종료 검증 결과, **기록 없는 커밋**을 출력한다. 같은 저장소의 모든 worktree 기록을 함께 읽는다(세션은 띄운
  checkout에 기록되므로, 기본 checkout에서 띄운 세션이 다른 worktree에서 커밋해도 찾는다).
  - 작업 구간: 한 세션의 사건을 시각순으로 잇되 `start`(resume 포함)와 2시간 넘는 공백에서 끊는다. 응답 하나가 2시간을
    넘으면 그 중간의 커밋은 기록 없는 커밋으로 보일 수 있다.
  - 작업 시간: 같은 구간 안의 이웃한 두 사건 사이 시간을 뒤 사건의 브랜치(응답이 끝난 브랜치)에 준다.
  - 기록 없는 커밋: 기준 ref와의 merge-base 이후 커밋 중 committer 시각이 저장소의 어느 세션 작업 구간(+ 끝에서 10분)에도
    들지 않는 것. hooks가 꺼졌거나 다른 도구·사람이 만든 커밋의 대리 신호이고 단정하지 않는다.
    기준은 기본 `origin/<integration_branch>`.
  - JSON으로 읽히지 않거나 값이 틀린 줄(시각 형식 등)은 "읽을 수 없는 줄"로 세고 건너뛴다.
  - 토큰과 **추정 비용**: 세션 사건마다 기록된 누적 토큰의 차를 그 사건의 브랜치에 준다(누적이 줄면 새 transcript로 본다).
    비용 = 토큰 × 단가. 단가는 키트 기본값(Claude API 1차 단가, 기준일 표시)에 `harness.json` 최상위 `pricing`을 모델 단위로
    덮어쓴 것이다. 날짜가 붙은 모델 ID(`claude-haiku-4-5-20251001`)는 날짜를 뗀 ID의 단가를 쓴다. `currency`를 키트(USD)와
    다르게 주면 키트 단가를 섞지 않고 설정한 모델만 쓴다. `currency`·`as_of`는 `models`와 함께 준다. 단가가 없는 모델은 토큰만
    세고 이름을 알린다. transcript를 다 읽지 못한 세션이 있으면 그 수와 사유를 알리고 합이 일부라고 적는다.
  - 청구액이 아니라 추정이다. 배치·우선 처리·플랫폼별 가격, fast mode(표준의 2배), 웹 검색 같은 서버 도구 요금,
    Haiku 5.5의 100K 초과 프롬프트 단가(5배)는 반영하지 않아 그런 세션은 적게 추정된다.

    ```json
    {
      "pricing": {
        "currency": "USD",
        "as_of": "2026-11-01",
        "models": {
          "claude-opus-5-5": {"input": 4, "output": 20, "cache_write_5m": 5, "cache_write_1h": 8, "cache_read": 0.2}
        }
      }
    }
    ```

## 알려진 우회

hooks는 실수를 막는 장치이지 보안 경계가 아니다. 최종 방어선은 CI다.

- **Bash로 쓰는 경로는 보지 않는다.** `sed -i`, 리다이렉션(`>`), `git checkout -- <파일>`, 패키지 관리자 명령은
  보호 경로를 바꿀 수 있다. 명령 문자열을 추측해 막으면 오탐과 미탐이 모두 커서 1차에서는 다루지 않는다.
- `check`는 `.claude/hooks/`에 추가된 파일이나 디렉터리를 드리프트로 보고한다. 다만 검사 실행 전에
  임의 코드를 실행한 경우까지 막는 보안 경계는 아니다.
- 사용자 설정(`~/.claude/settings.json`)의 `disableAllHooks`는 확인 창을 거치면 바뀐다.
- hook 스크립트 파일이 없으면 Python이 종료 코드 2를 내서 모든 편집이 거부된다. `init --force`로 다시 설치한다.
- Claude Code 외의 도구(Codex, IDE 확장 등)에는 적용되지 않는다. MR 단계에서 같은 규칙을 MR lint의 보호 경로 확인과
  변경 영역 검증 조각(`core/ci/gitlab/verify.yml`)이 다시 건다(M5-5, [`core/ci/README.md`](../ci/README.md)).
  Codex는 `AGENTS.md`를 읽으므로 규칙 문서는 같이 적용되지만, Codex 자체의 hook 기능으로 같은 보호를 걸 수 있는지는
  확인하지 않았다(2026-10-20, 이 PC의 Codex 설정에 hook 항목 없음).
- 종료 검증은 종료 시점의 체크아웃에서 돈다. 세션이 다른 브랜치에 깨진 커밋을 만들고 원래 브랜치로 돌아와 끝나면
  그 커밋은 작업 트리에 없어 검증되지 않는다. 그 브랜치의 MR 파이프라인이 잡는다.
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
- 세션 시작 기준선(SessionStart, #82)을 확장해 세션 전부터 있던 미커밋 변경을 종료 검증에서 빼기(#8)
