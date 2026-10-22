"""Event collection, notifier preservation and windowless launches without live services."""
import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("agentops_triggers", Path(__file__).resolve().parents[1] / "core/metrics/agentops.py")
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


class TriggerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = SimpleNamespace(root=self.root, path=self.root / "state", records=Mock(return_value=[]))
        self.store.path.mkdir()

    def test_removed_periodic_command_is_rejected_before_opening_store(self):
        with patch.object(a, "Store") as store, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            a.main(["watch", "--interval", "30"])
        self.assertEqual(error.exception.code, 2)
        store.assert_not_called()
        self.assertFalse(hasattr(a, "watch"))

    def test_help_keeps_event_and_manual_commands_without_periodic_options(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as error:
            a.main(["--help"])
        self.assertEqual(error.exception.code, 0)
        for command in ("collect", "notify", "sync", "push"):
            self.assertIn(command, output.getvalue())
        self.assertNotIn("watch", output.getvalue())
        self.assertNotIn("--interval", output.getvalue())

    def test_historical_heartbeat_records_remain_readable(self):
        record = {"id": "old-heartbeat", "schema_version": 1, "ts": "2026-01-01T00:00:00Z",
                  "task": "TEST-1", "session": "s1", "tool": "codex", "source": "observed",
                  "kind": "heartbeat", "status": "watching", "interval_sec": 30}
        self.assertEqual(a.validate(record), record)

    def test_unchanged_does_not_contact_transport_or_emit_heartbeat(self):
        with patch.object(a, "sync", return_value={"imported": 0}), patch.object(a, "push") as push:
            self.assertEqual(a.collect_once(self.store)["status"], "unchanged")
            push.assert_not_called()
        self.assertEqual(self.store.records.call_count, 1)
        self.assertFalse((self.store.path / "events.jsonl").exists())

    def test_failed_transfer_retries_on_next_event_without_timer(self):
        self.store.records.return_value = [{"id": "one"}]
        with patch.object(a, "sync", return_value={"imported": 0}), \
                patch.object(a, "push", side_effect=[a.EvidenceError("missing endpoint"), {"status": "delivered", "pending": 0}]) as push:
            self.assertEqual(a.collect_once(self.store, "turn")["status"], "unavailable")
            self.assertEqual(a.collect_once(self.store, "pr")["status"], "delivered")
            self.assertEqual(push.call_count, 2)

    def test_concurrent_collection_does_not_start_second_sync(self):
        with a.lock(self.store.path / "collection.json"), patch.object(a, "sync") as sync:
            self.assertEqual(a.collect_once(self.store), {"status": "busy"})
            sync.assert_not_called()

    def test_sync_error_is_not_misreported_as_busy(self):
        with patch.object(a, "sync", side_effect=a.EvidenceError("invalid source")):
            with self.assertRaises(a.EvidenceError):
                a.collect_once(self.store)

    def test_completion_keeps_existing_notifier_and_discards_message_content(self):
        raw = json.dumps({"type": "agent-turn-complete", "cwd": str(self.root),
                          "input-messages": ["PRIVATE PROMPT"], "last-assistant-message": "PRIVATE RESPONSE"})
        with patch.object(a, "launch_hidden") as forward, patch.object(a, "git", return_value=str(self.root)), \
                patch.object(a, "Store", return_value=self.store), patch.object(a, "collect_once") as collect:
            self.assertEqual(a.codex_notify(self.root, raw, '["existing-notifier", "turn-ended"]'), 0)
            forward.assert_called_once_with(["existing-notifier", "turn-ended", raw])
            collect.assert_called_once_with(self.store, "turn")
        self.assertEqual(list(self.store.path.iterdir()), [])

    def test_other_events_foreign_worktrees_and_nested_repos_are_ignored(self):
        for payload, top in (({"type": "approval-requested", "cwd": str(self.root)}, str(self.root)),
                             ({"type": "agent-turn-complete", "cwd": str(self.root.parent)}, str(self.root.parent)),
                             ({"type": "agent-turn-complete", "cwd": str(self.root / "nested")}, str(self.root / "nested"))):
            with self.subTest(payload=payload), patch.object(a, "git", return_value=top), patch.object(a, "collect_once") as collect:
                self.assertEqual(a.codex_notify(self.root, json.dumps(payload)), 0)
                collect.assert_not_called()

    def test_bad_payload_and_broken_existing_notifier_do_not_block_coding(self):
        with patch.object(a, "launch_hidden", side_effect=OSError("not available")):
            self.assertEqual(a.codex_notify(self.root, "bad json", '["missing"]'), 0)

    def test_claude_stop_collects_only_after_explicit_install(self):
        self.store.emit = Mock()
        with patch.object(a, "launch_hidden") as launch, patch.object(a, "hidden_python", return_value="pythonw"):
            a.hook(self.store, {"hook_event_name": "Stop"})
            launch.assert_not_called()
            a.atomic_json(self.store.path / "event-collection.json", {"enabled": True})
            a.hook(self.store, {"hook_event_name": "Stop"})
            self.assertEqual(launch.call_args.args[0][-3:], ["collect", "--reason", "turn"])

    def test_pr_summary_collects_when_enabled_and_reports_transfer_failure(self):
        summary_spec = importlib.util.spec_from_file_location("trigger_summary", Path(a.__file__).with_name("summary.py"))
        summary = importlib.util.module_from_spec(summary_spec)
        summary_spec.loader.exec_module(summary)
        path = self.store.path / "event-collection.json"
        common = SimpleNamespace(git_state_path=Mock(return_value=path))
        evidence = SimpleNamespace(Store=Mock(return_value=self.store), collect_once=Mock(return_value={"status": "error"}))
        warn = Mock()
        with patch.object(summary, "load_module", return_value=evidence) as load:
            summary.prepare_evidence(common, self.root, self.root, warn)
            load.assert_not_called()
            a.atomic_json(path, {"enabled": True})
            summary.prepare_evidence(common, self.root, self.root, warn)
            evidence.collect_once.assert_called_once_with(self.store, "pr")
            warn.assert_called_once()

    def test_installer_preserves_existing_notify_other_config_and_is_idempotent(self):
        import tomllib
        (self.root / ".harness").mkdir()
        (self.root / ".harness/agentops.py").write_text("# installed")
        path = self.root / "config.toml"
        original = '# comment\nnotify = [\n "previous", "turn-ended",\n]\n[features]\nkeep = true\n'
        path.write_text(original)
        with patch.object(a, "hidden_python", return_value="pythonw.exe"):
            a.install_codex_notify(self.store, path)
            first = path.read_text()
            self.assertTrue(a.install_codex_notify(self.store, path)["unchanged"])
        config = tomllib.loads(first)
        self.assertTrue(config["features"]["keep"])
        self.assertEqual(json.loads(config["notify"][-1]), ["previous", "turn-ended"])
        self.assertEqual(path.with_name("config.toml.before-agentops").read_text(), original)
        self.assertEqual(path.read_text(), first)
        self.assertIn("# comment", first)

    def test_background_launch_uses_windowless_detached_flags(self):
        with patch.object(a.os, "name", "nt"), \
                patch.object(a.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
                patch.object(a.subprocess, "DETACHED_PROCESS", 0x00000008, create=True), \
                patch.object(a.subprocess, "Popen") as spawn:
            a.launch_hidden(["collector"])
            options = spawn.call_args.kwargs
            self.assertEqual(options["creationflags"], 0x08000008)
            for stream in ("stdin", "stdout", "stderr"):
                self.assertEqual(options[stream], subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
