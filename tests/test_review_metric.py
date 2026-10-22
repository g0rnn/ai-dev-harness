"""#99 M5-4 리뷰 처리 표 → MR 본문 `## 리뷰 결과` 요약 → 지표 8."""

import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import process_support  # noqa: E402,F401 — test-only Windows child launch policy
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
        self.assertIn("| # | 결함 | 심각도 | 발견 | 처리 | 근거 |", template)  # #135 T6


SEVERITY_TABLE = """## 7. 처리 (작성자가 반영 뒤 채운다)

플랜 승인: 해당 없음

| # | 결함 | 심각도 | 발견 | 처리 | 근거 |
| --- | --- | --- | --- | --- | --- |
| U1 | 토큰 누출 | 높음(보안) | R1 F2, R2 F1 | 채택 | |
| U2 | 경계 누락 | 중간 | R1 F3 | 채택·수정 | |
| U3 | 문서 오타 | 낮음 | R2 F4 | 기각 | |
| U4 | 이름 제안 | 참고 | F5 | 후속 #12 | |
| U5 | 표기 없음 | 중간 | | 판단 필요 | |
| U6 | | | | | |
"""


class CommittedReviewParseTest(unittest.TestCase):
    """#135: 심각도 칸, R1·R2 표기, 발견 세션별 수(detail). 본문 두 줄의 형태는 그대로다."""

    def test_r1_r2_found_column(self):
        """T1: `R1 F2, R2 F1`은 독립 중복, `R1`만·`R2`만은 한쪽, `R12`·`AR1` 같은 다른 낱말은 세션 표기가 아니다"""
        summary, errors = collect.summarize_review(SEVERITY_TABLE, detail=True)
        self.assertEqual(errors, [])
        self.assertEqual(summary["duplicates"], 1)
        self.assertEqual(summary["finder"], {"single": 1, "first": 1, "second": 1, "both": 1, "unknown": 1})
        other = SEVERITY_TABLE.replace("R1 F2, R2 F1", "R12 F2, XR2 F1")
        summary, _ = collect.summarize_review(other, detail=True)
        self.assertEqual((summary["duplicates"], summary["finder"]["both"], summary["finder"]["single"]), (0, 0, 2))
        dashed = SEVERITY_TABLE.replace("R1 F2, R2 F1", "R1-2, R2-1")
        self.assertEqual(collect.summarize_review(dashed)[0]["duplicates"], 1)

    def test_r1_r2_columns(self):
        """T2: 엄격 종합 표의 `R1`·`R2` 열도 `A`·`B` 열과 같은 규칙"""
        table = STRICT_TABLE.replace("| A | B |", "| R1 | R2 |")
        summary, errors = collect.summarize_review(table, detail=True)
        self.assertEqual(errors, [])
        self.assertEqual(summary["duplicates"], 1)
        self.assertEqual(summary["finder"], {"single": 0, "first": 2, "second": 2, "both": 1, "unknown": 0})
        self.assertEqual(collect.summarize_review(STRICT_TABLE, detail=True)[0]["finder"], summary["finder"])

    def test_severity_counts(self):
        """T3: 심각도·처리·발견 세션별 수. 심각도 칸은 접두로 읽는다(`높음(보안)`)"""
        summary, errors = collect.summarize_review(SEVERITY_TABLE, detail=True)
        self.assertEqual(errors, [])
        self.assertEqual(summary["severity"], {"high": 1, "medium": 2, "low": 1, "info": 1, "unknown": 0})
        self.assertEqual((summary["findings"], summary["adopted"], summary["rejected"], summary["followup"],
                          summary["pending"]), (5, 2, 1, 1, 1))

    def test_severity_errors(self):
        """T4: 심각도 열이 있는데 비었거나 다른 값이면 행 번호가 든 오류(잘못 세지 않는다)"""
        _, errors = collect.summarize_review(SEVERITY_TABLE.replace("| 낮음 |", "|  |"))
        self.assertEqual(errors, ["심각도가 비었거나 높음·중간·낮음·참고로 시작하지 않는다: U3 "])
        _, errors = collect.summarize_review(SEVERITY_TABLE.replace("| 참고 |", "| 치명 |"))
        self.assertEqual(len(errors), 1)
        self.assertIn("U4 치명", errors[0])

    def test_legacy_table_without_severity(self):
        """T5: 심각도 열이 없는 옛 표는 그대로 읽고 심각도는 모두 미기재, detail 없이는 기존 요약 그대로"""
        summary, errors = collect.summarize_review(FOUND_TABLE, detail=True)
        self.assertEqual(errors, [])
        self.assertEqual(summary["severity"], {"high": 0, "medium": 0, "low": 0, "info": 0, "unknown": 2})
        self.assertEqual(summary["finder"], {"single": 1, "first": 0, "second": 0, "both": 1, "unknown": 0})
        self.assertEqual(set(collect.summarize_review(FOUND_TABLE)[0]),
                         {"findings", "adopted", "rejected", "followup", "pending", "duplicates"})

    def test_detail_lines(self):
        summary, _ = collect.summarize_review(SEVERITY_TABLE, detail=True)
        self.assertEqual(collect.review_detail_lines(summary),
                         "- 심각도: 높음 1 · 중간 2 · 낮음 1 · 참고 1 · 미기재 0\n"
                         "- 발견 세션: 단일 세션 1 · 첫 세션만 1 · 둘째 세션만 1 · 둘 다 1 · 기록 없음 1\n")

    def test_review_path_rules(self):
        """T13: `docs/reviews/<키>.md`만 리뷰 파일이다. 브랜치 키는 `/` 뒤 키와 구분자로 맞춘다"""
        self.assertEqual(collect.review_file_key("docs/reviews/135.md"), "135")
        self.assertEqual(collect.review_file_key("docs/reviews/KKUGI-12.md"), "KKUGI-12")
        for path in ("docs/reviews/README.md", "docs/reviews/readme.md", "docs/reviews/a/1.md", "docs/reviews/../x.md",
                     "docs/reviews/.1.md", "plans/1-review.md", "docs/reviews/1.txt", None):
            self.assertIsNone(collect.review_file_key(path), path)
        self.assertTrue(collect.branch_has_key("feat/135-review-commit", "135"))
        self.assertTrue(collect.branch_has_key("feat/kkugi-12-x", "KKUGI-12"))
        self.assertTrue(collect.branch_has_key("feat/11", "11"))
        for branch in ("feat/1135-x", "feat/135x", "feat/x-135", None):
            self.assertFalse(collect.branch_has_key(branch, "135"), branch)


class CommittedReviewCollectTest(unittest.TestCase):
    """T11·T12: 수집기가 MR과 함께 커밋된 리뷰 파일을 그 MR의 병합 커밋(API)에서 읽는다."""

    def run_gitlab(self, diffs, files, shas=None):
        """files: {(경로, ref): bytes}. 그 밖의 요청은 404."""
        from test_metrics_collect import fixture, gitlab_api, run_collect
        data = fixture("gitlab.json")
        for number, paths in diffs.items():
            data["diffs"].setdefault(number, []).extend({"old_path": p, "new_path": p} for p in paths)
        for mr in data["merge_requests"]:
            mr.update((shas or {}).get(mr["iid"], {}))
        api = gitlab_api(data)
        self.requested = []

        def raw(query, encoded):
            key = (urllib.parse.unquote(encoded), query.get("ref", [""])[0])
            self.requested.append(key)
            return collect.Response(200, {}, files[key]) if key in files else collect.Response(404)

        api.routes.insert(0, (r"gitlab\.example/api/v4/projects/1/repository/files/([^/]+)/raw", raw))
        code, result, _, err = run_collect("gitlab", api)
        self.assertEqual(code, 0, err)
        return result, {mr["number"]: mr for mr in result["mrs"]}, next(w for w in result["weeks"]
                                                                         if w["week"] == "2026-W39")

    def test_collect_links_by_branch_key(self):
        """병합 커밋에서 읽는다(통합 브랜치에 병합돼 기본 브랜치에 아직 없는 MR도 센다, 리뷰 F1)"""
        result, mrs, first = self.run_gitlab({"11": ["docs/reviews/11.md"]},
                                             {("docs/reviews/11.md", "m11"): SEVERITY_TABLE.encode("utf-8")},
                                             {11: {"merge_commit_sha": "m11"}})
        self.assertEqual(self.requested, [("docs/reviews/11.md", "m11")])  # 리뷰 파일을 바꾼 MR만 더 부른다
        self.assertEqual(mrs[11]["review_file"]["status"], "ok")
        self.assertEqual(mrs[11]["review_file"]["path"], "docs/reviews/11.md")
        self.assertEqual(mrs[11]["review_file"]["severity"]["medium"], 2)
        self.assertIsNone(mrs[10]["review_file"])
        self.assertEqual(first["review_files"], {
            "mrs": 1, "missing": 0, "invalid": 0, "findings": 5, "adopted": 2, "rejected": 1, "followup": 1, "pending": 1,
            "duplicates": 1, "severity": {"high": 1, "medium": 2, "low": 1, "info": 1, "unknown": 0},
            "finder": {"single": 1, "first": 1, "second": 1, "both": 1, "unknown": 1}})
        # 본문 두 줄의 지표 8은 그대로(이 MR들 본문에는 리뷰 결과가 없다)
        self.assertEqual(first["review"]["reported_mrs"], 0)
        self.assertNotIn("토큰 누출", json.dumps(result, ensure_ascii=False))  # 결함 요지는 출력에 옮기지 않는다

    def test_same_key_mrs_read_their_own_merge(self):
        """리뷰 F2: 같은 키의 리뷰 파일을 뒤 MR이 덮어써도 각 MR은 자기 병합 커밋의 내용을 센다(squash 커밋도 쓴다)"""
        later = SEVERITY_TABLE.replace("| U2 | 경계 누락 | 중간 |", "| U2 | 경계 누락 | 높음 |")
        _, mrs, first = self.run_gitlab(
            {"10": ["docs/reviews/10.md"], "12": ["docs/reviews/10.md"]},
            {("docs/reviews/10.md", "s10"): SEVERITY_TABLE.encode("utf-8"),
             ("docs/reviews/10.md", "m12"): later.encode("utf-8")},
            {10: {"squash_commit_sha": "s10"}, 12: {"merge_commit_sha": "m12", "source_branch": "fix/10-again"}})
        self.assertEqual((mrs[10]["review_file"]["severity"]["high"], mrs[12]["review_file"]["severity"]["high"]), (1, 2))
        self.assertEqual(first["review_files"]["mrs"], 2)

    def test_unlinked_missing_invalid(self):
        """키가 다른 파일은 잇지 않고, 없는 파일은 missing, 표가 틀리거나 UTF-8이 아니거나 1 MB를 넘으면 invalid(리뷰 F4)"""
        bad = SEVERITY_TABLE.replace("| 참고 |", "| 치명 |").encode("utf-8")
        _, mrs, first = self.run_gitlab(
            {"10": ["docs/reviews/11.md", "docs/reviews/10.md", "docs/reviews/README.md"], "12": ["docs/reviews/12.md"]},
            {("docs/reviews/10.md", "develop"): bad, ("docs/reviews/11.md", "develop"): SEVERITY_TABLE.encode("utf-8")})
        self.assertEqual(mrs[10]["review_file"], {"path": "docs/reviews/10.md", "status": "invalid"})  # 11.md는 키가 다르다
        self.assertEqual(mrs[12]["review_file"], {"path": "docs/reviews/12.md", "status": "missing"})
        self.assertIsNone(mrs[11]["review_file"])
        self.assertEqual((first["review_files"]["mrs"], first["review_files"]["missing"],
                          first["review_files"]["invalid"], first["review_files"]["findings"]), (0, 1, 1, 0))
        self.assertNotIn(("docs/reviews/README.md", "develop"), self.requested)
        reader = {"docs/reviews/1.md": b"\xff\xfe broken", "docs/reviews/2.md": b"|" * (collect.REVIEW_FILE_MAX_BYTES + 1)}
        for path in reader:
            with self.subTest(path=path):
                self.assertEqual(collect.committed_review([path], "feat/" + path[-4], reader.get),
                                 {"path": path, "status": "invalid"})

    def test_github_contents(self):
        """GitHub는 contents API(base64)를 PR 병합 커밋(없으면 대상 브랜치)에서 읽는다"""
        from test_metrics_collect import github_api, json_response, run_collect
        api = github_api()
        api.routes.insert(0, (r"api\.github\.example/repos/owner/demo/pulls/7/files",
                              lambda q: json_response([{"filename": "src/app.py"}, {"filename": "docs/reviews/7.md"}])))
        encoded = base64.b64encode(SEVERITY_TABLE.encode("utf-8")).decode("ascii")
        api.routes.insert(0, (r"api\.github\.example/repos/owner/demo/contents/docs/reviews/7\.md",
                              lambda q: json_response({"encoding": "base64", "content": encoded})
                              if q.get("ref") == ["main"] else collect.Response(404)))
        code, result, _, err = run_collect("github", api)
        self.assertEqual(code, 0, err)
        mr = next(m for m in result["mrs"] if m["number"] == 7)
        self.assertEqual((mr["review_file"]["status"], mr["review_file"]["findings"]), ("ok", 5))


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

    def git_repo(self, name="repo"):
        repo = self.tmp / name
        (repo / "plans").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        return repo

    def test_save_copies_final_review(self):
        """T7: `--save`는 출력 두 줄을 그대로 내고 같은 바이트를 그 저장소의 `docs/reviews/<키>.md`에 쓰고 경로를 알린다"""
        repo = self.git_repo()
        path = repo / "plans" / "12-review.md"
        path.write_bytes(SEVERITY_TABLE.replace("\n", "\r\n").encode("utf-8"))
        code, out, err = run_cli(["review", "summary", str(path), "--save"], cwd=repo)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "- 리뷰 발견: 5 (채택 2 · 기각 1 · 후속 1 · 판단 필요 1)\n- 독립 중복 발견: 1\n")
        self.assertEqual((repo / "docs" / "reviews" / "12.md").read_bytes(), path.read_bytes())
        self.assertIn("최종 리뷰 파일을 썼다", err)
        self.assertIn(str(Path("docs") / "reviews" / "12.md"), err)
        path.write_text(SEVERITY_TABLE.replace("| 참고 |", "| 낮음 |"), encoding="utf-8")  # 고친 뒤 다시 저장하면 덮어쓴다
        self.assertEqual(run_cli(["review", "summary", str(path), "--save"], cwd=repo / "plans")[0], 0)
        self.assertIn("| 낮음 | F5 |", (repo / "docs" / "reviews" / "12.md").read_text(encoding="utf-8"))
        self.assertFalse((repo / "plans" / "docs").exists())  # 리뷰 F3: plans/ 안에서 실행해도 저장소 루트에 쓴다

    def test_save_uses_review_files_repo(self):
        """리뷰 F3: 다른 디렉터리(키트 등)에서 실행해도 리뷰 파일이 든 저장소에 쓰고, git 저장소 밖 파일은 거절한다"""
        repo, elsewhere = self.git_repo(), self.tmp / "elsewhere"
        elsewhere.mkdir()
        path = repo / "plans" / "12-review.md"
        path.write_text(SEVERITY_TABLE, encoding="utf-8")
        self.assertEqual(run_cli(["review", "summary", str(path), "--save"], cwd=elsewhere)[0], 0)
        self.assertTrue((repo / "docs" / "reviews" / "12.md").is_file())
        self.assertFalse((elsewhere / "docs").exists())
        outside = self.tmp / "12-review.md"
        outside.write_text(SEVERITY_TABLE, encoding="utf-8")
        with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.tmp)}):
            code, out, err = run_cli(["review", "summary", str(outside), "--save"], cwd=elsewhere)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("git 저장소 안의 리뷰 파일만", err)

    def test_save_refuses_raw_and_invalid(self):
        """T8: 엄격 원본(`-review-A.md`)·이름 규칙 밖·표가 틀린 파일은 종료 2이고 파일을 만들지 않는다"""
        repo = self.git_repo()
        for name, text in (("12-review-A.md", SEVERITY_TABLE), ("notes.md", SEVERITY_TABLE),
                           ("13-review.md", SEVERITY_TABLE.replace("| 참고 |", "| 치명 |"))):
            with self.subTest(name=name):
                path = repo / "plans" / name
                path.write_text(text, encoding="utf-8")
                code, out, err = run_cli(["review", "summary", str(path), "--save"], cwd=repo)
                self.assertEqual((code, out), (2, ""))
                self.assertFalse((repo / "docs").exists())
        self.assertIn("--save는 최종 리뷰 파일", run_cli(["review", "summary", str(repo / "plans" / "notes.md"), "--save"],
                                                       cwd=repo)[2])

    def test_detail(self):
        """T9: `--detail`은 두 줄 뒤에 심각도·발견 세션 줄"""
        path = self.tmp / "12-review.md"
        path.write_text(SEVERITY_TABLE, encoding="utf-8")
        code, out, err = run_cli(["review", "summary", str(path), "--detail"], cwd=self.tmp)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines()[2:], ["- 심각도: 높음 1 · 중간 2 · 낮음 1 · 참고 1 · 미기재 0",
                                                "- 발견 세션: 단일 세션 1 · 첫 세션만 1 · 둘째 세션만 1 · 둘 다 1 · 기록 없음 1"])
        self.assertFalse((self.tmp / "docs").exists())  # --save 없이는 파일을 쓰지 않는다


if __name__ == "__main__":
    unittest.main()
