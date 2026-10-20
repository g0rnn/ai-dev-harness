"""hooks 공통 코드. 대상 저장소의 `.claude/hooks/`에 복사되며 표준 라이브러리만 쓴다.

키트의 `bin/harness.py`도 이 파일을 읽어 `harness.json`의 `hooks` 항목을 같은 규칙으로 검사한다.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import locale
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

CONFIG_NAME = "harness.json"
HOOK_KEYS = {"protected_paths", "stop_verify", "stop_timeout_sec", "python"}
RULE_KEYS = {"pattern", "mode", "reason"}
MODES = ("ask", "block")  # 뒤로 갈수록 강하다
DEFAULT_PYTHON = "python3"
DEFAULT_STOP_TIMEOUT = 300
MAX_STOP_TIMEOUT = 840  # settings.json의 Stop hook timeout(900초)보다 작아야 한다

# 설정과 무관하게 항상 적용한다. AI가 hook이나 설정을 고쳐 스스로 가드레일을 푸는 경로를 막는다.
ALWAYS_PROTECTED = [
    {"pattern": "/.claude/settings.json", "mode": "block", "reason": "hooks 설정(자기 보호)"},
    {"pattern": "/.claude/settings.local.json", "mode": "block", "reason": "hooks 설정(자기 보호)"},
    {"pattern": "/.claude/hooks/", "mode": "block", "reason": "hooks 스크립트(자기 보호)"},
    {"pattern": "/harness.json", "mode": "ask", "reason": "키트 설정(보호 규칙·종료 검증)"},
    # CI 검사 코드(MR lint·변경 영역 검증·측정). 고치면 MR 단계의 보호 경로 확인을 끌 수 있다(#100 리뷰 A F1)
    {"pattern": "/.harness/", "mode": "ask", "reason": "키트 CI 검사 코드"},
]

# 프로젝트 밖이지만 hooks를 끌 수 있는 사용자 설정. 전역 설정을 맡기는 정상 작업이 있어 확인만 받는다.
USER_SETTINGS = ("settings.json", "settings.local.json")

# `harness.json`의 hooks.protected_paths가 없을 때 쓰는 기본값. 지정하면 이 목록을 대체한다.
DEFAULT_PROTECTED = [
    *({"pattern": name, "mode": "block", "reason": "lock 파일(패키지 관리자가 생성)"} for name in (
        "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "gradle.lockfile",
        "poetry.lock", "uv.lock", "Cargo.lock", "go.sum",
    )),
    {"pattern": ".gitlab-ci.yml", "mode": "ask", "reason": "CI 정의"},
    {"pattern": "/.github/workflows/", "mode": "ask", "reason": "CI 정의"},
    {"pattern": "**/db/migration/", "mode": "ask", "reason": "DB 마이그레이션"},
    {"pattern": "migrations/", "mode": "ask", "reason": "DB 마이그레이션"},
    {"pattern": ".env.example", "mode": "ask", "reason": "환경 변수 계약"},
]


class ConfigError(Exception):
    """harness.json을 읽거나 검사하지 못했다."""


def _nonempty_str(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _str_list(value, allow_empty=False) -> bool:
    return isinstance(value, list) and (allow_empty or bool(value)) and all(_nonempty_str(v) for v in value)


def validate_hooks(hooks, source) -> None:
    if not isinstance(hooks, dict):
        raise ConfigError(f"{source}: hooks는 객체여야 한다")
    unknown = sorted(set(hooks) - HOOK_KEYS)
    if unknown:
        raise ConfigError(f"{source}: hooks에 알 수 없는 키가 있다: {', '.join(unknown)}")
    rules = hooks.get("protected_paths", [])
    if not isinstance(rules, list):
        raise ConfigError(f"{source}: hooks.protected_paths는 목록이어야 한다")
    for rule in rules:
        if not isinstance(rule, dict) or not _nonempty_str(rule.get("pattern")):
            raise ConfigError(f"{source}: hooks.protected_paths 항목에는 pattern이 필요하다")
        extra = sorted(set(rule) - RULE_KEYS)
        if extra:
            raise ConfigError(f"{source}: 보호 경로 {rule['pattern']}에 알 수 없는 키가 있다: {', '.join(extra)}")
        if rule.get("mode") not in MODES:
            raise ConfigError(f"{source}: 보호 경로 {rule['pattern']}의 mode는 {' 또는 '.join(MODES)}여야 한다")
        if "reason" in rule and not _nonempty_str(rule["reason"]):
            raise ConfigError(f"{source}: 보호 경로 {rule['pattern']}의 reason은 비어 있지 않은 문자열이어야 한다")
    if "stop_verify" in hooks and not _str_list(hooks["stop_verify"]):
        raise ConfigError(f"{source}: hooks.stop_verify는 비어 있지 않은 문자열 목록이어야 한다")
    if "stop_timeout_sec" in hooks:
        value = hooks["stop_timeout_sec"]
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_STOP_TIMEOUT:
            raise ConfigError(f"{source}: hooks.stop_timeout_sec는 1~{MAX_STOP_TIMEOUT} 정수여야 한다")
    if "python" in hooks:
        value = hooks["python"]
        # settings.json의 shell 명령 앞에 들어간다. 기존 Windows launcher 형태만 예외다.
        if not isinstance(value, str) or not (re.fullmatch(r"[A-Za-z0-9_.-]+", value) or value == "py -3"):
            raise ConfigError(f"{source}: hooks.python은 명령 이름 또는 'py -3'이어야 한다")


def _normalized_dir(value) -> bool:
    if not _nonempty_str(value) or "\\" in value or value.startswith("/") or value.endswith("/"):
        return False
    return all(part not in ("", ".", "..") for part in value.split("/"))


def validate_areas_for_hooks(areas, source) -> None:
    """hook이 쓰는 영역 필드만 검사한다. 전체 검사는 키트의 `harness check`가 한다."""
    if not isinstance(areas, list):
        raise ConfigError(f"{source}: areas는 목록이어야 한다")
    for area in areas:
        if not isinstance(area, dict) or not _normalized_dir(area.get("dir")):
            raise ConfigError(f"{source}: areas 항목의 dir은 정규화된 상대 경로여야 한다")
        if not _str_list(area.get("verify")):
            raise ConfigError(f"{source}: 영역 {area['dir']}의 verify는 비어 있지 않은 문자열 목록이어야 한다")
        if "docs" in area and not _str_list(area["docs"]):
            raise ConfigError(f"{source}: 영역 {area['dir']}의 docs는 비어 있지 않은 문자열 목록이어야 한다")


def load_config(project: Path) -> dict:
    """대상의 harness.json. 없으면 빈 설정(기본값 적용)."""
    path = project / CONFIG_NAME
    if not path.is_file():
        return {}
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if not isinstance(config, dict):
        raise ConfigError(f"{path}: 최상위는 객체여야 한다")
    if "hooks" in config:
        validate_hooks(config["hooks"], path)
    validate_areas_for_hooks(config.get("areas", []), path)
    return config


# ---------------------------------------------------------------------------
# 경로
# ---------------------------------------------------------------------------


def compile_glob(pattern: str) -> re.Pattern:
    """gitignore 방식의 glob. 대소문자는 구분하지 않는다(대소문자 무시 FS에서의 우회 방지).

    - 앞의 `./`는 무시한다. 끝의 `/`는 "그 아래 전체"다.
    - `/`로 시작하거나 중간에 `/`가 있으면 루트 기준, 아니면 모든 깊이에서 맞춘다.
    - 패턴이 디렉터리에 맞으면 그 아래 파일도 맞는다.
    - `**`는 0개 이상의 디렉터리, `*`·`?`는 `/`를 넘지 않는다.
    """
    p = pattern.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    dir_only = p.endswith("/")
    p = p.rstrip("/")
    if p.startswith("/"):
        p = p.lstrip("/")
    elif "/" not in p:
        p = "**/" + p
    parts = p.split("/")
    rx = ""
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        if part == "**":
            rx += ".+" if last else "(?:[^/]+/)*"
            continue
        for ch in part:
            rx += "[^/]*" if ch == "*" else "[^/]" if ch == "?" else re.escape(ch)
        if not last:
            rx += "/"
    if parts[-1] != "**":
        rx += "/.+" if dir_only else "(?:/.+)?"
    return re.compile(rx, re.IGNORECASE)


def project_root_raw(payload: dict) -> str:
    return os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()


def project_dir(payload: dict) -> Path:
    return Path(project_root_raw(payload)).resolve()


def lexical(path: Path) -> Path:
    """링크를 따라가지 않고 `.`·`..`만 정리한 절대 경로."""
    return Path(os.path.normpath(os.path.abspath(path)))


def classify_path(project: Path, raw: str, cwd: str | None = None,
                  project_lexical: Path | None = None) -> tuple[str, list[str], list[Path], list[Path]]:
    """("unc", [], [], []) 또는 ("paths", 프로젝트 안 `a/b` 목록, 프로젝트 밖 절대 경로 목록, 판정한 절대 경로).

    링크를 따라가지 않은 논리 경로와 `resolve()`한 실제 경로를 둘 다 돌려준다. 보호 대상 이름이
    심볼릭 링크이거나, 링크된 디렉터리를 거쳐 보호 파일에 닿는 경우를 모두 잡기 위해서다.
    Windows의 `\\\\?\\` 확장 경로는 일반 경로로 바꾼다. 그 밖의 UNC 경로는 프로젝트 소속을
    확인할 수 없으므로 "unc"로 돌려 호출자가 확인을 받게 한다.
    """
    text = raw.replace("\\", "/")
    if os.name == "nt":
        if text.lower().startswith("//?/unc/"):
            text = "//" + text[8:]
        elif text.startswith("//?/") or text.startswith("//./"):
            text = text[4:]
        if text.startswith("//"):
            return "unc", [], [], []
    path = Path(text)
    if not path.is_absolute():
        path = Path(cwd or project) / path
    roots = [project] + ([project_lexical] if project_lexical and project_lexical != project else [])
    inside: list[str] = []
    outside: list[Path] = []
    candidates = [lexical(path), path.resolve()]
    for candidate in candidates:
        rel = None
        for root in roots:
            try:
                rel = candidate.relative_to(root).as_posix()
                break
            except ValueError:
                continue
        if rel is None:
            if candidate not in outside:
                outside.append(candidate)
        elif rel not in ("", ".") and rel not in inside:
            inside.append(rel)
    return "paths", inside, outside, candidates


def self_protected_real(project: Path, candidates: list[Path]) -> dict | None:
    """자기 보호 경로의 실제 위치와 비교한다. `.claude`나 `.claude/hooks`가 링크여도 그 대상 경로로 쓰거나
    다른 링크를 거쳐 쓰는 경우, 보호 파일의 하드링크를 다른 이름으로 쓰는 경우를 잡는다."""
    claude = project / ".claude"
    files = [claude / "settings.json", claude / "settings.local.json"]
    hooks = claude / "hooks"
    try:
        real_files = [f.resolve() for f in files]
        real_hooks = hooks.resolve()
        hook_files = [p for p in hooks.iterdir() if p.is_file()] if hooks.is_dir() else []
    except OSError:
        return None
    for candidate in candidates:
        try:
            real = candidate.resolve()
        except OSError:
            continue
        if real in real_files or real == real_hooks or real_hooks in real.parents:
            return {"pattern": "/.claude/(실제 위치)", "mode": "block", "reason": "hooks 설정·스크립트(자기 보호)"}
        if real.is_file():
            for protected in [*files, *hook_files]:
                try:
                    if protected.is_file() and os.path.samefile(real, protected):
                        return {"pattern": "/.claude/(같은 파일)", "mode": "block",
                                "reason": "hooks 설정·스크립트의 하드링크(자기 보호)"}
                except OSError:
                    continue
    return None


def is_user_settings(path: Path) -> bool:
    folder = Path.home() / ".claude"
    try:
        return path.parent.resolve() == folder.resolve() and path.name.lower() in USER_SETTINGS
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 판정 (harness judge)
# ---------------------------------------------------------------------------

TIERS = ("lite", "standard", "strict")  # 뒤로 갈수록 강하다. 소비자 계약 식별자
TRIGGER_TIERS = ("strict", "standard")
LOCK_FILES = (
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lock", "bun.lockb",
    "gradle.lockfile", "poetry.lock", "uv.lock", "Pipfile.lock", "Cargo.lock", "go.sum",
    "Gemfile.lock", "composer.lock",
)

# 영역에 trigger_paths가 없거나 영역 밖 파일일 때 쓰는 공통 기본. 지정하면 이 값을 대체한다.
DEFAULT_TRIGGER_PATHS = {
    "strict": [
        *LOCK_FILES,
        ".gitlab-ci.yml", "/.github/workflows/", "Jenkinsfile",
        "**/db/migration/", "**/db/migrate/", "**/db/changelog/", "migrations/",
    ],
    "standard": [],
}

# 영역 AGENTS.md의 기본 트리거(스택 중립). triggers를 생략한 영역은 이 문장으로 사람 확인을 낸다.
# 스택 고유 항목은 프로필이 채운다
DEFAULT_AREA_TRIGGERS = [
    "계약 문서(API·스키마·영역 간 교환 계약)의 변경, 또는 계약과 다른 구현",
    "DB 마이그레이션",
    "인증·인가, 토큰·세션·쿠키 처리",
    "트랜잭션 경계의 신설·변경, 여러 저장소에 걸친 쓰기, 외부 시스템(메시지·캐시) 쓰기와 그 재시도·멱등성·부분 실패 처리",
    "공용 모듈 또는 다른 담당자 소유 영역의 생산 코드",
    "의존성·lock 파일, 운영 설정, 환경 변수 계약, CI 정의",
]

# 이 경로만 바뀌면 경량이다. 스택 중립 기본값이며 영역의 test_paths로 대체한다.
DEFAULT_TEST_PATHS = [
    "test/", "tests/", "__tests__/", "**/src/test/",
    "test_*.py", "*_test.py", "*_test.go", "*_spec.rb",
    # `*.spec.*` 전체를 쓰면 API 명세(`openapi.spec.yaml`)까지 경량이 된다. 판정은 하한이라 코드 확장자로 좁힌다
    *(f"*.{kind}.{ext}" for kind in ("test", "spec") for ext in ("js", "jsx", "ts", "tsx", "mjs", "cjs")),
    "*Test.java", "*Tests.java", "*Test.kt", "*Tests.kt",
]

# 문서는 저장소 루트 기준으로 본다.
DOC_PATHS = ["*.md", "/docs/"]


def validate_judge_rules(area: dict, source) -> None:
    """영역의 trigger_paths·test_paths 선택 키를 검사한다."""
    name = area.get("dir")
    if "trigger_paths" in area:
        rules = area["trigger_paths"]
        if not isinstance(rules, dict):
            raise ConfigError(f"{source}: 영역 {name}의 trigger_paths는 객체여야 한다")
        unknown = sorted(set(rules) - set(TRIGGER_TIERS))
        if unknown:
            raise ConfigError(f"{source}: 영역 {name}의 trigger_paths에 알 수 없는 키가 있다: {', '.join(unknown)}"
                              f" (허용: {', '.join(TRIGGER_TIERS)})")
        for tier, patterns in rules.items():
            if not _str_list(patterns, allow_empty=True):
                raise ConfigError(f"{source}: 영역 {name}의 trigger_paths.{tier}는 비어 있지 않은 문자열 목록이어야 한다")
    if "test_paths" in area and not _str_list(area["test_paths"], allow_empty=True):
        raise ConfigError(f"{source}: 영역 {name}의 test_paths는 비어 있지 않은 문자열 목록이어야 한다")


def normalize_change_path(raw: str) -> str:
    """git·사용자 입력 경로를 저장소 기준 `a/b`로 맞춘다."""
    parts = [part for part in raw.strip().replace("\\", "/").split("/") if part not in ("", ".")]
    return "/".join(parts)


def _first_match(patterns: list[str], path: str) -> str | None:
    return next((p for p in patterns if compile_glob(p).fullmatch(path)), None)


JUDGE_KEYS = {"trigger_paths", "test_paths", "triggers"}


def validate_judge_root(block, source) -> None:
    """최상위 judge: 영역 밖 파일에 적용하는 trigger_paths·test_paths와 사람 확인 문장 triggers."""
    if not isinstance(block, dict):
        raise ConfigError(f"{source}: judge는 객체여야 한다")
    unknown = sorted(set(block) - JUDGE_KEYS)
    if unknown:
        raise ConfigError(f"{source}: judge에 알 수 없는 키가 있다: {', '.join(unknown)}")
    validate_judge_rules({"dir": "(judge)", **block}, source)
    if "triggers" in block and not _str_list(block["triggers"]):
        raise ConfigError(f"{source}: judge.triggers는 비어 있지 않은 문자열 목록이어야 한다")


def judge_path(path: str, area: dict | None, root: dict | None = None) -> tuple[str, str]:
    """(tier, 근거 규칙). 영역 규칙은 영역 디렉터리 기준, 최상위 judge 규칙은 저장소 루트 기준 상대 경로에 맞춘다."""
    owner = area if area else (root or {})
    rel = path[len(area["dir"]) + 1:] if area else path  # 영역 경로 자체(하위 모듈)는 빈 문자열
    rules = owner.get("trigger_paths", DEFAULT_TRIGGER_PATHS)
    for tier in TRIGGER_TIERS:
        hit = _first_match(rules.get(tier, []), rel)
        if hit:
            return tier, f"trigger_paths.{tier}:{hit}"
    hit = _first_match(DOC_PATHS, path)
    if hit:
        return "lite", f"docs:{hit}"
    tests = owner.get("test_paths", DEFAULT_TEST_PATHS)
    hit = _first_match(tests, rel)
    if hit:
        return "lite", f"test_paths:{hit}"
    return "standard", "default"


def judge(paths: list[str], config: dict) -> dict:
    """변경 경로 목록의 판정. 경로로 판정할 수 없는 영역 트리거는 human_check로 돌려준다(판정값은 하한)."""
    areas = sorted(config.get("areas", []), key=lambda a: len(a["dir"]), reverse=True)
    root = config.get("judge") or {}
    files, human_check, seen = [], [], set()
    for path in dict.fromkeys(normalize_change_path(p) for p in paths):
        if not path:
            continue
        # 하위 모듈 커밋 변경은 영역 이름 그대로(`backend`) 나온다. stop-verify와 같이 영역 소유로 본다
        area = next((a for a in areas if path == a["dir"] or path.startswith(a["dir"] + "/")), None)
        tier, rule = judge_path(path, area, root)
        files.append({"path": path, "area": area["dir"] if area else None, "tier": tier, "rule": rule})
        owner = area["dir"] if area else None
        if owner not in seen:
            seen.add(owner)
            # 영역 문서는 triggers가 없으면 기본 트리거를 렌더하므로 판정도 같은 문장을 낸다
            triggers = area.get("triggers", DEFAULT_AREA_TRIGGERS) if area else root.get("triggers", [])
            human_check.extend({"area": owner, "trigger": t} for t in triggers)
    overall = max((f["tier"] for f in files), key=TIERS.index, default="lite")
    return {"tier": overall, "files": files, "human_check": human_check}


def changed_files(project: Path, base: str) -> tuple[str, list[str]]:
    """(merge-base, 변경 경로). base와 HEAD의 merge-base 이후 커밋·작업 트리·추적 안 된 파일을 모은다.

    rename은 옛 경로와 새 경로를 모두 넣는다(옮겨 간 쪽과 지워진 쪽 모두 판정 대상).
    """
    if base.startswith("-"):  # git 옵션으로 해석되지 않게 한다
        raise ConfigError(f"기준 ref가 잘못됐다: {base}")
    merge_base, err = git_result(project, "merge-base", base, "HEAD")
    if merge_base is None:
        raise ConfigError(f"기준 {base}와 HEAD의 merge-base를 구할 수 없다: {err}")
    merge_base = merge_base.strip()
    # 대상이 git 최상위의 하위 디렉터리일 수 있다. --relative는 대상 밖 변경을 빼고 대상 기준 경로를 낸다.
    # ls-files는 기본으로 현재 디렉터리 아래만 대상 기준으로 낸다
    diff, err = git_result(project, "-c", "core.quotepath=off", "diff", "--name-only", "--no-renames",
                           "--relative", "-z", merge_base)
    if diff is None:
        raise ConfigError(f"git diff 실패: {err}")
    untracked, err = git_result(project, "ls-files", "--others", "--exclude-standard", "-z")
    if untracked is None:
        raise ConfigError(f"git ls-files 실패: {err}")
    paths = [p for p in (diff + untracked).split("\0") if p]
    return merge_base, list(dict.fromkeys(paths))


# ---------------------------------------------------------------------------
# 보호 경로 규칙 (protect-paths hook과 MR lint가 같이 쓴다, #100)
# ---------------------------------------------------------------------------


def rules_for(config: dict | None) -> list[dict]:
    if config is None:
        return list(ALWAYS_PROTECTED)
    hooks = config.get("hooks") or {}
    rules = hooks.get("protected_paths")
    if rules is None:
        rules = DEFAULT_PROTECTED
    docs = [
        {"pattern": "/" + doc_path(doc), "mode": "ask", "reason": "계약 문서"}
        for area in config.get("areas", []) for doc in area.get("docs", [])
    ]
    return [*ALWAYS_PROTECTED, *rules, *docs]


def doc_path(doc: str) -> str:
    """영역 기준 문서 경로를 루트 기준 패턴 본문으로. `./`·앞 `/`를 떼어 낸다."""
    path = doc.strip().replace("\\", "/")
    while path.startswith("./") or path.startswith("/"):
        path = path[2:] if path.startswith("./") else path[1:]
    return path


def decide(rel: str, rules: list[dict]) -> dict | None:
    """일치하는 규칙 중 가장 강한 것. 같은 강도면 먼저 나온 규칙."""
    best = None
    for rule in rules:
        if not compile_glob(rule["pattern"]).fullmatch(rel):
            continue
        if best is None or MODES.index(rule["mode"]) > MODES.index(best["mode"]):
            best = rule
    return best


def commands_for(config: dict, files: list[str]) -> list[str]:
    """변경 파일이 속한 영역의 `verify`(설정 순서), 영역 밖 변경이면 `hooks.stop_verify`. 중복 명령은 한 번."""
    areas = sorted(config.get("areas", []), key=lambda a: len(a["dir"]), reverse=True)
    touched, root_touched = set(), False
    for path in files:
        owner = next((a["dir"] for a in areas if path == a["dir"] or path.startswith(a["dir"] + "/")), None)
        if owner is None:
            root_touched = True
        else:
            touched.add(owner)
    commands: list[str] = []
    for area in config.get("areas", []):  # 설정 순서대로 실행한다
        if area["dir"] in touched:
            commands.extend(c for c in area["verify"] if c not in commands)
    if root_touched:
        stop_verify = (config.get("hooks") or {}).get("stop_verify", [])
        commands.extend(c for c in stop_verify if c not in commands)
    return commands


# ---------------------------------------------------------------------------
# git과 기록
# ---------------------------------------------------------------------------


def git_result(project: Path, *args: str, timeout: float = 30) -> tuple[str | None, str]:
    """(stdout, 오류 첫 줄). 실패하면 stdout이 None."""
    try:
        result = subprocess.run(["git", *args], cwd=project, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    if result.returncode != 0:
        lines = result.stderr.decode("utf-8", errors="replace").strip().splitlines()
        return None, lines[0] if lines else f"git 종료 코드 {result.returncode}"
    return result.stdout.decode("utf-8", errors="replace"), ""


def git(project: Path, *args: str) -> str | None:
    return git_result(project, *args)[0]


NO_BASH_HINT = ("Windows에서 bash를 찾지 못해 cmd로 실행했다. 검증 명령은 bash 문법(`./gradlew`, `&&`, `/` 경로)으로 쓰므로 "
                "Git for Windows를 설치하거나 환경 변수 CLAUDE_CODE_GIT_BASH_PATH에 bash.exe 경로를 정한다")


def verify_shell(os_name: str | None = None, env=None, which=shutil.which) -> str | None:
    """검증 명령을 실행할 bash 경로(Windows만). POSIX면 None으로 기본 셸(/bin/sh)을 쓴다(#115).

    Claude Code는 Windows에서 hook을 Git Bash로 실행하고, 팀의 검증 명령(`cd backend && ./gradlew build`)도 bash 문법이다.
    cmd.exe(`shell=True`)로는 실행되지 않는다. 찾는 순서: `CLAUDE_CODE_GIT_BASH_PATH`(Claude Code와 같은 변수),
    git 설치 위치의 `bin/bash.exe`, PATH의 bash. `System32\\bash.exe`는 WSL 실행기라 프로젝트 경로가 달라 쓰지 않는다.
    찾지 못하면 None(cmd로 실행하고 실패 출력에 NO_BASH_HINT를 붙인다).
    """
    if (os_name or os.name) != "nt":
        return None
    env = os.environ if env is None else env
    candidates = []
    if env.get("CLAUDE_CODE_GIT_BASH_PATH"):
        candidates.append(Path(env["CLAUDE_CODE_GIT_BASH_PATH"]))
    git_exe = which("git")
    if git_exe:  # ...\Git\cmd\git.exe 또는 ...\Git\mingw64\bin\git.exe → ...\Git\bin\bash.exe
        parents = Path(git_exe).resolve().parents
        candidates += [parents[level] / "bin" / "bash.exe" for level in (1, 2) if len(parents) > level]
    bash = which("bash")
    if bash:
        candidates.append(Path(bash))
    for candidate in candidates:
        parts = {part.lower() for part in candidate.parts}
        # 이름이 bash.exe인 파일만(cmd.exe 등을 잘못 주면 모든 검증이 통과로 보인다), WSL 실행기(System32·WindowsApps)는
        # 프로젝트를 /mnt/c 아래에서 리눅스로 실행하므로 쓰지 않는다(#115 리뷰 A F1·B F3)
        if candidate.name.lower() == "bash.exe" and not parts & {"system32", "windowsapps"} and candidate.is_file():
            return str(candidate)
    return None


VERIFY_CMD_ENV = "HARNESS_VERIFY_CMD"


def shell_command(cmd: str, bash: str | None) -> tuple[list[str] | str, bool, dict | None]:
    """(Popen 인자, shell 여부, 환경). bash가 있으면 명령을 환경 변수로 넘겨 `bash -c 'eval ...'`로 실행한다.
    명령줄 인자로 넘기면 Windows 인자 따옴표 규칙과 MSYS 해석이 겹쳐 `\\\\`가 `\\`로 바뀐다(#115 리뷰 B F2).
    bash가 없으면 기본 셸(shell=True)."""
    if not bash:
        return cmd, True, None
    return [bash, "-c", f'eval "${VERIFY_CMD_ENV}"'], False, {**os.environ, VERIFY_CMD_ENV: cmd}


def git_state_path(project: Path, name: str) -> Path | None:
    """worktree마다 따로 두는 git 내부 경로(`.git/harness/<name>`). git 저장소가 아니면 None."""
    out = git(project, "rev-parse", "--git-path", f"harness/{name}")
    if not out:
        return None
    path = Path(out.strip())
    return path if path.is_absolute() else project / path


def current_branch(project: Path) -> str | None:
    """현재 브랜치 이름. detached HEAD나 git 오류면 None.

    첫 커밋 전(unborn HEAD)에도 이름을 내도록 `rev-parse --abbrev-ref` 대신 `symbolic-ref`를 쓴다.
    """
    out = git(project, "symbolic-ref", "-q", "--short", "HEAD")
    name = out.strip() if out else ""
    return name or None


APPEND_LOCK_WAIT_SEC = 5


@contextlib.contextmanager
def _file_lock(path: Path):
    """프로세스 간 배타 잠금(`<path>.lock`의 첫 바이트). Windows의 append는 끝으로 옮긴 뒤 쓰는 두 단계라
    동시에 쓰면 줄이 덮여 사라진다(#96). 시간 안에 잠그지 못하면 OSError."""
    with open(f"{path}.lock", "a+b") as handle:
        deadline = time.monotonic() + APPEND_LOCK_WAIT_SEC
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def append_jsonl(project: Path, name: str, record: dict) -> None:
    """`.git/harness/<name>`에 시각·브랜치를 붙인 한 줄을 추가한다. 실패해도 hook 동작에 영향을 주지 않는다.

    여러 hook·세션이 같은 파일에 동시에 쓸 수 있어 잠금 안에서 한 번의 `write`로 쓴다.
    """
    try:
        path = git_state_path(project, name)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        line = {"ts": stamp, "branch": current_branch(project), **record}
        data = (json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8")
        with _file_lock(path), path.open("ab") as handle:
            handle.write(data)
    except Exception:  # noqa: BLE001 - 기록은 최선 노력
        pass


def log_event(project: Path, record: dict) -> None:
    """판정 기록 한 줄(`events.jsonl`).

    `branch`는 `harness events summary`가 MR 본문용 요약을 브랜치별로 고르는 데 쓴다.
    """
    append_jsonl(project, "events.jsonl", record)


SESSION_LOG = "sessions.jsonl"  # 세션 사건 기록. 기준선 SESSIONS_FILE(sessions.json)과 다른 파일이다
SESSION_TOOL = "claude-code"


USAGE_FIELDS = ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read")
TRANSCRIPT_MAX_BYTES = 200_000_000


def _tokens(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _message_usage(usage: dict) -> dict:
    """API usage 객체 → 다섯 토큰 수. 캐시 쓰기는 5분·1시간으로 나누고, 나눔이 없으면 5분으로 본다."""
    split = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    write_5m, write_1h = _tokens(split.get("ephemeral_5m_input_tokens")), _tokens(split.get("ephemeral_1h_input_tokens"))
    if not write_5m and not write_1h:
        write_5m = _tokens(usage.get("cache_creation_input_tokens"))
    return {"input": _tokens(usage.get("input_tokens")), "output": _tokens(usage.get("output_tokens")),
            "cache_write_5m": write_5m, "cache_write_1h": write_1h,
            "cache_read": _tokens(usage.get("cache_read_input_tokens"))}


def transcript_usage(transcript) -> tuple[dict | None, str | None]:
    """세션 transcript(와 서브에이전트 transcript)의 모델별 누적 토큰(#97). (usage, 오류 사유).

    스트리밍 블록마다 같은 `message.id`의 usage가 반복되므로 id별 마지막 값만 센다. 서브에이전트는
    `<transcript에서 .jsonl을 뺀 경로>/subagents/*.jsonl`이다. 읽기만 하고, 형식이 바뀌어 읽지 못해도 예외를 내지 않는다.
    메인을 읽지 못하면 usage가 None이고, 서브에이전트 파일만 못 읽으면 나머지 합과 함께 사유(`*_subagent`)를 낸다.
    """
    if not isinstance(transcript, str) or not transcript:
        return None, "no_transcript"
    main = Path(transcript)
    if main.suffix != ".jsonl" or not main.is_file():
        return None, "not_found"
    files = [main]
    subagents = main.with_suffix("") / "subagents"
    if subagents.is_dir():
        files += sorted(subagents.glob("*.jsonl"))
    messages: dict[str, tuple[str, dict]] = {}
    skipped = None
    for path in files:
        try:
            if path.stat().st_size > TRANSCRIPT_MAX_BYTES:
                if path == main:
                    return None, "too_large"
                skipped = "too_large_subagent"  # 그 파일만 빼고 센다. 요약이 일부만 셌다고 알린다(#97 리뷰 B F1)
                continue
            with path.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if '"usage"' not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(record, dict) or record.get("type") != "assistant":
                        continue
                    message = record.get("message")
                    if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
                        continue
                    key = message.get("id") or record.get("uuid")
                    model = message.get("model") if isinstance(message.get("model"), str) else "unknown"
                    if isinstance(key, str) and key:
                        messages[key] = (model, message["usage"])
        except OSError:
            if path == main:
                return None, "unreadable"
            skipped = "unreadable_subagent"
    totals: dict[str, dict] = {}
    for model, usage in messages.values():
        counts = _message_usage(usage)
        bucket = totals.setdefault(model, {field: 0 for field in USAGE_FIELDS})
        for field in USAGE_FIELDS:
            bucket[field] += counts[field]
    return totals, skipped


def log_session_usage(project: Path, event: str, payload: dict, **fields) -> None:
    """세션 사건 한 줄에 transcript 누적 토큰(`usage`)이나 읽지 못한 사유(`usage_error`)를 붙인다(#97)."""
    try:
        usage, error = transcript_usage(payload.get("transcript_path"))
    except Exception:  # noqa: BLE001 - 계산 실패가 hook 결과를 바꾸지 않는다
        usage, error = None, "error"
    log_session(project, event, payload.get("session_id"), usage=usage, usage_error=error, **fields)


def log_session(project: Path, event: str, session, **fields) -> None:
    """세션 사건 한 줄(`start`·`stop`·`end`). 프롬프트·코드 내용은 쓰지 않는다(#96)."""
    try:
        record = {"event": event, "session": session if isinstance(session, str) else None, "tool": SESSION_TOOL}
        head = git(project, "rev-parse", "--verify", "-q", "HEAD")
        record["head"] = head.strip() if head else None
        record.update({key: value for key, value in fields.items() if value is not None})
        append_jsonl(project, SESSION_LOG, record)
    except Exception:  # noqa: BLE001 - 기록은 최선 노력
        pass


SESSIONS_FILE = "sessions.json"
BASELINE_MAX_AGE_DAYS = 30
BASELINE_MAX_ENTRIES = 200


def read_baselines(project: Path) -> dict:
    """세션별 시작 기준선(`.git/harness/sessions.json`). 없거나 읽을 수 없으면 빈 객체."""
    try:
        path = git_state_path(project, SESSIONS_FILE)
        data = json.loads(path.read_text(encoding="utf-8")) if path and path.is_file() else {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _baseline_time(entry: dict) -> datetime.datetime:
    try:
        stamp = datetime.datetime.fromisoformat(entry["ts"])
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=datetime.timezone.utc)
    except (KeyError, TypeError, ValueError):
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)


def record_baseline(project: Path, session: str, now: datetime.datetime | None = None) -> None:
    """세션 시작 `HEAD`를 기준선으로 남긴다. 같은 세션이 이미 있으면 덮지 않는다(resume·compact).

    커밋이 없는 저장소면 `head`는 null이다. 오래된 항목과 상한을 넘는 항목은 지운다.
    실패해도 세션에 영향을 주지 않는다. 동시에 쓰면 한쪽 기록이 빠질 수 있고, 그 세션은 기준선 없이 동작한다.
    """
    try:
        path = git_state_path(project, SESSIONS_FILE)
        if path is None:
            return
        data = read_baselines(project)
        if isinstance(data.get(session), dict):
            return
        now = now or datetime.datetime.now(datetime.timezone.utc)
        head = git(project, "rev-parse", "--verify", "-q", "HEAD")
        data[session] = {"head": head.strip() if head else None, "ts": now.isoformat(timespec="seconds")}
        cutoff = now - datetime.timedelta(days=BASELINE_MAX_AGE_DAYS)
        kept = sorted(((key, entry) for key, entry in data.items()
                       if isinstance(entry, dict) and _baseline_time(entry) >= cutoff),
                      key=lambda item: _baseline_time(item[1]), reverse=True)[:BASELINE_MAX_ENTRIES]
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            temp.write_text(json.dumps(dict(kept), ensure_ascii=False), encoding="utf-8")
            os.replace(temp, path)  # Windows에서는 다른 프로세스가 읽는 중이면 실패한다
        finally:
            temp.unlink(missing_ok=True)  # 교체에 실패한 임시 파일이 쌓이지 않게
    except Exception:  # noqa: BLE001 - 기준선은 최선 노력
        pass


def decode_output(data: bytes) -> str:
    """명령 출력. UTF-8이 아니면 시스템 로캘 인코딩(한국어 Windows의 cp949 등)으로 읽는다."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(locale.getpreferredencoding(False) or "utf-8", errors="replace")


def read_payload() -> dict:
    data = sys.stdin.buffer.read().decode("utf-8-sig")  # BOM이 붙은 입력(PowerShell 파이프 등)도 읽는다
    payload = json.loads(data) if data.strip() else {}
    if not isinstance(payload, dict):
        raise ValueError("hook 입력은 JSON 객체여야 한다")
    return payload


def setup_streams() -> None:
    for stream in (sys.stdout, sys.stderr):  # Windows 콘솔에서도 한국어 메시지를 깨지지 않게 출력한다
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
