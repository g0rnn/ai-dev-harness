#!/usr/bin/env python3
"""도구 내장 텔레메트리 설정과 점검(#145). 표준 라이브러리만 쓴다.

키트의 `harness telemetry setup|doctor`와 대상 저장소의 `python .harness/telemetry.py setup|doctor`가 같은 코드를 쓴다.

- setup: 다음을 한 번에 맞춘다. 바꿀 내용을 먼저 보여 주고 승인을 받은 뒤 쓰며, 사용자 설정 파일은 바꾸기 전 내용을
  `<파일>.harness-backup`에 남긴다.
  1. `harness.json`에 telemetry 블록이 없으면 `--endpoint`로 만든다(팀 공통 값, 커밋 대상).
  2. 사용자 자격 파일(`harness_common.config_dir()/credentials.json`)에 수집기 주소별 토큰을 둔다.
  3. Claude Code `otelHeadersHelper`가 실행할 `otel_headers.py`를 같은 디렉터리에 설치한다(저장소 밖).
  4. 도구마다 사용자 전역 설정에서 내장 텔레메트리를 켠다. 두 도구 모두 프로젝트 설정으로는 켤 수 없다
     (Claude Code는 프로젝트·로컬 설정의 OTEL 변수를 무시하고, Codex는 프로젝트 설정의 `otel`을 거부한다).
     - Claude Code `~/.claude/settings.json`(`CLAUDE_CONFIG_DIR`): `env`의 OTEL 변수와 `otelHeadersHelper`.
       평문 토큰이 든 `OTEL_EXPORTER_OTLP_*HEADERS`는 지운다.
     - Codex `~/.codex/config.toml`(`CODEX_HOME`): 표시 주석으로 감싼 `[otel]` 블록. Codex는 헤더에 환경 변수
       치환을 하지 않으므로 토큰을 이 파일에 쓴다.
  사용자가 직접 둔 다른 수집기 설정과 겹치면 쓰지 않고 멈춘다(`--force`로 Claude 쪽 값은 덮을 수 있다).
- doctor: 각 단계가 실제로 맞는지 점검한다. 기본으로 수집기에 빈 OTLP 로그 요청을 보내 토큰이 받아들여지는지 본다.

토큰 값은 출력·오류 메시지에 쓰지 않는다.
"""

# `from __future__ import annotations`를 쓰지 않는다. 이 파일은 sys.modules에 등록하지 않고 경로로 읽히는데,
# 문자열 어노테이션이면 dataclass가 모듈을 찾다 실패한다
import json
import os
import shutil
import stat
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

HELPER_NAME = "otel_headers.py"
BACKUP_SUFFIX = ".harness-backup"
CODEX_BEGIN = "# >>> harness telemetry: `harness telemetry setup`이 관리한다. 직접 고치지 않는다"
CODEX_END = "# <<< harness telemetry"
CLAUDE_ENV = ("CLAUDE_CODE_ENABLE_TELEMETRY", "OTEL_METRICS_EXPORTER", "OTEL_LOGS_EXPORTER",
              "OTEL_EXPORTER_OTLP_PROTOCOL", "OTEL_EXPORTER_OTLP_ENDPOINT")
CLAUDE_ENDPOINT_VARS = ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
                        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT")
# 평문 토큰이 들어가는 변수. otelHeadersHelper가 대신하므로 지운다
CLAUDE_HEADER_VARS = ("OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_LOGS_HEADERS", "OTEL_EXPORTER_OTLP_METRICS_HEADERS")
ONLINE_TIMEOUT_SEC = 10


class SetupError(Exception):
    """설정을 바꿀 수 없다(종료 2)."""


@dataclass
class Change:
    path: Path
    lines: list[str]            # 사람이 읽는 변경 요약(토큰 값 없음)
    content: str                # 쓸 내용
    private: bool = False       # POSIX에서 본인만 읽고 쓰게(600) 한다
    backup: bool = True         # 바꾸기 전 내용을 남긴다(키트가 소유한 파일은 남기지 않는다)


@dataclass
class Paths:
    config: Path                # 사용자 수준 키트 디렉터리
    claude: Path                # Claude Code 사용자 설정 디렉터리
    codex: Path                 # Codex 홈

    @property
    def credentials(self) -> Path:
        return self.config / "credentials.json"

    @property
    def helper(self) -> Path:
        return self.config / HELPER_NAME

    @property
    def claude_settings(self) -> Path:
        return self.claude / "settings.json"

    @property
    def codex_config(self) -> Path:
        return self.codex / "config.toml"


def user_paths(common, env=None, home: Path | None = None) -> Paths:
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    return Paths(config=common.config_dir(env, home=home),
                 claude=Path(env["CLAUDE_CONFIG_DIR"]) if env.get("CLAUDE_CONFIG_DIR") else home / ".claude",
                 codex=Path(env["CODEX_HOME"]) if env.get("CODEX_HOME") else home / ".codex")


@dataclass
class Plan:
    endpoint: str
    tools: list[str]
    changes: list[Change] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 계획
# ---------------------------------------------------------------------------

def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def helper_command(python: str, helper: Path, endpoint: str, os_name: str | None = None) -> str:
    """Claude Code가 셸로 실행할 명령. Windows는 cmd 규칙, 그 밖은 POSIX 셸 규칙으로 인용한다."""
    if (os_name or os.name) == "nt":
        return subprocess.list2cmdline([python, str(helper), endpoint])
    import shlex
    return " ".join(shlex.quote(part) for part in (python, str(helper), endpoint))


def plan_project(common, project: Path, endpoint: str | None) -> tuple[dict, Change | None]:
    """(telemetry 설정, harness.json 변경). 블록이 없으면 `--endpoint`로 만든다. 파일 모양은 그대로 두고 끝에 덧붙인다."""
    path = project / common.CONFIG_NAME
    text = read_text(path)
    if text is None:
        raise SetupError(f"{path} 이 없다. 키트를 적용한 저장소 루트에서 실행한다")
    try:
        config = json.loads(text)
        settings = common.telemetry_settings(config) if isinstance(config, dict) else None
    except (ValueError, common.ConfigError) as exc:
        raise SetupError(f"{path}: {exc}") from None
    if not isinstance(config, dict):
        raise SetupError(f"{path}: 최상위는 객체여야 한다")
    if settings is not None:
        if endpoint and common.endpoint_key(endpoint) != common.endpoint_key(settings["endpoint"]):
            raise SetupError(f"--endpoint 가 {path}의 telemetry.endpoint({settings['endpoint']})와 다르다. "
                             "팀 공통 값이므로 harness.json을 고치는 MR로 바꾼다")
        return settings, None
    if not endpoint:
        raise SetupError(f"{path}에 telemetry 블록이 없다. --endpoint <수집기 주소>로 만든다")
    problem = common.endpoint_problem(endpoint)
    if problem:
        raise SetupError(f"--endpoint: {problem}")
    endpoint = endpoint.strip().rstrip("/")
    body = text.rstrip()
    if not body.endswith("}"):
        raise SetupError(f"{path}: 끝이 닫는 중괄호가 아니라 블록을 덧붙일 수 없다")
    inner = body[:-1].rstrip()
    # 템플릿 자리표시자 검사(이중 중괄호 금지)에 걸리지 않게 f-string 중괄호 이스케이프 대신 json으로 만든다
    block = '"telemetry": ' + json.dumps({"endpoint": endpoint}, indent=2).replace("\n", "\n  ")
    separator = "" if inner.endswith("{") else ","
    new = inner + separator + "\n  " + block + "\n}\n"
    if json.loads(new) != {**config, "telemetry": {"endpoint": endpoint}}:  # 덧붙인 결과가 의도와 같은지 확인한다
        raise SetupError(f"{path}: telemetry 블록을 안전하게 덧붙이지 못했다. 직접 넣는다")
    settings = common.telemetry_settings({"telemetry": {"endpoint": endpoint}})
    return settings, Change(path, [f'telemetry 블록 추가: endpoint {endpoint} (커밋 대상, 팀 공통 값)'], new, backup=False)


def plan_credentials(common, paths: Paths, endpoint: str, token: str | None) -> tuple[dict, Change | None]:
    """(이 주소의 헤더, 자격 파일 변경). 토큰을 주지 않으면 이미 있는 항목을 쓴다."""
    key = common.endpoint_key(endpoint)
    try:
        current = common.load_credentials(paths.credentials).get(key)
    except common.ConfigError as exc:
        raise SetupError(f"{exc}. 파일을 고치거나 지운 뒤 다시 실행한다") from None
    if token is None:
        if current is None:
            raise SetupError(f"{key}의 토큰이 없다. --token-stdin으로 넘기거나 터미널에서 실행해 입력한다")
        return current, None
    headers = {"Authorization": f"Bearer {token}"}
    if current == headers:
        return headers, None
    raw = json.loads(read_text(paths.credentials) or '{"endpoints": {}}')
    endpoints = {url: entry for url, entry in raw["endpoints"].items() if common.endpoint_key(url) != key}
    endpoints[key] = {"headers": headers}
    content = json.dumps({**raw, "endpoints": endpoints}, ensure_ascii=False, indent=2) + "\n"
    what = "토큰 저장" if current is None else "토큰 교체"
    return headers, Change(paths.credentials, [f"{key}: {what}(값은 표시하지 않는다), 본인만 읽기"], content,
                           private=True, backup=False)


def plan_helper(paths: Paths, source: Path) -> Change | None:
    content = read_text(source)
    if content is None:
        raise SetupError(f"{source} 이 없다. 키트 `harness init <저장소> --force`로 다시 생성한다")
    if read_text(paths.helper) == content:
        return None
    return Change(paths.helper, ["Claude Code otelHeadersHelper가 실행할 인증 헤더 출력 스크립트 설치"], content, backup=False)


def plan_claude(paths: Paths, endpoint: str, command: str, force: bool) -> Change | None:
    path = paths.claude_settings
    text = read_text(path)
    try:
        data = json.loads(text) if text and text.strip() else {}
    except ValueError:
        raise SetupError(f"{path}: JSON 형식이 잘못됐다. 고친 뒤 다시 실행한다") from None
    env = data.get("env", {})
    if not isinstance(data, dict) or not isinstance(env, dict):
        raise SetupError(f"{path}: 최상위와 env는 객체여야 한다")
    wanted = {"CLAUDE_CODE_ENABLE_TELEMETRY": "1", "OTEL_METRICS_EXPORTER": "otlp", "OTEL_LOGS_EXPORTER": "otlp",
              "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf", "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint}
    conflicts = [f"env.{name}={env[name]}" for name in CLAUDE_ENDPOINT_VARS
                 if isinstance(env.get(name), str) and env[name].strip()
                 and env[name].rstrip("/") not in (endpoint, endpoint + "/v1/logs", endpoint + "/v1/metrics")]
    helper = data.get("otelHeadersHelper")
    if isinstance(helper, str) and helper.strip() and HELPER_NAME not in helper:
        conflicts.append("otelHeadersHelper(직접 설정한 명령)")
    if conflicts and not force:
        raise SetupError(f"{path}에 다른 수집기 설정이 있다: {', '.join(conflicts)}. 확인 후 --force로 덮는다. "
                         "Claude Code 텔레메트리는 사용자 전역 설정이라 수집기를 하나만 쓴다")
    lines = []
    new_env = dict(env)
    for name, value in wanted.items():
        if new_env.get(name) != value:
            new_env[name] = value
            lines.append(f"env.{name} = {value}")
    for name in CLAUDE_ENDPOINT_VARS[1:]:  # 신호별 주소는 공통 주소보다 먼저 쓰이므로 남기지 않는다
        if name in new_env:
            del new_env[name]
            lines.append(f"env.{name} 삭제(공통 주소로 통일)")
    for name in CLAUDE_HEADER_VARS:
        if name in new_env:
            del new_env[name]
            lines.append(f"env.{name} 삭제(평문 토큰, otelHeadersHelper가 대신한다)")
    if helper != command:
        lines.append(f"otelHeadersHelper = {command}")
    if not lines:
        return None
    new = {**data, "env": new_env, "otelHeadersHelper": command}
    return Change(path, lines, json.dumps(new, ensure_ascii=False, indent=2) + "\n")


def toml_str(value: str) -> str:
    return json.dumps(value)  # JSON 문자열 이스케이프는 TOML 기본 문자열에서도 유효하다


def codex_block(endpoint: str, headers: dict[str, str]) -> str:
    def table(pairs: list[str]) -> str:  # TOML 인라인 표. 이중 중괄호를 쓰지 않는다(템플릿 자리표시자 검사)
        return "{ " + ", ".join(pairs) + " }"
    header_table = table([f"{toml_str(name)} = {toml_str(value)}" for name, value in headers.items()])
    def exporter(signal: str) -> str:
        options = table([f"endpoint = {toml_str(endpoint + '/v1/' + signal)}", 'protocol = "binary"',
                         f"headers = {header_table}"])
        return table([f"otlp-http = {options}"])
    return "\n".join([CODEX_BEGIN, "[otel]", f"exporter = {exporter('logs')}",
                      f"metrics_exporter = {exporter('metrics')}", CODEX_END])


def strip_managed(text: str) -> tuple[str, bool]:
    """(관리 블록을 뺀 내용, 블록이 있었는가). 짝이 맞지 않으면 SetupError."""
    begin, end = text.find(CODEX_BEGIN), text.find(CODEX_END)
    if begin < 0 and end < 0:
        return text, False
    if begin < 0 or end < begin:
        raise SetupError("Codex config.toml의 harness 표시 주석 짝이 맞지 않는다. 직접 정리한 뒤 다시 실행한다")
    tail = text[end + len(CODEX_END):]
    return text[:begin].rstrip("\n") + ("\n" + tail.lstrip("\n") if tail.strip() else "\n"), True


def plan_codex(paths: Paths, endpoint: str, headers: dict[str, str]) -> Change | None:
    path = paths.codex_config
    text = read_text(path) or ""
    rest, managed = strip_managed(text)
    try:
        parsed = tomllib.loads(rest)
    except tomllib.TOMLDecodeError as exc:
        raise SetupError(f"{path}: TOML 형식이 잘못됐다({exc}). 고친 뒤 다시 실행한다") from None
    if "otel" in parsed:
        raise SetupError(f"{path}에 직접 둔 [otel] 설정이 있다. 지우거나 harness 블록으로 옮긴 뒤 다시 실행한다")
    body = rest.rstrip("\n")
    new = (body + "\n\n" if body else "") + codex_block(endpoint, headers) + "\n"
    if new == text:
        return None
    expected = tomllib.loads(new)["otel"]
    if expected["exporter"]["otlp-http"]["endpoint"] != endpoint + "/v1/logs":  # 덧붙인 결과를 다시 읽어 확인한다
        raise SetupError(f"{path}: [otel] 블록을 안전하게 덧붙이지 못했다")
    what = "harness [otel] 블록 갱신" if managed else "harness [otel] 블록 추가"
    return Change(path, [f"{what}: 로그·지표를 {endpoint}로(토큰 포함, 본인만 읽기)"], new, private=True)


def make_plan(common, project: Path, paths: Paths, *, endpoint: str | None, token: str | None, helper_source: Path,
              python: str = sys.executable, force: bool = False) -> Plan:
    settings, project_change = plan_project(common, project, endpoint)
    plan = Plan(settings["endpoint"].rstrip("/"), settings["tools"])
    headers, credential_change = plan_credentials(common, paths, plan.endpoint, token)
    plan.changes += [change for change in (project_change, credential_change) if change]
    if "claude-code" in plan.tools:
        plan.changes += [change for change in (
            plan_helper(paths, helper_source),
            plan_claude(paths, plan.endpoint, helper_command(python, paths.helper, plan.endpoint), force)) if change]
    if "codex" in plan.tools:
        change = plan_codex(paths, plan.endpoint, headers)
        plan.changes += [change] if change else []
        plan.notes.append("Codex는 헤더에 환경 변수 치환을 하지 않아 토큰을 config.toml에 쓴다. "
                          "이 파일을 공유하거나 커밋하지 않는다")
    if not settings["harness_events"]:
        plan.notes.append("harness.json telemetry.harness_events가 false라 키트 기록(hooks·종료 검증)은 보내지 않는다")
    return plan


# ---------------------------------------------------------------------------
# 적용
# ---------------------------------------------------------------------------

def write_file(change: Change) -> None:
    path = change.path
    path.parent.mkdir(parents=True, exist_ok=True)
    if change.backup and path.is_file():
        shutil.copy2(path, path.with_name(path.name + BACKUP_SUFFIX))  # 권한도 그대로 남긴다
    temp = path.with_name(f".{path.name}.harness-tmp")
    with open(temp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(change.content)
    if change.private and os.name != "nt":
        os.chmod(temp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(temp, path)


def show(plan: Plan, out) -> None:
    out(f"수집기: {plan.endpoint}  도구: {', '.join(plan.tools)}")
    if not plan.changes:
        out("바꿀 것이 없다. 이미 설정돼 있다.")
    for change in plan.changes:
        out(f"\n{change.path}")
        for line in change.lines:
            out(f"  - {line}")
        if change.backup and change.path.is_file():
            out(f"  (바꾸기 전 내용: {change.path.name}{BACKUP_SUFFIX})")
    for note in plan.notes:
        out(f"\n참고: {note}")


def setup(common, project: Path, *, endpoint: str | None, token: str | None, helper_source: Path, yes: bool = False,
          dry_run: bool = False, force: bool = False, env=None, home: Path | None = None, out=print,
          confirm=None) -> int:
    """종료 코드: 0(적용했거나 바꿀 것이 없다), 1(승인하지 않았다), 2(설정 오류)."""
    try:
        plan = make_plan(common, project, user_paths(common, env, home), endpoint=endpoint, token=token,
                         helper_source=helper_source, force=force)
    except SetupError as exc:
        out(f"harness telemetry setup: {exc}")
        return 2
    show(plan, out)
    if dry_run or not plan.changes:
        return 0
    if not yes:
        if confirm is None:
            out("\n적용하지 않았다. 터미널에서 다시 실행해 승인하거나 --yes를 붙인다.")
            return 1
        if not confirm("\n위 내용을 적용한다? [y/N] "):
            out("적용하지 않았다.")
            return 1
    for change in plan.changes:
        write_file(change)
    out(f"\n{len(plan.changes)}개 파일을 바꿨다. 실행 중인 Claude Code·Codex는 다시 시작해야 새 설정을 읽는다.")
    if any(change.path.name == common.CONFIG_NAME for change in plan.changes):
        out(f"{common.CONFIG_NAME}는 팀 공통 설정이다. 커밋해 팀원이 같은 주소를 쓰게 한다.")
    out("확인: telemetry doctor")
    return 0


# ---------------------------------------------------------------------------
# 점검
# ---------------------------------------------------------------------------

def loose_permissions(path: Path) -> bool:
    """POSIX에서 그룹·다른 사용자가 읽을 수 있는가. Windows는 사용자 프로필 아래라 보지 않는다."""
    return os.name != "nt" and bool(path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO))


def run_helper(common, python: str, helper: Path, endpoint: str, env=None) -> dict | None:
    """Claude Code처럼 스크립트를 실행해 헤더 JSON을 받는다. env는 자격 파일 위치를 정하는 변수까지 그대로 넘긴다."""
    try:
        result = subprocess.run([python, str(helper), endpoint], capture_output=True, timeout=ONLINE_TIMEOUT_SEC,
                                stdin=subprocess.DEVNULL, env=None if env is None else dict(env),
                                **common.hidden_kwargs())
        headers = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return headers if isinstance(headers, dict) else None


def probe(endpoint: str, headers: dict[str, str]) -> tuple[str, str]:
    """빈 OTLP 로그 요청으로 수집기가 토큰을 받는지 본다. (level, message)."""
    request = urllib.request.Request(endpoint + "/v1/logs", data=b'{"resourceLogs": []}', method="POST",
                                     headers={**headers, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=ONLINE_TIMEOUT_SEC) as response:
            return "ok", f"수집기가 요청을 받았다({response.status})"
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return "fail", f"수집기가 토큰을 거절했다({exc.code}). 토큰을 확인하고 setup을 다시 실행한다"
        return "fail", f"수집기 응답 {exc.code}"
    except (urllib.error.URLError, OSError) as exc:
        return "fail", f"수집기에 연결하지 못했다: {getattr(exc, 'reason', exc)}"


def doctor(common, project: Path, *, helper_source: Path, offline: bool = False, env=None, home: Path | None = None,
           python: str = sys.executable, out=print) -> int:
    """종료 코드: 0(문제 없음, 경고만 있어도 0), 1(실패 항목이 있다)."""
    findings: list[tuple[str, str]] = []
    add = lambda level, message: findings.append((level, message))  # noqa: E731
    paths = user_paths(common, env, home)
    try:
        config = json.loads(read_text(project / common.CONFIG_NAME) or "{}")
        settings = common.telemetry_settings(config) if isinstance(config, dict) else None
    except (ValueError, common.ConfigError) as exc:
        settings = None
        add("fail", f"{common.CONFIG_NAME}: {exc}")
    if settings is None:
        if not findings:
            add("fail", f"{common.CONFIG_NAME}에 telemetry 블록이 없다. setup --endpoint <수집기 주소>로 만든다")
        return report(findings, out)
    endpoint = settings["endpoint"].rstrip("/")
    add("ok", f"{common.CONFIG_NAME} telemetry.endpoint = {endpoint} (도구: {', '.join(settings['tools'])})")
    if not settings["harness_events"]:
        add("warn", "telemetry.harness_events가 false라 키트 기록(hooks·종료 검증)은 보내지 않는다")

    try:
        headers = common.load_credentials(paths.credentials).get(common.endpoint_key(endpoint))
    except common.ConfigError as exc:
        headers = None
        add("fail", str(exc))
    else:
        if headers is None:
            add("fail", f"{paths.credentials}에 {common.endpoint_key(endpoint)}의 토큰이 없다. setup으로 넣는다")
        else:
            add("ok", f"자격 파일에 토큰이 있다: {paths.credentials}")
            if loose_permissions(paths.credentials):
                add("warn", f"{paths.credentials}를 다른 사용자가 읽을 수 있다. chmod 600")

    if "claude-code" in settings["tools"]:
        check_claude(common, paths, endpoint, helper_source, python, env, add)
    if "codex" in settings["tools"]:
        check_codex(paths, endpoint, headers, add)
    if headers and not offline:
        add(*probe(endpoint, headers))
    return report(findings, out)


def check_claude(common, paths: Paths, endpoint: str, helper_source: Path, python: str, process_env, add) -> None:
    if not paths.helper.is_file():
        add("fail", f"인증 헤더 출력 스크립트가 없다: {paths.helper}. setup으로 설치한다")
    else:
        if read_text(paths.helper) != read_text(helper_source):
            add("warn", f"{paths.helper}가 저장소의 {HELPER_NAME}와 다르다. setup으로 갱신한다")
        if run_helper(common, python, paths.helper, endpoint, process_env) is None:
            add("fail", f"{paths.helper}가 {endpoint}의 헤더를 내지 못했다")
        else:
            add("ok", "인증 헤더 출력 스크립트가 동작한다")
    try:
        data = json.loads(read_text(paths.claude_settings) or "{}")
        env = data.get("env", {}) if isinstance(data, dict) else None
    except ValueError:
        data, env = None, None
    if not isinstance(env, dict):
        add("fail", f"{paths.claude_settings}를 읽지 못했다(JSON 객체가 아니다)")
        return
    missing = [name for name in CLAUDE_ENV if not env.get(name)]
    if missing:
        add("fail", f"Claude Code 텔레메트리가 꺼져 있다. 없는 값: {', '.join(missing)}. setup으로 켠다")
    elif env["OTEL_EXPORTER_OTLP_ENDPOINT"].rstrip("/") != endpoint:
        add("fail", f"Claude Code가 다른 수집기로 보낸다: {env['OTEL_EXPORTER_OTLP_ENDPOINT']}")
    else:
        add("ok", "Claude Code 내장 텔레메트리가 켜져 있다")
    if HELPER_NAME not in str(data.get("otelHeadersHelper", "")):
        add("fail", "Claude Code otelHeadersHelper가 설정돼 있지 않다. setup으로 넣는다")
    plaintext = [name for name in CLAUDE_HEADER_VARS if name in env]
    if plaintext:
        add("warn", f"Claude Code 설정에 평문 토큰 변수가 남아 있다: {', '.join(plaintext)}. setup이 지운다")


def check_codex(paths: Paths, endpoint: str, headers: dict | None, add) -> None:
    text = read_text(paths.codex_config)
    if text is None:
        add("fail", f"Codex 설정이 없다: {paths.codex_config}. setup으로 만든다")
        return
    try:
        otel = tomllib.loads(text).get("otel")
    except tomllib.TOMLDecodeError as exc:
        add("fail", f"{paths.codex_config}: TOML 형식이 잘못됐다({exc})")
        return
    if CODEX_BEGIN not in text or not isinstance(otel, dict):
        add("fail", "Codex 내장 텔레메트리 블록이 없다. setup으로 넣는다")
        return
    def otlp_http(value) -> dict:
        inner = value.get("otlp-http") if isinstance(value, dict) else None
        return inner if isinstance(inner, dict) else {}
    exporters = {signal: otlp_http(otel.get(key)) for signal, key in (("logs", "exporter"), ("metrics", "metrics_exporter"))}
    wrong = [signal for signal, exporter in exporters.items() if exporter.get("endpoint") != f"{endpoint}/v1/{signal}"]
    if wrong:
        add("fail", f"Codex가 다른 수집기로 보낸다({', '.join(wrong)}). setup으로 다시 맞춘다")
    elif headers is not None and any(exporter.get("headers") != headers for exporter in exporters.values()):
        add("fail", "Codex 설정의 토큰이 자격 파일과 다르다. setup으로 다시 맞춘다")
    else:
        add("ok", "Codex 내장 텔레메트리가 켜져 있다")
    if loose_permissions(paths.codex_config):
        add("warn", f"{paths.codex_config}에 토큰이 있는데 다른 사용자가 읽을 수 있다. chmod 600")


LABELS = {"ok": "정상", "warn": "경고", "fail": "실패"}


def report(findings: list[tuple[str, str]], out) -> int:
    for level, message in findings:
        out(f"[{LABELS[level]}] {message}")
    failed = sum(level == "fail" for level, _ in findings)
    out(f"\n실패 {failed}건, 경고 {sum(level == 'warn' for level, _ in findings)}건")
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# 명령(키트 `harness telemetry`와 저장소 안 `.harness/telemetry.py`가 같이 쓴다)
# ---------------------------------------------------------------------------

def add_arguments(sub) -> tuple:
    """setup·doctor 하위 명령을 더하고 (setup, doctor) 파서를 돌려준다(키트 명령은 대상 인자를 더 붙인다)."""
    setup_cmd = sub.add_parser("setup", help="토큰을 사용자 자격 파일에 두고 Claude Code·Codex 내장 텔레메트리를 켠다(#145)")
    setup_cmd.add_argument("--endpoint", help="harness.json에 telemetry 블록이 없을 때 만들 수집기 주소(https)")
    setup_cmd.add_argument("--token-stdin", action="store_true", help="토큰을 표준 입력 첫 줄에서 읽는다(이때 --yes 필요)")
    setup_cmd.add_argument("--yes", action="store_true", help="묻지 않고 적용한다")
    setup_cmd.add_argument("--dry-run", action="store_true", help="바꿀 내용만 보여 주고 쓰지 않는다")
    setup_cmd.add_argument("--force", action="store_true", help="Claude Code에 직접 둔 다른 수집기 설정을 덮는다")
    doctor_cmd = sub.add_parser("doctor", help="텔레메트리 설정이 실제로 맞는지 점검한다")
    doctor_cmd.add_argument("--offline", action="store_true", help="수집기에 시험 요청을 보내지 않는다")
    return setup_cmd, doctor_cmd


def run_command(common, name: str, args, project: Path, helper_source: Path, out=print) -> int:
    if name == "doctor":
        return doctor(common, project, helper_source=helper_source, offline=args.offline, out=out)
    try:
        token = read_token(args.token_stdin)
    except SetupError as exc:
        out(f"harness telemetry setup: {exc}")
        return 2
    interactive = not args.token_stdin and is_terminal()
    confirm = (lambda prompt: input(prompt).strip().lower() in ("y", "yes")) if interactive else None
    return setup(common, project, endpoint=args.endpoint, token=token, helper_source=helper_source, yes=args.yes,
                 dry_run=args.dry_run, force=args.force, out=out, confirm=confirm)


def is_terminal(stdin=None, stdout=None) -> bool:
    """사람이 입력할 수 있는 터미널인가. 표준 입력과 출력이 모두 터미널이어야 한다. Windows는 입력을 NUL로 돌려도
    isatty()가 참이라, 입력만 보면 CI·AI 도구 안에서 토큰 입력을 기다리며 멈춘다."""
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    return stdin.isatty() and stdout.isatty()


def read_token(from_stdin: bool, stdin=None, stdout=None) -> str | None:
    """`--token-stdin`이면 표준 입력 첫 줄, 터미널이면 숨겨 입력받는다. 둘 다 아니면 None(이미 저장된 토큰을 쓴다)."""
    stdin = sys.stdin if stdin is None else stdin
    if from_stdin:
        token = stdin.readline().strip()
    elif is_terminal(stdin, stdout):
        import getpass
        token = getpass.getpass("수집기 토큰(입력은 보이지 않는다. 이미 저장했으면 Enter): ").strip()
        if not token:
            return None
    else:
        return None
    if not token or any(char.isspace() or ord(char) < 32 for char in token):
        raise SetupError("토큰은 공백·제어 문자가 없는 한 줄이어야 한다")
    return token
