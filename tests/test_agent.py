"""#98 M5-3 에이전트 귀속: MR 본문 `## 에이전트 기록`과 지표 6·7."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_metrics_collect import ROOT, gitlab_api, github_api, judge, run_collect  # noqa: E402
from test_sessions import SessionRecordCase, collect, line, run_cli  # noqa: E402
from test_usage import UsageHookTest, assistant, usage_line  # noqa: E402


def body(*lines):
    return "\n".join(["## 판정", "", "- standard — x", "", *lines, "", "## 검증", "", "- 방법: x"])


class AgentSectionTest(unittest.TestCase):
    def test_markdown_roundtrip(self):
        """T1: 세션 요약 → 절 → 파서가 같은 수"""
        lines = [line("a", "2026-10-20T01:00:00+00:00", "start"),
                 usage_line("a", "2026-10-20T01:30:00+00:00", 1_000_000),
                 usage_line("a", "2026-10-20T02:00:00+00:00", 2_000_000)]
        summary = collect.summarize_sessions(lines, "feat/x")
        section = collect.agent_markdown(summary, 1, 3)
        self.assertIn("- 추정 비용(USD): 40.00", section)  # Opus 5.5 출력 2M × $20
        parsed = judge().agent(body(section))
        self.assertEqual(parsed["tools"], {"claude-code": 1})
        self.assertEqual((parsed["turns"], parsed["active_min"]), (2, 60))
        self.assertEqual(parsed["tokens"], {"input": 0, "output": 2_000_000, "cache_write": 0, "cache_read": 0})
        self.assertEqual((parsed["cost"], parsed["currency"]), (40.0, "USD"))
        self.assertEqual((parsed["unrecorded"], parsed["commits"]), (1, 3))

    def test_missing_usage_is_not_zero(self):
        """#108 Codex 리뷰: 토큰 기록이 없으면 0이 아니라 "기록 없음"이고, 읽지 못한 세션 경고는 따로 남는다"""
        lines = [line("a", "2026-10-20T01:00:00+00:00", "start"),
                 line("a", "2026-10-20T01:30:00+00:00", usage_error="too_large")]
        section = collect.agent_markdown(collect.summarize_sessions(lines, "feat/x"), 0, 1)
        self.assertIn("- 토큰: 기록 없음", section)
        self.assertNotIn("추정 비용", section)
        self.assertIn("- 참고: 토큰을 다 읽지 못한 세션 1개\n", section)
        parsed = judge().agent(body(section))
        self.assertEqual((parsed["tools"], parsed["tokens"], parsed["cost"]), ({"claude-code": 1}, None, None))
        partial = lines + [usage_line("b", "2026-10-20T02:00:00+00:00", 10)]
        self.assertIn("토큰을 다 읽지 못한 세션 1개(합은 일부)",
                      collect.agent_markdown(collect.summarize_sessions(partial, "feat/x"), 0, 1))

    def test_manual_tool_line_only(self):
        """T2: 다른 도구는 도구 줄만 적어도 보고다"""
        parsed = judge().agent(body("## 에이전트 기록", "", "- 도구: codex 1"))
        self.assertEqual(parsed["tools"], {"codex": 1})
        self.assertEqual((parsed["turns"], parsed["cost"], parsed["unrecorded"]), (None, None, None))
        mixed = judge().agent(body("## 에이전트 기록", "", "- 도구: claude-code 2, codex 1, 잘못된 항목"))
        self.assertEqual(mixed["tools"], {"claude-code": 2, "codex": 1})

    def test_mention_syntax_is_not_accepted(self):
        """#101 리뷰 F2: 도구 이름·통화는 주간 요약 메시지에 들어가므로 멘션 문법을 받지 않는다"""
        parsed = judge().agent(body("## 에이전트 기록", "", "- 도구: @channel 1, <!here> 1, codex 1",
                                    "- 추정 비용(@here): 1", "- 추정 비용(<!c>): 2"))
        self.assertEqual((parsed["tools"], parsed["cost"]), ({"codex": 1}, None))
        won = judge().agent(body("## 에이전트 기록", "", "- 도구: 클로드 1", "- 추정 비용(원): 1500"))
        self.assertEqual((won["tools"], won["currency"], won["cost"]), ({"클로드": 1}, "원", 1500.0))

    def test_missing_or_fenced(self):
        """T3: 절 없음·기록 없음·주석과 코드 블록 안의 예시는 미보고"""
        j = judge()
        self.assertIsNone(j.agent(body()))
        self.assertIsNone(j.agent(body("## 에이전트 기록", "", "기록 없음")))
        self.assertIsNone(j.agent(body("## 에이전트 기록", "<!-- 예: `- 도구: claude-code 2, codex 1` -->", "기록 없음")))
        self.assertIsNone(j.agent(body("## 에이전트 기록", "```", "- 도구: codex 1", "```")))
        self.assertIsNone(j.agent(body("## 에이전트 기록", "- 도구: 없음")))

    def test_template_comment_is_not_a_report(self):
        """생성 템플릿 그대로면 미보고(주석 안 예시를 읽지 않는다)"""
        template = (ROOT / "core" / "templates" / "merge-request" / "Default.md").read_text(encoding="utf-8")
        self.assertIn("## 에이전트 기록", template)
        self.assertIsNone(judge().agent(template))


class AgentCliTest(SessionRecordCase):
    transcript = UsageHookTest.transcript
    stop_with = UsageHookTest.stop_with

    def test_agent_summary_cli(self):
        """T4"""
        self.init_git()
        self.start()
        self.stop_with(self.transcript([assistant("m1", output=100_000)]))
        code, out, err = run_cli(["agent", "summary", str(self.project), "--base", "HEAD"])
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("## 에이전트 기록\n\n- 도구: claude-code 1\n- 응답: 1\n"))
        self.assertIn("- 추정 비용(USD): 2.00", out)
        self.assertIn("- 기록 없는 커밋: 0/0", out)
        self.assertEqual(judge().agent(body(out))["tools"], {"claude-code": 1})


class CollectAgentTest(unittest.TestCase):
    def test_weekly_and_by_tool(self):
        """T5: 주별 지표 6과 기간 지표 7. 실패 job ref가 MR 11의 GitLab MR ref"""
        code, result, _, err = run_collect("gitlab", gitlab_api())
        self.assertEqual(code, 0, err)
        first = next(w for w in result["weeks"] if w["week"] == "2026-W39")
        # fixture MR 본문에는 에이전트 절이 없다: 통합 MR을 뺀 3건 모두 미보고
        self.assertEqual((first["agent"]["reported_mrs"], first["agent"]["unreported_mrs"]), (0, 3))
        self.assertEqual(result["by_tool"], {"미보고": {"mrs": 3, "ci_failed_mrs": 1, "escaped_mrs": 1, "mismatch_mrs": 1}})

    def test_reported_section_through_collect(self):
        """리뷰 F5: 보고한 절이 collect 전체를 지나 주별·by_tool에 들어가고, 깨진 수 줄이 수집을 멈추지 않는다(F1·F2)"""
        from test_metrics_collect import fixture
        data = fixture("gitlab.json")
        mr10 = next(mr for mr in data["merge_requests"] if mr["iid"] == 10)
        mr10["description"] = (mr10.get("description") or "") + "\n\n## 에이전트 기록\n\n- 도구: claude-code 2, Codex CLI 1\n" \
            "- 응답: 9\n- 작업 시간(분): 40\n- 토큰: 입력 , · 출력 1 · 캐시 쓰기 1 · 캐시 읽기 1\n" \
            f"- 추정 비용(USD): {'9' * 400}\n- 추정 비용(USD): 1.25\n- 도구: claude-code 5\n"
        code, result, text, err = run_collect("gitlab", gitlab_api(data))
        self.assertEqual(code, 0, err)
        self.assertNotIn("Infinity", text)
        first = next(w for w in result["weeks"] if w["week"] == "2026-W39")
        self.assertEqual(first["agent"]["reported_mrs"], 1)
        self.assertEqual((first["agent"]["sessions"], first["agent"]["turns"], first["agent"]["active_min"]), (3, 9, 40))
        self.assertEqual((first["agent"]["cost"], first["agent"]["cost_mrs"]), ({"USD": 1.25}, 1))
        self.assertEqual(first["agent"]["tools"], {"claude-code": 1, "Codex CLI": 1})
        mr = next(m for m in result["mrs"] if m["number"] == 10)
        self.assertIsNone(mr["agent"]["tokens"])
        self.assertEqual(set(result["by_tool"]), {"claude-code", "Codex CLI", "미보고"})

    def test_by_tool_counts(self):
        mrs = [{"number": 1, "source_branch": "feat/1", "integration": False, "tier": {"mismatch": True},
                "agent": {"tools": {"claude-code": 2}}},
               {"number": 2, "source_branch": "feat/2", "integration": False, "tier": {"mismatch": False},
                "agent": {"tools": {"claude-code": 1, "codex": 1}}},
               {"number": 3, "source_branch": "feat/3", "integration": False, "tier": {"mismatch": False},
                "agent": None},
               {"number": 4, "source_branch": "develop", "integration": True, "tier": None, "agent": None}]
        jobs = [{"ref": "feat/2"}, {"ref": "feat/2"}, {"ref": None}, {"ref": "main"}]
        defects = [{"cause": 1, "status": "linked"}, {"cause": 3, "status": "out_of_window"}]
        self.assertEqual(collect.by_tool(mrs, defects, jobs, "github"), {
            "claude-code": {"mrs": 2, "ci_failed_mrs": 1, "escaped_mrs": 1, "mismatch_mrs": 1},
            "codex": {"mrs": 1, "ci_failed_mrs": 1, "escaped_mrs": 0, "mismatch_mrs": 0},
            "미보고": {"mrs": 1, "ci_failed_mrs": 0, "escaped_mrs": 0, "mismatch_mrs": 0}})

    def test_week_agent_totals(self):
        week = collect.empty_week_agent()
        collect.add_week_agent(week, {"tools": {"claude-code": 2}, "turns": 5, "active_min": 30, "cost": 1.5,
                                      "currency": "USD", "tokens": None})
        collect.add_week_agent(week, {"tools": {"codex": 1}, "turns": None, "active_min": None, "cost": None,
                                      "currency": None, "tokens": None})
        collect.add_week_agent(week, None)
        self.assertEqual(week, {"reported_mrs": 2, "unreported_mrs": 1, "sessions": 3, "turns": 5, "active_min": 30,
                                "tokens": {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}, "tokens_mrs": 0,
                                "cost": {"USD": 1.5}, "cost_mrs": 1, "tools": {"claude-code": 1, "codex": 1}})

    def test_job_ref_recorded(self):
        """T6"""
        _, gitlab, _, _ = run_collect("gitlab", gitlab_api())
        self.assertEqual({job["id"]: job["ref"] for job in gitlab["jobs"]}, {501: "refs/merge-requests/11/head", 502: None})
        _, github, _, err = run_collect("github", github_api())
        self.assertIn("feat/7", [job["ref"] for job in github["jobs"]], err)


if __name__ == "__main__":
    unittest.main()
