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
                      "ci_failures": {category: 0 for category in CATEGORIES}})
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
                             "created_at": job.get("created_at"), "url": job.get("web_url"), "pipeline": pid})
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
                             "pipeline": rid})
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
                  "files": 0, "tier": None, "mismatch_higher": None}
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
                     "pipeline": item["pipeline"], "url": item["url"], "created_at": item["created_at"], "week": week})

    return {"weeks": weeks, "mrs": mrs, "defects": defects, "jobs": jobs}


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
