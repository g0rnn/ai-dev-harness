#!/usr/bin/env python3
"""주간 요약 발송(M4-2). 표준 라이브러리만 쓴다.

수집기(`collect.py`)가 쓴 `metrics.json`(`version` 1)을 읽어 마지막 주의 4개 지표와 직전 주 대비 증감, 리포트 링크를
평문 Markdown 한 메시지로 만들어 incoming webhook(Slack·Mattermost 공통 `{"text": ...}`) 또는 Mattermost 계정
로그인(`/api/v4/users/login` → `/api/v4/posts` → logout)으로 보낸다(README "발송" 절). 요약 값은 같은 디렉터리의
`report.py`를 import해 리포트의 "이번 주 요약"과 같은 계산을 쓴다.

    python core/metrics/notify.py [--in metrics.json] [--mode webhook|mattermost|auto] [--report-url URL] [--dry-run]

설정은 환경 변수로만 받는다(CI 변수는 masked). 값은 로그·오류 메시지·출력에 쓰지 않는다.
  webhook:    HARNESS_NOTIFY_WEBHOOK_URL
  mattermost: HARNESS_MATTERMOST_URL, HARNESS_MATTERMOST_LOGIN_ID, HARNESS_MATTERMOST_PASSWORD,
              HARNESS_MATTERMOST_CHANNEL_ID 또는 HARNESS_MATTERMOST_TEAM + HARNESS_MATTERMOST_CHANNEL
모드 auto(기본)는 webhook 변수가 있으면 webhook, 없고 Mattermost URL이 있으면 mattermost다. URL은 https만 받고
리다이렉트는 따르지 않는다(3xx도 오류, POST 재전송 방지). 오류 메시지에는 HTTP 상태와 호스트만 남긴다(webhook URL
경로에는 토큰이 있다). `--dry-run`은 본문만 표준 출력에 쓰고 네트워크·설정 검사 없이 끝낸다.

종료 코드: 성공 0, 입력·설정·전송 오류 2.
"""

from __future__ import annotations

import argparse
import http.client
import importlib.util
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

EXIT_OK, EXIT_ERROR = 0, 2
MODES = ("webhook", "mattermost", "auto")
DEFAULT_IN = "metrics.json"
MAX_MESSAGE_LENGTH = 4000  # Slack이 긴 text를 자르는 선. Mattermost 기본 상한(16383)보다 작다
HTTP_TIMEOUT = 30
RESPONSE_MAX_BYTES = 1 << 20
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
USER_AGENT = "ai-dev-harness-metrics"
LINK_SCHEMES = ("http", "https")
TRUNCATED = "\n…(길이 제한으로 생략)"

WEBHOOK_ENV = "HARNESS_NOTIFY_WEBHOOK_URL"
MATTERMOST_URL_ENV = "HARNESS_MATTERMOST_URL"
MATTERMOST_LOGIN_ENV = "HARNESS_MATTERMOST_LOGIN_ID"
MATTERMOST_PASSWORD_ENV = "HARNESS_MATTERMOST_PASSWORD"
MATTERMOST_CHANNEL_ID_ENV = "HARNESS_MATTERMOST_CHANNEL_ID"
MATTERMOST_TEAM_ENV = "HARNESS_MATTERMOST_TEAM"
MATTERMOST_CHANNEL_ENV = "HARNESS_MATTERMOST_CHANNEL"

# 리포트와 같은 요약 계산(normalize_weeks·ratio·delta_text·in_progress)을 쓴다. 경로로 읽어 소비자 복사본에서도 동작한다
_REPORT_SPEC = importlib.util.spec_from_file_location("harness_metrics_report_for_notify",
                                                      Path(__file__).resolve().parent / "report.py")
report = importlib.util.module_from_spec(_REPORT_SPEC)
_REPORT_SPEC.loader.exec_module(report)


class NotifyError(Exception):
    """보낼 수 없는 입력·설정·전송 오류(종료 2). 메시지에 webhook URL 경로·비밀번호·세션 토큰·응답 본문을 넣지 않는다."""


class HttpStatusError(NotifyError):
    def __init__(self, status: int, url: str):
        hint = {401: " 로그인 정보를 확인한다.", 403: " 권한·채널 접근을 확인한다.", 404: " URL·팀·채널을 확인한다."}
        super().__init__(f"HTTP {status} {url_host(url)}.{hint.get(status, '')}")
        self.status = status


# ---------------------------------------------------------------------------
# 본문
# ---------------------------------------------------------------------------


def _project(data: dict) -> str:
    value = data.get("project")
    return " ".join(value.split()) if isinstance(value, str) else ""


def title_line(project: str, week: str, progress: bool) -> str:
    parts = [project, "주간 측정 요약", week]
    return " ".join(part for part in parts if part) + (" (진행 중)" if progress else "")


def metric_line(label: str, value: str, previous: str, delta: str) -> str:
    return f"- {label}: {value} (지난주 {previous}, {delta})"


def summary_lines(weeks: list[dict]) -> list[str]:
    """리포트 '이번 주 요약' 표와 같은 값. 주가 하나면 지난주·증감은 `-`."""
    current, previous = weeks[-1], weeks[-2] if len(weeks) > 1 else None

    def count(key) -> tuple[str, str, str]:
        value = key(current)
        before = key(previous) if previous else None
        return str(value), "-" if before is None else str(before), report.delta_text(value, before)

    current_pct, current_fraction = report.ratio(current)
    previous_pct, previous_fraction = report.ratio(previous) if previous else (None, "")
    return [
        metric_line("1 병합 후 누락 수", *count(lambda week: week["escaped_defects"])),
        metric_line("2 엄격 비율", report.ratio_text(current_pct, current_fraction),
                    report.ratio_text(previous_pct, previous_fraction) if previous else "-",
                    report.delta_ratio_text(current_pct, previous_pct)),
        metric_line("3 CI 실패 수", *count(report.ci_total)),
        metric_line("4 판정 불일치 수", *count(lambda week: week["mismatch"])),
    ]


def compose(lines: list[str], tail: str | None) -> str:
    """줄을 잇고 상한을 넘으면 본문을 잘라 표시한다. 마지막 줄(리포트 링크)은 자르지 않는다."""
    body = "\n".join(lines)
    extra = f"\n{tail}" if tail else ""
    if len(body) + len(extra) > MAX_MESSAGE_LENGTH:
        room = max(MAX_MESSAGE_LENGTH - len(extra) - len(TRUNCATED), 0)
        body = body[:room].rstrip() + TRUNCATED
    return (body + extra)[:MAX_MESSAGE_LENGTH]


def build_message(data: dict, report_url: str | None) -> str:
    """평문 Markdown(제목 줄, `- ` 목록, 맨 URL). 서식 차이를 피해 강조·머리글 기호를 쓰지 않는다."""
    weeks = report.normalize_weeks(data.get("weeks") if isinstance(data.get("weeks"), list) else [])
    window = data.get("window") if isinstance(data.get("window"), dict) else {}
    project = _project(data)
    if not weeks:
        lines = [title_line(project, "", False), "데이터 없음"]
    else:
        current = weeks[-1]
        lines = [title_line(project, current["week"], report.in_progress(current, window)), *summary_lines(weeks)]
    return compose(lines, f"리포트: {report_url}" if report_url else None)


def report_url_arg(value: str | None) -> str | None:
    if value is None:
        return None
    target = " ".join(value.split())
    try:
        scheme = urllib.parse.urlsplit(target).scheme.lower()
    except ValueError:
        scheme = ""
    if scheme not in LINK_SCHEMES:
        raise NotifyError("--report-url은 http 또는 https URL이어야 한다.")
    return target


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Response:
    def __init__(self, status: int, headers: dict | None = None, body: bytes = b""):
        self.status = status
        self.headers = {str(k).lower(): v for k, v in (headers or {}).items()}
        self.body = body


def url_host(url: str) -> str:
    """오류·요약 메시지용 호스트. 경로(webhook 토큰)·쿼리는 보이지 않는다."""
    try:
        return urllib.parse.urlsplit(url).hostname or "?"
    except ValueError:
        return "?"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """리다이렉트를 따르지 않는다. 3xx는 Response로 돌아와 오류가 된다(POST를 다른 주소로 다시 보내지 않는다)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None = None) -> Response:
    """기본 전송. 3xx·4xx·5xx도 Response로 돌려주고, 오류 응답 본문은 읽지 않는다(메시지에 옮기지 않는다)."""
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        response = _OPENER.open(request, timeout=HTTP_TIMEOUT)
    except urllib.error.HTTPError as exc:
        response = exc
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        raise NotifyError(f"요청 실패({type(exc).__name__}): {url_host(url)}") from None
    try:
        status = response.code if isinstance(response, urllib.error.HTTPError) else response.status
        resp_headers = dict(response.headers.items()) if response.headers is not None else {}
        data = b"" if isinstance(response, urllib.error.HTTPError) else response.read(RESPONSE_MAX_BYTES + 1)
    except (http.client.HTTPException, OSError, ValueError) as exc:
        raise NotifyError(f"응답을 읽을 수 없다({type(exc).__name__}): {url_host(url)}") from None
    finally:
        response.close()
    if len(data) > RESPONSE_MAX_BYTES:
        raise NotifyError(f"응답이 너무 크다: {url_host(url)}")
    return Response(status, resp_headers, data)


# 테스트가 바꿔 끼우는 기본 전송 함수(transport(method, url, headers, body) -> Response)
TRANSPORT = urllib_transport


def send(method: str, url: str, headers: dict, payload: dict | None, transport) -> Response:
    """JSON 요청 하나. 2xx만 성공이고 3xx는 리다이렉트 오류, 그 밖은 HttpStatusError(상태·호스트만)."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request_headers = {"Accept": "application/json", "User-Agent": USER_AGENT, **headers}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    response = transport(method, url, request_headers, body)
    if response.status in REDIRECT_STATUSES:
        raise NotifyError(f"리다이렉트 응답(HTTP {response.status})은 따르지 않는다. 최종 주소를 준다: {url_host(url)}")
    if not 200 <= response.status < 300:
        raise HttpStatusError(response.status, url)
    return response


def https_url(value: str, name: str) -> str:
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        parts = None
    if parts is None or parts.scheme.lower() != "https" or not parts.hostname:
        raise NotifyError(f"{name}은 https URL이어야 한다(토큰·비밀번호를 평문으로 보내지 않는다. 값은 출력하지 않는다).")
    return value


# ---------------------------------------------------------------------------
# webhook
# ---------------------------------------------------------------------------


def webhook_url(env: dict) -> str:
    value = (env.get(WEBHOOK_ENV) or "").strip()
    if not value:
        raise NotifyError(f"{WEBHOOK_ENV}이 없다. incoming webhook URL을 CI 변수(masked)로 준다.")
    return https_url(value, WEBHOOK_ENV)


def send_webhook(url: str, message: str, transport) -> str:
    """Slack·Mattermost incoming webhook 공통 `{"text": ...}`. 성공하면 호스트를 돌려준다."""
    send("POST", url, {}, {"text": message}, transport)
    return url_host(url)


# ---------------------------------------------------------------------------
# Mattermost 계정 로그인(feelm notify_mattermost.py 발송부)
# ---------------------------------------------------------------------------


class MattermostConfig:
    def __init__(self, base_url: str, login_id: str, password: str, channel_id: str | None,
                 team: str | None, channel: str | None):
        self.base_url, self.login_id, self.password = base_url, login_id, password
        self.channel_id, self.team, self.channel = channel_id, team, channel


def mattermost_config(env: dict) -> MattermostConfig:
    """필요한 변수 이름만 메시지에 쓴다. 값은 쓰지 않는다."""
    def value(name: str) -> str:
        return (env.get(name) or "").strip()

    missing = [name for name in (MATTERMOST_URL_ENV, MATTERMOST_LOGIN_ENV, MATTERMOST_PASSWORD_ENV) if not value(name)]
    if missing:
        raise NotifyError(f"Mattermost 발송에 필요한 변수가 없다: {', '.join(missing)}")
    base_url = https_url(value(MATTERMOST_URL_ENV).rstrip("/"), MATTERMOST_URL_ENV)
    parts = urllib.parse.urlsplit(base_url)
    if parts.query or parts.fragment:
        raise NotifyError(f"{MATTERMOST_URL_ENV}은 서버 주소만 적는다(쿼리·조각 없음).")
    channel_id = value(MATTERMOST_CHANNEL_ID_ENV) or None
    team, channel = value(MATTERMOST_TEAM_ENV) or None, value(MATTERMOST_CHANNEL_ENV) or None
    if not channel_id and not (team and channel):
        raise NotifyError(f"{MATTERMOST_CHANNEL_ID_ENV} 또는 {MATTERMOST_TEAM_ENV}과 {MATTERMOST_CHANNEL_ENV}이 필요하다.")
    return MattermostConfig(base_url, value(MATTERMOST_LOGIN_ENV), value(MATTERMOST_PASSWORD_ENV), channel_id, team, channel)


class MattermostClient:
    """login → (팀·채널 조회) → posts → logout. 세션 토큰은 Authorization 헤더에만 넣는다."""

    def __init__(self, config: MattermostConfig, transport):
        self.config = config
        self.transport = transport

    def api(self, method: str, path: str, token: str | None = None, payload: dict | None = None) -> Response:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return send(method, f"{self.config.base_url}{path}", headers, payload, self.transport)

    @staticmethod
    def parse(response: Response):
        try:
            return json.loads(response.body.decode("utf-8")) if response.body else None
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise NotifyError("Mattermost 응답이 JSON이 아니다.") from None

    def login(self) -> str:
        response = self.api("POST", "/api/v4/users/login",
                            payload={"login_id": self.config.login_id, "password": self.config.password})
        token = str(response.headers.get("token", "")).strip()
        if not token:
            raise NotifyError("Mattermost 로그인 응답에 세션 토큰이 없다.")
        return token

    def resolve_channel_id(self, token: str) -> str:
        if self.config.channel_id:
            return self.config.channel_id
        team_name, channel_name = self.config.team, self.config.channel
        team = self.parse(self.api("GET", f"/api/v4/teams/name/{urllib.parse.quote(team_name, safe='')}", token))
        team_id = team.get("id") if isinstance(team, dict) else None
        if not isinstance(team_id, str) or not team_id:
            raise NotifyError(f"Mattermost 팀 ID를 확인하지 못했다({MATTERMOST_TEAM_ENV} 값을 확인한다).")
        channel = self.parse(self.api("GET", f"/api/v4/teams/{urllib.parse.quote(team_id, safe='')}/channels/name/"
                                             f"{urllib.parse.quote(channel_name, safe='')}", token))
        channel_id = channel.get("id") if isinstance(channel, dict) else None
        if not isinstance(channel_id, str) or not channel_id:
            raise NotifyError(f"Mattermost 채널 ID를 확인하지 못했다({MATTERMOST_CHANNEL_ENV} 값을 확인한다).")
        return channel_id

    def post(self, token: str, channel_id: str, message: str) -> str:
        data = self.parse(self.api("POST", "/api/v4/posts", token, {"channel_id": channel_id, "message": message}))
        post_id = data.get("id") if isinstance(data, dict) else None
        if not isinstance(post_id, str) or not post_id:
            raise NotifyError("Mattermost 게시 응답에 post ID가 없다.")
        return post_id

    def logout(self, token: str) -> None:
        self.api("POST", "/api/v4/users/logout", token, {})


def send_mattermost(config: MattermostConfig, message: str, transport, warnings: list[str]) -> str:
    """로그아웃 실패는 경고로만 남긴다(게시는 이미 끝났다). 성공하면 호스트를 돌려준다."""
    client = MattermostClient(config, transport)
    token = client.login()
    try:
        client.post(token, client.resolve_channel_id(token), message)
    finally:
        try:
            client.logout(token)
        except NotifyError as exc:
            warnings.append(f"Mattermost 로그아웃 실패: {exc}")
    return url_host(config.base_url)


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """notify.py와 `harness metrics notify`가 같은 옵션을 쓰게 한다."""
    parser.add_argument("--in", dest="input", default=DEFAULT_IN, help=f"수집기 출력 JSON(기본 {DEFAULT_IN})")
    parser.add_argument("--mode", choices=MODES, default="auto",
                        help=f"전송 방식(기본 auto: {WEBHOOK_ENV}이 있으면 webhook, 없고 {MATTERMOST_URL_ENV}이 있으면 mattermost)")
    parser.add_argument("--report-url", help="본문 마지막 줄 '리포트: <URL>'에 넣을 HTML 리포트 주소")
    parser.add_argument("--dry-run", action="store_true", help="보내지 않고 본문만 표준 출력에 쓴다(설정 검사 없음)")


def load_input(path: Path) -> dict:
    try:
        return report.load_input(path)
    except report.ReportError as exc:
        raise NotifyError(str(exc)) from None


def select_mode(mode: str, env: dict) -> str:
    if mode != "auto":
        return mode
    if (env.get(WEBHOOK_ENV) or "").strip():
        return "webhook"
    if (env.get(MATTERMOST_URL_ENV) or "").strip():
        return "mattermost"
    raise NotifyError(f"발송 설정이 없다. {WEBHOOK_ENV}(incoming webhook) 또는 {MATTERMOST_URL_ENV}·{MATTERMOST_LOGIN_ENV}·"
                      f"{MATTERMOST_PASSWORD_ENV}와 채널 변수(Mattermost 로그인)를 준다. 본문만 보려면 --dry-run.")


def run(args, env: dict, transport=None) -> dict:
    """본문을 만들고(--dry-run이면 여기서 끝) 보낸다. 오류는 NotifyError."""
    data = load_input(Path(args.input))
    message = build_message(data, report_url_arg(args.report_url))
    result = {"message": message, "dry_run": bool(args.dry_run), "mode": None, "host": None, "warnings": []}
    if args.dry_run:
        return result
    mode = select_mode(args.mode, env)
    transport = transport or TRANSPORT
    if mode == "webhook":
        host = send_webhook(webhook_url(env), message, transport)
    else:
        host = send_mattermost(mattermost_config(env), message, transport, result["warnings"])
    result.update(mode=mode, host=host)
    return result


def summary(result: dict) -> str:
    return f"주간 요약을 {result['mode']}로 보냈다({result['host']}, {len(result['message'])}자)."


def emit(result: dict, prefix: str = "harness: ") -> None:
    """--dry-run이면 본문만 표준 출력에, 아니면 경고와 요약(호스트만)을 표준 오류에 쓴다."""
    if result["dry_run"]:
        sys.stdout.write(result["message"] + "\n")
        return
    for warning in result["warnings"]:
        print(f"{prefix}경고: {warning}", file=sys.stderr)
    print(f"{prefix}{summary(result)}", file=sys.stderr)


def main(argv: list[str] | None = None, env: dict | None = None, transport=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="notify", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    add_arguments(parser)
    args = parser.parse_args(argv)
    try:
        result = run(args, dict(os.environ) if env is None else env, transport)
    except NotifyError as exc:
        print(f"harness: 주간 요약을 보낼 수 없다: {exc}", file=sys.stderr)
        return EXIT_ERROR
    emit(result)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
