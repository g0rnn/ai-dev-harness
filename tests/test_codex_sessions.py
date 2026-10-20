"""#116 Codex 세션 로그를 세션 기록·토큰 요약에 포함한다."""

import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_hooks import git  # noqa: E402
from test_sessions import SessionRecordCase, collect, harness, run_cli  # noqa: E402

SECRET = "SECRET-PROMPT-DO-NOT-READ"


def ts(minutes_ago=0):
    moment = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def rollout(home: Path, session: str, cwd: Path | str, branch="feat/x", turns=2, model="gpt-6-sol", start=30):
    """실제 Codex 로그(2026-10, cli 0.162)와 같은 구조의 세션 파일. 대화 내용 줄에는 SECRET을 넣는다."""
    lines = [{"timestamp": ts(start), "type": "session_meta",
              "payload": {"id": session, "timestamp": ts(start), "cwd": str(cwd), "originator": "codex_cli",
                          "cli_version": "0.162.0", "git": {"commit_hash": "abc", "branch": branch,
                                                            "repository_url": "https://example.invalid/r.git"}}}]
    for turn in range(turns):
        moment = start - 5 * (turn + 1)
        lines += [
            {"timestamp": ts(moment + 1), "type": "turn_context", "payload": {"cwd": str(cwd), "model": model}},
            {"timestamp": ts(moment + 1), "type": "response_item",
             "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": SECRET}]}},
            {"timestamp": ts(moment), "type": "token_usage_record",
             "payload": {"session_id": session, "usage": {"input_tokens": 1000, "cached_input_tokens": 400,
                                                          "cache_write_input_tokens": 0, "output_tokens": 50,
                                                          "reasoning_output_tokens": 10, "total_tokens": 1050}}},
            {"timestamp": ts(moment), "type": "event_msg",
             "payload": {"type": "task_complete", "last_agent_message": SECRET, "duration_ms": 1000}},
        ]
    path = home / "sessions" / "2026" / "10" / "20" / f"rollout-{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path


class CodexRecordsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)
        self.home, self.repo = self.tmp / "codex", self.tmp / "repo"
        (self.repo / "sub").mkdir(parents=True)

    def test_events_usage_and_no_content(self):
        path = rollout(self.home, "s1", self.repo / "sub")
        root, events = collect.codex_session_records(path, [str(self.repo)])
        self.assertEqual(root, str(self.repo))
        self.assertEqual([e["event"] for e in events], ["start", "stop", "stop"])
        self.assertEqual((events[0]["tool"], events[0]["branch"], events[0]["model"]), ("codex", "feat/x", "gpt-6-sol"))
        self.assertEqual(events[-1]["usage"], {"gpt-6-sol": {"input": 1200, "output": 100, "cache_write_5m": 0,
                                                             "cache_write_1h": 0, "cache_read": 800}})
        self.assertNotIn(SECRET, json.dumps(events))

    def test_other_repositories_and_broken_files_are_skipped(self):
        other = rollout(self.home, "s2", self.tmp / "elsewhere")
        prefix = rollout(self.home, "s3", str(self.repo) + "-other")  # 이름이 같은 앞부분인 다른 폴더
        self.assertEqual(collect.codex_session_records(other, [str(self.repo)]), (None, []))
        self.assertEqual(collect.codex_session_records(prefix, [str(self.repo)]), (None, []))
        broken = self.home / "sessions" / "rollout-broken.jsonl"
        broken.write_text("not json\n", encoding="utf-8")
        self.assertEqual(collect.codex_session_records(broken, [str(self.repo)]), (None, []))
        no_meta = self.home / "sessions" / "rollout-nometa.jsonl"
        no_meta.write_text(json.dumps({"type": "turn_context", "payload": {}}) + "\n", encoding="utf-8")
        self.assertEqual(collect.codex_session_records(no_meta, [str(self.repo)]), (None, []))
        garbage = rollout(self.home, "s4", self.repo)
        with open(garbage, "a", encoding="utf-8") as handle:
            handle.write('{"type": "token_usage_record", broken\n')
            handle.write(json.dumps({"type": "token_usage_record", "payload": {"usage": {"input_tokens": -5,
                                                                                         "output_tokens": True}}}) + "\n")
        _, events = collect.codex_session_records(garbage, [str(self.repo)])
        self.assertEqual(events[-1]["usage"]["gpt-6-sol"]["output"], 100)  # 깨진 줄·음수·불리언은 0으로

    def test_reader_matches_worktrees_and_caches(self):
        rollout(self.home, "s1", self.repo)
        rollout(self.home, "s2", self.tmp / "wt", branch="feat/wt")
        reader = collect.CodexSessions(self.home)
        found = reader.records([str(self.repo), str(self.tmp / "wt")])
        self.assertEqual(sorted({(Path(root).name, e["session"]) for root, e in found}), [("repo", "s1"), ("wt", "s2")])
        with mock.patch.object(collect, "codex_session_records", side_effect=AssertionError("다시 읽음")):
            self.assertEqual(len(reader.records([str(self.repo), str(self.tmp / "wt")])), len(found))
        self.assertEqual(collect.CodexSessions(self.tmp / "none").records([str(self.repo)]), [])

    def write(self, name, records):
        path = self.home / "sessions" / f"rollout-{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        return path

    def meta(self, session, **extra):
        return {"timestamp": ts(30), "type": "session_meta",
                "payload": {"id": session, "cwd": str(self.repo), "git": {"branch": "main"}, **extra}}

    def test_helper_sessions_count_tokens_only(self):
        """#116 리뷰 F2: 승인 검토·서브에이전트(parent_thread_id)는 부모 세션의 토큰으로만 센다"""
        rollout(self.home, "parent", self.repo, branch="main")
        path = rollout(self.home, "child", self.repo, branch="main")
        lines = path.read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0])
        first["payload"]["parent_thread_id"] = "parent"
        path.write_text("\n".join([json.dumps(first)] + lines[1:]) + "\n", encoding="utf-8")
        _, events = collect.codex_session_records(path, [str(self.repo)])
        self.assertEqual({e["event"] for e in events}, {"usage"})
        self.assertEqual({e["parent"] for e in events}, {"parent"})
        records = collect.CodexSessions(self.home).records([str(self.repo)])
        summary = collect.summarize_sessions([json.dumps(e) for _, e in records], "main")
        self.assertEqual((summary["sessions"], summary["turns"], summary["tools"]), (1, 2, {"codex": 1}))
        self.assertEqual(summary["usage"]["gpt-6-sol"]["input"], 2400)  # 부모 1200 + 하위 1200

    def test_branch_follows_reflog(self):
        """#116 리뷰 F1: 응답은 그 시각에 체크아웃한 브랜치에 넣는다"""
        rollout(self.home, "s1", self.repo, branch="main", turns=2, start=30)  # 응답 25분 전·20분 전
        moved = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=22)).isoformat()
        timeline = collect.branch_timeline([f"HEAD@{{{moved}}}\tcheckout: moving from main to feat/y",
                                            "HEAD@{bad}\tcommit: x", f"HEAD@{{{moved}}}\tcommit: y"])
        self.assertEqual([branch for _, branch in timeline], ["feat/y"])
        records = collect.CodexSessions(self.home).records([str(self.repo)], {str(self.repo): timeline})
        self.assertEqual([e["branch"] for _, e in records], ["main", "main", "feat/y"])
        detached = collect.branch_timeline([f"HEAD@{{{moved}}}\tcheckout: moving from main to 0123abcd"])
        self.assertEqual(detached[0][1], None)

    def test_old_logs_trailing_usage_and_bad_timestamp(self):
        """#116 리뷰 F4·F5: 옛 로그는 token_count 누적, 마지막 턴 뒤 토큰도 남기고, 범위 밖 시각 줄 하나는 건너뛴다"""
        old = self.write("old", [
            self.meta("old"),
            {"timestamp": ts(25), "type": "turn_context", "payload": {"model": "gpt-5.6-sol"}},
            {"timestamp": ts(24), "type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 500, "cached_input_tokens": 100, "output_tokens": 20}}}},
            {"timestamp": "2026-13-01T00:00:00Z", "type": "event_msg", "payload": {"type": "task_complete"}},
            {"timestamp": ts(23), "type": "event_msg", "payload": {"type": "task_complete"}},
            {"timestamp": ts(22), "type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 900, "cached_input_tokens": 100, "output_tokens": 40}}}},
        ])
        _, events = collect.codex_session_records(old, [str(self.repo)])
        self.assertEqual([e["event"] for e in events], ["start", "stop", "usage"])
        self.assertEqual(events[1]["usage"]["gpt-5.6-sol"]["input"], 400)
        self.assertEqual(events[2]["usage"]["gpt-5.6-sol"], {"input": 800, "output": 40, "cache_write_5m": 0,
                                                             "cache_write_1h": 0, "cache_read": 100})

    def test_legacy_totals_split_by_model_and_survive_format_change(self):
        """#118 Codex 리뷰: 옛 누적은 늘어난 만큼만 그때의 모델에, 새 형식으로 바뀌어도 옛 몫을 잃지 않는다"""
        def count(seconds_ago, total):
            return {"timestamp": ts(seconds_ago), "type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": total, "cached_input_tokens": 0, "output_tokens": 0}}}}

        def turn(model, minutes_ago):
            return {"timestamp": ts(minutes_ago), "type": "turn_context", "payload": {"model": model}}

        def done(minutes_ago):
            return {"timestamp": ts(minutes_ago), "type": "event_msg", "payload": {"type": "task_complete"}}

        path = self.write("mixed", [
            self.meta("mixed"), turn("model-a", 28), count(27, 500), done(26),
            turn("model-b", 25), count(24, 1000), done(23),  # 누적 1000 = a 500 + b 500
            turn("model-b", 22),  # 재개 뒤 새 형식: 응답별 기록이 먼저, 그 뒤 누적(옛 몫 포함)은 무시
            {"timestamp": ts(21), "type": "token_usage_record", "payload": {"usage": {
                "input_tokens": 800, "cached_input_tokens": 0, "output_tokens": 0}}},
            count(21, 1800), done(20),
        ])
        _, events = collect.codex_session_records(path, [str(self.repo)])
        last = events[-1]["usage"]
        self.assertEqual((last["model-a"]["input"], last["model-b"]["input"]), (500, 1300))
        summary = collect.summarize_sessions([json.dumps(e) for e in events], "main")
        self.assertEqual(sum(c["input"] for c in summary["usage"].values()), 1800)

    def test_resumed_turn_and_branch_rename(self):
        """#118 Codex 리뷰: 오래 쉰 뒤 재개한 턴은 시작 시각부터 작업 구간, 체크아웃한 브랜치 이름 변경을 따른다"""
        path = self.write("resumed", [
            self.meta("resumed"),
            {"timestamp": ts(300), "type": "event_msg", "payload": {"type": "task_started"}},
            {"timestamp": ts(290), "type": "event_msg", "payload": {"type": "task_complete"}},
            {"timestamp": ts(40), "type": "event_msg", "payload": {"type": "task_started"}},  # 4시간 뒤 재개
            {"timestamp": ts(10), "type": "event_msg", "payload": {"type": "task_complete"}},
        ])
        path.write_text(path.read_text(encoding="utf-8").replace(ts(30), ts(310), 1), encoding="utf-8")
        _, events = collect.codex_session_records(path, [str(self.repo)])
        self.assertEqual([e["event"] for e in events], ["start", "turn", "stop", "turn", "stop"])
        summary = collect.summarize_sessions([json.dumps(e) for e in events], "main")
        self.assertEqual((summary["turns"], summary["active_sec"] // 60), (2, 50))  # 시작~첫 턴 20분 + 재개 뒤 30분(없으면 20)
        renamed = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=20)).isoformat()
        timeline = collect.branch_timeline([f"HEAD@{{{renamed}}}\tBranch: renamed refs/heads/main to refs/heads/feat/new"])
        self.assertEqual(collect.branch_at(timeline, dt.datetime.now(dt.timezone.utc), "main"), "feat/new")
        # 같은 초에 main → a → b: reflog는 최신부터(a→b, main→a). 마지막 이동(b)이 이긴다(#118 Codex 리뷰)
        same = collect.branch_timeline([f"HEAD@{{{renamed}}}\tcheckout: moving from a to b",
                                        f"HEAD@{{{renamed}}}\tcheckout: moving from main to a"])
        self.assertEqual(collect.branch_at(same, dt.datetime.now(dt.timezone.utc), "main"), "b")

    def test_openai_prices_and_excluded_auto_review(self):
        """#119: OpenAI 공식 단가로 비용을 내고, 공식 단가가 없는 승인 검토는 비용에서 빼고 수를 알린다"""
        pricing = collect.merged_pricing({})
        usage = {"gpt-6-sol": {"input": 1_000_000, "output": 100_000, "cache_write_5m": 0, "cache_write_1h": 0,
                               "cache_read": 1_000_000},
                 "codex-auto-review": {"input": 500, "output": 10, "cache_write_5m": 0, "cache_write_1h": 0,
                                       "cache_read": 0}}
        self.assertEqual(collect.estimate_cost(usage, pricing), (2.0 + 1.0 + 0.2, []))
        self.assertIn("Codex 승인 검토(codex-auto-review) 토큰 510은 공식 단가가 없어 비용에서 뺐다",
                      collect.excluded_note(usage, pricing))
        summary = {"tools": {"codex": 1}, "turns": 1, "active_sec": 60, "usage": usage}
        section = collect.agent_markdown(summary, None, None, pricing)
        self.assertIn("- 추정 비용(USD): 3.20", section)
        self.assertIn("공식 단가가 없어 비용에서 뺐다", section)
        unknown = {**usage, "gpt-99": {"input": 1, "output": 0, "cache_write_5m": 0, "cache_write_1h": 0,
                                       "cache_read": 0}}
        self.assertNotIn("추정 비용(", collect.agent_markdown({**summary, "usage": unknown}, None, None, pricing))
        with_price = collect.merged_pricing({"pricing": {"models": {"codex-auto-review": {
            "input": 1, "output": 1, "cache_write_5m": 1, "cache_write_1h": 1, "cache_read": 1}}}})
        self.assertIsNone(collect.excluded_note(usage, with_price))  # 팀이 단가를 주면 빼지 않는다

    def test_codex_home_variable(self):
        self.assertEqual(collect.codex_home({"CODEX_HOME": str(self.home)}), self.home)
        self.assertEqual(collect.codex_home({}), Path.home() / ".codex")


class CodexCliTest(SessionRecordCase):
    def test_agent_summary_counts_codex_and_withholds_unpriced_cost(self):
        """인수 조건: worktree에서 연 Codex 세션이 도구 줄·토큰에 든다. 단가가 없으면 추정 비용을 내지 않는다"""
        self.init_git()
        git(self.project, "checkout", "-q", "-b", "feat/x")
        home = self.tmp / "codex"
        rollout(home, "c1", self.project, model="gpt-unpriced-test")
        rollout(home, "c2", self.tmp / "unrelated", model="gpt-unpriced-test")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}):
            code, out, err = run_cli(["agent", "summary", str(self.project), "--base", "HEAD"])
        self.assertEqual(code, 0, err)
        self.assertIn("- 도구: codex 1\n", out)
        self.assertIn("- 응답: 2\n", out)
        self.assertIn("- 토큰: 입력 1,200 · 출력 100 · 캐시 쓰기 0 · 캐시 읽기 800", out)
        self.assertNotIn("추정 비용(", out)
        self.assertIn("단가 없는 모델 gpt-unpriced-test", out)
        parsed = harness.metrics_collect.parse_agent_lines(out.splitlines()[2:])
        self.assertEqual((parsed["tools"], parsed["cost"]), ({"codex": 1}, None))

        # 팀이 pricing.models에 단가를 넣으면 비용 줄이 나온다
        pricing = collect.merged_pricing({"pricing": {"models": {"gpt-unpriced-test": {
            "input": 1000, "output": 10000, "cache_write_5m": 1000, "cache_write_1h": 1000, "cache_read": 100}}}})
        lines = [json.dumps(e) for _, e in collect.CodexSessions(home).records([str(self.project)])]
        section = collect.agent_markdown(collect.summarize_sessions(lines, "feat/x"), None, None, pricing)
        self.assertIn("- 추정 비용(USD): 2.28", section)  # (1200×1000 + 100×10000 + 800×100) / 1e6

    def test_dashboard_shows_active_codex_session(self):
        self.init_git()
        home = self.tmp / "codex"
        rollout(home, "c1", self.project, start=20)
        dashboard = harness.load_dashboard()
        source = dashboard.Source(self.project, harness.hooks_common, collect, collect.merged_pricing({}),
                                  dt.timezone.utc, None, collect.CodexSessions(home))
        state = source.state(wait=True)
        self.assertEqual([(s["tool"], s["worktree"], s["turns"]) for s in state["active"]], [("codex", "(기본)", 2)])
        self.assertNotIn(SECRET, json.dumps(state))


if __name__ == "__main__":
    unittest.main()
