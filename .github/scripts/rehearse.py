"""리허설 대상에 hooks와 보안 검사를 실제로 돌려 본다(M1-8).

- hooks: `harness.py init`으로 만든 대상에 설치된 hook을 실제 hook처럼 stdin JSON으로 실행한다.
- security: 시나리오마다 PR merge 커밋 모양의 임시 저장소를 만들고 `run_fragment.py`로 조각을 실행한다.
  검출 시나리오는 종료 코드와 리포트의 기대 ID를 함께 본다. 도구 오류로 실패한 것을 "기대대로 실패"로
  읽으면 거짓 안심이 되기 때문이다. 가짜 Secret은 실행 중 난수로 만들고 출력·리포트에 원문이 없는지 본다.

검출 fixture는 시간이 지나도 결과가 바뀌지 않는 오래된 CVE로 고른다. 깨끗한 의존성 정상 시나리오는
새 CVE가 나오면 깨지므로 두지 않는다.

사용법: python .github/scripts/rehearse.py hooks|security [시나리오 이름 ...]
"""

import json
import os
import secrets
import shutil
import stat
import string
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
RUNNER = ROOT / ".github" / "scripts" / "run_fragment.py"
HARNESS = ROOT / "bin" / "harness.py"
# alpine 3.10.9. CVE-2021-36159(apk-tools, 고친 버전 있음)
IMAGE_FIXTURE = "alpine@sha256:451eee8bedcb2f029756dc3e9d73bab0e7943c1ac55cff3a4861c52a0fdd3e98"
TRIVY_CACHE = ".trivycache"
REPORTS = {
    "secret-detection": "gitleaks-report.json",
    "sast": "semgrep-report.json",
    "dependency-audit": "trivy-dependency.json",
    "image-scan": "trivy-image.json",
}


class RehearsalError(Exception):
    pass


def run(cmd, cwd=None, env=None, stdin=None, shell=False) -> subprocess.CompletedProcess:
    child_env = dict(os.environ if env is None else env)
    child_env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(cmd, cwd=cwd, env=child_env, shell=shell,
                          input=stdin, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def remove_tree(path: Path) -> None:
    def writable_remove(func, name, _error):
        os.chmod(name, stat.S_IWRITE)
        func(name)
    shutil.rmtree(path, onerror=writable_remove)


def remove_container_cache(repo: Path) -> None:
    """trivy 컨테이너가 root 소유로 만든 캐시는 같은 Docker 마운트에서 지운다."""
    if not (repo / TRIVY_CACHE).is_dir():
        return
    result = run(["docker", "run", "--rm", "-v", f"{repo}:/work", "--entrypoint", "sh",
                  IMAGE_FIXTURE, "-c", "rm -rf /work/.trivycache"])
    if result.returncode != 0:
        raise RehearsalError(f"컨테이너 캐시 정리 실패: {result.stderr.strip()[:200]}")


def git(repo: Path, *args: str) -> str:
    result = run(["git", "-C", str(repo), *args])
    if result.returncode != 0:
        raise RehearsalError(f"git {' '.join(args)} 실패: {result.stderr.strip()}")
    return result.stdout.strip()


def make_target(dest: Path) -> Path:
    """기존 리허설과 같은 GitLab + Jira 대상."""
    result = run([sys.executable, str(HARNESS), "init", str(dest), "--project-name", "rehearsal",
                  "--platform", "gitlab", "--tracker", "jira", "--issue-prefix", "DEMO"])
    if result.returncode != 0:
        raise RehearsalError(f"init 실패: {result.stdout}{result.stderr}")
    return dest


def write(repo: Path, rel: str, content: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


def make_pr_repo(dest: Path, feature) -> Path:
    """init 대상 위에 base → feature(시나리오 변경) / main(대상 앞선 커밋) → --no-ff merge(HEAD, detached)."""
    repo = make_target(dest)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "rehearsal@example.invalid")
    git(repo, "config", "user.name", "rehearsal")
    git(repo, "config", "core.autocrlf", "false")
    write(repo, "src/app.py", "def greet(name):\n    return f'hello {name}'\n")
    commit(repo, "base")
    git(repo, "checkout", "-qb", "feature")
    feature(repo)
    if git(repo, "status", "--porcelain"):
        commit(repo, "feature")
    git(repo, "checkout", "-q", "main")
    write(repo, "docs/changelog.md", "# changelog\n")
    commit(repo, "main moves")
    git(repo, "checkout", "-q", "--detach", "main")
    git(repo, "merge", "-q", "--no-ff", "feature", "-m", "Merge feature into main")
    return repo


# ---------------------------------------------------------------------------
# 시나리오
# ---------------------------------------------------------------------------


def fake_token() -> str:
    alphabet = string.ascii_letters + string.digits
    return "ghp_" + "".join(secrets.choice(alphabet) for _ in range(36))


def leak_then_remove(token):
    def feature(repo):
        write(repo, "src/config.py", f'TOKEN = "{token}"\n')
        commit(repo, "add config")
        (repo / "src" / "config.py").unlink()
        commit(repo, "remove config")
    return feature


def clean_change(repo):
    write(repo, "src/util.py", "def add(a, b):\n    return a + b\n")


def sast_finding(repo):
    write(repo, "src/run.py", "import subprocess\n\n\ndef run(cmd):\n    subprocess.call(cmd, shell=True)\n")


def npm_project(lock: bool):
    def feature(repo):
        write(repo, "web/package.json", json.dumps(
            {"name": "web", "version": "1.0.0", "dependencies": {"lodash": "4.17.20"}}, indent=2) + "\n")
        if lock:
            write(repo, "web/package-lock.json", json.dumps({
                "name": "web", "version": "1.0.0", "lockfileVersion": 3, "requires": True,
                "packages": {
                    "": {"name": "web", "version": "1.0.0", "dependencies": {"lodash": "4.17.20"}},
                    "node_modules/lodash": {"version": "4.17.20",
                                            "resolved": "https://registry.npmjs.org/lodash/-/lodash-4.17.20.tgz"},
                },
            }, indent=2) + "\n")
    return feature


def gradle_project(repo):
    write(repo, "api/build.gradle", "plugins { id 'java' }\n")
    write(repo, "api/gradle.lockfile",
          "# This is a Gradle generated file for dependency locking.\n"
          "org.apache.logging.log4j:log4j-api:2.14.1=runtimeClasspath\n"
          "org.apache.logging.log4j:log4j-core:2.14.1=runtimeClasspath\n"
          "empty=\n")


def scenarios(token: str) -> list[dict]:
    """name, fragment, feature, env, code(기대 종료 코드), ids(리포트에 있어야 할 ID), output(출력에 있어야 할 문자열),
    empty(리포트가 비어야 함)."""
    return [
        {"name": "secret-clean", "fragment": "secret-detection", "feature": clean_change, "code": 0,
         "empty": True, "output": ["commits scanned"], "forbid_output": ["0 commits scanned"]},
        {"name": "secret-leak", "fragment": "secret-detection", "feature": leak_then_remove(token), "code": 1,
         "ids": ["github-pat"]},
        {"name": "sast-clean", "fragment": "sast", "feature": clean_change, "code": 0,
         "empty": True, "min_scanned": 1, "forbid_output": ["::warning"]},
        {"name": "sast-finding", "fragment": "sast", "feature": sast_finding, "code": 0,
         "ids": ["subprocess-shell-true"], "output": ["::warning title=harness-sast::"]},
        {"name": "npm-vulnerable", "fragment": "dependency-audit", "feature": npm_project(lock=True), "code": 1,
         "ids": ["CVE-2021-23337"], "needs_db": True},
        {"name": "npm-no-lockfile", "fragment": "dependency-audit", "feature": npm_project(lock=False), "code": 1,
         "output": ["web/package.json: lockfile 없음"]},
        {"name": "gradle-vulnerable", "fragment": "dependency-audit", "feature": gradle_project, "code": 1,
         "ids": ["CVE-2021-44228"], "needs_db": True},
        {"name": "image-vulnerable", "fragment": "image-scan", "feature": clean_change, "code": 1,
         "env": {"HARNESS_SCAN_IMAGE": IMAGE_FIXTURE}, "ids": ["CVE-2021-36159"], "needs_db": True},
        {"name": "image-no-input", "fragment": "image-scan", "feature": clean_change, "code": 2,
         "output": ["HARNESS_SCAN_IMAGE 또는 HARNESS_SCAN_ARCHIVE를 지정한다"]},
    ]


def report_ids(fragment: str, data) -> set[str]:
    """리포트에서 발견 ID를 모은다. 형식이 다르면 실패한다(빈 집합으로 넘기지 않는다)."""
    if fragment == "secret-detection":
        if not isinstance(data, list):
            raise RehearsalError("gitleaks 리포트가 목록이 아니다")
        return {item["RuleID"] for item in data}
    if fragment == "sast":
        return {row["check_id"].rsplit(".", 1)[-1] for row in data["results"]}
    ids = set()
    for result in data.get("Results") or []:
        ids.update(v["VulnerabilityID"] for v in result.get("Vulnerabilities") or [])
    return ids


def token_fragments(text: str, token: str) -> bool:
    return any(token[i:i + 8] in text for i in range(len(token) - 7))


def redact_token(text: str, token: str) -> str:
    for size in range(len(token), 7, -1):
        for i in range(len(token) - size + 1):
            text = text.replace(token[i:i + size], "<TOKEN>")
    return text


def judge(scenario: dict, code: int, output: str, report_text: str | None, token: str) -> list[str]:
    """기대와 다른 점 목록. 비어 있으면 통과."""
    problems = []
    if code != scenario["code"]:
        problems.append(f"종료 코드 {code}, 기대 {scenario['code']}")
    for needle in scenario.get("output", []):
        if needle not in output:
            problems.append(f"출력에 '{needle}'이 없다")
    for needle in scenario.get("forbid_output", []):
        if needle in output:
            problems.append(f"출력에 금지된 문구 '{needle}'이 있다")
    if scenario.get("ids") or scenario.get("empty"):
        if report_text is None:
            problems.append("리포트가 없다")
        else:
            try:
                data = json.loads(report_text)
                ids = report_ids(scenario["fragment"], data)
            except (ValueError, KeyError, TypeError, AttributeError, RehearsalError) as exc:
                problems.append(f"리포트를 읽지 못했다: {exc}")
            else:
                missing = [i for i in scenario.get("ids", []) if i not in ids]
                if missing:
                    problems.append(f"리포트에 기대 ID가 없다: {', '.join(missing)} (찾은 것 {sorted(ids)[:10]})")
                if scenario.get("empty") and ids:
                    problems.append(f"정상 시나리오인데 발견이 있다: {sorted(ids)[:10]}")
                if scenario.get("min_scanned") and data.get("scanned", 0) < scenario["min_scanned"]:
                    problems.append("검사한 파일 수가 기대보다 적다")
    if token and token_fragments(output + (report_text or ""), token):
        problems.append("가짜 토큰 일부가 출력이나 리포트에 남았다")
    if scenario["fragment"] == "secret-detection" and report_text:
        try:
            rows = json.loads(report_text)
            if isinstance(rows, list) and any(row.get(field) != "REDACTED"
                                                  for row in rows for field in ("Secret", "Match")):
                problems.append("gitleaks 리포트의 Secret 또는 Match가 가려지지 않았다")
        except (ValueError, AttributeError, TypeError):
            pass  # 리포트 형식 오류는 위에서 보고한다
    return problems


def run_security(names: list[str]) -> int:
    token = fake_token()
    chosen = [s for s in scenarios(token) if not names or s["name"] in names]
    if names and len(chosen) != len(set(names)):
        raise RehearsalError(f"모르는 시나리오가 있다: {names}")
    work = Path(tempfile.mkdtemp(prefix="harness-rehearsal-"))
    cache = work / "trivy-cache"
    env = {k: v for k, v in os.environ.items()
           if k != "GITHUB_ACTIONS" and not k.startswith(("HARNESS_", "TRIVY_"))}
    failed = []
    try:
        for scenario in chosen:
            repo = make_pr_repo(work / scenario["name"], scenario["feature"])
            if scenario.get("needs_db") and cache.is_dir():
                shutil.copytree(cache, repo / TRIVY_CACHE)  # DB를 시나리오마다 받지 않는다
            print(f"::group::{scenario['name']} ({scenario['fragment']})", flush=True)
            result = run([sys.executable, str(RUNNER), scenario["fragment"], str(repo)],
                         env={**env, **scenario.get("env", {})})
            output = result.stdout + result.stderr
            safe_output = redact_token(output, token).encode(sys.stdout.encoding or "utf-8", errors="replace")
            print(safe_output.decode(sys.stdout.encoding or "utf-8"))  # Windows 콘솔에서도 출력 가능
            print("::endgroup::", flush=True)
            report = repo / REPORTS[scenario["fragment"]]
            report_text = report.read_text(encoding="utf-8", errors="replace") if report.is_file() else None
            problems = judge(scenario, result.returncode, output, report_text, token)
            if (repo / TRIVY_CACHE).is_dir() and not cache.is_dir():
                shutil.copytree(repo / TRIVY_CACHE, cache)
            status = "통과" if not problems else "실패: " + "; ".join(problems)
            print(f"rehearsal {scenario['name']}: {status}", flush=True)
            if problems:
                failed.append(scenario["name"])
            remove_container_cache(repo)
            remove_tree(repo)
    finally:
        remove_tree(work)
    if failed:
        print(f"::error::보안 리허설 실패: {', '.join(failed)}")
        return 1
    print(f"보안 리허설 {len(chosen)}개 시나리오 통과")
    return 0


# ---------------------------------------------------------------------------
# hooks
# ---------------------------------------------------------------------------


def hook(target: Path, command: str, payload: dict) -> subprocess.CompletedProcess:
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(target)}
    # Windows에서도 설치된 셸 명령을 실행한다. python3만 현재 인터프리터로 바꾸고
    # 뒤에 붙은 셸 연산자까지 그대로 실행해 종료 코드 변경을 검출한다.
    executable = '"' + str(Path(sys.executable).as_posix()) + '"'
    command = command.replace("python3 ", executable + " ", 1)
    command = command.replace("$CLAUDE_PROJECT_DIR", target.as_posix())
    return run(command, cwd=target, env=env,
               shell=True,
               stdin=json.dumps({"cwd": str(target), "session_id": "rehearsal", **payload}))


def decision(result: subprocess.CompletedProcess) -> str:
    if result.returncode != 0:
        raise RehearsalError(f"protect-paths 종료 코드 {result.returncode}: {result.stderr.strip()}")
    if not result.stdout.strip():
        return "allow"
    return json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"]


def run_hooks() -> int:
    work = Path(tempfile.mkdtemp(prefix="harness-rehearsal-hooks-"))
    try:
        target = make_target(work / "consumer")
        problems = check_hooks(target)
    finally:
        remove_tree(work)
    for problem in problems:
        print(f"::error::hooks 리허설: {problem}")
    if problems:
        return 1
    print("hooks 리허설 통과")
    return 0


def check_hooks(target: Path) -> list[str]:
    problems = []
    for rel in (".claude/settings.json", ".claude/hooks/protect-paths.py", ".claude/hooks/stop-verify.py",
                ".claude/hooks/session-start.py", ".claude/hooks/session-end.py", ".claude/hooks/harness_common.py", ".harness/agentops.py"):
        if not (target / rel).is_file():
            problems.append(f"{rel} 이 설치되지 않았다")
    if problems:
        return problems
    try:
        groups = json.loads((target / ".claude/settings.json").read_text("utf-8"))["hooks"]
        events = ("PreToolUse", "SessionStart", "SessionEnd", "Stop")
        observed = ("PreToolUse", "SessionStart", "Stop", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "PreCompact")
        expected = {name: int(name in events) + int(name in observed) for name in set(events) | set(observed)}
        if set(groups) != set(expected) or any(len(groups[name]) != count for name, count in expected.items()):
            raise ValueError("기존 가드 4개와 추가 관측 7개의 등록이 정확하지 않다")
        for name in observed:
            observer = groups[name][1 if name in events else 0]
            wanted = {"hooks": [{"type": "command", "command": 'python3 "$CLAUDE_PROJECT_DIR/.harness/agentops.py" hook --tool claude-code', "timeout": 10}]}
            if name in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
                wanted["matcher"] = ".*"
            if observer != wanted:
                raise ValueError("추가 관측 hook이 기대와 다르다")
        pre, start, end, stop = (groups[name][0] for name in events)
        if pre["matcher"] != "Edit|Write|MultiEdit|NotebookEdit" or any("matcher" in g for g in (start, end, stop)):
            raise ValueError("hook matcher가 기대와 다르다")
        if any(len(group["hooks"]) != 1 for group in (pre, start, end, stop)):
            raise ValueError("이벤트마다 명령 하나가 아니다")
        protect_cmd, start_cmd, end_cmd, stop_cmd = (group["hooks"][0]["command"]
                                                     for group in (pre, start, end, stop))
        if any(group["hooks"][0]["type"] != "command" for group in (pre, start, end, stop)):
            raise ValueError("hook 형식이 command가 아니다")
        if protect_cmd != 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/protect-paths.py"' or \
                start_cmd != 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/session-start.py"' or \
                end_cmd != 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/session-end.py"' or \
                stop_cmd != 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/stop-verify.py"':
            raise ValueError("hook 명령이 설치된 스크립트를 정확히 가리키지 않는다")
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        return [f"settings.json hook 등록이 잘못됐다: {exc}"]

    expected = {"package-lock.json": "deny", "api/gradle.lockfile": "deny", ".gitlab-ci.yml": "ask",
                ".claude/hooks/stop-verify.py": "deny", "harness.json": "ask", "src/app.py": "allow"}
    for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        for rel, want in expected.items():
            path_key = "notebook_path" if tool == "NotebookEdit" else "file_path"
            got = decision(hook(target, protect_cmd, {
                "hook_event_name": "PreToolUse", "tool_name": tool,
                "tool_input": {path_key: str(target / rel)}}))
            if got != want:
                problems.append(f"보호 경로 {tool} {rel}: {got}, 기대 {want}")

    config_path = target / "harness.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    py = Path(sys.executable).as_posix()
    git(target, "init", "-q", "-b", "main")
    git(target, "config", "user.email", "rehearsal@example.invalid")
    git(target, "config", "user.name", "rehearsal")
    for label, exit_code, want in (("실패", 3, 2), ("성공", 0, 0)):
        config["hooks"] = {"stop_verify": [f'"{py}" -c "raise SystemExit({exit_code})"']}
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        commit(target, f"verify {label}")
        write(target, "src/app.py", f"# {label}\n")  # 미커밋 변경이 있어야 종료 검증이 돈다
        result = hook(target, stop_cmd, {"hook_event_name": "Stop", "stop_hook_active": False})
        if result.returncode != want:
            problems.append(f"종료 검증({label}): 종료 코드 {result.returncode}, 기대 {want}. {result.stderr.strip()[:200]}")
        elif want == 2 and "종료 코드 3" not in result.stderr:
            problems.append("종료 검증(실패): 보류 메시지에 실패한 명령 결과가 없다")
        (target / "src" / "app.py").unlink()
    return problems


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in ("hooks", "security") or (argv[1] == "hooks" and len(argv) > 2):
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    try:
        return run_hooks() if argv[1] == "hooks" else run_security(argv[2:])
    except RehearsalError as exc:
        print(f"::error::리허설 준비 실패: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
