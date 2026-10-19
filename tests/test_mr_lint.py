"""MR 본문 lint(M2-2, core/ci/mr-lint/mr_lint.py)와 GitLab 조각·키트 workflow job 구조."""

import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tempdirs import remove_tree  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("mr_lint", ROOT / "core" / "ci" / "mr-lint" / "mr_lint.py")
lint = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lint)
HARNESS_SPEC = importlib.util.spec_from_file_location("harness_for_mr_lint", ROOT / "bin" / "harness.py")
harness = importlib.util.module_from_spec(HARNESS_SPEC)
HARNESS_SPEC.loader.exec_module(harness)
COMMON = ROOT / "core" / "hooks" / "harness_common.py"
FRAGMENT = ROOT / "core" / "ci" / "gitlab" / "mr-lint.yml"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

GITLAB_CONFIG = {"project_name": "demo", "platform": "gitlab", "tracker": "jira", "issue_prefix": "DEMO",
                 "default_branch": "main", "integration_branch": "develop"}


def template(platform="gitlab"):
    config = dict(GITLAB_CONFIG, platform=platform)
    ctx = harness.build_context(config)
    text = (harness.TEMPLATES_DIR / "merge-request" / "Default.md").read_text(encoding="utf-8")
    return harness.render(text, ctx)


def filled(tier="standard", strict_parts=False):
    """새 템플릿을 채운 본문. strict_parts면 플랜 요약·리뷰 결과와 측정 칸도 채운다."""
    body = template()
    # 템플릿의 예시 업무 키(#52, DEMO-52)는 참조로 인정하지 않으므로 실제 키로 바꾼다
    body = body.replace("Closes #52", "Closes #7").replace("Closes DEMO-52", "Closes DEMO-7")
    body = body.replace("## 판정\n", "## 판정\n").replace(
        "-->\n\n-\n\n## 배경과 목적", f"-->\n\n- {tier} — 근거 한 줄\n\n## 배경과 목적")
    body = body.replace("- 방법:", "- 방법: unittest").replace("- 결과:", "- 결과: 통과").replace(
        "- 검증하지 못한 것과 남은 위험:", "- 검증하지 못한 것과 남은 위험: 없음")
    body = body.replace("- [ ] 해당 없음", "- [x] 해당 없음")
    if strict_parts:
        body = body.replace("-->\n\n해당 없음\n\n## 검토가 필요한 결정",
                            "-->\n\n- 설계 결정 두 개, 인수 테스트 T1~T5\n\n## 검토가 필요한 결정")
        body = body.replace("-->\n\n해당 없음\n\n| 항목", "-->\n\n- W2 리뷰 A·B, 발견 2건 모두 반영\n\n| 항목")
        for row, value in (("읽은 기준 문서 절 수 |", "6"), ("(동시성·트랜잭션 / 인가) |", "닫힘 / 열림"),
                           ("(참고 제외 / 참고) |", "2 / 1"), ("리뷰 세션 모델 |", "Claude Opus 5.5"),
                           ("(병합 후 기입) |", "병합 전")):
            body = body.replace(f"{row} |", f"{row} {value} |")
    return body


class BodyLintTest(unittest.TestCase):
    def test_valid_standard_body_passes(self):
        """T1"""
        result = lint.lint_body(filled(), "standard")
        self.assertEqual(result["failures"], [])
        self.assertEqual(result["tier"], {"body": "standard", "judge": "standard", "effective": "standard",
                                          "mismatch": False})

    def test_rendered_template_fails_each_rule(self):
        """T2: 템플릿을 채우지 않으면 판정·검증 세 줄·영향 범위가 모두 실패한다(예시 Closes 줄은 남아 있다)."""
        for platform in ("gitlab", "github"):
            failures = " ".join(lint.lint_body(template(platform), None)["failures"])
            for needle in ("'방법' 값", "'결과' 값", "'미검증' 값", "`## 영향 범위`에 체크", "`## 판정` 값"):
                self.assertIn(needle, failures, platform)

    def test_closes_or_refs_required(self):
        """T3"""
        body = filled().replace("- Closes DEMO-7", "-")
        self.assertIn("`Closes` 또는 `Refs`", " ".join(lint.lint_body(body, None)["failures"]))
        hidden = filled().replace("- Closes DEMO-7", "<!-- Closes DEMO-7 -->")
        self.assertIn("`Closes` 또는 `Refs`", " ".join(lint.lint_body(hidden, None)["failures"]))
        empty = filled().replace("- Closes DEMO-7", "- Closes")
        self.assertIn("`Closes` 또는 `Refs`", " ".join(lint.lint_body(empty, None)["failures"]))
        refs = filled().replace("- Closes DEMO-7", "- refs DEMO-1")
        self.assertEqual(lint.lint_body(refs, None)["failures"], [])

    def test_example_or_empty_reference_rejected(self):
        """리뷰 F1·F3: 템플릿 예시 키, '없음', 코드 블록 안의 참조는 참조로 보지 않는다."""
        examples = lint.example_references({"tracker": "jira", "issue_prefix": "DEMO"})
        self.assertEqual(examples, ("DEMO-52",))
        self.assertEqual(lint.example_references({"tracker": "github"}), ("#52",))
        for line in ("- Closes DEMO-52", "- Closes 없음", "- Refs 해당 없음", "- Closes N/A"):
            with self.subTest(line=line):
                body = filled().replace("- Closes DEMO-7", line)
                self.assertIn("`Closes` 또는 `Refs`", " ".join(lint.lint_body(body, None, examples)["failures"]))
        fenced = filled().replace("- Closes DEMO-7", "```\nCloses DEMO-9\n```")
        self.assertIn("`Closes` 또는 `Refs`", " ".join(lint.lint_body(fenced, None, examples)["failures"]))
        self.assertEqual(lint.lint_body(filled().replace("- Closes DEMO-7", "- Closes DEMO-52, DEMO-8"),
                                        None, examples)["failures"], [])

    def test_fenced_checkbox_not_counted(self):
        """리뷰 F3: 코드 블록 안의 체크 상자는 영향 범위 체크로 보지 않는다."""
        body = filled().replace("- [x] 해당 없음", "- [ ] 해당 없음\n```\n- [x] 해당 없음\n```")
        self.assertIn("영향 범위", " ".join(lint.lint_body(body, None)["failures"]))

    def test_strict_not_applicable_variants(self):
        """리뷰 F2: 엄격으로 올라갔는데 '해당 없음' 변형이나 측정 표만 있으면 실패한다."""
        base = filled("standard", strict_parts=True)
        summary = "- 설계 결정 두 개, 인수 테스트 T1~T5"
        for variant in ("해당 없음 (표준이라)", "해당없음", "N/A", "-"):
            with self.subTest(variant=variant):
                failures = lint.lint_body(base.replace(summary, variant), "strict")["failures"]
                self.assertTrue(any("플랜 요약" in f for f in failures), failures)
        review_line = "- W2 리뷰 A·B, 발견 2건 모두 반영"
        failures = lint.lint_body(base.replace(review_line, ""), "strict")["failures"]
        self.assertTrue(any("리뷰 결과" in f and "비어" in f for f in failures), failures)
        self.assertEqual(lint.lint_body(base, "strict")["failures"], [])

    def test_verification_lines(self):
        """T4"""
        empty = filled().replace("- 방법: unittest", "- 방법:")
        self.assertEqual(lint.lint_body(empty, None)["failures"], ["`## 검증`의 '방법' 값이 비어 있다."])
        missing = filled().replace("- 결과: 통과\n", "")
        self.assertEqual(lint.lint_body(missing, None)["failures"], ["`## 검증`에 '결과' 줄이 없다."])
        nested = filled().replace("- 검증하지 못한 것과 남은 위험: 없음", "- 검증하지 못한 것과 남은 위험:\n  - GitLab 실환경")
        self.assertEqual(lint.lint_body(nested, None)["failures"], [])
        bold = filled().replace("- 방법: unittest", "- **방법**: unittest")
        self.assertEqual(lint.lint_body(bold, None)["failures"], [])
        no_section = filled().replace("## 검증", "## 확인")
        self.assertEqual(lint.lint_body(no_section, None)["failures"], ["`## 검증` 절이 없다."])

    def test_impact_needs_checked_box(self):
        """T5"""
        body = filled().replace("- [x] 해당 없음", "- [ ] 해당 없음")
        self.assertIn("체크한 항목이 없다", " ".join(lint.lint_body(body, None)["failures"]))
        upper = body.replace("- [ ] API 계약", "- [X] API 계약")
        self.assertEqual(lint.lint_body(upper, None)["failures"], [])

    def test_tier_values(self):
        """T6"""
        cases = {"standard": "standard", "표준 — 근거": "standard", "**엄격** 토큰 처리": "strict",
                 "STRICT": "strict", "lite(경량)": "lite", "경량": "lite"}
        for value, expected in cases.items():
            self.assertEqual(lint.parse_tier([f"- {value}"]), expected, value)
        for value in ("표준화", "medium", "", "판정: standard"):
            self.assertIsNone(lint.parse_tier([f"- {value}"]), value)
        body = filled().replace("## 판정", "## 단계")
        self.assertIn("`## 판정` 절이 없다.", lint.lint_body(body, None)["failures"])

    def test_strict_requires_plan_and_review(self):
        """T7"""
        na = lint.lint_body(filled("strict"), None)["failures"]
        self.assertIn("엄격 판정인데 `## 플랜 요약`가 '해당 없음'이다.", na)
        self.assertIn("엄격 판정인데 `## 리뷰 결과`가 '해당 없음'이다.", na)
        self.assertTrue(any("측정 칸 '읽은 기준 문서 절 수'이 비어" in f for f in na))
        ok = filled("strict", strict_parts=True)
        self.assertEqual(lint.lint_body(ok, None)["failures"], [])
        blank = ok.replace("| 리뷰 세션 모델 | Claude Opus 5.5 |", "| 리뷰 세션 모델 |  |")
        self.assertEqual(lint.lint_body(blank, None)["failures"], ["엄격 판정인데 `## 리뷰 결과` 측정 칸 '리뷰 세션 모델'이 비어 있다."])
        dropped = ok.replace("| 리뷰 세션 모델 | Claude Opus 5.5 |\n", "")
        self.assertEqual(lint.lint_body(dropped, None)["failures"],
                         ["엄격 판정인데 `## 리뷰 결과` 측정 칸에 '리뷰 세션 모델' 항목이 없다."])
        removed = re.sub(r"## 플랜 요약\n.*?(?=## 검토)", "", ok, flags=re.S)
        self.assertEqual(lint.lint_body(removed, None)["failures"], ["엄격 판정인데 `## 플랜 요약` 절이 없다."])
        # 측정 표가 아닌 내용에 "해당 없음"이 남아 있으면 요약과 함께 있어도 실패다
        both = ok.replace("- W2 리뷰 A·B", "해당 없음\n- W2 리뷰 A·B")
        self.assertIn("엄격 판정인데 `## 리뷰 결과`가 '해당 없음'이다.", lint.lint_body(both, None)["failures"])

    def test_judge_raises_tier_and_records_mismatch(self):
        """T8: 결정 D-10"""
        raised = lint.lint_body(filled("standard"), "strict")
        self.assertEqual(raised["tier"], {"body": "standard", "judge": "strict", "effective": "strict", "mismatch": True})
        self.assertIn("엄격 판정인데 `## 플랜 요약`가 '해당 없음'이다.", raised["failures"])
        higher = lint.lint_body(filled("strict", strict_parts=True), "lite")
        self.assertEqual(higher["tier"], {"body": "strict", "judge": "lite", "effective": "strict", "mismatch": True})
        self.assertEqual(higher["failures"], [])
        lower = lint.lint_body(filled("lite"), "standard")
        self.assertEqual((lower["tier"]["effective"], lower["tier"]["mismatch"], lower["failures"]), ("standard", True, []))
        unknown = lint.lint_body(filled("medium"), "strict")
        self.assertEqual(unknown["tier"]["effective"], "strict")
        self.assertFalse(unknown["tier"]["mismatch"])

    def test_code_block_heading_ignored(self):
        """T9"""
        body = "- Closes #1\n\n```\n## 판정\n- strict\n```\n"
        sections = lint.split_sections(lint.content_lines(body))
        self.assertNotIn("판정", sections)
        self.assertEqual(lint.split_sections(["## 판정", "~~~", "## 검증", "~~~", "- lite"])["판정"][-1], "- lite")

    def test_long_fence_keeps_inner_triple_backticks(self):
        """Codex 리뷰: 네 개짜리 울타리 안의 ``` 줄은 블록을 닫지 않는다."""
        demo = "````markdown\n```\n- Closes DEMO-9\n## 판정\n- strict\n- [x] 해당 없음\n```\n````"
        body = filled().replace("- [x] 해당 없음", "- [ ] 해당 없음").replace("- Closes DEMO-7", demo)
        result = lint.lint_body(body, None)
        failures = " ".join(result["failures"])
        self.assertIn("`Closes` 또는 `Refs`", failures)
        self.assertIn("체크한 항목이 없다", failures)
        self.assertEqual(result["tier"]["body"], "standard")  # 블록 안 `## 판정` / strict는 절이 아니다
        lines = demo.split("\n") + ["## 검증", "- 방법: x"]
        self.assertEqual(lint.unfenced(lines)[:8], [""] * 8)
        sections = lint.split_sections(lines)
        self.assertEqual(list(sections), ["검증"])
        # 닫은 뒤의 내용은 다시 본다
        after = filled().replace("- Closes DEMO-7", "````\n```\n````\n- Closes DEMO-7")
        self.assertEqual(lint.lint_body(after, "standard")["failures"], [])

    def test_comment_inside_fence_does_not_close_it(self):
        """Codex P2(PR #40): 코드 블록 안의 `` ```<!-- x --> `` 줄이 주석 제거로 닫는 펜스가 되면 안 된다."""
        body = filled().replace("- Closes DEMO-7", "```\n```<!-- note -->\n- Closes DEMO-9\n```")
        self.assertIn("`Closes` 또는 `Refs`", " ".join(lint.lint_body(body, None)["failures"]))
        # 코드 블록 밖의 주석은 그대로 지운다
        self.assertEqual(lint.lint_body(filled().replace("- Closes DEMO-7", "- Closes DEMO-7 <!-- x -->"), None)["failures"], [])

    def test_fence_close_rules(self):
        """Codex 리뷰: 다른 문자는 닫지 않고, 더 긴 울타리는 닫고, 짧거나 뒤에 글자가 있으면 닫지 않는다."""
        self.assertEqual(lint.unfenced(["~~~", "```", "- a", "~~~", "- b"]), ["", "", "", "", "- b"])
        self.assertEqual(lint.unfenced(["```", "~~~", "- a", "```", "- b"]), ["", "", "", "", "- b"])
        self.assertEqual(lint.unfenced(["```", "- a", "`````", "- b"]), ["", "", "", "- b"])
        self.assertEqual(lint.unfenced(["````", "```", "- a", "````  ", "- b"]), ["", "", "", "", "- b"])
        self.assertEqual(lint.unfenced(["```", "``` x", "- a", "```", "- b"]), ["", "", "", "", "- b"])
        self.assertEqual(lint.unfenced(["   ```", "- a", "   ```", "- b"]), ["", "", "", "- b"])
        self.assertEqual(lint.unfenced(["    ```", "- a"]), ["    ```", "- a"])  # 4칸 들여쓰기는 울타리가 아니다
        self.assertEqual(lint.unfenced(["``` a`b", "- a"]), ["``` a`b", "- a"])  # backtick 정보 문자열에 backtick
        self.assertEqual(lint.unfenced(["~~~ a`b", "- a", "~~~", "- b"]), ["", "", "", "- b"])
        sections = lint.split_sections(["## 판정", "````", "```", "## 검증", "```", "````", "- lite"])
        self.assertEqual((list(sections), sections["판정"][-1]), (["판정"], "- lite"))

    def test_crlf_body(self):
        body = filled().replace("\n", "\r\n")
        self.assertEqual(lint.lint_body(body, "standard")["failures"], [])


class PlansRuleTest(unittest.TestCase):
    def test_plans_paths_and_gitignore(self):
        """T10: 결정 D-13"""
        ignore = "node_modules/\n/plans/\n"
        self.assertEqual(len(lint.plans_failures([("A", "plans/1.md")], ignore)), 1)
        self.assertEqual(len(lint.plans_failures([("M", "plans/1-review.md")], ignore)), 1)
        self.assertEqual(lint.plans_failures([("D", "plans/1.md")], ignore), [])
        self.assertEqual(lint.plans_failures([("A", "src/plans/x.py"), ("A", "plansx/a")], ignore), [])
        self.assertIn("`.gitignore`", lint.plans_failures([], None)[0])
        self.assertIn("`.gitignore`", lint.plans_failures([], "plans/\n/plans/x\n")[0])
        self.assertEqual(lint.plans_failures([], "  /plans/  \r\n"), [])


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    if args[0] != "commit":
        return None
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()


class RepoCase(unittest.TestCase):
    """harness.json·harness_common 사본·.gitignore가 있는 임시 git 저장소."""

    config = {"project_name": "demo", "platform": "gitlab", "tracker": "jira", "issue_prefix": "DEMO",
              "default_branch": "main", "integration_branch": "develop",
              "areas": [{"dir": "backend", "verify": ["true"], "trigger_paths": {"strict": ["/src/auth/"]}}]}

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="mr-lint-"))
        self.addCleanup(remove_tree, self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "config", "core.autocrlf", "false")
        (self.repo / ".claude" / "hooks").mkdir(parents=True)
        shutil.copy(COMMON, self.repo / ".claude" / "hooks" / "harness_common.py")
        (self.repo / "harness.json").write_text(json.dumps(self.config), encoding="utf-8")
        (self.repo / ".gitignore").write_text("/plans/\n", encoding="utf-8")
        (self.repo / "backend" / "src" / "auth").mkdir(parents=True)
        (self.repo / "backend" / "src" / "auth" / "a.py").write_text("a\n", encoding="utf-8")
        (self.repo / "old.txt").write_text("old\n", encoding="utf-8")
        (self.repo / "gone.txt").write_text("gone\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        self.base = git(self.repo, "commit", "-q", "-m", "base")
        git(self.repo, "switch", "-q", "-c", "feature")

    def commit(self, message="change"):
        git(self.repo, "add", "-A")
        return git(self.repo, "commit", "-q", "-m", message)


class GitChangesTest(RepoCase):
    def test_changes_and_judge_from_git(self):
        """T11"""
        (self.repo / "backend" / "src" / "auth" / "a.py").write_text("b\n", encoding="utf-8")
        (self.repo / "README.md").write_text("doc\n", encoding="utf-8")
        (self.repo / "gone.txt").unlink()
        (self.repo / "old.txt").rename(self.repo / "new.txt")
        head = self.commit()
        git(self.repo, "switch", "-q", "main")  # 대상 브랜치가 앞서가도 merge-base 이후만 본다
        (self.repo / "main-only.txt").write_text("x\n", encoding="utf-8")
        main_head = self.commit("main")
        changes = lint.changed(self.repo, main_head, head)
        self.assertEqual(sorted(changes), [("A", "README.md"), ("A", "new.txt"), ("D", "gone.txt"),
                                           ("D", "old.txt"), ("M", "backend/src/auth/a.py")])
        common = lint.load_common(self.repo / lint.DEFAULT_COMMON)
        self.assertFalse((self.repo / ".claude" / "hooks" / "__pycache__").exists())  # check의 여분 항목이 된다
        judged = lint.rejudge(self.repo, common, [p for _s, p in changes])
        self.assertEqual(judged, common.judge([p for _s, p in changes], self.config))
        self.assertEqual(judged["tier"], "strict")

    def test_errors(self):
        with self.assertRaises(lint.LintError):
            lint.changed(self.repo, "0" * 40, self.base)
        with self.assertRaises(lint.LintError):
            lint.changed(self.repo, "--output=x", self.base)
        with self.assertRaises(lint.LintError):
            lint.load_common(self.repo / "missing.py")
        (self.repo / "harness.json").write_text('{"judge": {"trigger_paths": {"bad": []}}}', encoding="utf-8")
        common = lint.load_common(self.repo / lint.DEFAULT_COMMON)
        with self.assertRaises(lint.LintError):
            lint.rejudge(self.repo, common, ["a"])


class ContextTest(unittest.TestCase):
    def test_gitlab_env(self):
        """T12"""
        env = {"GITLAB_CI": "true", "CI_MERGE_REQUEST_DESCRIPTION": "본문", "CI_MERGE_REQUEST_DIFF_BASE_SHA": "b",
               "CI_MERGE_REQUEST_SOURCE_BRANCH_SHA": "s", "CI_COMMIT_SHA": "m", "CI_API_V4_URL": "https://g/api/v4",
               "CI_PROJECT_ID": "7", "CI_MERGE_REQUEST_IID": "3"}
        ctx = lint.context_from_env(env)
        self.assertEqual((ctx["platform"], ctx["body"], ctx["base"], ctx["head"], ctx["number"]),
                         ("gitlab", "본문", "b", "s", "3"))
        self.assertEqual(lint.context_from_env({**env, "CI_MERGE_REQUEST_SOURCE_BRANCH_SHA": ""})["head"], "m")
        self.assertEqual(lint.context_from_env({**env, "CI_MERGE_REQUEST_DESCRIPTION": ""})["body"], "")
        missing = dict(env)
        del missing["CI_MERGE_REQUEST_DESCRIPTION"]
        with self.assertRaisesRegex(lint.LintError, "16.7"):
            lint.context_from_env(missing)
        # #41 T10: 잘림은 예외 대신 표시로 넘긴다(run이 통합 MR 판단 뒤 토큰으로 읽거나 종료 2)
        self.assertFalse(ctx["truncated"])
        self.assertTrue(lint.context_from_env({**env, "CI_MERGE_REQUEST_DESCRIPTION_IS_TRUNCATED": "true"})["truncated"])
        self.assertEqual(ctx["mr_project"], "7")  # CI_MERGE_REQUEST_PROJECT_ID가 없으면 CI_PROJECT_ID
        branches = {**env, "CI_MERGE_REQUEST_SOURCE_BRANCH_NAME": "develop", "CI_MERGE_REQUEST_TARGET_BRANCH_NAME": "main",
                    "CI_MERGE_REQUEST_SOURCE_PROJECT_ID": "8", "CI_MERGE_REQUEST_PROJECT_ID": "8"}
        ctx = lint.context_from_env(branches)
        self.assertEqual((ctx["source_branch"], ctx["target_branch"], ctx["same_repo"], ctx["mr_project"]),
                         ("develop", "main", True, "8"))
        self.assertFalse(lint.context_from_env({**branches, "CI_MERGE_REQUEST_SOURCE_PROJECT_ID": "9"})["same_repo"])
        self.assertFalse(lint.context_from_env(env)["same_repo"])  # 프로젝트 ID가 없으면 같은 프로젝트로 보지 않는다
        with self.assertRaises(lint.LintError):
            lint.context_from_env({})

    def test_github_event(self):
        """T13"""
        tmp = Path(tempfile.mkdtemp(prefix="mr-lint-event-"))
        self.addCleanup(remove_tree, tmp)
        event = tmp / "event.json"
        event.write_text(json.dumps({"pull_request": {"number": 9, "body": None, "base": {"sha": "b"},
                                                      "head": {"sha": "h"}}}), encoding="utf-8")
        env = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "pull_request", "GITHUB_EVENT_PATH": str(event),
               "GITHUB_API_URL": "https://api.github.com", "GITHUB_REPOSITORY": "o/r"}
        ctx = lint.context_from_env(env)
        self.assertEqual((ctx["platform"], ctx["body"], ctx["base"], ctx["head"], ctx["number"]),
                         ("github", "", "b", "h", "9"))
        self.assertEqual((ctx["truncated"], ctx["same_repo"]), (False, False))
        with self.assertRaisesRegex(lint.LintError, "pull_request_target"):
            lint.context_from_env({**env, "GITHUB_EVENT_NAME": "pull_request_target"})
        event.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(lint.LintError, "pull_request가 없다"):
            lint.context_from_env(env)


class MainTest(RepoCase):
    def run_main(self, body, *extra, env=None):
        (self.repo / "body.md").write_text(body, encoding="utf-8")
        out_file = self.repo / "out.json"
        out = io.StringIO()
        with redirect_stdout(out):
            code = lint.main(["--project", str(self.repo), "--body-file", str(self.repo / "body.md"),
                              "--base", self.base, "--head", self.head, "--out", str(out_file), *extra],
                             env=env or {})
        return code, out.getvalue(), json.loads(out_file.read_text(encoding="utf-8"))

    def setUp(self):
        super().setUp()
        (self.repo / "src.py").write_text("x\n", encoding="utf-8")
        self.head = self.commit()

    def test_exit_codes_and_json(self):
        """T14"""
        code, out, report = self.run_main(filled())
        self.assertEqual(code, 0, out)
        self.assertEqual(report["result"], "pass")
        self.assertEqual(report["tier"]["effective"], "standard")
        self.assertEqual(report["comment"], "skipped")
        self.assertEqual([f["path"] for f in report["files"]], ["src.py"])
        self.assertIn("MR 본문 lint 통과", out)

        code, out, report = self.run_main(template())
        self.assertEqual(code, 1)
        self.assertEqual(report["result"], "fail")
        self.assertTrue(report["failures"])
        self.assertIn("MR 본문 lint 실패", out)

        (self.repo / "plans").mkdir()
        (self.repo / "plans" / "1.md").write_text("plan\n", encoding="utf-8")
        git(self.repo, "add", "-f", "plans/1.md")
        self.head = self.commit()
        code, _out, report = self.run_main(filled())
        self.assertEqual(code, 1)
        self.assertIn("플랜·리뷰 파일", report["failures"][0])

        (self.repo / lint.DEFAULT_COMMON).unlink()
        code, out, report = self.run_main(filled())
        self.assertEqual(code, 2)
        self.assertEqual(report["result"], "error")
        self.assertIn("검사할 수 없다", out)

    def test_judge_strict_requires_strict_sections(self):
        (self.repo / "backend" / "src" / "auth" / "a.py").write_text("c\n", encoding="utf-8")
        self.head = self.commit()
        code, _out, report = self.run_main(filled("standard"))
        self.assertEqual(code, 1)
        self.assertTrue(report["tier"]["mismatch"])
        code, _out, report = self.run_main(filled("strict", strict_parts=True))
        self.assertEqual(code, 0, report)

    def test_missing_gitignore_rule_fails(self):
        (self.repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
        code, _out, report = self.run_main(filled())
        self.assertEqual(code, 1)
        self.assertIn("`/plans/`", report["failures"][0])

    def test_comment_failure_keeps_exit_code(self):
        """T16: 댓글 실패는 lint 종료 코드를 바꾸지 않는다"""
        with mock.patch.object(lint, "try_comment", return_value="failed") as comment:
            code, _out, report = self.run_main(filled(), env={"HARNESS_COMMENT_TOKEN": "t"})
        self.assertEqual((code, report["comment"]), (0, "failed"))
        self.assertEqual(comment.call_args.args[2], "t")
        code, _out, report = self.run_main(filled(), "--no-comment", env={"HARNESS_COMMENT_TOKEN": "t"})
        self.assertEqual(report["comment"], "skipped")

    def test_local_run_needs_range(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = lint.main(["--project", str(self.repo), "--body-file", "x", "--out", str(self.repo / "o.json")],
                             env={})
        self.assertEqual(code, 2)


class FakeUrlopen:
    """urllib.request.urlopen 대역(전체 본문 읽기). 요청을 기록하고 payload 바이트나 error를 준다."""

    def __init__(self, payload=b"", error=None):
        self.requests, self.payload, self.error = [], payload, error

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if self.error:
            raise self.error
        return io.BytesIO(self.payload)


class CiRunCase(RepoCase):
    """CI 환경 변수로 main을 실행한다(--body-file 없음)."""

    def setUp(self):
        super().setUp()
        (self.repo / "src.py").write_text("x\n", encoding="utf-8")
        self.head = self.commit()

    def gitlab_env(self, body, **extra):
        return {"GITLAB_CI": "true", "CI_MERGE_REQUEST_DESCRIPTION": body, "CI_MERGE_REQUEST_DIFF_BASE_SHA": self.base,
                "CI_COMMIT_SHA": self.head, "CI_API_V4_URL": "https://g/api/v4", "CI_PROJECT_ID": "7",
                "CI_MERGE_REQUEST_IID": "3", **extra}

    def github_env(self, body, head_ref="feature", base_ref="main", head_repo="o/r", base_repo="o/r"):
        event = self.repo / "event.json"
        event.write_text(json.dumps({"pull_request": {
            "number": 9, "body": body,
            "base": {"sha": self.base, "ref": base_ref, "repo": {"full_name": base_repo}},
            "head": {"sha": self.head, "ref": head_ref, "repo": {"full_name": head_repo} if head_repo else None}}}),
            encoding="utf-8")
        return {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "pull_request", "GITHUB_EVENT_PATH": str(event),
                "GITHUB_API_URL": "https://api.github.com", "GITHUB_REPOSITORY": "o/r"}

    def run_ci(self, env, opener=None):
        """(종료 코드, 출력, 리포트, urlopen 대역). 댓글 HTTP는 FakeApi로 막고 호출을 self.comment_api에 남긴다."""
        opener = opener or FakeUrlopen()
        self.comment_api = FakeApi(pages=[], me=1)
        out_file = self.repo / "out.json"
        out = io.StringIO()
        with mock.patch.object(lint.urllib.request, "urlopen", opener), \
                mock.patch.object(lint, "http_json", self.comment_api), redirect_stdout(out):
            code = lint.main(["--project", str(self.repo), "--out", str(out_file)], env=env)
        text = out_file.read_text(encoding="utf-8")
        self.assertNotIn("tok-secret", out.getvalue())
        self.assertNotIn("tok-secret", text)
        return code, out.getvalue(), json.loads(text), opener


class FullBodyTest(CiRunCase):
    """#41 Q5(a): 잘린 GitLab 본문은 토큰이 있을 때만 API로 전체 본문을 읽는다. 실패는 종료 2."""

    def payload(self, **fields):
        return json.dumps({"iid": 3, "description": filled(), **fields}).encode("utf-8")

    def test_truncated_with_token_reads_api(self):
        """T1"""
        env = self.gitlab_env("잘린 본문", CI_MERGE_REQUEST_DESCRIPTION_IS_TRUNCATED="true",
                              HARNESS_COMMENT_TOKEN="tok-secret")
        code, out, report, opener = self.run_ci(env, FakeUrlopen(self.payload()))
        self.assertEqual((code, report["result"]), (0, "pass"), out)
        self.assertIn("API로 전체 본문", out)
        self.assertEqual(len(opener.requests), 1)
        request = opener.requests[0]
        self.assertEqual((request.get_method(), request.full_url), ("GET", "https://g/api/v4/projects/7/merge_requests/3"))
        # 토큰은 리다이렉트로 옮겨지지 않는 헤더에만 있다
        self.assertEqual(request.unredirected_hdrs.get("Private-token"), "tok-secret")
        self.assertNotIn("Private-token", request.headers)
        # 소스 프로젝트가 아니라 MR 프로젝트로 읽는다(fork MR 파이프라인은 fork에서 돈다)
        env["CI_MERGE_REQUEST_PROJECT_ID"] = "group/p"
        _code, _out, _report, opener = self.run_ci(env, FakeUrlopen(self.payload()))
        self.assertEqual(opener.requests[0].full_url, "https://g/api/v4/projects/group%2Fp/merge_requests/3")
        # API 본문도 lint한다(본문이 비면 실패)
        code, _out, report, _ = self.run_ci(env, FakeUrlopen(self.payload(description=None)))
        self.assertEqual((code, report["result"]), (1, "fail"))

    def test_api_failure_is_error(self):
        """T2: 잘린 본문으로 판정하지 않는다. 토큰·응답 본문은 출력하지 않는다"""
        env = self.gitlab_env(filled(), CI_MERGE_REQUEST_DESCRIPTION_IS_TRUNCATED="true",
                              HARNESS_COMMENT_TOKEN="tok-secret")
        unauthorized = urllib.error.HTTPError("https://g/api/v4/x", 401, "Unauthorized", {},
                                              io.BytesIO(b"SECRET-DETAIL"))
        cases = {
            "401": FakeUrlopen(error=unauthorized),
            "down": FakeUrlopen(error=urllib.error.URLError("down")),
            "too big": FakeUrlopen(self.payload(pad="x" * 300)),
            "not json": FakeUrlopen(b"SECRET-DETAIL not json"),
            "list": FakeUrlopen(b"[]"),
            "no description": FakeUrlopen(b'{"iid": 3}'),
            "bad description": FakeUrlopen(b'{"iid": 3, "description": 5}'),
            "other mr": FakeUrlopen(self.payload(iid=4)),
        }
        for name, opener in cases.items():
            with self.subTest(name), mock.patch.object(lint, "MR_RESPONSE_MAX_BYTES", 2000 if name == "too big" else 10**6):
                code, out, report, _ = self.run_ci(env, opener)
                self.assertEqual((code, report["result"]), (2, "error"), out)
                self.assertIn("API로 전체 본문을 읽지 못했다", out)
                self.assertNotIn("SECRET-DETAIL", out + json.dumps(report))
                self.assertEqual(len(opener.requests), 1)
        _code, out, _report, _ = self.run_ci(env, cases["401"])
        self.assertIn("HTTP 401", out)
        self.assertIn("read_api", out)

    def test_truncated_without_token_is_error(self):
        """T3"""
        env = self.gitlab_env(filled(), CI_MERGE_REQUEST_DESCRIPTION_IS_TRUNCATED="true")
        code, out, report, opener = self.run_ci(env)
        self.assertEqual((code, report["result"]), (2, "error"))
        self.assertIn("잘렸다", out)
        self.assertEqual(opener.requests, [])

    def test_not_truncated_makes_no_api_call(self):
        """T4: 잘리지 않았으면 토큰이 있어도 본문을 API로 읽지 않는다"""
        code, out, report, opener = self.run_ci(self.gitlab_env(filled(), HARNESS_COMMENT_TOKEN="tok-secret"))
        self.assertEqual((code, report["result"]), (0, "pass"), out)
        self.assertEqual(opener.requests, [])
        self.assertNotIn("API로 전체 본문", out)

    def test_http_api_url_rejected(self):
        """T5"""
        base = self.gitlab_env(filled(), CI_MERGE_REQUEST_DESCRIPTION_IS_TRUNCATED="true",
                               HARNESS_COMMENT_TOKEN="tok-secret")
        for extra in ({"CI_API_V4_URL": "http://g/api/v4"}, {"CI_MERGE_REQUEST_IID": "3/../../user"},
                      {"CI_MERGE_REQUEST_IID": ""}, {"CI_PROJECT_ID": ""}):
            with self.subTest(extra=extra):
                code, out, report, opener = self.run_ci({**base, **extra}, FakeUrlopen(self.payload()))
                self.assertEqual((code, report["result"]), (2, "error"), out)
                self.assertEqual(opener.requests, [])


class IntegrationSkipTest(CiRunCase):
    """#41 Q6(a): 같은 저장소의 integration → default MR은 건너뜀(통과)."""

    GITLAB_INTEGRATION = {"CI_MERGE_REQUEST_SOURCE_BRANCH_NAME": "develop", "CI_MERGE_REQUEST_TARGET_BRANCH_NAME": "main",
                          "CI_MERGE_REQUEST_SOURCE_PROJECT_ID": "7", "CI_MERGE_REQUEST_PROJECT_ID": "7"}

    def assert_skipped(self, env):
        with mock.patch.object(lint, "changed", side_effect=AssertionError("git을 보지 않는다")):
            code, out, report, opener = self.run_ci(env)
        self.assertEqual(code, 0, out)
        self.assertEqual((report["result"], report["reason"], report["comment"], report["failures"]),
                         ("skipped", "integration", "skipped", []))
        self.assertIn("통합 MR(develop → main)이라 MR 본문 lint를 건너뛴다(통과)", out)
        self.assertNotIn("MR 본문 lint 통과", out)
        self.assertEqual(opener.requests, [])
        self.assertEqual(self.comment_api.calls, [])  # 건너뜀은 댓글을 남기지 않는다

    def test_gitlab_integration_mr_skipped(self):
        """T6: Release 본문이 잘렸고 토큰이 없어도 건너뜀이 먼저다"""
        self.assert_skipped(self.gitlab_env("", **self.GITLAB_INTEGRATION))
        self.assert_skipped(self.gitlab_env("", CI_MERGE_REQUEST_DESCRIPTION_IS_TRUNCATED="true",
                                            **self.GITLAB_INTEGRATION))
        self.assert_skipped(self.gitlab_env("", HARNESS_COMMENT_TOKEN="tok-secret", **self.GITLAB_INTEGRATION))

    def test_github_integration_pr_skipped(self):
        """T7"""
        self.assert_skipped(self.github_env(None, head_ref="develop", base_ref="main"))

    def test_target_tip_settings_used_when_available(self):
        """Codex P2(PR #42): 대상 브랜치 최신 커밋의 harness.json으로 판단한다(diff 기준 커밋의 옛 값이 아니라)."""
        (self.repo / "harness.json").write_text(json.dumps({**self.config, "integration_branch": "release"}),
                                                encoding="utf-8")
        tip = self.commit("rename integration branch on target")
        renamed = {**self.GITLAB_INTEGRATION, "CI_MERGE_REQUEST_SOURCE_BRANCH_NAME": "release",
                   "CI_MERGE_REQUEST_TARGET_BRANCH_SHA": tip}
        self.assert_skipped_from("release", self.gitlab_env("", **renamed))
        # 옛 통합 브랜치(develop)는 대상 최신 값으로 보면 더 이상 통합 MR이 아니다
        old = {**self.GITLAB_INTEGRATION, "CI_MERGE_REQUEST_TARGET_BRANCH_SHA": tip}
        code, out, report, _ = self.run_ci(self.gitlab_env("", **old))
        self.assertEqual((code, report["result"]), (1, "fail"), out)

    def assert_skipped_from(self, source, env):
        with mock.patch.object(lint, "changed", side_effect=AssertionError("git diff를 보지 않는다")):
            code, out, report, _ = self.run_ci(env)
        self.assertEqual((code, report["result"]), (0, "skipped"), out)
        self.assertIn(f"통합 MR({source} → main)", out)

    def test_mr_cannot_redefine_integration_branch(self):
        """리뷰 F1: MR이 같은 MR에서 harness.json의 브랜치 값을 바꿔 자기 lint를 끌 수 없다(기준 커밋 값으로 판단)."""
        (self.repo / "harness.json").write_text(
            json.dumps({**self.config, "integration_branch": "feature", "default_branch": "main"}), encoding="utf-8")
        self.head = self.commit()
        env = self.gitlab_env("", **{**self.GITLAB_INTEGRATION, "CI_MERGE_REQUEST_SOURCE_BRANCH_NAME": "feature"})
        code, out, report, _ = self.run_ci(env)
        self.assertEqual((code, report["result"]), (1, "fail"), out)
        # 기준 커밋에 harness.json이 없거나 읽을 수 없으면 건너뛰지 않는다
        self.assertIsNone(lint.base_config(self.repo, "no-such-rev"))
        self.assertIsNone(lint.base_config(self.repo, "--output=x"))

    def test_fork_or_other_branches_not_skipped(self):
        """T8: fork의 같은 이름 브랜치, 다른 브랜치 조합, integration=default는 검사한다(빈 본문이라 실패)"""
        envs = {
            "github fork": self.github_env("", head_ref="develop", head_repo="evil/r"),
            "github deleted fork": self.github_env("", head_ref="develop", head_repo=None),
            "gitlab fork": self.gitlab_env("", **{**self.GITLAB_INTEGRATION, "CI_MERGE_REQUEST_SOURCE_PROJECT_ID": "9"}),
            "gitlab no project ids": self.gitlab_env("", CI_MERGE_REQUEST_SOURCE_BRANCH_NAME="develop",
                                                     CI_MERGE_REQUEST_TARGET_BRANCH_NAME="main"),
            "feature": self.gitlab_env("", **{**self.GITLAB_INTEGRATION, "CI_MERGE_REQUEST_SOURCE_BRANCH_NAME": "feature"}),
            "to develop": self.gitlab_env("", **{**self.GITLAB_INTEGRATION, "CI_MERGE_REQUEST_TARGET_BRANCH_NAME": "develop"}),
        }
        for name, env in envs.items():
            with self.subTest(name):
                code, out, report, _ = self.run_ci(env)
                self.assertEqual((code, report["result"]), (1, "fail"), out)
        # 통합 브랜치를 쓰지 않으면(integration == default) 건너뛰지 않는다
        (self.repo / "harness.json").write_text(json.dumps({**self.config, "integration_branch": "main"}),
                                                encoding="utf-8")
        env = self.gitlab_env("", **{**self.GITLAB_INTEGRATION, "CI_MERGE_REQUEST_SOURCE_BRANCH_NAME": "main"})
        self.assertEqual(self.run_ci(env)[0], 1)
        self.assertFalse(lint.is_integration_mr({"same_repo": True, "source_branch": "develop", "target_branch": "main"},
                                                {"integration_branch": "develop", "default_branch": 5}))


class FakeApi:
    """http_json 대역. (method, url, headers, data)를 기록하고 GET 응답은 표로 준다."""

    def __init__(self, pages=None, me=None, error=None):
        self.calls, self.pages, self.me, self.error = [], pages or [], me, error

    def __call__(self, method, url, headers, data=None):
        self.calls.append((method, url, headers, data))
        if self.error:
            raise self.error
        if method == "GET" and url.endswith("/user"):
            return {"id": self.me}
        if method == "GET":
            return self.pages
        return {"id": 1}


REPORT = {"result": "fail", "failures": ["`## 판정` 절이 없다."], "human_check": [{"area": None, "trigger": "x"}],
          "tier": {"body": None, "judge": "standard", "effective": "standard", "mismatch": False}}
GITLAB_CTX = {"platform": "gitlab", "api": "https://gitlab.example/api/v4", "project": "group/p", "number": "3"}
GITHUB_CTX = {"platform": "github", "api": "https://api.github.com", "project": "o/r", "number": "9"}


class CommentTest(unittest.TestCase):
    def comment(self, ctx, api, token="tok-secret"):
        out = io.StringIO()
        with mock.patch.object(lint, "http_json", api), redirect_stdout(out):
            status = lint.try_comment(ctx, REPORT, token)
        self.assertNotIn("tok-secret", out.getvalue())
        return status, out.getvalue()

    def test_creates_then_updates_marked_comment(self):
        """T15"""
        api = FakeApi(pages=[], me=5)
        self.assertEqual(self.comment(GITLAB_CTX, api)[0], "created")
        method, url, headers, data = api.calls[-1]
        self.assertEqual((method, url), ("POST", "https://gitlab.example/api/v4/projects/group%2Fp/merge_requests/3/notes"))
        self.assertEqual(headers["PRIVATE-TOKEN"], "tok-secret")
        self.assertTrue(data["body"].startswith(lint.MARKER))
        self.assertIn("`## 판정` 절이 없다.", data["body"])

        notes = [{"id": 11, "body": lint.MARKER + " old", "author": {"id": 6}},
                 {"id": 12, "body": lint.MARKER + " mine", "author": {"id": 5}}]
        api = FakeApi(pages=notes, me=5)
        self.assertEqual(self.comment(GITLAB_CTX, api)[0], "updated")
        self.assertEqual(api.calls[-1][:2], ("PUT", "https://gitlab.example/api/v4/projects/group%2Fp/merge_requests/3/notes/12"))
        api = FakeApi(pages=notes[:1], me=5)  # 남의 표식 댓글은 고치지 않는다
        self.assertEqual(self.comment(GITLAB_CTX, api)[0], "created")

        comments = [{"id": 21, "body": lint.MARKER, "user": {"login": "someone"}},
                    {"id": 22, "body": lint.MARKER, "user": {"login": "github-actions[bot]"}}]
        api = FakeApi(pages=comments)
        self.assertEqual(self.comment(GITHUB_CTX, api)[0], "updated")
        method, url, headers, _ = api.calls[-1]
        self.assertEqual((method, url), ("PATCH", "https://api.github.com/repos/o/r/issues/comments/22"))
        self.assertEqual(headers["Authorization"], "Bearer tok-secret")
        api = FakeApi(pages=comments[:1])
        self.assertEqual(self.comment(GITHUB_CTX, api)[0], "created")
        self.assertEqual(api.calls[-1][:2], ("POST", "https://api.github.com/repos/o/r/issues/9/comments"))

    def test_failure_is_warning_and_no_token_skips(self):
        """T16"""
        error = urllib.error.HTTPError("https://api.github.com/x", 403, "Forbidden", {}, None)
        status, out = self.comment(GITHUB_CTX, FakeApi(error=error))
        self.assertEqual(status, "failed")
        self.assertIn("HTTP 403", out)
        status, out = self.comment(GITHUB_CTX, FakeApi(error=urllib.error.URLError("down")))
        self.assertEqual(status, "failed")
        api = FakeApi()
        self.assertEqual(self.comment(GITHUB_CTX, api, token="")[0], "skipped")
        self.assertEqual(api.calls, [])
        status, _ = self.comment({**GITHUB_CTX, "api": "http://api.github.com"}, api)
        self.assertEqual((status, api.calls), ("failed", []))

    def test_comment_has_no_body_or_paths(self):
        text = lint.comment_text({**REPORT, "files": [{"path": "@everyone/x.py"}]})
        self.assertNotIn("@everyone", text)


class SanitizeTest(unittest.TestCase):
    """Codex 리뷰: 댓글은 리포트의 검증한 필드만 옮긴다(리포트는 PR 코드가 쓴 신뢰할 수 없는 입력)."""

    def test_unknown_keys_and_values_dropped(self):
        raw = {"result": "pass", "body": "SECRET-BODY", "files": [{"path": "secret/path.py"}], "comment": "x",
               "tier": {"body": "evil", "judge": "strict", "effective": "<b>strict</b>", "mismatch": "yes",
                        "extra": "SECRET-TIER"},
               "failures": [], "human_check": "many"}
        clean = lint.sanitize_report(raw)
        self.assertEqual(clean, {"result": "pass", "tier": {"body": None, "judge": "strict", "effective": None,
                                                            "mismatch": False},
                                 "failures": [], "omitted": 0, "human_check": 0})
        text = lint.comment_text(raw)
        for needle in ("SECRET", "secret/path", "evil", "<b>", "불일치"):
            self.assertNotIn(needle, text)

    def test_invalid_result_rejected(self):
        for raw in (None, [], "pass", {}, {"result": "PASS"}, {"result": ["pass"]}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                lint.sanitize_report(raw)

    def test_failures_capped_and_neutralised(self):
        evil = ("line1\n## 제목\r\n<!-- hide --> <img src=x> @everyone ![a](https://t/x.png) [l](u) ```js ~~~ "
                " end")
        raw = {"result": "fail", "tier": {}, "failures": [evil, 7, None, {"a": 1}, "", "x" * 1000]
               + [f"f{i}" for i in range(40)]}
        clean = lint.sanitize_report(raw)
        self.assertEqual(len(clean["failures"]), lint.COMMENT_MAX_ITEMS)
        self.assertEqual(clean["omitted"], 42 - lint.COMMENT_MAX_ITEMS)
        first = clean["failures"][0]
        self.assertNotIn("\n", first)
        self.assertNotIn("\r", first)
        self.assertNotIn(" ", first)
        for needle in ("<", ">", "@everyone", "![", "```", "~~~", "[l]"):
            self.assertNotIn(needle, first)
        self.assertTrue(first.startswith("line1 ## 제목"))
        self.assertLessEqual(len(clean["failures"][1]), lint.COMMENT_ITEM_CHARS)
        self.assertTrue(clean["failures"][1].endswith("…"))
        text = lint.comment_text(raw)
        # 모든 실패 항목이 목록 한 줄이다(목록 밖으로 나가는 줄이 없다)
        block = text.split("실패 항목:\n", 1)[1].split("\n\n", 1)[0].split("\n")
        self.assertEqual(len(block), lint.COMMENT_MAX_ITEMS + 1)
        self.assertTrue(all(line.startswith("- ") for line in block))
        self.assertEqual(block[-1], f"- 외 {42 - lint.COMMENT_MAX_ITEMS}개(job 로그)")

    def test_lint_failures_survive_unchanged(self):
        """lint가 만드는 실패 문구(백틱 코드 스팬 포함)는 그대로 보인다."""
        text = lint.comment_text(REPORT)
        self.assertIn("- `## 판정` 절이 없다.", text)
        self.assertIn("사람 확인이 필요한 트리거 1개", text)

    def test_skipped_result_accepted(self):
        """#41 T9: 건너뜀 결과는 허용 값이고 댓글은 남기지 않는다(실패 항목도 옮기지 않는다)."""
        raw = {"result": "skipped", "reason": "integration", "failures": ["SECRET"], "tier": {}}
        clean = lint.sanitize_report(raw)
        self.assertEqual((clean["result"], clean["failures"]), ("skipped", []))
        text = lint.comment_text(raw)
        self.assertIn("harness MR 본문 lint: 건너뜀", text)
        self.assertNotIn("SECRET", text)
        api = FakeApi()
        with mock.patch.object(lint, "http_json", api):
            self.assertEqual(lint.try_comment(GITLAB_CTX, raw, "tok-secret"), "skipped")
        self.assertEqual(api.calls, [])

    def test_error_report_has_no_message(self):
        """오류 메시지는 경로를 담을 수 있어 댓글에 옮기지 않는다."""
        text = lint.comment_text({"result": "error", "error": "/home/runner/secret/harness_common.py 이 없다."})
        self.assertIn("검사할 수 없음", text)
        self.assertNotIn("secret", text)


class PostReportTest(unittest.TestCase):
    """--post-report: 기준 커밋 모듈이 lint 리포트를 읽어 댓글만 남긴다. 실패는 경고, 종료 0."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mr-lint-post-"))
        self.addCleanup(remove_tree, self.tmp)
        event = self.tmp / "event.json"
        event.write_text(json.dumps({"pull_request": {"number": 9, "body": "본문 BODY-TEXT", "base": {"sha": "b"},
                                                      "head": {"sha": "h"}}}), encoding="utf-8")
        self.env = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "pull_request", "GITHUB_EVENT_PATH": str(event),
                    "GITHUB_API_URL": "https://api.github.com", "GITHUB_REPOSITORY": "o/r",
                    "HARNESS_COMMENT_TOKEN": "tok-secret"}
        self.report = self.tmp / "mr-lint.json"

    def post(self, content, env=None):
        if content is not None:
            self.report.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        api = FakeApi(pages=[])
        out = io.StringIO()
        with mock.patch.object(lint, "http_json", api), redirect_stdout(out):
            code = lint.main(["--post-report", str(self.report)], env=self.env if env is None else env)
        self.assertEqual(code, 0)
        self.assertNotIn("tok-secret", out.getvalue())
        return api, out.getvalue()

    def test_posts_rebuilt_text(self):
        raw = dict(REPORT, files=[{"path": "secret/path.py"}], body="BODY-TEXT", failures=["a\n## b"])
        built = lint.comment_text(raw)
        with mock.patch.object(lint, "post_comment", return_value="created") as post:
            _api, out = self.post(json.dumps(raw))
        ctx, text, token = post.call_args.args
        self.assertEqual((ctx["platform"], ctx["project"], ctx["number"], token), ("github", "o/r", "9", "tok-secret"))
        self.assertEqual(text, built)
        self.assertIn("- a ## b", text)
        for needle in ("secret/path", "BODY-TEXT"):
            self.assertNotIn(needle, text)
        self.assertIn("판정 댓글 created", out)
        # 실제 HTTP 경로(대역)로 POST 한 번
        api, _ = self.post(json.dumps(raw))
        self.assertEqual(api.calls[-1][:2], ("POST", "https://api.github.com/repos/o/r/issues/9/comments"))
        self.assertEqual(api.calls[-1][3]["body"], built)

    def test_missing_or_garbled_report_is_warning(self):
        for content in (None, "not json", "[]", '{"result": "ok"}', b"\xff\xfe", "[" * 100000,
                        json.dumps({"result": "pass", "pad": "x" * (lint.REPORT_MAX_BYTES + 1)})):
            with self.subTest(content=(content or b"")[:20]):
                if content is None and self.report.exists():
                    self.report.unlink()
                api, out = self.post(content)
                self.assertIn("harness 경고", out)
                self.assertEqual(api.calls, [])

    def test_no_token_or_bad_context_is_warning(self):
        env = {k: v for k, v in self.env.items() if k != "HARNESS_COMMENT_TOKEN"}
        api, out = self.post(json.dumps(REPORT), env=env)
        self.assertIn("HARNESS_COMMENT_TOKEN", out)
        self.assertEqual(api.calls, [])
        api, out = self.post(json.dumps(REPORT), env={**self.env, "GITHUB_EVENT_NAME": "push"})
        self.assertIn("harness 경고", out)
        self.assertEqual(api.calls, [])

    def test_skipped_report_posts_nothing(self):
        """#41 T9: 기준 커밋 댓글 job은 건너뜀 리포트를 받아도 경고 없이 댓글을 남기지 않는다"""
        api, out = self.post(json.dumps({"result": "skipped", "reason": "integration", "failures": [], "tier": {}}))
        self.assertEqual(api.calls, [])
        self.assertIn("판정 댓글 skipped (결과 skipped)", out)
        self.assertNotIn("harness 경고", out)

    def test_api_failure_is_warning(self):
        error = urllib.error.HTTPError("https://api.github.com/x", 403, "Forbidden", {}, None)
        self.report.write_text(json.dumps(REPORT), encoding="utf-8")
        out = io.StringIO()
        with mock.patch.object(lint, "http_json", FakeApi(error=error)), redirect_stdout(out):
            code = lint.main(["--post-report", str(self.report)], env=self.env)
        self.assertEqual(code, 0)
        self.assertIn("HTTP 403", out.getvalue())


class FragmentTest(unittest.TestCase):
    def test_structure(self):
        """T17: core/ci 조각 관례(test_ci.py)와 같은 형식"""
        text = FRAGMENT.read_text(encoding="utf-8")
        head = "\n".join(line for line in text.splitlines() if line.startswith("#"))
        for needle in ("include:", "- remote: https://", "core/ci/gitlab/mr-lint.yml", "필요 조건:", "예외:",
                       "HARNESS_COMMENT_TOKEN", "16.7", "2700자", "read_api", "통합 MR"):
            self.assertIn(needle, head)
        self.assertEqual(re.findall(r"^([^\s#][^:]*):\s*$", text, re.M), ["harness-mr-lint"])
        self.assertRegex(text, r"name: python:[\w.-]+@sha256:[0-9a-f]{64}\n")
        self.assertIn('  rules:\n    - if: $CI_PIPELINE_SOURCE == "merge_request_event"\n  interruptible', text)
        self.assertIn('GIT_DEPTH: "0"', text)
        self.assertNotIn("allow_failure", text)
        self.assertNotIn("set +e", text)
        self.assertRegex(text, r"script:\n    - \|\n      set -eu\n")
        for path in (".harness/mr-lint/mr_lint.py", ".claude/hooks/harness_common.py", "harness.json"):
            self.assertIn(path, text)
        self.assertIn("python3 -I -B .harness/mr-lint/mr_lint.py --out mr-lint.json", text)
        self.assertRegex(text, r"artifacts:\n    when: always\n    expire_in: 30 days\n    paths:\n      - mr-lint.json")

    def test_manifest_copies_module_for_gitlab(self):
        entries = [e for e in harness.load_manifest() if e["dest"] == ".harness/mr-lint/mr_lint.py"]
        self.assertEqual(entries, [{"base": "ci", "src": "mr-lint/mr_lint.py", "dest": ".harness/mr-lint/mr_lint.py",
                                    "self": False, "render": False, "platform": "gitlab"}])


def job_lines(body, name):
    """ci.yml에서 job 하나의 본문 줄(주석·빈 줄·줄 끝 주석 제외)."""
    block = re.search(rf"^  {re.escape(name)}:\n((?:(?:    .*)?\n)+)", body, re.M).group(1)
    return [line.split("  #")[0].rstrip() for line in block.splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.body = WORKFLOW.read_text(encoding="utf-8")

    def test_ci_mr_lint_job(self):
        """T18: 결정 D-11·D-12·D-14. Codex 리뷰: PR 코드를 돌리는 lint job에는 쓰기 권한·토큰이 없다"""
        self.assertIn("  pull_request:\n    types: [opened, synchronize, reopened, edited]\n", self.body)
        self.assertNotIn("pull_request_target", self.body.replace("pull_request_target은", ""))
        lines = job_lines(self.body, "mr-lint")
        self.assertEqual(lines, [
            "    if: github.event_name == 'pull_request'",
            "    runs-on: ubuntu-latest",
            "    timeout-minutes: 5",
            "    concurrency:",
            "      group: mr-lint-${{ github.event.pull_request.number }}",
            "      cancel-in-progress: true",
            "    permissions:",
            "      contents: read",
            "    steps:",
            "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
            "        with:",
            "          fetch-depth: 0",
            "          persist-credentials: false",
            "      - name: PR 본문 lint",
            "        run: python3 core/ci/mr-lint/mr_lint.py --no-comment --out mr-lint.json",
            "      - uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
            "        if: always()",
            "        with:",
            "          name: mr-lint",
            "          path: mr-lint.json",
            "          retention-days: 30",
            "          if-no-files-found: ignore",
        ])
        joined = "\n".join(lines)
        for needle in ("pull-requests: write", "token", "TOKEN", "secrets.", "env:"):
            self.assertNotIn(needle, joined)

    def test_ci_mr_lint_comment_job(self):
        """Codex 리뷰: 댓글 job은 기준 커밋 코드만 토큰과 함께 돌리고 실패하지 않는다"""
        lines = job_lines(self.body, "mr-lint-comment")
        self.assertEqual(lines, [
            "    needs: mr-lint",
            "    if: always() && github.event_name == 'pull_request' && (needs.mr-lint.result == 'success'"
            " || needs.mr-lint.result == 'failure')",
            "    runs-on: ubuntu-latest",
            "    timeout-minutes: 5",
            "    concurrency:",
            "      group: mr-lint-comment-${{ github.event.pull_request.number }}",
            "      cancel-in-progress: true",
            "    permissions:",
            "      contents: read",
            "      pull-requests: write",
            "    steps:",
            "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
            "        with:",
            "          ref: ${{ github.event.pull_request.base.sha }}",
            "          persist-credentials: false",
            "      - uses: actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
            "        continue-on-error: true",
            "        with:",
            "          name: mr-lint",
            "          path: ${{ runner.temp }}/mr-lint",
            "      - name: PR 본문 lint 댓글",
            "        env:",
            "          HARNESS_COMMENT_TOKEN: ${{ github.token }}",
            "        run: |",
            "          if [ ! -f core/ci/mr-lint/mr_lint.py ]; then",
            "            echo \"::warning::기준 커밋에 core/ci/mr-lint/mr_lint.py가 없어 판정 댓글을 건너뛴다.\"",
            "            exit 0",
            "          fi",
            "          python3 -I -B core/ci/mr-lint/mr_lint.py --post-report \"$RUNNER_TEMP/mr-lint/mr-lint.json\""
            " || echo \"::warning::판정 댓글 단계가 실패했다(lint 결과와 무관).\"",
        ])
        # 토큰이 있는 job에서는 PR head를 checkout하지 않는다
        self.assertNotIn("head.sha", "\n".join(lines))
        self.assertNotIn("fetch-depth", "\n".join(lines))


class TemplateTest(unittest.TestCase):
    def test_sections_and_measurement_rows(self):
        """T19: 결정 D-8. MR 템플릿 측정 칸 = 리뷰 템플릿 6장 = lint 상수"""
        mr = (harness.TEMPLATES_DIR / "merge-request" / "Default.md").read_text(encoding="utf-8")
        sections = harness.markdown_sections(mr)
        for title in ("판정", "플랜 요약", "리뷰 결과", "검증", "영향 범위", "관련 업무"):
            self.assertIn(title, sections)
        self.assertTrue(sections["플랜 요약"].rstrip().endswith(lint.NOT_APPLICABLE))
        review = harness.markdown_sections(
            (harness.TEMPLATES_DIR / "docs" / "templates" / "review.md").read_text(encoding="utf-8"))
        measure = next(body for title, body in review.items() if title.startswith("6."))
        mr_rows = [row[0] for row in lint.table_rows(sections["리뷰 결과"].splitlines())]
        review_rows = [row[0] for row in lint.table_rows(measure.splitlines())]
        self.assertEqual(mr_rows, review_rows)
        self.assertEqual(len(mr_rows), len(lint.MEASUREMENT_ROWS))
        for name, row in zip(lint.MEASUREMENT_ROWS, mr_rows):
            self.assertIn(name, row)

    def test_non_strict_template_defaults_are_not_failures(self):
        """비엄격 본문은 플랜 요약·리뷰 결과를 그대로 둬도 통과한다."""
        self.assertEqual(lint.lint_body(filled("lite"), "lite")["failures"], [])


class RoadmapTest(unittest.TestCase):
    def test_m2_completion_note(self):
        """#41 T11(O-5): M2 완료 기준 바로 뒤에 댓글(토큰 있을 때만)·CI 플랜 검사(MR 본문 측정 칸) 해석이 있다"""
        roadmap = (ROOT / "docs" / "roadmap.md").read_text(encoding="utf-8")
        m2 = roadmap.split("### M2.", 1)[1].split("\n- [", 1)[0]
        criteria = m2.split("완료 기준:", 1)[1]
        for needle in ("HARNESS_COMMENT_TOKEN", "mr-lint.json", "plans/", "측정 칸"):
            self.assertIn(needle, criteria)


if __name__ == "__main__":
    unittest.main()
