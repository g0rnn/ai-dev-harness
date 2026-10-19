"""#97 M5-2 세션 토큰 사용량과 추정 비용."""

import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_hooks import common  # noqa: E402
from test_sessions import SessionRecordCase, collect, harness, line, run_cli  # noqa: E402


def assistant(message_id, model="claude-opus-5-5", input_=0, output=0, write=0, read=0, split=None, **extra):
    usage = {"input_tokens": input_, "output_tokens": output, "cache_creation_input_tokens": write,
             "cache_read_input_tokens": read}
    if split is not None:
        usage["cache_creation"] = {"ephemeral_5m_input_tokens": split[0], "ephemeral_1h_input_tokens": split[1]}
    return json.dumps({"type": "assistant", "uuid": f"u-{message_id}",
                       "message": {"id": message_id, "model": model, "usage": usage}, **extra})


def write_lines(path: Path, lines) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class UsageTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        from tempdirs import remove_tree
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-usage-"))
        self.addCleanup(remove_tree, self.tmp)

    def test_sums_by_model_dedup(self):
        """T1: 같은 id는 한 번(마지막 값), 모델별 합"""
        path = write_lines(self.tmp / "s.jsonl", [
            json.dumps({"type": "user", "message": {"content": "usage 단어가 있어도 사용자 줄"}}),
            assistant("m1", input_=10, output=1),
            assistant("m1", input_=10, output=5),  # 스트리밍 반복: 마지막 값
            assistant("m2", model="claude-sonnet-5-5", input_=3, output=4, read=100),
            json.dumps({"type": "system", "usage": {"input_tokens": 999}}),
        ])
        usage, error = common.transcript_usage(str(path))
        self.assertIsNone(error)
        self.assertEqual(usage["claude-opus-5-5"], {"input": 10, "output": 5, "cache_write_5m": 0,
                                                    "cache_write_1h": 0, "cache_read": 0})
        self.assertEqual(usage["claude-sonnet-5-5"]["cache_read"], 100)

    def test_includes_subagents(self):
        """T2"""
        path = write_lines(self.tmp / "s.jsonl", [assistant("m1", output=5)])
        write_lines(self.tmp / "s" / "subagents" / "agent-a.jsonl", [assistant("a1", output=7, isSidechain=True)])
        usage, _ = common.transcript_usage(str(path))
        self.assertEqual(usage["claude-opus-5-5"]["output"], 12)

    def test_cache_ttl_split(self):
        """T3: 5분·1시간 분리, 분리 없으면 5분"""
        path = write_lines(self.tmp / "s.jsonl", [assistant("m1", write=30, split=(10, 20)), assistant("m2", write=7)])
        usage, _ = common.transcript_usage(str(path))
        self.assertEqual((usage["claude-opus-5-5"]["cache_write_5m"], usage["claude-opus-5-5"]["cache_write_1h"]),
                         (17, 20))

    def test_bad_inputs(self):
        """T4"""
        self.assertEqual(common.transcript_usage(None), (None, "no_transcript"))
        self.assertEqual(common.transcript_usage(str(self.tmp / "none.jsonl")), (None, "not_found"))
        self.assertEqual(common.transcript_usage(str(self.tmp)), (None, "not_found"))
        txt = write_lines(self.tmp / "s.txt", [assistant("m1", output=1)])
        self.assertEqual(common.transcript_usage(str(txt)), (None, "not_found"))
        path = write_lines(self.tmp / "b.jsonl", [
            "{broken usage", json.dumps(["usage"]), json.dumps({"type": "assistant", "message": "usage"}),
            json.dumps({"type": "assistant", "message": {"id": "x", "usage": {"input_tokens": "9", "output_tokens": -1,
                                                                               "cache_read_input_tokens": True}}})])
        usage, error = common.transcript_usage(str(path))
        self.assertIsNone(error)
        self.assertEqual(usage, {"unknown": {"input": 0, "output": 0, "cache_write_5m": 0, "cache_write_1h": 0,
                                             "cache_read": 0}})

    def test_real_transcript_fixture(self):
        """리뷰 B F4: 실제 transcript에서 내용만 지운 fixture(날짜 붙은 모델 ID, 포크 서브에이전트, usage 단어가 든 사용자 줄)"""
        fixtures = Path(__file__).resolve().parent / "fixtures" / "usage"
        expected = json.loads((fixtures / "expected.json").read_text(encoding="utf-8"))
        self.assertEqual(common.transcript_usage(str(fixtures / "session.jsonl")), (expected["usage"], None))
        self.assertIn("claude-haiku-4-5-20251001", expected["usage"])
        cost, unpriced = collect.estimate_cost(expected["usage"], collect.merged_pricing({}))
        self.assertEqual(unpriced, [])
        self.assertGreater(cost, 0)

    def test_oversized_subagent_is_skipped_not_whole_session(self):
        """리뷰 B F1: 서브에이전트 파일 하나가 상한을 넘으면 그 파일만 빼고 사유를 남긴다"""
        from unittest import mock
        path = write_lines(self.tmp / "s.jsonl", [assistant("m1", output=5)])
        write_lines(self.tmp / "s" / "subagents" / "agent-a.jsonl", [assistant("a1", output=7)])
        big = write_lines(self.tmp / "s" / "subagents" / "agent-b.jsonl",
                          [assistant(f"b{i}", output=9) for i in range(50)])  # 메인·agent-a보다 크다
        with mock.patch.object(common, "TRANSCRIPT_MAX_BYTES", big.stat().st_size - 1):
            usage, error = common.transcript_usage(str(path))
        self.assertEqual((usage["claude-opus-5-5"]["output"], error), (12, "too_large_subagent"))
        with mock.patch.object(common, "TRANSCRIPT_MAX_BYTES", 1):
            self.assertEqual(common.transcript_usage(str(path)), (None, "too_large"))

    def test_large_transcript_time(self):
        """T11: 20MB 합성 transcript를 2초 안에(느린 PC 여유)"""
        filler = json.dumps({"type": "user", "message": {"content": "x" * 2000}})
        lines = []
        for i in range(10_000):
            lines += [filler, assistant(f"m{i}", output=1)]
        path = write_lines(self.tmp / "big.jsonl", lines)
        self.assertGreater(path.stat().st_size, 20_000_000)
        started = time.perf_counter()
        usage, _ = common.transcript_usage(str(path))
        self.assertLess(time.perf_counter() - started, 2.0)
        self.assertEqual(usage["claude-opus-5-5"]["output"], 10_000)


class UsageHookTest(SessionRecordCase):
    def transcript(self, lines):
        return write_lines(self.tmp / "t" / "s1.jsonl", lines)

    def stop_with(self, path, active=False):
        return self.hook("stop-verify.py", {"hook_event_name": "Stop", "stop_hook_active": active, "session_id": "s1",
                                            "transcript_path": str(path), "cwd": str(self.project)})

    def test_stop_records_cumulative_usage(self):
        """T5: stop마다 누적값, 요약은 마지막 누적값 한 번"""
        self.init_git()
        path = self.transcript([assistant("m1", output=10)])
        self.stop_with(path)
        path = self.transcript([assistant("m1", output=10), assistant("m2", output=5)])
        self.stop_with(path)
        self.stop_with(path, active=True)
        stops = [r for r in self.records() if r["event"] == "stop"]
        self.assertEqual([r["usage"]["claude-opus-5-5"]["output"] for r in stops], [10, 15, 15])
        lines = git_log(self)
        summary = collect.summarize_sessions(lines, stops[0]["branch"])
        self.assertEqual(summary["usage"]["claude-opus-5-5"]["output"], 15)

    def test_end_records_usage(self):
        """T6"""
        self.init_git()
        path = self.transcript([assistant("m1", output=3)])
        self.hook("session-end.py", {"session_id": "s1", "reason": "other", "transcript_path": str(path)})
        [record] = self.records()
        self.assertEqual(record["usage"]["claude-opus-5-5"]["output"], 3)

    def test_usage_failure_keeps_hook_result(self):
        """T7: transcript 경로가 디렉터리여도 종료 코드·출력이 같다"""
        self.setup_repo({"hooks": {"stop_verify": [self.marker_cmd("root", 1)]}})
        (self.project / "x.txt").write_text("1", encoding="utf-8")
        result = self.stop_with(self.tmp)
        self.assertEqual(result.returncode, 2)
        self.assertIn("세션 종료 전 검증이 실패했다", result.stderr.decode("utf-8"))
        record = self.records()[-1]
        self.assertEqual(record["usage_error"], "not_found")
        self.assertNotIn("usage", record)

    def test_usage_exception_records_error(self):
        """리뷰 A F3: 계산 중 예외는 `usage_error: error`로 남고 기록은 계속된다"""
        from unittest import mock
        self.init_git()
        with mock.patch.object(common, "transcript_usage", side_effect=RuntimeError("boom")):
            common.log_session_usage(self.project, "stop", {"session_id": "s1"}, verify="none")
        record = self.records()[-1]
        self.assertEqual((record["event"], record["usage_error"]), ("stop", "error"))


def git_log(case):
    from test_hooks import git_path
    return git_path(case.project, "sessions.jsonl").read_text(encoding="utf-8").splitlines()


def usage_line(session, ts, output, branch="feat/x", model="claude-opus-5-5", event="stop"):
    counts = {"input": 0, "output": output, "cache_write_5m": 0, "cache_write_1h": 0, "cache_read": 0}
    return line(session, ts, event, branch=branch, usage={model: counts})


class PricingTest(unittest.TestCase):
    def test_estimate_with_defaults_and_override(self):
        """T8: 같은 통화면 모델 단위로 덮어쓰고, 다른 통화면 키트 단가를 섞지 않는다(리뷰 A F1·B F3)"""
        usage = {"claude-opus-5-5": {"input": 1_000_000, "output": 1_000_000, "cache_write_5m": 1_000_000,
                                     "cache_write_1h": 1_000_000, "cache_read": 1_000_000},
                 "my-model": {"input": 5, "output": 0, "cache_write_5m": 0, "cache_write_1h": 0, "cache_read": 0}}
        cost, unpriced = collect.estimate_cost(usage, collect.merged_pricing({}))
        self.assertAlmostEqual(cost, 4 + 20 + 5 + 8 + 0.2)
        self.assertEqual(unpriced, ["my-model"])
        mine = {"my-model": {"input": 1_000_000, "output": 0, "cache_write_5m": 0, "cache_write_1h": 0, "cache_read": 0}}
        pricing = collect.merged_pricing({"pricing": {"models": mine}})
        cost, unpriced = collect.estimate_cost(usage, pricing)
        self.assertAlmostEqual(cost, 37.2 + 5)
        self.assertEqual((unpriced, pricing["currency"], pricing["as_of"]), ([], "USD", "2026-10-06, 설정 1개 모델"))
        krw = collect.merged_pricing({"pricing": {"currency": "KRW", "as_of": "2026-11-01", "models": mine}})
        cost, unpriced = collect.estimate_cost(usage, krw)
        self.assertAlmostEqual(cost, 5)  # 키트 USD 단가(Opus)를 섞지 않는다
        self.assertEqual((unpriced, krw["as_of"]), (["claude-opus-5-5"], "2026-11-01"))

    def test_dated_model_uses_base_price(self):
        """리뷰 B F2: 실제 transcript의 날짜 붙은 ID"""
        usage = {"claude-haiku-4-5-20251001": {"input": 1_000_000, "output": 0, "cache_write_5m": 0,
                                               "cache_write_1h": 0, "cache_read": 0}}
        self.assertEqual(collect.estimate_cost(usage, collect.merged_pricing({})), (1.0, []))

    def test_config_validation(self):
        """T9"""
        base = {"project_name": "p", "platform": "github", "tracker": "github",
                "default_branch": "main", "integration_branch": "main"}
        prices = {"input": 1, "output": 2, "cache_write_5m": 0, "cache_write_1h": 0, "cache_read": 0.1}
        harness.validate_config({**base, "pricing": {"currency": "USD", "as_of": "2026-11-01",
                                                     "models": {"m": prices}}}, "x")
        harness.validate_config({**base, "pricing": {"models": {"m": prices}}}, "x")
        for bad in ([], {"models": []}, {"bogus": 1}, {"currency": ""}, {"currency": "KRW"}, {"as_of": "2026-11-01"},
                    {"currency": "@here", "models": {"m": prices}}, {"currency": "USD\n", "models": {"m": prices}},
                    {"models": {"m": {"input": -1, "output": 0, "cache_write_5m": 0, "cache_write_1h": 0, "cache_read": 0}}},
                    {"models": {"m": {"input": "1"}}}):
            with self.subTest(bad=bad), self.assertRaises(harness.HarnessError):
                harness.validate_config({**base, "pricing": bad}, "x")

    def test_summary_reports_usage_errors(self):
        """리뷰 B F1: 다 읽지 못한 세션이 있으면 합이 일부라고 알리고, 전부 오류면 0과 구분한다"""
        err = json.loads(line("b", "2026-10-20T02:00:00+00:00"))
        err["usage_error"] = "too_large"
        lines = [usage_line("a", "2026-10-20T01:00:00+00:00", 10), json.dumps(err)]
        s = collect.summarize_sessions(lines, "feat/x")
        self.assertEqual(s["usage_errors"], {"b": ["too_large"]})
        text = collect.sessions_text(s, None, None)
        self.assertIn("- 토큰을 다 읽지 못한 세션: 1 (too_large). 아래 합은 일부다", text)
        only_errors = collect.sessions_text(collect.summarize_sessions([json.dumps(err)], "feat/x"), None, None)
        self.assertIn("토큰을 다 읽지 못한 세션: 1", only_errors)
        self.assertIn("- 토큰: 기록 없음", only_errors)

    def test_partial_snapshot_is_not_a_reset(self):
        """#107 Codex 리뷰: 서브에이전트를 못 읽어 줄어든 스냅숏을 새 transcript로 보아 다시 세지 않는다"""
        main_only = {"claude-opus-5-5": {"input": 0, "output": 10, "cache_write_5m": 0, "cache_write_1h": 0,
                                         "cache_read": 0}}
        both = {"claude-opus-5-5": {**main_only["claude-opus-5-5"], "output": 110}}
        recovered = {"claude-opus-5-5": {**main_only["claude-opus-5-5"], "output": 115}}

        def stop(ts, value, error=None):
            record = json.loads(line("a", ts, branch="feat/x"))
            record["usage"] = value
            if error:
                record["usage_error"] = error
            return json.dumps(record)

        lines = [stop("2026-10-20T01:00:00+00:00", both),
                 stop("2026-10-20T01:05:00+00:00", main_only, "too_large_subagent"),  # 일부만: 다시 세지 않는다
                 stop("2026-10-20T01:10:00+00:00", recovered)]  # 회복: 늘어난 5만
        self.assertEqual(collect.summarize_sessions(lines, "feat/x")["usage"]["claude-opus-5-5"]["output"], 115)
        reset = [stop("2026-10-20T01:00:00+00:00", both), stop("2026-10-20T01:05:00+00:00", main_only)]  # 오류 없음
        self.assertEqual(collect.summarize_sessions(reset, "feat/x")["usage"]["claude-opus-5-5"]["output"], 120)

    def test_usage_split_by_branch_and_reset(self):
        """누적 차를 사건의 브랜치에 주고, 누적이 줄면 새 transcript로 본다"""
        lines = [line("a", "2026-10-20T01:00:00+00:00", "start", branch="feat/a"),
                 usage_line("a", "2026-10-20T01:10:00+00:00", 10, branch="feat/a"),
                 usage_line("a", "2026-10-20T01:20:00+00:00", 25, branch="feat/b"),
                 usage_line("a", "2026-10-20T01:30:00+00:00", 4, branch="feat/b")]  # resume 등으로 새로 셈
        self.assertEqual(collect.summarize_sessions(lines, "feat/a")["usage"]["claude-opus-5-5"]["output"], 10)
        self.assertEqual(collect.summarize_sessions(lines, "feat/b")["usage"]["claude-opus-5-5"]["output"], 19)
        self.assertEqual(collect.summarize_sessions(lines + [line("a", "2026-10-20T01:31:00+00:00", usage=[1])],
                                                    "feat/b")["usage"]["claude-opus-5-5"]["output"], 19)


class UsageCliTest(SessionRecordCase):
    # UsageHookTest를 상속하면 그 테스트가 다시 돌므로 도우미만 가져온다
    transcript = UsageHookTest.transcript
    stop_with = UsageHookTest.stop_with

    def test_summary_shows_tokens_and_cost(self):
        """T10"""
        self.init_git()
        self.start()
        path = self.transcript([assistant("m1", input_=1_000_000, output=100_000)])
        self.stop_with(path)
        code, out, err = run_cli(["sessions", "summary", str(self.project), "--base", "HEAD"])
        self.assertEqual(code, 0, err)
        self.assertIn("- 토큰: 입력 1,000,000 · 출력 100,000 · 캐시 쓰기 0 · 캐시 읽기 0", out)
        self.assertIn("- 추정 비용: 6.00 USD (단가 기준 2026-10-06)", out)


if __name__ == "__main__":
    unittest.main()
