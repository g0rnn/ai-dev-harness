"""#101 M5-6 주간 리포트·요약의 지표 6~8."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_metrics_notify import notify  # noqa: E402
from test_metrics_report import find_all, parse, report, sample, text_of  # noqa: E402


def with_agentops():
    data = sample()
    last = data["weeks"][-1]
    last["agent"] = {"reported_mrs": 1, "unreported_mrs": 0, "sessions": 3, "turns": 12, "active_min": 50,
                     "cost": {"USD": 5.0, "a b": 1, "KRW": float("inf"), "EUR": -3, "@here": 9, "BIG": 10 ** 400},
                     "cost_mrs": 2, "tokens": {"input": 1200, "output": 3400, "cache_write": 0, "cache_read": 0},
                     "tokens_mrs": 2, "tools": {"claude-code": 1}}
    last["review"] = {"reported_mrs": 2, "strict_reported_mrs": 1, "strict_adopted": 3, "findings": 12,
                      "adopted": 11, "rejected": 1, "followup": 0, "pending": 0, "duplicates": 1}  # 표준 MR 채택 8
    data["by_tool"] = {"claude-code": {"mrs": 2, "ci_failed_mrs": 1, "escaped_mrs": 0, "mismatch_mrs": 1},
                       "<script>": {"mrs": 0}, "bad": 3}
    week = last["week"]
    data["mrs"] += [{"number": 90, "week": week, "agent": {"tools": {"claude-code": 1}, "cost": 1.0, "currency": "USD"},
                     "tier": {"effective": "standard"}},
                    {"number": 91, "week": week, "agent": {"tools": {"codex": 1}, "cost": 4.0, "currency": "USD"},
                     "tier": {"effective": "standard"}},
                    {"number": 92, "week": week, "agent": {"tools": {"x": 1}, "cost": -1, "currency": "USD"},
                     "tier": {"effective": "standard"}},
                    {"number": 93, "week": week, "agent": None, "tier": {"effective": "lite"}}]
    return data


class ReportAgentOpsTest(unittest.TestCase):
    def test_sections_and_values(self):
        html = report.render(json.loads(json.dumps(with_agentops(), allow_nan=True)), None)
        root = parse(html)  # XML로 읽힌다(이스케이프 확인)
        self.assertNotIn("<script>", html)
        self.assertNotIn(" KRW", html)  # 무한대 비용은 버린다
        self.assertNotIn("EUR", html)  # 음수 비용도 버린다
        self.assertNotIn("1.00 a b", html)  # 공백이 든 통화는 버린다
        tables = {text_of(h): h for h in find_all(root, "h2")}
        self.assertIn("지표 7 도구별 결과 (기간 전체)", tables)
        text = " ".join(text_of(td) for td in find_all(root, "td"))
        self.assertIn("5.00 USD", text)  # 주별 합(형식이 틀린 통화·무한대·아주 큰 정수·멘션 문법은 버림)
        self.assertIn("2.50 USD", text)  # MR당 중앙값: 1.0, 4.0 → 2.5(음수 비용 MR은 빠짐)
        self.assertNotIn("@here", html)
        self.assertIn("입력 1,200 · 출력 3,400", text)  # 토큰 추세
        self.assertIn("1 (50%)", text)  # 도구별 CI 실패 MR
        self.assertIn("3.0", text)  # 엄격 MR당 채택: 엄격 MR 채택 3 / 1(표준 MR 채택 8은 섞지 않는다, #110)
        self.assertNotIn("11.0", text)
        self.assertIn("claude-code 1 · 1.00 USD", text)
        self.assertIn("미보고", text)

    def test_old_json_still_renders(self):
        html = report.render(sample(), None)
        self.assertIn("지표 6 에이전트 기록", html)
        self.assertIn("데이터 없음", html.split("지표 7 도구별 결과")[1].split("지표 8")[0])

    def test_notify_lines(self):
        lines = notify.build_message(with_agentops(), None).split("\n")
        self.assertIn("- 6 에이전트 추정 비용: 5.00 USD (지난주 -, -), 보고 MR 1/1", lines)
        self.assertIn("- 8 리뷰 채택 결함 수: 11 (지난주 0, +11)", lines)
        self.assertIn("- 7 도구별 MR(기간): claude-code 2", lines)  # `<script>` 같은 이름은 발송에서 다시 거른다
        self.assertFalse(any("<script>" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
