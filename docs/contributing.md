# 기여 규칙

키트 저장소는 GitHub에서 `main` 브랜치 하나로 운영한다. 루트 `AGENTS.md`의 공통 절차를 전제로,
이 저장소에만 해당하는 규칙을 정한다.

## 브랜치와 PR

- `main`에는 직접 Push하지 않고 PR로만 병합한다. 예외는 저장소 초기화 커밋뿐이다.
- 작업 브랜치 이름은 `<type>/<이슈번호>-<description>`이다. 예: `feat/12-judge-script`.
- 한 브랜치에는 GitHub 이슈 하나만 담는다. 이슈 제목에는 로드맵 번호(M1-5 등)를 붙인다.
- PR 본문은 `.github/PULL_REQUEST_TEMPLATE.md`를 채운다. 검증 절을 비워 두지 않는다.
- Squash merge를 사용하고 병합 후 브랜치를 삭제한다.
- 혼자 작업하는 동안에도 PR을 만든다. 리뷰어가 없으면 분리 세션의 AI 리뷰 결과를 본문에 붙인다.

## AI 병합 예외

이 저장소에서는 루트 `AGENTS.md`의 "병합은 사람이 한다"에 다음 예외를 둔다.
AI는 사용자가 해당 PR의 병합을 명시적으로 요청한 경우에만 아래 절차로 병합할 수 있다.

1. 리뷰 확인: PR의 리뷰와 인라인 코멘트(Codex 봇 포함)를 모두 읽는다.
   Codex 리뷰 신호(리뷰 본문의 `Reviewed commit` 또는 👍 반응)가 현재 head를 덮어야 한다.
   신호 이후의 커밋이 2단계의 반영 커밋뿐이면 덮는 것으로 본다. 신호가 없거나 그 밖의 커밋이 있으면
   `@codex review`로 리뷰를 요청하고, 병합하지 않고 기다린다고 보고한다.
2. 코멘트 판정: 각 코멘트를 반영 또는 미반영으로 판정한다.
   - 반영: 같은 브랜치에서 수정하고, 검증 명령을 다시 실행한 뒤 커밋·Push하고, 코멘트에 반영 커밋을 답글로 남긴다.
   - 미반영: 이유를 답글로 남긴다.
   - 수정이 이슈 범위를 벗어나거나, 엄격 조건에 해당하거나, 판단이 갈리면 병합을 멈추고 사용자에게 확인한다.
3. 병합 조건: 아래를 모두 만족할 때만 Squash merge하고 브랜치를 삭제한다.
   - 판정이 경량 또는 표준이다. 엄격 단계 PR은 사람이 병합한다.
   - Codex 리뷰 신호가 현재 head를 덮는다(1단계 기준).
   - CI가 성공했다(반영 커밋 기준).
   - 모든 리뷰 코멘트에 답글이 달려 있다.
   - 최신 `main`과 충돌이 없다.
4. 보고: 반영·미반영한 코멘트와 이유, 검증 결과, 병합 커밋을 보고한다.
   조건을 하나라도 만족하지 못하면 병합하지 않고 이유를 보고한다.

반영 커밋에는 Codex 재리뷰를 요청하지 않는다. 반영과 재리뷰가 반복되지 않도록 반영 커밋은 검증 명령과 CI로 확인하고,
재리뷰가 없다는 점을 보고에 남긴다.

## 커밋

Conventional Commits를 쓴다. Scope는 변경 영역이다.

| Scope | 변경 영역 |
| --- | --- |
| `templates` | `core/templates/` |
| `hooks` | `core/hooks/` |
| `ci` | `core/ci/`, `.github/workflows/` |
| `metrics` | `core/metrics/` |
| `profiles` | `profiles/` |
| `cli` | `bin/`, `tests/` |
| `docs` | `docs/`, `README.md` |

## 판정과 플랜·리뷰

키트는 백엔드 도메인이 아니라 스크립트와 문서이고, 주 안전망은 단위 테스트와 CI 리허설이다.
엄격 단계는 틀렸을 때 팀이 키트를 끄거나 거짓 안심을 주는 변경에만 둔다.

| 단계 | 조건 |
| --- | --- |
| 경량 | `docs/` 하위의 손으로 관리하는 문서, 주석, 테스트만 변경. 플랜 없이 판정과 근거를 시작 보고와 PR 본문에 남긴다 |
| 표준 | 그 밖의 변경(`bin/harness.py`, `core/templates/`, 보안 검사가 아닌 CI 조각·워크플로 단계 포함). 플랜·구현·리뷰 산출물을 한 세션에서 만든다 |
| 엄격 | `core/hooks/`의 차단·수정 로직 변경(오탐이 나면 팀이 hooks를 끈다), 보안 검사 변경 — `core/ci/`의 보안 검사 조각, `.github/workflows/`의 보안 검사 단계와 이를 실행하는 `.github/scripts/run_fragment.py`(틀려도 조용히 통과해 거짓 안심을 준다) |

첫 소비자 저장소에 적용한 뒤에는 `harness.json` 스키마, CLI 인자, 자리표시자 이름, 판정 식별자
(`lite`·`standard`·`strict`)를 깨는 변경도 엄격이다. 그 전에는 로드맵 M1-10의 적용 전 계약 리뷰에서
한 번에 본다.

이 표를 경로 규칙으로 옮긴 것이 `harness.json`의 `judge`다(`harness judge --self`). 표를 고치면 `judge`와
`tests/test_judge.py`의 `KitSelfJudgeTest` 기대값을 같이 고친다. 이 테스트는 대표 경로의 판정, 모든 엄격 규칙의 사용,
엄격 대상 디렉터리(`core/hooks/`, `core/ci/gitlab/`, `.github/`)의 새 파일에 판정이 적혀 있는지를 본다. 표 문구 자체의
변경은 잡지 못하므로 사람이 맞춘다. 경로로 판정할 수 없는 조건(hooks 공통 코드의 차단 로직 여부, `ci.yml`의 보안 리허설 단계,
소비자 계약 파괴)은 사람 확인 목록으로 나온다.

표를 고친 뒤에는 키트 PR 이력으로 만든 사례 세트도 다시 돌린다. 본문 판정과 재판정이 다른 PR은 재판정을 정답으로
확정했다(사례의 `note`)
(`python bin/harness.py judge --self --eval tests/fixtures/judge-eval/kit.jsonl`, 사례는 `tests/test_judge_eval.py`가 검사한다).
판정이 바뀐 사례는 다시 생성해 덮지 말고 사례마다 새 판정이 맞는지 검토한 뒤 고친다. 시드 세트에는 엄격 사례가 없어
엄격 판정의 회귀는 `KitSelfJudgeTest`가 맡는다.

엄격 단계는 플랜 승인과 분리 리뷰를 거친다. 플랜과 리뷰 파일은 루트 `plans/`에 두며 커밋하지 않는다.
양식은 `docs/templates/`를 따른다. 측정 칸을 비워 두지 않는다.

## 검증

```bash
python -m unittest discover tests -v
python bin/harness.py check --self
```

`core/templates/`나 `core/hooks/`를 고쳤으면 `python bin/harness.py init --self`로 자기 적용 파일을 다시 생성한 뒤 검증한다.
생성된 파일(`AGENTS.md`, `docs/templates/*`, `docs/adr/0000-template.md`, PR 템플릿, `.claude/settings.json`, `.claude/hooks/*` 등)은 직접 고치지 않는다.

이 저장소에도 hooks가 설치돼 있다(`harness.json`의 `hooks`). Claude Code 세션은 응답을 끝낼 때마다 위 두 명령을 실행한다.
전체 테스트가 Windows 작업 PC에서 5~8분 걸려 기본 명령별 제한(300초)을 넘으므로 `stop_timeout_sec`를 780초로 둔다(전체 예산 840초, #92).
테스트가 더 길어지면 제한을 올리기 전에 느린 테스트를 줄이거나 종료 검증 명령을 나눈다.
`hooks.python`은 `python`이다. `python`이 없는 환경이면 개인 설정(`.claude/settings.local.json`)으로 끄고 `friction` 이슈로 남긴다.

PR에는 `security` workflow가 `core/ci/gitlab/`의 Secret 탐지(차단)와 SAST(경고) 조각을 같은 이미지·스크립트로 실행한다
(`.github/scripts/run_fragment.py`). 조각을 고치면 키트 PR에서 바로 그 조각이 돈다.
`ci` workflow의 `rehearsal` job은 설치된 hooks와 보안 조각 4종의 검출 시나리오를 임시 대상 저장소에서 실행한다.
외부 이미지·취약점 DB에 의존하므로 필수 체크에는 포함하지 않는다.
같은 workflow의 `profiles` job은 프로필마다 `tests/fixtures/profiles/<name>/` 저장소에 `init --area --profile`과
`scaffold`를 적용하고 영역 검증 명령(형식·컨벤션 테스트·lint·빌드)을 저장소 루트에서 실행한다. 규칙 위반을 넣은
음성 시나리오는 실패해야 통과다(`.github/scripts/rehearse_profiles.py`). Maven·npm 레지스트리와 러너의 Android SDK에
의존하므로 역시 필수 체크가 아니다. 프로필 템플릿·컨벤션 파일을 고친 PR은 이 job 결과를 확인한다.
PR이 workflow·러너·조각을 바꾸면 그 PR의 검사도 바뀐 정의로 돈다. 러너가 경고로 알리지만 막지는 않는다.
이런 PR은 엄격이라 사람이 병합하는 것이 최종 방어선이다. 브랜치 보호에는 `secret-detection`·`sast` 필수 체크와
"Require branches to be up to date before merging"을 함께 켠다(대상 브랜치가 앞서가도 검사가 다시 돌지 않는다).

## 자기 적용에서 얻은 불편은 이슈로

키트를 쓰면서 절차를 건너뛰고 싶어지는 순간이 오면 그 이유를 `friction` 라벨 이슈로 남긴다.
이 기록이 다음 프로젝트 팀원이 만날 불편의 예고이자 측정의 시작이다.
