#!/usr/bin/env python3
"""측정 수집기(M4-1). 표준 라이브러리만 쓴다.

GitLab·GitHub API에서 기간 내 병합 MR·`escaped-defect` 라벨 이슈·실패 CI job을 읽어 4개 지표를
ISO 주 단위로 모으고 원자료와 함께 JSON 한 파일로 쓴다(README "수집기" 절).

    python core/metrics/collect.py --platform gitlab|github --since YYYY-MM-DD [--until YYYY-MM-DD]
                                   [--out metrics.json] [--api-url URL] [--project ID]
                                   [--utc-offset +09:00] [--defect-label escaped-defect]
                                   [--security-jobs a,b] [--common PATH] [--config PATH] [--mr-lint PATH]

토큰은 환경 변수 `HARNESS_METRICS_TOKEN`(GitHub는 없으면 `GITHUB_TOKEN`). 토큰은 출력·오류 메시지에 쓰지 않고,
다른 호스트로 넘어가는 리다이렉트에는 인증 헤더를 붙이지 않는다.

판정(지표 2·4)은 MR lint(`mr_lint.lint_body`)와 같은 정의를 쓰려고 mr_lint.py를 경로로 읽는다. 변경 파일 재판정은
수집 시점의 `harness.json` 규칙으로 한다(MR 시점 규칙과 다를 수 있다).

MR 본문의 `## hooks 기록` 절(지표 5)도 읽는다. 이 절을 만드는 `harness events summary`의 집계·출력 함수도
형식을 한곳에 두려고 이 파일에 둔다(`summarize_events`·`hooks_markdown`).

종료 코드: 성공 0, 입력·설정·API 오류 2.
"""

from __future__ import annotations

import argparse
import datetime as dt
import http.client
import importlib.util
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCHEMA_VERSION = 1
EXIT_OK, EXIT_ERROR = 0, 2
PLATFORMS = ("gitlab", "github")
PER_PAGE = 100
MAX_PAGES = 1000  # 목록 하나의 페이지 상한(Link가 끝없이 이어지는 응답을 막는다)
MAX_REDIRECTS = 5
HTTP_TIMEOUT = 30
JSON_MAX_BYTES = 50_000_000
TRACE_TAIL_BYTES = 1 << 20  # trace는 끝 1 MiB만 분류에 쓴다
READ_CHUNK = 1 << 16
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
AUTH_HEADERS = ("authorization", "private-token")  # 소문자로 비교한다
TOKEN_ENV = "HARNESS_METRICS_TOKEN"
GITHUB_TOKEN_ENV = "GITHUB_TOKEN"
DEFAULT_OUT = "metrics.json"
DEFAULT_OFFSET = "+09:00"
DEFAULT_DEFECT_LABEL = "escaped-defect"
DEFAULT_COMMON = ".claude/hooks/harness_common.py"
DEFAULT_CONFIG = "harness.json"
DEFAULT_GITHUB_API = "https://api.github.com"
USER_AGENT = "ai-dev-harness-metrics"
SECURITY_PREFIX = "harness-"
TIERS = ("lite", "standard", "strict")
# 키트에서는 core/metrics/../ci/mr-lint, 소비자에서는 저장소 루트 기준 .harness/mr-lint
KIT_MR_LINT = Path(__file__).resolve().parent.parent / "ci" / "mr-lint" / "mr_lint.py"
CONSUMER_MR_LINT = Path(".harness") / "mr-lint" / "mr_lint.py"
CAUSE_PATTERNS = {"gitlab": re.compile(r"원인\s*[:：]\s*!(\d+)"), "github": re.compile(r"원인\s*[:：]\s*#(\d+)")}
OFFSET_FORMAT = re.compile(r"^([+-])(\d\d):(\d\d)$")
DATE_FORMAT = re.compile(r"^\d{4}-\d\d-\d\d$")
GITHUB_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
HOOKS_SECTION = "hooks 기록"
HOOKS_LABELS = {"block": "차단(block)", "ask": "확인(ask)", "verify_fail": "종료 검증 실패"}  # 본문 줄 순서
# 본문은 신뢰할 수 없는 입력이다. 자릿수를 제한해 int 변환 오류(자릿수 상한)와 그래프 계산 넘침을 막는다
HOOKS_LINE = re.compile(r"^(차단\(block\)|확인\(ask\)|종료 검증 실패)\s*[:：]\s*(\d{1,6})\s*$")

_CLASSIFY_SPEC = importlib.util.spec_from_file_location("harness_metrics_classify_ci",
                                                        Path(__file__).resolve().parent / "classify_ci.py")
classify_ci = importlib.util.module_from_spec(_CLASSIFY_SPEC)
_CLASSIFY_SPEC.loader.exec_module(classify_ci)
CATEGORIES = classify_ci.CATEGORIES


class CollectError(Exception):
    """수집할 수 없는 입력·설정·API 오류(종료 2). 메시지에 토큰·응답 본문을 넣지 않는다."""


class HttpStatusError(CollectError):
    def __init__(self, status: int, url: str):
        hint = " 토큰 범위·역할·만료와 --project를 확인한다." if status in (401, 403, 404) else ""
        super().__init__(f"API 응답 HTTP {status}: {url_path(url)}.{hint}")
        self.status = status


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Response:
    def __init__(self, status: int, headers: dict | None = None, body: bytes = b""):
        self.status = status
        self.headers = {str(k).lower(): v for k, v in (headers or {}).items()}
        self.body = body


def url_path(url: str) -> str:
    """오류 메시지용 URL 경로. 호스트·쿼리(서명 등)는 빼고 경로만 보인다."""
    return urllib.parse.urlsplit(url).path or "/"


def origin(url: str) -> tuple[str, str]:
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    default = {"https": 443, "http": 80}.get(parts.scheme.lower())
    try:
        port = parts.port or default
    except ValueError:
        port = None
    return parts.scheme.lower(), f"{host}:{port}"


def read_body(stream, tail: int | None) -> bytes:
    """응답 본문. tail이면 끝 tail 바이트만 남기며 읽는다(큰 trace를 메모리에 다 올리지 않는다)."""
    if not tail:
        data = stream.read(JSON_MAX_BYTES + 1)
        if len(data) > JSON_MAX_BYTES:
            raise CollectError("API 응답이 너무 크다.")
        return data
    kept = bytearray()
    while True:
        chunk = stream.read(READ_CHUNK)
        if not chunk:
            break
        kept += chunk
        if len(kept) > tail:
            del kept[:len(kept) - tail]
    return bytes(kept)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """리다이렉트는 ApiClient가 직접 따라가며 인증 헤더를 정한다."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def urllib_transport(method: str, url: str, headers: dict, tail: int | None = None) -> Response:
    """기본 전송. 리다이렉트를 따라가지 않고 3xx·4xx·5xx도 Response로 돌려준다."""
    request = urllib.request.Request(url, method=method, headers=headers)
    try:
        response = _OPENER.open(request, timeout=HTTP_TIMEOUT)
    except urllib.error.HTTPError as exc:
        response = exc
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        raise CollectError(f"API 요청 실패({type(exc).__name__}): {url_path(url)}") from None
    try:
        status = response.code if isinstance(response, urllib.error.HTTPError) else response.status
        resp_headers = dict(response.headers.items()) if response.headers is not None else {}
        # 오류·리다이렉트 응답 본문은 쓰지 않는다(메시지에 옮기지 않는다)
        body = b"" if isinstance(response, urllib.error.HTTPError) else read_body(response, tail)
    except (http.client.HTTPException, OSError, ValueError) as exc:
        raise CollectError(f"API 응답을 읽을 수 없다({type(exc).__name__}): {url_path(url)}") from None
    finally:
        response.close()
    return Response(status, resp_headers, body)


# 테스트가 바꿔 끼우는 기본 전송 함수(transport(method, url, headers, tail) -> Response)
TRANSPORT = urllib_transport

LINK_ITEM = re.compile(r"<([^>]*)>([^<]*)")


def next_link(header: str | None) -> str | None:
    for match in LINK_ITEM.finditer(header or ""):
        if re.search(r'\brel\s*=\s*"?[^"]*\bnext\b', match.group(2)):
            return match.group(1).strip()
    return None


class ApiClient:
    """인증 헤더는 API와 같은 origin(스킴·호스트·포트)으로 가는 요청에만 붙인다."""

    def __init__(self, api_url: str, auth: dict, headers: dict, transport=None):
        self.api_url = api_url.rstrip("/")
        self.origin = origin(self.api_url)
        self.auth = auth
        self.headers = headers
        self.transport = transport or TRANSPORT

    def url(self, path: str, params: dict | list | None = None) -> str:
        query = urllib.parse.urlencode(params or {}, doseq=True)
        return f"{self.api_url}/{path.lstrip('/')}" + (f"?{query}" if query else "")

    def request(self, url: str, tail: int | None = None) -> Response:
        current = url
        authed = origin(current) == self.origin
        for _ in range(MAX_REDIRECTS + 1):
            headers = {**self.headers, **(self.auth if authed else {})}
            response = self.transport("GET", current, headers, tail)
            if response.status in REDIRECT_STATUSES:
                location = response.headers.get("location")
                if not location:
                    raise CollectError(f"API 리다이렉트에 Location이 없다: {url_path(current)}")
                target = urllib.parse.urljoin(current, location)
                if urllib.parse.urlsplit(target).scheme.lower() not in ("https", "http"):
                    raise CollectError(f"API 리다이렉트 주소가 잘못됐다: {url_path(current)}")
                # 다른 호스트(GitHub 로그 → 저장소 서버)나 스킴이 바뀌면 인증을 떼고, 한 번 떼면 다시 붙이지 않는다
                authed = authed and origin(target) == origin(current)
                current = target
                continue
            if response.status >= 400:
                raise HttpStatusError(response.status, current)
            if tail and len(response.body) > tail:
                response.body = response.body[-tail:]
            return response
        raise CollectError(f"API 리다이렉트가 너무 많다: {url_path(url)}")

    def get_json(self, url: str):
        response = self.request(url)
        try:
            return json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise CollectError(f"API 응답이 JSON이 아니다: {url_path(url)}") from None

    def get_list(self, url: str, key: str | None = None, stop=None) -> list:
        """`Link: rel="next"`를 따라 모든 페이지 항목. key는 감싼 객체의 목록 키, stop(item)이 참이면 그 항목부터 멈춘다."""
        items, seen = [], set()
        current = url
        for _ in range(MAX_PAGES):
            if current in seen:
                break
            seen.add(current)
            response = self.request(current)
            try:
                data = json.loads(response.body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError):
                raise CollectError(f"API 응답이 JSON이 아니다: {url_path(current)}") from None
            page = data.get(key) if key and isinstance(data, dict) else data
            if not isinstance(page, list):
                raise CollectError(f"API 응답이 목록이 아니다: {url_path(current)}")
            for item in page:
                if not isinstance(item, dict):
                    continue
                if stop is not None and stop(item):
                    return items
                items.append(item)
            following = next_link(response.headers.get("link"))
            if not following:
                return items
            current = urllib.parse.urljoin(current, following)
        raise CollectError(f"API 목록 페이지가 너무 많다: {url_path(url)}")


# ---------------------------------------------------------------------------
# 시간
# ---------------------------------------------------------------------------


def parse_offset(text: str) -> dt.timezone:
    match = OFFSET_FORMAT.match(text or "")
    if not match or int(match.group(2)) > 14 or int(match.group(3)) >= 60:
        raise CollectError(f"--utc-offset은 +HH:MM 형식이어야 한다: {text!r}")
    minutes = int(match.group(2)) * 60 + int(match.group(3))
    return dt.timezone(dt.timedelta(minutes=-minutes if match.group(1) == "-" else minutes))


def parse_date(text: str, tz: dt.timezone, name: str) -> dt.datetime:
    if not DATE_FORMAT.match(text or ""):
        raise CollectError(f"{name}는 YYYY-MM-DD 형식이어야 한다: {text!r}")
    try:
        day = dt.date.fromisoformat(text)
    except ValueError:
        raise CollectError(f"{name} 날짜가 잘못됐다: {text!r}") from None
    return dt.datetime(day.year, day.month, day.day, tzinfo=tz)


TIMESTAMP = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:?\d\d)?$")


def parse_ts(value) -> dt.datetime | None:
    """API 시각(`2026-10-04T16:00:00.000Z`, `+09:00`). 형식이 아니면 None. 시간대가 없으면 UTC로 본다."""
    if not isinstance(value, str):
        return None
    match = TIMESTAMP.match(value.strip())
    if not match:
        return None
    base = dt.datetime.fromisoformat(match.group(1))
    fraction = match.group(2)
    if fraction:
        base = base.replace(microsecond=int((fraction[1:] + "000000")[:6]))
    zone = match.group(3)
    if not zone or zone == "Z":
        return base.replace(tzinfo=dt.timezone.utc)
    sign = -1 if zone[0] == "-" else 1
    digits = zone[1:].replace(":", "")
    return base.replace(tzinfo=dt.timezone(sign * dt.timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))))


def iso_week(moment: dt.datetime, tz: dt.timezone) -> str:
    year, week, _ = moment.astimezone(tz).isocalendar()
    return f"{year}-W{week:02d}"


def week_monday(moment: dt.datetime, tz: dt.timezone) -> dt.date:
    local = moment.astimezone(tz).date()
    return local - dt.timedelta(days=local.weekday())


def window_weeks(since: dt.datetime, until: dt.datetime, tz: dt.timezone) -> list[dict]:
    """기간과 겹치는 모든 ISO 주(빈 주 포함)."""
    weeks = []
    monday = week_monday(since, tz)
    last = week_monday(until - dt.timedelta(microseconds=1), tz)
    while monday <= last:
        year, week, _ = monday.isocalendar()
        weeks.append({"week": f"{year}-W{week:02d}", "start": monday.isoformat(), "merged_mrs": 0, "strict": 0,
                      "mismatch": 0, "escaped_defects": 0, "unlinked_defects": 0,
                      "ci_failures": {category: 0 for category in CATEGORIES}, "hooks": empty_week_hooks(),
                      "agent": empty_week_agent(), "review": empty_week_review()})
        monday += dt.timedelta(days=7)
    return weeks


def utc_text(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Window:
    def __init__(self, since: dt.datetime, until: dt.datetime, tz: dt.timezone):
        self.since, self.until, self.tz = since, until, tz

    def contains(self, moment: dt.datetime | None) -> bool:
        return moment is not None and self.since <= moment < self.until

    def week(self, moment: dt.datetime) -> str:
        return iso_week(moment, self.tz)


# ---------------------------------------------------------------------------
# 판정 정의(mr_lint)와 재판정(harness_common)
# ---------------------------------------------------------------------------


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True  # 관리 디렉터리에 __pycache__를 남기면 `harness check`가 여분으로 본다
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # 복사본이 깨졌으면 원인을 그대로 보인다
        raise CollectError(f"{path} 을 읽을 수 없다: {exc}") from exc
    finally:
        sys.dont_write_bytecode = previous
    return module


def find_mr_lint(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise CollectError(f"--mr-lint 파일이 없다: {path}")
        return path
    for path in (KIT_MR_LINT, CONSUMER_MR_LINT):
        if path.is_file():
            return path
    raise CollectError(f"mr_lint.py를 찾을 수 없다({CONSUMER_MR_LINT}). `harness init`으로 생성하거나 --mr-lint로 지정한다.")


def load_mr_lint(explicit: str | None):
    path = find_mr_lint(explicit)
    module = _load_module("harness_metrics_mr_lint", path)
    for name in ("lint_body", "load_common", "content_lines", "LintError"):
        if not hasattr(module, name):
            raise CollectError(f"{path} 에 {name} 이 없다. 키트 버전을 맞춘다.")
    return module


def load_judge_config(path: Path, common) -> dict:
    """harness.json을 읽어 판정 규칙을 검사한다(mr_lint.rejudge와 같은 검사). 한 번만 읽어 모든 MR에 쓴다."""
    if not path.is_file():
        raise CollectError(f"{path} 이 없다. 판정을 다시 계산할 수 없다.")
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise CollectError(f"{path}: 최상위는 객체여야 한다.")
        areas = config.get("areas", [])
        if not isinstance(areas, list) or any(not isinstance(a, dict) or not isinstance(a.get("dir"), str)
                                              for a in areas):
            raise CollectError(f"{path}: areas 항목에는 dir이 필요하다.")
        for area in areas:
            common.validate_judge_rules(area, path)
        if "judge" in config:
            common.validate_judge_root(config["judge"], path)
    except (OSError, ValueError) as exc:
        raise CollectError(f"{path}: {exc}") from exc
    except common.ConfigError as exc:
        raise CollectError(str(exc)) from exc
    return config


class Judge:
    """MR 본문과 변경 파일로 lint_body의 tier(body·judge·effective·mismatch)를 낸다."""

    def __init__(self, mr_lint, common, config: dict):
        self.mr_lint, self.common, self.config = mr_lint, common, config

    def tier(self, body: str | None, paths: list[str]) -> dict:
        judged = self.common.judge(paths, self.config)["tier"]
        return self.mr_lint.lint_body(body or "", judged)["tier"]

    def is_integration(self, source: str | None, target: str | None) -> bool:
        """통합 MR(integration → default). MR lint가 검사하지 않는 MR이라 지표에서 뺀다."""
        integration, default = self.config.get("integration_branch"), self.config.get("default_branch")
        return (isinstance(integration, str) and isinstance(default, str) and bool(integration)
                and integration != default and source == integration and target == default)

    def hooks(self, body: str | None) -> dict | None:
        """MR 본문의 `## hooks 기록` 세 수. 절이 없거나 세 수가 다 있지 않으면 None(미보고)."""
        lines = self.mr_lint.unfenced(self.mr_lint.content_lines(body or ""))
        section = self.mr_lint.split_sections(lines).get(HOOKS_SECTION)
        return parse_hooks_lines(section) if section is not None else None

    def review(self, body: str | None) -> dict | None:
        """MR 본문 `## 리뷰 결과`의 리뷰 요약 두 줄(#99). 없으면 None."""
        lines = self.mr_lint.unfenced(self.mr_lint.content_lines(body or ""))
        section = self.mr_lint.split_sections(lines).get(REVIEW_SECTION)
        return parse_review_lines(section) if section is not None else None

    def agent(self, body: str | None) -> dict | None:
        """MR 본문의 `## 에이전트 기록`. 절이 없거나 도구 줄이 없으면 None(미보고, #98)."""
        lines = self.mr_lint.unfenced(self.mr_lint.content_lines(body or ""))
        section = self.mr_lint.split_sections(lines).get(AGENT_SECTION)
        return parse_agent_lines(section) if section is not None else None

    def cause(self, platform: str, body: str | None) -> int | None:
        """결함 본문의 `원인: !N`(GitLab)·`원인: #N`(GitHub). 주석·코드 블록 안은 보지 않는다."""
        text = "\n".join(self.mr_lint.content_lines(body or ""))
        match = CAUSE_PATTERNS[platform].search(text)
        return int(match.group(1)) if match else None


def higher_side(tier: dict) -> str | None:
    if not tier.get("mismatch"):
        return None
    return "body" if TIERS.index(tier["body"]) > TIERS.index(tier["judge"]) else "judge"


# ---------------------------------------------------------------------------
# hooks 기록 (지표 5)
# ---------------------------------------------------------------------------


def empty_week_hooks() -> dict:
    return {**{key: 0 for key in HOOKS_LABELS}, "reported_mrs": 0, "unreported_mrs": 0}


def empty_week_agent() -> dict:
    """주별 지표 6(#98). 비용은 같은 통화끼리만 더한다."""
    return {"reported_mrs": 0, "unreported_mrs": 0, "sessions": 0, "turns": 0, "active_min": 0,
            "tokens": {key: 0 for key in AGENT_TOKEN_KEYS}, "tokens_mrs": 0, "cost": {}, "cost_mrs": 0, "tools": {}}


def summarize_events(lines, branch: str) -> dict:
    """events.jsonl 줄 중 branch의 기록을 센다. `branch` 키가 없는 옛 기록과 깨진 줄은 따로 센다."""
    counts = {key: 0 for key in HOOKS_LABELS}
    rules: dict[str, dict[str, int]] = {}
    legacy = malformed = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            malformed += 1
            continue
        if not isinstance(record, dict):
            malformed += 1
            continue
        if "branch" not in record:
            legacy += 1
            continue
        if record["branch"] != branch:
            continue
        hook, decision = record.get("hook"), record.get("decision")
        if hook == "protect-paths" and decision in ("block", "ask"):
            counts[decision] += 1
            rule = record["rule"] if isinstance(record.get("rule"), str) else "?"
            rules.setdefault(rule, {"block": 0, "ask": 0})[decision] += 1
        elif hook == "stop-verify" and decision == "fail":
            counts["verify_fail"] += 1
    return {"branch": branch, **counts, "rules": rules, "legacy": legacy, "malformed": malformed}


def hooks_markdown(summary: dict) -> str:
    """MR 본문에 붙이는 `## hooks 기록` 절. 세 수 줄은 수집기가 읽으므로 형식을 바꾸지 않는다."""
    lines = [f"## {HOOKS_SECTION}", ""]
    lines += [f"- {label}: {summary[key]}" for key, label in HOOKS_LABELS.items()]
    parts = []
    for rule in sorted(summary["rules"]):
        counts = summary["rules"][rule]
        detail = ", ".join(f"{decision} {counts[decision]}" for decision in ("block", "ask") if counts[decision])
        parts.append(f"`{rule.replace('`', chr(39))}` {detail}")
    if parts:
        lines.append(f"- 규칙: {'; '.join(parts)}")
    return "\n".join(lines) + "\n"


VERIFY_OUTCOMES = ("pass", "fail", "cached-pass", "cached-fail", "none", "error")
UNRECORDED_GRACE = dt.timedelta(minutes=10)  # 마지막 응답 뒤 사람이 커밋할 여유
# 같은 세션이라도 이보다 긴 공백은 작업 구간을 나눈다. 응답 하나가 한 시간을 넘기도 하므로 넉넉히 둔다.
# resume은 `start` 사건이 따로 끊는다
IDLE_GAP = dt.timedelta(hours=2)


USAGE_FIELDS = ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read")  # hooks와 같은 순서
# Claude API 1차 단가(USD / 백만 토큰). 기준일 이후 바뀔 수 있어 harness.json `pricing`으로 덮어쓴다(#97).
# Haiku 5.5는 프롬프트 100K 토큰 이하 단가다. 초과 요청(5배)은 요청별로 가르지 않아 그만큼 적게 추정된다.
PRICING_AS_OF = "2026-10-06"
DEFAULT_PRICING = {
    "currency": "USD",
    "as_of": PRICING_AS_OF,
    "models": {
        model: dict(zip(USAGE_FIELDS, prices)) for model, prices in {
            "claude-fable-5-1": (10.0, 50.0, 12.5, 20.0, 0.25),
            "claude-fable-5": (10.0, 50.0, 12.5, 20.0, 1.0),
            "claude-opus-5-5": (4.0, 20.0, 5.0, 8.0, 0.20),
            "claude-opus-5": (5.0, 25.0, 6.25, 10.0, 0.50),
            "claude-opus-4-8": (5.0, 25.0, 6.25, 10.0, 0.50),
            "claude-opus-4-7": (5.0, 25.0, 6.25, 10.0, 0.50),
            "claude-opus-4-6": (5.0, 25.0, 6.25, 10.0, 0.50),
            "claude-sonnet-5-5": (2.0, 10.0, 2.5, 4.0, 0.20),
            "claude-sonnet-5": (2.0, 10.0, 2.5, 4.0, 0.20),
            "claude-sonnet-4-6": (3.0, 15.0, 3.75, 6.0, 0.30),
            "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.20, 0.01),
            "claude-haiku-4-5": (1.0, 5.0, 1.25, 2.0, 0.10),
        }.items()
    },
}
PRICING_KEYS = {"currency", "as_of", "models"}


def validate_pricing(pricing, source) -> list[str]:
    """`harness.json` 최상위 `pricing` 검사. 오류 문장 목록(비면 통과)."""
    if not isinstance(pricing, dict):
        return [f"{source}: pricing은 객체여야 한다"]
    errors = [f"{source}: pricing에 알 수 없는 키: {key}" for key in sorted(set(pricing) - PRICING_KEYS)]
    for key in ("currency", "as_of"):
        if key in pricing and not (isinstance(pricing[key], str) and pricing[key].strip()):
            errors.append(f"{source}: pricing.{key}는 비어 있지 않은 문자열이어야 한다")
    currency = pricing.get("currency")
    if isinstance(currency, str) and currency.strip() and not CURRENCY_TEXT.fullmatch(currency):
        errors.append(f"{source}: pricing.currency는 영문 3자(USD·KRW 등) 또는 원·₩·$·€·¥·£ 중 하나여야 한다"
                      "(MR 본문과 주간 요약 메시지에 그대로 쓴다)")
    # 통화·기준일만 바꾸면 키트 단가에 다른 표시가 붙는다(#97 리뷰 A F1·B F3). 단가와 함께 준다
    for key in ("currency", "as_of"):
        if key in pricing and not pricing.get("models"):
            errors.append(f"{source}: pricing.{key}는 pricing.models와 함께 준다")
    models = pricing.get("models", {})
    if not isinstance(models, dict):
        return errors + [f"{source}: pricing.models는 객체여야 한다"]
    for model, prices in models.items():
        if not isinstance(prices, dict) or set(prices) != set(USAGE_FIELDS) or not all(
                isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 for v in prices.values()):
            errors.append(f"{source}: pricing.models.{model}에는 {', '.join(USAGE_FIELDS)} 다섯 개의 0 이상 숫자가 필요하다")
    return errors


def merged_pricing(config: dict | None) -> dict:
    """키트 기본 단가에 `harness.json` `pricing`을 모델 단위로 덮어쓴다.

    통화를 키트와 다르게 주면 키트 단가를 섞지 않고 설정한 모델만 쓴다(다른 통화의 합이 섞이지 않게).
    기준일은 설정값, 없으면 키트 기준일에 덮어쓴 모델 수를 붙인다.
    """
    custom = (config or {}).get("pricing") or {}
    models = custom.get("models") or {}
    currency = custom.get("currency", DEFAULT_PRICING["currency"])
    if currency != DEFAULT_PRICING["currency"]:
        return {"currency": currency, "as_of": custom.get("as_of", "설정"), "models": dict(models)}
    as_of = custom.get("as_of") or (f"{DEFAULT_PRICING['as_of']}, 설정 {len(models)}개 모델" if models
                                    else DEFAULT_PRICING["as_of"])
    return {"currency": currency, "as_of": as_of, "models": {**DEFAULT_PRICING["models"], **models}}


DATED_MODEL = re.compile(r"^(.+)-\d{8}$")


def model_prices(model: str, pricing: dict) -> dict | None:
    """모델 단가. 날짜가 붙은 스냅샷 ID(`claude-haiku-4-5-20251001`)는 날짜를 뗀 ID로도 찾는다(#97 리뷰 B F2)."""
    prices = pricing["models"].get(model)
    if prices is None and (match := DATED_MODEL.match(model)):
        prices = pricing["models"].get(match.group(1))
    return prices


def add_usage(total: dict, delta: dict) -> None:
    for model, counts in delta.items():
        bucket = total.setdefault(model, {field: 0 for field in USAGE_FIELDS})
        for field in USAGE_FIELDS:
            bucket[field] += counts.get(field, 0)


def usage_delta(current: dict, previous: dict, partial: bool = False) -> tuple[dict, dict]:
    """(차, 다음 기준). 세션 사건마다 누적값이 기록되므로 차를 그 사건의 브랜치에 준다.

    누적이 줄면 새 transcript(다시 시작)로 본다. 다만 일부 파일을 못 읽은 스냅숏(`partial`, `usage_error`가 함께 있는 기록)은
    줄어도 다시 시작으로 보지 않고 필드마다 늘어난 만큼만 세며, 기준은 둘 중 큰 값을 남긴다. 그러지 않으면 못 읽은 파일이
    빠져 줄어든 합을 새 transcript로 보아 남은 토큰을 다시 센다(#107 Codex 리뷰).
    """
    if not partial and _usage_total(current) < _usage_total(previous):
        previous = {}
    delta, following = {}, {}
    for model in set(current) | set(previous):
        now, before = current.get(model, {}), previous.get(model, {})
        delta[model] = {field: max(now.get(field, 0) - before.get(field, 0), 0) for field in USAGE_FIELDS}
        following[model] = ({field: max(now.get(field, 0), before.get(field, 0)) for field in USAGE_FIELDS}
                            if partial else {field: now.get(field, 0) for field in USAGE_FIELDS})
    return delta, following


def _usage_total(usage: dict) -> int:
    return sum(sum(counts.values()) for counts in usage.values())


def clean_usage(value) -> dict | None:
    """기록의 `usage`가 {모델: {필드: 0 이상 정수}} 꼴이면 그 값, 아니면 None."""
    if not isinstance(value, dict):
        return None
    result = {}
    for model, counts in value.items():
        if not isinstance(model, str) or not isinstance(counts, dict):
            return None
        result[model] = {field: counts.get(field, 0) if isinstance(counts.get(field, 0), int)
                         and not isinstance(counts.get(field, 0), bool) and counts.get(field, 0) >= 0 else 0
                         for field in USAGE_FIELDS}
    return result


def estimate_cost(usage: dict, pricing: dict) -> tuple[float, list[str]]:
    """(추정 비용, 단가 없는 모델). 비용 = Σ 토큰 × 단가 / 1e6."""
    cost, unpriced = 0.0, []
    for model, counts in sorted(usage.items()):
        prices = model_prices(model, pricing)
        if prices is None:
            if any(counts.values()):
                unpriced.append(model)
            continue
        cost += sum(counts[field] * prices[field] for field in USAGE_FIELDS) / 1_000_000
    return cost, unpriced


def continues(previous: dt.datetime, moment: dt.datetime, event: str | None) -> bool:
    """같은 작업 구간으로 잇는가. `start`(resume 포함)와 IDLE_GAP보다 긴 공백에서 끊는다."""
    return event != "start" and moment - previous <= IDLE_GAP


def session_segments(events: list[tuple]) -> list[tuple[dt.datetime, dt.datetime]]:
    """한 세션의 (시각, 사건, …) 목록을 작업 구간으로 나눈다."""
    segments: list[list[dt.datetime]] = []
    for moment, event, *_ in sorted(events, key=lambda item: item[0]):
        if segments and continues(segments[-1][1], moment, event):
            segments[-1][1] = moment
        else:
            segments.append([moment, moment])
    return [(start, end) for start, end in segments]


def read_session_record(line: str):
    """(세션, 시각, 사건, 브랜치, 원본) 또는 None(셀 수 없는 줄), "malformed"(읽을 수 없는 줄)."""
    try:
        record = json.loads(line)
        if not isinstance(record, dict):
            return "malformed"
        moment = parse_ts(record.get("ts"))
    except (ValueError, OverflowError):  # 월 13, 오프셋 +99:99 같은 값도 깨진 줄로 센다
        return "malformed"
    session = record.get("session")
    if not isinstance(session, str) or not session or moment is None:
        return None
    event = record.get("event") if isinstance(record.get("event"), str) else None
    branch = record.get("branch") if isinstance(record.get("branch"), str) else None
    return session, moment, event, branch, record


# ---------------------------------------------------------------------------
# Codex 세션 로그(#116)
# ---------------------------------------------------------------------------

CODEX_TOOL = "codex"
# 줄 전체를 JSON으로 읽기 전에 이 표지가 있는 줄만 본다. 대화 내용(response_item)은 읽지 않는다
CODEX_MARKERS = ('"turn_context"', '"token_usage_record"', '"task_complete"', '"token_count"', '"task_started"')


def codex_home(env=None) -> Path:
    """Codex 기록 위치. Codex와 같은 `CODEX_HOME`, 없으면 `~/.codex`."""
    env = os.environ if env is None else env
    return Path(env["CODEX_HOME"]) if env.get("CODEX_HOME") else Path.home() / ".codex"


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def codex_root_of(cwd, roots: list[str]) -> str | None:
    """세션 작업 폴더가 든 저장소 worktree 경로(가장 긴 것). 없으면 None."""
    if not isinstance(cwd, str) or not cwd:
        return None
    here = _norm(cwd)
    found = [root for root in roots if here == _norm(root) or here.startswith(_norm(root).rstrip(os.sep) + os.sep)]
    return max(found, key=len) if found else None


def _codex_usage(value) -> dict | None:
    """Codex 응답 토큰 → 키트 필드. 입력은 캐시 입력을 뺀 값이다(OpenAI 입력 토큰은 캐시 입력을 포함한다)."""
    if not isinstance(value, dict):
        return None
    number = {key: value.get(key) if isinstance(value.get(key), int) and not isinstance(value.get(key), bool)
              and value.get(key) >= 0 else 0
              for key in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens")}
    return {"input": max(number["input_tokens"] - number["cached_input_tokens"], 0),
            "output": number["output_tokens"], "cache_write_5m": number["cache_write_input_tokens"],
            "cache_write_1h": 0, "cache_read": number["cached_input_tokens"]}


def _safe_ts(value) -> dt.datetime | None:
    try:
        return parse_ts(value)
    except (ValueError, OverflowError):  # 줄 하나의 범위 밖 시각으로 세션 전체를 버리지 않는다(#116 리뷰 F4)
        return None


def codex_session_records(path: Path, roots: list[str]) -> tuple[str | None, list[dict]]:
    """Codex 세션 로그 하나 → (worktree 경로, 세션 기록 형식의 사건). 이 저장소 밖이거나 읽지 못하면 (None, []).

    - 사용자 세션: 시작(`session_meta`의 시각·세션 ID·시작 브랜치, 첫 턴의 모델)과 턴 완료(`task_complete`)마다 응답 사건
      (그때까지의 모델별 누적 토큰). 마지막 턴 완료 뒤에 쓴 토큰(중단·진행 중인 턴)은 `usage` 사건으로 남긴다.
    - 하위 세션(`parent_thread_id`가 있는 승인 검토·서브에이전트·포크): 모든 사건을 `usage`로 내고 `parent`를 붙인다.
      토큰만 부모 세션의 작업으로 세고 세션·응답 수와 작업 시간에는 넣지 않는다(#116 리뷰 F2).
    - 응답별 토큰(`token_usage_record`)이 없는 옛 로그(cli 0.150~0.152)는 `token_count`의 누적 합을 쓴다(#116 리뷰 F5).
    - 대화 내용은 기록하거나 돌려주지 않는다. 표지가 든 줄만 JSON으로 읽고 시각·모델·토큰만 꺼낸다.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            first = json.loads(handle.readline() or "null")
            meta = first.get("payload") if isinstance(first, dict) and first.get("type") == "session_meta" else None
            if not isinstance(meta, dict):
                return None, []
            root = codex_root_of(meta.get("cwd"), roots)
            session = meta.get("id")
            started = first.get("timestamp") or meta.get("timestamp")
            if root is None or not isinstance(session, str) or _safe_ts(started) is None:
                return None, []
            git = meta.get("git") if isinstance(meta.get("git"), dict) else {}
            branch = git.get("branch") if isinstance(git.get("branch"), str) and git.get("branch") else None
            parent = meta.get("parent_thread_id") if isinstance(meta.get("parent_thread_id"), str) else None
            base = {"session": session, "tool": CODEX_TOOL, "branch": branch, **({"parent": parent} if parent else {})}
            turn_event = "usage" if parent else "stop"
            events: list[dict] = [{"ts": started, "event": "usage" if parent else "start", "source": "codex", **base}]
            model, usage = None, {}
            legacy = {}  # 옛 로그 token_count 누적의 직전 값. 늘어난 만큼만 그때의 모델에 더한다(#118 Codex 리뷰)
            per_record = False
            last_usage_ts, emitted = None, None
            for line in handle:
                if not any(marker in line for marker in CODEX_MARKERS):
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                payload = record.get("payload") if isinstance(record, dict) else None
                if not isinstance(payload, dict):
                    continue
                kind, moment = record.get("type"), _safe_ts(record.get("timestamp"))
                if kind == "turn_context":
                    model = payload.get("model") if isinstance(payload.get("model"), str) else model
                    if events[0].get("model") is None:
                        events[0]["model"] = model
                elif kind == "token_usage_record":
                    counts = _codex_usage(payload.get("usage"))
                    if counts:
                        per_record = True
                        add_usage(usage, {model or "codex-unknown": counts})
                        last_usage_ts = record["timestamp"] if moment else last_usage_ts
                elif kind == "event_msg" and payload.get("type") == "token_count" and not per_record:
                    # 응답별 기록이 나오기 전까지만 쓴다. 새 형식은 같은 응답의 token_usage_record가 늘 먼저 나오므로
                    # 형식이 바뀐 뒤(재개)에도 옛 누적은 usage에 남고 새 기록만 더해진다
                    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                    counts = _codex_usage(info.get("total_token_usage"))
                    if counts:
                        delta = {field: max(counts[field] - legacy.get(field, 0), 0) for field in USAGE_FIELDS}
                        legacy = counts
                        add_usage(usage, {model or "codex-unknown": delta})
                        last_usage_ts = record["timestamp"] if moment else last_usage_ts
                elif kind == "event_msg" and payload.get("type") == "task_started" and moment:
                    # 턴 시작. 하루 뒤 재개한 턴도 작업 구간이 시작 시각부터 잡히게 한다(#118 Codex 리뷰).
                    # 응답 수에는 들지 않는다(응답은 턴 완료만)
                    events.append({"ts": record["timestamp"], "event": "usage" if parent else "turn", **base})
                elif kind == "event_msg" and payload.get("type") == "task_complete" and moment:
                    emitted = {name: dict(counts) for name, counts in usage.items()}
                    events.append({"ts": record["timestamp"], "event": turn_event, **base, "usage": emitted})
            if usage and usage != emitted and last_usage_ts:
                events.append({"ts": last_usage_ts, "event": "usage", **base,
                               "usage": {name: dict(counts) for name, counts in usage.items()}})
            return root, events
    except (OSError, ValueError, OverflowError, AttributeError, TypeError):
        return None, []


def branch_timeline(lines) -> list[tuple[dt.datetime, str | None]]:
    """`git reflog show --date=iso-strict --format=%gd%x09%gs HEAD` 출력 → (시각, 그때부터 체크아웃한 브랜치) 시간순.
    분리된 HEAD(커밋 해시로 이동)는 None."""
    timeline = []
    for line in lines:
        selector, _, subject = line.partition("\t")
        match = re.search(r"@\{([^}]+)\}", selector)
        moved = re.match(r"checkout: moving from .+ to (\S+)$", subject.strip()) \
            or re.match(r"Branch: renamed refs/heads/.+ to refs/heads/(\S+)$", subject.strip())  # 체크아웃한 브랜치 이름 변경
        moment = _safe_ts(match.group(1)) if match else None
        if moment and moved:
            target = moved.group(1)
            timeline.append((moment, None if re.fullmatch(r"[0-9a-f]{7,40}", target) else target))
    # reflog는 최신부터 나온다. 같은 초의 이동이 시간순을 지키게 뒤집은 뒤 안정 정렬한다(#118 Codex 리뷰)
    return sorted(reversed(timeline), key=lambda item: item[0])


def branch_at(timeline, moment: dt.datetime, default: str | None) -> str | None:
    """moment에 체크아웃돼 있던 브랜치. 기록보다 앞이면 default(세션 시작 때 Codex가 적은 브랜치)."""
    found = default
    for changed, branch in timeline:
        if changed > moment:
            break
        found = branch
    return found


class CodexSessions:
    """`<CODEX_HOME>/sessions/**/rollout-*.jsonl` 중 이 저장소 worktree에서 연 세션. 파일 크기·수정 시각이 같으면 캐시."""

    def __init__(self, home: Path | None = None):
        self.home = codex_home() if home is None else Path(home)
        self.cache: dict[str, tuple] = {}

    def records(self, roots: list[str], timelines: dict | None = None) -> list[tuple[str, dict]]:
        """(worktree 경로, 사건) 목록. timelines(worktree 경로 → `branch_timeline`)가 있으면 사건마다 그 시각에 체크아웃한
        브랜치를 쓴다. Codex는 세션 시작 브랜치만 적기 때문이다(#116 리뷰 F1)."""
        sessions = self.home / "sessions"
        if not roots or not sessions.is_dir():
            return []
        found = []
        key_roots = tuple(sorted(_norm(root) for root in roots))
        for path in sorted(sessions.glob("**/rollout-*.jsonl")):
            try:
                stat = path.stat()
            except OSError:
                continue
            key = (stat.st_size, stat.st_mtime_ns, key_roots)
            cached = self.cache.get(str(path))
            if not cached or cached[0] != key:
                cached = (key, codex_session_records(path, roots))
                self.cache[str(path)] = cached
            root, events = cached[1]
            timeline = (timelines or {}).get(root) or []
            for event in events:
                moment = _safe_ts(event["ts"]) if timeline else None
                found.append((root, {**event, "branch": branch_at(timeline, moment, event.get("branch"))}
                              if moment else event))
        return found


def summarize_sessions(lines, branch: str) -> dict:
    """sessions.jsonl 줄(같은 저장소의 모든 worktree 기록을 합친 것)에서 branch의 세션 사건을 센다(#96).

    - 수(세션·응답·종료 검증·도구)는 그 브랜치에서 남긴 사건만 센다. 세션 ID가 없는 사건은 세지 않는다.
    - 작업 시간은 같은 작업 구간 안의 이웃한 두 사건 사이 시간을 뒤 사건의 브랜치에 준다(응답이 끝난 브랜치).
    - `intervals`는 저장소의 모든 세션 작업 구간이다. 기록 없는 커밋 판정에 쓴다. 세션을 기본 checkout에서 띄우고
      다른 worktree에서 커밋해도, 세션 중에 만든 브랜치에서 첫 응답에 커밋해도 그 세션 구간에 든다.
    """
    events: dict[str, list[tuple]] = {}
    on_branch: set[str] = set()
    helpers: set[str] = set()  # 부모 세션의 일부(Codex 승인 검토·서브에이전트): 토큰만 센다(#116)
    tools: dict[str, str] = {}
    turns = malformed = 0
    verify = {key: 0 for key in VERIFY_OUTCOMES}
    usage_errors: dict[str, set[str]] = {}
    for line in lines:
        if not line.strip():
            continue
        parsed = read_session_record(line)
        if parsed == "malformed":
            malformed += 1
            continue
        if parsed is None:
            continue
        session, moment, event, record_branch, record = parsed
        partial = isinstance(record.get("usage_error"), str)
        events.setdefault(session, []).append((moment, event, record_branch, clean_usage(record.get("usage")), partial))
        if record_branch != branch:
            continue
        if isinstance(record.get("parent"), str) and record["parent"]:
            helpers.add(session)
            continue
        on_branch.add(session)
        tools.setdefault(session, record["tool"] if isinstance(record.get("tool"), str) else "?")
        if isinstance(record.get("usage_error"), str) and event in ("stop", "end"):
            usage_errors.setdefault(session, set()).add(record["usage_error"])
        if event == "stop":
            turns += 1
            outcome = record.get("verify")
            if isinstance(outcome, str) and outcome in verify:
                verify[outcome] += 1
    active = 0.0
    usage: dict = {}
    for session in on_branch | helpers:
        ordered = sorted(events[session], key=lambda item: item[0])
        for (previous, *_), (moment, event, record_branch, *_) in zip(ordered, ordered[1:]):
            if session in on_branch and record_branch == branch and continues(previous, moment, event):
                active += (moment - previous).total_seconds()
        cumulative: dict = {}
        for _, _, record_branch, current, partial in ordered:  # 누적 usage의 차를 그 사건의 브랜치에 준다(#97)
            if current is None:
                continue
            delta, cumulative = usage_delta(current, cumulative, partial)
            if record_branch == branch:
                add_usage(usage, delta)
    by_tool: dict[str, int] = {}
    for tool in tools.values():
        by_tool[tool] = by_tool.get(tool, 0) + 1
    intervals = sorted(segment for items in events.values() for segment in session_segments(items))
    return {"branch": branch, "sessions": len(on_branch), "turns": turns, "active_sec": int(active),
            "verify": verify, "tools": by_tool, "intervals": intervals, "malformed": malformed, "usage": usage,
            "usage_errors": {session: sorted(reasons) for session, reasons in sorted(usage_errors.items())}}


def unrecorded_commits(commits, intervals, grace: dt.timedelta = UNRECORDED_GRACE) -> list[str]:
    """세션 구간 [첫 기록, 마지막 기록 + grace] 어디에도 들지 않는 커밋(sha, committer 시각 목록에서).

    hooks가 꺼졌거나 다른 도구·사람이 만든 커밋의 대리 신호다. 단정하지 않는다.
    """
    return [sha for sha, moment in commits
            if not any(start <= moment <= end + grace for start, end in intervals)]


def format_duration(seconds: int) -> str:
    hours, rest = divmod(max(seconds, 0), 3600)
    return f"{hours}시간 {rest // 60}분" if hours else f"{rest // 60}분"


def usage_lines(usage: dict, pricing: dict, errors: dict | None = None) -> list[str]:
    """토큰 합과 추정 비용 줄. 비용은 항상 "추정"이고 단가 기준과 통화를 함께 적는다.
    transcript를 다 읽지 못한 세션이 있으면 합이 일부라고 알린다(#97 리뷰 B F1)."""
    totals = {field: sum(counts[field] for counts in usage.values()) for field in USAGE_FIELDS}
    reasons = sorted({reason for found in (errors or {}).values() for reason in found})
    warning = [f"- 토큰을 다 읽지 못한 세션: {len(errors)} ({', '.join(reasons)}). 아래 합은 일부다"] if errors else []
    if not any(totals.values()):
        return warning + ["- 토큰: 기록 없음"]
    lines = [f"- 토큰: 입력 {totals['input']:,} · 출력 {totals['output']:,} · 캐시 쓰기 "
             f"{totals['cache_write_5m'] + totals['cache_write_1h']:,} · 캐시 읽기 {totals['cache_read']:,}"]
    cost, unpriced = estimate_cost(usage, pricing)
    if unpriced:  # 일부 모델만 센 합은 비용이 적게 보인다. 숫자를 내지 않는다(#116: Codex·OpenAI 모델은 키트 단가가 없다)
        lines.append(f"- 추정 비용: 계산하지 않음(단가 없는 모델: {', '.join(unpriced)}). "
                     "harness.json `pricing.models`에 단가를 넣는다")
    else:
        lines.append(f"- 추정 비용: {cost:,.2f} {pricing['currency']} (단가 기준 {pricing['as_of']})")
    return warning + lines


def sessions_text(summary: dict, unrecorded: int | None, total: int | None, pricing: dict | None = None) -> str:
    """`harness sessions summary` 출력. MR 본문 절은 #98에서 정한다."""
    verify = summary["verify"]
    tools = ", ".join(f"{tool} {count}" for tool, count in sorted(summary["tools"].items())) or "없음"
    lines = [f"세션 기록 (브랜치 {summary['branch']})",
             f"- 세션: {summary['sessions']} ({tools})",
             f"- 응답: {summary['turns']}, 작업 시간: {format_duration(summary['active_sec'])}",
             f"- 종료 검증: 통과 {verify['pass']} · 실패 {verify['fail']} · 같은 상태 재사용 "
             f"{verify['cached-pass'] + verify['cached-fail']} · 대상 없음 {verify['none']} · 오류 {verify['error']}"]
    lines += usage_lines(summary.get("usage", {}), pricing or DEFAULT_PRICING, summary.get("usage_errors"))
    if total is not None:
        lines.append(f"- 기록 없는 커밋: {unrecorded}/{total}")
    return "\n".join(lines) + "\n"


AGENT_SECTION = "에이전트 기록"
AGENT_TOOLS_LINE = re.compile(r"^도구\s*[:：]\s*(.+)$")
# 도구 이름은 글자·숫자·공백·`._-`만(사람이 쓴 `Claude Code 2`·`클로드 1`도 받는다). 이름은 주간 요약 메시지에
# 들어가므로 `@channel`·`<!channel>` 같은 멘션 문법을 받지 않는다(#101 리뷰 F2)
AGENT_TOOL_ITEM = re.compile(r"^([\w][\w .-]{0,39}?)\s+(\d{1,6})$")
AGENT_INT_LINES = {"turns": re.compile(r"^응답\s*[:：]\s*(\d{1,9})$"),
                   "active_min": re.compile(r"^작업 시간\(분\)\s*[:：]\s*(\d{1,9})$")}
# 수는 자릿수를 묶어 읽는다(본문은 신뢰할 수 없는 입력, #98 리뷰 F1·F2). 토큰은 15자리, 비용은 정수 9자리까지
_TOKEN_NUMBER = r"(\d{1,3}(?:,\d{3}){0,4}|\d{1,15})"
AGENT_TOKENS_LINE = re.compile(rf"^토큰\s*[:：]\s*입력\s*{_TOKEN_NUMBER}\s*·\s*출력\s*{_TOKEN_NUMBER}\s*·\s*캐시 쓰기\s*"
                               rf"{_TOKEN_NUMBER}\s*·\s*캐시 읽기\s*{_TOKEN_NUMBER}$")
CURRENCY_TEXT = re.compile(r"[A-Za-z]{3}|[원₩$€¥£]")  # fullmatch로 쓴다
AGENT_COST_LINE = re.compile(r"^추정 비용\(([A-Za-z]{3}|[원₩$€¥£])\)\s*[:：]\s*(\d{1,9}(?:\.\d{1,6})?)$")
AGENT_TOKEN_KEYS = ("input", "output", "cache_write", "cache_read")
AGENT_UNRECORDED_LINE = re.compile(r"^기록 없는 커밋\s*[:：]\s*(\d{1,6})\s*/\s*(\d{1,6})$")


def agent_markdown(summary: dict, unrecorded: int | None, total: int | None, pricing: dict | None = None) -> str:
    """MR 본문 `## 에이전트 기록` 절(#98). 라벨이 붙은 줄은 수집기가 읽으므로 형식을 바꾸지 않는다."""
    pricing = pricing or DEFAULT_PRICING
    tools = ", ".join(f"{tool} {count}" for tool, count in sorted(summary["tools"].items())) or "없음"
    usage = summary.get("usage", {})
    totals = {field: sum(counts[field] for counts in usage.values()) for field in USAGE_FIELDS}
    lines = [f"## {AGENT_SECTION}", "", f"- 도구: {tools}", f"- 응답: {summary['turns']}",
             f"- 작업 시간(분): {summary['active_sec'] // 60}"]
    notes = []
    if usage:
        lines.append(f"- 토큰: 입력 {totals['input']:,} · 출력 {totals['output']:,} · 캐시 쓰기 "
                     f"{totals['cache_write_5m'] + totals['cache_write_1h']:,} · 캐시 읽기 {totals['cache_read']:,}")
        cost, unpriced = estimate_cost(usage, pricing)
        if unpriced:  # 비용 줄을 내지 않는다(수집기가 비용 미보고로 센다). 일부만 센 합이 지표 6을 낮추지 않게(#116)
            notes = [f"단가 없는 모델 {', '.join(unpriced)}이 있어 추정 비용을 내지 않았다(harness.json pricing.models)"]
        else:
            lines.append(f"- 추정 비용({pricing['currency']}): {cost:.2f}")
            notes = [f"단가 기준 {pricing['as_of']}"]
    else:  # 토큰 기록이 하나도 없으면 0이 아니라 "기록 없음"(수집기가 측정하지 않은 MR로 센다, #108 Codex 리뷰)
        lines.append("- 토큰: 기록 없음")
    if summary.get("usage_errors"):
        notes.append(f"토큰을 다 읽지 못한 세션 {len(summary['usage_errors'])}개" + ("(합은 일부)" if usage else ""))
    if notes:
        lines.append(f"- 참고: {'; '.join(notes)}")
    if total is not None:
        lines.append(f"- 기록 없는 커밋: {unrecorded}/{total}")
        if unrecorded:
            lines.append("<!-- 기록 없는 커밋은 hooks가 꺼졌거나 다른 도구·사람이 만든 커밋일 수 있다. "
                         "다른 도구로 작업했으면 도구 줄에 이름과 세션 수를 더한다. -->")
    return "\n".join(lines) + "\n"


def _count(text: str) -> int:
    """`1,234` 꼴 정수."""
    return int(text.replace(",", ""))


def parse_agent_lines(lines: list[str]) -> dict | None:
    """`## 에이전트 기록` 절의 줄(주석·코드 블록을 지운 뒤). 도구 줄이 없으면 보고하지 않은 것으로 본다(None).

    다른 수는 없어도 된다(다른 도구는 도구 줄만 적을 수 있다). 형식이 틀린 도구 항목은 건너뛴다.
    """
    result: dict = {"tools": {}, "turns": None, "active_min": None, "tokens": None, "cost": None,
                    "currency": None, "unrecorded": None, "commits": None}
    reported = tools_seen = False
    for line in lines:
        text = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s*", "", line.strip()).strip()
        if (match := AGENT_TOOLS_LINE.match(text)) and not tools_seen:
            tools_seen = True  # 다시 실행한 출력을 아래에 붙여도 처음 도구 줄만 센다(#98 리뷰 F3)
            for item in match.group(1).split(","):
                if tool := AGENT_TOOL_ITEM.match(item.strip()):
                    reported = True
                    result["tools"][tool.group(1)] = result["tools"].get(tool.group(1), 0) + int(tool.group(2))
            continue
        for key, pattern in AGENT_INT_LINES.items():
            if (match := pattern.match(text)) and result[key] is None:
                result[key] = int(match.group(1))
        if (match := AGENT_TOKENS_LINE.match(text)) and result["tokens"] is None:
            result["tokens"] = dict(zip(("input", "output", "cache_write", "cache_read"), map(_count, match.groups())))
        if (match := AGENT_COST_LINE.match(text)) and result["cost"] is None:
            result["currency"], result["cost"] = match.group(1), float(match.group(2))
        if (match := AGENT_UNRECORDED_LINE.match(text)) and result["unrecorded"] is None:
            result["unrecorded"], result["commits"] = int(match.group(1)), int(match.group(2))
    return result if reported else None


REVIEW_SECTION = "리뷰 결과"
REVIEW_OUTCOMES = {"채택": "adopted", "기각": "rejected", "후속": "followup", "판단 필요": "pending"}
REVIEW_FOUND_LINE = re.compile(r"^리뷰 발견\s*[:：]\s*(\d{1,6})\s*\(채택\s*(\d{1,6})\s*·\s*기각\s*(\d{1,6})\s*·\s*후속\s*(\d{1,6})"
                               r"\s*·\s*판단 필요\s*(\d{1,6})\)$")
REVIEW_DUP_LINE = re.compile(r"^독립 중복 발견\s*[:：]\s*(\d{1,6})$")
NO_FINDING = re.compile(r"^(?:|[-—–]|해소.*)$")  # 빈 칸·대시, 다른 리뷰의 발견을 확인만 한 기록
CELL_SPLIT = re.compile(r"(?<!\\)\|")


def _cells(line: str) -> list[str]:
    """표 한 줄의 칸. 이스케이프한 `\\|`는 칸 구분이 아니다(#99 리뷰 F3)."""
    text = line.strip()
    text = text[1:] if text.startswith("|") else text
    text = text[:-1] if text.endswith("|") and not text.endswith("\\|") else text
    return [cell.replace("\\|", "|").strip().strip("*").strip() for cell in CELL_SPLIT.split(text)]


def _found(cell: str) -> bool:
    return not NO_FINDING.match(cell)


REVIEW_SECTION_HEADING = re.compile(r"^##\s*7\.\s*처리")
FENCE_LINE = re.compile(r"^\s*(`{3,}|~{3,})")


def _unfenced(lines: list[str]) -> list[str]:
    """코드 블록 안의 줄을 빈 줄로 바꾼다(줄 번호는 그대로). 발견 사항이 인용한 표 예시를 처리 표로 읽지 않게(#110 Codex 리뷰)."""
    result, fence = [], None
    for line in lines:
        match = FENCE_LINE.match(line)
        if fence is None and match:
            fence = match.group(1)[0] * 3
            result.append("")
        elif fence is not None:
            result.append("")
            if line.strip().startswith(fence):
                fence = None
        else:
            result.append(line)
    return result


def summarize_review(text: str) -> tuple[dict | None, list[str]]:
    """리뷰 파일 7장(처리) 표를 센다(#99). (요약, 오류). 7장 제목이 있으면 그 뒤에서, 코드 블록 밖의 처리 열이 있는 첫 표를 읽는다.

    모든 칸이 빈 행(템플릿 빈 행)은 건너뛰고, 다른 칸에 내용이 있는데 처리가 비었거나 칸이 모자라면 오류다.
    A·B 열이 따로 있으면(엄격 종합 표) 두 열 모두 발견 기록(빈 칸·대시·"해소…"가 아님)인 행을, `발견` 열이면
    `A`와 `B` 표기(`A F1`·`A-1`)가 함께 있는 행을 독립 중복 발견으로 센다.
    """
    lines = _unfenced(text.splitlines())
    section = next((index for index, line in enumerate(lines) if REVIEW_SECTION_HEADING.match(line)), 0)
    for start, line in enumerate(lines):
        if start < section or not line.lstrip().startswith("|"):
            continue
        header = _cells(line)
        if "처리" not in header or start + 1 >= len(lines) or not re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[start + 1]):
            continue
        outcome_at = header.index("처리")
        a_at, b_at = (header.index("A"), header.index("B")) if {"A", "B"} <= set(header) else (None, None)
        found_at = header.index("발견") if "발견" in header else None
        counts = {key: 0 for key in REVIEW_OUTCOMES.values()}
        duplicates, errors = 0, []
        for row_line in lines[start + 2:]:
            if not row_line.lstrip().startswith("|"):
                break
            row = _cells(row_line)
            if not any(row[1:]):
                continue  # 템플릿 빈 행
            if len(row) < len(header):
                errors.append(f"칸이 모자란 행: {row[0]}")
                continue
            outcome = row[outcome_at]
            if not outcome:
                errors.append(f"처리가 비었다: {row[0]}")
                continue
            key = next((value for label, value in REVIEW_OUTCOMES.items() if outcome.startswith(label)), None)
            if key is None:
                errors.append(f"처리 값이 채택·기각·후속·판단 필요로 시작하지 않는다: {row[0]} {outcome[:20]}")
                continue
            counts[key] += 1
            if a_at is not None:
                duplicates += _found(row[a_at]) and _found(row[b_at])
            elif found_at is not None:
                cell = row[found_at]
                duplicates += bool(re.search(r"(?<![A-Za-z])A\s*-?\s*F?\d", cell)
                                   and re.search(r"(?<![A-Za-z])B\s*-?\s*F?\d", cell))
        return {"findings": sum(counts.values()), **counts, "duplicates": duplicates}, errors
    return None, ["처리 열이 있는 표가 없다(리뷰 템플릿 7장)"]


def review_lines(summary: dict) -> str:
    """MR 본문 `## 리뷰 결과`에 붙이는 두 줄. 수집기가 읽으므로 형식을 바꾸지 않는다."""
    return (f"- 리뷰 발견: {summary['findings']} (채택 {summary['adopted']} · 기각 {summary['rejected']} · "
            f"후속 {summary['followup']} · 판단 필요 {summary['pending']})\n"
            f"- 독립 중복 발견: {summary['duplicates']}\n")


def parse_review_lines(lines: list[str]) -> dict | None:
    """`## 리뷰 결과` 절의 두 줄(각각 처음 나온 줄). 리뷰 발견 줄이 없거나, 총계가 처리별 합과 다르거나,
    독립 중복이 총계보다 크면 미보고(None)로 본다(#99 리뷰 F4). 독립 중복 줄이 없으면 0이다."""
    result = duplicates = None
    for line in lines:
        text = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s*", "", line.strip()).strip()
        if (match := REVIEW_FOUND_LINE.match(text)) and result is None:
            total, *parts = map(int, match.groups())
            result = {"findings": total, **dict(zip(REVIEW_OUTCOMES.values(), parts))}
        elif (match := REVIEW_DUP_LINE.match(text)) and duplicates is None:
            duplicates = int(match.group(1))
    if result is None or result["findings"] != sum(result[key] for key in REVIEW_OUTCOMES.values()):
        return None
    result["duplicates"] = duplicates or 0
    return result if result["duplicates"] <= result["findings"] else None


def empty_week_review() -> dict:
    """주별 지표 8(#99). 엄격 MR 중 리뷰 요약을 붙인 수와 그 MR들의 채택 수(`strict_adopted`)도 센다.
    엄격 MR당 채택 결함은 분모·분자가 같은 엄격 MR이어야 한다(#110 Codex 리뷰)."""
    return {"reported_mrs": 0, "strict_reported_mrs": 0, "strict_adopted": 0, "findings": 0,
            **{key: 0 for key in REVIEW_OUTCOMES.values()}, "duplicates": 0}


def add_week_review(week: dict, review: dict | None, strict: bool) -> None:
    if review is None:
        return
    week["reported_mrs"] += 1
    week["strict_reported_mrs"] += strict
    week["strict_adopted"] += review["adopted"] if strict else 0
    for key in ("findings", *REVIEW_OUTCOMES.values(), "duplicates"):
        week[key] += review[key]


def parse_hooks_lines(lines: list[str]) -> dict | None:
    """`## hooks 기록` 절의 줄(주석·코드 블록을 지운 뒤)에서 세 수. 하나라도 없으면 보고하지 않은 것으로 본다(None)."""
    keys = {label: key for key, label in HOOKS_LABELS.items()}
    found: dict[str, int] = {}
    for line in lines:
        text = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s*", "", line.strip()).strip()
        match = HOOKS_LINE.match(text)
        if match:
            found.setdefault(keys[match.group(1)], int(match.group(2)))
    return {key: found[key] for key in HOOKS_LABELS} if len(found) == len(HOOKS_LABELS) else None


# ---------------------------------------------------------------------------
# 플랫폼 어댑터
# ---------------------------------------------------------------------------


class GitLabAdapter:
    platform = "gitlab"

    def __init__(self, client: ApiClient, project: str):
        self.client = client
        self.base = f"projects/{urllib.parse.quote(str(project), safe='')}"

    def merged_mrs(self, window: Window) -> list[dict]:
        # merged_at 필터는 GitLab 버전마다 달라 updated_after(병합 뒤 갱신 시각은 병합 시각 이후)로 받고 거른다
        url = self.client.url(f"{self.base}/merge_requests", {
            "state": "merged", "scope": "all", "updated_after": window.since.isoformat(),
            "order_by": "updated_at", "sort": "asc", "per_page": PER_PAGE})
        return [self._mr(item) for item in self.client.get_list(url)]

    def _mr(self, item: dict) -> dict:
        return {"number": item.get("iid"), "title": item.get("title"), "url": item.get("web_url"),
                "merged_at": item.get("merged_at"), "body": item.get("description"),
                "source_branch": item.get("source_branch"), "target_branch": item.get("target_branch")}

    def mr(self, number: int) -> dict | None:
        try:
            item = self.client.get_json(self.client.url(f"{self.base}/merge_requests/{number}"))
        except HttpStatusError as exc:
            if exc.status == 404:
                return None
            raise
        return self._mr(item) if isinstance(item, dict) else None

    def changed_paths(self, number: int) -> list[str]:
        url = self.client.url(f"{self.base}/merge_requests/{number}/diffs", {"per_page": PER_PAGE})
        paths = []
        for item in self.client.get_list(url):
            paths.extend(p for p in (item.get("old_path"), item.get("new_path")) if isinstance(p, str) and p)
        return list(dict.fromkeys(paths))

    def defects(self, label: str, window: Window) -> list[dict]:
        params = {"labels": label, "scope": "all", "created_after": window.since.isoformat(), "per_page": PER_PAGE}
        found = []
        for kind, path in (("issue", "issues"), ("mr", "merge_requests")):
            for item in self.client.get_list(self.client.url(f"{self.base}/{path}", params)):
                found.append({"kind": kind, "number": item.get("iid"), "title": item.get("title"),
                              "url": item.get("web_url"), "created_at": item.get("created_at"),
                              "body": item.get("description")})
        return found

    def failed_jobs(self, window: Window, security_jobs: set[str]) -> list[dict]:
        # updated_before로 자르면 기간 뒤에 끝났거나 재시도한 파이프라인의 기간 내 실패 job이 빠진다. job은 created_at으로 거른다
        url = self.client.url(f"{self.base}/pipelines", {"updated_after": window.since.isoformat(), "per_page": PER_PAGE})
        jobs = []
        for pipeline in self.client.get_list(url):
            pid = pipeline.get("id")
            if not isinstance(pid, int):
                continue
            jobs_url = self.client.url(f"{self.base}/pipelines/{pid}/jobs",
                                       [("scope[]", "failed"), ("include_retried", "true"), ("per_page", PER_PAGE)])
            for job in self.client.get_list(jobs_url):
                if job.get("status") != "failed" or not isinstance(job.get("id"), int):
                    continue
                jobs.append({"job": {"id": job["id"], "name": job.get("name"), "stage": job.get("stage"),
                                     "status": "failed", "failure_reason": job.get("failure_reason")},
                             "created_at": job.get("created_at"), "url": job.get("web_url"), "pipeline": pid,
                             "ref": pipeline.get("ref") if isinstance(pipeline.get("ref"), str) else None})
        return jobs

    def trace(self, job_id: int) -> str | None:
        return _fetch_trace(self.client, self.client.url(f"{self.base}/jobs/{job_id}/trace"))


class GitHubAdapter:
    platform = "github"

    def __init__(self, client: ApiClient, project: str):
        if not GITHUB_REPO.match(project or ""):
            raise CollectError(f"GitHub --project는 owner/repo 형식이어야 한다: {project!r}")
        self.client = client
        self.base = f"repos/{project}"

    def merged_mrs(self, window: Window) -> list[dict]:
        url = self.client.url(f"{self.base}/pulls", {"state": "closed", "sort": "updated", "direction": "desc",
                                                     "per_page": PER_PAGE})
        # 갱신 시각 내림차순이라 기간 시작보다 오래된 항목이 나오면 뒤는 모두 더 오래됐다
        older = lambda item: (parse_ts(item.get("updated_at")) or window.since) < window.since  # noqa: E731
        return [self._mr(item) for item in self.client.get_list(url, stop=older) if item.get("merged_at")]

    def _mr(self, item: dict) -> dict:
        head, base = item.get("head") or {}, item.get("base") or {}
        return {"number": item.get("number"), "title": item.get("title"), "url": item.get("html_url"),
                "merged_at": item.get("merged_at"), "body": item.get("body"),
                "source_branch": head.get("ref") if isinstance(head, dict) else None,
                "target_branch": base.get("ref") if isinstance(base, dict) else None}

    def mr(self, number: int) -> dict | None:
        try:
            item = self.client.get_json(self.client.url(f"{self.base}/pulls/{number}"))
        except HttpStatusError as exc:
            if exc.status == 404:
                return None
            raise
        return self._mr(item) if isinstance(item, dict) else None

    def changed_paths(self, number: int) -> list[str]:
        url = self.client.url(f"{self.base}/pulls/{number}/files", {"per_page": PER_PAGE})
        paths = []
        for item in self.client.get_list(url):
            paths.extend(p for p in (item.get("previous_filename"), item.get("filename")) if isinstance(p, str) and p)
        return list(dict.fromkeys(paths))

    def defects(self, label: str, window: Window) -> list[dict]:
        # since는 갱신 시각 기준이라 생성 시각으로 다시 거른다. PR도 같은 목록에 나온다(pull_request 키)
        url = self.client.url(f"{self.base}/issues", {"labels": label, "state": "all", "since": utc_text(window.since),
                                                      "per_page": PER_PAGE})
        found = []
        for item in self.client.get_list(url):
            created = parse_ts(item.get("created_at"))
            if created is None or created < window.since:
                continue
            found.append({"kind": "mr" if "pull_request" in item else "issue", "number": item.get("number"),
                          "title": item.get("title"), "url": item.get("html_url"),
                          "created_at": item.get("created_at"), "body": item.get("body")})
        return found

    def failed_jobs(self, window: Window, security_jobs: set[str]) -> list[dict]:
        url = self.client.url(f"{self.base}/actions/runs", {
            "created": f"{utc_text(window.since)}..{utc_text(window.until - dt.timedelta(seconds=1))}",
            "per_page": PER_PAGE})
        jobs = []
        for run in self.client.get_list(url, key="workflow_runs"):
            rid = run.get("id")
            # 다시 실행한 run은 앞 시도의 실패 job이 있을 수 있어 결론이 성공이어도 job을 본다. 취소된 run에도 실패 job이 남는다
            retried = isinstance(run.get("run_attempt"), int) and run["run_attempt"] > 1
            if not isinstance(rid, int) or (run.get("conclusion") not in ("failure", "timed_out", "cancelled")
                                            and not retried):
                continue
            jobs_url = self.client.url(f"{self.base}/actions/runs/{rid}/jobs", {"filter": "all", "per_page": PER_PAGE})
            for job in self.client.get_list(jobs_url, key="jobs"):
                converted = convert_github_job(job, security_jobs)
                if converted["status"] != "failed" or not isinstance(converted["id"], int):
                    continue
                jobs.append({"job": converted, "created_at": run.get("created_at"), "url": job.get("html_url"),
                             "pipeline": rid,
                             "ref": run.get("head_branch") if isinstance(run.get("head_branch"), str) else None})
        return jobs

    def trace(self, job_id: int) -> str | None:
        return _fetch_trace(self.client, self.client.url(f"{self.base}/actions/jobs/{job_id}/logs"))


def convert_github_job(job: dict, security_jobs: set[str]) -> dict:
    """README 수집 어댑터의 jq 변환과 같다. timed_out은 실패 + job_execution_timeout, 보안 job은 harness- 접두."""
    name = job.get("name")
    conclusion = job.get("conclusion")
    if isinstance(name, str) and name in security_jobs:
        name = SECURITY_PREFIX + name
    return {"id": job.get("id"), "name": name, "stage": None,
            "failure_reason": "job_execution_timeout" if conclusion == "timed_out" else None,
            "status": "failed" if conclusion in ("failure", "timed_out") else (conclusion or job.get("status"))}


def _fetch_trace(client: ApiClient, url: str) -> str | None:
    """trace 끝 1 MiB. 만료·삭제(404·410)면 None(메타데이터만으로 분류)."""
    try:
        response = client.request(url, tail=TRACE_TAIL_BYTES)
    except HttpStatusError as exc:
        if exc.status in (404, 410):
            return None
        raise
    return response.body.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 수집
# ---------------------------------------------------------------------------


def _number(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def add_week_agent(week: dict, agent: dict | None) -> None:
    if agent is None:
        week["unreported_mrs"] += 1
        return
    week["reported_mrs"] += 1
    week["sessions"] += sum(agent["tools"].values())
    week["turns"] += agent["turns"] or 0
    week["active_min"] += agent["active_min"] or 0
    if agent["tokens"] is not None:
        week["tokens_mrs"] += 1
        for key in AGENT_TOKEN_KEYS:
            week["tokens"][key] += agent["tokens"][key]
    if agent["cost"] is not None:
        week["cost_mrs"] += 1
        week["cost"][agent["currency"]] = round(week["cost"].get(agent["currency"], 0.0) + agent["cost"], 6)
    for tool in agent["tools"]:
        week["tools"][tool] = week["tools"].get(tool, 0) + 1


UNREPORTED_TOOL = "미보고"


def mr_refs(mr: dict, platform: str) -> set[str]:
    """MR 파이프라인·워크플로 run이 가질 수 있는 ref. GitLab MR 파이프라인은 `refs/merge-requests/<iid>/head`다."""
    refs = {mr["source_branch"]} if isinstance(mr.get("source_branch"), str) else set()
    if platform == "gitlab":
        refs.add(f"refs/merge-requests/{mr['number']}/head")
    return refs


def by_tool(mrs: list[dict], defects: list[dict], jobs: list[dict], platform: str) -> dict:
    """지표 7(#98): 도구별 MR 수와 그 MR들 중 CI 실패·병합 후 누락 원인·판정 불일치가 있었던 수.

    한 MR이 여러 도구를 적으면 각 도구에 센다. 절이 없는 MR은 `미보고`로 묶는다. CI 실패는 실패 job의 `ref`가
    MR 소스 브랜치(또는 GitLab MR ref)와 같을 때 그 MR의 것으로 본다(같은 이름의 브랜치를 다시 쓰면 섞일 수 있다).
    """
    failed_refs = {job["ref"] for job in jobs if isinstance(job.get("ref"), str)}
    causes = {defect["cause"] for defect in defects if defect.get("status") == "linked"}
    result: dict[str, dict] = {}
    for mr in mrs:
        if mr.get("integration") or mr.get("tier") is None:
            continue
        tools = list(mr["agent"]["tools"]) if mr.get("agent") else [UNREPORTED_TOOL]
        for tool in tools:
            row = result.setdefault(tool, {"mrs": 0, "ci_failed_mrs": 0, "escaped_mrs": 0, "mismatch_mrs": 0})
            row["mrs"] += 1
            row["ci_failed_mrs"] += bool(mr_refs(mr, platform) & failed_refs)
            row["escaped_mrs"] += mr["number"] in causes
            row["mismatch_mrs"] += bool(mr["tier"]["mismatch"])
    return dict(sorted(result.items()))


def collect(adapter, judge: Judge, window: Window, defect_label: str, security_jobs: set[str]) -> dict:
    weeks = window_weeks(window.since, window.until, window.tz)
    by_week = {entry["week"]: entry for entry in weeks}

    mrs, merged = [], {}
    for item in adapter.merged_mrs(window):
        number, merged_at = _number(item["number"]), parse_ts(item["merged_at"])
        if number is None or not window.contains(merged_at) or number in merged:
            continue
        week = window.week(merged_at)
        record = {"number": number, "title": item["title"], "url": item["url"], "merged_at": item["merged_at"],
                  "week": week, "source_branch": item["source_branch"], "target_branch": item["target_branch"],
                  "integration": judge.is_integration(item["source_branch"], item["target_branch"]),
                  "files": 0, "tier": None, "mismatch_higher": None, "hooks": None, "agent": None, "review": None}
        merged[number] = record
        mrs.append(record)
        if record["integration"]:
            continue  # 통합 MR은 본문 판정이 없고 다른 MR을 모은 것이라 세지 않는다
        paths = adapter.changed_paths(number)
        record["files"] = len(paths)
        record["paths"] = paths  # 판정 eval 사례 후보(`harness metrics cases`)
        record["tier"] = judge.tier(item["body"], paths)
        record["mismatch_higher"] = higher_side(record["tier"])
        entry = by_week[week]
        entry["merged_mrs"] += 1
        entry["strict"] += record["tier"]["effective"] == "strict"
        entry["mismatch"] += bool(record["tier"]["mismatch"])
        record["hooks"] = judge.hooks(item["body"])
        if record["hooks"] is None:
            entry["hooks"]["unreported_mrs"] += 1
        else:
            entry["hooks"]["reported_mrs"] += 1
            for key in HOOKS_LABELS:
                entry["hooks"][key] += record["hooks"][key]
        record["agent"] = judge.agent(item["body"])
        add_week_agent(entry["agent"], record["agent"])
        record["review"] = judge.review(item["body"])
        add_week_review(entry["review"], record["review"], record["tier"]["effective"] == "strict")

    defects, looked_up = [], {}
    for item in adapter.defects(defect_label, window):
        cause = judge.cause(adapter.platform, item["body"])
        created = parse_ts(item["created_at"])
        record = {"kind": item["kind"], "number": item["number"], "title": item["title"], "url": item["url"],
                  "created_at": item["created_at"], "cause": cause, "cause_merged_at": None, "week": None,
                  "status": None}
        if not window.contains(created):
            # 기간 밖(특히 --until 뒤)에 만든 결함은 세지 않고 원자료에만 남긴다. 같은 창을 다시 수집해도 결과가 같다
            record["status"] = "out_of_window"
            defects.append(record)
            continue
        if cause is None:
            record["week"], record["status"] = window.week(created), "unlinked"
            by_week[record["week"]]["unlinked_defects"] += 1
            defects.append(record)
            continue
        if cause in merged:
            cause_mr = merged[cause]
        else:
            if cause not in looked_up:
                looked_up[cause] = adapter.mr(cause)
            cause_mr = looked_up[cause]
        merged_at = parse_ts(cause_mr.get("merged_at")) if cause_mr else None
        record["cause_merged_at"] = cause_mr.get("merged_at") if cause_mr else None
        if cause_mr is None or merged_at is None:
            record["status"] = "cause_not_merged"
        elif window.contains(merged_at):
            record["week"], record["status"] = window.week(merged_at), "linked"
            by_week[record["week"]]["escaped_defects"] += 1
        else:
            record["status"] = "out_of_window"
        defects.append(record)

    jobs = []
    for item in adapter.failed_jobs(window, security_jobs):
        created = parse_ts(item["created_at"])
        if not window.contains(created):
            continue
        result = classify_ci.classify_job(item["job"], adapter.trace(item["job"]["id"]))
        week = window.week(created)
        by_week[week]["ci_failures"][result["category"]] += 1
        jobs.append({"id": result["job_id"], "name": result["name"], "stage": result["stage"],
                     "failure_reason": result["failure_reason"], "category": result["category"],
                     "rules": list(dict.fromkeys(m["rule"] for m in result["matches"])),
                     "pipeline": item["pipeline"], "ref": item.get("ref"), "url": item["url"],
                     "created_at": item["created_at"], "week": week})

    return {"weeks": weeks, "mrs": mrs, "defects": defects, "jobs": jobs,
            "by_tool": by_tool(mrs, defects, jobs, adapter.platform)}


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """collect.py와 `harness metrics collect`가 같은 옵션을 쓰게 한다."""
    parser.add_argument("--platform", required=True, choices=PLATFORMS)
    parser.add_argument("--since", required=True, help="시작 날짜 YYYY-MM-DD(포함, --utc-offset 기준 0시)")
    parser.add_argument("--until", help="끝 날짜 YYYY-MM-DD(제외). 생략하면 지금")
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"출력 JSON(기본 {DEFAULT_OUT})")
    parser.add_argument("--api-url", help="API 주소(GitLab 기본 CI_API_V4_URL, GitHub 기본 GITHUB_API_URL 또는 "
                                          f"{DEFAULT_GITHUB_API})")
    parser.add_argument("--project", help="프로젝트(GitLab 기본 CI_PROJECT_ID, GitHub 기본 GITHUB_REPOSITORY owner/repo)")
    parser.add_argument("--utc-offset", default=DEFAULT_OFFSET, help=f"주 경계 시간대(기본 {DEFAULT_OFFSET})")
    parser.add_argument("--defect-label", default=DEFAULT_DEFECT_LABEL,
                        help=f"병합 후 발견 결함 라벨(기본 {DEFAULT_DEFECT_LABEL})")
    parser.add_argument("--security-jobs", default="",
                        help="GitHub 보안 검사 job 이름(쉼표 구분). harness- 접두를 붙여 security로 분류한다")
    parser.add_argument("--common", default=DEFAULT_COMMON, help=f"harness_common.py(기본 {DEFAULT_COMMON})")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help=f"harness.json(기본 {DEFAULT_CONFIG})")
    parser.add_argument("--mr-lint", help="mr_lint.py(기본 키트 core/ci/mr-lint, 없으면 .harness/mr-lint)")


def resolve_target(args, env: dict) -> tuple[str, str, dict]:
    """(API 주소, 프로젝트, 인증 헤더). 토큰은 헤더에만 넣는다."""
    if args.platform == "gitlab":
        api_url, project = args.api_url or env.get("CI_API_V4_URL"), args.project or env.get("CI_PROJECT_ID")
        token = env.get(TOKEN_ENV)
        if not token:
            raise CollectError(f"GitLab 수집에는 {TOKEN_ENV}(read_api 범위의 프로젝트 액세스 토큰)이 필요하다. "
                               "CI job 토큰(CI_JOB_TOKEN)은 MR·파이프라인 목록 API를 읽지 못한다.")
        auth = {"PRIVATE-TOKEN": token}
    else:
        api_url = args.api_url or env.get("GITHUB_API_URL") or DEFAULT_GITHUB_API
        project = args.project or env.get("GITHUB_REPOSITORY")
        token = env.get(TOKEN_ENV) or env.get(GITHUB_TOKEN_ENV)
        if not token:
            raise CollectError(f"GitHub 수집에는 {TOKEN_ENV} 또는 {GITHUB_TOKEN_ENV}가 필요하다.")
        auth = {"Authorization": f"Bearer {token}"}
    if not api_url:
        raise CollectError("API 주소가 없다. --api-url을 준다(GitLab CI는 CI_API_V4_URL).")
    if not api_url.lower().startswith("https://"):
        raise CollectError("API 주소는 https여야 한다(토큰을 평문으로 보내지 않는다).")
    if not project:
        raise CollectError("프로젝트가 없다. --project를 준다.")
    return api_url, project, auth


def build_window(args) -> Window:
    tz = parse_offset(args.utc_offset)
    since = parse_date(args.since, tz, "--since")
    until = parse_date(args.until, tz, "--until") if args.until else dt.datetime.now(tz).replace(microsecond=0)
    if until <= since:
        raise CollectError("--until은 --since보다 뒤여야 한다.")
    return Window(since, until, tz)


def write_atomic(path: Path, text: str) -> None:
    """임시 파일에 쓴 뒤 교체한다. 실패하면 기존 파일을 남긴다."""
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def run(args, env: dict, transport=None) -> dict:
    """수집해 출력 파일을 쓰고 결과 dict를 돌려준다. 오류는 CollectError."""
    window = build_window(args)
    api_url, project, auth = resolve_target(args, env)
    mr_lint = load_mr_lint(args.mr_lint)
    common_path = Path(args.common)
    try:
        common = mr_lint.load_common(common_path)
    except mr_lint.LintError as exc:
        raise CollectError(str(exc)) from None
    judge = Judge(mr_lint, common, load_judge_config(Path(args.config), common))
    security_jobs = {name.strip() for name in (args.security_jobs or "").split(",") if name.strip()}
    if args.platform == "gitlab":
        client = ApiClient(api_url, auth, {"Accept": "application/json", "User-Agent": USER_AGENT}, transport)
        adapter = GitLabAdapter(client, project)
    else:
        client = ApiClient(api_url, auth, {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT,
                                           "X-GitHub-Api-Version": "2022-11-28"}, transport)
        adapter = GitHubAdapter(client, project)
    data = collect(adapter, judge, window, args.defect_label, security_jobs)
    result = {
        "version": SCHEMA_VERSION,
        "platform": args.platform,
        "project": project,
        "window": {"since": window.since.isoformat(), "until": window.until.isoformat(),
                   "utc_offset": args.utc_offset},
        "generated_at": dt.datetime.now(window.tz).replace(microsecond=0).isoformat(),
        **data,
    }
    try:
        write_atomic(Path(args.out), json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    except OSError as exc:
        raise CollectError(f"출력 파일을 쓸 수 없다({type(exc).__name__}): {args.out}") from None
    return result


def summary(result: dict, out: str) -> str:
    counted = [m for m in result["mrs"] if not m["integration"]]
    linked = sum(1 for d in result["defects"] if d["status"] == "linked")
    return (f"주 {len(result['weeks'])}개, 병합 MR {len(counted)}개, 병합 후 결함 {linked}개, "
            f"실패 job {len(result['jobs'])}개를 {out}에 썼다.")


def main(argv: list[str] | None = None, env: dict | None = None, transport=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="collect", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    add_arguments(parser)
    args = parser.parse_args(argv)
    try:
        result = run(args, dict(os.environ) if env is None else env, transport)
    except CollectError as exc:
        print(f"harness: 수집할 수 없다: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(f"harness: {summary(result, args.out)}", file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
