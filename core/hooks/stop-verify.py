#!/usr/bin/env python3
"""Stop hook: 응답을 끝내기 전에 바뀐 영역의 검증 명령을 실행한다.

- 검증 대상은 작업 트리의 미커밋 변경과, 세션 시작 기준선(session-start.py) 이후 커밋된 변경이다.
  기준선이 없으면(이전 설치, 세션 ID 없음, git이 모르는 커밋) 미커밋 변경만 본다. 둘 다 없으면 아무것도 하지 않는다.
- 변경 파일이 속한 영역의 `areas[].verify`, 영역 밖 변경이면 `hooks.stop_verify`를 실행한다.
- 직전 확인과 변경 상태가 같으면 다시 실행하지 않는다. 그 상태가 실패였다면 보류하지 않고
  사용자에게 한 번 알린다(세션과 무관한 기존 변경 때문에 매 턴 보류되지 않게).
- 실패하면 한 번 종료를 보류하고(종료 코드 2) 결과를 모델에 넘긴다. 이미 보류한 뒤라면
  (`stop_hook_active`) 다시 보류하지 않고 사용자에게 경고만 남긴다.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.dont_write_bytecode = True  # 대상 저장소의 .claude/hooks/에 __pycache__를 남기지 않는다
import harness_common as common  # noqa: E402

TAIL_LINES = 40
KILL_GRACE_SEC = 5


def notify(message: str) -> None:
    """종료를 막지 않고 사용자에게 보이는 알림."""
    print(json.dumps({"systemMessage": message}, ensure_ascii=False))


def parse_porcelain(out: str) -> list[str]:
    """`status --porcelain=v1 -z` 출력의 경로. 이름 변경은 새 경로와 원래 경로를 둘 다 넣는다."""
    tokens = out.split("\0")
    paths, i = [], 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        if len(token) < 4:
            continue
        paths.append(token[3:])
        if token[0] in "RC" and i < len(tokens):  # 이름 변경은 다음 토큰이 원래 경로다
            paths.append(tokens[i])
            i += 1
    return paths


def changed_files(project: Path) -> tuple[list[str] | None, str]:
    """프로젝트 기준 변경 경로 목록과 오류. 프로젝트가 git 최상위의 하위 디렉터리여도 맞게 바꾼다."""
    top, error = common.git_result(project, "rev-parse", "--show-toplevel")
    if top is None:
        return None, error
    out, error = common.git_result(project, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if out is None:
        return None, error
    toplevel = Path(top.strip()).resolve()
    files = set()
    for path in parse_porcelain(out):
        try:
            rel = (toplevel / path.rstrip("/")).relative_to(project).as_posix()
        except ValueError:
            continue  # 프로젝트 밖의 변경은 이 프로젝트의 검증 대상이 아니다
        if rel not in ("", "."):
            files.add(rel)
    return sorted(files), ""


def committed_files(project: Path, session) -> list[str]:
    """세션 기준선 이후 커밋된 프로젝트 기준 경로. 기준선이 없거나 git이 비교하지 못하면 빈 목록.
    이름 변경은 옛 경로와 새 경로를 둘 다 넣는다(`--no-renames`)."""
    if not isinstance(session, str) or not session:
        return []
    entry = common.read_baselines(project).get(session)
    if not isinstance(entry, dict) or "head" not in entry:
        return []
    base = entry["head"]
    if base is None:  # 커밋이 없던 저장소에서 시작했다. 지금 HEAD의 모든 파일이 이 세션의 커밋이다
        out, _ = common.git_result(project, "-c", "core.quotepath=off", "ls-tree", "-r", "-z", "--name-only", "HEAD")
    elif isinstance(base, str) and base:
        # --relative는 프로젝트 밖 변경을 빼고 프로젝트 기준 경로를 낸다
        out, _ = common.git_result(project, "-c", "core.quotepath=off", "diff", "--name-only", "--no-renames",
                                   "--relative", "-z", base, "HEAD", "--")
    else:
        return []
    return [path for path in (out or "").split("\0") if path]


commands_for = common.commands_for  # CI 검증 스크립트도 같은 선택을 쓴다(#100)


MAX_NEST_DEPTH = 8


class StateUnknown(Exception):
    """변경 상태를 확정하지 못했다. 이 상태로는 캐시를 읽지도 쓰지도 않는다."""


class BudgetExceeded(StateUnknown):
    """상태 해시를 시간 예산 안에 끝내지 못했다."""


def remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise BudgetExceeded
    return left


def hash_file(digest, path: Path, deadline: float) -> None:
    remaining(deadline)
    try:
        if path.is_file():
            digest.update(path.read_bytes())
        else:
            digest.update(b"<deleted>")
    except OSError:
        digest.update(b"<unreadable>")


def hash_repo(digest, repo: Path, deadline: float, depth: int = 0) -> None:
    """중첩 저장소·하위 모듈을 그 저장소의 HEAD와 변경 파일로 해시한다. ignore된 파일과 바뀌지 않은
    파일은 읽지 않는다. 안쪽의 중첩 저장소도 같은 방식으로 따라간다. git 호출도 예산 안에서 한다."""
    head, _ = common.git_result(repo, "rev-parse", "HEAD", timeout=min(30, remaining(deadline)))
    digest.update((head or "<none>").encode("utf-8"))  # 커밋이 없는 저장소는 HEAD가 없다
    out, error = common.git_result(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all",
                                   timeout=min(30, remaining(deadline)))
    if out is None:
        remaining(deadline)  # 시간 초과로 실패했으면 예산 초과로 본다
        raise StateUnknown(f"{repo}: {error}")
    for name in sorted(set(parse_porcelain(out))):
        digest.update(b"\0nested\0" + name.encode("utf-8") + b"\0")
        path = repo / name.rstrip("/")
        if path.is_dir():
            if depth >= MAX_NEST_DEPTH:  # 한도 너머는 내용을 반영할 수 없으므로 캐시하지 않는다
                raise StateUnknown(f"{path}: 중첩 저장소가 {MAX_NEST_DEPTH}단계를 넘는다")
            hash_repo(digest, path, deadline, depth + 1)
        else:
            hash_file(digest, path, deadline)


def state_hash(project: Path, files: list[str], commands: list[str], deadline: float) -> str:
    digest = hashlib.sha256()
    digest.update((common.git(project, "rev-parse", "HEAD") or "<no-head>").encode())
    for cmd in commands:
        digest.update(b"\0cmd\0" + cmd.encode("utf-8"))
    for rel in files:
        digest.update(b"\0file\0" + rel.encode("utf-8") + b"\0")
        path = project / rel
        if path.is_dir():  # 하위 모듈·중첩 저장소
            hash_repo(digest, path, deadline)
        else:
            hash_file(digest, path, deadline)
    return digest.hexdigest()


def kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()


def run(cmd: str, cwd: Path, timeout: float) -> tuple[int | None, str]:
    """(종료 코드, 출력). 시간 초과면 종료 코드 None.

    출력은 파이프 대신 임시 파일로 받는다. 명령이 남긴 후손 프로세스가 파이프를 쥐고 있어도
    셸이 끝나면 바로 돌아오고, 시간 초과를 넘겨 기다리지 않는다.
    """
    kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" \
        else {"start_new_session": True}
    with tempfile.TemporaryFile() as out:
        proc = subprocess.Popen(cmd, shell=True, cwd=cwd, stdin=subprocess.DEVNULL,
                                stdout=out, stderr=subprocess.STDOUT, **kwargs)
        try:
            code = proc.wait(timeout=max(timeout, 0.1))
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            try:
                proc.wait(timeout=KILL_GRACE_SEC)
            except subprocess.TimeoutExpired:
                pass
            code = None
        out.seek(0)
        return code, common.decode_output(out.read())


def tail(text: str) -> str:
    lines = text.rstrip().splitlines()
    shown = lines[-TAIL_LINES:]
    head = [f"... (앞 {len(lines) - len(shown)}줄 생략)"] if len(lines) > len(shown) else []
    return "\n".join(head + shown)


def read_cache(path: Path | None) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path else {}
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_cache(path: Path | None, data: dict) -> None:
    try:
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def verify(payload: dict, project: Path, outcome: dict) -> int:
    """종료 검증 본체. `outcome`에 세션 기록용 결과(`verify`, `files`)를 채운다."""
    try:
        config = common.load_config(project)
    except common.ConfigError as exc:
        print(f"harness stop-verify: {common.CONFIG_NAME}를 읽지 못해 종료 검증이 꺼진 상태다: {exc}",
              file=sys.stderr)
        outcome["verify"] = "error"
        return 1
    files, error = changed_files(project)
    if files is None:
        notify(f"harness stop-verify: git 상태를 읽지 못해 종료 검증을 건너뛴다: {error}")
        outcome["verify"] = "error"
        return 0
    files = sorted(set(files) | set(committed_files(project, payload.get("session_id"))))
    commands = commands_for(config, files)
    outcome["files"] = len(files)
    if not commands:
        return 0
    deadline = time.monotonic() + common.MAX_STOP_TIMEOUT  # 해시와 명령을 합쳐 hook timeout 안에서 끝낸다

    cache_path = common.git_state_path(project, "stop-verify.json")
    cache = read_cache(cache_path)
    try:
        current = state_hash(project, files, commands, deadline)
    except StateUnknown:
        current = None  # 상태를 확정하지 못했으므로 캐시를 읽지도 쓰지도 않는다
    if current is not None and cache.get("state") == current:
        outcome["verify"] = "cached-pass" if cache.get("ok") else "cached-fail"
        if not cache.get("ok") and not cache.get("notified"):
            notify("harness stop-verify: 직전 확인과 같은 상태에서 검증이 실패한 채다. "
                   "다시 보류하지 않는다. 사람이 확인한다.\n\n" + str(cache.get("report", "")))
            write_cache(cache_path, {**cache, "notified": True})
        return 0

    per_command = (config.get("hooks") or {}).get("stop_timeout_sec", common.DEFAULT_STOP_TIMEOUT)
    failures = []
    for cmd in commands:
        left = deadline - time.monotonic()
        if left <= 0:
            failures.append((cmd, None, "(전체 시간 예산을 넘어 실행하지 않았다)"))
            current = None  # 명령 자체의 결과가 아니므로 캐시하지 않는다
            continue
        code, output = run(cmd, project, min(per_command, left))
        if code is None and left < per_command:
            current = None  # 전체 예산에 잘려 끝나지 못했다. 다음에 다시 시도한다
        if code != 0:
            failures.append((cmd, code, output))

    outcome["verify"] = "fail" if failures else "pass"
    if not failures:
        if current is not None:
            write_cache(cache_path, {"state": current, "ok": True})
        return 0

    active = bool(payload.get("stop_hook_active"))
    common.log_event(project, {
        "hook": "stop-verify", "decision": "fail", "commands": [cmd for cmd, _, _ in failures],
        "retry": active, "session": payload.get("session_id"),
    })
    report = []
    for cmd, code, output in failures:
        status = "시간 초과" if code is None else f"종료 코드 {code}"
        report.append(f"$ {cmd}  ({status})\n{tail(output)}")
    body = "\n\n".join(report)
    if current is not None:
        write_cache(cache_path, {"state": current, "ok": False, "notified": active, "report": body})
    if active:
        notify("harness stop-verify: 검증이 여전히 실패한다. 다시 보류하지 않고 종료한다. "
               "사람이 확인한다.\n\n" + body)
        return 0
    print("세션 종료 전 검증이 실패했다. 원인을 고친 뒤 다시 끝낸다. "
          "고칠 수 없으면 이유를 사용자에게 보고한다.\n\n" + body, file=sys.stderr)
    return 2


def main() -> int:
    common.setup_streams()
    try:
        payload = common.read_payload()
    except ValueError as exc:
        print(f"harness stop-verify: 입력을 읽지 못해 종료 검증을 건너뛴다: {exc}", file=sys.stderr)
        return 1
    project = common.project_dir(payload)
    outcome = {"verify": "none"}
    try:
        return verify(payload, project, outcome)
    finally:  # 응답마다 한 줄(#96)과 누적 토큰(#97). 기록은 최선 노력이라 종료 코드와 출력에 영향을 주지 않는다
        common.log_session_usage(project, "stop", payload, verify=outcome["verify"],
                                 files=outcome.get("files"), retry=bool(payload.get("stop_hook_active")))


if __name__ == "__main__":
    sys.exit(main())
