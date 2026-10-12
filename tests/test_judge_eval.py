"""`harness judge --eval`와 `harness metrics cases` 테스트, 키트 시드 eval 세트 회귀."""

import importlib.util
import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("harness", ROOT / "bin" / "harness.py")
harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)

KIT_CASES = ROOT / "tests" / "fixtures" / "judge-eval" / "kit.jsonl"


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = harness.main(argv)
    return code, out.getvalue(), err.getvalue()


class EvalTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-eval-"))
        self.repo = self.tmp / "repo"
        code, _, err = run(["init", str(self.repo), "--platform", "github", "--tracker", "github",
                            "--integration-branch", "main"])
        self.assertEqual(code, 0, err)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cases(self, *lines):
        path = self.tmp / "cases.jsonl"
        path.write_text("".join((line if isinstance(line, str) else json.dumps(line)) + "\n" for line in lines),
                        encoding="utf-8")
        return path

    def evaluate(self, *lines, extra=()):
        return run(["judge", str(self.repo), "--eval", str(self.cases(*lines)), *extra])


class JudgeEvalTest(EvalTestBase):
    def test_all_match(self):
        code, out, err = self.evaluate(
            {"id": "a", "paths": ["docs/x.md"], "expected": "lite"},
            {"id": "b", "paths": ["src/app.py", "tests/test_app.py"], "expected": "standard"},
            {"id": "c", "paths": [".github/workflows/deploy.yml"], "expected": "strict", "source": "https://x/1"},
        )
        self.assertEqual(code, 0, err)
        self.assertIn("eval: 3/3 일치 (100.0%)", out)
        self.assertNotIn("불일치:", out)

    def test_mismatch_exits_1_and_names_decider(self):
        code, out, _ = self.evaluate(
            {"id": "pr-1", "paths": ["docs/x.md", "src/app.py"], "expected": "lite", "note": "사람 확정"},
            {"id": "pr-2", "paths": ["docs/y.md"], "expected": "lite"},
        )
        self.assertEqual(code, 1)
        self.assertIn("eval: 1/2 일치 (50.0%)", out)
        self.assertIn("pr-1: 정답 lite, 판정 standard  src/app.py  default", out)

    def test_json_confusion_and_unlabeled(self):
        code, out, _ = self.evaluate(
            {"id": "a", "paths": ["src/a.py"], "expected": "strict"},
            {"id": "b", "paths": ["src/b.py"], "expected": None, "note": "불일치"},
            "",
            {"id": "c", "paths": ["docs/c.md"], "expected": "lite"},
            extra=("--json",),
        )
        self.assertEqual(code, 1)
        report = json.loads(out)
        self.assertEqual((report["total"], report["matched"], report["unlabeled"]), (2, 1, 1))
        self.assertEqual(report["accuracy"], 0.5)
        self.assertEqual(report["confusion"]["strict"], {"lite": 0, "standard": 1, "strict": 0})
        self.assertEqual(report["confusion"]["lite"]["lite"], 1)
        self.assertEqual(report["mismatches"], [{"id": "a", "expected": "strict", "judged": "standard",
                                                 "path": "src/a.py", "rule": "default"}])

    def test_only_unlabeled(self):
        code, out, _ = self.evaluate({"id": "a", "paths": ["src/a.py"], "expected": None})
        self.assertEqual(code, 0)
        self.assertIn("eval: 0/0 일치 (-), 정답 미확정 1건 제외", out)

    def test_invalid_cases(self):
        bad = [
            ("{oops", "2: JSON 객체가 아니다"),
            ("[1]", "2: JSON 객체가 아니다"),
            ({"id": "x", "paths": ["a"], "expected": "lite", "extra": 1}, "알 수 없는 키: extra"),
            ({"id": "", "paths": ["a"], "expected": "lite"}, "id는"),
            ({"id": "ok", "paths": ["a"], "expected": "lite"}, "id가 중복된다: ok"),
            ({"id": "x", "paths": [], "expected": "lite"}, "paths는"),
            ({"id": "x", "paths": ["a", 3], "expected": "lite"}, "paths는"),
            ({"id": "x", "paths": ["a"]}, "expected가 없다"),
            ({"id": "x", "paths": ["a"], "expected": "엄격"}, "expected는"),
            ({"id": "x", "paths": ["a"], "expected": "lite", "note": 3}, "note는"),
        ]
        for line, message in bad:
            with self.subTest(message=message):
                code, _, err = self.evaluate({"id": "ok", "paths": ["a"], "expected": "standard"}, line)
                self.assertEqual(code, 2)
                self.assertIn(message, err)
                self.assertIn("cases.jsonl:2", err)  # 줄 번호를 알린다

    def test_missing_file_and_exclusive_with_files(self):
        code, _, err = run(["judge", str(self.repo), "--eval", str(self.tmp / "none.jsonl")])
        self.assertEqual(code, 2)
        self.assertIn("eval 사례 파일을 읽을 수 없다", err)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            harness.main(["judge", str(self.repo), "--eval", "a", "--files", "b"])


class KitSeedEvalTest(unittest.TestCase):
    """키트 PR 이력에서 본문 판정과 재판정이 일치한 사례. 판정 규칙을 바꿔 결과가 달라지면 사례마다 다시 검토한다."""

    def test_seed_set_has_no_mismatch(self):
        code, out, err = run(["judge", "--self", "--eval", str(KIT_CASES)])
        self.assertEqual(code, 0, out + err)

    def test_seed_set_shape(self):
        lines = KIT_CASES.read_text(encoding="utf-8").splitlines()
        cases = [json.loads(line) for line in lines]
        self.assertGreaterEqual(len(cases), 20)
        ids = [int(case["id"].removeprefix("pr-")) for case in cases]
        self.assertEqual(ids, sorted(set(ids)))  # PR 번호 순, 중복 없음
        for case in cases:
            self.assertLessEqual(set(case), {"id", "paths", "expected", "source", "note"})
            self.assertIn(case["expected"], ("lite", "standard"))


class MetricsCasesTest(EvalTestBase):
    def metrics(self, mrs, version=1):
        path = self.tmp / "metrics.json"
        path.write_text(json.dumps({"version": version, "mrs": mrs}), encoding="utf-8")
        return path

    def test_cases_from_mrs(self):
        source = self.metrics([
            {"number": 10, "url": "https://git.example/mr/10", "integration": False, "paths": ["docs/a.md"],
             "tier": {"body": "lite", "judge": "lite", "effective": "lite", "mismatch": False}},
            {"number": 11, "url": "https://git.example/mr/11", "integration": False, "paths": ["src/a.py"],
             "tier": {"body": "lite", "judge": "standard", "effective": "standard", "mismatch": True}},
            {"number": 12, "integration": False, "paths": ["src/b.py"],
             "tier": {"body": None, "judge": "standard", "effective": "standard", "mismatch": False}},
            {"number": 13, "integration": True, "tier": None},
            {"number": 14, "integration": False, "files": 2,
             "tier": {"body": "lite", "judge": "lite", "effective": "lite", "mismatch": False}},
        ])
        out_path = self.tmp / "out" / "cases.jsonl"
        code, _, err = run(["metrics", "cases", "--in", str(source), "--out", str(out_path)])
        self.assertEqual(code, 0, err)
        self.assertIn("eval 사례 후보 3건(정답 미확정 2건)", err)
        self.assertIn("2건은 뺐다", err)
        cases = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(cases[0], {"id": "mr-10", "paths": ["docs/a.md"], "expected": "lite",
                                    "source": "https://git.example/mr/10"})
        self.assertIsNone(cases[1]["expected"])
        self.assertEqual(cases[1]["note"], "불일치: 본문 lite / 재판정 standard — 사람이 정답을 정한다")
        self.assertEqual(cases[2]["note"], "본문 판정 없음 — 사람이 정답을 정한다")
        self.assertNotIn("source", cases[2])
        # 산출물은 그대로 judge --eval 입력이 된다
        code, out, _ = run(["judge", str(self.repo), "--eval", str(out_path)])
        self.assertEqual(code, 0, out)
        self.assertIn("eval: 1/1 일치 (100.0%), 정답 미확정 2건 제외", out)

    def test_stdout_and_bad_input(self):
        code, out, _ = run(["metrics", "cases", "--in", str(self.metrics([]))])
        self.assertEqual((code, out), (0, ""))
        code, _, err = run(["metrics", "cases", "--in", str(self.metrics([], version=2))])
        self.assertEqual(code, 2)
        self.assertIn("version 1", err)
        broken = self.tmp / "broken.json"
        broken.write_text("{", encoding="utf-8")
        code, _, err = run(["metrics", "cases", "--in", str(broken)])
        self.assertEqual(code, 2)
        self.assertIn("JSON이 아니다", err)

    def test_out_failure_keeps_existing_file(self):
        existing = self.tmp / "cases.jsonl"
        existing.write_text("old\n", encoding="utf-8")
        blocker = self.tmp / "file"
        blocker.write_text("", encoding="utf-8")
        code, _, err = run(["metrics", "cases", "--in", str(self.metrics([])), "--out", str(blocker / "x.jsonl")])
        self.assertEqual(code, 2)  # 트레이스백 대신 harness 오류
        self.assertIn("사례 파일을 쓸 수 없다", err)
        code, _, _ = run(["metrics", "cases", "--in", str(self.metrics([])), "--out", str(existing)])
        self.assertEqual((code, existing.read_text(encoding="utf-8")), (0, ""))  # 교체로 쓴다


if __name__ == "__main__":
    unittest.main()
