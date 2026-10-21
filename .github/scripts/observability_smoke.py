#!/usr/bin/env python3
"""팀 대시보드 조각(M5-8) 스모크 검사. `docker compose up` 뒤 CI에서 실행한다.

1. 임시 저장소에 키트 세션·hooks 기록과 그 저장소의 Codex 세션을 만들고 `harness telemetry push`로 수집기에 보낸다
2. Claude Code 형식의 지표(delta)·이벤트를 OTLP/HTTP로 보낸다(실제 Claude Code 대신 문서의 이름·속성으로 만든 것)
   둘 다 인증 프록시가 넣는 `X-Harness-Member` 헤더를 붙여 수집기가 `member` 리소스 속성으로 옮기는지 본다(#127)
3. Grafana 데이터 소스 프록시로 대시보드의 모든 쿼리를 실행해 오류가 없는지, 핵심 값이 보낸 만큼 나오는지 본다.
   데이터소스 변수(`${DS_PROMETHEUS}`·`${DS_LOKI}`)는 프로비저닝 uid와 기존 Grafana의 흔한 uid(`prometheus`·`loki`, 같은
   원천으로 따로 만든다) 두 벌로, 사람 키 변수(`$person`)는 `user_email`·`member` 두 값으로 모두 실행한다(#127)

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
MEMBER = "smoke-member"  # 인증 프록시가 토큰으로 정해 넣는 값 대신(스모크는 프록시 없이 헤더를 직접 붙인다)
MEMBER_HEADER = {"X-Harness-Member": MEMBER}
# 대시보드 변수 값 조합: (데이터소스 uid, 사람 키). 두 번째 uid 벌은 기존 Grafana에 얹는 경우(#127)
PROVISIONED = {"DS_PROMETHEUS": "harness-prometheus", "DS_LOKI": "harness-loki"}
EXISTING = {"DS_PROMETHEUS": "prometheus", "DS_LOKI": "loki"}


def request(url: str, data: dict | None = None, auth: bool = False, extra: dict | None = None):
    headers = {"Content-Type": "application/json", **(extra or {})}
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


def codex_session(home: Path, repo: Path) -> int:
    """이 저장소에서 연 Codex 세션 하나(응답 2개). Codex 로그(cli 0.162)와 같은 구조. 만든 사건 수(시작 1 + 응답 2)"""
    lines = [{"timestamp": iso(400), "type": "session_meta",
              "payload": {"id": "smoke-codex", "cwd": str(repo), "git": {"branch": "feat/codex"}}}]
    for turn, ago in enumerate((350, 250)):
        lines += [{"timestamp": iso(ago + 10), "type": "turn_context", "payload": {"model": "gpt-6-sol"}},
                  {"timestamp": iso(ago + 10), "type": "response_item",  # 대화 내용: 보내지 않아야 한다
                   "payload": {"type": "message", "role": "user", "content": [{"text": "smoke-codex-prompt"}]}},
                  {"timestamp": iso(ago), "type": "token_usage_record",
                   "payload": {"usage": {"input_tokens": 1000, "cached_input_tokens": 0, "output_tokens": 50 * (turn + 1)}}},
                  {"timestamp": iso(ago), "type": "event_msg", "payload": {"type": "task_complete"}}]
    path = home / "sessions" / "2026" / "10" / "20" / "rollout-smoke-codex.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return 3


def kit_records() -> tuple[Path, int]:
    """임시 저장소의 `.git/harness/`에 세션 2개·hooks 개입 3개 기록. (저장소, hooks 개입 수)"""
    repo = Path(tempfile.mkdtemp(prefix="obs-smoke-")).resolve()
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
    # 누적값 합산·브랜치별 중복·기간 경계 회귀를 잡는다. gpt-6-sol 기본 단가: input $2/M, output $10/M.
    for session, snapshots in (("smoke-boundary", ((4200, 100, 10), (180, 300, 30), (170, 300, 30))),
                               ("smoke-new", ((160, 100, 10), (150, 200, 20)))):
        for index, (ago, inputs, outputs) in enumerate(snapshots):
            sessions.append({"ts": iso(ago), "event": "usage", "session": session, "tool": "codex",
                             "branch": f"feat/snapshot-{index}", "usage": {"gpt-6-sol": {
                                 "input": inputs, "output": outputs, "cache_read": 0,
                                 "cache_write_5m": 0, "cache_write_1h": 0}}})
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
    request(f"{COLLECTOR}/v1/metrics", metrics, extra=MEMBER_HEADER)

    def event(name, **attrs):
        return {"timeUnixNano": str(now), "body": {"stringValue": f"claude_code.{name}"},
                "attributes": [kv("event.name", name), kv("session.id", "smoke-1"), kv("user.email", "smoke@example.com")]
                + [kv(k, v) for k, v in attrs.items()]}
    logs = {"resourceLogs": [{"resource": resource, "scopeLogs": [{"scope": {"name": "com.anthropic.claude_code"},
        "logRecords": [event("tool_decision", tool_name="Edit", decision="accept", source="config"),
                       event("tool_decision", tool_name="Bash", decision="reject", source="hook"),
                       event("hook_execution_complete", hook_event="PreToolUse", hook_name="PreToolUse:Edit",
                             num_hooks=1, num_success=0, num_blocking=1)]}]}]}
    request(f"{COLLECTOR}/v1/logs", logs, extra=MEMBER_HEADER)


def existing_datasources() -> None:
    """기존 Grafana의 흔한 uid(`prometheus`·`loki`)로 같은 원천을 하나 더 만든다. 대시보드를 그런 Grafana에 얹은 경우"""
    for kind, url in (("prometheus", "http://prometheus:9090"), ("loki", "http://loki:3100")):
        try:
            request(f"{GRAFANA}/api/datasources", {"name": f"existing-{kind}", "uid": kind, "type": kind, "url": url,
                                                   "access": "proxy"}, auth=True)
        except urllib.error.HTTPError as exc:
            if exc.code != 409:  # 이미 있음(다시 실행)
                raise


def query(target: dict, variables: dict | None = None):
    """대시보드 쿼리 하나를 Grafana 데이터 소스 프록시로 실행. `$__range`·`$__auto`는 1h로 바꾸고, 대시보드 변수
    (`${DS_PROMETHEUS}`·`${DS_LOKI}`·`$person`)는 variables로 바꾼다(기본: 프로비저닝 uid, user_email)."""
    variables = {**PROVISIONED, "person": "user_email", **(variables or {})}
    expr = target["expr"].replace("$__range", "1h").replace("$__auto", "1m").replace("$__rate_interval", "1m")
    expr = expr.replace("${person}", variables["person"]).replace("$person", variables["person"])
    uid = target["datasource"]["uid"]
    for name, value in variables.items():
        uid = uid.replace(f"${{{name}}}", value)
    if "$" in uid or "$" in expr.replace("$__", ""):
        raise ValueError(f"바꾸지 못한 변수: {uid} {expr}")
    if target["datasource"].get("type") == "prometheus" or uid in (PROVISIONED["DS_PROMETHEUS"], EXISTING["DS_PROMETHEUS"]):
        path = "/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    else:
        is_log_query = not any(word in expr for word in ("count_over_time", "sum_over_time", "max_over_time", "rate("))
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


def combined_cost(panel, uids, empty_codex=False, empty_claude=False):
    """실제 Grafana 표현식까지 실행한다. 데이터소스 쿼리만 검사하면 Mixed 합계 실패를 놓친다."""
    targets = json.loads(json.dumps(panel["targets"]))
    for target in targets:
        target.update(intervalMs=30000, maxDataPoints=100)
        for key in ("expr", "expression"):
            if key in target:
                target[key] = target[key].replace("$__range", "1h").replace("$person", "member")
        for name, uid in uids.items():
            target["datasource"]["uid"] = target["datasource"]["uid"].replace(f"${{{name}}}", uid)
        if empty_codex and target["refId"] == "B":
            target["expr"] = target["expr"].replace('{service_name="harness"}',
                                                   '{service_name="harness"} | harness_repo="smoke-missing"')
        if empty_claude and target["refId"] == "A":
            target["expr"] = target["expr"].replace('claude_code_cost_usage.*', 'smoke_missing_cost.*')
    now = int(time.time() * 1000)
    response = request(f"{GRAFANA}/api/ds/query", {"from": str(now - 3600000), "to": str(now), "queries": targets}, auth=True)
    result = response["results"]["C"]
    if result.get("error"):
        raise ValueError(f"합계 표현식 실패: {result['error']}")
    values = [value for frame in result.get("frames", [])
              for field, column in zip(frame["schema"]["fields"], frame["data"]["values"])
              if field["type"] == "number" for value in column if value is not None]
    if len(values) != 1:
        raise ValueError(f"합계가 단일 값이 아니다: {values}")
    return values[0]


def main() -> int:
    wait(lambda: request(f"{GRAFANA}/api/health").get("database") == "ok", "Grafana 기동")
    dashboard = wait(lambda: request(f"{GRAFANA}/api/dashboards/uid/harness-agentops", auth=True), "대시보드 프로비저닝")
    existing_datasources()
    repo, hook_count = kit_records()
    codex_home = Path(tempfile.mkdtemp(prefix="obs-smoke-codex-"))
    codex_count = codex_session(codex_home, repo)
    env = {**os.environ, "CODEX_HOME": str(codex_home), "OTEL_EXPORTER_OTLP_HEADERS": f"X-Harness-Member={MEMBER}"}

    def pushed():
        out = subprocess.run([sys.executable, str(ROOT / "bin" / "harness.py"), "telemetry", "push", str(repo),
                              "--endpoint", COLLECTOR], capture_output=True, text=True, env=env)
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
    wait(lambda: scalar(query({"expr": 'sum(count_over_time({service_name="harness"} | harness_tool="codex" [1h]))',
                               "datasource": {"type": "loki", "uid": "harness-loki"}})) == codex_count + 4,
         "이 저장소의 Codex 세션이 Loki에")
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
    for secret in ("secret-cmd", "/home/someone", "transcript", "smoke-codex-prompt"):
        if secret in text:
            sys.exit(f"보내지 않아야 할 값이 Loki에 있다: {secret}")

    # 사람 키 member: 수집기가 헤더를 리소스 속성으로 옮겨 지표 레이블·Loki 구조화 메타데이터에 있다(#127)
    for expr, uid, what in (('sum by (member) ({__name__=~"claude_code_cost_usage.*"})', "harness-prometheus", "지표"),
                            ('sum by (member) (count_over_time({service_name="harness"} [6h]))', "harness-loki", "키트 기록"),
                            ('sum by (member) (count_over_time({service_name=~"claude-code.*"} [1h]))', "harness-loki",
                             "Claude Code 이벤트")):
        members = {series["metric"].get("member") for series in query({"expr": expr, "datasource": {"uid": uid}})}
        if members != {MEMBER}:
            sys.exit(f"{what}의 member가 {MEMBER}가 아니다: {members}")

    defined = {item["name"]: item for item in dashboard["dashboard"]["templating"]["list"]}
    if {name: defined[name]["current"]["value"] for name in PROVISIONED} != PROVISIONED:
        sys.exit(f"데이터소스 변수 기본값이 프로비저닝 uid가 아니다: {defined}")
    failures, runs = [], 0
    for uids in (PROVISIONED, EXISTING):
        for person in ("user_email", "member"):
            combo = f"{uids['DS_PROMETHEUS']}·{uids['DS_LOKI']}·{person}"
            # 패널 값: 보낸 만큼 나와야 한다(오류 없음만 보면 increase()가 첫 샘플을 빼 0이 되는 것을 놓친다, 리뷰 F1·F4)
            expected = {("Claude Code 세션", "A"): 1, ("비용(USD, Claude Code + Codex)", "A"): 1.25,
                        ("비용(USD, Claude Code + Codex)", "B"): 0.0067,
                        ("Codex 추정 비용(USD)", "A"): 0.0067,
                        ("Codex 토큰(종류별)", "A"): 2400, ("Codex 토큰(종류별)", "B"): 190,
                        ("Codex 토큰(종류별)", "C"): 0, ("Codex 토큰(종류별)", "D"): 0,
                        ("Codex 토큰(종류별)", "E"): 0, ("도구 거부 비율", "A"): 0.5,
                        ("hook 차단(Claude Code)", "A"): 1, ("키트 hooks 개입", "A"): hook_count,
                        ("hooks 기록 없는 세션(근사)", "A"): 1, ("hooks 기록 없는 세션(근사)", "B"): 2}
            labels = {"user_email": "smoke@example.com", "member": MEMBER}[person]
            for panel in dashboard["dashboard"]["panels"]:
                for target in panel["targets"]:
                    if "expr" not in target:
                        continue
                    runs += 1
                    try:
                        result = query(target, {**uids, "person": person})
                    except (urllib.error.URLError, ValueError) as exc:
                        failures.append(f"{combo} {panel['title']}/{target['refId']}: {exc}")
                        continue
                    want = expected.pop((panel["title"], target["refId"]), None)
                    if want is not None and abs(scalar(result) - want) > 1e-9:
                        failures.append(f"{combo} {panel['title']}/{target['refId']}: {scalar(result)} != {want}")
                    if "$person" in target["expr"] and panel["datasource"]["type"] == "prometheus" and \
                            {series["metric"].get(person) for series in result} != {labels}:
                        failures.append(f"{combo} {panel['title']}/{target['refId']}: 사람 키 {person} 값이 "
                                        f"{sorted(str(series['metric'].get(person)) for series in result)}")
            failures += [f"{combo} 패널을 찾지 못했다: {title}/{ref}" for title, ref in expected]
        total = next(p for p in dashboard["dashboard"]["panels"] if p["id"] == 2)
        for empty_codex, empty_claude, want in ((False, False, 1.2567), (True, False, 1.25),
                                               (False, True, 0.0067), (True, True, 0)):
            try:
                actual = combined_cost(total, uids, empty_codex, empty_claude)
                if abs(actual - want) > 1e-9:
                    failures.append(f"합계(empty_codex={empty_codex}, empty_claude={empty_claude}): {actual} != {want}")
            except (urllib.error.URLError, ValueError, KeyError) as exc:
                failures.append(f"합계(empty_codex={empty_codex}, empty_claude={empty_claude}): {exc}")
    for failure in failures:
        print(f"실패: {failure}")
    print(f"대시보드 쿼리 {runs}개 실행(데이터소스 uid 2벌 × 사람 키 2개), 실패 {len(failures)}개")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
