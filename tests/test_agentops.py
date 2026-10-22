"""Evidence contract integration tests: real Git repos, transcripts and OTLP."""
import concurrent.futures
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import process_support  # noqa: E402,F401 — test-only Windows child launch policy

SPEC = importlib.util.spec_from_file_location("agentops", Path(__file__).resolve().parents[1] / "core/metrics/agentops.py")
a = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(a)


class ConsoleWindowTest(unittest.TestCase):
    def test_git_uses_no_window_on_windows_and_zero_flags_on_posix(self):
        for system, expected in (("nt", 0x08000000), ("posix", 0)):
            with self.subTest(system=system), patch.object(a.os, "name", system), \
                    patch.object(a.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
                    patch.object(a.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"branch\n")) as run:
                self.assertEqual(a.git("repo", "branch", "--show-current"), "branch")
                self.assertEqual(run.call_args.kwargs["creationflags"], expected)

class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        for args in (["init", "-q"], ["config", "user.email", "test@example.invalid"], ["config", "user.name", "Test"]):
            subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)
        (self.root / "file.txt").write_text("original", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "initial"], check=True, capture_output=True)
        self.store = a.Store(self.root)
        self.store.bind("TEST-1", "s1", "codex", since="2026-01-01T00:00:00Z")

    def test_schema_rejects_content_unknown_fields_secret_and_bad_types(self):
        for data in ({"content": "secret"}, {"length": True}, {"length": -1}, {"message_id": "sk-" + "a" * 30}):
            with self.subTest(data=data), self.assertRaises(a.EvidenceError):
                self.store.emit("prompt", data, session="s1")
        self.assertEqual(len(self.store.records()), 1)

    def auto_log(self, session="auto-1", cwd=None, timestamp="2026-01-01T00:00:01Z", branch="feat/TEST-42-task"):
        path = self.codex(session, cwd)
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        rows[0]["timestamp"] = timestamp
        rows[0]["payload"]["git"] = {"branch": branch, "commit_hash": "a" * 40}
        home = Path(self.temp.name) / "codex-home"
        dest = home / "sessions" / path.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        return home, dest

    def enable_auto(self):
        with patch.object(a, "stamp", return_value="2026-01-01T00:00:00Z"):
            return a.auto_bind(self.store, "enable")

    def test_auto_enrollment_is_opt_in_and_imports_new_session_once(self):
        home, _ = self.auto_log()
        with patch.dict(os.environ, {"CODEX_HOME": str(home)}):
            self.assertEqual(a.sync(self.store)["discovery"]["discovered"], 0)
            self.enable_auto()
            state = a.sync(self.store)
            self.assertEqual(state["discovery"]["discovered"], 1)
            self.assertNotIn("auto-1", state["missing_sessions"])
            first = [r for r in self.store.records() if r["session"] == "auto-1"]
            self.assertTrue(any(r["kind"] == "prompt" for r in first))
            self.assertTrue(any(r.get("rules_digest") for r in first))
            self.assertTrue(all(r["task"] == "TEST-42" for r in first))
            self.assertEqual(a.sync(self.store)["discovery"]["discovered"], 0)
            self.assertEqual(first, [r for r in self.store.records() if r["session"] == "auto-1"])

    def test_auto_scope_excludes_siblings_nested_repos_and_other_worktrees(self):
        sibling = self.root.parent / "repo-other"
        sibling.mkdir()
        nested = self.root / "nested"
        nested.mkdir()
        for folder in (sibling, nested):
            subprocess.run(["git", "-C", str(folder), "init", "-q"], check=True, capture_output=True)
        other = self.root.parent / "worktree"
        subprocess.run(["git", "-C", str(self.root), "worktree", "add", "--detach", str(other)], check=True, capture_output=True)
        for i, folder in enumerate((sibling, nested, other)):
            home, _ = self.auto_log("foreign-" + str(i), folder)
        subfolder = self.root / "subfolder"
        subfolder.mkdir()
        self.auto_log("inside", subfolder)
        self.enable_auto()
        with patch.dict(os.environ, {"CODEX_HOME": str(home)}):
            self.assertEqual(a.sync(self.store)["discovery"]["discovered"], 1)
        self.assertEqual(set(a.read_json(self.store.binding)), {"s1", "inside"})

    def test_auto_does_not_enroll_old_resumed_session_or_replace_explicit_binding(self):
        home, _ = self.auto_log("old", timestamp="2025-12-31T23:59:59Z")
        self.auto_log("s1")
        self.enable_auto()
        before = a.read_json(self.store.binding)["s1"]
        with patch.dict(os.environ, {"CODEX_HOME": str(home)}):
            a.sync(self.store)
        self.assertEqual(a.read_json(self.store.binding), {"s1": before})
        self.assertIsNone(self.store.bind("TEST-99", "s1", "codex", only_if_absent=True))
        self.assertEqual(a.read_json(self.store.binding)["s1"], before)

    def test_auto_uses_session_branch_and_keeps_missing_task_unbound(self):
        home, _ = self.auto_log("start-branch")
        self.auto_log("main-session", branch="main")
        subprocess.run(["git", "-C", str(self.root), "checkout", "-qb", "feat/TEST-99-now"], check=True, capture_output=True)
        self.enable_auto()
        with patch.dict(os.environ, {"CODEX_HOME": str(home)}):
            a.sync(self.store)
        bindings = a.read_json(self.store.binding)
        self.assertEqual(bindings["start-branch"]["task"], "TEST-42")
        self.assertEqual(bindings["main-session"]["task"], "unbound")

    def test_auto_retries_partial_header_and_isolates_malformed_header(self):
        home, path = self.auto_log("partial")
        complete = path.read_text(encoding="utf-8")
        path.write_text(complete[:20], encoding="utf-8")
        bad = home / "sessions" / "bad.jsonl"
        bad.write_text('{"type":"session_meta","payload":[]}\n', encoding="utf-8")
        self.enable_auto()
        with patch.dict(os.environ, {"CODEX_HOME": str(home)}):
            first = a.sync(self.store)
            self.assertEqual(first["discovery"]["discovered"], 0)
            self.assertTrue(first["errors"])
            path.write_text(complete, encoding="utf-8")
            second = a.sync(self.store)
            self.assertEqual(second["discovery"]["discovered"], 1)

    def test_auto_extracts_task_before_sanitizing_unicode_or_long_branch(self):
        for session, branch in (("unicode", "feat/TEST-42-수집"), ("long", "feat/TEST-42-" + "x" * 180)):
            home, _ = self.auto_log(session, branch=branch)
        self.enable_auto()
        with patch.dict(os.environ, {"CODEX_HOME": str(home)}):
            a.sync(self.store)
        for session in ("unicode", "long"):
            self.assertEqual(a.read_json(self.store.binding)[session]["task"], "TEST-42")
            binding = next(r for r in self.store.records() if r["kind"] == "binding" and r["session"] == session)
            self.assertEqual(binding["branch"], "unknown")

    def test_auto_enable_is_idempotent_and_disable_stops_new_enrollment(self):
        home, _ = self.auto_log()
        first = self.enable_auto()
        with patch.object(a, "lock", side_effect=AssertionError("status must be read-only")):
            self.assertEqual(a.auto_bind(self.store), first)
        self.assertEqual(a.auto_bind(self.store, "enable"), first)
        a.auto_bind(self.store, "disable")
        with patch.dict(os.environ, {"CODEX_HOME": str(home)}):
            self.assertFalse(a.sync(self.store)["discovery"]["enabled"])
        a.atomic_json(self.store.path / "discovery.json", [])
        with patch.dict(os.environ, {"CODEX_HOME": str(home)}):
            self.assertIn("discovery-settings", a.sync(self.store)["errors"])

    def test_concurrent_appends_are_complete(self):
        def emit(i):
            a.Store(self.root).emit("prompt", {"length": i}, session="s1")
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(emit, range(25)))
        self.assertEqual(len(self.store.records()), 26)

    def test_verification_success_failure_and_timeout(self):
        code, passed = a.verify(self.store, "unit", [sys.executable, "-c", "pass"], session="s1")
        self.assertEqual(code, 0)
        self.assertEqual(passed["status"], "pass")
        self.assertEqual(a.verify(self.store, "unit", [sys.executable, "-c", "raise SystemExit(2)"], session="s1")[1]["status"], "fail")
        self.assertEqual(a.verify(self.store, "unit", [sys.executable, "-c", "import time;time.sleep(10)"], timeout=.1, session="s1")[1]["status"], "timeout")
        self.store.emit("outcome", {"criterion": "acceptance", "status": "pass", "run_id": passed["run_id"]}, session="s1")
        self.assertEqual(a.report(self.store)["verified_criteria"], 1)

    def test_changed_state_is_not_a_pass(self):
        code, row = a.verify(self.store, "mutating", [sys.executable, "-c", "open('file.txt','w').write('changed')"], session="s1")
        self.assertEqual(code, 1)
        self.assertEqual(row["status"], "error")

    def test_untracked_and_staged_changes_affect_fingerprint(self):
        before = a.state_digest(self.root)
        (self.root / "new.txt").write_text("new")
        self.assertNotEqual(before, a.state_digest(self.root))
        subprocess.run(["git", "-C", str(self.root), "add", "new.txt"], check=True)
        self.assertNotEqual(before, a.state_digest(self.root))

    def codex(self, session="s1", cwd=None):
        rows = [
            {"type": "session_meta", "payload": {"id": session, "cwd": str(cwd or self.root), "cli_version": "1.0"}},
            {"type": "turn_context", "payload": {"turn_id": "turn-1", "model": "model-a", "effort": "high"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "do this password=secret-value"}},
            {"type": "response_item", "payload": {"type": "function_call", "name": "exec", "call_id": "c1", "arguments": "SECRET-INPUT"}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c1", "output": "SECRET-OUTPUT"}},
            {"type": "event_msg", "payload": {"type": "context_compacted"}},
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-1"}},
        ]
        for i, row in enumerate(rows):
            row["timestamp"] = f"2026-01-01T00:00:{i:02}Z"
        path = Path(self.temp.name) / (session + ".jsonl")
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        return path

    def test_codex_import_idempotent_private_and_conservative(self):
        path = self.codex()
        self.assertGreater(a.transcript(self.store, path, "codex"), 0)
        count = len(self.store.records())
        a.transcript(self.store, path, "codex")
        self.assertEqual(count, len(self.store.records()))
        raw = self.store.events.read_text(encoding="utf-8")
        self.assertNotIn("SECRET", raw)
        self.assertNotIn("password", raw)
        tools = [r for r in self.store.records() if r["kind"] == "tool"]
        self.assertEqual(tools[-1]["status"], "unknown")
        self.assertNotIn("head", tools[-1])
        self.assertEqual(a.report(self.store)["observed_tool_duration_ms"], 1000)

    def test_explicit_content_is_local_and_redacted(self):
        a.transcript(self.store, self.codex(), "codex", capture_content=True)
        private = list((self.store.path / "private").glob("*.json"))
        self.assertEqual(len(private), 1)
        self.assertIn("[REDACTED]", private[0].read_text(encoding="utf-8"))
        self.assertNotIn("secret-value", json.dumps(a.otlp(self.store, self.store.records())))

    def test_foreign_session_refused_until_explicitly_bound(self):
        path = self.codex("foreign", Path(self.temp.name))
        with self.assertRaises(a.EvidenceError):
            a.transcript(self.store, path, "codex")
        self.store.bind("TEST-1", "foreign", "codex", since="2026-01-01T00:00:00Z")
        self.assertGreater(a.transcript(self.store, path, "codex"), 0)

    def test_binding_boundary_and_partial_live_row(self):
        self.store.bind("TEST-2", "s1", "codex", since="2026-01-01T00:00:05Z")
        path = self.codex()
        with path.open("a") as f:
            f.write('{"timestamp":')
        a.transcript(self.store, path, "codex")
        events = [r for r in self.store.records() if r["task"] == "TEST-2" and r["source"] == "observed"]
        self.assertEqual({r["kind"] for r in events}, {"context", "turn"})

    def test_delayed_import_preserves_previous_task_interval(self):
        self.store.bind("TEST-2", "s1", "codex", since="2026-01-01T00:00:05Z")
        a.transcript(self.store, self.codex(), "codex")
        first = [r for r in self.store.records() if r["task"] == "TEST-1" and r["kind"] == "tool"]
        second = [r for r in self.store.records() if r["task"] == "TEST-2" and r["kind"] == "turn"]
        self.assertEqual(len(first), 2)
        self.assertEqual(len(second), 1)

    def test_unbound_import_is_rejected_before_any_events_are_added(self):
        path = self.codex("new-session")
        before = len(self.store.records())
        with self.assertRaises(a.EvidenceError):
            a.transcript(self.store, path, "codex")
        self.assertEqual(before, len(self.store.records()))

    def test_token_info_schema_drift_does_not_kill_sync(self):
        path = self.codex()
        with path.open("a") as handle:
            handle.write(json.dumps({"type": "event_msg", "timestamp": "2026-01-01T00:00:07Z", "payload": {"type": "token_count", "info": "unavailable"}}) + "\n")
        self.assertGreater(a.transcript(self.store, path, "codex"), 0)

    def test_claude_verification_sync_preserves_task_history(self):
        self.store.bind("TEST-2", "s1", "claude-code", since="2026-01-01T00:00:05Z")
        rows = [{"session": "s1", "ts": f"2026-01-01T00:00:0{i}Z", "verify": "pass"} for i in (3, 6)]
        (self.store.path.parent / "sessions.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        a.sync(self.store)
        events = [r for r in self.store.records() if r["kind"] == "verification"]
        self.assertEqual([r["task"] for r in events], ["TEST-1", "TEST-2"])

    def test_claude_tool_blocks_are_individually_preserved(self):
        path = Path(self.temp.name) / "claude.jsonl"
        rows = [
            {"type": "assistant", "message": {"model": "claude-test", "content": [
                {"type": "tool_use", "id": "c1", "name": "Read", "input": {"secret": "no"}},
                {"type": "tool_use", "id": "c2", "name": "Bash"}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "c1", "is_error": True, "content": "sensitive"}]}},
        ]
        for i, row in enumerate(rows):
            row.update({"sessionId": "s1", "cwd": str(self.root), "timestamp": f"2026-01-01T00:00:0{i}Z"})
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        a.transcript(self.store, path, "claude-code")
        self.assertEqual(len([r for r in self.store.records() if r["kind"] == "tool"]), 3)
        self.assertNotIn("sensitive", self.store.events.read_text(encoding="utf-8"))

    def test_hooks_preserve_existing_settings_and_are_idempotent(self):
        path = self.root / ".claude/settings.json"
        path.parent.mkdir()
        original = {"permissions": {"deny": ["Bash(rm:*)"]}, "hooks": {"Stop": [{"hooks": [{"command": "keep-me"}]}]}}
        path.write_text(json.dumps(original))
        a.install_hooks(self.store)
        once = json.loads(path.read_text())
        a.install_hooks(self.store)
        self.assertEqual(once, json.loads(path.read_text()))
        self.assertEqual(once["permissions"], original["permissions"])
        self.assertEqual(once["hooks"]["Stop"][0], original["hooks"]["Stop"][0])

    def test_report_does_not_treat_reported_outcome_as_verified(self):
        self.store.emit("outcome", {"criterion": "test", "status": "pass", "run_id": "fake"}, session="s1")
        self.store.emit("intervention", {"action": "redirect", "reason": "requirements"}, session="s1", source="human")
        self.assertEqual(a.report(self.store)["verified_criteria"], 0)
        self.assertIsNone(a.report(self.store)["first_verification_pass_rate"])

    def test_sync_bound_sessions_only_and_missing_is_visible(self):
        home = Path(self.temp.name) / "home"
        (home / "sessions").mkdir(parents=True)
        path = self.codex()
        (home / "sessions/rollout-s1.jsonl").write_bytes(path.read_bytes())
        self.store.bind("TEST-1", "missing", "codex")
        with patch.dict(os.environ, {"CODEX_HOME": str(home)}):
            state = a.sync(self.store)
        self.assertGreater(state["imported"], 0)
        self.assertEqual(state["missing_sessions"], ["missing"])

    def test_corrupt_store_stops_report_and_export(self):
        with self.store.events.open("a") as f:
            f.write("null\n")
        with self.assertRaises(a.EvidenceError):
            a.report(self.store)

    def test_transport_failure_retry_and_partial_rejection(self):
        replies = [(500, {}), (200, {"partialSuccess": {"rejectedLogRecords": "1"}})]
        received = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                code, body = replies.pop(0)
                self.send_response(code); self.end_headers(); self.wfile.write(json.dumps(body).encode())
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        endpoint = f"http://127.0.0.1:{server.server_port}"
        self.assertEqual(a.push(self.store, endpoint, True)["pending"], 1)
        self.assertFalse((self.store.path / "transport.json").exists())
        self.assertEqual(a.push(self.store, endpoint)["status"], "error")
        self.assertEqual(a.push(self.store, endpoint)["status"], "partial")
        self.assertEqual(len(received), 2)
        self.assertEqual(a.push(self.store, endpoint)["pending"], 0)
        self.assertEqual(len(received), 2)

    def test_remote_cleartext_refused(self):
        with self.assertRaises(a.EvidenceError):
            a.push(self.store, "http://example.invalid")

    def test_resume_preserves_explicit_review_binding(self):
        self.store.bind("TEST-1", "s1", "claude-code", pr="123", role="review", parent="parent")
        before = a.read_json(self.store.binding)
        a.hook(self.store, {"hook_event_name": "SessionStart", "source": "resume", "session_id": "s1"})
        self.assertEqual(before, a.read_json(self.store.binding))

    def test_untracked_rename_cannot_reuse_state_hash(self):
        p = self.root / "a"
        p.write_text("bc")
        before = a.state_digest(self.root)
        p.unlink()
        (self.root / "ab").write_text("c")
        self.assertNotEqual(before, a.state_digest(self.root))

    def test_redirect_does_not_forward_auth_or_payload(self):
        received = []
        class Destination(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(self.headers.get("Authorization")); self.send_response(200); self.end_headers()
            def log_message(self, *args):
                pass
        target = ThreadingHTTPServer(("127.0.0.1", 0), Destination)
        class Redirect(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(302); self.send_header("Location", f"http://127.0.0.1:{target.server_port}/"); self.end_headers()
            def log_message(self, *args):
                pass
        origin = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        for server in (target, origin):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        with patch.dict(os.environ, {"OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer%20test"}):
            state = a.push(self.store, f"http://127.0.0.1:{origin.server_port}")
        self.assertEqual(state["status"], "error")
        self.assertEqual(received, [])

    def test_outcomes_are_derived_and_idempotent_without_claiming_task_done(self):
        _, passed = a.verify(self.store, "unit", [sys.executable, "-c", "pass"], session="s1")
        a.sync_outcomes(self.store)
        rows = [r for r in self.store.records() if r["kind"] == "outcome"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["criterion"], "check:unit")
        self.assertEqual(rows[0]["run_id"], passed["run_id"])
        self.assertEqual(rows[0]["status"], "pass")

    def test_unexecuted_check_becomes_unverified(self):
        self.store.emit("verification", {"check_id": "lint", "status": "not_run"}, session="s1")
        a.sync_outcomes(self.store)
        self.assertEqual(a.report(self.store)["outcomes"][0]["status"], "unverified")

    def question_transcript(self, answer="approve", ref=None, prefix="", answered_at="2026-01-01T00:00:06Z"):
        path = self.codex()
        rows = [
            {"timestamp": "2026-01-01T00:00:07Z", "type": "response_item", "payload": {
                "type": "function_call", "name": "request_user_input_async", "call_id": "q1",
                "arguments": json.dumps({"questions": [{"title": "PRIVATE-QUESTION", "options": ["PRIVATE-OPTION"]}]})}},
            {"timestamp": answered_at, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": prefix + '<send_user_message_question_reply>' + json.dumps([{"questionItemId": json.dumps(ref or ["request_user_input_async", "q1", 0]), "question": "PRIVATE-QUESTION", "answer": answer}]) + '</send_user_message_question_reply>'}]}},
        ]
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        return path

    def test_structured_answer_records_intervention_and_exact_wait_without_text(self):
        path = self.question_transcript(answer="PRIVATE-ANSWER", answered_at="2026-01-01T00:00:12Z")
        a.transcript(self.store, path, "codex")
        a.transcript(self.store, path, "codex")
        rows = self.store.records()
        waits = [r for r in rows if r["kind"] == "wait"]
        actions = [r for r in rows if r["kind"] == "intervention"]
        self.assertEqual(len(waits), 2)
        self.assertEqual(waits[-1]["duration_ms"], 5000)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action"], "answer")
        self.assertNotIn("PRIVATE", self.store.events.read_text(encoding="utf-8"))

    def test_quoted_reply_unknown_question_and_backwards_time_do_not_resolve(self):
        for kwargs in ({"prefix": "quoted example: "}, {"ref": ["request_user_input_async", "foreign", 0]}, {"answered_at": "2026-01-01T00:00:06Z"}):
            with self.subTest(kwargs=kwargs):
                path = self.question_transcript(**kwargs)
                a.transcript(self.store, path, "codex")
                self.assertFalse([r for r in self.store.records() if r["kind"] == "intervention"])

    def test_answer_after_task_switch_closes_original_task_wait(self):
        self.store.bind("TEST-2", "s1", "codex", since="2026-01-01T00:00:09Z")
        a.transcript(self.store, self.question_transcript(answered_at="2026-01-01T00:00:12Z"), "codex")
        waits = [r for r in self.store.records() if r["kind"] == "wait"]
        self.assertEqual([r["task"] for r in waits], ["TEST-1", "TEST-1"])
        self.assertEqual(a.report(self.store, "TEST-1")["open_waits"], 0)

    def test_explicit_interaction_pairs_wait_and_human_decision(self):
        a.interaction(self.store, "approval-1", reason="approval", session="s1", source="agent")
        self.store.bind("TEST-2", "s1", "codex")
        a.interaction(self.store, "approval-1", "approve", "approval", "s1", source="human")
        result = a.report(self.store, "TEST-1")
        self.assertEqual(result["interventions"][0]["action"], "approve")
        self.assertEqual(result["open_waits"], 0)
        with self.assertRaises(a.EvidenceError):
            a.interaction(self.store, "approval-1", "reject", session="s1")

    def review_path(self, treatment="채택", extra=""):
        path = self.root / "docs/reviews/TEST-1.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# PRIVATE-FINDING\n## 7. 처리\n| # | 결함 | 심각도 | 처리 |\n| --- | --- | --- | --- |\n| F1 | PRIVATE-DESCRIPTION | 중간 | " + treatment + " |\n" + extra, encoding="utf-8")
        return path

    def test_review_treatment_tracks_changes_not_prose_and_not_duplicate_appendices(self):
        path = self.review_path()
        a.track_review(self.store, path, "s1")
        self.assertEqual(a.sync_workflow(self.store)["reviews"], 1)
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\nactivation note changed\n")
        self.assertEqual(a.sync_workflow(self.store)["reviews"], 0)
        self.review_path("후속")
        self.assertEqual(a.sync_workflow(self.store)["reviews"], 1)
        self.assertNotIn("PRIVATE", self.store.events.read_text(encoding="utf-8"))
        self.assertEqual([r["status"] for r in a.report(self.store)["reviews"]], ["accepted", "followup"])

    def test_incomplete_review_reports_error_without_partial_findings(self):
        path = self.review_path(extra="| F2 | description | 중간 | |\n")
        a.track_review(self.store, path, "s1")
        result = a.sync_workflow(self.store)
        self.assertTrue(result["errors"])
        self.assertEqual(result["reviews"], 0)

    def test_review_from_different_task_or_foreign_repo_is_rejected(self):
        path = self.review_path()
        other = path.with_name("TEST-2.md")
        other.write_bytes(path.read_bytes())
        with self.assertRaises(a.EvidenceError):
            a.track_review(self.store, other, "s1")
        with self.assertRaises(a.EvidenceError):
            a.track_review(self.store, Path(self.temp.name) / "TEST-1.md", "s1")

    def test_review_roundtrip_and_row_removal_are_observable(self):
        path = self.review_path()
        a.track_review(self.store, path, "s1")
        for treatment in ("채택", "후속", "채택"):
            self.review_path(treatment)
            self.assertEqual(a.sync_workflow(self.store)["reviews"], 1)
        path.write_text(path.read_text(encoding="utf-8").replace('| F1 | PRIVATE-DESCRIPTION | 중간 | 채택 |\n', ''), encoding="utf-8")
        self.assertEqual(a.sync_workflow(self.store)["reviews"], 1)
        self.assertEqual(a.sync_workflow(self.store)["reviews"], 0)
        self.assertEqual([r["status"] for r in a.report(self.store)["reviews"]], ["accepted", "followup", "accepted", "removed"])

    def test_overlapping_questions_do_not_double_count_waiting_time(self):
        for wait_id in ("q1", "q2"):
            self.store.emit("wait", {"wait_id": wait_id, "status": "started"}, session="s1", ts="2026-01-01T00:00:00Z")
            self.store.emit("wait", {"wait_id": wait_id, "status": "ended"}, session="s1", ts="2026-01-01T00:00:05Z")
        self.assertEqual(a.report(self.store)["reported_wait_ms"], 5000)

    def test_generic_approve_answer_is_not_classified_as_permission_approval(self):
        a.transcript(self.store, self.question_transcript(answer="approve", answered_at="2026-01-01T00:00:12Z"), "codex")
        self.assertEqual(a.report(self.store)["interventions"][0]["action"], "answer")

    def test_bad_review_registry_is_visible_without_stopping_session_sync(self):
        (self.store.path / "reviews.json").write_text('[]')
        state = a.sync(self.store)
        self.assertIn("review-registry", state["errors"])
        self.assertTrue((self.store.path / "sync.json").is_file())

    def test_bad_reply_item_does_not_discard_later_valid_answer(self):
        path = self.question_transcript(answered_at="2026-01-01T00:00:12Z")
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        envelope = rows[-1]["payload"]["content"][0]
        text = envelope["text"]
        begin, end = '<send_user_message_question_reply>', '</send_user_message_question_reply>'
        items = json.loads(text[len(begin):-len(end)])
        items.insert(0, {"questionItemId": [], "answer": "invalid"})
        envelope["text"] = begin + json.dumps(items) + end
        path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
        a.transcript(self.store, path, "codex")
        self.assertEqual(len(a.report(self.store)["interventions"]), 1)
        self.assertEqual(a.report(self.store)["open_waits"], 0)

    def test_workflow_backfill_preserves_original_time_but_indexes_observation(self):
        row = self.store.emit("wait", {"wait_id": "old", "status": "started"}, session="s1", ts="2026-01-01T00:00:00Z")
        data = a.otlp(self.store, [row])["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
        self.assertEqual(data["timeUnixNano"], data["observedTimeUnixNano"])
        attrs = {x["key"]: x["value"] for x in data["attributes"]}
        self.assertEqual(attrs["harness.evidence.ts"]["stringValue"], row["ts"])

    def test_selected_replay_does_not_skip_unsent_rows_or_rewind_cursor(self):
        batches = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                batches.append(sum(len(s["logRecords"]) for r in body["resourceLogs"] for s in r["scopeLogs"]))
                self.send_response(200); self.end_headers(); self.wfile.write(b'{}')
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        endpoint = f"http://127.0.0.1:{server.server_port}"
        first = self.store.records()[0]["id"]
        self.assertEqual(a.push(self.store, endpoint)["offset"], 1)
        self.store.emit("prompt", {"length": 5}, session="s1")
        replay = a.push(self.store, endpoint, event_ids=[first])
        self.assertEqual(replay["offset"], 1)
        self.assertEqual(replay["pending"], 1)
        self.assertEqual(a.push(self.store, endpoint)["offset"], 2)
        self.assertEqual(batches, [1, 1, 1])


if __name__ == "__main__":
    unittest.main()
