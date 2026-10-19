"""키트 세션·hooks 기록을 OTLP/HTTP(JSON) 로그로 보낸다(M5-8, #103). 표준 라이브러리만 쓰고 키트에서 실행한다.

    python bin/harness.py telemetry push [<target>|--self] [--endpoint http://collector:4318] [--dry-run] [--from-start]

- 같은 저장소의 모든 worktree의 `sessions.jsonl`·`events.jsonl`에서 지난번 이후 새 줄만 보낸다. 어디까지 보냈는지는
  공용 git 디렉터리의 `harness/telemetry.json`에 남긴다(보내기에 성공한 묶음까지만 전진).
- 정해진 필드만 속성으로 보낸다(세션·브랜치·도구·모델·결정·규칙·경로·토큰 수·추정 비용). transcript 경로, 종료 검증 명령,
  프롬프트·코드 내용은 보내지 않는다.
- 엔드포인트와 헤더는 OpenTelemetry 표준 변수(`OTEL_EXPORTER_OTLP_LOGS_ENDPOINT`·`OTEL_EXPORTER_OTLP_ENDPOINT`,
  `OTEL_EXPORTER_OTLP_LOGS_HEADERS`·`OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_RESOURCE_ATTRIBUTES`)를 읽는다. 헤더 값은 출력하지 않는다.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SERVICE = "harness"
STATE_FILE = "telemetry.json"
LOG_FILES = ("sessions.jsonl", "events.jsonl")
BATCH = 500
TIMEOUT_SEC = 10
SEVERITY = {"INFO": 9, "WARN": 13}

# 보내는 필드(기록 키 → 속성 이름). 이 밖의 키는 보내지 않는다.
SESSION_FIELDS = {"event": "harness.event", "session": "session.id", "branch": "harness.branch", "tool": "harness.tool",
                  "model": "model", "source": "harness.source", "verify": "harness.verify", "files": "harness.files",
                  "retry": "harness.retry", "usage_error": "harness.usage_error"}
EVENT_FIELDS = {"hook": "harness.hook", "decision": "harness.decision", "rule": "harness.rule", "path": "harness.path",
                "tool": "tool_name", "session": "session.id", "branch": "harness.branch", "retry": "harness.retry"}


class PushError(Exception):
    """설정 오류(종료 2)."""


def endpoint_from(arg: str | None, env) -> str:
    """로그 엔드포인트 URL. 인자(기본 URL)와 `OTEL_EXPORTER_OTLP_ENDPOINT`에는 `/v1/logs`를 붙이고, 신호별 변수는 그대로 쓴다."""
    if arg:
        url = arg.rstrip("/") + "/v1/logs"
    elif env.get("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"):
        url = env["OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"]
    elif env.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        url = env["OTEL_EXPORTER_OTLP_ENDPOINT"].rstrip("/") + "/v1/logs"
    else:
        raise PushError("엔드포인트가 없다. --endpoint 또는 OTEL_EXPORTER_OTLP_ENDPOINT(OTLP/HTTP, 예: http://127.0.0.1:4318)")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise PushError(f"엔드포인트는 http(s) URL이어야 한다: {parsed.scheme}://{parsed.hostname or ''}")
    return url


def parse_pairs(text: str | None, what: str) -> dict[str, str]:
    """`k=v,k2=v2`(값은 퍼센트 인코딩 가능). OpenTelemetry 환경 변수 형식."""
    pairs = {}
    for item in (text or "").split(","):
        if not item.strip():
            continue
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise PushError(f"{what} 형식이 잘못됐다(k=v,k2=v2). 값은 출력하지 않는다")
        pairs[urllib.parse.unquote(key.strip())] = urllib.parse.unquote(value.strip())
    return pairs


def headers_from(env) -> dict[str, str]:
    text = env.get("OTEL_EXPORTER_OTLP_LOGS_HEADERS") or env.get("OTEL_EXPORTER_OTLP_HEADERS")
    return parse_pairs(text, "OTEL_EXPORTER_OTLP_HEADERS")


def any_value(value) -> dict | None:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str) and value:
        return {"stringValue": value[:512]}
    return None


def attributes(pairs: dict) -> list[dict]:
    return [{"key": key, "value": encoded} for key, value in pairs.items() if (encoded := any_value(value)) is not None]


def to_log(worktree: str, name: str, record: dict, collect, pricing: dict) -> dict | None:
    """기록 한 줄 → OTLP LogRecord. 시각을 읽을 수 없으면 None."""
    try:
        moment = collect.parse_ts(record.get("ts"))
    except (ValueError, OverflowError):
        return None
    if moment is None:
        return None
    session_log = name == "sessions.jsonl"
    fields = SESSION_FIELDS if session_log else EVENT_FIELDS
    pairs = {"harness.kind": "session" if session_log else "hook", "harness.worktree": worktree}
    pairs.update({attr: record.get(key) for key, attr in fields.items()})
    if isinstance(pairs.get("harness.files"), bool):
        pairs["harness.files"] = None
    usage = collect.clean_usage(record.get("usage")) if session_log else None
    if usage:
        for field in collect.USAGE_FIELDS:
            pairs[f"harness.tokens.{field}"] = sum(counts[field] for counts in usage.values())
        cost, _unpriced = collect.estimate_cost(usage, pricing)
        pairs["harness.cost_estimate"] = round(cost, 6)
        pairs["harness.currency"] = pricing["currency"]
    label = record.get("event") if session_log else record.get("decision")
    label = label if isinstance(label, str) and label else "unknown"
    warn = not session_log and label in ("block", "fail")
    nanos = str(int(moment.timestamp()) * 1_000_000_000 + moment.microsecond * 1000)
    return {"timeUnixNano": nanos, "observedTimeUnixNano": str(time.time_ns()),
            "severityNumber": SEVERITY["WARN" if warn else "INFO"], "severityText": "WARN" if warn else "INFO",
            "body": {"stringValue": f"harness.{pairs['harness.kind']}.{label}"}, "attributes": attributes(pairs)}


def new_lines(path: Path, offset: int) -> tuple[list[tuple[int, str]], int]:
    """offset 이후의 완성된 줄과 각 줄 끝 위치. 파일이 offset보다 작아졌으면(바뀐 파일) 처음부터 읽는다."""
    try:
        size = path.stat().st_size
    except OSError:
        return [], offset
    if size < offset:
        offset = 0
    with open(path, "rb") as handle:
        handle.seek(offset)
        data = handle.read()
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset
    lines, position = [], offset
    for raw in data[:end + 1].split(b"\n")[:-1]:
        position += len(raw) + 1
        lines.append((position, raw.decode("utf-8", errors="replace")))
    return lines, offset


def resource(env, repo: str, version: str) -> dict:
    pairs = parse_pairs(env.get("OTEL_RESOURCE_ATTRIBUTES"), "OTEL_RESOURCE_ATTRIBUTES")
    pairs.update({"service.name": SERVICE, "service.version": version, "harness.repo": repo})
    return {"attributes": attributes(pairs)}


def payload(res: dict, logs: list[dict], version: str) -> dict:
    """세션마다 리소스(`session.id`)를 나누고 시각 순으로 담는다. 수집기의 Loki 설정이 `session.id`를 색인 레이블로 두어
    세션마다 스트림이 갈리므로, 팀원 여러 명의 기록이나 지난 기록을 늦게 보내도 한 스트림 안에서 순서가 크게 어긋나지
    않는다(Loki는 스트림의 가장 새 줄보다 1시간 넘게 오래된 줄을 버린다)."""
    groups: dict[str | None, list[dict]] = {}
    for log in sorted(logs, key=lambda item: int(item["timeUnixNano"])):
        session = next((item["value"]["stringValue"] for item in log["attributes"] if item["key"] == "session.id"), None)
        groups.setdefault(session, []).append(log)
    resource_logs = []
    for session, records in groups.items():
        attrs = res["attributes"] + ([{"key": "session.id", "value": {"stringValue": session}}] if session else [])
        resource_logs.append({"resource": {"attributes": attrs}, "scopeLogs": [
            {"scope": {"name": SERVICE, "version": version}, "logRecords": records}]})
    return {"resourceLogs": resource_logs}


def send(url: str, headers: dict, body: dict) -> tuple[int, str]:
    """보낸다. (수집기가 거절한 줄 수, 거절 사유). OTLP 부분 성공은 200과 함께 `partialSuccess.rejectedLogRecords`로
    알린다(#109 Codex 리뷰). 거절된 줄은 다시 보내도 받지 않으므로 다시 보내지 않고 알리기만 한다."""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST",
                                     headers={**headers, "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:  # noqa: S310 - http(s)만 허용(endpoint_from)
        if not 200 <= response.status < 300:
            raise urllib.error.HTTPError(url, response.status, "unexpected status", response.headers, None)
        raw = response.read(1_000_000)
    try:
        partial = json.loads(raw or b"{}").get("partialSuccess") or {}
        rejected = int(partial.get("rejectedLogRecords") or 0)
        message = str(partial.get("errorMessage") or "")[:200]
    except (ValueError, TypeError, AttributeError):
        return 0, ""
    return max(rejected, 0), message


def push(repo: str, dirs: list[tuple[str, Path]], state_path: Path, collect, pricing: dict, version: str,
         lock, endpoint: str | None = None, env=None, dry_run: bool = False, from_start: bool = False,
         out=print) -> int:
    """새 기록을 보낸다. 종료 코드: 0(보낼 것이 없어도), 1(전송 실패·다른 push 실행 중, 보낸 묶음까지만 기록),
    2(설정 오류), 3(수집기가 일부를 거절, 거절된 줄은 다시 보내지 않음)."""
    env = os.environ if env is None else env
    try:
        url = None if dry_run and not (endpoint or env.get("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT")
                                       or env.get("OTEL_EXPORTER_OTLP_ENDPOINT")) else endpoint_from(endpoint, env)
        headers = headers_from(env)
        res = resource(env, repo, version)
    except PushError as exc:
        out(f"harness telemetry: {exc}")
        return 2
    if url and headers and urllib.parse.urlsplit(url).scheme == "http" and \
            urllib.parse.urlsplit(url).hostname not in ("127.0.0.1", "localhost", "::1"):
        out("harness telemetry: 경고: 인증 헤더를 암호화하지 않은 http로 보낸다. 수집기 앞에 TLS를 둔다")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    held = lock(state_path)
    try:
        held.__enter__()
    except OSError:  # 잠금 시간 초과: 다른 push가 같은 저장소를 보내는 중(#103 리뷰 F6)
        out("harness telemetry: 다른 telemetry push가 실행 중이라 잠그지 못했다. 끝난 뒤 다시 실행한다")
        return 1
    try:
        return _push_locked(dirs, state_path, collect, pricing, version, url, headers, res, dry_run, from_start, out)
    finally:
        held.__exit__(None, None, None)


def _push_locked(dirs, state_path, collect, pricing, version, url, headers, res, dry_run, from_start, out) -> int:
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    except (OSError, ValueError):
        state = {}
    offsets = {} if from_start or not isinstance(state.get("offsets"), dict) else state["offsets"]
    # 파일마다 줄 순서대로 [줄 끝 위치, 보냈는가]. 위치는 앞에서부터 끊김 없이 보낸 줄까지만 전진한다.
    # 깨진 줄은 처음부터 보낸 것으로 두므로, 그 앞의 줄이 실패하면 깨진 줄도 함께 다시 읽는다(#103 리뷰 F3).
    entries: dict[str, list[list]] = {}
    pending: list[tuple[str, list, dict]] = []  # (파일 키, 항목, LogRecord)
    for worktree, directory in dirs:
        for name in LOG_FILES:
            path = directory / name
            key = str(path)
            start = offsets.get(key, 0) if isinstance(offsets.get(key), int) else 0
            lines, start = new_lines(path, start)
            offsets[key] = start
            entries[key] = []
            for end, line in lines:
                try:
                    record = json.loads(line)
                except ValueError:
                    record = None
                log = to_log(worktree, name, record, collect, pricing) if isinstance(record, dict) else None
                entry = [end, log is None]
                entries[key].append(entry)
                if log is not None:
                    pending.append((key, entry, log))

    def advance() -> None:
        for key, items in entries.items():
            for end, done in items:
                if not done:
                    break
                offsets[key] = end

    kinds: dict[str, int] = {}
    for _, _, log in pending:
        kinds[log["body"]["stringValue"]] = kinds.get(log["body"]["stringValue"], 0) + 1
    summary = ", ".join(f"{kind} {count}" for kind, count in sorted(kinds.items())) or "없음"
    if dry_run:
        out(f"harness telemetry: 보낼 기록 {len(pending)}개({summary}). --dry-run이라 보내지 않는다")
        if pending:
            out(json.dumps(payload(res, [log for _, _, log in pending[:BATCH]], version), ensure_ascii=False,
                           indent=1))
        return 0
    pending.sort(key=lambda item: int(item[2]["timeUnixNano"]))
    sent = rejected = 0
    reasons: list[str] = []
    for index in range(0, len(pending), BATCH):
        batch = pending[index:index + BATCH]
        try:
            batch_rejected, reason = send(url, headers, payload(res, [log for _, _, log in batch], version))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            reason = exc.code if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__
            out(f"harness telemetry: 전송 실패({reason}). {sent}개까지 보냈고 나머지는 다음에 다시 보낸다")
            advance()
            save(state_path, offsets)
            return 1
        for _, entry, _ in batch:
            entry[1] = True
        sent += len(batch)
        rejected += batch_rejected
        if batch_rejected and reason and reason not in reasons:
            reasons.append(reason)
        advance()
        save(state_path, offsets)
    advance()
    save(state_path, offsets)
    if rejected:
        detail = f": {'; '.join(reasons)}" if reasons else ""
        out(f"harness telemetry: {sent}개 중 {rejected}개를 수집기가 거절했다{detail}. 거절된 줄은 다시 보내지 않는다")
        return 3
    out(f"harness telemetry: {sent}개 보냄({summary})")
    return 0


def save(state_path: Path, offsets: dict) -> None:
    temp = state_path.with_suffix(".tmp")
    temp.write_text(json.dumps({"version": 1, "offsets": offsets}, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, state_path)
