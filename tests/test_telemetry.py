"""#103 M5-8 팀 대시보드 선택 조각: `harness telemetry push`와 조각 설정."""

import datetime as dt
import json
import re
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_hooks import git  # noqa: E402
from test_sessions import SessionRecordCase, collect, harness, run_cli  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OBS = ROOT / "core" / "observability"
telemetry = harness.load_telemetry()
PRICING = collect.merged_pricing({})


class Collector:
    """OTLP/HTTP 로그를 받는 가짜 수집기. fail이 참이면 500."""

    def __init__(self):
        self.bodies, self.headers, self.fail, self.reply = [], [], False, b"{}"
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):  # noqa: N802
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
        dashboard = json.loads((OBS / "grafana" / "dashboards" / "harness-agentops.json").read_text(encoding="utf-8"))
        provisioned = (OBS / "grafana" / "provisioning" / "datasources" / "datasources.yaml").read_text(encoding="utf-8")
        uids = set(re.findall(r"uid:\s*(\S+)", provisioned))
        self.assertEqual(uids, {"harness-prometheus", "harness-loki"})
        ids = [panel["id"] for panel in dashboard["panels"]]
        self.assertEqual(len(ids), len(set(ids)))
        used = {target["datasource"]["uid"] for panel in dashboard["panels"] for target in panel["targets"]}
        self.assertEqual(used - {"__expr__"}, uids)
        exprs = [target.get("expr", "") for panel in dashboard["panels"] for target in panel["targets"]]
        self.assertTrue(any("claude_code_cost_usage" in e for e in exprs))
        self.assertTrue(any('service_name="harness"' in e for e in exprs))
        self.assertTrue(any("tool_decision" in e for e in exprs))
        self.assertTrue(any("hook_execution_complete" in e for e in exprs))

    def test_collector_pipelines(self):
        text = (OBS / "otel-collector.yaml").read_text(encoding="utf-8")
        self.assertIn("deltatocumulative", text)  # Claude Code 기본 delta 지표
        self.assertIn("http://loki:3100/otlp", text)
        self.assertIn("allow_structured_metadata: true", (OBS / "loki.yaml").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
