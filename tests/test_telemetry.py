"""#103 M5-8 팀 대시보드 선택 조각: `harness telemetry push`와 조각 설정. #127 저장소 안 실행·Codex 세션·대시보드 변수."""

import datetime as dt
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_codex_sessions import SECRET, rollout  # noqa: E402
from test_hooks import git  # noqa: E402
from test_sessions import SessionRecordCase, collect, harness, run_cli  # noqa: E402
from test_summary import init_target  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OBS = ROOT / "core" / "observability"
telemetry = harness.load_telemetry()
PRICING = collect.merged_pricing({})


def dashboard_panels(panels):
    """접힌 Grafana 행 안의 패널도 같은 검증 대상으로 포함한다."""
    for panel in panels:
        yield panel
        yield from dashboard_panels(panel.get("panels", []))


class Collector:
    """OTLP/HTTP 로그를 받는 가짜 수집기. fail이 참이면 500."""

    def __init__(self):
        self.bodies, self.headers, self.fail, self.reply = [], [], False, b"{}"
        self.attempts = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):  # noqa: N802
                owner.attempts += 1
                data = self.rfile.read(int(self.headers["Content-Length"]))
                if owner.fail:
                    self.send_response(500)
                    self.end_headers()
                    return
                owner.bodies.append(json.loads(data))
                owner.headers.append(dict(self.headers))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(owner.reply)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def records(self):
        return [record for body in self.bodies for resource in body["resourceLogs"]
                for scope in resource["scopeLogs"] for record in scope["logRecords"]]


def attrs(record):
    return {item["key"]: next(iter(item["value"].values())) for item in record["attributes"]}


def ts(minutes_ago=0):
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)).isoformat()


class PushTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)
        self.state_dir = self.tmp / "harness"
        self.state_dir.mkdir()
        self.collector = Collector()
        self.addCleanup(self.collector.close)
        self.out = []

    def write(self, name, records, mode="a"):
        with open(self.state_dir / name, mode, encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write((record if isinstance(record, str) else json.dumps(record)) + "\n")

    def push(self, env=None, **kwargs):
        env = {"OTEL_EXPORTER_OTLP_ENDPOINT": self.collector.url} if env is None else env
        return telemetry.push("repo", [("(기본)", self.state_dir)], self.state_dir / telemetry.STATE_FILE, collect,
                              PRICING, "0.0.0", harness.hooks_common._file_lock, env=env, out=self.out.append,
                              **kwargs)

    def test_sends_whitelisted_fields_once(self):
        usage = {"claude-opus-5-5": {"input": 0, "output": 1_000_000, "cache_write_5m": 0, "cache_write_1h": 0,
                                     "cache_read": 0}}
        self.write("sessions.jsonl", [
            {"ts": ts(5), "event": "start", "session": "s1", "tool": "claude-code", "branch": "feat/a",
             "transcript": "/home/someone/.claude/secret.jsonl", "head": "abc"},
            {"ts": ts(4), "event": "stop", "session": "s1", "branch": "feat/a", "verify": "pass", "files": 2,
             "usage": usage}])
        self.write("events.jsonl", [
            {"ts": ts(3), "hook": "stop-verify", "decision": "fail", "commands": ["curl -H 'token: x'"],
             "session": "s1", "branch": "feat/a", "retry": True},
            {"ts": ts(2), "hook": "protect-paths", "decision": "block", "rule": "package-lock.json",
             "path": "package-lock.json", "tool": "Edit", "session": "s1"}])
        self.assertEqual(self.push(), 0)
        records = self.collector.records()
        self.assertEqual(sorted(r["body"]["stringValue"] for r in records),
                         ["harness.hook.block", "harness.hook.fail", "harness.session.start", "harness.session.stop"])
        dump = json.dumps(self.collector.bodies)
        for leaked in ("secret.jsonl", "/home/someone", "curl", "token: x", "abc"):
            self.assertNotIn(leaked, dump)
        stop = next(attrs(r) for r in records if r["body"]["stringValue"] == "harness.session.stop")
        self.assertEqual((stop["session.id"], stop["harness.branch"], stop["harness.verify"], stop["harness.files"]),
                         ("s1", "feat/a", "pass", "2"))
        self.assertEqual((stop["harness.tokens.output"], stop["harness.cost_estimate"], stop["harness.currency"]),
                         ("1000000", 20.0, "USD"))
        block = next(r for r in records if r["body"]["stringValue"] == "harness.hook.block")
        self.assertEqual((block["severityText"], attrs(block)["tool_name"], attrs(block)["harness.path"]),
                         ("WARN", "Edit", "package-lock.json"))
        resource = {item["key"]: item["value"]["stringValue"]
                    for item in self.collector.bodies[0]["resourceLogs"][0]["resource"]["attributes"]}
        self.assertEqual((resource["service.name"], resource["harness.repo"]), ("harness", "repo"))
        self.assertEqual(self.collector.headers[0]["Content-Type"], "application/json")

        self.assertEqual(self.push(), 0)  # 두 번째는 보낼 것이 없다
        self.assertEqual(len(self.collector.records()), 4)
        self.write("events.jsonl", [{"ts": ts(1), "hook": "protect-paths", "decision": "ask"}])
        with open(self.state_dir / "events.jsonl", "a", encoding="utf-8", newline="\n") as handle:
            handle.write('{"ts": "' + ts(0) + '", "hook": "x", "decis')  # 아직 쓰는 중인 줄
        self.assertEqual(self.push(), 0)
        self.assertEqual(len(self.collector.records()), 5)
        with open(self.state_dir / "events.jsonl", "a", encoding="utf-8", newline="\n") as handle:
            handle.write('ion": "block"}\n')
        self.assertEqual(self.push(), 0)
        self.assertEqual(self.collector.records()[-1]["body"]["stringValue"], "harness.hook.block")

    def test_failure_keeps_position_and_retries(self):
        self.write("events.jsonl", [{"ts": ts(1), "hook": "protect-paths", "decision": "block"}])
        self.collector.fail = True
        self.assertEqual(self.push(), 1)
        self.assertIn("전송 실패(500)", self.out[-1])
        self.collector.fail = False
        self.assertEqual(self.push(), 0)
        self.assertEqual(len(self.collector.records()), 1)

    def test_partial_success_is_reported(self):
        """#109 Codex 리뷰: OTLP 부분 성공(200 + rejectedLogRecords)은 완료로 보고하지 않고, 거절된 줄은 다시 보내지 않는다"""
        self.write("events.jsonl", [{"ts": ts(1), "hook": "protect-paths", "decision": "ask"},
                                    {"ts": ts(1), "hook": "protect-paths", "decision": "block"}])
        self.collector.reply = b'{"partialSuccess": {"rejectedLogRecords": "1", "errorMessage": "too old"}}'
        self.assertEqual(self.push(), 3)
        self.assertIn("2개 중 1개를 수집기가 거절했다: too old", self.out[-1])
        self.collector.reply = b"{}"
        self.assertEqual(self.push(), 0)
        self.assertEqual(len(self.collector.records()), 2)  # 다시 보내지 않았다

    def test_batches_advance_independently(self):
        self.write("sessions.jsonl", [{"ts": ts(1), "event": "stop", "session": f"s{i}"}
                                      for i in range(telemetry.BATCH + 3)])
        self.assertEqual(self.push(), 0)
        self.assertEqual([sum(len(r["scopeLogs"][0]["logRecords"]) for r in b["resourceLogs"])
                          for b in self.collector.bodies], [telemetry.BATCH, 3])

    def test_broken_line_after_unsent_records_is_not_skipped_on_failure(self):
        """#103 리뷰 F3: 보내지 못한 줄 뒤의 깨진 줄로 위치가 넘어가 앞 줄을 잃지 않는다"""
        self.write("events.jsonl", [{"ts": ts(1), "hook": "protect-paths", "decision": "block"}, "{broken"])
        self.collector.fail = True
        self.assertEqual(self.push(), 1)
        self.collector.fail = False
        self.assertEqual(self.push(), 0)
        self.assertEqual(len(self.collector.records()), 1)
        self.assertEqual(self.push(), 0)  # 깨진 줄은 이제 지나간다
        self.assertEqual(len(self.collector.records()), 1)

    def test_streams_per_session_in_time_order(self):
        """#103 리뷰 F2: 세션마다 리소스(Loki 스트림)를 나누고 시각 순으로 보낸다"""
        self.write("sessions.jsonl", [{"ts": ts(1), "event": "stop", "session": "b"},
                                      {"ts": ts(3), "event": "start", "session": "a"},
                                      {"ts": ts(2), "event": "stop", "session": "a"}])
        self.write("events.jsonl", [{"ts": ts(200), "hook": "protect-paths", "decision": "ask", "session": "a"}])
        self.assertEqual(self.push(), 0)
        streams = {}
        for resource in self.collector.bodies[0]["resourceLogs"]:
            session = {i["key"]: i["value"]["stringValue"] for i in resource["resource"]["attributes"]}["session.id"]
            streams[session] = [int(r["timeUnixNano"]) for r in resource["scopeLogs"][0]["logRecords"]]
        self.assertEqual(sorted(streams), ["a", "b"])
        self.assertEqual(len(streams["a"]), 3)
        self.assertEqual(streams["a"], sorted(streams["a"]))

    def test_concurrent_push_reports_lock(self):
        """#103 리뷰 F6: 다른 push가 잠금을 잡고 있으면 트레이스백 없이 종료 1"""
        self.write("events.jsonl", [{"ts": ts(1), "hook": "protect-paths", "decision": "ask"}])
        with harness.hooks_common._file_lock(self.state_dir / telemetry.STATE_FILE):
            done = []
            thread = threading.Thread(target=lambda: done.append(self.push()))
            thread.start()
            thread.join(30)
        self.assertEqual(done, [1])
        self.assertIn("다른 telemetry push가 실행 중", self.out[-1])
        self.assertEqual(self.push(), 0)
        self.assertEqual(len(self.collector.records()), 1)

    def test_malformed_lines_skipped_and_rotation(self):
        self.write("sessions.jsonl", ["{", "[]", json.dumps({"ts": "2026-13-01T00:00:00Z"}),
                                      json.dumps({"ts": ts(1), "event": "end", "session": "s"})])
        self.assertEqual(self.push(), 0)
        self.assertEqual(len(self.collector.records()), 1)
        self.write("sessions.jsonl", [{"ts": ts(0), "event": "start", "session": "t"}], mode="w")  # 더 작은 새 파일
        self.assertEqual(self.push(), 0)
        self.assertEqual(self.collector.records()[-1]["body"]["stringValue"], "harness.session.start")
        self.assertEqual(self.push(from_start=True), 0)
        self.assertEqual(len(self.collector.records()), 3)

    def test_headers_and_resource_attributes_from_env(self):
        self.write("events.jsonl", [{"ts": ts(1), "hook": "protect-paths", "decision": "ask"}])
        env = {"OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": self.collector.url + "/v1/logs",
               "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer%20s3cr3t,X-Team=a",
               "OTEL_RESOURCE_ATTRIBUTES": "team.id=platform,user.name=Kim%20A"}
        self.assertEqual(self.push(env=env), 0)
        self.assertEqual(self.collector.headers[0]["Authorization"], "Bearer s3cr3t")
        resource = {item["key"]: item["value"]["stringValue"]
                    for item in self.collector.bodies[0]["resourceLogs"][0]["resource"]["attributes"]}
        self.assertEqual((resource["team.id"], resource["user.name"]), ("platform", "Kim A"))
        self.assertNotIn("s3cr3t", "\n".join(self.out))

    def test_config_errors_and_dry_run(self):
        self.write("events.jsonl", [{"ts": ts(1), "hook": "protect-paths", "decision": "ask"}])
        self.assertEqual(self.push(env={}), 2)
        self.assertIn("엔드포인트가 없다", self.out[-1])
        self.assertEqual(self.push(env={"OTEL_EXPORTER_OTLP_ENDPOINT": "file:///etc"}), 2)
        bad = {"OTEL_EXPORTER_OTLP_ENDPOINT": self.collector.url, "OTEL_EXPORTER_OTLP_HEADERS": "novalue-s3cr3t"}
        self.assertEqual(self.push(env=bad), 2)
        self.assertNotIn("s3cr3t", "\n".join(self.out))
        self.assertEqual(self.push(env={}, dry_run=True), 0)  # 엔드포인트 없이도 내용 확인
        self.assertIn("보낼 기록 1개(harness.hook.ask 1)", self.out[-2])
        self.assertFalse((self.state_dir / telemetry.STATE_FILE).exists())
        self.assertEqual(self.collector.bodies, [])
        self.assertEqual(self.push(env={"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.example:4318",
                                        "OTEL_EXPORTER_OTLP_HEADERS": "a=b"}, dry_run=True), 0)
        self.assertTrue(any("암호화하지 않은 http" in line for line in self.out))

    def test_endpoint_resolution(self):
        self.assertEqual(telemetry.endpoint_from("http://c:4318/", {}), "http://c:4318/v1/logs")
        self.assertEqual(telemetry.endpoint_from(None, {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://c"}), "https://c/v1/logs")
        self.assertEqual(telemetry.endpoint_from(None, {"OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "https://c/custom",
                                                        "OTEL_EXPORTER_OTLP_ENDPOINT": "https://x"}), "https://c/custom")


class PushCliTest(SessionRecordCase):
    def test_cli_reads_all_worktrees_and_keeps_state_in_common_dir(self):
        self.init_git()
        wt = self.tmp / "wt-b"
        git(self.project, "worktree", "add", "-q", "-b", "feat/b", str(wt))
        self.assertEqual(self.start().returncode, 0)
        self.assertEqual(self.start(session="s2", project=wt).returncode, 0)
        collector = Collector()
        self.addCleanup(collector.close)
        code, out, err = run_cli(["telemetry", "push", str(wt), "--endpoint", collector.url])
        self.assertEqual(code, 0, err)
        self.assertIn("2개 보냄", out)
        worktrees = sorted(attrs(r)["harness.worktree"] for r in collector.records())
        self.assertEqual(worktrees, ["(기본)", "wt-b"])
        common = Path(git_common(self.project))
        self.assertTrue((common / "harness" / "telemetry.json").is_file())
        code, out, _ = run_cli(["telemetry", "push", str(self.project), "--endpoint", collector.url])
        self.assertEqual((code, len(collector.records())), (0, 2))  # 다른 worktree에서 불러도 같은 위치를 쓴다


def git_common(project):
    import subprocess
    out = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=project,
                         capture_output=True, text=True, check=True).stdout.strip()
    return out


class FragmentConfigTest(unittest.TestCase):
    def test_images_pinned_ports_local_and_no_default_secret(self):
        compose = (OBS / "docker-compose.yml").read_text(encoding="utf-8")
        images = re.findall(r"^\s+image:\s*(\S+)", compose, re.M)
        self.assertEqual(len(images), 4)
        for image in images:
            self.assertRegex(image, r":[\w.-]+@sha256:[0-9a-f]{64}$")
        ports = re.findall(r'^\s+- "([^"]+)"', compose, re.M)
        self.assertTrue(ports)
        for port in ports:
            expected = "${HARNESS_OBS_BIND:-127.0.0.1}:" if port.endswith(":3000") else                 "${HARNESS_OBS_COLLECTOR_BIND:-127.0.0.1}:"  # 인증 없는 수집기는 Grafana와 따로 연다(#103 리뷰 F5)
            self.assertTrue(port.startswith(expected), port)
        self.assertIn("${GRAFANA_ADMIN_PASSWORD:?", compose)
        self.assertIn('GF_AUTH_ANONYMOUS_ENABLED: "false"', compose)

    def test_dashboard_matches_provisioning(self):
        """T6(#127): 모든 패널·쿼리가 데이터소스 변수를 쓰고 변수 기본값이 프로비저닝 uid다. 사람 키는 `$person` 변수"""
        dashboard = json.loads((OBS / "grafana" / "dashboards" / "harness-agentops.json").read_text(encoding="utf-8"))
        provisioned = (OBS / "grafana" / "provisioning" / "datasources" / "datasources.yaml").read_text(encoding="utf-8")
        uids = set(re.findall(r"uid:\s*(\S+)", provisioned))
        self.assertEqual(uids, {"harness-prometheus", "harness-loki"})
        variables = {item["name"]: item for item in dashboard["templating"]["list"]}
        self.assertEqual({name: (variables[name]["type"], variables[name]["query"], variables[name]["current"]["value"])
                          for name in ("DS_PROMETHEUS", "DS_LOKI")},
                         {"DS_PROMETHEUS": ("datasource", "prometheus", "harness-prometheus"),
                          "DS_LOKI": ("datasource", "loki", "harness-loki")})
        person = variables["person"]
        self.assertEqual((person["type"], person["query"].split(","), person["current"]["value"]),
                         ("custom", ["user_email", "member"], "member"))  # 팀 화면은 인증 프록시의 팀원 기준
        self.assertNotIn("harness-prometheus", json.dumps(dashboard["panels"]))  # 고정 uid가 남지 않는다
        self.assertNotIn("harness-loki", json.dumps(dashboard["panels"]))
        panels = list(dashboard_panels(dashboard["panels"]))
        ids = [panel["id"] for panel in panels]
        self.assertEqual(len(ids), len(set(ids)))
        by_type = {"prometheus": "${DS_PROMETHEUS}", "loki": "${DS_LOKI}"}
        for panel in panels:
            if not panel.get("targets"):
                continue  # 접힌 행·안내문은 데이터소스를 조회하지 않는다
            source = panel["datasource"]
            self.assertTrue(source["uid"] == by_type.get(source["type"]) or source["uid"] == "-- Mixed --", panel["title"])
            for target in panel["targets"]:
                used = target["datasource"]
                self.assertTrue(used["uid"] == by_type.get(used["type"]) or used["uid"] == "__expr__",
                                f"{panel['title']}/{target['refId']}")
        exprs = [target.get("expr", "") for panel in panels for target in panel.get("targets", [])]
        self.assertTrue(any("claude_code_cost_usage" in e for e in exprs))
        self.assertTrue(any('service_name="harness"' in e for e in exprs))
        self.assertTrue(any("tool_decision" in e for e in exprs))
        self.assertTrue(any("hook_execution_complete" in e for e in exprs))
        self.assertFalse([e for e in exprs if "user_email" in e])  # 사람 묶음은 모두 변수
        self.assertGreaterEqual(sum("$person" in e for e in exprs), 3)
        # 키트 세션 수(hooks 기록 없는 세션 근사)는 Claude Code 텔레메트리에 없는 Codex 세션을 뺀다
        self.assertTrue(any('harness_kind="session" | harness_tool!="codex"' in e for e in exprs))

    def test_codex_panels_and_combined_cost(self):
        dashboard = json.loads((OBS / "grafana/dashboards/harness-agentops.json").read_text(encoding="utf-8"))
        panels = {panel["id"]: panel for panel in dashboard_panels(dashboard["panels"])}
        targets = {target["refId"]: target for target in panels[2]["targets"]}
        a, b, total = (targets[ref] for ref in ("A", "B", "C"))
        self.assertIn("claude_code_cost_usage", a["expr"])
        self.assertEqual(total["expression"], "($A + $B) / (($D + $E) > 0)")
        self.assertTrue(targets["D"]["hide"] and targets["E"]["hide"])
        self.assertTrue(a["hide"] and b["hide"])
        self.assertEqual(b["expr"].removesuffix(" or vector(0)"), panels[15]["targets"][0]["expr"])
        self.assertIn('harness_currency="USD"', b["expr"])
        fields = {target["legendFormat"] for target in panels[16]["targets"]}
        self.assertEqual(fields, set(collect.USAGE_FIELDS))
        for target in [b, *panels[16]["targets"]]:
            expr = target["expr"]
            self.assertIn('harness_tool="codex"', expr)
            self.assertIn("max by ($person, harness_repo, session_id)", expr)
            self.assertIn("[7d] offset $__range", expr)
            self.assertIn(' | __error__=""', expr)
            if target is b:
                self.assertTrue(expr.endswith("or vector(0)"))  # 부분 수신의 합계 계산용
            else:
                self.assertFalse(expr.endswith("or vector(0)"))  # 원천 무기록을 0으로 꾸미지 않는다

    def test_dashboard_visibility_and_scope(self):
        dashboard = json.loads((OBS / "grafana/dashboards/harness-agentops.json").read_text(encoding="utf-8"))
        panels = {panel["id"]: panel for panel in dashboard_panels(dashboard["panels"])}
        self.assertEqual(dashboard["uid"], "harness-agentops")  # 기존 배포·링크 유지
        rows = [panel for panel in dashboard["panels"] if panel["type"] == "row"]
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(row["collapsed"] and row["panels"] for row in rows))
        # 진단에서는 수집 관측과 Claude 개입·실패를 우선 표시한다.
        self.assertEqual([panel["id"] for panel in dashboard["panels"] if panel["type"] == "stat"], [101, 102, 5, 18])
        self.assertIn('harness_parent=""', panels[17]["targets"][0]["expr"])
        self.assertIn('harness_decision=~"block|ask"', panels[5]["targets"][0]["expr"])
        self.assertIn('harness_hook="stop-verify"', panels[18]["targets"][0]["expr"])
        self.assertIn('harness_decision="fail"', panels[18]["targets"][0]["expr"])
        # legendFormat만으로 Value A/B가 되는 경로도 있어 ref별 표시 이름을 고정한다.
        names = {item["matcher"]["options"]: next(prop["value"] for prop in item["properties"]
                 if prop["id"] == "displayName") for item in panels[16]["fieldConfig"]["overrides"]}
        self.assertEqual(set(names), {target["refId"] for target in panels[16]["targets"]})
        self.assertTrue(all(name and not name.startswith("Value") for name in names.values()))
        self.assertIn("실제 캐시 TTL", panels[16]["description"])
        self.assertTrue(next(t for t in panels[16]["targets"] if t["refId"] == "E")["hide"])
        self.assertEqual(panels[19]["interval"], "5m")
        self.assertEqual(panels[19]["fieldConfig"]["defaults"]["custom"]["drawStyle"], "line")
        for target in panels[20]["targets"]:
            self.assertIn('"person_name", "미식별"', target["expr"])
        # Loki instant 그룹은 표 데이터다. 숫자 열만 reduce하면 마지막 그룹만 남을 수 있다.
        for pid in (14, 23, 24):
            self.assertEqual(panels[pid]["type"], "table")
        self.assertEqual(panels[20]["type"], "barchart")
        self.assertEqual(panels[20]["options"]["stacking"], "normal")
        self.assertEqual(len(panels[20]["targets"]), 2)
        self.assertNotIn(26, panels)
        self.assertNotIn(29, panels)  # 요약 위의 긴 설명을 제거한다.
        self.assertEqual(next(t for t in panels[20]["transformations"] if t["id"] == "joinByField")
                         ["options"]["byField"], "person_name")
        self.assertFalse(panels[19]["fieldConfig"]["defaults"]["custom"]["spanNulls"])
        for target in panels[19]["targets"]:
            self.assertNotIn("$__range", target["expr"])  # 시간당 그래프를 전체 기간 누적으로 오표시하지 않는다
            self.assertFalse(target["expr"].endswith("or vector(0)"))
        self.assertIn("미수집", panels[30]["options"]["content"])
        self.assertIn("#140", panels[30]["options"]["content"])
        self.assertIn("조회 실패", panels[31]["options"]["content"])
        for panel in panels.values():
            if panel.get("targets"):
                self.assertEqual(panel["fieldConfig"]["defaults"]["noValue"], "기록 없음")

    def test_dashboard_layout_has_no_overlapping_visible_panels(self):
        dashboard = json.loads((OBS / "grafana/dashboards/harness-agentops.json").read_text(encoding="utf-8"))

        def check(panels):
            for index, panel in enumerate(panels):
                rect = panel["gridPos"]
                self.assertGreater(rect["w"], 0)
                self.assertGreater(rect["h"], 0)
                self.assertLessEqual(rect["x"] + rect["w"], 24)
                for other in panels[index + 1:]:
                    b = other["gridPos"]
                    overlap = (rect["x"] < b["x"] + b["w"] and b["x"] < rect["x"] + rect["w"] and
                               rect["y"] < b["y"] + b["h"] and b["y"] < rect["y"] + rect["h"])
                    self.assertFalse(overlap, (panel["id"], other["id"]))
                check(panel.get("panels", []))
        check(dashboard["panels"])

    def test_smoke_checks_real_ranges_and_allows_label_replacements(self):
        spec = importlib.util.spec_from_file_location("observability_smoke", ROOT / ".github/scripts/observability_smoke.py")
        smoke = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(smoke)
        dashboard = json.loads((OBS / "grafana/dashboards/harness-agentops.json").read_text(encoding="utf-8"))
        bodies = []

        def response(url, data=None, **kwargs):
            bodies.append(data)
            return {"results": {q["refId"]: {} for q in data["queries"]}}

        with mock.patch.object(smoke, "request", side_effect=response):
            runs = smoke.range_queries(dashboard["panels"], smoke.PROVISIONED, "member", "7d")
        self.assertGreater(runs, 0)
        for body in bodies:
            self.assertEqual(int(body["to"]) - int(body["from"]), 7 * 86400000)
            for target in body["queries"]:
                self.assertNotIn("$person", target["expr"])
                self.assertNotIn("$__", target["expr"])
                if target["datasource"]["type"] == "loki":
                    self.assertEqual(target["queryType"], "range")
                else:
                    self.assertTrue(target["range"])
        people = next(panel for panel in dashboard_panels(dashboard["panels"]) if panel["id"] == 20)
        with mock.patch.object(smoke, "request", return_value={"status": "success", "data": {"result": []}}) as request:
            smoke.query(people["targets"][0], {"person": "member"})  # $1·^$는 정규식이며 미치환 변수가 아니다.
            self.assertTrue(request.called)
            bad = {**people["targets"][0], "expr": "$missing_variable"}
            with self.assertRaises(ValueError):
                smoke.query(bad)

    def test_collector_pipelines(self):
        """T7(#127): 프록시가 넣은 헤더를 member 리소스 속성으로, batch 앞에서 옮긴다(batch는 요청 메타데이터를 버린다)"""
        text = (OBS / "otel-collector.yaml").read_text(encoding="utf-8")
        self.assertIn("deltatocumulative", text)  # Claude Code 기본 delta 지표
        self.assertIn("http://loki:3100/otlp", text)
        self.assertIn("allow_structured_metadata: true", (OBS / "loki.yaml").read_text(encoding="utf-8"))
        self.assertEqual(text.count("include_metadata: true"), 2)  # gRPC(Claude Code)와 HTTP(telemetry push)
        self.assertRegex(text, r"resource/member:.*\n\s+attributes:\n\s+- key: member\n"
                               r"\s+from_context: metadata\.x-harness-member\n\s+action: upsert")
        pipelines = re.findall(r"processors: \[([^\]]+)\]", text)
        self.assertEqual(len(pipelines), 2)
        for line in pipelines:
            names = [name.strip() for name in line.split(",")]
            self.assertLess(names.index("resource/member"), names.index("batch"), line)


class CodexPushTest(unittest.TestCase):
    """#127: 이 저장소의 Codex 세션을 같이 보내고, 세션마다 워터마크로 다시 보내지 않는다"""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)
        self.home, self.repo, self.state_dir = self.tmp / "codex", self.tmp / "repo", self.tmp / "harness"
        self.repo.mkdir()
        self.state_dir.mkdir()
        self.collector = Collector()
        self.addCleanup(self.collector.close)
        self.out = []

    def push(self, **kwargs):
        codex = [("(기본)", event) for _, event in collect.CodexSessions(self.home).records([str(self.repo)])]
        return telemetry.push("repo", [("(기본)", self.state_dir)], self.state_dir / telemetry.STATE_FILE, collect,
                              PRICING, "0.0.0", harness.hooks_common._file_lock,
                              env={"OTEL_EXPORTER_OTLP_ENDPOINT": self.collector.url}, out=self.out.append,
                              codex=codex, **kwargs)

    def events(self):
        return [(attrs(r)["session.id"], attrs(r)["harness.event"]) for r in self.collector.records()]

    def test_codex_sessions_sent_once(self):
        """T3: 이 저장소 worktree에서 연 세션만, 내용 없이, 한 번만"""
        rollout(self.home, "c1", self.repo / "sub")
        rollout(self.home, "other", self.tmp / "elsewhere")
        self.assertEqual(self.push(), 0)
        self.assertEqual(self.events(), [("c1", "start"), ("c1", "stop"), ("c1", "stop")])
        self.assertIn("그중 Codex 세션 사건 3", self.out[-1])
        last = attrs(self.collector.records()[-1])
        self.assertEqual((last["harness.tool"], last["harness.branch"], last["harness.worktree"], last["harness.kind"]),
                         ("codex", "feat/x", "(기본)", "session"))
        self.assertEqual(last["harness.tokens.input"], "1200")  # 세션 누적(Claude Code 세션 기록과 같은 뜻)
        self.assertNotIn(SECRET, json.dumps(self.collector.bodies))
        self.assertNotIn("example.invalid", json.dumps(self.collector.bodies))  # 원격 URL·커밋도 보내지 않는다
        state = json.loads((self.state_dir / telemetry.STATE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(list(state["codex"]), ["c1"])
        self.assertEqual(self.push(), 0)  # 두 번째는 보낼 것이 없다
        self.assertEqual(len(self.events()), 3)
        self.assertIn("0개 보냄", self.out[-1])

    def test_codex_new_turns_after_push(self):
        """T4: 진행 중인 턴의 usage → 턴 완료로 사건이 바뀌어도 이미 보낸 사건은 다시 보내지 않고 새 사건만"""
        path = rollout(self.home, "c1", self.repo, turns=1)
        self.assertEqual(self.push(), 0)
        with open(path, "a", encoding="utf-8") as handle:  # 다음 턴이 진행 중(토큰만 썼다)
            handle.write(json.dumps({"timestamp": codex_ts(10), "type": "event_msg", "payload": {"type": "task_started"}}) + "\n")
            handle.write(json.dumps({"timestamp": codex_ts(9), "type": "token_usage_record",
                                     "payload": {"usage": {"input_tokens": 10, "output_tokens": 5}}}) + "\n")
        self.assertEqual(self.push(), 0)
        with open(path, "a", encoding="utf-8") as handle:  # 턴 완료: 마지막 usage 사건이 응답 사건으로 바뀐다
            handle.write(json.dumps({"timestamp": codex_ts(5), "type": "event_msg",
                                     "payload": {"type": "task_complete"}}) + "\n")
        self.assertEqual(self.push(), 0)
        self.assertEqual([event for _, event in self.events()], ["start", "stop", "turn", "usage", "stop"])
        self.assertEqual(self.push(), 0)
        self.assertEqual(len(self.events()), 5)

    def test_same_time_new_event_and_bad_state(self):
        """워터마크와 같은 시각의 다른 사건은 보낸다. 깨진 상태 값은 무시하고 처음부터"""
        path = rollout(self.home, "c1", self.repo, turns=1)
        self.assertEqual(self.push(), 0)
        stop = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])["timestamp"]
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"timestamp": stop, "type": "event_msg", "payload": {"type": "task_started"}}) + "\n")
        self.assertEqual(self.push(), 0)
        self.assertEqual([event for _, event in self.events()], ["start", "stop", "turn"])
        state_path = self.state_dir / telemetry.STATE_FILE
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["codex"]["c1"] = "broken"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        self.assertEqual(self.push(), 0)
        self.assertEqual(len(self.events()), 6)

    def test_codex_failure_and_from_start(self):
        """T5: 실패하면 워터마크가 전진하지 않고, 복구 뒤 한 번만, --from-start는 다시"""
        rollout(self.home, "c1", self.repo)
        self.collector.fail = True
        self.assertEqual(self.push(), 1)
        self.collector.fail = False
        self.assertEqual(self.push(), 0)
        self.assertEqual(len(self.events()), 3)
        self.assertEqual(self.push(), 0)
        self.assertEqual(len(self.events()), 3)
        self.assertEqual(self.push(from_start=True), 0)
        self.assertEqual(len(self.events()), 6)


def codex_ts(minutes_ago):
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class InRepoTelemetryTest(SessionRecordCase):
    """#127: `init`한 저장소에서 키트 없이 `python .harness/telemetry.py push`"""

    def in_repo(self, *args, cwd=None):
        env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(self.tmp)}
        return subprocess.run([sys.executable, str(self.project / ".harness" / "telemetry.py"), *args],
                              cwd=cwd or self.project, env=env, capture_output=True, text=True, encoding="utf-8",
                              timeout=120)

    def prepare(self):
        code, out, err = init_target(self.project, "github")
        self.assertEqual(code, 0, err)
        self.init_git()
        self.assertEqual(self.start().returncode, 0)
        rollout(Path(os.environ["CODEX_HOME"]), "c1", self.project)  # SessionRecordCase가 임시 CODEX_HOME을 준다

    def test_same_dry_run_as_kit(self):
        """T1: 저장소 안 dry-run이 키트 명령과 같은 내용(관측 시각만 다르다), Codex 세션 포함"""
        self.prepare()
        result = self.in_repo("push", "--dry-run")
        code, out, err = run_cli(["telemetry", "push", str(self.project), "--dry-run"])
        self.assertEqual((result.returncode, code), (0, 0), result.stderr + err)
        observed = re.compile(r'"observedTimeUnixNano": "\d+"')
        self.assertEqual(observed.sub("", result.stdout), observed.sub("", out))
        self.assertEqual(result.stderr, err)
        self.assertIn("보낼 기록 4개", out)  # Claude Code 세션 시작 1 + Codex 3
        self.assertIn("그중 Codex 세션 사건 3", out)
        self.assertIn('"stringValue": "codex"', out)
        self.assertFalse((Path(git_common(self.project)) / "harness" / telemetry.STATE_FILE).exists())
        # 관리 디렉터리에 __pycache__를 남기지 않아 check가 여분으로 보지 않는다
        code, out, err = run_cli(["check", str(self.project)])
        self.assertEqual(code, 0, out + err)

    def test_sends_and_shares_state_with_kit(self):
        """T2: 다른 worktree에서 실행해도 같은 위치(공용 git 디렉터리)를 쓰고, 키트 명령과 위치를 나눠 쓴다"""
        self.prepare()
        collector = Collector()
        self.addCleanup(collector.close)
        wt = self.tmp / "wt-b"
        git(self.project, "worktree", "add", "-q", "-b", "feat/b", str(wt))
        result = self.in_repo("push", "--endpoint", collector.url, cwd=wt)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("4개 보냄", result.stdout)
        resource = {item["key"]: item["value"]["stringValue"]
                    for item in collector.bodies[0]["resourceLogs"][0]["resource"]["attributes"]}
        self.assertEqual(resource["service.version"], harness.VERSION)  # harness.json의 harness_version
        self.assertEqual(self.in_repo("push", "--endpoint", collector.url).returncode, 0)
        code, out, _ = run_cli(["telemetry", "push", str(self.project), "--endpoint", collector.url])
        self.assertEqual((code, len(collector.records())), (0, 4))
        self.assertIn("0개 보냄", out)

    def test_subdirectory_reads_root_config(self):
        """#127 리뷰 F2: 하위 디렉터리에서 실행해도 worktree 루트의 harness.json(키트 버전·단가)을 쓴다"""
        self.prepare()
        sub = self.project / "docs"
        result = self.in_repo("push", "--dry-run", cwd=sub)
        root = self.in_repo("push", "--dry-run")
        self.assertEqual((result.returncode, root.returncode), (0, 0), result.stderr)
        self.assertIn(f'"stringValue": "{harness.VERSION}"', result.stdout)
        observed = re.compile(r'"observedTimeUnixNano": "\d+"')
        self.assertEqual(observed.sub("", result.stdout), observed.sub("", root.stdout))

    def test_missing_copy_exits_2(self):
        """복사본(summary.py·collect.py)이 없으면 종료 2와 다시 생성하라는 안내"""
        self.prepare()
        for rel in (".harness/metrics/collect.py", ".harness/summary.py"):
            with self.subTest(rel=rel):
                (self.project / rel).unlink()
                result = self.in_repo("push", "--dry-run")
                self.assertEqual((result.returncode, result.stdout), (2, ""))
                self.assertIn("init <저장소> --force", result.stderr)


class AutoPushTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        spec = importlib.util.spec_from_file_location("session_end_auto_push", ROOT / "core/hooks/session-end.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.env = mock.patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def install(self):
        path = self.project / ".harness/telemetry.py"
        path.parent.mkdir()
        path.touch()
        return path

    def test_no_endpoint_or_missing_copy_does_not_spawn(self):
        with mock.patch.object(self.module.common.subprocess, "Popen") as spawn:
            self.module.push_in_background(self.project)
            self.install()
            self.module.push_in_background(self.project)
            os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "https://collector.invalid"
            (self.project / ".harness/telemetry.py").unlink()
            self.module.push_in_background(self.project)
        spawn.assert_not_called()

    def test_detached_without_shell_or_pipes(self):
        script = self.install()
        for endpoint in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"):
            with self.subTest(endpoint=endpoint), mock.patch.dict(os.environ, {endpoint: "https://collector.invalid",
                      "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=secret-sentinel"}, clear=True), \
                    mock.patch.object(self.module.common.subprocess, "Popen") as spawn:
                self.module.push_in_background(self.project)
                args, kwargs = spawn.call_args
                self.assertEqual(args, ([sys.executable, str(script), "push"],))
                self.assertEqual(kwargs["cwd"], self.project)
                self.assertTrue(kwargs["close_fds"])
                self.assertNotIn("env", kwargs)  # 엔드포인트·인증을 현재 환경에서 상속
                self.assertNotIn("shell", kwargs)
                for stream in ("stdin", "stdout", "stderr"):
                    self.assertEqual(kwargs[stream], subprocess.DEVNULL)
                if os.name == "nt":
                    # 콘솔 없는 프로세스(DETACHED_PROCESS)가 git을 띄우면 실행마다 창이 생긴다(#145)
                    self.assertFalse(kwargs["creationflags"] & subprocess.DETACHED_PROCESS)
                    self.assertTrue(kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW)
                    self.assertTrue(kwargs["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP)
                else:
                    self.assertTrue(kwargs["start_new_session"])
                spawn.return_value.wait.assert_not_called()
                spawn.return_value.communicate.assert_not_called()

    def test_record_precedes_push_and_failures_stay_silent(self):
        for failed in (None, "record", "push"):
            calls = []
            def action(name):
                calls.append(name)
                if name == failed:
                    raise OSError("failure")
            with self.subTest(failed=failed), \
                    mock.patch.object(self.module.common, "read_payload", return_value={}), \
                    mock.patch.object(self.module.common, "project_dir", return_value=self.project), \
                    mock.patch.object(self.module.common, "log_session_usage", side_effect=lambda *a, **k: action("record")), \
                    mock.patch.object(self.module, "push_in_background", side_effect=lambda *a: action("push")), \
                    mock.patch("sys.stdout") as out, mock.patch("sys.stderr") as err:
                self.assertEqual(self.module.main(), 0)
                self.assertEqual(calls, ["record", "push"])
                out.write.assert_not_called()
                err.write.assert_not_called()


class AutoPushIntegrationTest(SessionRecordCase):
    def prepare(self):
        code, _, err = init_target(self.project, "github")
        self.assertEqual(code, 0, err)
        self.init_git()
        self.collector = Collector()
        self.addCleanup(self.collector.close)
        self.env = {**os.environ, "CLAUDE_PROJECT_DIR": str(self.project),
                    "OTEL_EXPORTER_OTLP_ENDPOINT": self.collector.url, "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "",
                    "OTEL_EXPORTER_OTLP_HEADERS": "X-Test=auto-push", "OTEL_EXPORTER_OTLP_LOGS_HEADERS": "",
                    "OTEL_RESOURCE_ATTRIBUTES": ""}
        self.state = Path(git_common(self.project)) / "harness/telemetry.json"

    def wait_for(self, predicate):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail("백그라운드 push가 완료되지 않았다")

    def saved(self):
        if not self.state.is_file():
            return False
        # save는 원자적 교체를 하므로 완성된 JSON만 보인다.
        return json.loads(self.state.read_text(encoding="utf-8"))

    def end_installed(self):
        result = subprocess.run([sys.executable, str(self.project / ".claude/hooks/session-end.py")],
                                input=json.dumps({"session_id": "s1"}), cwd=self.project, env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_installed_hook_sends_codex_and_resumes_without_duplicates(self):
        self.prepare()
        self.start()
        rollout(Path(self.env["CODEX_HOME"]), "auto-codex", self.project)
        self.end_installed()
        self.wait_for(lambda: len(self.collector.records()) == 5 and self.saved())
        self.assertEqual(self.collector.headers[0]["X-Test"], "auto-push")
        self.end_installed()
        self.wait_for(lambda: len(self.collector.records()) == 6)
        self.assertEqual(sum(attrs(r).get("harness.tool") == "codex" for r in self.collector.records()), 3)
        self.assertNotIn(SECRET, json.dumps(self.collector.bodies))
        self.wait_for(lambda: self.saved())

    def test_failure_retries_at_next_session_end(self):
        self.prepare()
        self.collector.fail = True
        self.end_installed()
        self.wait_for(lambda: self.collector.attempts == 1 and self.saved())
        self.assertEqual(self.collector.records(), [])
        self.collector.fail = False
        self.end_installed()
        self.wait_for(lambda: len(self.collector.records()) == 2)
        self.assertTrue(all(attrs(r)["harness.event"] == "end" for r in self.collector.records()))
        self.wait_for(lambda: self.saved())


if __name__ == "__main__":
    unittest.main()
