"""CI 실패 원인 분류기(측정 3번). 표준 라이브러리만 사용한다.

실패한 job의 메타데이터(GitLab job API 형태)와 trace 텍스트를 받아 원인 범주 하나를 정한다.
네트워크로 수집하지 않는다. 수집은 `glab api` 등의 출력을 파일로 저장해 넘긴다(README 참고).

판정 순서 (앞에서 정해지면 뒤는 matches에만 남는다)
1. `failure_reason`: 타임아웃·runner 계열만 결정한다. `script_failure` 등은 결정하지 않는다.
2. 구조화 표식: trace의 `<NAME>_RESULT={...}` JSON에 있는 `category`
3. job 이름: `harness-`로 시작하면 보안 검사
4. 로그 정규식: 타임아웃 > 인프라 > 포맷 > 테스트
"""

from __future__ import annotations

import json
import re
from pathlib import Path

CATEGORIES = ("test", "format", "infra", "timeout", "security", "unclassified")
SECURITY_JOB_PREFIX = "harness-"
FAILED_STATUS = "failed"

# GitLab failure_reason 중 원인을 바로 정하는 값. 나머지(script_failure, unknown_failure 등)는 로그를 본다.
REASON_CATEGORIES = {
    "job_execution_timeout": "timeout",
    "stuck_or_timeout_failure": "timeout",
    "runner_system_failure": "infra",
    "api_failure": "infra",
    "scheduler_failure": "infra",
    "data_integrity_failure": "infra",
    "runner_unsupported": "infra",
    "no_matching_runner": "infra",
    "missing_dependency_failure": "infra",
    "unmet_prerequisites": "infra",
    "ci_quota_exceeded": "infra",
}

MARKER = re.compile(r"\b([A-Z][A-Z0-9_]*_RESULT)=(\{.*\})\s*$")

# (규칙 ID, 범주, 정규식). 범주 순서가 로그 판정 우선순위다.
LOG_CATEGORY_ORDER = ("timeout", "infra", "format", "test")
LOG_RULES = [
    ("timeout.job_execution", "timeout", r"execution took longer than \S+ seconds"),

    ("infra.dns", "infra",
     r"Could not resolve host|Temporary failure in name resolution|getaddrinfo (?:ENOTFOUND|EAI_AGAIN)"
     r"|\bno such host\b|UnknownHostException|Name or service not known"),
    ("infra.network", "infra",
     r"Connection reset by peer|\bECONNRESET\b|\bETIMEDOUT\b|\bEAI_AGAIN\b|TLS handshake timeout"
     r"|[Nn]etwork is unreachable|Could not (?:GET|HEAD) '|npm (?:ERR!|error) network"),
    ("infra.registry", "infra",
     r"toomanyrequests|pull rate limit|failed to pull image|[Ee]rror pulling image|ImagePullBackOff"
     r"|manifest unknown"),
    ("infra.oom", "infra",
     r"OutOfMemoryError|JavaScript heap out of memory|Cannot allocate memory|OOMKilled"
     r"|exit(?:ed with)? code 137|^\s*Killed\s*$"),
    ("infra.disk", "infra", r"No space left on device"),
    ("infra.runner", "infra",
     r"Job failed \(system failure\)|ERROR: Preparation failed|Cannot connect to the Docker daemon"),

    ("format.spotless", "format",
     r"The following files had format violations|Task :\S*spotless\w*Check FAILED|Execution failed for task '[^']*:spotless\w*'"
     r"|Run '[^']*spotlessApply' to fix"),
    ("format.prettier", "format", r"Code style issues found in|Forgot to run Prettier\?"),
    # 경고만 있으면 eslint는 0으로 끝나 job을 실패시키지 않으므로 오류가 1개 이상일 때만 맞춘다
    ("format.eslint", "format", r"\b\d+ problems? \([1-9]\d* errors?, \d+ warnings?\)"),
    ("format.python", "format", r"\d+ files? would be reformatted|^would reformat |^Would reformat: "),
    ("format.java_lint", "format",
     r"Checkstyle rule violations were found|\[ant:checkstyle\]|ktlint\w* FAILED|Lint error > "),

    ("test.gradle", "test",
     r"There were failing tests|\d+ tests completed, \d+ failed|Task :\S*[tT]est FAILED|Execution failed for task '[^']*[tT]est'"
     r"|^(?=.*\S FAILED$)\S.* > "),  # 줄 끝을 먼저 확인해 ` > `가 많은 긴 줄에서 되추적이 커지지 않게 한다
    ("test.maven", "test", r"There are test failures|Tests run: \d+, Failures: \d+, Errors: \d+.*<<< FAIL"),
    ("test.jest", "test",
     r"^Tests:\s+.*\b\d+ failed|^Test Files\s+.*\b\d+ failed|^\s*FAIL\s+\S+\.(?:test|spec)\.[cm]?[jt]sx?\b"),
    ("test.pytest", "test", r"short test summary info|^FAILED \S+::|^=+ .*\b\d+ failed\b.* in [\d.]+s"),
    ("test.unittest", "test", r"^FAILED \((?:failures|errors)=\d+"),
    ("test.go", "test", r"^--- FAIL: |^FAIL\t\S+"),
]
_COMPILED_RULES = [(rule, category, re.compile(pattern)) for rule, category, pattern in LOG_RULES]

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
# GitLab FF_TIMESTAMPS 접두 `2024-01-02T03:04:05.123456Z 00O+ `, gh 로그 접두 `2024-01-02T03:04:05.1234567Z `
TIMESTAMP_PREFIX = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z (?:[0-9a-f]{2}[OE]\+? )?")
MAX_ID_DIGITS = 18


class ClassifyError(Exception):
    """입력 형식 오류. CLI가 사용자 메시지로 바꿔 보여 준다."""


def normalize_lines(trace: str) -> list[str]:
    """trace를 화면에 보이는 줄 목록으로 바꾼다. 원본 줄 번호(1부터)와 위치가 같다."""
    lines = []
    for raw in trace.split("\n"):
        line = ANSI.sub("", raw.rstrip("\r"))
        segments = [seg for seg in line.split("\r") if seg.strip()]
        line = segments[-1] if segments else ""  # 진행률·section 표식은 마지막으로 덮어쓴 내용만 보인다
        lines.append(TIMESTAMP_PREFIX.sub("", line))
    return lines


def _match(rule: str, category: str, line: int | None, count: int = 1) -> dict:
    return {"rule": rule, "category": category, "line": line, "count": count}


def _marker_matches(lines: list[str]) -> list[dict]:
    found: dict[str, dict] = {}
    for number, line in enumerate(lines, 1):
        hit = MARKER.search(line)
        if not hit:
            continue
        try:
            data = json.loads(hit.group(2))
        except ValueError:
            continue
        category = data.get("category") if isinstance(data, dict) else None
        if category not in CATEGORIES or category == "unclassified":
            continue
        rule = f"marker.{hit.group(1)}"
        if rule in found:
            found[rule]["count"] += 1
        else:
            found[rule] = _match(rule, category, number)
    return list(found.values())


def _log_matches(lines: list[str]) -> list[dict]:
    found: dict[str, dict] = {}
    for number, line in enumerate(lines, 1):
        for rule, category, pattern in _COMPILED_RULES:
            if pattern.search(line):
                if rule in found:
                    found[rule]["count"] += 1
                else:
                    found[rule] = _match(rule, category, number)
    return list(found.values())


def classify_job(job: dict, trace: str | None) -> dict:
    """실패한 job 하나를 분류해 JSONL 한 줄에 해당하는 dict를 돌려준다."""
    reason = job.get("failure_reason")
    name = job.get("name") or ""
    lines = normalize_lines(trace) if trace else []
    matches: list[dict] = []
    category = None

    reason_category = REASON_CATEGORIES.get(reason) if isinstance(reason, str) else None
    if reason_category:
        matches.append(_match(f"reason.{reason}", reason_category, None))
        category = reason_category

    markers = _marker_matches(lines)
    matches.extend(markers)
    if category is None and markers:
        category = min(markers, key=lambda m: m["line"])["category"]

    if isinstance(name, str) and name.startswith(SECURITY_JOB_PREFIX):
        matches.append(_match("name.harness_job", "security", None))
        if category is None:
            category = "security"

    logs = _log_matches(lines)
    matches.extend(logs)
    if category is None:
        for wanted in LOG_CATEGORY_ORDER:
            if any(m["category"] == wanted for m in logs):
                category = wanted
                break

    return {
        "job_id": job["id"],
        "name": job.get("name"),
        "stage": job.get("stage"),
        "failure_reason": reason,
        "category": category or "unclassified",
        "matches": matches,
    }


def _job_id(job: dict) -> int:
    value = job.get("id")
    if isinstance(value, bool):
        value = None
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= MAX_ID_DIGITS:
        value = int(value)
    if not isinstance(value, int) or not 0 <= value < 10 ** MAX_ID_DIGITS:
        raise ClassifyError(f"job id는 0 이상의 정수여야 한다: {job.get('id')!r}")
    return value


def load_jobs(text: str) -> list[dict]:
    """JSON 배열·단일 객체·JSONL·페이지별 배열 이어붙임(`][`)을 job 목록으로 읽는다."""
    decoder = json.JSONDecoder()
    values, pos, text = [], 0, text.lstrip("\ufeff")
    while True:
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            break
        try:
            value, pos = decoder.raw_decode(text, pos)
        except ValueError as exc:
            raise ClassifyError(f"job 메타데이터 JSON 오류: {exc}") from exc
        values.extend(value if isinstance(value, list) else [value])
    if not values:
        raise ClassifyError("job 메타데이터가 비어 있다")
    jobs = []
    for job in values:
        if not isinstance(job, dict):
            raise ClassifyError("job 항목은 객체여야 한다")
        job = {**job, "id": _job_id(job)}
        if not isinstance(job.get("status"), str):
            raise ClassifyError(f"job {job['id']}: status가 없다")
        for key in ("name", "stage", "failure_reason"):
            if job.get(key) is not None and not isinstance(job[key], str):
                raise ClassifyError(f"job {job['id']}: {key}는 문자열이어야 한다")
        jobs.append(job)
    return jobs


def read_trace(trace_dir: Path | None, job_id: int) -> str | None:
    """`<trace_dir>/<job_id>.log`를 읽는다. 없으면 None(메타데이터만으로 분류)."""
    if trace_dir is None:
        return None
    path = trace_dir / f"{job_id}.log"
    if not path.is_file():
        return None
    return path.read_bytes().decode("utf-8", errors="replace")


def classify_jobs(jobs: list[dict], trace_dir: Path | None = None) -> tuple[list[dict], int]:
    """실패한 job만 분류한다. (산출 목록, 제외한 job 수)."""
    records, skipped = [], 0
    for job in jobs:
        if job["status"] != FAILED_STATUS:
            skipped += 1
            continue
        records.append(classify_job(job, read_trace(trace_dir, job["id"])))
    return records, skipped


def to_jsonl(records: list[dict]) -> str:
    return "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
