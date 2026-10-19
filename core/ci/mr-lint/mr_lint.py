#!/usr/bin/env python3
"""MR 본문 lint(M2-2). 표준 라이브러리만 쓴다.

GitLab 조각(`core/ci/gitlab/mr-lint.yml`)은 `harness init`이 복사한 `.harness/mr-lint/mr_lint.py`를,
키트의 GitHub `ci` workflow는 이 원본을 실행한다(PR 쪽 원본으로 토큰 없이 lint, 기준 커밋 원본으로
`--post-report` 댓글). 판정 재실행에는 대상 저장소의 `harness.json`과
`.claude/hooks/harness_common.py`(init이 복사)를 쓴다.

    python3 mr_lint.py [--project DIR] [--common PATH] [--out mr-lint.json] [--no-comment]
                       [--body-file FILE --base REV --head REV]   # CI 밖에서 실행할 때
    python3 mr_lint.py --post-report mr-lint.json                 # 댓글 전용(키트 GitHub workflow)

종료 코드: 통과 0(통합 MR 건너뜀 포함), lint 실패 1, 입력·설정·git 오류 2. --post-report는 항상 0(실패는 경고).

GitLab이 CI 변수의 본문을 2700자에서 잘랐으면 `HARNESS_COMMENT_TOKEN`이 있을 때만 같은 CI의 API(`CI_API_V4_URL`)로
전체 본문을 다시 읽는다. 토큰이 없거나 읽지 못하면 잘린 본문으로 판정하지 않고 종료 2다.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

EXIT_PASS, EXIT_FAIL, EXIT_ERROR = 0, 1, 2
MARKER = "<!-- harness-mr-lint -->"
TIERS = ("lite", "standard", "strict")  # 뒤로 갈수록 강하다. harness_common.TIERS와 같다
TIER_ALIASES = {"lite": "lite", "standard": "standard", "strict": "strict",
                "경량": "lite", "표준": "standard", "엄격": "strict"}
TIER_LABELS = {"lite": "경량", "standard": "표준", "strict": "엄격"}
NOT_APPLICABLE = "해당 없음"
PLANS_IGNORE = "/plans/"
DEFAULT_COMMON = ".claude/hooks/harness_common.py"
# 리뷰 템플릿 6장 측정 칸의 항목. 엄격이면 MR 본문 `## 리뷰 결과`의 표에 모두 있고 값이 비어 있지 않아야 한다
MEASUREMENT_ROWS = (
    "읽은 기준 문서 절 수",
    "열린 조건부 관점",
    "발견 사항 수",
    "리뷰 세션 모델",
    "리뷰에서 뒤늦게 발견된 누락",
)
# 검증 절의 세 줄. (표시 이름, 줄 머리로 인정하는 낱말)
VERIFY_LINES = (("방법", ("방법",)), ("결과", ("결과",)), ("미검증", ("검증하지 못한", "미검증")))
GITHUB_BOT = "github-actions[bot]"
HTTP_TIMEOUT = 15
COMMENT_ITEM_CHARS = 300  # 댓글 실패 항목 한 줄 길이 상한
COMMENT_MAX_ITEMS = 30  # 댓글 실패 항목 수 상한(나머지는 개수만)
REPORT_MAX_BYTES = 1_000_000  # --post-report가 읽는 리포트 크기 상한
MR_RESPONSE_MAX_BYTES = 5_000_000  # 전체 본문 API 응답 크기 상한(GitLab 본문 상한 1,048,576자)
RESULTS = ("pass", "fail", "error", "skipped")  # 리포트 result 허용 값


class LintError(Exception):
    """검사를 할 수 없는 입력·설정·git 오류(종료 2)."""


# ---------------------------------------------------------------------------
# 본문 파싱
# ---------------------------------------------------------------------------


def strip_comments(text: str) -> str:
    """HTML 주석(템플릿 안내문)을 지운다. 닫히지 않은 주석은 끝까지 지운다."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    return re.sub(r"<!--.*\Z", "", text, flags=re.S)


def content_lines(body: str) -> list[str]:
    """검사할 줄. 코드 블록을 먼저 비운 뒤 HTML 주석을 지운다.

    주석을 먼저 지우면 코드 블록 안의 `` ```<!-- x --> `` 같은 줄이 닫는 펜스로 바뀌어 뒤의 예시 글이 본문으로 읽힌다.
    """
    lines = unfenced(body.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    return strip_comments("\n".join(lines)).split("\n")


FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")


def fence_step(line: str, fence: str | None) -> tuple[str | None, bool]:
    """CommonMark 코드 블록 경계. (다음 줄부터의 열린 울타리, 이 줄이 울타리 줄인가).

    여는 줄은 공백 3칸까지 + 같은 문자 3개 이상(backtick이면 정보 문자열에 backtick이 없어야 한다).
    닫는 줄은 여는 문자와 같은 문자를 여는 길이 이상 + 뒤에 공백만. 짧은 울타리나 다른 문자는 내용이다.
    """
    if fence is None:
        match = FENCE_OPEN.match(line)
        if match and not (match.group(1)[0] == "`" and "`" in match.group(2)):
            return match.group(1), True
        return None, False
    match = FENCE_CLOSE.match(line)
    if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence):
        return None, True
    return fence, False


def unfenced(lines: list[str]) -> list[str]:
    """코드 블록 안 줄을 빈 줄로 바꾼다. 예시로 적은 `Closes`·체크 상자가 검사를 통과시키지 않게 한다."""
    out, fence = [], None
    for line in lines:
        inside = fence is not None
        fence, marker = fence_step(line, fence)
        out.append("" if marker or inside else line)
    return out


def split_sections(lines: list[str]) -> dict[str, list[str]]:
    """`## 제목` 단위 줄 목록. 코드 블록 안의 `##`는 제목으로 보지 않는다. 같은 제목이 또 나오면 이어 붙인다."""
    sections: dict[str, list[str]] = {}
    current, fence = None, None
    for line in lines:
        inside = fence is not None
        fence, marker = fence_step(line, fence)
        if not marker and not inside and re.match(r"^##\s+\S", line) and not line.startswith("###"):
            current = line[2:].strip().rstrip("#").strip()
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return sections


def meaningful(lines: list[str]) -> list[str]:
    return [line.strip() for line in lines if line.strip()]


def bullet_text(line: str) -> str:
    """목록 기호·강조 기호를 걷어 낸 내용."""
    text = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s*", "", line.strip())
    return text.strip().strip("*_`").strip()


NOT_APPLICABLE_WORDS = ("해당없음", "없음", "n/a", "na", "none", "-")


def is_not_applicable(line: str) -> bool:
    """'해당 없음'과 그 변형(`해당없음`, `해당 없음 (표준이라)`, `N/A`, `-`)."""
    text = re.sub(r"\s+", "", bullet_text(line)).rstrip(".。").lower()
    return text.startswith("해당없음") or text in NOT_APPLICABLE_WORDS or not re.search(r"[0-9A-Za-z가-힣]", text)


def parse_tier(lines: list[str]) -> str | None:
    """`## 판정` 절의 첫 내용 줄에서 판정 식별자. 첫 낱말이 허용 값이 아니면 None."""
    for line in meaningful(lines):
        text = bullet_text(line)
        if not text:
            continue
        match = re.match(r"^([A-Za-z]+|[가-힣]+)", text)
        return TIER_ALIASES.get(match.group(1).lower()) if match else None
    return None


def has_reference(lines: list[str], examples: tuple[str, ...] = ()) -> bool:
    """`Closes`/`Refs` 줄에 실제 업무 참조가 있는가. 템플릿 예시 키나 '없음'은 참조로 보지 않는다."""
    blocked = {e.lower() for e in examples} | {w.lower() for w in NOT_APPLICABLE_WORDS} | {"해당"}
    for line in lines:
        match = re.match(r"^\s*(?:[-*+]\s+)?(?:closes|refs)\b[:\s]+(.+)$", line, re.I)
        if not match:
            continue
        tokens = [tok.strip(".,;`*_()") for tok in re.split(r"[\s,]+", match.group(1)) if tok.strip(".,;`*_()")]
        if any(tok.lower() not in blocked for tok in tokens):
            return True
    return False


def example_references(config: dict | None) -> tuple[str, ...]:
    """MR 템플릿 `Closes` 줄의 예시 업무 키(자리표시자 issue_key_example, bin/harness.py build_context와 같은 규칙)."""
    if not config:
        return ()
    if config.get("tracker") == "jira":
        return (f"{config.get('issue_prefix', '')}-52",)
    return ("#52",)


def verify_values(lines: list[str]) -> dict[str, str | None]:
    """검증 절의 방법·결과·미검증 값. 줄이 없으면 None. 값은 같은 줄 또는 들여쓴 다음 줄에 적을 수 있다."""
    found: dict[str, str | None] = {name: None for name, _ in VERIFY_LINES}
    for i, line in enumerate(lines):
        match = re.match(r"^[-*+]\s+(.*)$", line)
        if not match:
            continue
        head, value = (re.split(r"[:：]", match.group(1), maxsplit=1) + [""])[:2]
        head = head.strip().strip("*_").strip()
        for name, prefixes in VERIFY_LINES:
            if found[name] is None and head.startswith(prefixes):
                parts = [value.strip()]
                for follow in lines[i + 1:]:
                    if not follow.strip():
                        continue
                    if not follow[:1].isspace():
                        break
                    parts.append(follow.strip())
                found[name] = " ".join(p for p in parts if p)
                break
    return found


def table_rows(lines: list[str]) -> list[list[str]]:
    """GFM 표의 데이터 행(머리·구분줄 제외) 칸 목록."""
    rows, header_seen = [], False
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            header_seen = False  # 표 밖 줄이 나오면 표가 끝난다
            continue
        cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", stripped.strip("|"))]
        if all(re.fullmatch(r":?-{1,}:?", cell) for cell in cells if cell) and any(cells):
            header_seen = True
            continue
        if header_seen:
            rows.append(cells)
    return rows


def lint_body(body: str, judged: str | None, examples: tuple[str, ...] = ()) -> dict:
    """본문 검사 결과. judged는 CI가 다시 낸 판정(없으면 본문만), examples는 템플릿 예시 업무 키."""
    lines = unfenced(content_lines(body))  # 주석을 지운 뒤 새로 생긴 펜스도 비운다
    sections = split_sections(lines)
    failures: list[str] = []

    if not has_reference(lines, examples):
        failures.append("`Closes` 또는 `Refs` 업무 참조 줄이 없다.")

    if "검증" not in sections:
        failures.append("`## 검증` 절이 없다.")
    else:
        for name, value in verify_values(sections["검증"]).items():
            if value is None:
                failures.append(f"`## 검증`에 '{name}' 줄이 없다.")
            elif not value:
                failures.append(f"`## 검증`의 '{name}' 값이 비어 있다.")

    if "영향 범위" not in sections:
        failures.append("`## 영향 범위` 절이 없다.")
    elif not any(re.match(r"^\s*[-*+]\s+\[[xX]\]", line) for line in sections["영향 범위"]):
        failures.append("`## 영향 범위`에 체크한 항목이 없다(해당 없으면 '해당 없음'을 체크한다).")

    body_tier = None
    if "판정" not in sections:
        failures.append("`## 판정` 절이 없다.")
    else:
        body_tier = parse_tier(sections["판정"])
        if body_tier is None:
            failures.append("`## 판정` 값이 lite·standard·strict(경량·표준·엄격) 중 하나가 아니다.")

    known = [t for t in (body_tier, judged) if t]
    effective = max(known, key=TIERS.index) if known else None
    if effective == "strict":
        failures.extend(strict_failures(sections))
    return {
        "failures": failures,
        "tier": {
            "body": body_tier,
            "judge": judged,
            "effective": effective,
            "mismatch": bool(body_tier and judged and body_tier != judged),
        },
    }


def strict_failures(sections: dict[str, list[str]]) -> list[str]:
    failures = []
    for title in ("플랜 요약", "리뷰 결과"):
        if title not in sections:
            failures.append(f"엄격 판정인데 `## {title}` 절이 없다.")
            continue
        # 표(측정 칸)는 내용으로 치지 않는다. 표 밖에 실제 요약이 있어야 한다
        lines = [line for line in meaningful(sections[title]) if not line.startswith("|")]
        if any(is_not_applicable(line) for line in lines):
            failures.append(f"엄격 판정인데 `## {title}`가 '{NOT_APPLICABLE}'이다.")
        elif not lines:
            failures.append(f"엄격 판정인데 `## {title}`가 비어 있다.")
    if "리뷰 결과" in sections:
        rows = table_rows(sections["리뷰 결과"])
        labels = [row[0] for row in rows if row]
        for name in MEASUREMENT_ROWS:
            if not any(name in label for label in labels):
                failures.append(f"엄격 판정인데 `## 리뷰 결과` 측정 칸에 '{name}' 항목이 없다.")
        for row in rows:
            if len(row) < 2 or any(not cell for cell in row):
                failures.append(f"엄격 판정인데 `## 리뷰 결과` 측정 칸 '{row[0] if row else ''}'이 비어 있다.")
    return failures


# ---------------------------------------------------------------------------
# /plans/ 규칙(D-13)
# ---------------------------------------------------------------------------


CONFIRM_SECTION = "보호 경로 확인"  # 판정의 "사람 확인 필요"(트리거)와 다른 절이다
PROTECTED_LIST_LIMIT = 8


def protected_failures(protected: list[tuple[str, dict]], body: str) -> list[str]:
    """보호 경로를 바꾼 MR은 본문 `## 보호 경로 확인`에 체크한 항목이 있어야 한다(#100).

    hooks가 없는 도구(Codex 등)나 hooks를 끈 세션의 변경도 MR 단계에서 같은 규칙으로 사람 확인을 요구한다.
    hook이 AI에게 막는(`block`) 경로도 사람이 의존성을 갱신하는 정상 작업이 있어 여기서는 확인 표시로 통과시킨다.
    """
    if not protected:
        return []
    sections = split_sections(unfenced(content_lines(body)))
    if any(re.match(r"^\s*[-*+]\s+\[[xX]\]", line) for line in sections.get(CONFIRM_SECTION, [])):
        return []
    shown = ", ".join(f"`{path}`({rule.get('reason', '보호 경로')})" for path, rule in protected[:PROTECTED_LIST_LIMIT])
    more = f" 외 {len(protected) - PROTECTED_LIST_LIMIT}개" if len(protected) > PROTECTED_LIST_LIMIT else ""
    return [f"보호 경로를 바꿨다: {shown}{more}. 사람이 변경을 확인한 뒤 `## {CONFIRM_SECTION}`의 항목을 체크한다"
            "(hooks가 없는 도구로 한 변경도 같은 규칙이다)."]


def rules_config_for(common, config: dict | None) -> tuple[dict, str | None]:
    """규칙에 쓸 설정과 경고. hook과 같은 검사(`validate_hooks`·`validate_areas_for_hooks`)를 통과하지 못하면
    끌 수 없는 규칙과 기본 목록으로 검사한다(설정이 깨져도 보호를 끄지 않고, 설정을 고치는 MR도 막지 않는다, #100 리뷰 B F1)."""
    if config is None:
        return {}, None
    try:
        if "hooks" in config:
            common.validate_hooks(config["hooks"], "harness.json")
        common.validate_areas_for_hooks(config.get("areas", []), "harness.json")
    except common.ConfigError as exc:
        return {}, f"기준 설정이 잘못돼 기본 보호 경로로 검사한다: {exc}"
    return config, None


def protected_changes(common, changes: list[tuple[str, str]], rules_config: dict | None) -> list[tuple[str, dict]]:
    """변경(삭제 포함) 중 보호 경로 규칙에 맞는 것. 규칙은 hook과 같은 `harness_common.rules_for`·`decide`다."""
    rules = common.rules_for(rules_config if rules_config is not None else {})
    return [(path, rule) for _status, path in changes if (rule := common.decide(path, rules)) is not None]


def gitignore_has_plans(text: str | None) -> bool:
    return text is not None and any(line.strip() == PLANS_IGNORE for line in text.splitlines())


def plans_failures(changes: list[tuple[str, str]], gitignore: str | None) -> list[str]:
    failures = []
    added = sorted(path for status, path in changes if not status.startswith("D") and path.startswith("plans/"))
    if added:
        failures.append(f"플랜·리뷰 파일(`plans/`)은 커밋하지 않는다: {len(added)}개 파일")
    if not gitignore_has_plans(gitignore):
        failures.append(f"`.gitignore`에 `{PLANS_IGNORE}` 줄이 없다(`harness init`이 추가한다).")
    return failures


# ---------------------------------------------------------------------------
# git과 판정 재실행
# ---------------------------------------------------------------------------


def git(project: Path, *args: str) -> str:
    try:
        result = subprocess.run(["git", "-c", "core.quotepath=off", *args], cwd=project,
                                capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise LintError(f"git을 실행할 수 없다: {exc}") from exc
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise LintError(f"git {args[0]} 실패: {message[0] if message else result.returncode}")
    return result.stdout.decode("utf-8", errors="replace")


def changed(project: Path, base: str, head: str) -> list[tuple[str, str]]:
    """(상태, 경로) 목록. base와 head의 merge-base 이후 head까지. rename은 삭제·추가 두 줄로 본다."""
    for name, rev in (("기준", base), ("대상", head)):
        if not rev or rev.startswith("-"):
            raise LintError(f"{name} 커밋이 잘못됐다: {rev!r}")
        try:
            git(project, "cat-file", "-e", rev + "^{commit}")  # 템플릿 자리표시자 모양(이중 중괄호)을 쓰지 않는다
        except LintError as exc:
            raise LintError(f"{name} 커밋 {rev} 이 저장소에 없다. 얕은 clone이면 GIT_DEPTH를 0으로 한다.") from exc
    merge_base = git(project, "merge-base", base, head).strip()
    fields = git(project, "diff", "--name-status", "--no-renames", "-z", merge_base, head).split("\0")
    fields = [f for f in fields if f]
    return [(fields[i], fields[i + 1]) for i in range(0, len(fields) - 1, 2)]


def load_common(path: Path):
    if not path.is_file():
        raise LintError(f"{path} 이 없다. 판정을 다시 계산할 수 없다. `harness init`으로 생성한다.")
    spec = importlib.util.spec_from_file_location("harness_common_for_mr_lint", path)
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True  # 관리 hook 디렉터리에 __pycache__를 남기면 `harness check`가 여분으로 본다
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # 복사본이 깨졌으면 원인을 그대로 보인다
        raise LintError(f"{path} 을 읽을 수 없다: {exc}") from exc
    finally:
        sys.dont_write_bytecode = previous
    for name in ("judge", "validate_judge_root", "validate_judge_rules", "ConfigError", "rules_for", "decide",
                 "validate_hooks", "validate_areas_for_hooks"):
        if not hasattr(module, name):
            raise LintError(f"{path} 에 {name} 이 없다. 키트 버전을 맞춘다(`harness init --force`).")
    return module


def rejudge(project: Path, common, paths: list[str]) -> dict:
    config_path = project / "harness.json"
    if not config_path.is_file():
        raise LintError(f"{config_path} 이 없다.")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise LintError(f"{config_path}: 최상위는 객체여야 한다.")
        areas = config.get("areas", [])
        if not isinstance(areas, list) or any(not isinstance(a, dict) or not isinstance(a.get("dir"), str)
                                              for a in areas):
            raise LintError(f"{config_path}: areas 항목에는 dir이 필요하다.")
        for area in areas:
            common.validate_judge_rules(area, config_path)
        if "judge" in config:
            common.validate_judge_root(config["judge"], config_path)
        return common.judge(paths, config)
    except (OSError, ValueError) as exc:
        raise LintError(f"{config_path}: {exc}") from exc
    except common.ConfigError as exc:
        raise LintError(str(exc)) from exc


# ---------------------------------------------------------------------------
# CI 입력
# ---------------------------------------------------------------------------


def context_from_env(env: dict) -> dict:
    """CI 환경에서 본문·커밋 범위·댓글 위치. CI가 아니면 LintError."""
    if env.get("GITLAB_CI") == "true":
        body = env.get("CI_MERGE_REQUEST_DESCRIPTION")
        if body is None:
            raise LintError("CI_MERGE_REQUEST_DESCRIPTION이 없다. MR 파이프라인인지, GitLab 16.7 이상인지 확인한다.")
        source_project = env.get("CI_MERGE_REQUEST_SOURCE_PROJECT_ID", "")
        mr_project = env.get("CI_MERGE_REQUEST_PROJECT_ID", "")
        return {
            "platform": "gitlab",
            "body": body,
            # 잘렸으면 run이 통합 MR 판단 뒤에 토큰으로 전체 본문을 읽거나 검사 불가로 끝낸다
            "truncated": env.get("CI_MERGE_REQUEST_DESCRIPTION_IS_TRUNCATED") == "true",
            "source_branch": env.get("CI_MERGE_REQUEST_SOURCE_BRANCH_NAME", ""),
            "target_branch": env.get("CI_MERGE_REQUEST_TARGET_BRANCH_NAME", ""),
            # fork MR이면 소스 프로젝트가 다르다. 둘 다 있어야 같은 프로젝트로 본다
            "same_repo": bool(source_project) and source_project == mr_project,
            "mr_project": mr_project or env.get("CI_PROJECT_ID", ""),
            "base": env.get("CI_MERGE_REQUEST_DIFF_BASE_SHA", ""),
            # 통합 MR 판단용 대상 브랜치 최신 커밋. merged results 파이프라인에서만 있고, 없으면 diff 기준 커밋을 쓴다
            "target_sha": env.get("CI_MERGE_REQUEST_TARGET_BRANCH_SHA") or env.get("CI_MERGE_REQUEST_DIFF_BASE_SHA", ""),
            # merged results면 CI_COMMIT_SHA는 병합 결과다. MR 변경만 보도록 소스 커밋을 쓴다
            "head": env.get("CI_MERGE_REQUEST_SOURCE_BRANCH_SHA") or env.get("CI_COMMIT_SHA", ""),
            "api": env.get("CI_API_V4_URL", ""),
            "project": env.get("CI_PROJECT_ID", ""),
            "number": env.get("CI_MERGE_REQUEST_IID", ""),
        }
    if env.get("GITHUB_ACTIONS") == "true":
        if env.get("GITHUB_EVENT_NAME") != "pull_request":
            raise LintError(f"pull_request 이벤트에서만 실행한다(현재 {env.get('GITHUB_EVENT_NAME')!r}).")
        try:
            event = json.loads(Path(env.get("GITHUB_EVENT_PATH", "")).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise LintError(f"GitHub 이벤트 파일을 읽을 수 없다: {exc}") from exc
        pr = event.get("pull_request") if isinstance(event, dict) else None
        if not isinstance(pr, dict):
            raise LintError("이벤트에 pull_request가 없다.")
        base = pr.get("base") if isinstance(pr.get("base"), dict) else {}
        head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
        base_repo = (base.get("repo") or {}).get("full_name")
        head_repo = (head.get("repo") or {}).get("full_name")
        return {
            "platform": "github",
            "body": pr.get("body") or "",  # 본문이 비어 있으면 null이다
            "truncated": False,
            "source_branch": head.get("ref", ""),
            "target_branch": base.get("ref", ""),
            # fork PR이면 head 저장소가 다르다(지워진 fork는 null). 같은 이름의 fork 브랜치로 건너뛰지 못하게 한다
            "same_repo": isinstance(head_repo, str) and bool(head_repo) and head_repo == base_repo,
            "base": base.get("sha", ""),
            "head": head.get("sha", ""),
            "api": env.get("GITHUB_API_URL", ""),
            "project": env.get("GITHUB_REPOSITORY", ""),
            "number": str(pr.get("number", "")),
        }
    raise LintError("GitLab MR 파이프라인이나 GitHub pull_request가 아니다. --body-file·--base·--head를 준다.")


def is_integration_mr(ctx: dict | None, config: dict | None) -> bool:
    """통합 MR(integration → default, 같은 저장소)인가. 값이 하나라도 없거나 이상하면 False(검사한다)."""
    if not ctx or not isinstance(config, dict):
        return False
    integration, default = config.get("integration_branch"), config.get("default_branch")
    if not (isinstance(integration, str) and isinstance(default, str) and integration and default
            and integration != default):
        return False
    return (ctx.get("same_repo") is True and ctx.get("source_branch") == integration
            and ctx.get("target_branch") == default)


def fetch_full_body(ctx: dict, token: str) -> str:
    """잘린 GitLab 본문 대신 API로 전체 본문을 읽는다. 실패는 LintError(토큰·응답 본문은 메시지에 넣지 않는다)."""
    prefix = "MR 본문이 CI 변수에서 잘렸고(2700자) API로 전체 본문을 읽지 못했다"
    api = (ctx.get("api") or "").rstrip("/")
    project, number = str(ctx.get("mr_project") or ""), str(ctx.get("number") or "")
    if not api.startswith("https://") or not project or not number.isdigit():
        raise LintError(f"{prefix}: API 주소가 https가 아니거나 MR 정보가 없다.")
    url = f"{api}/projects/{urllib.parse.quote(project, safe='')}/merge_requests/{number}"
    request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    # 리다이렉트로 다른 주소에 토큰을 넘기지 않는다(urllib은 unredirected 헤더를 옮기지 않는다)
    request.add_unredirected_header("PRIVATE-TOKEN", token)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            raw = response.read(MR_RESPONSE_MAX_BYTES + 1)
        if len(raw) > MR_RESPONSE_MAX_BYTES:
            raise ValueError("응답이 너무 크다")
        data = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        hint = " 토큰 범위(read_api 이상)·역할·만료를 확인한다." if exc.code in (401, 403, 404) else ""
        raise LintError(f"{prefix}(HTTP {exc.code}).{hint}") from None
    except (urllib.error.URLError, OSError, ValueError, RecursionError) as exc:
        raise LintError(f"{prefix}({type(exc).__name__}).") from None
    if not isinstance(data, dict) or str(data.get("iid")) != number:
        raise LintError(f"{prefix}(응답이 이 MR이 아니다).")
    description = data.get("description", 0)  # 빈 본문은 null이다. 키가 없으면 형식 오류
    if description is not None and not isinstance(description, str):
        raise LintError(f"{prefix}(description 형식).")
    return description or ""


# ---------------------------------------------------------------------------
# 댓글(D-12, 선택)
# ---------------------------------------------------------------------------


def http_json(method: str, url: str, headers: dict, data: dict | None = None):
    request = urllib.request.Request(url, method=method, headers={**headers, "Content-Type": "application/json"},
                                     data=json.dumps(data).encode("utf-8") if data is not None else None)
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8")) if raw else None


def comment_item(text: str) -> str:
    """실패 항목 한 줄. 목록 밖으로 나가거나 HTML·멘션·이미지·코드 블록이 되지 않게 무력화하고 길이를 자른다."""
    text = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f  ]", " ", text)).strip()
    if len(text) > COMMENT_ITEM_CHARS:
        text = text[:COMMENT_ITEM_CHARS - 1] + "…"
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("@", "@​")
    text = re.sub(r"[\[\]!]", lambda m: "\\" + m.group(0), text)
    return re.sub(r"([`~])\1{2,}", lambda m: "".join("\\" + ch for ch in m.group(0)), text)


def sanitize_report(raw) -> dict:
    """댓글에 쓸 필드만 검증해 새로 만든다. 모르는 키는 버린다. result가 허용 값이 아니면 ValueError."""
    if not isinstance(raw, dict) or raw.get("result") not in RESULTS:
        raise ValueError("리포트 형식이 아니다(result).")
    tier = raw.get("tier") if isinstance(raw.get("tier"), dict) else {}
    clean_tier = {key: tier.get(key) if tier.get(key) in TIERS else None for key in ("body", "judge", "effective")}
    clean_tier["mismatch"] = tier.get("mismatch") is True
    # 오류 메시지는 경로를 담을 수 있어 옮기지 않는다(job 로그에 있다)
    listed = raw["result"] in ("pass", "fail") and isinstance(raw.get("failures"), list)
    items = raw.get("failures") if listed else []
    failures = [comment_item(item) for item in items if isinstance(item, str) and item.strip()]
    human = raw.get("human_check")
    return {
        "result": raw["result"],
        "tier": clean_tier,
        "failures": failures[:COMMENT_MAX_ITEMS],
        "omitted": max(0, len(failures) - COMMENT_MAX_ITEMS),
        "human_check": min(len(human), 999) if isinstance(human, list) else 0,
    }


def comment_text(report: dict) -> str:
    """판정 댓글. 입력은 sanitize_report를 거친 필드만 쓴다(본문·경로는 옮기지 않는다)."""
    report = sanitize_report(report)
    tier = report["tier"]
    title = {"pass": "통과", "fail": "실패", "error": "검사할 수 없음", "skipped": "건너뜀"}[report["result"]]
    lines = [MARKER, f"### harness MR 본문 lint: {title}", ""]
    if tier["effective"]:
        lines.append(f"- 적용 판정: {TIER_LABELS[tier['effective']]}({tier['effective']})")
    if report["result"] == "skipped":
        lines.append("- 통합 MR(integration → default)이라 검사하지 않았다.")
    elif report["result"] != "error":
        lines.append(f"- 본문 판정: {tier['body'] or '없음'} · 변경 파일 판정(harness judge): {tier['judge'] or '없음'}"
                     + (" · **불일치**" if tier["mismatch"] else ""))
    if report["human_check"]:
        lines.append(f"- 사람 확인이 필요한 트리거 {report['human_check']}개(job 로그). 해당하면 판정을 올린다.")
    if report["result"] == "error":
        lines.append("- 입력·설정·git 오류로 검사하지 못했다(종료 2). 원인은 job 로그에 있다.")
    if report["failures"]:
        lines += ["", "실패 항목:"] + [f"- {item}" for item in report["failures"]]
        if report["omitted"]:
            lines.append(f"- 외 {report['omitted']}개(job 로그)")
    lines += ["", "자세한 근거는 job 로그와 `mr-lint.json` 아티팩트에 있다."]
    return "\n".join(lines)


def _paged(url: str, headers: dict) -> list:
    items = []
    for page in range(1, 21):
        sep = "&" if "?" in url else "?"
        batch = http_json("GET", f"{url}{sep}per_page=100&page={page}", headers)
        if not isinstance(batch, list):
            break
        items.extend(batch)
        if len(batch) < 100:
            break
    return items


def post_comment(ctx: dict, text: str, token: str) -> str:
    """표식 댓글을 갱신하거나 만든다. 결과 문자열(created·updated). 실패는 예외."""
    api = ctx["api"].rstrip("/")
    if not api.startswith("https://") or not ctx.get("project") or not ctx.get("number"):
        raise LintError("댓글 API 주소가 https가 아니거나 MR 정보가 없다.")
    if ctx["platform"] == "gitlab":
        headers = {"PRIVATE-TOKEN": token}
        base = f"{api}/projects/{urllib.parse.quote(str(ctx['project']), safe='')}/merge_requests/{ctx['number']}/notes"
        me = (http_json("GET", f"{api}/user", headers) or {}).get("id")
        notes = _paged(f"{base}?sort=asc&order_by=created_at", headers)
        mine = next((n for n in notes if isinstance(n, dict) and not n.get("system") and MARKER in (n.get("body") or "")
                     and me is not None and (n.get("author") or {}).get("id") == me), None)
        if mine:
            http_json("PUT", f"{base}/{mine['id']}", headers, {"body": text})
            return "updated"
        http_json("POST", base, headers, {"body": text})
        return "created"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    repo = ctx["project"]
    comments = _paged(f"{api}/repos/{repo}/issues/{ctx['number']}/comments", headers)
    mine = next((c for c in comments if isinstance(c, dict) and MARKER in (c.get("body") or "")
                 and (c.get("user") or {}).get("login") == GITHUB_BOT), None)
    if mine:
        http_json("PATCH", f"{api}/repos/{repo}/issues/comments/{mine['id']}", headers, {"body": text})
        return "updated"
    http_json("POST", f"{api}/repos/{repo}/issues/{ctx['number']}/comments", headers, {"body": text})
    return "created"


def try_comment(ctx: dict | None, report: dict, token: str | None) -> str:
    if not token:
        return "skipped"
    if isinstance(report, dict) and report.get("result") == "skipped":
        return "skipped"  # 통합 MR 건너뜀은 보여 줄 판정이 없어 댓글을 남기지 않는다(API 호출도 없다)
    if ctx is None:
        print("harness 경고: CI 밖 실행이라 댓글을 남기지 않는다.")
        return "skipped"
    try:
        return post_comment(ctx, comment_text(report), token)
    except urllib.error.HTTPError as exc:
        reason = f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError, ValueError, LintError) as exc:
        reason = type(exc).__name__ if not isinstance(exc, LintError) else str(exc)
    # fork PR의 읽기 전용 토큰 등. 댓글은 선택 기능이라 lint 결과를 바꾸지 않는다
    print(f"harness 경고: 판정 댓글을 남기지 못했다({reason}). 결과는 job 로그와 아티팩트에 있다.")
    return "failed"


def post_report(path: str, env: dict) -> str:
    """댓글 전용 모드. lint job이 남긴 리포트를 읽어 검증한 필드로 댓글을 만든다. 실패는 경고만 낸다.

    키트 GitHub workflow는 PR 코드를 토큰 없이 lint하고, 기준 커밋의 이 모듈만 토큰을 받아 이 모드로 댓글을 남긴다.
    리포트는 PR 코드가 쓴 신뢰할 수 없는 입력이라 sanitize_report를 거친 값만 쓴다.
    """
    token = env.get("HARNESS_COMMENT_TOKEN")
    if not token:
        print("harness 경고: HARNESS_COMMENT_TOKEN이 없어 판정 댓글을 남기지 않는다.")
        return "skipped"
    try:
        raw = Path(path).read_bytes()
        if len(raw) > REPORT_MAX_BYTES:
            raise ValueError("리포트가 너무 크다.")
        report = json.loads(raw.decode("utf-8"))
        result = sanitize_report(report)["result"]  # 형식 확인. 댓글 문구는 comment_text가 다시 검증해 만든다
        ctx = context_from_env(env)
    except (OSError, ValueError, RecursionError, LintError) as exc:
        reason = str(exc) if isinstance(exc, LintError) else type(exc).__name__
        print(f"harness 경고: 리포트나 PR 정보를 읽을 수 없어 판정 댓글을 남기지 않는다({reason}).")
        return "failed"
    status = try_comment(ctx, report, token)
    print(f"harness: 판정 댓글 {status} (결과 {result})")
    return status


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------


def read_config(project: Path) -> dict | None:
    """harness.json. 없으면 None(없을 때의 오류는 rejudge가 낸다)."""
    config_path = project / "harness.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else None
    except (OSError, ValueError) as exc:
        raise LintError(f"harness.json을 읽을 수 없다: {exc}") from exc
    return config if isinstance(config, dict) else None


def base_config(project: Path, base: str) -> dict | None:
    """대상 쪽 기준 커밋의 harness.json. MR이 바꿀 수 없는 값이라 통합 MR 판단에 쓴다. 읽지 못하면 None(건너뛰지 않는다)."""
    if not base or base.startswith("-"):
        return None
    try:
        config = json.loads(git(project, "show", f"{base}:harness.json"))
    except (LintError, ValueError):
        return None
    return config if isinstance(config, dict) else None


def run(args, env: dict) -> tuple[int, dict]:
    project = Path(args.project).resolve()
    local = args.body_file is not None
    if local:
        if not args.base or not args.head:
            raise LintError("--body-file에는 --base와 --head가 필요하다.")
        try:
            body = Path(args.body_file).read_bytes().decode("utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            raise LintError(f"본문 파일을 읽을 수 없다: {exc}") from exc
        ctx = None
        base, head = args.base, args.head
    else:
        ctx = context_from_env(env)
        body, base, head = ctx["body"], ctx["base"], ctx["head"]
    config = read_config(project)
    # 통합 MR 판단은 MR이 고친 harness.json이 아니라 대상 쪽 기준 커밋의 값으로 한다.
    # MR 쪽 값을 쓰면 같은 MR에서 integration_branch를 자기 브랜치로 바꿔 lint를 끌 수 있다
    if is_integration_mr(ctx, base_config(project, ctx.get("target_sha") or base) if ctx is not None else None):
        # 통합 MR(Release.md)에는 업무 참조·판정 절이 없다. 본문·git을 보지 않고 통과로 끝낸다
        print(f"harness: 통합 MR({ctx['source_branch']} → {ctx['target_branch']})이라 MR 본문 lint를 건너뛴다(통과).")
        return EXIT_PASS, {
            "result": "skipped",
            "reason": "integration",
            "failures": [],
            "tier": {"body": None, "judge": None, "effective": None, "mismatch": False},
            "files": [],
            "human_check": [],
            "comment": "skipped",
        }
    if ctx is not None and ctx.get("truncated"):
        token = env.get("HARNESS_COMMENT_TOKEN")
        if not token:
            raise LintError("MR 본문이 길어 CI 변수에서 잘렸다(2700자). 잘린 본문으로 판정하지 않는다. 본문을 줄이거나 "
                            "HARNESS_COMMENT_TOKEN(read_api 이상)을 등록해 전체 본문을 읽게 한다.")
        body = fetch_full_body(ctx, token)
        print(f"harness: MR 본문이 CI 변수에서 잘려 API로 전체 본문({len(body)}자)을 읽었다.")
    common_path = Path(args.common) if args.common else project / DEFAULT_COMMON
    common = load_common(common_path if common_path.is_absolute() else project / common_path)
    changes = changed(project, base, head)
    judged = rejudge(project, common, [path for _status, path in changes])
    gitignore_path = project / ".gitignore"
    gitignore = gitignore_path.read_text(encoding="utf-8", errors="replace") if gitignore_path.is_file() else None
    result = lint_body(body, judged["tier"], example_references(config))
    # 보호 경로 규칙은 대상 쪽 기준 커밋의 설정으로 정한다. MR이 같은 MR에서 protected_paths를 비워 검사를 끌 수 없다
    base_rules = base_config(project, (ctx or {}).get("target_sha") or base)
    rules_config, warning = rules_config_for(common, base_rules if base_rules is not None else config)
    if warning:
        print(f"harness 경고: {warning}")
    protected = protected_changes(common, changes, rules_config)
    failures = plans_failures(changes, gitignore) + result["failures"] + protected_failures(protected, body)
    report = {
        "result": "fail" if failures else "pass",
        "failures": failures,
        "tier": result["tier"],
        "files": judged["files"],
        "human_check": judged["human_check"],
    }
    report["comment"] = "skipped" if args.no_comment else try_comment(ctx, report, env.get("HARNESS_COMMENT_TOKEN"))
    return (EXIT_FAIL if failures else EXIT_PASS), report


def print_report(report: dict) -> None:
    if report["result"] == "skipped":
        return  # run이 건너뛴 이유를 이미 출력했다
    tier = report["tier"]
    print(f"판정: 적용 {tier['effective'] or '없음'} (본문 {tier['body'] or '없음'}, harness judge {tier['judge'] or '없음'})")
    if tier["mismatch"]:
        print("판정 불일치: 본문 판정과 변경 파일 판정이 다르다(측정 4번). 높은 쪽을 적용했다.")
    for item in sorted(report["files"], key=lambda f: (-TIERS.index(f["tier"]), f["path"])):
        print(f"  {item['tier']:<8} {item['path']}  {item['rule']}")
    if report["human_check"]:
        print("사람 확인 필요(경로로 판정하지 않는 트리거. 해당하면 판정을 올린다):")
        for item in report["human_check"]:
            print(f"  [{item['area'] or '영역 밖'}] {item['trigger']}")
    if report["failures"]:
        print("harness: MR 본문 lint 실패")
        for item in report["failures"]:
            print(f"  - {item}")
    else:
        print("harness: MR 본문 lint 통과")


def main(argv: list[str] | None = None, env: dict | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="mr_lint", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", default=".", help="대상 저장소(기본 현재 디렉터리)")
    parser.add_argument("--common", help=f"harness_common.py 경로(기본 {DEFAULT_COMMON})")
    parser.add_argument("--out", default="mr-lint.json", help="JSON 결과 파일(기본 mr-lint.json)")
    parser.add_argument("--no-comment", action="store_true", help="토큰이 있어도 댓글을 남기지 않는다")
    parser.add_argument("--body-file", help="CI 밖 실행: MR 본문 파일")
    parser.add_argument("--base", help="CI 밖 실행: 기준 커밋")
    parser.add_argument("--head", help="CI 밖 실행: 대상 커밋")
    parser.add_argument("--post-report", metavar="JSON",
                        help="댓글 전용: lint 리포트를 읽어 판정 댓글만 남긴다(검사하지 않음, 항상 종료 0)")
    args = parser.parse_args(argv)
    env = dict(os.environ) if env is None else env
    if args.post_report is not None:
        post_report(args.post_report, env)  # 댓글은 선택 기능이라 PR 체크를 실패시키지 않는다
        return EXIT_PASS
    try:
        code, report = run(args, env)
    except LintError as exc:
        print(f"harness: 검사할 수 없다: {exc}")
        report, code = {"result": "error", "error": str(exc)}, EXIT_ERROR
    else:
        print_report(report)
    try:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"harness: 결과 파일을 쓸 수 없다: {exc}")
        return EXIT_ERROR
    return code


if __name__ == "__main__":
    sys.exit(main())
