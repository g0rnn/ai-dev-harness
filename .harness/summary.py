#!/usr/bin/env python3
"""MR 본문 기록 요약(#123). 표준 라이브러리만 쓴다.

키트의 `harness events|sessions|agent|review summary`와, `init`이 대상 저장소에 넣는 `.harness/summary.py`가 같은 코드를
쓴다. 대상 저장소에서는 키트 없이 실행한다(MR을 만드는 사람·AI가 작업한 worktree에서):

    python .harness/summary.py agent      # MR 본문 `## 에이전트 기록`
    python .harness/summary.py events     # MR 본문 `## hooks 기록`
    python .harness/summary.py sessions   # 사람이 읽는 세션 요약
    python .harness/summary.py review plans/<키>-review.md --save   # MR 본문 `## 리뷰 결과` 두 줄, 최종 리뷰 저장

기록은 이 PC에만 있다(`.git/harness/`의 hooks 기록, `CODEX_HOME`의 Codex 세션 로그). 출력은 표준 출력이고 파일을 바꾸지 않는다.
예외는 `review --save`로, 표 검사를 통과한 리뷰 파일을 커밋할 `docs/reviews/<키>.md`에 그대로 쓴다(#135).
대상 저장소에서는 `.harness/metrics/collect.py`와 `.claude/hooks/harness_common.py`를 읽는다(둘 다 `init`이 넣는다).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

CONFIG_NAME = "harness.json"


class SummaryError(Exception):
    """입력·저장소 오류(종료 2)."""


def current_branch(common, target: Path, branch: str | None) -> str:
    branch = branch or common.current_branch(target)
    if not branch:
        raise SummaryError("현재 브랜치를 알 수 없다(git 저장소가 아니거나 detached HEAD). --branch 로 지정한다")
    return branch


def events_report(common, collect, target: Path, branch: str | None) -> tuple[str, list[str]]:
    """(`## hooks 기록` 절, 제외 안내). 이 worktree의 hooks 기록(`events.jsonl`)에서 브랜치의 사건을 센다."""
    branch = current_branch(common, target, branch)
    path = common.git_state_path(target, "events.jsonl")
    if path is None:
        raise SummaryError(f"git 저장소가 아니다: {target}")
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.is_file() else []
    except OSError as exc:
        raise SummaryError(f"hooks 기록을 읽을 수 없다: {path}: {exc}") from exc
    summary = collect.summarize_events(lines, branch)
    notes = []
    if summary["legacy"]:
        notes.append(f"브랜치가 없는 옛 기록 {summary['legacy']}줄")
    if summary["malformed"]:
        notes.append(f"읽을 수 없는 줄 {summary['malformed']}줄")
    return collect.hooks_markdown(summary), notes


def session_logs(common, target: Path) -> list[Path]:
    """같은 저장소의 모든 worktree 세션 기록. 세션은 띄운 checkout에 기록되므로 다른 worktree에서 한 커밋도
    여기서 찾는다(#96 리뷰 B F1). 공용 git 디렉터리의 `harness/`와 `worktrees/*/harness/`를 본다."""
    common_dir = common.git(target, "rev-parse", "--path-format=absolute", "--git-common-dir") \
        or common.git(target, "rev-parse", "--git-common-dir")  # git 2.31 전에는 --path-format이 없다
    if not common_dir:
        return []
    root = Path(common_dir.strip())
    root = root if root.is_absolute() else target / root
    paths = [root / "harness" / common.SESSION_LOG]
    worktrees = root / "worktrees"
    if worktrees.is_dir():
        paths += sorted(path / "harness" / common.SESSION_LOG for path in worktrees.iterdir())
    return [path for path in paths if path.is_file()]


def worktree_paths(common, target: Path) -> list[str]:
    """같은 저장소의 worktree 경로(첫 항목이 기본 checkout). Codex 세션을 이 저장소 것으로 고를 때 쓴다(#116)."""
    out = common.git(target, "worktree", "list", "--porcelain") or ""
    return [line[len("worktree "):] for line in out.splitlines() if line.startswith("worktree ")]


def branch_timelines(common, collect, roots: list[str]) -> dict[str, list]:
    """worktree마다 HEAD reflog의 브랜치 이동(Codex 사건을 그 시각의 브랜치에 넣는다, #116)."""
    timelines = {}
    for root in roots:
        out = common.git(Path(root), "reflog", "show", "--date=iso-strict", "--format=%gd%x09%gs", "HEAD")
        timelines[root] = collect.branch_timeline((out or "").splitlines())
    return timelines


def branch_commits(common, collect, target: Path, base: str) -> list[tuple] | None:
    """base와 HEAD의 merge-base 이후 이 브랜치 커밋(sha, committer 시각). 구하지 못하면 None."""
    merge_base = common.git(target, "merge-base", base, "HEAD")
    if not merge_base:
        return None
    out = common.git(target, "log", "--format=%H %cI", f"{merge_base.strip()}..HEAD")
    if out is None:
        return None
    commits = []
    for line in out.splitlines():
        sha, _, stamp = line.partition(" ")
        moment = collect.parse_ts(stamp)
        if sha and moment is not None:
            commits.append((sha, moment))
    return commits


def session_report(common, collect, target: Path, branch: str | None, base: str | None, config: dict,
                   warn=lambda text: print(text, file=sys.stderr)) -> tuple[dict, int | None, int | None, dict]:
    """(요약, 기록 없는 커밋 수, 비교한 커밋 수, 단가). `sessions`와 `agent`가 같이 쓴다. config는 읽어 검사한 `harness.json`."""
    asked = branch
    branch = current_branch(common, target, branch)
    if common.git_state_path(target, common.SESSION_LOG) is None:
        raise SummaryError(f"git 저장소가 아니다: {target}")
    lines: list[str] = []
    for path in session_logs(common, target):
        try:
            lines += path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            raise SummaryError(f"세션 기록을 읽을 수 없다: {path}: {exc}") from exc
    # Codex는 hooks가 없으므로 자기 세션 로그(`CODEX_HOME/sessions`)에서 이 저장소의 세션을 읽어 같이 센다(#116)
    roots = worktree_paths(common, target)
    lines += [json.dumps(event) for _, event in collect.CodexSessions().records(roots, branch_timelines(common, collect, roots))]
    summary = collect.summarize_sessions(lines, branch)
    commits = None
    if not asked or asked == common.current_branch(target):  # 커밋 비교는 체크아웃한 브랜치만
        base = base or f"origin/{config.get('integration_branch') or config.get('default_branch') or 'main'}"
        commits = branch_commits(common, collect, target, base)
        if commits is None:
            warn(f"기록 없는 커밋은 세지 않았다: 기준 {base}와 merge-base를 구할 수 없다")
    unrecorded = len(collect.unrecorded_commits(commits, summary["intervals"])) if commits is not None else None
    if summary["malformed"]:
        warn(f"제외: 읽을 수 없는 줄 {summary['malformed']}줄")
    return summary, unrecorded, None if commits is None else len(commits), collect.merged_pricing(config)


FINAL_REVIEW_NAME = re.compile(r"^(.+)-review\.md$", re.IGNORECASE)


def final_review_path(collect, path: Path, root: Path) -> Path:
    """`plans/<키>-review.md` → `<root>/docs/reviews/<키>.md`(#135). 엄격 A·B 원본(`-review-A.md` 등)은 저장하지 않는다."""
    match = FINAL_REVIEW_NAME.match(path.name)
    key = match.group(1) if match else None
    if key is None or collect.review_file_key(f"{collect.REVIEWS_DIR}/{key}.md") is None:
        raise SummaryError(f"--save는 최종 리뷰 파일(`<키>-review.md`, 엄격이면 합집합 대조표)만 저장한다: {path.name}")
    return root / collect.REVIEWS_DIR / f"{key}.md"


def repo_root(path: Path) -> Path:
    """리뷰 파일이 든 저장소(worktree)의 루트. 실행한 디렉터리와 상관없이 그 저장소의 `docs/reviews/`에 쓰게 한다(#135 리뷰 F3)."""
    try:
        out = subprocess.run(["git", "-C", str(path.resolve().parent), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, encoding="utf-8", timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SummaryError(f"git을 실행할 수 없어 저장소 루트를 모른다: {exc}") from exc
    if out.returncode != 0 or not out.stdout.strip():
        raise SummaryError(f"--save는 git 저장소 안의 리뷰 파일만 저장한다: {path}")
    return Path(out.stdout.strip())


def review_report(collect, path: Path, save: bool = False, detail: bool = False,
                  note=lambda text: print(text, file=sys.stderr)) -> str:
    """리뷰 파일 7장(처리) 표 → `## 리뷰 결과`에 붙일 두 줄(detail이면 심각도·발견 세션 줄을 덧붙인다).

    save면 표 검사를 통과한 파일을 그대로 그 파일이 든 저장소의 `docs/reviews/<키>.md`에 쓰고 경로를 알린다(커밋할 최종 리뷰, #135)."""
    if not path.is_file():
        raise SummaryError(f"리뷰 파일이 없다: {path}")
    target = final_review_path(collect, path, repo_root(path)) if save else None
    try:
        data = path.read_bytes()
        text = data.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SummaryError(f"리뷰 파일을 읽을 수 없다: {path}: {exc}") from exc
    summary, errors = collect.summarize_review(text, detail=True)
    if summary is None or errors:
        raise SummaryError(f"{path}: " + "; ".join(errors))
    if target is not None and target.resolve() != path.resolve():
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        except OSError as exc:
            raise SummaryError(f"최종 리뷰 파일을 쓸 수 없다: {target}: {exc}") from exc
    if target is not None:
        note(f"최종 리뷰 파일을 썼다: {target} (MR·PR과 함께 커밋한다)")
    return collect.review_lines(summary) + (collect.review_detail_lines(summary) if detail else "")


# ---------------------------------------------------------------------------
# 대상 저장소의 `.harness/summary.py`로 실행할 때
# ---------------------------------------------------------------------------

def load_module(path: Path, name: str):
    if not path.is_file():
        raise SummaryError(f"{path} 이 없다. 키트 `harness init <저장소> --force`로 다시 생성한다")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - 복사본이 깨졌으면 원인을 보이고 종료 2
        # 복사본이 깨졌거나 함께 읽는 파일(collect.py의 classify_ci.py 등)이 없다(#123 리뷰 F6)
        raise SummaryError(f"{path} 을 읽을 수 없다: {exc}. 키트 `harness init <저장소> --force`로 다시 생성한다") from exc
    return module


def repo_config(common, collect, target: Path, warn) -> dict:
    """`harness.json`. 깨졌거나 요약에 쓰는 값(기준 브랜치·단가)이 틀리면 알리고 설정 전체를 버려 기본값(기준 origin/main,
    키트 단가)으로 요약한다(#96 리뷰 B F3). 키트 명령도 이 함수를 써서 같은 수치를 낸다(#123 리뷰 F3). 요약에 쓰지 않는
    키·플랫폼 등은 보지 않는다(키트의 다른 명령이 검사한다)."""
    source = target / CONFIG_NAME
    try:
        config = common.load_config(target)
        errors = collect.validate_pricing(config["pricing"], source) if "pricing" in config else []
        errors += [f"{source}: {key}는 비어 있지 않은 문자열이어야 한다"
                   for key in ("integration_branch", "default_branch")
                   if key in config and not (isinstance(config[key], str) and config[key].strip())]
    except common.ConfigError as exc:
        errors = [str(exc)]
    if errors:
        warn(f"{CONFIG_NAME}를 읽지 못해 기본값(기준 origin/main, 키트 단가)으로 요약한다: {errors[0]}")
        return {}
    return config


def main(argv: list[str] | None = None) -> int:
    sys.dont_write_bytecode = True  # 관리 디렉터리(`.harness/metrics/`·`.claude/hooks/`)에 __pycache__를 남기면 check가 여분으로 본다
    for stream in (sys.stdout, sys.stderr):  # Windows 콘솔·파이프에서도 한국어가 깨지지 않게
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="python .harness/summary.py",
                                     description="MR 본문 기록 요약(작업한 worktree에서 실행한다, 키트 없이)")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, text in (("agent", "MR 본문 `## 에이전트 기록` 절"), ("sessions", "사람이 읽는 세션 요약"),
                       ("events", "MR 본문 `## hooks 기록` 절")):
        command = sub.add_parser(name, help=text)
        command.add_argument("--branch", help="집계할 브랜치(기본 현재 브랜치)")
        if name != "events":
            command.add_argument("--base", help="기록 없는 커밋을 셀 기준 ref(기본 origin/<integration_branch>)")
    review = sub.add_parser("review", help="리뷰 파일 7장(처리) 표 → MR 본문 `## 리뷰 결과` 두 줄")
    review.add_argument("file", help="리뷰 파일(엄격이면 A·B를 묶은 종합 파일)")
    review.add_argument("--save", action="store_true",
                        help="검사를 통과하면 커밋할 최종 리뷰로 그 저장소의 docs/reviews/<키>.md에 쓴다")
    review.add_argument("--detail", action="store_true", help="두 줄 뒤에 심각도·발견 세션별 수를 덧붙인다(확인용)")
    args = parser.parse_args(argv)

    def warn(text: str) -> None:
        print(text, file=sys.stderr)

    harness_dir = Path(__file__).resolve().parent
    try:
        collect = load_module(harness_dir / "metrics" / "collect.py", "harness_summary_collect")
        if args.command == "review":
            sys.stdout.write(review_report(collect, Path(args.file), args.save, args.detail))
            return 0
        common = load_module(harness_dir.parent / ".claude" / "hooks" / "harness_common.py", "harness_summary_common")
        target = Path.cwd()
        if args.command == "events":
            text, notes = events_report(common, collect, target, args.branch)
            sys.stdout.write(text)
            if notes:
                warn(f"제외: {', '.join(notes)}")
            return 0
        config = repo_config(common, collect, target, warn)
        report = session_report(common, collect, target, args.branch, args.base, config, warn)
        render = collect.agent_markdown if args.command == "agent" else collect.sessions_text
        sys.stdout.write(render(*report))
        return 0
    except SummaryError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
