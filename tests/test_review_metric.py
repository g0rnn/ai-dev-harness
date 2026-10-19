"""#99 M5-4 리뷰 처리 표 → MR 본문 `## 리뷰 결과` 요약 → 지표 8."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tempdirs import remove_tree  # noqa: E402
from test_agent import body  # noqa: E402
from test_metrics_collect import ROOT, judge  # noqa: E402
from test_sessions import collect, run_cli  # noqa: E402

STRICT_TABLE = """# 리뷰 종합

| # | 결함 | A | B | 처리 | 근거 |
| --- | --- | --- | --- | --- | --- |
| U1 | x | F1(중) | F3(낮) | **채택·수정** | |
| U2 | y | — | F1(중) | 채택(문서) | |
| U3 | z | F2(낮) | 해소 확인 | 기각 | |
| U4 | w | F4 | — | 후속 #12 | |
| U5 | v | — | F5 | 판단 필요 | |
"""

FOUND_TABLE = """## 7. 처리 (작성자가 반영 뒤 채운다)

| # | 결함 | 발견 | 처리 | 근거 |
| --- | --- | --- | --- | --- |
| U1 | a | A F1, B F2 | 채택 | |
| U2 | b | F3 | 기각 | |
| U3 | | | | |
"""


class SummarizeReviewTest(unittest.TestCase):
    def test_strict_table_with_a_b_columns(self):
        summary, errors = collect.summarize_review(STRICT_TABLE)
        self.assertEqual(errors, [])
        self.assertEqual(summary, {"findings": 5, "adopted": 2, "rejected": 1, "followup": 1, "pending": 1,
                                   "duplicates": 1})  # U3의 B는 "해소 확인"이라 발견이 아니다

    def test_found_column_and_empty_template_row(self):
        summary, errors = collect.summarize_review(FOUND_TABLE)
        self.assertEqual((summary["findings"], summary["adopted"], summary["rejected"], summary["duplicates"]),
                         (2, 1, 1, 1))
        self.assertEqual(errors, [])

    def test_invalid_outcome_and_missing_table(self):
        _, errors = collect.summarize_review(FOUND_TABLE.replace("| 기각 |", "| 결함 반영 |"))
        self.assertEqual(len(errors), 1)
        self.assertIn("U2", errors[0])
        self.assertEqual(collect.summarize_review("# 리뷰\n\n발견 없음\n"), (None, ["처리 열이 있는 표가 없다(리뷰 템플릿 7장)"]))

    def test_dash_numbering_and_escaped_pipe(self):
        """리뷰 F1·F3: `A-1`/`B-5` 표기와 `-` 빈 칸(실제 82-review 형식), 칸 안의 `\\|`"""
        table = ("| 항목 | 내용 | A | B | 처리 | 근거 |\n| --- | --- | --- | --- | --- | --- |\n"
                 "| 1 | a \\| b | A-1 | B-5 | 채택 | |\n| 2 | c | A-2(참고) | - | 채택(문서) | |\n"
                 "| 3 | d | - | B-1 | 기각 | |\n")
        summary, errors = collect.summarize_review(table)
        self.assertEqual(errors, [])
        self.assertEqual((summary["findings"], summary["adopted"], summary["rejected"], summary["duplicates"]),
                         (3, 2, 1, 1))
        found = FOUND_TABLE.replace("A F1, B F2", "A-3, B-4")
        self.assertEqual(collect.summarize_review(found)[0]["duplicates"], 1)

    def test_unprocessed_rows_are_errors(self):
        """리뷰 F2: 내용이 있는데 처리가 비었거나 칸이 모자란 행은 조용히 빠지지 않는다"""
        _, errors = collect.summarize_review(FOUND_TABLE.replace("| 기각 |", "|  |"))
        self.assertEqual(errors, ["처리가 비었다: U2"])
        _, errors = collect.summarize_review(FOUND_TABLE.replace("| U2 | b | F3 | 기각 | |", "| U2 | b |"))
        self.assertEqual(errors, ["칸이 모자란 행: U2"])

    def test_fenced_example_table_is_ignored(self):
        """#110 Codex 리뷰: 발견 사항이 인용한 표 예시(코드 블록, 7장 앞)를 처리 표로 읽지 않는다"""
        text = "\n".join([
            "## 3. 발견 사항", "", "```markdown", "| # | 결함 | 발견 | 처리 | 근거 |", "| --- | --- | --- | --- | --- |",
            "| U1 | | | | |", "```", "", "| # | 결함 | 발견 | 처리 | 근거 |", "| --- | --- | --- | --- | --- |",
            "| U9 | 예시 | F9 | 기각 | 7장 앞의 표 |", "",
            "## 7. 처리", "", "| # | 결함 | 발견 | 처리 | 근거 |", "| --- | --- | --- | --- | --- |",
            "| U1 | 실제 결함 | F1 | 채택·수정 | 고침 |"])
        summary, errors = collect.summarize_review(text)
        self.assertEqual((summary["findings"], summary["adopted"], summary["rejected"], errors), (1, 1, 0, []))

    def test_template_has_disposition_table(self):
        template = (ROOT / "core" / "templates" / "docs" / "templates" / "review.md").read_text(encoding="utf-8")
        summary, errors = collect.summarize_review(template)
        self.assertEqual((summary["findings"], errors), (0, []))  # 빈 행만 있는 템플릿


class ReviewSectionTest(unittest.TestCase):
    def test_roundtrip_and_parse_cases(self):
        summary, _ = collect.summarize_review(STRICT_TABLE)
        text = collect.review_lines(summary)
        parsed = judge().review(body("## 리뷰 결과", "", text, "| 항목 | 값 |", "| --- | --- |"))
        self.assertEqual(parsed, summary)
        self.assertIsNone(judge().review(body("## 리뷰 결과", "", "해당 없음")))
        self.assertIsNone(judge().review(body("## 리뷰 결과", "<!--", text, "-->")))
        only_found = judge().review(body("## 리뷰 결과", "- 리뷰 발견: 3 (채택 3 · 기각 0 · 후속 0 · 판단 필요 0)"))
        self.assertEqual(only_found["duplicates"], 0)

    def test_inconsistent_lines_are_unreported(self):
        """리뷰 F4: 총계와 처리별 합이 다르거나 중복이 총계보다 크면 미보고, 같은 줄은 처음 값"""
        j = judge()
        self.assertIsNone(j.review(body("## 리뷰 결과", "- 리뷰 발견: 5 (채택 1 · 기각 0 · 후속 0 · 판단 필요 0)")))
        self.assertIsNone(j.review(body("## 리뷰 결과", "- 리뷰 발견: 1 (채택 1 · 기각 0 · 후속 0 · 판단 필요 0)",
                                        "- 독립 중복 발견: 2")))
        first = j.review(body("## 리뷰 결과", "- 리뷰 발견: 2 (채택 2 · 기각 0 · 후속 0 · 판단 필요 0)",
                              "- 독립 중복 발견: 1", "- 독립 중복 발견: 0"))
        self.assertEqual(first["duplicates"], 1)

    def test_through_collect(self):
        """리뷰 F6: MR 본문의 두 줄이 collect를 지나 MR 원자료와 주별 지표 8에 들어간다(엄격 여부 포함)"""
        from test_metrics_collect import fixture, gitlab_api, run_collect
        data = fixture("gitlab.json")
        mr11 = next(mr for mr in data["merge_requests"] if mr["iid"] == 11)  # 재판정 엄격
        mr11["description"] = (mr11.get("description") or "") + \
            "\n\n## 리뷰 결과\n\n- 리뷰 발견: 3 (채택 2 · 기각 1 · 후속 0 · 판단 필요 0)\n- 독립 중복 발견: 1\n"
        code, result, _, err = run_collect("gitlab", gitlab_api(data))
        self.assertEqual(code, 0, err)
        first = next(w for w in result["weeks"] if w["week"] == "2026-W39")
        self.assertEqual(first["review"], {"reported_mrs": 1, "strict_reported_mrs": 1, "strict_adopted": 2, "findings": 3,
                                           "adopted": 2,
                                           "rejected": 1, "followup": 0, "pending": 0, "duplicates": 1})
        self.assertEqual(next(m for m in result["mrs"] if m["number"] == 11)["review"]["adopted"], 2)

    def test_week_totals(self):
        week = collect.empty_week_review()
        collect.add_week_review(week, {"findings": 5, "adopted": 2, "rejected": 1, "followup": 1, "pending": 1,
                                       "duplicates": 1}, True)
        collect.add_week_review(week, {"findings": 1, "adopted": 1, "rejected": 0, "followup": 0, "pending": 0,
                                       "duplicates": 0}, False)
        collect.add_week_review(week, None, True)
        self.assertEqual(week, {"reported_mrs": 2, "strict_reported_mrs": 1, "strict_adopted": 2, "findings": 6,
                                "adopted": 3, "rejected": 1, "followup": 1, "pending": 1, "duplicates": 1})


class ReviewCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-review-"))
        self.addCleanup(remove_tree, self.tmp)

    def test_cli(self):
        path = self.tmp / "12-review.md"
        path.write_text(STRICT_TABLE, encoding="utf-8")
        code, out, err = run_cli(["review", "summary", str(path)])
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "- 리뷰 발견: 5 (채택 2 · 기각 1 · 후속 1 · 판단 필요 1)\n- 독립 중복 발견: 1\n")
        path.write_text("# 리뷰\n", encoding="utf-8")
        code, out, err = run_cli(["review", "summary", str(path)])
        self.assertEqual((code, out), (2, ""))
        self.assertIn("처리 열이 있는 표가 없다", err)
        self.assertEqual(run_cli(["review", "summary", str(self.tmp / "none.md")])[0], 2)


if __name__ == "__main__":
    unittest.main()
