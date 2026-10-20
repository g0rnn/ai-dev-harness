"""#102 M5-7 로컬 실시간 대시보드."""

import datetime as dt
import http.client
import json
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_hooks import git, git_path  # noqa: E402
from test_sessions import SessionRecordCase, collect, harness  # noqa: E402
from test_usage import UsageHookTest, assistant  # noqa: E402

dashboard = harness.load_dashboard()
UTC = dt.timezone.utc
KST = dt.timezone(dt.timedelta(hours=9))
NOW = dt.datetime(2026, 10, 20, 3, 0, tzinfo=UTC)  # KST 10/20(화) 12:00, 이번 주는 10/19(월)부터


def rec(session, minutes_ago, event="stop", branch="feat/x", **extra):
    ts = (NOW - dt.timedelta(minutes=minutes_ago)).isoformat()
    return {"ts": ts, "branch": branch, "event": event, "session": session, "tool": "claude-code", **extra}


def usage(output):
    return {"claude-opus-5-5": {"input": 0, "output": output, "cache_write_5m": 0, "cache_write_1h": 0, "cache_read": 0}}


class BuildStateTest(unittest.TestCase):
    def state(self, sessions, events=(), live=None):
        return dashboard.build_state([("wt", r) for r in sessions], [("wt", e) for e in events], NOW, KST, collect,
                                     collect.merged_pricing({}), live)

    def test_active_sessions_and_totals(self):
        sessions = [rec("aaaaaaaa-1", 30, "start", model="claude-opus-5-5", transcript="t1.jsonl"),
                    rec("aaaaaaaa-1", 10, verify="pass", usage=usage(1_000_000)),
                    rec("bbbbbbbb-2", 300, "start"), rec("bbbbbbbb-2", 290, verify="fail"),  # 2시간 넘게 조용함
                    rec("cccccccc-3", 20, "start"), rec("cccccccc-3", 5, "end"),  # 끝남
                    rec("dddddddd-4", 60 * 24 * 3, "start")]  # 10/17(토) → 지난주, 이번 주 합계 밖
        events = [{"ts": (NOW - dt.timedelta(minutes=1)).isoformat(), "branch": "feat/x", "hook": "protect-paths",
                   "decision": "block", "rule": "package-lock.json", "path": "package-lock.json"},
                  {"ts": (NOW - dt.timedelta(days=1)).isoformat(), "branch": "feat/x", "hook": "stop-verify",
                   "decision": "fail"},
                  {"ts": NOW.isoformat(), "hook": "x", "decision": "allow"}]  # 개입이 아닌 결정은 빠진다
        state = self.state(sessions, events)
        self.assertEqual([s["session"] for s in state["active"]], ["aaaaaaaa"])
        active = state["active"][0]
        self.assertEqual((active["turns"], active["verify"], active["model"], active["elapsed_min"]),
                         (1, "pass", "claude-opus-5-5", 30))
        self.assertEqual((active["tokens"]["output"], active["cost"]), (1_000_000, 20.0))
        self.assertEqual(state["totals"]["today"], {"sessions": 3, "turns": 2, "interventions": 1, "cost": 20.0,
                                                    "branches": [{"branch": "feat/x", "cost": 20.0}], "unpriced": [],
                                                    "excluded": []})
        self.assertEqual(state["totals"]["week"]["interventions"], 2)
        self.assertEqual(state["totals"]["week"]["sessions"], 3)  # 토요일 세션은 지난주
        self.assertEqual([e["decision"] for e in state["events"]], ["block", "fail"])
        json.dumps(state)  # 직렬화할 수 있다

    def test_live_usage_overrides_recorded(self):
        sessions = [rec("a", 10, "start", transcript="t.jsonl"), rec("a", 5, usage=usage(10))]
        state = self.state(sessions, live=lambda path: (usage(1_000_000), None) if path == "t.jsonl" else (None, None))
        self.assertEqual(state["active"][0]["tokens"]["output"], 1_000_000)
        self.assertEqual(state["totals"]["today"]["cost"], 20.0)  # 기록 뒤 transcript에 더 쓴 토큰도 오늘에 든다

    def test_partial_live_snapshot_is_not_a_reset(self):
        """#113 Codex 리뷰: 서브에이전트를 못 읽어 줄어든 실시간 합은 다시 시작이 아니다(20 → 일부 10이면 오늘 20, 30이 아님)"""
        sessions = [rec("a", 10, "start", transcript="t.jsonl"), rec("a", 5, usage=usage(1_000_000))]
        state = self.state(sessions, live=lambda path: (usage(500_000), "too_large_subagent"))
        self.assertEqual(state["totals"]["today"]["cost"], 20.0)
        self.assertEqual(state["active"][0]["usage_error"], "too_large_subagent")
        unreadable = self.state(sessions, live=lambda path: (None, "too_large"))  # 본 transcript를 못 읽음
        self.assertEqual((unreadable["active"][0]["usage_error"], unreadable["active"][0]["cost"]), ("too_large", 20.0))

    def test_today_branches_follow_weekly_list(self):
        """#113 Codex 리뷰: 이번 주 상위 브랜치의 오늘 값은 오늘 상위 10개 밖이어도 남는다"""
        def at(day, hour):
            return int((NOW - dt.datetime(2026, 10, day, hour, 0, tzinfo=KST)).total_seconds() // 60)
        sessions = [rec("big", at(19, 9), "start", branch="feat/big"),
                    rec("big", at(19, 10), branch="feat/big", usage=usage(5_000_000)),  # 어제 100
                    rec("big", at(20, 9), branch="feat/big", usage=usage(5_050_000))]  # 오늘 +1
        for i in range(10):
            sessions += [rec(f"s{i}", at(20, 8), "start", branch=f"feat/b{i}"),
                         rec(f"s{i}", at(20, 10), branch=f"feat/b{i}", usage=usage(100_000))]  # 오늘 2씩
        totals = self.state(sessions)["totals"]
        week = [b["branch"] for b in totals["week"]["branches"]]
        self.assertEqual(len(week), dashboard.TOP_BRANCHES)
        self.assertEqual(week[0], "feat/big")
        today = {b["branch"]: b["cost"] for b in totals["today"]["branches"]}
        self.assertEqual(today["feat/big"], 1.0)
        self.assertLessEqual(set(today), set(week))

    def test_excluded_model_tokens_are_shown(self):
        """#119 리뷰 F1: 공식 단가가 없는 승인 검토 토큰은 비용에서 빠지고 합계에 토큰 수가 나온다"""
        review = {"codex-auto-review": {"input": 300, "output": 20, "cache_write_5m": 0, "cache_write_1h": 0,
                                        "cache_read": 0}}
        sessions = [rec("a", 10, "start"), rec("a", 5, usage={**usage(1_000_000), **review})]
        totals = self.state(sessions)["totals"]
        self.assertEqual((totals["today"]["cost"], totals["today"]["unpriced"]), (20.0, []))
        self.assertEqual(totals["today"]["excluded"], [{"model": "codex-auto-review", "tokens": 320}])

    def test_costs_and_turns_follow_event_dates(self):
        """자정·월요일을 넘는 세션은 그날 쓴 만큼만, 브랜치별로 나눠 센다(sessions summary와 같은 사건별 차)"""
        def at(day, hour, minute):  # KST 시각 → NOW 기준 몇 분 전
            return int((NOW - dt.datetime(2026, 10, day, hour, minute, tzinfo=KST)).total_seconds() // 60)
        sessions = [rec("a", at(18, 23, 0), "start"),  # 10/18(일) 지난주
                    rec("a", at(18, 23, 50), usage=usage(1_000_000)),  # 20.0, 지난주
                    rec("a", at(20, 0, 10), branch="feat/y", usage=usage(1_050_000)),  # +1.0 오늘, feat/y
                    rec("a", at(20, 0, 15), "end", branch="feat/y"),
                    rec("b", at(19, 23, 0), "start"),
                    rec("b", at(19, 23, 59), usage=usage(500_000))]  # 10.0, 어제(이번 주)
        totals = self.state(sessions)["totals"]
        self.assertEqual((totals["today"]["turns"], totals["today"]["cost"]), (1, 1.0))
        self.assertEqual(totals["today"]["branches"], [{"branch": "feat/y", "cost": 1.0}])
        self.assertEqual((totals["week"]["turns"], totals["week"]["cost"]), (2, 11.0))
        self.assertEqual(totals["week"]["branches"], [{"branch": "feat/x", "cost": 10.0},
                                                       {"branch": "feat/y", "cost": 1.0}])

    def test_utc_and_local_dates_differ(self):
        """10/19 23:30Z는 KST로 10/20(오늘), UTC로는 어제다"""
        moment = dt.datetime(2026, 10, 19, 23, 30, tzinfo=UTC)
        sessions = [("wt", {"ts": moment.isoformat(), "event": "stop", "session": "a", "branch": "b",
                            "usage": usage(50_000)})]
        pricing = collect.merged_pricing({})
        kst = dashboard.build_state(sessions, [], NOW, KST, collect, pricing)["totals"]
        utc = dashboard.build_state(sessions, [], NOW, UTC, collect, pricing)["totals"]
        self.assertEqual((kst["today"]["turns"], kst["today"]["cost"]), (1, 1.0))
        self.assertEqual((utc["today"]["turns"], utc["today"]["cost"]), (0, 0.0))
        self.assertEqual(utc["week"]["cost"], 1.0)

    def test_elapsed_counts_current_segment(self):
        """어제 띄운 세션을 오늘 resume하면 경과는 resume부터"""
        sessions = [rec("a", 60 * 20, "start"), rec("a", 60 * 20 - 5), rec("a", 30, "start", transcript="t"),
                    rec("a", 10)]
        active = self.state(sessions)["active"][0]
        self.assertEqual((active["elapsed_min"], active["turns"]), (30, 2))

    def test_malformed_lines_are_skipped(self):
        records = dashboard.parse_lines(["{", "[]", json.dumps({"ts": "bad"}), json.dumps(rec("a", 1)),
                                         json.dumps({"ts": "2026-13-01T00:00:00Z", "session": "a"}),
                                         json.dumps({"ts": "2026-10-20T00:00:00+99:99", "session": "a"})], collect)
        self.assertEqual(len(records), 1)

    def test_usage_cache_follows_subagent_files(self):
        """#113 Codex 리뷰: 본 transcript가 그대로여도 서브에이전트 파일이 바뀌면 다시 센다"""
        import tempfile
        root = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, root, ignore_errors=True)
        main = root / "s.jsonl"
        main.write_text("{}\n", encoding="utf-8")
        calls = []
        cache = dashboard.UsageCache(lambda path: (calls.append(path) or (usage(len(calls)), None)))
        self.assertEqual(cache.get(str(main))[0], usage(1))
        self.assertEqual(cache.get(str(main))[0], usage(1))  # 바뀐 것이 없으면 캐시
        (root / "s" / "subagents").mkdir(parents=True)
        (root / "s" / "subagents" / "agent-1.jsonl").write_text("{}\n", encoding="utf-8")
        self.assertEqual(cache.get(str(main))[0], usage(2))
        with open(root / "s" / "subagents" / "agent-1.jsonl", "a", encoding="utf-8") as handle:
            handle.write("{}\n")
        self.assertEqual(cache.get(str(main))[0], usage(3))
        self.assertEqual(cache.get(None), (None, None))

    def test_host_allowed(self):
        for ok in ("127.0.0.1:8765", "localhost:1", "LOCALHOST", "127.0.0.1"):
            self.assertTrue(dashboard.host_allowed(ok), ok)
        for bad in (None, "", "evil.example:8765", "127.0.0.1.evil.example", "localhost.evil:80", "[::1]:8765"):
            self.assertFalse(dashboard.host_allowed(bad), bad)


class ServerTest(SessionRecordCase):
    transcript = UsageHookTest.transcript
    stop_with = UsageHookTest.stop_with

    def setUp(self):
        super().setUp()
        self.init_git()
        source = dashboard.Source(self.project, harness.hooks_common, collect, collect.merged_pricing({}), KST, None)
        self.server = dashboard.make_server(source, 0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def get(self, path, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", path, headers={"Host": host or f"127.0.0.1:{self.port}"})
        response = conn.getresponse()
        return response.status, response.read(), response.getheader("Content-Type")

    def test_binds_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_pages_and_forbidden_host(self):
        status, body, kind = self.get("/")
        self.assertEqual((status, kind), (200, "text/html; charset=utf-8"))
        self.assertNotIn(b"http://", body.replace(b"http://www.w3.org", b""))  # 외부 리소스를 부르지 않는다
        self.assertNotIn(b"innerHTML", body)
        status, body, _ = self.get("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["active"], [])
        self.assertEqual(self.get("/api/state", host="evil.example")[0], 403)
        self.assertEqual(self.get("/nope")[0], 404)

    def test_stream_shows_new_session_and_block_quickly(self):
        """세션 시작 뒤 5초, block 이벤트 뒤 2초 안에 스트림에 나온다"""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/api/stream", headers={"Host": f"127.0.0.1:{self.port}"})
        response = conn.getresponse()
        self.assertEqual(response.getheader("Content-Type"), "text/event-stream; charset=utf-8")

        def next_state(predicate, limit):
            deadline = time.monotonic() + limit
            while time.monotonic() < deadline:
                line = response.fp.readline().decode("utf-8")
                if line.startswith("data: "):
                    state = json.loads(line[6:])
                    if predicate(state):
                        return state
            self.fail("시간 안에 나오지 않았다")

        next_state(lambda s: True, 5)  # 첫 상태
        self.assertEqual(self.start().returncode, 0)
        started = time.monotonic()  # 시작 기록이 파일에 쓰인 뒤
        next_state(lambda s: s["active"], 5)  # 시작 기록만으로 진행 중 세션에 나온다
        self.assertLess(time.monotonic() - started, 5)
        self.stop_with(self.transcript([assistant("m1", output=7)]))
        state = next_state(lambda s: s["active"] and s["active"][0]["tokens"]["output"] == 7, 5)
        self.assertEqual(state["active"][0]["turns"], 1)
        harness.hooks_common.log_event(self.project, {"hook": "protect-paths", "decision": "block",
                                                      "rule": "yarn.lock", "path": "yarn.lock"})
        logged = time.monotonic()
        next_state(lambda s: any(e["decision"] == "block" for e in s["events"]), 2)
        self.assertLess(time.monotonic() - logged, 2)
        conn.close()

    def test_broken_line_does_not_break_state(self):
        """범위 밖 시각 줄이 있어도 /api/state는 응답한다"""
        path = git_path(self.project, "sessions.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"ts": "2026-13-01T00:00:00Z", "session": "a", "event": "start"}) + "\n",
                        encoding="utf-8")
        status, body, _ = self.get("/api/state")
        self.assertEqual((status, json.loads(body)["active"]), (200, []))


class UnrecordedTest(SessionRecordCase):
    def test_worktrees_named_alike_and_unrecorded_counted(self):
        """기록 없는 커밋: worktree마다 현재 브랜치, 분리된 HEAD는 건너뛴다. 이름은 세션 표와 같은 규칙"""
        self.init_git()
        git(self.project, "branch", "base")
        git(self.project, "checkout", "-q", "-b", "feat/main")
        self.commit("a.txt")
        wt = self.tmp / "wt-a"
        git(self.project, "worktree", "add", "-q", "-b", "feat/a", str(wt), "base")
        self.commit("b.txt", project=wt)
        self.commit("c.txt", project=wt)
        git(self.project, "worktree", "add", "-q", "--detach", str(self.tmp / "wt-detached"), "base")
        time.sleep(1.1)  # 커밋 시각(초 단위)보다 뒤에 시작한 세션이라 앞 커밋은 미기록
        self.assertEqual(self.start(project=wt).returncode, 0)
        pricing = collect.merged_pricing({})
        source = dashboard.Source(self.project, harness.hooks_common, collect, pricing, KST, "base")
        names = [name for name, _ in source.state_dirs()]
        self.assertEqual(names[0], "(기본)")
        self.assertIn("wt-a", names)
        state = source.state(wait=True)
        self.assertEqual([s["worktree"] for s in state["active"]], ["wt-a"])
        self.assertEqual(sorted((u["worktree"], u["branch"], u["unrecorded"], u["commits"])
                                for u in state["unrecorded"]),
                         [("(기본)", "feat/main", 1, 1), ("wt-a", "feat/a", 2, 2)])
        no_base = dashboard.Source(self.project, harness.hooks_common, collect, pricing, KST, None)
        self.assertEqual(no_base.state(wait=True)["unrecorded"], [])


if __name__ == "__main__":
    unittest.main()
