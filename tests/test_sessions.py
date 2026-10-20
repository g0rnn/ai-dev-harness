"""#96 M5-1 세션 단위 실행 기록(`sessions.jsonl`)과 `harness sessions summary`."""

import datetime as dt
import importlib.util
import io
import json
import os
import subprocess
import sys
import unittest
from unittest import mock
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_hooks import HOOKS, SessionBaselineCase, git, git_path  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("harness_sessions_test", ROOT / "bin" / "harness.py")
harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)
collect = harness.metrics_collect


def run_cli(args, cwd=None):
    out, err = io.StringIO(), io.StringIO()
    old = os.getcwd()
    try:
        if cwd:
            os.chdir(cwd)
        with redirect_stdout(out), redirect_stderr(err):
            code = harness.main(args)
    finally:
        os.chdir(old)
    return code, out.getvalue(), err.getvalue()


class SessionRecordCase(SessionBaselineCase):
    def setUp(self):
        super().setUp()
        # 요약이 이 PC의 실제 Codex 로그(~/.codex)를 읽지 않게 빈 위치를 준다(#116 리뷰 F7)
        patcher = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.tmp / "no-codex")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def records(self, project=None):
        path = git_path(project or self.project, "sessions.jsonl")
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def end(self, reason="prompt_input_exit", session="s1"):
        return self.hook("session-end.py", {"hook_event_name": "SessionEnd", "session_id": session,
                                            "reason": reason, "cwd": str(self.project)})


class SessionRecordTest(SessionRecordCase):
    def test_start_writes_record(self):
        """T1"""
        self.init_git()
        result = self.hook("session-start.py", {"hook_event_name": "SessionStart", "session_id": "s1",
                                                "source": "startup", "model": "claude-opus-5-5",
                                                "transcript_path": "C:/t/s1.jsonl", "cwd": str(self.project)})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        [record] = self.records()
        self.assertEqual(record["event"], "start")
        self.assertEqual(record["session"], "s1")
        self.assertEqual(record["source"], "startup")
        self.assertEqual(record["model"], "claude-opus-5-5")
        self.assertEqual(record["transcript"], "C:/t/s1.jsonl")
        self.assertEqual(record["tool"], "claude-code")
        self.assertEqual(record["head"], self.head())
        self.assertIn("branch", record)
        self.assertIn("ts", record)

    def test_model_object_uses_id(self):
        self.init_git()
        self.hook("session-start.py", {"session_id": "s1", "model": {"id": "claude-sonnet-5-5"}})
        self.assertEqual(self.records()[0]["model"], "claude-sonnet-5-5")

    def test_resume_adds_start_keeps_baseline(self):
        """T2"""
        self.init_git()
        self.start(source="startup")
        first = self.baselines()["s1"]["head"]
        self.commit("a.txt")
        self.start(source="resume")
        self.assertEqual([r["source"] for r in self.records()], ["startup", "resume"])
        self.assertEqual(self.baselines()["s1"]["head"], first)

    def test_stop_records_outcomes(self):
        """T3: 기록이 있어도 종료 코드는 기존과 같다"""
        self.setup_repo({"hooks": {"stop_verify": [self.marker_cmd("root", 0)]}})
        self.assertEqual(self.stop().returncode, 0)  # 변경 없음
        (self.project / "x.txt").write_text("1", encoding="utf-8")
        self.assertEqual(self.stop().returncode, 0)  # 통과
        self.assertEqual(self.stop().returncode, 0)  # 같은 상태
        self.write_config({"hooks": {"stop_verify": [self.marker_cmd("root", 1)]}})
        self.assertEqual(self.stop().returncode, 2)  # 실패
        self.assertEqual(self.stop(active=True).returncode, 0)  # 같은 상태 재호출
        stops = [r for r in self.records() if r["event"] == "stop"]
        self.assertEqual([r["verify"] for r in stops], ["none", "pass", "cached-pass", "fail", "cached-fail"])
        self.assertEqual([r["retry"] for r in stops], [False, False, False, False, True])
        self.assertEqual((stops[0]["files"], stops[1]["files"]), (0, 1))

    def test_config_error_records_error(self):
        """T4"""
        self.init_git()
        (self.project / "harness.json").write_text("{", encoding="utf-8")
        result = self.stop()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.records()[-1]["verify"], "error")

    def test_end_writes_record(self):
        """T5"""
        self.init_git()
        result = self.end()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        [record] = self.records()
        self.assertEqual((record["event"], record["reason"], record["session"]), ("end", "prompt_input_exit", "s1"))

    def test_end_survives_bad_input(self):
        self.init_git()
        result = self.hook("session-end.py", None, raw="{not json")
        self.assertEqual((result.returncode, result.stdout), (0, b""))

    def test_unwritable_log_does_not_change_hooks(self):
        """T6: 기록 파일이 디렉터리여도 종료 코드·출력이 같다"""
        self.setup_repo({"hooks": {"stop_verify": [self.marker_cmd("root", 1)]}})
        git_path(self.project, "sessions.jsonl").mkdir(parents=True)
        start = self.start()
        self.assertEqual((start.returncode, start.stdout), (0, b""))
        self.assertIn("s1", self.baselines())  # 기준선은 그대로 남는다
        (self.project / "x.txt").write_text("1", encoding="utf-8")
        stop = self.stop()
        self.assertEqual(stop.returncode, 2)
        self.assertIn("세션 종료 전 검증이 실패했다", stop.stderr.decode("utf-8"))
        self.assertEqual(self.end().returncode, 0)

    def test_not_git_repo(self):
        """T7"""
        self.write_config({"hooks": {"stop_verify": [self.marker_cmd("root", 1)]}})
        self.assertEqual(self.start().returncode, 0)
        self.assertEqual(self.stop().returncode, 0)
        self.assertEqual(self.end().returncode, 0)
        self.assertFalse((self.project / ".git").exists())

    def test_concurrent_appends(self):
        """T12: 두 프로세스가 동시에 써도 줄이 섞이지 않는다"""
        self.init_git()
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import harness_common as c; from pathlib import Path\n"
                "for i in range(60): c.log_session(Path(sys.argv[2]), 'stop', sys.argv[3], verify='pass')")
        procs = [subprocess.Popen([sys.executable, "-c", code, str(HOOKS), str(self.project), name],
                                  env={**os.environ, "GIT_CEILING_DIRECTORIES": str(self.tmp)})
                 for name in ("a", "b")]
        for proc in procs:
            self.assertEqual(proc.wait(timeout=120), 0)
        records = self.records()
        self.assertEqual(len(records), 120)
        self.assertEqual(sorted({r["session"] for r in records}), ["a", "b"])


def line(session, ts, event="stop", branch="feat/x", **extra):
    return json.dumps({"ts": ts, "branch": branch, "event": event, "session": session, "tool": "claude-code",
                       **extra})


class SessionSummaryTest(unittest.TestCase):
    def test_counts_by_branch(self):
        """T8"""
        lines = [
            line("a", "2026-10-20T01:00:00+00:00", "start"),
            line("a", "2026-10-20T01:10:00+00:00", verify="pass"),
            line("a", "2026-10-20T01:30:00+00:00", verify="fail"),
            line("a", "2026-10-20T01:31:00+00:00", "end"),
            line("b", "2026-10-20T02:00:00+00:00", "start"),
            line("b", "2026-10-20T02:05:00+00:00", verify="cached-fail"),
            line("c", "2026-10-20T03:00:00+00:00", "start", branch="main"),
            line("c", "2026-10-20T03:30:00+00:00", branch="main", verify="pass"),
            "{broken", "[]", "",
            json.dumps({"ts": "2026-10-20T04:00:00+00:00", "branch": "feat/x", "event": "stop"}),  # 세션 없음
        ]
        s = collect.summarize_sessions(lines, "feat/x")
        self.assertEqual((s["sessions"], s["turns"], s["active_sec"]), (2, 3, 31 * 60 + 5 * 60))
        self.assertEqual((s["verify"]["pass"], s["verify"]["fail"], s["verify"]["cached-fail"]), (1, 1, 1))
        self.assertEqual(s["tools"], {"claude-code": 2})
        self.assertEqual(s["malformed"], 2)
        text = collect.sessions_text(s, 1, 3)
        self.assertIn("- 세션: 2 (claude-code 2)", text)
        self.assertIn("- 응답: 3, 작업 시간: 36분", text)
        self.assertIn("통과 1 · 실패 1 · 같은 상태 재사용 1", text)
        self.assertIn("- 기록 없는 커밋: 1/3", text)
        self.assertNotIn("기록 없는 커밋", collect.sessions_text(s, None, None))

    def test_branch_created_mid_session(self):
        """리뷰 A F1: main에서 시작한 세션이 새 브랜치에서 첫 응답에 커밋해도 기록된 커밋이다"""
        lines = [line("a", "2026-10-20T01:00:00+00:00", "start", branch="main"),
                 line("a", "2026-10-20T01:20:00+00:00", verify="pass")]
        s = collect.summarize_sessions(lines, "feat/x")
        self.assertEqual((s["sessions"], s["turns"], s["active_sec"]), (1, 1, 20 * 60))
        commit = ("c1", dt.datetime(2026, 10, 20, 1, 15, tzinfo=dt.timezone.utc))
        self.assertEqual(collect.unrecorded_commits([commit], s["intervals"]), [])

    def test_resume_gap_splits_segments(self):
        """리뷰 A F2: resume 전 공백은 작업 시간에 넣지 않고, 그 사이 커밋은 기록 없는 커밋이다"""
        lines = [line("a", "2026-10-20T01:00:00+00:00", "start"),
                 line("a", "2026-10-20T02:00:00+00:00", verify="pass"),  # 한 시간짜리 응답은 이어진다
                 line("a", "2026-10-22T01:00:00+00:00", "start", source="resume"),
                 line("a", "2026-10-22T02:00:00+00:00", verify="pass"),
                 line("a", "2026-10-22T05:00:00+00:00", verify="pass")]  # 3시간 공백은 끊긴다
        s = collect.summarize_sessions(lines, "feat/x")
        self.assertEqual(s["active_sec"], 2 * 3600)
        self.assertEqual(len(s["intervals"]), 3)
        gap = ("gap", dt.datetime(2026, 10, 21, 12, 0, tzinfo=dt.timezone.utc))
        self.assertEqual(collect.unrecorded_commits([gap], s["intervals"]), ["gap"])

    def test_bad_values_count_as_malformed(self):
        """리뷰 B F2: JSON이지만 값이 틀린 줄은 예외 없이 깨진 줄로 센다"""
        lines = [line("a", "2026-13-20T01:00:00+00:00"), line("a", "2026-10-20T01:00:00+99:99"),
                 line("a", "2026-10-20T01:00:00+00:00", verify=["pass"]),
                 line("a", "2026-10-20T01:05:00+00:00", verify={"x": 1}), json.dumps({"session": "a", "ts": 5})]
        s = collect.summarize_sessions(lines, "feat/x")
        self.assertEqual((s["malformed"], s["sessions"], s["turns"]), (2, 1, 2))
        self.assertEqual(sum(s["verify"].values()), 0)

    def test_active_time_goes_to_branch_of_later_event(self):
        """리뷰 B F4: 한 세션이 feat/a에서 3시간, feat/b에서 1시간 일하면 각각 그만큼"""
        lines = [line("a", "2026-10-20T01:00:00+00:00", "start", branch="feat/a"),
                 line("a", "2026-10-20T02:00:00+00:00", branch="feat/a"),
                 line("a", "2026-10-20T03:00:00+00:00", branch="feat/a"),
                 line("a", "2026-10-20T04:00:00+00:00", branch="feat/a"),
                 line("a", "2026-10-20T05:00:00+00:00", branch="feat/b")]
        self.assertEqual(collect.summarize_sessions(lines, "feat/a")["active_sec"], 3 * 3600)
        self.assertEqual(collect.summarize_sessions(lines, "feat/b")["active_sec"], 3600)

    def test_unrecorded_commits(self):
        """T9: 세션 구간 + 10분 안의 커밋만 기록된 것으로 본다"""
        utc = dt.timezone.utc
        intervals = [(dt.datetime(2026, 10, 20, 1, 0, tzinfo=utc), dt.datetime(2026, 10, 20, 1, 30, tzinfo=utc))]
        commits = [("in", dt.datetime(2026, 10, 20, 1, 15, tzinfo=utc)),
                   ("grace", dt.datetime(2026, 10, 20, 1, 39, tzinfo=utc)),
                   ("late", dt.datetime(2026, 10, 20, 1, 41, tzinfo=utc)),
                   ("kst", dt.datetime(2026, 10, 20, 10, 20, tzinfo=dt.timezone(dt.timedelta(hours=9))))]  # 01:20 UTC
        self.assertEqual(collect.unrecorded_commits(commits, intervals), ["late"])
        self.assertEqual(collect.format_duration(3725), "1시간 2분")


class SessionsCliTest(SessionRecordCase):
    def test_summary_cli(self):
        """T10: 실제 hook 기록과 커밋으로 요약한다"""
        self.init_git()
        git(self.project, "branch", "-M", "main")
        git(self.project, "checkout", "-q", "-b", "feat/x")
        self.start()
        self.commit("a.txt")
        self.stop()
        code, out, err = run_cli(["sessions", "summary", str(self.project), "--base", "main"])
        self.assertEqual(code, 0, err)
        self.assertIn("세션 기록 (브랜치 feat/x)", out)
        self.assertIn("- 세션: 1 (claude-code 1)", out)
        self.assertIn("- 기록 없는 커밋: 0/1", out)

    def test_summary_without_records(self):
        self.init_git()
        code, out, err = run_cli(["sessions", "summary", str(self.project), "--base", "HEAD"])
        self.assertEqual(code, 0, err)
        self.assertIn("- 세션: 0 (없음)", out)
        self.assertIn("- 기록 없는 커밋: 0/0", out)

    def test_session_in_main_checkout_commit_in_worktree(self):
        """리뷰 B F1: 기본 checkout에서 띄운 세션이 linked worktree에서 커밋해도 기록 없는 커밋이 아니다"""
        self.init_git()
        git(self.project, "branch", "-M", "main")
        worktree = self.tmp / "wt"
        git(self.project, "worktree", "add", "-q", "-b", "feat/x", str(worktree))
        self.start()  # 세션 기록은 기본 checkout(main)에 쌓인다
        self.commit("a.txt", project=worktree)
        self.stop()
        code, out, err = run_cli(["sessions", "summary", str(worktree), "--base", "main"])
        self.assertEqual(code, 0, err)
        self.assertIn("- 기록 없는 커밋: 0/1", out)

    def test_broken_config_still_summarizes(self):
        """리뷰 B F3"""
        self.init_git()
        (self.project / "harness.json").write_text("{", encoding="utf-8")
        code, out, err = run_cli(["sessions", "summary", str(self.project)])
        self.assertEqual(code, 0, err)
        self.assertIn("세션 기록", out)
        code, out, err = run_cli(["sessions", "summary", str(self.project), "--base", "HEAD"])
        self.assertEqual(code, 0, err)
        self.assertIn("- 기록 없는 커밋: 0/0", out)

    def test_missing_base_skips_commit_check(self):
        self.init_git()
        code, out, err = run_cli(["sessions", "summary", str(self.project), "--base", "origin/nope"])
        self.assertEqual(code, 0)
        self.assertNotIn("기록 없는 커밋", out)
        self.assertIn("기록 없는 커밋은 세지 않았다", err)


if __name__ == "__main__":
    unittest.main()
