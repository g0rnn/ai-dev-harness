#!/usr/bin/env python3
"""MR 변경 영역의 검증 명령을 CI에서 실행한다(#100). 표준 라이브러리만 쓴다.

종료 검증 hook(stop-verify)과 같은 선택(`harness_common.commands_for`)을 쓴다: 변경 파일이 속한 영역의
`areas[].verify`, 영역 밖 변경이면 `hooks.stop_verify`. hooks가 없는 도구(Codex 등)로 만든 MR도 같은 검증을 거친다.

    python verify_changed.py --base <ref> [--project .] [--common .claude/hooks/harness_common.py]

변경 파일은 `<base>`와 HEAD의 merge-base 이후 커밋이다(작업 트리는 보지 않는다).
종료 코드: 모두 통과 0(실행할 명령이 없어도 0), 하나라도 실패 1, 입력·설정·git 오류 2.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True
DEFAULT_COMMON = ".claude/hooks/harness_common.py"


class VerifyError(Exception):
    """입력·설정·git 오류(종료 2)."""


def load_common(path: Path):
    if not path.is_file():
        raise VerifyError(f"{path} 이 없다. `harness init`으로 생성한다.")
    spec = importlib.util.spec_from_file_location("harness_common_for_verify", path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - 복사본이 깨졌으면 원인을 보이고 종료 2(#100 리뷰 B F3)
        raise VerifyError(f"{path} 을 읽을 수 없다: {exc}") from exc
    for name in ("commands_for", "load_config", "git_result"):
        if not hasattr(module, name):
            raise VerifyError(f"{path} 에 {name} 이 없다. 키트 버전을 맞춘다(`harness init --force`).")
    return module


def changed_files(common, project: Path, base: str) -> list[str]:
    if base.startswith("-"):
        raise VerifyError(f"기준 ref가 잘못됐다: {base}")
    merge_base, err = common.git_result(project, "merge-base", base, "HEAD")
    if merge_base is None:
        raise VerifyError(f"기준 {base}와 HEAD의 merge-base를 구할 수 없다(얕은 clone이면 GIT_DEPTH=0): {err}")
    out, err = common.git_result(project, "-c", "core.quotepath=off", "diff", "--name-only", "--no-renames",
                                 "--relative", "-z", merge_base.strip(), "HEAD", "--")
    if out is None:
        raise VerifyError(f"git diff 실패: {err}")
    return [path for path in out.split("\0") if path]


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MR 변경 영역의 검증 명령을 실행한다")
    parser.add_argument("--base", required=True, help="대상 브랜치 ref(예: origin/main)")
    parser.add_argument("--project", default=".", help="저장소 루트(기본 현재 디렉터리)")
    parser.add_argument("--common", help=f"harness_common.py 경로(기본 <project>/{DEFAULT_COMMON})")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # Windows 파이프에서도 한국어·명령 문자열을 깨지지 않게
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    project = Path(args.project).resolve()
    try:
        common = load_common(Path(args.common) if args.common else project / DEFAULT_COMMON)
        try:
            config = common.load_config(project)
        except common.ConfigError as exc:
            raise VerifyError(str(exc)) from exc
        files = changed_files(common, project, args.base)
    except VerifyError as exc:
        print(f"harness verify: {exc}", file=sys.stderr)
        return 2
    commands = common.commands_for(config, files)
    bash = common.verify_shell() if hasattr(common, "verify_shell") else None
    print(f"harness verify: 변경 파일 {len(files)}개, 실행할 검증 명령 {len(commands)}개")
    failed = []
    for cmd in commands:
        print(f"$ {cmd}", flush=True)
        args, shell, env = common.shell_command(cmd, bash) if bash else (cmd, True, None)
        if subprocess.run(args, shell=shell, cwd=project, env=env).returncode != 0:
            failed.append(cmd)
    for cmd in failed:
        print(f"harness verify: 실패: {cmd}", file=sys.stderr)
    if failed and os.name == "nt" and bash is None and hasattr(common, "NO_BASH_HINT"):  # #115 리뷰 A F2
        print(f"harness verify: {common.NO_BASH_HINT}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run())
