#!/usr/bin/env python3
"""ai-dev-harness 명령 진입점. 표준 라이브러리만 사용한다.

    python bin/harness.py init <target> [--project-name ..] [--platform gitlab|github]
                               [--tracker jira|github] [--issue-prefix KEY] [--force]
    python bin/harness.py init --self        # 키트 저장소 자신에게 템플릿을 다시 생성
    python bin/harness.py init <target> --area <dir> --verify-cmd <cmd> [--verify-cmd ..]
                               [--trigger ..] [--review-focus ..] [--area-doc ..] [--force]
                                             # 영역 디렉터리에 AGENTS.md 생성(루트 init 이후)
    python bin/harness.py init <target> --area <dir> --profile <name> [--var k=v ..] [--force]
                                             # 프로필 기본값을 영역 설정에 굳히고 컨벤션 파일 생성
    python bin/harness.py scaffold <area> <kind> <Name> [--var k=v ..] [--target <dir>]
                                             # 영역 프로필의 템플릿으로 뼈대 생성(기존 파일이 있으면 중단)
    python bin/harness.py check <target>    # 렌더링 결과와 실제 파일의 차이(드리프트) 검사
    python bin/harness.py check --self
    python bin/harness.py judge [<target>|--self] [--base <ref> | --files <목록 파일|->] [--json]
                                             # 변경 파일로 경량·표준·엄격(lite·standard·strict) 판정
    python bin/harness.py judge [<target>|--self] --eval <cases.jsonl> [--json]
                                             # eval 사례의 정답과 판정을 비교(일치율·혼동 행렬, 불일치면 종료 1)
    python bin/harness.py events summary [<target>|--self] [--branch <name>]
                                             # 현재 브랜치의 hooks 기록을 MR 본문 `## hooks 기록` 절로 출력
    python bin/harness.py sessions summary [<target>|--self] [--branch <name>] [--base <ref>]
                                             # 현재 브랜치의 세션 수·응답·작업 시간·종료 검증·기록 없는 커밋
    python bin/harness.py agent summary [<target>|--self] [--branch <name>] [--base <ref>]
                                             # 같은 내용을 MR 본문 `## 에이전트 기록` 절로 출력(도구·토큰·추정 비용)
    python bin/harness.py dashboard [<target>|--self] [--port 8765] [--once]
                                             # 로컬 실시간 대시보드(127.0.0.1만): 진행 중 세션·토큰·추정 비용·hooks 개입
    python bin/harness.py review summary <리뷰 파일>
                                             # 리뷰 7장(처리) 표 → `## 리뷰 결과`에 붙일 발견·채택·중복 두 줄
    python bin/harness.py classify-ci --jobs <jobs.json> [--trace-dir <dir>] [--out <file.jsonl>]
                                             # 실패한 CI job의 원인 범주를 JSONL로 산출(trace는 <job_id>.log)
    python bin/harness.py metrics collect --platform gitlab|github --since <YYYY-MM-DD> [--until <YYYY-MM-DD>]
                               [--out metrics.json] [--api-url ..] [--project ..] [--utc-offset +09:00] ..
                                             # 플랫폼 API에서 병합 MR·결함 이슈·실패 job을 읽어 주별 지표 JSON 생성
                                             # (토큰은 HARNESS_METRICS_TOKEN, GitHub는 없으면 GITHUB_TOKEN)
    python bin/harness.py metrics report [--in metrics.json] [--out report.html] [--title ..]
                                             # 수집 JSON으로 5개 지표의 주별 추세·이번 주 요약·원자료를 담은 정적 HTML 생성(오프라인)
    python bin/harness.py metrics notify [--in metrics.json] [--mode webhook|mattermost|auto] [--report-url ..] [--dry-run]
                                             # 마지막 주 5개 지표 요약을 incoming webhook(HARNESS_NOTIFY_WEBHOOK_URL) 또는
                                             # Mattermost 계정 로그인(HARNESS_MATTERMOST_*)으로 발송. --dry-run은 본문만 출력
    python bin/harness.py metrics cases [--in metrics.json] [--out cases.jsonl]
                                             # 수집 JSON의 병합 MR을 judge --eval 사례 후보로 산출(불일치 MR은 정답 미확정)
    python bin/harness.py lint-plans [<key>] [--target <dir> | --self]
                                             # plans/의 플랜·리뷰 파일 검사(실패 1, 경고만이면 0)
    python bin/harness.py version
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

VERSION = "0.2.0"
KIT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = KIT_ROOT / "core" / "templates"
HOOKS_DIR = KIT_ROOT / "core" / "hooks"
CI_DIR = KIT_ROOT / "core" / "ci"
METRICS_DIR = KIT_ROOT / "core" / "metrics"
# 매니페스트 항목의 base가 가리키는 원본 디렉터리
SOURCE_DIRS = {"templates": TEMPLATES_DIR, "hooks": HOOKS_DIR, "ci": CI_DIR, "metrics": METRICS_DIR}
MANIFEST_PATH = TEMPLATES_DIR / "manifest.json"
AREA_TEMPLATE = "AREA-AGENTS.md"
AREA_PROFILE_KEYS = {"profile", "vars", "layers", "allow", "disabled_rules"}
AREA_KEYS = {"dir", "verify", "triggers", "review_focus", "docs", "trigger_paths", "test_paths", *AREA_PROFILE_KEYS}
CONFIG_KEYS = {"harness_version", "project_name", "platform", "tracker", "issue_prefix",
               "default_branch", "integration_branch", "related_docs", "areas", "hooks", "judge", "pricing"}
RELATED_DOC_KEYS = {"label", "path"}
CONFIG_NAME = "harness.json"

# 프로필(ADR-0004): 스택 종속 기본값 묶음. core는 프로필 이름을 모르고 profile.json 스키마만 안다
PROFILES_DIR = KIT_ROOT / "profiles"
PROFILE_FILE = "profile.json"
PROFILE_KEYS = {"name", "description", "verify", "trigger_paths", "test_paths", "triggers", "review_focus",
                "vars", "scaffold", "conventions", "layers", "allow", "setup_notes"}
PROFILE_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
VAR_NAME = re.compile(r"[a-z][a-z_]*")
VAR_KEYS = {"description", "pattern", "default"}
LAYER_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
RULE_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
SCAFFOLD_KIND = re.compile(r"[a-z][a-z0-9-]*")
SCAFFOLD_NAME = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)*")
CONVENTION_KINDS = ("fixed", "configured")
FORMAT_KEYS = {"layer", "layer_empty", "pattern", "pattern_separator", "allowed", "allowed_separator", "separator"}
NAME_VARIANTS = ("name", "name_pascal", "name_camel", "name_kebab", "name_snake", "name_lower")
LAYER_JSON_NAMES = ("layers_json", "allow_json")
# 프로필 verify·setup_notes 렌더에만 주는 값. 영역 검증 명령은 저장소 루트에서 실행되므로(stop-verify) 영역 경로가 필요하다
COMMAND_NAMES = ("area_dir",)
# 셸 명령에 따옴표 없이 들어가므로 sh·cmd 어느 쪽에서도 한 낱말인 문자만 받는다
# 첫 글자가 `-`면 `cd -x`처럼 옵션으로 읽히므로 막는다
COMMAND_SAFE_PATH = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._/-]*")
# 고정 규칙 블록 표식. 각 언어의 주석 안에 쓰고, 끈 규칙은 표식 줄과 함께 지운다
RULE_START = re.compile(r"harness:rule\s+(\S+)")
RULE_END = re.compile(r"harness:end\b")
# 계층·고정 규칙 설정은 컨벤션 테스트를 바꾸거나 끈다. 프로필 영역의 엄격 트리거에 항상 넣는다(P-12)
PROFILE_TRIGGER = "harness.json 이 영역 설정의 layers·allow·disabled_rules 변경(컨벤션 규칙을 바꾸거나 끈다)"
# harness.json은 저장소 루트라 영역 트리거로는 판정에 나오지 않는다. 최상위 judge.triggers에도 넣어 사람 확인으로 낸다
PROFILE_ROOT_TRIGGER = "harness.json 의 프로필 영역 설정(layers·allow·disabled_rules) 변경이면 엄격(컨벤션 규칙을 바꾸거나 끈다)"

PLATFORMS = {
    "gitlab": {
        "platform": "GitLab",
        "pr_noun": "MR",
        "pr_long": "Merge Request",
        "ci_variables": "GitLab CI/CD Variables",
    },
    "github": {
        "platform": "GitHub",
        "pr_noun": "PR",
        "pr_long": "Pull Request",
        "ci_variables": "GitHub Actions Secrets",
    },
}

TRACKERS = {
    "jira": {
        "tracker_name": "Jira",
        "issue_noun": "Jira 업무 항목",
        "issue_key": "Jira Issue Key",
    },
    "github": {
        "tracker_name": "GitHub Issues",
        "issue_noun": "GitHub 이슈",
        "issue_key": "이슈 번호",
    },
}

# 루트 init 인자의 기본값. argparse 기본값을 None으로 두어 사용자가 직접 준 인자를 구분한다.
ROOT_DEFAULTS = {
    "platform": "gitlab",
    "tracker": "jira",
    "default_branch": "main",
    "integration_branch": "develop",
}
ROOT_ONLY_FLAGS = ("config", "project_name", "issue_prefix", *ROOT_DEFAULTS)

CONSUMER_RELATED_DOCS = [
    {"label": "문서 역할과 우선순위", "path": "docs/README.md"},
    {"label": "AI 병렬 작업 및 충돌 방지 규칙", "path": "docs/ai-collaboration.md"},
    {"label": "Git 컨벤션(브랜치·커밋·병합 요청)", "path": "docs/git-convention.md"},
    {"label": "개발 흐름(업무 항목 시작부터 병합까지)", "path": "docs/development-workflow.md"},
    {"label": "Secret 및 환경변수 관리 규칙", "path": "docs/secret-environment-variables.md"},
]


DEFAULT_REVIEW_FOCUS = [
    "계약 정합성",
    "정확성(경계 조건, 실패 경로)",
    "보안(입력 검증, Secret)",
    "테스트 실효성(플랜 목록과 실제 테스트의 대조)",
]

# 존재를 확인할 수 없는 문서를 기본값으로 적지 않는다. 렌더링은 설정만으로 정해져야 check가 안정적이다.
NO_AREA_DOCS = "- 아직 지정한 기준 문서가 없다. 계약·스키마·컨벤션 문서가 생기면 `--area-doc`으로 추가한다."

# hooks 설정 검증은 대상에 복사되는 hook 공통 코드와 같은 규칙을 쓴다
_HOOKS_SPEC = importlib.util.spec_from_file_location("harness_hooks_common", HOOKS_DIR / "harness_common.py")
hooks_common = importlib.util.module_from_spec(_HOOKS_SPEC)
_HOOKS_SPEC.loader.exec_module(hooks_common)

# 영역 AGENTS.md의 기본 트리거. 판정의 사람 확인 목록과 같은 값이라 hook 공통 코드에 둔다
DEFAULT_TRIGGERS = list(hooks_common.DEFAULT_AREA_TRIGGERS)

# CI 실패 분류는 M4-1 수집기도 쓰는 core/metrics 모듈에 둔다
_CLASSIFY_SPEC = importlib.util.spec_from_file_location("harness_classify_ci", METRICS_DIR / "classify_ci.py")
classify_ci = importlib.util.module_from_spec(_CLASSIFY_SPEC)
_CLASSIFY_SPEC.loader.exec_module(classify_ci)

# 측정 수집기(M4-1). 소비자 CI에서도 단독 실행하는 스크립트라 명령은 감싸기만 한다
_COLLECT_SPEC = importlib.util.spec_from_file_location("harness_metrics_collect", METRICS_DIR / "collect.py")
metrics_collect = importlib.util.module_from_spec(_COLLECT_SPEC)
_COLLECT_SPEC.loader.exec_module(metrics_collect)

# 주간 HTML 리포트(M4-6). 수집기처럼 소비자 CI에서 단독 실행하는 스크립트라 명령은 감싸기만 한다
_REPORT_SPEC = importlib.util.spec_from_file_location("harness_metrics_report", METRICS_DIR / "report.py")
metrics_report = importlib.util.module_from_spec(_REPORT_SPEC)
_REPORT_SPEC.loader.exec_module(metrics_report)

# 주간 요약 발송(M4-2). 소비자 CI 예약 파이프라인에서 단독 실행하는 스크립트라 명령은 감싸기만 한다
_NOTIFY_SPEC = importlib.util.spec_from_file_location("harness_metrics_notify", METRICS_DIR / "notify.py")
metrics_notify = importlib.util.module_from_spec(_NOTIFY_SPEC)
_NOTIFY_SPEC.loader.exec_module(metrics_notify)

# `.gitignore`의 `/plans/` 판단은 CI의 MR 본문 lint(대상에 복사되는 모듈)와 같은 규칙을 쓴다
_MR_LINT_SPEC = importlib.util.spec_from_file_location("harness_mr_lint", CI_DIR / "mr-lint" / "mr_lint.py")
mr_lint = importlib.util.module_from_spec(_MR_LINT_SPEC)
_MR_LINT_SPEC.loader.exec_module(mr_lint)

PLACEHOLDER = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
PLACEHOLDER_NAME = re.compile(r"[a-z_]+")
# 브랜치 모델. integration_branch가 default_branch와 같으면 통합 브랜치 없이 main에 직접 병합하는 단일 브랜치다
TWO_BRANCH, TRUNK = "two-branch", "trunk"
BRANCH_MODES = (TWO_BRANCH, TRUNK)


class HarnessError(Exception):
    """사용자에게 그대로 보여 줄 오류."""


# ---------------------------------------------------------------------------
# 설정과 컨텍스트
# ---------------------------------------------------------------------------


def load_config(path: Path) -> dict:
    if not path.is_file():
        raise HarnessError(f"설정 파일이 없다: {path}")
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HarnessError(f"설정 파일 JSON 오류: {path}: {exc}") from exc
    validate_config(config, path)
    return config


def validate_config(config: dict, source: Path | str) -> None:
    if not isinstance(config, dict):
        raise HarnessError(f"{source}: 최상위 설정은 객체여야 한다")
    unknown = sorted(set(config) - CONFIG_KEYS)
    if unknown:
        # 제거한 기능의 키는 무엇을 지울지 알려 준다
        hint = (" (Claude MR 리뷰는 키트에서 제거됐다(#43). 이 키와 `.harness/claude-review/`, CI의 claude-review include를 지운다)"
                if "claude_review" in unknown else "")
        raise HarnessError(f"{source}: 알 수 없는 설정 키: {', '.join(unknown)}{hint}")
    required = ["project_name", "platform", "tracker", "default_branch", "integration_branch"]
    missing = [key for key in required if not isinstance(config.get(key), str) or not config[key].strip()]
    if missing:
        raise HarnessError(f"{source}: 필수 항목이 비어 있다: {', '.join(missing)}")
    for key in ("harness_version", "issue_prefix"):
        if key in config and not isinstance(config[key], str):
            raise HarnessError(f"{source}: {key}는 문자열이어야 한다")
    if "pricing" in config:  # 세션 추정 비용 단가(#97)
        errors = metrics_collect.validate_pricing(config["pricing"], source)
        if errors:
            raise HarnessError(errors[0])
    if config["platform"] not in PLATFORMS:
        raise HarnessError(f"{source}: platform은 {sorted(PLATFORMS)} 중 하나여야 한다")
    if config["tracker"] not in TRACKERS:
        raise HarnessError(f"{source}: tracker는 {sorted(TRACKERS)} 중 하나여야 한다")
    if config["tracker"] == "jira" and not config.get("issue_prefix", "").strip():
        raise HarnessError(f"{source}: tracker가 jira이면 issue_prefix(예: ABC123)가 필요하다")
    docs = config.get("related_docs", [])
    if not isinstance(docs, list) or any(
        not isinstance(d, dict) or set(d) != RELATED_DOC_KEYS or
        any(not isinstance(d[key], str) or not d[key].strip() for key in RELATED_DOC_KEYS)
        for d in docs
    ):
        raise HarnessError(f"{source}: related_docs는 label·path를 가진 객체 목록이어야 한다")
    validate_areas(config.get("areas", []), source)
    try:
        if "hooks" in config:  # 키를 생략하면 기본값, 명시한 null은 객체 계약 위반이다
            hooks_common.validate_hooks(config["hooks"], source)
        if "judge" in config:
            hooks_common.validate_judge_root(config["judge"], source)
    except hooks_common.ConfigError as exc:
        raise HarnessError(str(exc)) from exc


def normalize_area_dir(value: str, source: Path | str) -> str:
    """대상 저장소 안의 상대 경로만 허용하고 `a/b` 형태로 정규화한다."""
    raw = value.strip().replace("\\", "/") if isinstance(value, str) else ""
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not raw or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise HarnessError(f"{source}: 영역 dir은 대상 저장소 안의 상대 경로여야 한다: {value!r}")
    if not parts or ".." in parts:
        raise HarnessError(f"{source}: 영역 dir은 대상 저장소 안의 상대 경로여야 한다: {value!r}")
    return "/".join(parts)


def validate_areas(areas, source: Path | str) -> None:
    if not isinstance(areas, list):
        raise HarnessError(f"{source}: areas는 목록이어야 한다")
    seen = set()
    for area in areas:
        if not isinstance(area, dict) or "dir" not in area:
            raise HarnessError(f"{source}: areas 항목에는 dir이 필요하다")
        dir_ = normalize_area_dir(area["dir"], source)
        if dir_ != area["dir"]:
            raise HarnessError(f"{source}: 영역 dir은 정규화된 형태여야 한다: {area['dir']!r} → {dir_!r}")
        if dir_ in seen:
            raise HarnessError(f"{source}: 영역 dir이 중복된다: {dir_}")
        seen.add(dir_)
        unknown = sorted(set(area) - AREA_KEYS)
        if unknown:  # 오타 난 키가 조용히 무시되고 기본값이 적용되는 것을 막는다
            raise HarnessError(f"{source}: 영역 {dir_}에 알 수 없는 키가 있다: {', '.join(unknown)}")
        for key, required in (("verify", True), ("triggers", False),
                              ("review_focus", False), ("docs", False)):
            if key not in area and not required:
                continue
            items = area.get(key)
            if not isinstance(items, list) or not items or any(
                not isinstance(item, str) or not item.strip() for item in items
            ):
                raise HarnessError(f"{source}: 영역 {dir_}의 {key}는 비어 있지 않은 문자열 목록이어야 한다")
        try:
            hooks_common.validate_judge_rules(area, source)
        except hooks_common.ConfigError as exc:
            raise HarnessError(str(exc)) from exc
        validate_area_profile(area, f"{source}: 영역 {dir_}")


def validate_area_profile(area: dict, source: str) -> None:
    """영역의 프로필 키 형식. 프로필 내용과의 대조(변수·규칙 ID)는 렌더할 때 한다."""
    present = sorted(key for key in AREA_PROFILE_KEYS - {"profile"} if key in area)
    if "profile" not in area:
        if present:
            raise HarnessError(f"{source}: {', '.join(present)}는 profile과 함께 쓴다")
        return
    if not isinstance(area["profile"], str) or not PROFILE_NAME.fullmatch(area["profile"]):
        raise HarnessError(f"{source}: profile 이름이 잘못됐다: {area['profile']!r}")
    values = area.get("vars", {})
    if not isinstance(values, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                           for k, v in values.items()):
        raise HarnessError(f"{source}: vars는 문자열 값의 객체여야 한다")
    validate_layers(area.get("layers", {}), area.get("allow", {}), source)
    disabled = area.get("disabled_rules", {})
    if not isinstance(disabled, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) or not v.strip() for k, v in disabled.items()):
        raise HarnessError(f"{source}: disabled_rules는 규칙 ID → 끈 이유(비어 있지 않은 문자열) 객체여야 한다")


def validate_layers(layers, allow, source: str) -> None:
    if not isinstance(layers, dict):
        raise HarnessError(f"{source}: layers는 계층 이름 → 패턴 목록 객체여야 한다")
    for name, patterns in layers.items():
        if not LAYER_NAME.fullmatch(name):
            raise HarnessError(f"{source}: 계층 이름이 잘못됐다: {name!r}")
        if not _str_list(patterns):
            raise HarnessError(f"{source}: 계층 {name}의 패턴은 비어 있지 않은 문자열 목록이어야 한다")
    if not isinstance(allow, dict):
        raise HarnessError(f"{source}: allow는 계층 이름 → 의존 가능한 계층 목록 객체여야 한다")
    for name, targets in allow.items():
        if name not in layers:
            raise HarnessError(f"{source}: allow의 계층이 layers에 없다: {name}")
        if not _str_list(targets, allow_empty=True) or any(t not in layers for t in targets):
            raise HarnessError(f"{source}: allow.{name}은 layers에 있는 계층 이름 목록이어야 한다")


def _str_list(value, allow_empty: bool = False) -> bool:
    return (isinstance(value, list) and (allow_empty or bool(value)) and
            all(isinstance(item, str) and item.strip() for item in value))


def build_context(config: dict) -> dict:
    ctx = {
        "project_name": config["project_name"],
        "default_branch": config["default_branch"],
        "integration_branch": config["integration_branch"],
    }
    ctx.update(PLATFORMS[config["platform"]])
    ctx.update(TRACKERS[config["tracker"]])
    prefix = config.get("issue_prefix", "")
    if config["tracker"] == "jira":
        ctx["issue_key_example"] = f"{prefix}-52"
        ctx["branch_key_example"] = f"{prefix}-52"
    else:
        ctx["issue_key_example"] = "#52"
        ctx["branch_key_example"] = "52"
    docs = config.get("related_docs") or []
    ctx["related_docs"] = "\n".join(f"- [{d['label']}]({d['path']})" for d in docs)
    ctx["hook_python"] = (config.get("hooks") or {}).get("python", hooks_common.DEFAULT_PYTHON)
    mode = branch_mode(config)
    for include in load_includes():  # 다른 파일의 절을 자리표시자 값으로 넣는다. 포함 내용도 같은 컨텍스트로 렌더한다
        if include.get("when", mode) != mode:  # 브랜치 모델별 변형은 현재 모델 것만 넣는다
            continue
        if include["name"] in ctx:
            raise HarnessError(f"매니페스트 include 이름이 기존 자리표시자와 겹친다: {include['name']}")
        ctx[include["name"]] = render(include_text(include), ctx, source=f"{include['src']}#{include['name']}")
    return ctx


def branch_mode(config: dict) -> str:
    return TRUNK if config["integration_branch"] == config["default_branch"] else TWO_BRANCH


def area_context(area: dict) -> dict:
    dir_ = area["dir"]
    docs = area.get("docs")
    triggers = area.get("triggers") or DEFAULT_TRIGGERS
    focus = area.get("review_focus") or DEFAULT_REVIEW_FOCUS
    return {
        "area_dir": dir_,
        "area_docs": "\n".join(f"- `{doc}`" for doc in docs) if docs else NO_AREA_DOCS,
        "area_verify": "\n".join(f"- `{cmd}`" for cmd in area["verify"]),
        "area_triggers": "\n".join(f"{i}. {item}" for i, item in enumerate(triggers, 1)),
        "area_review_focus": " · ".join(focus),
    }


def render_area(area: dict, ctx: dict) -> str:
    path = TEMPLATES_DIR / AREA_TEMPLATE
    if not path.is_file():
        raise HarnessError(f"템플릿 파일이 없다: {path}")
    return render(path.read_text(encoding="utf-8"), {**ctx, **area_context(area)}, source=AREA_TEMPLATE)


def render(text: str, ctx: dict, source: str = "<template>") -> str:
    unknown: list[str] = []

    def substitute(match: re.Match) -> str:
        key = match.group(1)
        if key not in ctx:
            unknown.append(key)
            return match.group(0)
        return ctx[key]

    rendered = PLACEHOLDER.sub(substitute, text)
    if unknown:
        raise HarnessError(f"{source}: 알 수 없는 자리표시자: {', '.join(sorted(set(unknown)))}")
    unmatched = PLACEHOLDER.sub("", text)
    if "{{" in unmatched or "}}" in unmatched:
        raise HarnessError(f"{source}: 잘못된 자리표시자 형식")
    return rendered


# ---------------------------------------------------------------------------
# 매니페스트와 렌더링 계획
# ---------------------------------------------------------------------------


def read_manifest() -> dict:
    if not MANIFEST_PATH.is_file():
        raise HarnessError(f"매니페스트가 없다: {MANIFEST_PATH}")
    try:
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError(f"매니페스트를 읽을 수 없다: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        raise HarnessError("매니페스트 files는 목록이어야 한다")
    return data


def valid_template_path(value) -> bool:
    return (isinstance(value, str) and bool(value) and "\\" not in value and not value.startswith("/") and
            all(part not in ("", ".", "..") for part in value.split("/")) and not re.match(r"^[A-Za-z]:", value))


def load_includes() -> list[dict]:
    """매니페스트 includes: 원본 파일(templates 기준)의 `## 절`을 골라 자리표시자 값으로 쓴다."""
    includes = read_manifest().get("includes", [])
    if not isinstance(includes, list):
        raise HarnessError("매니페스트 includes는 목록이어야 한다")
    variants: dict[str, set] = {}  # 이름 → {"*"} 또는 브랜치 모델 집합
    for entry in includes:
        if not isinstance(entry, dict) or not {"name", "src", "sections"} <= set(entry) <= {"name", "src", "sections", "tag", "when"}:
            raise HarnessError("매니페스트 include 항목은 name·src·sections(선택 tag·when)를 가져야 한다")
        if "when" in entry and entry["when"] not in BRANCH_MODES:
            raise HarnessError(f"매니페스트 include when은 {'|'.join(BRANCH_MODES)} 중 하나여야 한다: {entry['when']!r}")
        if "tag" in entry and (not isinstance(entry["tag"], str) or not re.fullmatch(r"[^\[\]\s]+", entry["tag"])):
            raise HarnessError(f"매니페스트 include tag가 잘못됐다: {entry['tag']!r}")
        name = entry["name"]
        if not isinstance(name, str) or not PLACEHOLDER_NAME.fullmatch(name):
            raise HarnessError(f"매니페스트 include 이름은 소문자와 밑줄만 쓴다: {name!r}")
        if name.startswith("area_"):  # 영역 자리표시자 이름공간. 영역 문서에서 조용히 가려지는 것을 막는다
            raise HarnessError(f"매니페스트 include 이름은 area_로 시작할 수 없다: {name}")
        key = entry.get("when", "*")
        modes = variants.setdefault(name, set())
        if key in modes or "*" in modes or (key == "*" and modes):
            raise HarnessError(f"매니페스트 include 이름이 중복된다: {name}")
        modes.add(key)
        if not valid_template_path(entry["src"]):
            raise HarnessError(f"매니페스트 include src 경로가 잘못됐다: {entry['src']!r}")
        sections = entry["sections"]
        if not isinstance(sections, list) or not sections or any(
                not isinstance(s, str) or not s.strip() for s in sections):
            raise HarnessError(f"매니페스트 include {name}의 sections는 비어 있지 않은 문자열 목록이어야 한다")
    for name, modes in variants.items():  # 모델별 변형은 어느 모델에서도 자리표시자가 비지 않게 쌍으로 둔다
        if "*" not in modes and modes != set(BRANCH_MODES):
            raise HarnessError(f"매니페스트 include {name}은 when 변형을 {'·'.join(BRANCH_MODES)} 둘 다 둬야 한다")
    return includes


def markdown_sections(text: str) -> dict[str, str]:
    """`## 제목` 단위 본문. 첫 `##` 앞의 설명은 버린다."""
    sections: dict[str, list[str]] = {}
    current = None
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.startswith("## "):
            current = line[3:].strip()
            if current in sections:
                raise HarnessError(f"절 제목이 중복된다: {current}")
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return {name: "\n".join(lines).strip("\n") for name, lines in sections.items()}


def include_text(include: dict) -> str:
    path = TEMPLATES_DIR / include["src"]
    if not path.is_file():
        raise HarnessError(f"include 원본이 없다: {path}")
    sections = markdown_sections(path.read_text(encoding="utf-8"))
    missing = [s for s in include["sections"] if s not in sections]
    if missing:
        raise HarnessError(f"{include['src']}에 절이 없다: {', '.join(missing)}")
    text = "\n".join(sections[s] for s in include["sections"])  # 절 본문은 목록이라 한 목록으로 잇는다
    if "tag" in include:
        text = "\n".join(f"{line} [{include['tag']}]" if line.startswith("- ") else line
                         for line in text.split("\n"))
    return text


def load_manifest() -> list[dict]:
    data = read_manifest()
    seen = set()
    for entry in data["files"]:
        if not isinstance(entry, dict) or not {"src", "dest"} <= set(entry) or set(entry) - {"src", "dest", "base", "platform", "self", "render", "seed"}:
            raise HarnessError("매니페스트 항목의 키가 잘못됐다")
        base = entry.get("base", "templates")
        if not isinstance(base, str) or base not in SOURCE_DIRS:
            raise HarnessError(f"매니페스트 base는 {sorted(SOURCE_DIRS)} 중 하나여야 한다: {base}")
        for key in ("src", "dest"):
            value = entry[key]
            if not valid_template_path(value):
                raise HarnessError(f"매니페스트 {key} 경로가 잘못됐다: {value!r}")
        if entry["dest"] in seen:
            raise HarnessError(f"매니페스트 목적지가 중복된다: {entry['dest']}")
        seen.add(entry["dest"])
        if ("platform" in entry and (not isinstance(entry["platform"], str) or entry["platform"] not in PLATFORMS) or
                any(key in entry and not isinstance(entry[key], bool) for key in ("self", "render", "seed"))):
            raise HarnessError(f"매니페스트 선택 값이 잘못됐다: {entry['dest']}")
    return data["files"]


def planned_files(config: dict, self_mode: bool) -> list[tuple[Path, str, bool]]:
    """(원본 경로, 대상 상대 경로, 자리표시자 치환 여부) 목록."""
    plan = []
    for entry in load_manifest():
        wanted_platform = entry.get("platform")
        if wanted_platform and wanted_platform != config["platform"]:
            continue
        if self_mode and not entry.get("self", False):
            continue
        base = entry.get("base", "templates")
        if base not in SOURCE_DIRS:
            raise HarnessError(f"매니페스트 base는 {sorted(SOURCE_DIRS)} 중 하나여야 한다: {base}")
        plan.append((SOURCE_DIRS[base] / entry["src"], entry["dest"], entry.get("render", True)))
    return plan


def seed_dests(config: dict, self_mode: bool) -> set[str]:
    """소비자 소유 시드 파일(seed=true)의 대상 경로. 처음 한 번만 생성하고 그 뒤는 덮어쓰지도 비교하지도 않는다."""
    return {entry["dest"] for entry in load_manifest()
            if entry.get("seed", False)
            and not (entry.get("platform") and entry["platform"] != config["platform"])
            and not (self_mode and not entry.get("self", False))}


def render_all(config: dict, self_mode: bool) -> dict[str, str]:
    ctx = build_context(config)
    outputs: dict[str, str] = {}
    for path, dest, rendered in planned_files(config, self_mode):
        if not path.is_file():
            raise HarnessError(f"템플릿 파일이 없다: {path}")
        text = path.read_text(encoding="utf-8")
        outputs[dest] = render(text, ctx, source=path.name) if rendered else text
    for area in config.get("areas", []):
        outputs[f"{area['dir']}/AGENTS.md"] = render_area(area, ctx)
    for area in config.get("areas", []):
        for dest, content in render_conventions(area).items():
            if dest in outputs:
                raise HarnessError(f"영역 {area['dir']}: 컨벤션 파일 목적지가 다른 생성 파일과 겹친다: {dest}")
            outputs[dest] = content
    return outputs


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


SETTINGS_LOCAL_IGNORE = "/.claude/settings.local.json"


def has_settings_local(text: str) -> bool:
    return any(line.strip() in (SETTINGS_LOCAL_IGNORE, SETTINGS_LOCAL_IGNORE[1:]) for line in text.splitlines())


def ensure_ignored(target: Path) -> list[str]:
    """`.gitignore`에 개인 Claude Code 설정과 `/plans/` 줄이 없으면 끝에 추가한다(파일이 없으면 만든다).
    기존 내용은 그대로 둔다. 추가한 줄 목록."""
    path = target / ".gitignore"
    data = path.read_bytes() if path.is_file() else b""
    text = data.decode("utf-8", errors="replace")
    entries = [
        (SETTINGS_LOCAL_IGNORE, "# 개인 Claude Code 설정은 커밋하지 않는다(권한·hooks 끄기 등)", has_settings_local(text)),
        (mr_lint.PLANS_IGNORE, "# 플랜·리뷰 파일은 커밋하지 않는다(AGENTS.md, MR 본문 lint가 검사한다)",
         mr_lint.gitignore_has_plans(text)),
    ]
    missing = [(line, comment) for line, comment, present in entries if not present]
    if not missing:
        return []
    newline = b"\r\n" if b"\r\n" in data else b"\n"
    chunk = newline if data and not data.endswith(b"\n") else b""
    for line, comment in missing:
        chunk += comment.encode("utf-8") + newline + line.encode("ascii") + newline
    with path.open("ab") as handle:
        handle.write(chunk)
    return [line for line, _ in missing]


# ---------------------------------------------------------------------------
# 프로필 (M3-1). 프로필 코드는 실행하지 않는다: profile.json 데이터와 템플릿만 읽는다
# ---------------------------------------------------------------------------


def load_profile(name: str) -> tuple[dict, Path]:
    """(profile.json 내용, 프로필 디렉터리). 스키마와 템플릿 존재·자리표시자를 미리 검사한다."""
    if not isinstance(name, str) or not PROFILE_NAME.fullmatch(name):
        raise HarnessError(f"프로필 이름이 잘못됐다: {name!r}")
    base = PROFILES_DIR / name
    path = base / PROFILE_FILE
    if not path.is_file():
        available = sorted(p.name for p in PROFILES_DIR.iterdir() if (p / PROFILE_FILE).is_file()) \
            if PROFILES_DIR.is_dir() else []
        raise HarnessError(f"프로필이 없다: {name} (사용 가능: {', '.join(available) or '없음'})")
    source = f"{name}/{PROFILE_FILE}"
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError(f"{source}: 읽을 수 없다: {exc}") from exc
    validate_profile(profile, base, source)
    return profile, base


def validate_profile(profile, base: Path, source: str) -> None:
    if not isinstance(profile, dict):
        raise HarnessError(f"{source}: 최상위는 객체여야 한다")
    unknown = sorted(set(profile) - PROFILE_KEYS)
    if unknown:
        raise HarnessError(f"{source}: 알 수 없는 키: {', '.join(unknown)}")
    if profile.get("name") != base.name:
        raise HarnessError(f"{source}: name은 디렉터리 이름 {base.name!r}과 같아야 한다")
    if not isinstance(profile.get("description"), str) or not profile["description"].strip():
        raise HarnessError(f"{source}: description이 필요하다")
    for key in ("verify", "triggers", "review_focus", "setup_notes"):
        if key in profile and not _str_list(profile[key]):
            raise HarnessError(f"{source}: {key}는 비어 있지 않은 문자열 목록이어야 한다")
    try:
        hooks_common.validate_judge_rules({"dir": base.name, **{k: profile[k] for k in ("trigger_paths", "test_paths")
                                                                if k in profile}}, source)
    except hooks_common.ConfigError as exc:
        raise HarnessError(str(exc)) from exc
    variables = profile.get("vars", {})
    validate_var_spec(variables, source)
    validate_layers(profile.get("layers", {}), profile.get("allow", {}), source)
    dummy = var_context({name: "x" for name in variables})
    for key in ("verify", "setup_notes"):
        for text in profile.get(key, []):
            render(text, {**dummy, **{name: "x" for name in COMMAND_NAMES}}, source=f"{source} {key}")
    validate_scaffold(profile.get("scaffold", {}), variables, base, source)
    validate_conventions(profile.get("conventions"), dummy, base, source)


def validate_var_spec(spec, source: str, reserved: set[str] = frozenset()) -> None:
    """`reserved`는 앞서 선언된 변수(종류 변수에서 프로필 변수). 기본값은 그 변수와 앞의 변수만 쓸 수 있다."""
    if not isinstance(spec, dict):
        raise HarnessError(f"{source}: vars는 객체여야 한다")
    known = set(reserved)
    for name, item in spec.items():
        if (not VAR_NAME.fullmatch(name) or name.endswith("_path") or name.startswith("name") or
                name in LAYER_JSON_NAMES or name in COMMAND_NAMES or name in reserved):
            # `<이름>_path`·이름 변형·계층 JSON·명령용 영역 경로는 엔진이 만드는 값이라 변수 이름으로 쓸 수 없다
            raise HarnessError(f"{source}: 변수 이름이 잘못됐거나 예약어다: {name!r}")
        if not isinstance(item, dict) or set(item) - VAR_KEYS or not isinstance(item.get("description"), str):
            raise HarnessError(f"{source}: 변수 {name}은 description(필수)·pattern·default만 가진다")
        if "default" in item and not isinstance(item["default"], str):
            raise HarnessError(f"{source}: 변수 {name}의 default는 문자열이어야 한다")
        if "default" in item:  # 뒤에 선언된 변수를 쓰면 init에서야 실패하므로 여기서 잡는다
            render(item["default"], var_context({n: "x" for n in known}), source=f"{source} 변수 {name} 기본값")
        known.add(name)
        if "pattern" in item:
            try:
                re.compile(item["pattern"])
            except (re.error, TypeError) as exc:
                raise HarnessError(f"{source}: 변수 {name}의 pattern이 잘못됐다: {exc}") from exc


def profile_file(base: Path, rel, source: str, what: str) -> Path:
    if not valid_template_path(rel):
        raise HarnessError(f"{source}: {what} 경로가 잘못됐다: {rel!r}")
    path = base / rel
    if not path.is_file():
        raise HarnessError(f"{source}: {what} 파일이 없다: {rel}")
    return path


def validate_scaffold(scaffold, variables: dict, base: Path, source: str) -> None:
    if not isinstance(scaffold, dict):
        raise HarnessError(f"{source}: scaffold는 종류 → 정의 객체여야 한다")
    for kind, spec in scaffold.items():
        where = f"{source} scaffold.{kind}"
        if not SCAFFOLD_KIND.fullmatch(kind):
            raise HarnessError(f"{source}: 스캐폴드 종류 이름이 잘못됐다: {kind!r}")
        if not isinstance(spec, dict) or set(spec) - {"description", "files", "snippet", "vars"} or \
                not isinstance(spec.get("description"), str):
            raise HarnessError(f"{where}: description·files·snippet·vars만 가진다(description 필수)")
        kind_vars = spec.get("vars", {})
        validate_var_spec(kind_vars, where, reserved=set(variables))
        dummy = {**var_context({name: "x" for name in {**variables, **kind_vars}}), **name_variants("Demo")}
        files = spec.get("files")
        if not isinstance(files, list) or not files:
            raise HarnessError(f"{where}: files는 비어 있지 않은 목록이어야 한다")
        for entry in files:
            if not isinstance(entry, dict) or set(entry) != {"src", "dest"}:
                raise HarnessError(f"{where}: files 항목은 src·dest만 가진다")
            path = scaffold_template(base, kind, entry["src"], where)
            render(path.read_text(encoding="utf-8"), dummy, source=f"{base.name}/scaffold/{kind}/{entry['src']}")
            render_dest(entry["dest"], dummy, where)
        if "snippet" in spec:  # 본문 자리표시자도 미리 검사한다(소비자의 첫 scaffold에서 실패하지 않게)
            path = scaffold_template(base, kind, spec["snippet"], where)
            render(path.read_text(encoding="utf-8"), dummy, source=f"{base.name}/scaffold/{kind}/{spec['snippet']}")


def scaffold_template(base: Path, kind: str, rel, source: str) -> Path:
    """스캐폴드 템플릿은 `scaffold/<kind>/` 기준이다. 대상의 덮어쓰기 경로도 같은 상대 경로를 쓴다."""
    if not valid_template_path(rel):
        raise HarnessError(f"{source}: 템플릿 경로가 잘못됐다: {rel!r}")
    return profile_file(base, f"scaffold/{kind}/{rel}", source, "템플릿")


def validate_conventions(conventions, dummy: dict, base: Path, source: str) -> None:
    if conventions is None:
        return
    where = f"{source} conventions"
    if not isinstance(conventions, dict) or set(conventions) - {"rules", "files", "formats"}:
        raise HarnessError(f"{where}: rules·files·formats만 가진다")
    rules = conventions.get("rules", {})
    if not isinstance(rules, dict) or any(not RULE_ID.fullmatch(k) or not isinstance(v, str) or not v.strip()
                                          for k, v in rules.items()):
        raise HarnessError(f"{where}: rules는 규칙 ID → 설명 객체여야 한다")
    formats = conventions.get("formats", {})
    if not isinstance(formats, dict):
        raise HarnessError(f"{where}: formats는 객체여야 한다")
    for name, spec in formats.items():
        if not VAR_NAME.fullmatch(name) or name in dummy or name in LAYER_JSON_NAMES:
            raise HarnessError(f"{where}: 서식 이름이 잘못됐거나 변수·예약어와 겹친다: {name!r}")
        if not isinstance(spec, dict) or "layer" not in spec or set(spec) - FORMAT_KEYS or \
                any(not isinstance(v, str) for v in spec.values()):
            raise HarnessError(f"{where}: 서식 {name}은 layer(필수)와 {', '.join(sorted(FORMAT_KEYS - {'layer'}))}"
                               " 문자열만 가진다")
    # 서식 자리표시자를 미리 검사하고, 그 결과로 configured 본문도 검사한다
    configured = {**dummy, **format_blocks(formats, {"a": ["p"], "b": ["q"]}, {"a": ["b"]}, where),
                  **{name: "{}" for name in LAYER_JSON_NAMES}}
    files = conventions.get("files")
    if not isinstance(files, list) or not files:
        raise HarnessError(f"{where}: files는 비어 있지 않은 목록이어야 한다")
    used: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"src", "dest", "kind"} or \
                entry["kind"] not in CONVENTION_KINDS:
            raise HarnessError(f"{where}: files 항목은 src·dest·kind({'|'.join(CONVENTION_KINDS)})만 가진다")
        path = profile_file(base, entry["src"], where, "컨벤션")
        text, ids = apply_rule_blocks(path.read_text(encoding="utf-8"), rules, {}, f"{base.name}/{entry['src']}")
        render(text, configured if entry["kind"] == "configured" else dummy, source=f"{base.name}/{entry['src']}")
        used |= ids
        render_dest(entry["dest"], dummy, where)
    unused = sorted(set(rules) - used)
    if unused:
        raise HarnessError(f"{where}: 템플릿에 블록이 없는 규칙: {', '.join(unused)}")


def var_context(values: dict) -> dict:
    """변수 값과 파생 값 `<이름>_path`(점을 `/`로. 패키지 → 디렉터리)."""
    return {**values, **{f"{name}_path": value.replace(".", "/") for name, value in values.items()}}


def parse_var_args(items) -> dict:
    values: dict[str, str] = {}
    for item in items or []:
        name, sep, value = item.partition("=")
        if not sep or not VAR_NAME.fullmatch(name):
            raise HarnessError(f"--var 는 이름=값 형식이다: {item!r}")
        if name in values:
            raise HarnessError(f"--var {name} 이 중복된다")
        values[name] = value
    return values


def resolve_vars(spec: dict, given: dict, previous: dict, source: str) -> dict:
    """선언 순서대로 `--var` → 이전 영역 값 → 기본값. 기본값은 앞에서 정한 변수를 쓸 수 있다."""
    unknown = sorted(set(given) - set(spec))
    if unknown:
        raise HarnessError(f"{source}: 선언되지 않은 변수: {', '.join(unknown)} (선언: {', '.join(spec) or '없음'})")
    values: dict[str, str] = {}
    for name, item in spec.items():
        if name in given:
            value = given[name]
        elif name in previous:
            value = previous[name]
        elif "default" in item:
            value = render(item["default"], var_context(values), source=f"{source} 변수 {name} 기본값")
        else:
            raise HarnessError(f"{source}: 필수 변수 {name} 이 없다. --var {name}=… ({item['description']})")
        if "\n" in value or "{{" in value or "}}" in value:
            raise HarnessError(f"{source}: 변수 {name} 값에 줄바꿈이나 자리표시자 괄호를 쓸 수 없다")
        optional_empty = value == "" and item.get("default") == ""
        if "pattern" in item and not optional_empty and not re.fullmatch(item["pattern"], value):
            raise HarnessError(f"{source}: 변수 {name} 값 {value!r} 이 형식 {item['pattern']} 에 맞지 않는다")
        values[name] = value
    return values


def render_dest(pattern, ctx: dict, source: str) -> str:
    """목적지 경로를 렌더하고 빈 구간을 접는다(선택 변수가 빈 값). 영역 밖으로 나가는 경로는 거부한다."""
    if not isinstance(pattern, str):
        raise HarnessError(f"{source}: dest는 문자열이어야 한다")
    rendered = render(pattern, ctx, source=f"{source} dest")
    parts = [part for part in rendered.split("/") if part]
    if ("\\" in rendered or rendered.startswith("/") or re.match(r"^[A-Za-z]:", rendered) or not parts or
            any(part in (".", "..") for part in parts)):
        raise HarnessError(f"{source}: 목적지가 영역 안의 상대 경로가 아니다: {rendered!r}")
    return "/".join(parts)


def name_variants(name: str) -> dict:
    if not isinstance(name, str) or not SCAFFOLD_NAME.fullmatch(name):
        raise HarnessError(f"이름은 영문자로 시작하는 영숫자이고 단어는 - 나 _ 로 나눈다: {name!r}")
    words = [word for chunk in re.split(r"[-_]", name)
             for word in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+", chunk)]
    lower = [word.lower() for word in words]
    pascal = "".join(word[0].upper() + word[1:] for word in words)
    return {
        "name": name,
        "name_pascal": pascal,
        "name_camel": lower[0] + pascal[len(words[0]):],
        "name_kebab": "-".join(lower),
        "name_snake": "_".join(lower),
        "name_lower": "".join(lower),
    }


def format_blocks(formats: dict, layers: dict, allow: dict, source: str) -> dict:
    """계층 서식: 계층마다 서식을 렌더해 잇는다. allow에 없거나 빈 계층은 layer_empty(있으면)를 쓴다."""
    blocks = {}
    for name, spec in formats.items():
        items = []
        for layer, patterns in layers.items():
            targets = allow.get(layer, [])
            values = {
                "layer": layer,
                "patterns": spec.get("pattern_separator", ", ").join(
                    render(spec.get("pattern", "{{pattern}}"), {"pattern": p}, source=f"{source} {name}.pattern")
                    for p in patterns),
                "allowed": spec.get("allowed_separator", ", ").join(
                    render(spec.get("allowed", "{{name}}"), {"name": t}, source=f"{source} {name}.allowed")
                    for t in targets),
            }
            template = spec["layer_empty"] if not targets and "layer_empty" in spec else spec["layer"]
            items.append(render(template, values, source=f"{source} {name}.layer"))
        blocks[name] = spec.get("separator", "\n").join(items)
    return blocks


def apply_rule_blocks(text: str, rules: dict, disabled: dict, source: str) -> tuple[str, set[str]]:
    """`harness:rule <id>` ~ `harness:end` 블록 중 끈 규칙을 표식과 함께 지운다. (결과, 블록이 있는 규칙 ID)."""
    out, current, used = [], None, set()
    for number, line in enumerate(text.split("\n"), 1):
        start, end = RULE_START.search(line), RULE_END.search(line)
        if start:
            if current is not None:
                raise HarnessError(f"{source}:{number}: 규칙 블록 {current} 이 닫히기 전에 새 블록이 열렸다")
            current = start.group(1)
            if current not in rules:
                raise HarnessError(f"{source}:{number}: 선언되지 않은 규칙 ID: {current}")
            used.add(current)
        elif end and current is None:
            raise HarnessError(f"{source}:{number}: 열리지 않은 규칙 블록의 harness:end")
        if current is None or current not in disabled:
            out.append(line)
        if end and not start:
            current = None
    if current is not None:
        raise HarnessError(f"{source}: 규칙 블록 {current} 이 닫히지 않았다")
    return "\n".join(out), used


def render_conventions(area: dict) -> dict[str, str]:
    """프로필 영역의 컨벤션 파일(대상 저장소 기준 경로 → 내용). 설정이 바뀌면 결과도 바뀌어 check가 잡는다."""
    if "profile" not in area:
        return {}
    profile, base = load_profile(area["profile"])
    source = f"영역 {area['dir']}"
    values = resolve_vars(profile.get("vars", {}), {}, area.get("vars", {}), source)
    conventions = profile.get("conventions") or {}
    rules = conventions.get("rules", {})
    disabled = area.get("disabled_rules", {})
    unknown = sorted(set(disabled) - set(rules))
    if unknown:
        raise HarnessError(f"{source}: disabled_rules에 프로필 {area['profile']}에 없는 규칙: {', '.join(unknown)}"
                           f" (규칙: {', '.join(rules) or '없음'})")
    ctx = var_context(values)
    layers, allow = area.get("layers", {}), area.get("allow", {})
    configured = {**ctx, **format_blocks(conventions.get("formats", {}), layers, allow, source),
                  "layers_json": json.dumps(layers, ensure_ascii=False),
                  "allow_json": json.dumps(allow, ensure_ascii=False)}
    outputs: dict[str, str] = {}
    for entry in conventions.get("files", []):
        name = f"{area['profile']}/{entry['src']}"
        text, _used = apply_rule_blocks((base / entry["src"]).read_text(encoding="utf-8"), rules, disabled, name)
        dest = f"{area['dir']}/{render_dest(entry['dest'], ctx, source)}"
        if dest in outputs or dest == f"{area['dir']}/AGENTS.md":
            raise HarnessError(f"{source}: 컨벤션 파일 목적지가 겹친다: {dest}")
        outputs[dest] = render(text, configured if entry["kind"] == "configured" else ctx, source=name)
    return outputs


# ---------------------------------------------------------------------------
# 명령
# ---------------------------------------------------------------------------


def resolve_target(args) -> tuple[Path, bool]:
    if args.self:
        return KIT_ROOT, True
    if not args.target:
        raise HarnessError("대상 디렉터리 또는 --self 가 필요하다")
    return Path(args.target).resolve(), False


def config_from_args(args, target: Path) -> dict:
    if args.config:
        conflicting = [flag for flag in ROOT_ONLY_FLAGS if flag != "config" and getattr(args, flag) is not None]
        if conflicting:
            raise HarnessError(f"--config 와 루트 설정 인자를 함께 쓸 수 없다: {', '.join(conflicting)}")
        return load_config(Path(args.config))
    existing = target / CONFIG_NAME
    if existing.is_file():
        conflicting = [flag for flag in ROOT_ONLY_FLAGS if flag != "config" and getattr(args, flag) is not None]
        if conflicting:
            raise HarnessError(f"기존 {CONFIG_NAME} 과 루트 설정 인자를 함께 쓸 수 없다: {', '.join(conflicting)}")
        return load_config(existing)
    config = {
        "harness_version": VERSION,
        "project_name": args.project_name or target.name,
        "platform": args.platform or ROOT_DEFAULTS["platform"],
        "tracker": args.tracker or ROOT_DEFAULTS["tracker"],
        "issue_prefix": args.issue_prefix or "",
        "default_branch": args.default_branch or ROOT_DEFAULTS["default_branch"],
        "integration_branch": args.integration_branch or ROOT_DEFAULTS["integration_branch"],
        "related_docs": CONSUMER_RELATED_DOCS,
    }
    validate_config(config, "명령 인자")
    return config


def cmd_init_area(args, target: Path) -> int:
    given = [flag for flag in ROOT_ONLY_FLAGS if getattr(args, flag) is not None]
    if given:  # 영역 모드는 대상의 harness.json만 쓴다. 루트 인자를 조용히 버리지 않는다
        flags = ", ".join(f"--{flag.replace('_', '-')}" for flag in given)
        raise HarnessError(f"--area 는 대상의 {CONFIG_NAME} 설정을 쓰므로 {flags} 를 함께 쓸 수 없다")
    config_path = target / CONFIG_NAME
    if not config_path.is_file():
        raise HarnessError(f"{config_path} 이 없다. 루트 init 을 먼저 실행한다")
    config = load_config(config_path)
    dir_ = normalize_area_dir(args.area, "--area")
    areas = list(config.get("areas", []))
    same = [i for i, existing in enumerate(areas) if existing.get("dir") == dir_]
    previous = areas[same[0]] if same else {}
    # 재실행에서 --profile을 생략하면 이전 프로필을 유지한다. 다른 프로필로 바꾸면 설정이 섞이므로 거부한다
    if args.profile and previous.get("profile") and args.profile != previous["profile"]:
        raise HarnessError(f"영역 {dir_} 은 프로필 {previous['profile']} 로 만들어졌다. 프로필을 바꾸려면 "
                           f"{CONFIG_NAME} 에서 이 영역 항목을 지운 뒤 다시 만든다")
    profile_name = args.profile or previous.get("profile")
    if args.var and not profile_name:
        raise HarnessError("--var 는 --profile 과 함께 쓴다")
    profile, values = {}, {}
    if profile_name:
        profile, _base = load_profile(profile_name)
        values = resolve_vars(profile.get("vars", {}), parse_var_args(args.var), previous.get("vars", {}),
                              f"프로필 {profile_name}")
    ctx = var_context(values)
    command_ctx = {**ctx, "area_dir": dir_}  # 영역 검증 명령은 저장소 루트에서 실행된다
    if profile_name and not COMMAND_SAFE_PATH.fullmatch(dir_):
        raise HarnessError(f"프로필 영역 경로는 검증 명령에 그대로 들어가므로 영문자·숫자·`._/-`만 쓰고 `-`로 시작하지 않는다: {dir_!r}")
    # 명시 인자 → 이전 영역 값 → 프로필 기본값 → 키트 기본값(P-3). 프로필 없던 영역에 처음 적용하면 이전 값은
    # 키트 기본값을 굳힌 것이라 프로필 기본값을 가리지 않게 기준 문서만 이어받는다
    kept = previous if previous.get("profile") or not profile_name else \
        {key: value for key, value in previous.items() if key == "docs"}
    verify = args.verify_cmd or (kept.get("verify") if profile_name else None) or \
        [render(cmd, command_ctx, source=f"프로필 {profile_name} verify") for cmd in profile.get("verify", [])]
    if not verify:
        raise HarnessError("--area 에는 --verify-cmd 가 하나 이상 필요하다(프로필에 verify 가 있으면 생략 가능)")
    area = {"dir": dir_, "verify": verify}
    area["triggers"] = list(args.trigger or kept.get("triggers") or profile.get("triggers") or DEFAULT_TRIGGERS)
    if profile_name and PROFILE_TRIGGER not in area["triggers"]:
        area["triggers"].append(PROFILE_TRIGGER)  # 컨벤션 규칙을 조용히 바꾸거나 끄지 않게 한다(P-12)
    area["review_focus"] = args.review_focus or kept.get("review_focus") or profile.get("review_focus") or \
        list(DEFAULT_REVIEW_FOCUS)
    # 경로 판정 규칙도 생성 시점의 기본값을 설정에 굳힌다. 키트 기본값이 바뀌어도 기존 영역 판정은 그대로다
    area["trigger_paths"] = kept.get("trigger_paths", copy.deepcopy(
        profile.get("trigger_paths", hooks_common.DEFAULT_TRIGGER_PATHS)))
    area["test_paths"] = kept.get("test_paths", list(profile.get("test_paths", hooks_common.DEFAULT_TEST_PATHS)))
    if args.area_doc or previous.get("docs"):
        area["docs"] = args.area_doc or previous["docs"]
    if profile_name:
        area["profile"] = profile_name
        area["vars"] = values
        area["layers"] = previous.get("layers", copy.deepcopy(profile.get("layers", {})))
        area["allow"] = previous.get("allow", copy.deepcopy(profile.get("allow", {})))
        area["disabled_rules"] = previous.get("disabled_rules", {})
    if same:
        areas[same[0]] = area  # 같은 영역을 다시 생성하면 기존 항목을 제자리에서 바꾼다
    else:
        areas.append(area)
    config["areas"] = areas
    if profile_name:
        judge = config.setdefault("judge", {})
        if PROFILE_ROOT_TRIGGER not in judge.get("triggers", []):
            judge["triggers"] = [*judge.get("triggers", []), PROFILE_ROOT_TRIGGER]
    validate_config(config, "명령 인자")
    render_all(config, False)  # 다른 영역의 생성 파일과 목적지가 겹치는지 쓰기 전에 전체 설정으로 확인한다

    outputs = {f"{dir_}/AGENTS.md": render_area(area, build_context(config)), **render_conventions(area)}
    try:  # 변수(패키지 등)가 바뀌면 이전 경로의 컨벤션 파일이 남는다. 지우지 않고 알린다
        stale = [dest for dest in render_conventions(previous) if dest not in outputs and (target / dest).is_file()]
    except HarnessError:
        stale = []
    collisions = [dest for dest in outputs if (target / dest).exists()]
    if collisions and not args.force:
        listing = "\n".join(f"  {target / dest}" for dest in collisions)
        raise HarnessError(f"이미 존재하는 파일이 있어 중단한다. 덮어쓰려면 --force 를 지정한다:\n{listing}")
    for dest, content in outputs.items():
        write_text(target / dest, content)
    write_text(config_path, json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    print(f"영역 {dir_} 에 AGENTS.md 를 생성하고 {CONFIG_NAME} 의 areas 를 갱신했다.")
    if profile_name:
        print(f"프로필 {profile_name} 을 적용했다. 컨벤션 파일(손으로 고치지 않는다. 설정을 바꾸면 --force 로 다시 생성):")
        for dest in list(outputs)[1:]:
            print(f"  {dest}")
        for dest in stale:
            print(f"이전 컨벤션 파일(관리 대상에서 빠졌다. 확인 후 지운다): {dest}")
        for note in profile.get("setup_notes", []):
            print(f"안내: {render(note, command_ctx, source=f'프로필 {profile_name} setup_notes')}")
    return 0


def cmd_init(args) -> int:
    target, self_mode = resolve_target(args)
    if args.area is not None:
        return cmd_init_area(args, target)
    for flag in ("verify_cmd", "trigger", "review_focus", "area_doc", "profile", "var"):
        if getattr(args, flag):
            raise HarnessError(f"--{flag.replace('_', '-')} 는 --area 와 함께 쓴다")
    if self_mode:
        given = [flag for flag in ROOT_ONLY_FLAGS if getattr(args, flag) is not None]
        if given:
            raise HarnessError(f"--self 는 루트 설정 인자를 함께 쓸 수 없다: {', '.join(given)}")
        config = load_config(KIT_ROOT / CONFIG_NAME)
    else:
        config = config_from_args(args, target)
    outputs = render_all(config, self_mode)
    seeds = seed_dests(config, self_mode)

    if not self_mode and not args.force:
        collisions = [dest for dest in outputs if dest not in seeds and (target / dest).exists()]
        if collisions:
            listing = "\n".join(f"  {c}" for c in collisions)
            raise HarnessError(
                "이미 존재하는 파일이 있어 중단한다. 덮어쓰려면 --force 를 지정한다:\n" + listing
            )

    not_files = [dest for dest in outputs if dest in seeds and (target / dest).exists()
                 and not (target / dest).is_file()]
    if not_files:  # 디렉터리 등을 시드 파일처럼 유지하면 check는 없음으로 보고하고 init으로도 복구되지 않는다
        listing = "\n".join(f"  {d}" for d in not_files)
        raise HarnessError("시드 파일 경로가 파일이 아니라 중단한다. 그 경로를 정리한 뒤 다시 실행한다:\n" + listing)

    target.mkdir(parents=True, exist_ok=True)
    kept = [dest for dest in outputs if dest in seeds and (target / dest).is_file()]
    for dest, content in outputs.items():
        if dest in kept:  # 시드 파일은 소비자 소유라 --force 로도 덮어쓰지 않는다
            continue
        write_text(target / dest, content)
    ignored = ensure_ignored(target)
    if not self_mode:
        config_path = target / CONFIG_NAME
        if not config_path.exists() or args.force:
            config["harness_version"] = VERSION
            write_text(config_path, json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    print(f"{len(outputs) - len(kept)}개 파일을 {target} 에 생성했다.")
    for dest in outputs:
        print(f"  {dest}" + (" (유지: 소비자 소유 시드 파일)" if dest in kept else ""))
    for line in ignored:
        print(f".gitignore 에 {line} 를 추가했다.")
    return 0


def cmd_scaffold(args) -> int:
    target = Path(args.target or ".").resolve()
    config = load_config(target / CONFIG_NAME)
    dir_ = normalize_area_dir(args.area, "영역 인자")
    area = next((a for a in config.get("areas", []) if a["dir"] == dir_), None)
    if area is None:
        raise HarnessError(f"{CONFIG_NAME} 에 영역 {dir_} 이 없다. init --area 로 먼저 만든다")
    if "profile" not in area:
        raise HarnessError(f"영역 {dir_} 에 프로필이 없다. init --area {dir_} --profile <name> --force 로 적용한다")
    name = area["profile"]
    profile, base = load_profile(name)
    kinds = profile.get("scaffold", {})
    if args.kind not in kinds:
        raise HarnessError(f"프로필 {name} 에 스캐폴드 종류 {args.kind!r} 가 없다 (종류: {', '.join(kinds) or '없음'})")
    kind = kinds[args.kind]
    source = f"{name} scaffold.{args.kind}"
    values = resolve_vars({**profile.get("vars", {}), **kind.get("vars", {})}, parse_var_args(args.var),
                          area.get("vars", {}), source)
    ctx = {**var_context(values), **name_variants(args.name)}
    # 대상 저장소의 같은 이름 템플릿이 프로필 기본보다 우선한다(P-4). 아키텍처에 맞게 프로젝트가 덮어쓴다
    override = target / ".harness" / "templates" / name / args.kind

    def template(rel: str) -> tuple[Path, str]:
        path = override / rel
        if path.is_file():
            return path, f".harness/templates/{name}/{args.kind}/{rel}"
        return scaffold_template(base, args.kind, rel, source), f"{name}/scaffold/{args.kind}/{rel}"

    outputs: dict[str, str] = {}
    for entry in kind["files"]:
        path, label = template(entry["src"])
        dest = f"{dir_}/{render_dest(entry['dest'], ctx, source)}"
        if dest in outputs:
            raise HarnessError(f"{source}: 목적지가 겹친다: {dest}")
        outputs[dest] = render(path.read_text(encoding="utf-8"), ctx, source=label)
    # 생성물은 팀 소유라 덮어쓰지 않는다. 하나라도 있으면 아무것도 쓰지 않는다(P-5)
    existing = [dest for dest in outputs if (target / dest).exists()]
    if existing:
        listing = "\n".join(f"  {dest}" for dest in existing)
        raise HarnessError(f"이미 존재하는 파일이 있어 아무것도 생성하지 않았다:\n{listing}")
    snippet = None
    if "snippet" in kind:
        path, label = template(kind["snippet"])
        snippet = render(path.read_text(encoding="utf-8"), ctx, source=label)
    for dest, content in outputs.items():
        write_text(target / dest, content)
    print(f"{len(outputs)}개 파일을 생성했다({name} {args.kind} {args.name}):")
    for dest in outputs:
        print(f"  {dest}")
    if snippet is not None:  # 공유 파일(라우터·설정)은 고치지 않는다. 붙일 위치는 사람이 정한다(P-6)
        print("\n공유 파일에 직접 붙일 코드 조각:\n")
        print(snippet.rstrip("\n"))
    return 0


def cmd_check(args) -> int:
    target, self_mode = resolve_target(args)
    config = load_config(target / CONFIG_NAME)
    recorded = config.get("harness_version")
    version_drift = recorded != VERSION
    if version_drift:
        print(f"버전 불일치: 설정의 harness_version {recorded!r} 과 키트 {VERSION} 이 다르다.")
    outputs = render_all(config, self_mode)
    seeds = seed_dests(config, self_mode)
    missing, drifted = [], []
    for dest, content in outputs.items():
        path = target / dest
        if not path.is_file():
            missing.append(dest)
        elif dest in seeds:  # 시드 파일은 소비자가 채우므로 내용을 비교하지 않는다
            continue
        elif path.read_text(encoding="utf-8").replace("\r\n", "\n") != content:
            drifted.append(dest)
    hooks_dir = target / ".claude" / "hooks"
    expected_hooks = {Path(dest).name for dest in outputs if dest.startswith(".claude/hooks/")}
    extra_hooks = sorted(p.relative_to(target).as_posix() for p in hooks_dir.iterdir()
                         if p.name not in expected_hooks) if hooks_dir.is_dir() else []
    if not missing and not drifted and not extra_hooks and not version_drift:
        print(f"드리프트 없음: {len(outputs)}개 파일이 템플릿과 일치한다.")
        return 0
    for dest in missing:
        print(f"없음:   {dest}")
    for dest in drifted:
        print(f"불일치: {dest}")
    for dest in extra_hooks:
        print(f"여분:   {dest}")
    if extra_hooks:
        print("관리 hook 디렉터리의 여분 항목을 확인하고 제거한다.")
    if version_drift or missing or drifted:
        print("설정과 템플릿을 확인한 뒤 `init --self`(또는 `init <target> --force`)로 다시 생성한다.")
    return 1


TIER_LABELS = {"lite": "경량", "standard": "표준", "strict": "엄격"}


GIT_QUOTE_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def unquote_git_path(line: str) -> str:
    """`git diff --name-only`가 비ASCII·특수 문자 경로에 쓰는 C 방식 따옴표를 푼다(`"a/\\354\\240\\225.md"`)."""
    if len(line) < 2 or not (line.startswith('"') and line.endswith('"')):
        return line
    body, out, i = line[1:-1], bytearray(), 0
    while i < len(body):
        ch = body[i]
        if ch != "\\":
            out += ch.encode("utf-8")
            i += 1
        elif re.match(r"[0-7]{3}", body[i + 1:i + 4]):
            out.append(int(body[i + 1:i + 4], 8))
            i += 4
        elif body[i + 1:i + 2] in GIT_QUOTE_ESCAPES:
            out.append(GIT_QUOTE_ESCAPES[body[i + 1]])
            i += 2
        else:
            return line  # git이 만든 형식이 아니면 그대로 둔다
    return out.decode("utf-8", errors="replace")


def read_file_list(source: str) -> list[str]:
    try:
        if source == "-":
            stream = getattr(sys.stdin, "buffer", None)
            data = stream.read() if stream is not None else sys.stdin.read().encode("utf-8")
        else:
            data = Path(source).read_bytes()
        text = data.decode("utf-8-sig")  # 메모장 등이 붙인 BOM이 첫 경로에 섞이지 않게 한다
    except OSError as exc:
        raise HarnessError(f"파일 목록을 읽을 수 없다: {source}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise HarnessError(f"파일 목록은 UTF-8이어야 한다: {source}: {exc}") from exc
    return [unquote_git_path(line.strip()) for line in text.splitlines() if line.strip()]


EVAL_KEYS = {"id", "paths", "expected", "source", "note"}


def read_eval_cases(source: Path) -> tuple[list[dict], int]:
    """(정답이 있는 사례, 정답 미확정 수). 한 줄에 사례 하나(JSONL). 오류에는 줄 번호만 적고 입력 내용은 옮기지 않는다."""
    try:
        text = source.read_bytes().decode("utf-8-sig")
    except OSError as exc:
        raise HarnessError(f"eval 사례 파일을 읽을 수 없다: {source}: {exc.strerror or exc}") from None
    except UnicodeDecodeError:
        raise HarnessError(f"eval 사례 파일은 UTF-8이어야 한다: {source}") from None
    cases, unlabeled, ids = [], 0, set()
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        where = f"{source}:{number}"
        try:
            case = json.loads(line)
        except json.JSONDecodeError:
            raise HarnessError(f"{where}: JSON 객체가 아니다") from None
        if not isinstance(case, dict):
            raise HarnessError(f"{where}: JSON 객체가 아니다")
        unknown = sorted(set(case) - EVAL_KEYS)
        if unknown:
            raise HarnessError(f"{where}: 알 수 없는 키: {', '.join(unknown)}")
        if not isinstance(case.get("id"), str) or not case["id"].strip():
            raise HarnessError(f"{where}: id는 비어 있지 않은 문자열이어야 한다")
        if case["id"] in ids:
            raise HarnessError(f"{where}: id가 중복된다: {case['id']}")
        ids.add(case["id"])
        paths = case.get("paths")
        if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p.strip() for p in paths):
            raise HarnessError(f"{where}: paths는 비어 있지 않은 문자열 목록이어야 한다")
        if "expected" not in case:
            raise HarnessError(f"{where}: expected가 없다(정답 미확정이면 null)")
        if case["expected"] is None:
            unlabeled += 1
            continue
        if case["expected"] not in hooks_common.TIERS:
            raise HarnessError(f"{where}: expected는 lite·standard·strict 또는 null이어야 한다")
        for key in ("source", "note"):
            if key in case and case[key] is not None and not isinstance(case[key], str):
                raise HarnessError(f"{where}: {key}는 문자열이어야 한다")
        cases.append(case)
    return cases, unlabeled


def run_eval(cases: list[dict], unlabeled: int, config: dict) -> dict:
    """사례마다 `harness judge`와 같은 판정을 내고 정답과 비교한다."""
    matrix = {exp: {got: 0 for got in hooks_common.TIERS} for exp in hooks_common.TIERS}
    mismatches = []
    for case in cases:
        result = hooks_common.judge(case["paths"], config)
        judged = result["tier"]
        matrix[case["expected"]][judged] += 1
        if judged != case["expected"]:
            # 판정을 정한 파일(가장 높은 판정의 첫 파일). 판정이 정답보다 낮으면 근거는 기본값일 수 있다
            decider = next((f for f in result["files"] if f["tier"] == judged), None)
            mismatches.append({"id": case["id"], "expected": case["expected"], "judged": judged,
                               "path": decider["path"] if decider else None,
                               "rule": decider["rule"] if decider else None})
    matched = len(cases) - len(mismatches)
    return {"total": len(cases), "matched": matched, "unlabeled": unlabeled,
            "accuracy": round(matched / len(cases), 4) if cases else None,
            "confusion": matrix, "mismatches": mismatches}


def print_eval(report: dict) -> None:
    accuracy = "-" if report["accuracy"] is None else f"{report['accuracy'] * 100:.1f}%"
    print(f"eval: {report['matched']}/{report['total']} 일치 ({accuracy}), 정답 미확정 {report['unlabeled']}건 제외")
    print("혼동 행렬 (행 정답, 열 판정)")
    print("  " + " " * 9 + "".join(f"{t:>9}" for t in hooks_common.TIERS))
    for expected, row in report["confusion"].items():
        print(f"  {expected:<9}" + "".join(f"{row[t]:>9}" for t in hooks_common.TIERS))
    if report["mismatches"]:
        print("불일치:")
        for item in report["mismatches"]:
            decider = f"  {item['path']}  {item['rule']}" if item["path"] else ""
            print(f"  {item['id']}: 정답 {item['expected']}, 판정 {item['judged']}{decider}")


def cmd_judge(args) -> int:
    target, _self_mode = resolve_target(args) if args.self or args.target else (Path.cwd(), False)
    config = load_config(target / CONFIG_NAME)
    if args.eval is not None:
        report = run_eval(*read_eval_cases(Path(args.eval)), config)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print_eval(report)
        return 1 if report["mismatches"] else 0
    if args.files is not None:
        base, paths = None, read_file_list(args.files)
    else:
        ref = args.base or f"origin/{config['default_branch']}"
        try:
            base, paths = hooks_common.changed_files(target, ref)
        except hooks_common.ConfigError as exc:
            raise HarnessError(str(exc)) from exc
    result = {"base": base, **hooks_common.judge(paths, config)}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(f"판정: {result['tier']} ({TIER_LABELS[result['tier']]})")
    if base:
        print(f"기준: {base}")
    if not result["files"]:
        print("변경 파일 없음")
    for item in sorted(result["files"], key=lambda f: (-hooks_common.TIERS.index(f["tier"]), f["path"])):
        area = f" [{item['area']}]" if item["area"] else ""
        print(f"  {item['tier']:<8} {item['path']}{area}  {item['rule']}")
    if result["human_check"]:
        print("사람 확인 필요(경로로 판정하지 않는 트리거. 해당하면 판정을 올린다):")
        for item in result["human_check"]:
            print(f"  [{item['area'] or '영역 밖'}] {item['trigger']}")
    return 0


def cmd_events_summary(args) -> int:
    target, _self_mode = resolve_target(args) if args.self or args.target else (Path.cwd(), False)
    branch = args.branch or hooks_common.current_branch(target)
    if not branch:
        raise HarnessError("현재 브랜치를 알 수 없다(git 저장소가 아니거나 detached HEAD). --branch 로 지정한다")
    path = hooks_common.git_state_path(target, "events.jsonl")
    if path is None:
        raise HarnessError(f"git 저장소가 아니다: {target}")
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.is_file() else []
    except OSError as exc:
        raise HarnessError(f"hooks 기록을 읽을 수 없다: {path}: {exc}") from exc
    summary = metrics_collect.summarize_events(lines, branch)
    sys.stdout.write(metrics_collect.hooks_markdown(summary))
    notes = []
    if summary["legacy"]:
        notes.append(f"브랜치가 없는 옛 기록 {summary['legacy']}줄")
    if summary["malformed"]:
        notes.append(f"읽을 수 없는 줄 {summary['malformed']}줄")
    if notes:
        print(f"제외: {', '.join(notes)}", file=sys.stderr)
    return 0


def branch_commits(target: Path, base: str) -> list[tuple] | None:
    """base와 HEAD의 merge-base 이후 이 브랜치 커밋(sha, committer 시각). 구하지 못하면 None."""
    merge_base = hooks_common.git(target, "merge-base", base, "HEAD")
    if not merge_base:
        return None
    out = hooks_common.git(target, "log", "--format=%H %cI", f"{merge_base.strip()}..HEAD")
    if out is None:
        return None
    commits = []
    for line in out.splitlines():
        sha, _, stamp = line.partition(" ")
        moment = metrics_collect.parse_ts(stamp)
        if sha and moment is not None:
            commits.append((sha, moment))
    return commits


def session_logs(target: Path) -> list[Path]:
    """같은 저장소의 모든 worktree 세션 기록. 세션은 띄운 checkout에 기록되므로 다른 worktree에서 한 커밋도
    여기서 찾는다(리뷰 B F1). 공용 git 디렉터리의 `harness/`와 `worktrees/*/harness/`를 본다."""
    common = hooks_common.git(target, "rev-parse", "--path-format=absolute", "--git-common-dir") \
        or hooks_common.git(target, "rev-parse", "--git-common-dir")  # git 2.31 전에는 --path-format이 없다
    if not common:
        return []
    root = Path(common.strip())
    root = root if root.is_absolute() else target / root
    paths = [root / "harness" / hooks_common.SESSION_LOG]
    worktrees = root / "worktrees"
    if worktrees.is_dir():
        paths += sorted(path / "harness" / hooks_common.SESSION_LOG for path in worktrees.iterdir())
    return [path for path in paths if path.is_file()]


def session_report(args) -> tuple[dict, int | None, int | None, dict]:
    """(요약, 기록 없는 커밋 수, 비교한 커밋 수, 단가). `sessions summary`와 `agent summary`가 같이 쓴다."""
    target, _self_mode = resolve_target(args) if args.self or args.target else (Path.cwd(), False)
    branch = args.branch or hooks_common.current_branch(target)
    if not branch:
        raise HarnessError("현재 브랜치를 알 수 없다(git 저장소가 아니거나 detached HEAD). --branch 로 지정한다")
    if hooks_common.git_state_path(target, hooks_common.SESSION_LOG) is None:
        raise HarnessError(f"git 저장소가 아니다: {target}")
    lines: list[str] = []
    for path in session_logs(target):
        try:
            lines += path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            raise HarnessError(f"세션 기록을 읽을 수 없다: {path}: {exc}") from exc
    summary = metrics_collect.summarize_sessions(lines, branch)
    try:
        config = load_config(target / CONFIG_NAME) if (target / CONFIG_NAME).is_file() else {}
    except HarnessError as exc:  # 설정이 깨져도 요약은 낸다(#96 리뷰 B F3)
        print(f"{CONFIG_NAME}를 읽지 못해 기본값(기준 origin/main, 키트 단가)으로 요약한다: {exc}", file=sys.stderr)
        config = {}
    commits = None
    if not args.branch or args.branch == hooks_common.current_branch(target):  # 커밋 비교는 체크아웃한 브랜치만
        base = args.base or f"origin/{config.get('integration_branch') or config.get('default_branch') or 'main'}"
        commits = branch_commits(target, base)
        if commits is None:
            print(f"기록 없는 커밋은 세지 않았다: 기준 {base}와 merge-base를 구할 수 없다", file=sys.stderr)
    unrecorded = (len(metrics_collect.unrecorded_commits(commits, summary["intervals"]))
                  if commits is not None else None)
    if summary["malformed"]:
        print(f"제외: 읽을 수 없는 줄 {summary['malformed']}줄", file=sys.stderr)
    return summary, unrecorded, None if commits is None else len(commits), metrics_collect.merged_pricing(config)


def cmd_sessions_summary(args) -> int:
    sys.stdout.write(metrics_collect.sessions_text(*session_report(args)))
    return 0


def cmd_agent_summary(args) -> int:
    sys.stdout.write(metrics_collect.agent_markdown(*session_report(args)))
    return 0


def load_dashboard():
    spec = importlib.util.spec_from_file_location("harness_dashboard", KIT_ROOT / "core" / "dashboard" / "dashboard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cmd_dashboard(args) -> int:
    target, _self_mode = resolve_target(args) if args.self or args.target else (Path.cwd(), False)
    if hooks_common.git_state_path(target, hooks_common.SESSION_LOG) is None:
        raise HarnessError(f"git 저장소가 아니다: {target}")
    try:
        config = load_config(target / CONFIG_NAME) if (target / CONFIG_NAME).is_file() else {}
    except HarnessError as exc:
        print(f"{CONFIG_NAME}를 읽지 못해 기본값으로 연다: {exc}", file=sys.stderr)
        config = {}
    dashboard = load_dashboard()
    base = args.base or f"origin/{config.get('integration_branch') or config.get('default_branch') or 'main'}"
    source = dashboard.Source(target, hooks_common, metrics_collect, metrics_collect.merged_pricing(config),
                              metrics_collect.parse_offset(args.utc_offset), base)
    if args.once:  # 서버 없이 지금 상태를 JSON으로(스크립트·확인용)
        print(json.dumps(source.state(wait=True), ensure_ascii=False, indent=1))
        return 0
    try:
        server = dashboard.make_server(source, args.port)
    except OSError as exc:
        raise HarnessError(f"127.0.0.1:{args.port}에서 열 수 없다: {exc}") from exc
    print(f"harness 대시보드: http://127.0.0.1:{server.server_address[1]}/ (Ctrl+C로 끝낸다)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_review_summary(args) -> int:
    path = Path(args.review)
    if not path.is_file():
        raise HarnessError(f"리뷰 파일이 없다: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise HarnessError(f"리뷰 파일을 읽을 수 없다: {path}: {exc}") from exc
    summary, errors = metrics_collect.summarize_review(text)
    if summary is None or errors:
        raise HarnessError(f"{path}: " + "; ".join(errors))
    sys.stdout.write(metrics_collect.review_lines(summary))
    return 0


def cmd_classify_ci(args) -> int:
    jobs_path = Path(args.jobs)
    if not jobs_path.is_file():
        raise HarnessError(f"job 메타데이터 파일이 없다: {jobs_path}")
    trace_dir = Path(args.trace_dir) if args.trace_dir else None
    if trace_dir is not None and not trace_dir.is_dir():
        raise HarnessError(f"trace 디렉터리가 없다: {trace_dir}")
    try:
        jobs = classify_ci.load_jobs(jobs_path.read_bytes().decode("utf-8", errors="replace"))
        records, skipped = classify_ci.classify_jobs(jobs, trace_dir)
    except classify_ci.ClassifyError as exc:
        raise HarnessError(f"{jobs_path}: {exc}") from exc
    output = classify_ci.to_jsonl(records)
    if args.out:
        write_text(Path(args.out), output)
    else:
        sys.stdout.write(output)
    print(f"실패 job {len(records)}개를 분류했다. 실패가 아닌 job {skipped}개는 제외했다.", file=sys.stderr)
    return 0


def cmd_metrics_collect(args) -> int:
    try:
        result = metrics_collect.run(args, dict(os.environ))
    except metrics_collect.CollectError as exc:
        raise HarnessError(f"측정 수집 실패: {exc}") from None
    print(metrics_collect.summary(result, args.out), file=sys.stderr)
    return 0


def cmd_metrics_report(args) -> int:
    try:
        result = metrics_report.run(args)
    except metrics_report.ReportError as exc:
        raise HarnessError(f"리포트 생성 실패: {exc}") from None
    print(metrics_report.summary(result, args.out), file=sys.stderr)
    return 0


def metrics_cases(data: dict) -> tuple[list[dict], int]:
    """(eval 사례 후보, 뺀 MR 수). 본문과 재판정이 같으면 그 판정을 정답으로, 다르거나 본문 판정이 없으면 정답 미확정(null)."""
    cases, skipped = [], 0
    for mr in data.get("mrs") or []:
        if not isinstance(mr, dict):
            skipped += 1
            continue
        paths = mr.get("paths")
        number = mr.get("number")
        tier = mr.get("tier") if isinstance(mr.get("tier"), dict) else {}
        if (mr.get("integration") or not isinstance(number, int) or not isinstance(paths, list) or not paths
                or not all(isinstance(p, str) and p for p in paths)):
            skipped += 1  # 통합 MR, `paths`가 없는 옛 수집 결과
            continue
        body, judged = tier.get("body"), tier.get("judge")
        case = {"id": f"mr-{number}", "paths": paths, "expected": None}
        if isinstance(mr.get("url"), str) and mr["url"]:
            case["source"] = mr["url"]
        if body in hooks_common.TIERS and not tier.get("mismatch"):
            case["expected"] = body
        elif body in hooks_common.TIERS:
            case["note"] = f"불일치: 본문 {body} / 재판정 {judged} — 사람이 정답을 정한다"
        else:
            case["note"] = "본문 판정 없음 — 사람이 정답을 정한다"
        cases.append(case)
    return cases, skipped


def cmd_metrics_cases(args) -> int:
    source = Path(args.input)
    try:
        data = json.loads(source.read_bytes().decode("utf-8-sig"))
    except OSError as exc:
        raise HarnessError(f"수집 결과를 읽을 수 없다: {source}: {exc.strerror or exc}") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HarnessError(f"수집 결과가 JSON이 아니다: {source}") from None
    if not isinstance(data, dict) or data.get("version") != 1:
        raise HarnessError(f"수집 결과(version 1)가 아니다: {source}")
    cases, skipped = metrics_cases(data)
    output = "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases)
    if args.out:
        try:
            metrics_collect.write_atomic(Path(args.out), output)  # 실패하면 기존 파일을 남긴다
        except OSError as exc:
            raise HarnessError(f"사례 파일을 쓸 수 없다: {args.out}: {exc.strerror or exc}") from None
    else:
        sys.stdout.write(output)
    unlabeled = sum(case["expected"] is None for case in cases)
    print(f"eval 사례 후보 {len(cases)}건(정답 미확정 {unlabeled}건). 통합 MR·변경 파일 목록이 없는 MR {skipped}건은 뺐다.",
          file=sys.stderr)
    return 0


def cmd_metrics_notify(args) -> int:
    try:
        result = metrics_notify.run(args, dict(os.environ))
    except metrics_notify.NotifyError as exc:
        raise HarnessError(f"주간 요약 발송 실패: {exc}") from None
    metrics_notify.emit(result, prefix="")  # --dry-run이면 본문을 표준 출력에, 아니면 요약(호스트만)을 표준 오류에
    return 0


# ---------------------------------------------------------------------------
# 플랜·리뷰 파일 lint (M2-3). 절 목록과 자리표시자는 템플릿에서 읽어 템플릿 변경을 따라간다
# ---------------------------------------------------------------------------

PLAN_TEMPLATE = "docs/templates/plan.md"
REVIEW_TEMPLATE = "docs/templates/review.md"
LINT_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
UNCHOSEN_TIER = re.compile(r"엄격\s*\\?\|\s*표준\s*\\?\|\s*경량")
TIER_VALUE = re.compile(r"엄격|표준|경량|strict|standard|lite")
TEMPLATE_PLACEHOLDER = re.compile(r"<[^<>\n]+>")
TABLE_DELIMITER = re.compile(r"\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*")
CELL_SPLIT = re.compile(r"(?<!\\)\|")
FENCE = re.compile(r"\s*(```|~~~)")
INLINE_CODE = re.compile(r"(`+).+?\1")


def strip_fences(text: str) -> list[str]:
    """코드 블록 줄을 빈 줄로 바꾼 줄 목록. 코드 블록 안의 제목·표·자리표시자는 검사하지 않는다."""
    lines, fence = [], None
    for line in text.replace("\r\n", "\n").split("\n"):
        match = FENCE.match(line)
        if fence is None and match:
            fence = match.group(1)
            lines.append("")
        elif fence is not None:
            if line.strip().startswith(fence):
                fence = None
            lines.append("")
        else:
            lines.append(line)
    return lines


def normalize_heading(title: str) -> str:
    """공백을 줄이고 끝의 괄호 주석(작성 안내)을 뗀다: `6. 변경 기록 (구현 단계에서 추기)` → `6. 변경 기록`."""
    title = re.sub(r"\s+", " ", title).strip()
    return re.sub(r"\s*\([^()]*\)$", "", title)


def lint_sections(lines: list[str]) -> tuple[list[str], dict[str, list[str]]]:
    """(첫 `## ` 앞의 줄, 정규화한 `## ` 제목 → 본문 줄). 같은 제목은 첫 절만 쓴다."""
    head: list[str] = []
    sections: dict[str, list[str]] = {}
    current = None
    for line in lines:
        if line.startswith("## "):
            current = normalize_heading(line[3:])
            current = current if current not in sections else f"\0{len(sections)}"
            sections[current] = []
        elif current is None:
            head.append(line)
        else:
            sections[current].append(line)
    return head, sections


def table_cells(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    return [cell.strip() for cell in CELL_SPLIT.split(body)]


def section_tables(lines: list[str]) -> list[tuple[list[str], list[list[str]]]]:
    """절 안의 GFM 표 목록: (머리 칸, 데이터 행). 머리 행 다음에 구분줄이 와야 표로 본다."""
    tables, i = [], 0
    while i < len(lines) - 1:
        if "|" in lines[i] and "|" in lines[i + 1] and TABLE_DELIMITER.fullmatch(lines[i + 1]):
            rows, j = [], i + 2
            while j < len(lines) and "|" in lines[j] and lines[j].strip():
                rows.append(table_cells(lines[j]))
                j += 1
            tables.append((table_cells(lines[i]), rows))
            i = j
        else:
            i += 1
    return tables


def first_table(lines: list[str], header: list[str] | None = None) -> list[list[str]] | None:
    """머리 칸이 템플릿 표와 같은 표의 데이터 행. header가 없으면 첫 표. 없으면 None.

    같은 절의 다른 표(예: 대체 검증 표)를 인수 테스트 표로 잘못 읽지 않게 머리로 고른다.
    """
    for cells, rows in section_tables(lines):
        if header is None or [normalize_heading(c) for c in cells] == [normalize_heading(c) for c in header]:
            return rows
    return None


def template_spec(text: str) -> dict:
    lines = strip_fences(text)
    _head, sections = lint_sections(lines)
    placeholders = sorted({m.group(0) for line in lines for m in TEMPLATE_PLACEHOLDER.finditer(INLINE_CODE.sub("", line))})
    return {"headings": [h for h in sections if not h.startswith("\0")], "placeholders": placeholders,
            "sections": sections}


def numbered_heading(spec: dict, number: str, source: str) -> str:
    for heading in spec["headings"]:
        if heading.startswith(f"{number}. "):
            return heading
    raise HarnessError(f"{source}: 템플릿에 {number}장 절이 없다")


# 리뷰 7장(처리)은 리뷰 세션이 아니라 작성자가 반영 뒤 채우고, M5-4(#99) 전의 리뷰 파일에는 없다. 없어도 실패로 보지 않는다
# (형식은 `harness review summary`가 검사한다).
OPTIONAL_REVIEW_HEADINGS = {"7. 처리"}


def lint_document(text: str, spec: dict, kind: str) -> tuple[list[str], list[str]]:
    """(실패, 경고) 메시지 목록. kind는 plan 또는 review."""
    lines = strip_fences(text)
    head, sections = lint_sections(lines)
    optional = OPTIONAL_REVIEW_HEADINGS if kind == "review" else set()
    failures = [f"절 누락: ## {h}" for h in spec["headings"] if h not in sections and h not in optional]
    if kind == "plan":
        tier_lines = [line for line in head if "판정:" in line]
        if not tier_lines:
            failures.append("판정 줄(`판정:`)이 없다")
        elif len(tier_lines) > 1:
            failures.append("판정 줄(`판정:`)이 여러 개다")
        elif UNCHOSEN_TIER.search(tier_lines[0]):
            failures.append("판정을 고르지 않았다: `엄격 | 표준 | 경량`이 그대로 남았다")
        else:
            # 값은 `판정:` 뒤 첫 `·` 앞 전체다(템플릿: `> 판정: **표준** · 트리거: ...`). 강조 기호만 떼고 통째로 맞춘다
            value = tier_lines[0].split("판정:", 1)[1].split("·", 1)[0].strip().strip("*_` ").strip()
            if not TIER_VALUE.fullmatch(value):
                failures.append("판정 값은 엄격·표준·경량(strict·standard·lite) 중 하나여야 한다")
        heading = spec["acceptance"]
        if heading in sections:
            rows = first_table(sections[heading], spec.get("acceptance_header"))
            if rows is None:
                failures.append(f"## {heading}: 인수 테스트 표가 없다")
            elif not rows:
                failures.append(f"## {heading}: 인수 테스트 표에 데이터 행이 없다")
            for row in rows or []:
                if not any(row[1:]):
                    failures.append(f"## {heading}: 빈 인수 테스트 행: {row[0] or '(번호 없음)'}")
    else:
        heading = spec["measurement"]
        if heading in sections:
            rows = first_table(sections[heading], spec.get("measurement_header"))
            if rows is None:
                failures.append(f"## {heading}: 측정 표가 없다")
            elif not rows:
                failures.append(f"## {heading}: 측정 표에 데이터 행이 없다")
            for row in rows or []:
                if len(row) < 2 or not all(row[1:]):
                    failures.append(f"## {heading}: 측정 칸이 비었다: {row[0] or '(항목 없음)'}")
            # 행을 지운 것도 빈 측정 칸이다. 템플릿의 측정 항목이 모두 있어야 한다
            present = {normalize_heading(row[0]) for row in rows or [] if row}
            for label in spec.get("measurement_rows", []) if rows else []:  # 빈 표는 위에서 한 번만 보고한다
                if normalize_heading(label) not in present:
                    failures.append(f"## {heading}: 측정 항목이 없다: {label}")
    body = "\n".join(INLINE_CODE.sub("", line) for line in lines)
    warnings = [f"자리표시자가 남았다: {p}" for p in spec["placeholders"] if p in body]
    return failures, warnings


def load_lint_spec(target: Path, config: dict, rel: str, number: str, key: str) -> dict:
    """대상의 렌더된 템플릿을 쓰고, 없으면 키트 원본을 대상 설정으로 렌더한다."""
    path = target / rel
    if path.is_file():
        try:
            text, source = path.read_bytes().decode("utf-8-sig"), rel
        except (OSError, UnicodeDecodeError) as exc:
            raise HarnessError(f"템플릿을 UTF-8로 읽을 수 없다: {path}: {exc}") from exc
    else:
        original = TEMPLATES_DIR / rel
        if not original.is_file():
            raise HarnessError(f"템플릿 파일이 없다: {original}")
        text, source = render(original.read_text(encoding="utf-8"), build_context(config), source=rel), f"키트 {rel}"
    spec = template_spec(text)
    if not spec["headings"]:
        raise HarnessError(f"{source}: 템플릿에 `## ` 절이 없다")
    spec[key] = numbered_heading(spec, number, source)
    tables = section_tables(spec.pop("sections")[spec[key]])
    if not tables:
        raise HarnessError(f"{source}: 템플릿 {number}장에 표가 없다")
    header, rows = tables[0]
    spec[f"{key}_header"] = header
    if key == "measurement":
        spec["measurement_rows"] = [row[0] for row in rows if row and row[0]]
    spec["source"] = source
    return spec


def plan_key_pattern(config: dict) -> re.Pattern:
    """플랜 파일 키: github은 이슈 번호, jira는 `<issue_prefix>-<번호>`(대소문자 무시)."""
    if config["tracker"] == "jira":
        return re.compile(re.escape(config.get("issue_prefix", "")) + r"-\d+", re.IGNORECASE)
    return re.compile(r"\d+")


def classify_plan_files(names: list[str], config: dict, key: str | None) -> dict[str, list[str]]:
    """D-15: `<키>.md` 플랜, `<키>-review*.md` 리뷰, `-mapping`·`-self-review` 제외, 그 밖은 무시."""
    key_pattern = re.escape(key) if key is not None else plan_key_pattern(config).pattern
    pattern = re.compile(rf"({key_pattern})(-review.*)?\.md", re.IGNORECASE if config["tracker"] == "jira" else 0)
    groups: dict[str, list[str]] = {"plan": [], "review": [], "excluded": [], "ignored": []}
    for name in sorted(names):
        stem = name[:-3] if name.endswith(".md") else name
        match = pattern.fullmatch(name)
        if name.endswith(".md") and (stem.endswith("-mapping") or "-self-review" in stem):
            if key is None or stem.lower().startswith(f"{key.lower()}-"):
                groups["excluded"].append(name)
        elif match:
            groups["review" if match.group(2) else "plan"].append(name)
        elif key is None:
            groups["ignored"].append(name)
    # 엄격 단계의 `-review.md`는 A·B 결과의 합집합 대조표다(영역 AGENTS.md 4장). 리뷰 양식이 아니므로 제외한다
    lowered = {name.lower() for name in names}
    combined = [name for name in groups["review"] if name.lower().endswith("-review.md") and
                {name[:-3].lower() + "-a.md", name[:-3].lower() + "-b.md"} & lowered]
    groups["review"] = [name for name in groups["review"] if name not in combined]
    groups["excluded"].extend(combined)
    return groups


def cmd_lint_plans(args) -> int:
    if args.self:
        target = KIT_ROOT
    else:
        target = Path(args.target).resolve() if args.target else Path.cwd()
    if args.key is not None and not LINT_KEY.fullmatch(args.key):
        raise HarnessError(f"키는 영문·숫자·`.`·`_`·`-`만 쓴다: {args.key!r}")
    config = load_config(target / CONFIG_NAME)
    if args.key is not None and not plan_key_pattern(config).fullmatch(args.key):
        raise HarnessError(f"키는 {config['tracker']} 이슈 키 형식이어야 한다(예: {build_context(config)['branch_key_example']}): "
                           f"{args.key!r}")
    plans_dir = target / "plans"
    names = [p.name for p in plans_dir.iterdir() if p.is_file()] if plans_dir.is_dir() else []
    groups = classify_plan_files(names, config, args.key)
    if args.key is not None and not groups["plan"] and not groups["review"]:
        raise HarnessError(f"plans/ 에 키 {args.key} 의 플랜·리뷰 파일이 없다: {plans_dir}")
    if not groups["plan"] and not groups["review"]:
        print(f"검사할 파일 없음: {plans_dir}")
        return 0
    specs = {}
    if groups["plan"]:
        specs["plan"] = load_lint_spec(target, config, PLAN_TEMPLATE, "3", "acceptance")
    if groups["review"]:
        specs["review"] = load_lint_spec(target, config, REVIEW_TEMPLATE, "6", "measurement")
    total_failures = total_warnings = 0
    for kind, label in (("plan", "플랜"), ("review", "리뷰")):
        for name in groups[kind]:
            try:
                text = (plans_dir / name).read_bytes().decode("utf-8-sig")
            except (OSError, UnicodeDecodeError) as exc:
                raise HarnessError(f"plans/{name} 을 UTF-8로 읽을 수 없다: {exc}") from exc
            failures, warnings = lint_document(text, specs[kind], kind)
            total_failures += len(failures)
            total_warnings += len(warnings)
            status = "실패" if failures else ("경고" if warnings else "통과")
            print(f"{status}: plans/{name} ({label})")
            for message in failures:
                print(f"  실패: {message}")
            for message in warnings:
                print(f"  경고: {message}")
    if groups["excluded"]:
        print(f"제외(-mapping·-self-review·엄격 합본 -review.md): {', '.join(groups['excluded'])}")
    if groups["ignored"]:
        print(f"무시(플랜·리뷰 이름 아님): {', '.join(groups['ignored'])}")
    sources = ", ".join(f"{spec['source']}" for spec in specs.values())
    print(f"파일 {len(groups['plan']) + len(groups['review'])}개, 실패 {total_failures}건, 경고 {total_warnings}건 "
          f"(템플릿: {sources})")
    return 1 if total_failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harness", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="대상 저장소에 규칙 파일을 생성한다")
    init.add_argument("target", nargs="?")
    init.add_argument("--self", action="store_true", help="키트 저장소 자신에게 적용")
    init.add_argument("--config", help="사용할 harness.json 경로")
    init.add_argument("--project-name")
    init.add_argument("--platform", choices=sorted(PLATFORMS), help="기본 gitlab")
    init.add_argument("--tracker", choices=sorted(TRACKERS), help="기본 jira")
    init.add_argument("--issue-prefix", help="Jira 프로젝트 키(예: ABC123)")
    init.add_argument("--default-branch", help="기본 main")
    init.add_argument("--integration-branch", help="기본 develop")
    init.add_argument("--force", action="store_true", help="기존 파일을 덮어쓴다")
    init.add_argument("--area", help="영역 디렉터리(대상 저장소 기준 상대 경로)에 AGENTS.md 생성")
    init.add_argument("--verify-cmd", action="append", help="영역 검증 명령(반복 가능, --area 필수)")
    init.add_argument("--trigger", action="append", help="엄격 단계 트리거(반복 가능, 지정하면 기본값 대체)")
    init.add_argument("--review-focus", action="append", help="리뷰 관점(반복 가능, 지정하면 기본값 대체)")
    init.add_argument("--area-doc", action="append", help="영역 기준 문서 경로(반복 가능)")
    init.add_argument("--profile", help="영역에 적용할 프로필(profiles/<name>, --area 필수)")
    init.add_argument("--var", action="append", help="프로필 변수 이름=값(반복 가능)")
    init.set_defaults(func=cmd_init)

    scaffold = sub.add_parser("scaffold", help="영역 프로필의 템플릿으로 뼈대를 생성한다")
    scaffold.add_argument("area", help="영역 디렉터리(harness.json areas의 dir)")
    scaffold.add_argument("kind", help="스캐폴드 종류(profile.json scaffold의 키)")
    scaffold.add_argument("name", help="이름(예: MovieReview, movie-review)")
    scaffold.add_argument("--var", action="append", help="변수 이름=값(반복 가능)")
    scaffold.add_argument("--target", help="대상 저장소(기본 현재 디렉터리)")
    scaffold.set_defaults(func=cmd_scaffold)

    check = sub.add_parser("check", help="생성된 파일이 템플릿과 일치하는지 검사한다")
    check.add_argument("target", nargs="?")
    check.add_argument("--self", action="store_true")
    check.set_defaults(func=cmd_check)

    judge = sub.add_parser("judge", help="변경 파일로 경량·표준·엄격을 판정한다")
    judge.add_argument("target", nargs="?", help="대상 저장소(기본 현재 디렉터리)")
    judge.add_argument("--self", action="store_true")
    source = judge.add_mutually_exclusive_group()
    source.add_argument("--base", help="비교 기준 ref(기본 origin/<default_branch>). merge-base 이후 변경과 작업 트리를 본다")
    source.add_argument("--files", help="변경 파일 목록(줄 단위). - 이면 표준 입력")
    source.add_argument("--eval", metavar="CASES", help="eval 사례(JSONL)를 판정해 정답과 비교한다. 불일치가 있으면 종료 1")
    judge.add_argument("--json", action="store_true", help="JSON으로 출력")
    judge.set_defaults(func=cmd_judge)

    events = sub.add_parser("events", help="hooks 기록(.git/harness/events.jsonl)을 다룬다")
    events_sub = events.add_subparsers(dest="events_command", required=True)
    events_summary = events_sub.add_parser("summary", help="현재 브랜치의 hooks 기록을 MR 본문 `## hooks 기록` 절로 출력한다")
    events_summary.add_argument("target", nargs="?", help="대상 저장소(기본 현재 디렉터리). worktree마다 기록이 따로다")
    events_summary.add_argument("--self", action="store_true")
    events_summary.add_argument("--branch", help="집계할 브랜치(기본 현재 브랜치)")
    events_summary.set_defaults(func=cmd_events_summary)

    sessions = sub.add_parser("sessions", help="세션 기록(.git/harness/sessions.jsonl)을 다룬다")
    sessions_sub = sessions.add_subparsers(dest="sessions_command", required=True)
    sessions_summary = sessions_sub.add_parser("summary", help="현재 브랜치의 세션 수·응답·작업 시간·종료 검증·기록 없는 커밋")
    sessions_summary.add_argument("target", nargs="?", help="대상 저장소(기본 현재 디렉터리). worktree마다 기록이 따로다")
    sessions_summary.add_argument("--self", action="store_true")
    sessions_summary.add_argument("--branch", help="집계할 브랜치(기본 현재 브랜치). 다른 브랜치면 커밋 비교를 하지 않는다")
    sessions_summary.add_argument("--base", help="기록 없는 커밋을 셀 기준 ref(기본 origin/<integration_branch>)")
    sessions_summary.set_defaults(func=cmd_sessions_summary)

    agent = sub.add_parser("agent", help="에이전트 기록을 MR 본문 절로 다룬다")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    agent_summary = agent_sub.add_parser("summary", help="현재 브랜치의 세션 기록을 MR 본문 `## 에이전트 기록` 절로 출력한다")
    agent_summary.add_argument("target", nargs="?", help="대상 저장소(기본 현재 디렉터리)")
    agent_summary.add_argument("--self", action="store_true")
    agent_summary.add_argument("--branch", help="집계할 브랜치(기본 현재 브랜치). 다른 브랜치면 커밋 비교를 하지 않는다")
    agent_summary.add_argument("--base", help="기록 없는 커밋을 셀 기준 ref(기본 origin/<integration_branch>)")
    agent_summary.set_defaults(func=cmd_agent_summary)

    review = sub.add_parser("review", help="리뷰 파일의 처리 표를 MR 본문 요약으로 다룬다")
    review_sub = review.add_subparsers(dest="review_command", required=True)
    review_summary = review_sub.add_parser("summary", help="리뷰 파일 7장(처리) 표를 세어 `## 리뷰 결과`에 붙일 두 줄을 출력한다")
    review_summary.add_argument("review", help="리뷰 파일(엄격이면 A·B를 묶은 종합 파일)")
    review_summary.set_defaults(func=cmd_review_summary)

    dash = sub.add_parser("dashboard", help="로컬 실시간 대시보드(127.0.0.1): 진행 중 세션·토큰·추정 비용·hooks 개입")
    dash.add_argument("target", nargs="?", help="대상 저장소(기본 현재 디렉터리). 모든 worktree 기록을 읽는다")
    dash.add_argument("--self", action="store_true")
    dash.add_argument("--port", type=int, default=8765, help="포트(기본 8765, 0이면 빈 포트)")
    dash.add_argument("--base", help="기록 없는 커밋을 셀 기준 ref(기본 origin/<integration_branch>)")
    dash.add_argument("--utc-offset", default="+09:00", help="오늘·이번 주 경계 시간대(기본 +09:00)")
    dash.add_argument("--once", action="store_true", help="서버를 열지 않고 지금 상태를 JSON으로 출력한다")
    dash.set_defaults(func=cmd_dashboard)

    classify = sub.add_parser("classify-ci", help="실패한 CI job의 원인을 분류해 JSONL로 쓴다")
    classify.add_argument("--jobs", required=True, help="job 메타데이터 JSON(GitLab job API 형태, 목록 또는 객체)")
    classify.add_argument("--trace-dir", help="job trace 디렉터리(<job_id>.log). 없으면 메타데이터만으로 분류")
    classify.add_argument("--out", help="JSONL 출력 파일. 생략하면 표준 출력")
    classify.set_defaults(func=cmd_classify_ci)

    metrics = sub.add_parser("metrics", help="측정 지표를 수집하고 리포트를 만들고 주간 요약을 보낸다")
    metrics_sub = metrics.add_subparsers(dest="metrics_command", required=True)
    collect = metrics_sub.add_parser("collect", help="플랫폼 API에서 주별 지표와 원자료를 JSON으로 모은다")
    metrics_collect.add_arguments(collect)
    collect.set_defaults(func=cmd_metrics_collect)
    report = metrics_sub.add_parser("report", help="수집 JSON으로 주간 HTML 리포트를 만든다(오프라인)")
    metrics_report.add_arguments(report)
    report.set_defaults(func=cmd_metrics_report)
    notify = metrics_sub.add_parser("notify", help="수집 JSON의 마지막 주 요약을 webhook 또는 Mattermost로 보낸다")
    metrics_notify.add_arguments(notify)
    notify.set_defaults(func=cmd_metrics_notify)
    cases = metrics_sub.add_parser("cases", help="수집 JSON의 병합 MR을 판정 eval 사례 후보(JSONL)로 뽑는다")
    cases.add_argument("--in", dest="input", default="metrics.json", help="수집 결과(version 1). 기본 metrics.json")
    cases.add_argument("--out", help="JSONL 출력 파일. 생략하면 표준 출력")
    cases.set_defaults(func=cmd_metrics_cases)

    lint_plans = sub.add_parser("lint-plans", help="plans/의 플랜·리뷰 파일을 템플릿 기준으로 검사한다")
    lint_plans.add_argument("key", nargs="?", help="이 키의 플랜·리뷰만 검사(생략하면 plans/ 전체)")
    lint_target = lint_plans.add_mutually_exclusive_group()
    lint_target.add_argument("--target", help="대상 저장소(기본 현재 디렉터리)")
    lint_target.add_argument("--self", action="store_true", help="키트 저장소 자신")
    lint_plans.set_defaults(func=cmd_lint_plans)

    version = sub.add_parser("version")
    version.set_defaults(func=lambda _args: print(VERSION) or 0)

    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # Windows 콘솔에서도 한국어 메시지를 깨지지 않게 출력한다
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        return args.func(args)
    except HarnessError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
