#!/usr/bin/env python3
"""팀 대시보드 조각(M5-8) 스모크 검사. `docker compose up` 뒤 CI에서 실행한다.

1. 임시 저장소에 키트 세션·hooks 기록을 만들고 `harness telemetry push`로 수집기에 보낸다
2. Claude Code 형식의 지표(delta)·이벤트를 OTLP/HTTP로 보낸다(실제 Claude Code 대신 문서의 이름·속성으로 만든 것)
3. Grafana 데이터 소스 프록시로 대시보드의 모든 쿼리를 실행해 오류가 없는지, 핵심 값이 보낸 만큼 나오는지 본다

    GRAFANA_ADMIN_PASSWORD=... python3 .github/scripts/observability_smoke.py
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = ROOT / "core" / "observability" / "grafana" / "dashboards" / "harness-agentops.json"
GRAFANA = "http://127.0.0.1:3000"
COLLECTOR = "http://127.0.0.1:4318"
DEADLINE_SEC = 120  # 기동 대기 상한
VISIBLE_SEC = 60  # 인수 조건: 보낸 신호가 1분 안에 대시보드 원천에 보인다


def request(url: str, data: dict | None = None, auth: bool = False):
    headers = {"Content-Type": "application/json"}
    if auth:
        token = base64.b64encode(f"admin:{os.environ['GRAFANA_ADMIN_PASSWORD']}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=10) as response:  # noqa: S310 - 로컬 고정 주소
        return json.loads(response.read() or b"null")


def wait(check, what: str, limit: float = DEADLINE_SEC):
    deadline, last = time.monotonic() + limit, None
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            last = exc
        time.sleep(3)
    sys.exit(f"시간 안에 확인하지 못했다: {what} ({last})")


def kv(key, value):
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": value}}


def iso(seconds_ago: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))


def kit_records() -> tuple[Path, int]:
    """임시 저장소의 `.git/harness/`에 세션 2개·hooks 개입 3개 기록. (저장소, hooks 개입 수)"""
    repo = Path(tempfile.mkdtemp(prefix="obs-smoke-"))
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    state = repo / ".git" / "harness"
    state.mkdir(parents=True)
    sessions = [
        {"ts": iso(300), "event": "start", "session": "smoke-1", "tool": "claude-code", "branch": "feat/a",
         "transcript": "/home/someone/.claude/x.jsonl"},
        {"ts": iso(200), "event": "stop", "session": "smoke-1", "tool": "claude-code", "branch": "feat/a",
         "verify": "pass", "usage": {"claude-opus-5-5": {"input": 10, "output": 1000, "cache_write_5m": 0,
                                                         "cache_write_1h": 0, "cache_read": 0}}},
        {"ts": iso(100), "event": "start", "session": "smoke-2", "tool": "claude-code", "branch": "feat/b"},
    ]
    events = [
        {"ts": iso(150), "hook": "protect-paths", "decision": "block", "rule": "package-lock.json",
         "path": "package-lock.json", "session": "smoke-1", "branch": "feat/a"},
        {"ts": iso(140), "hook": "protect-paths", "decision": "ask", "rule": "/harness.json", "path": "harness.json",
         "session": "smoke-1", "branch": "feat/a"},
        {"ts": iso(130), "hook": "stop-verify", "decision": "fail", "commands": ["secret-cmd"], "session": "smoke-1",
         "branch": "feat/a"},
    ]
    (state / "sessions.jsonl").write_text("".join(json.dumps(r) + "\n" for r in sessions), encoding="utf-8")
    (state / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in events), encoding="utf-8")
    return repo, len(events)


def claude_code_telemetry() -> None:
    now = time.time_ns()
    resource = {"attributes": [kv("service.name", "claude-code"), kv("service.version", "smoke")]}
    point = [kv("user.email", "smoke@example.com"), kv("session.id", "smoke-1"), kv("model", "claude-opus-5-5")]
    metrics = {"resourceMetrics": [{"resource": resource, "scopeMetrics": [{"scope": {"name": "com.anthropic.claude_code"},
        "metrics": [
            {"name": "claude_code.cost.usage", "unit": "USD", "sum": {"aggregationTemporality": 1, "isMonotonic": True,
             "dataPoints": [{"asDouble": 1.25, "startTimeUnixNano": str(now - 10**9), "timeUnixNano": str(now),
                             "attributes": point}]}},
            {"name": "claude_code.session.count", "unit": "1", "sum": {"aggregationTemporality": 1, "isMonotonic": True,
             "dataPoints": [{"asInt": "1", "startTimeUnixNano": str(now - 10**9), "timeUnixNano": str(now),
                             "attributes": point}]}},
            {"name": "claude_code.token.usage", "unit": "tokens", "sum": {"aggregationTemporality": 1,
             "isMonotonic": True, "dataPoints": [{"asInt": "1000", "startTimeUnixNano": str(now - 10**9),
                                                  "timeUnixNano": str(now), "attributes": point + [kv("type", "output")]}]}},
        ]}]}]}
    request(f"{COLLECTOR}/v1/metrics", metrics)

    def event(name, **attrs):
        return {"timeUnixNano": str(now), "body": {"stringValue": f"claude_code.{name}"},
                "attributes": [kv("event.name", name), kv("session.id", "smoke-1"), kv("user.email", "smoke@example.com")]
                + [kv(k, v) for k, v in attrs.items()]}
    logs = {"resourceLogs": [{"resource": resource, "scopeLogs": [{"scope": {"name": "com.anthropic.claude_code"},
        "logRecords": [event("tool_decision", tool_name="Edit", decision="accept", source="config"),
                       event("tool_decision", tool_name="Bash", decision="reject", source="hook"),
                       event("hook_execution_complete", hook_event="PreToolUse", hook_name="PreToolUse:Edit",
                             num_hooks=1, num_success=0, num_blocking=1)]}]}]}
    request(f"{COLLECTOR}/v1/logs", logs)


def query(target: dict, instant: bool = True):
    """대시보드 쿼리 하나를 Grafana 데이터 소스 프록시로 실행. `$__range`·`$__auto`는 1h로 바꾼다."""
    expr = target["expr"].replace("$__range", "1h").replace("$__auto", "1m").replace("$__rate_interval", "1m")
    uid = target["datasource"]["uid"]
    if uid == "harness-prometheus":
        path = "/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    else:
        is_log_query = not any(word in expr for word in ("count_over_time", "sum_over_time", "rate("))
        if is_log_query:
            path = "/loki/api/v1/query_range?" + urllib.parse.urlencode(
                {"query": expr, "limit": 100, "start": str(time.time_ns() - 3600 * 10**9)})
        else:
            path = "/loki/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    result = request(f"{GRAFANA}/api/datasources/proxy/uid/{uid}{path}", auth=True)
    if result.get("status") != "success":
        raise ValueError(f"쿼리 실패: {expr}: {result}")
    return result["data"]["result"]


def scalar(result) -> float:
    return sum(float(series["value"][1]) for series in result) if result else 0.0


def main() -> int:
    wait(lambda: request(f"{GRAFANA}/api/health").get("database") == "ok", "Grafana 기동")
    dashboard = wait(lambda: request(f"{GRAFANA}/api/dashboards/uid/harness-agentops", auth=True), "대시보드 프로비저닝")
    repo, hook_count = kit_records()

    def pushed():
        out = subprocess.run([sys.executable, str(ROOT / "bin" / "harness.py"), "telemetry", "push", str(repo),
                              "--endpoint", COLLECTOR], capture_output=True, text=True)
        print(out.stdout.strip(), out.stderr.strip())
        return out.returncode == 0
    wait(pushed, "telemetry push(수집기 기동)")
    claude_code_telemetry()
    started = time.monotonic()

    wait(lambda: scalar(query({"expr": 'sum(count_over_time({service_name="harness"} | harness_kind="hook" [1h]))',
                               "datasource": {"uid": "harness-loki"}})) == hook_count, "키트 hooks 개입이 Loki에")
    wait(lambda: scalar(query({"expr": 'sum({__name__=~"claude_code_cost_usage.*"})',
                               "datasource": {"uid": "harness-prometheus"}})) == 1.25, "Claude Code 비용 지표가 Prometheus에")
    wait(lambda: scalar(query({"expr": 'sum(count_over_time({service_name=~"claude-code.*"} | event_name="tool_decision"'
                                       ' | decision="reject" [1h]))', "datasource": {"uid": "harness-loki"}})) == 1,
         "Claude Code tool_decision 이벤트가 Loki에")
    elapsed = time.monotonic() - started
    print(f"보낸 뒤 대시보드 원천에 보이기까지 {elapsed:.1f}초")
    if elapsed > VISIBLE_SEC:
        sys.exit(f"{VISIBLE_SEC}초 안에 보이지 않았다: {elapsed:.1f}초")

    # 늦게 보낸 지난 기록(다른 세션, 100분 전)도 받는다. 한 스트림이면 Loki가 1시간 넘게 거꾸로 간 줄을 버린다(리뷰 F2).
    # 3시간보다 오래되면 Loki가 아직 저장소로 내보내지 않은 ingester 데이터를 조회하지 않아(query_ingesters_within) 보이지 않는다
    with open(repo / ".git" / "harness" / "events.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": iso(100 * 60), "hook": "protect-paths", "decision": "ask", "rule": "/harness.json",
                                 "path": "harness.json", "session": "smoke-old", "branch": "feat/old"}) + "\n")
    if not pushed():
        sys.exit("지난 기록 push 실패")
    wait(lambda: scalar(query({"expr": 'sum(count_over_time({service_name="harness"} | harness_kind="hook" [6h]))',
                               "datasource": {"uid": "harness-loki"}})) == hook_count + 1, "늦게 보낸 지난 기록이 Loki에",
         VISIBLE_SEC)

    logs = query({"expr": '{service_name="harness"}', "datasource": {"uid": "harness-loki"}})
    text = json.dumps(logs)
    for secret in ("secret-cmd", "/home/someone", "transcript"):
        if secret in text:
            sys.exit(f"보내지 않아야 할 값이 Loki에 있다: {secret}")

    # 패널 값: 보낸 만큼 나와야 한다(오류 없음만 보면 increase()가 첫 샘플을 빼 0이 되는 것을 놓친다, 리뷰 F1·F4)
    expected = {("Claude Code 세션", "A"): 1, ("비용(USD, Claude Code)", "A"): 1.25, ("도구 거부 비율", "A"): 0.5,
                ("hook 차단(Claude Code)", "A"): 1, ("키트 hooks 개입", "A"): hook_count,
                ("hooks 기록 없는 세션(근사)", "A"): 1, ("hooks 기록 없는 세션(근사)", "B"): 2}
    failures = []
    for panel in dashboard["dashboard"]["panels"]:
        for target in panel["targets"]:
            if "expr" not in target:
                continue
            try:
                result = query(target)
            except (urllib.error.URLError, ValueError) as exc:
                failures.append(f"{panel['title']}/{target['refId']}: {exc}")
                continue
            want = expected.pop((panel["title"], target["refId"]), None)
            if want is not None and abs(scalar(result) - want) > 1e-9:
                failures.append(f"{panel['title']}/{target['refId']}: {scalar(result)} != {want}")
    failures += [f"패널을 찾지 못했다: {title}/{ref}" for title, ref in expected]
    for failure in failures:
        print(f"실패: {failure}")
    print(f"대시보드 쿼리 {sum(1 for p in dashboard['dashboard']['panels'] for t in p['targets'] if 'expr' in t)}개 실행,"
          f" 실패 {len(failures)}개")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
