"""#100 M5-5 hooks가 없는 도구를 위한 CI 측 보호: MR lint 보호 경로 확인과 변경 영역 검증."""

import importlib.util
import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_mr_lint import RepoCase, filled, git, lint  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("verify_changed", ROOT / "core" / "ci" / "verify" / "verify_changed.py")
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)
CONFIRMED = "- [x] 보호 경로 변경을 사람이 확인했다"


class ProtectedLintTest(RepoCase):
    def lint(self, body, head):
        (self.repo / "body.md").write_text(body, encoding="utf-8")
        out = self.repo / "out.json"
        with redirect_stdout(io.StringIO()) as stdout:
            code = lint.main(["--project", str(self.repo), "--body-file", str(self.repo / "body.md"),
                              "--base", self.base, "--head", head, "--out", str(out), "--no-comment"], env={})
        return code, json.loads(out.read_text(encoding="utf-8")), stdout.getvalue()

    def protected_failures(self, report):
        return [f for f in report["failures"] if f.startswith("보호 경로를 바꿨다")]

    def test_lock_file_without_confirmation_fails(self):
        """T1"""
        (self.repo / "backend" / "package-lock.json").write_text("{}", encoding="utf-8")
        code, report, _ = self.lint(filled(), self.commit())
        self.assertEqual(code, 1)
        [failure] = self.protected_failures(report)
        self.assertIn("`backend/package-lock.json`(lock 파일", failure)

    def test_migration_with_checked_confirmation_passes(self):
        """T2"""
        (self.repo / "backend" / "src" / "main" / "resources" / "db" / "migration").mkdir(parents=True)
        (self.repo / "backend" / "src" / "main" / "resources" / "db" / "migration" / "V1__a.sql").write_text(
            "create table a(id int);\n", encoding="utf-8")
        head = self.commit()
        body = filled().replace("- [ ] 보호 경로 변경을 사람이 확인했다", CONFIRMED)
        code, report, out = self.lint(body, head)
        self.assertEqual(self.protected_failures(report), [], out)
        self.assertEqual(code, 0, report["failures"])
        _, report, _ = self.lint(filled(), head)
        self.assertIn("DB 마이그레이션", self.protected_failures(report)[0])

    def test_unchecked_or_commented_confirmation_fails(self):
        """T3"""
        (self.repo / ".gitlab-ci.yml").write_text("x: 1\n", encoding="utf-8")
        head = self.commit()
        commented = filled().replace("- [ ] 보호 경로 변경을 사람이 확인했다", f"<!-- {CONFIRMED} -->")
        for body in (filled(), commented, filled().replace("## 보호 경로 확인", "## 확인 없음")):
            with self.subTest(body=body[-200:]):
                self.assertEqual(len(self.protected_failures(self.lint(body, head)[1])), 1)

    def test_rules_from_base_config(self):
        """T4: MR이 같은 MR에서 protected_paths를 비워도 대상 쪽 규칙으로 검사한다"""
        config = json.loads((self.repo / "harness.json").read_text(encoding="utf-8"))
        config["hooks"] = {"protected_paths": []}
        (self.repo / "harness.json").write_text(json.dumps(config), encoding="utf-8")
        (self.repo / "yarn.lock").write_text("x\n", encoding="utf-8")
        _, report, _ = self.lint(filled(), self.commit())
        paths = self.protected_failures(report)[0]
        self.assertIn("`yarn.lock`", paths)
        self.assertIn("`harness.json`", paths)  # 끌 수 없는 규칙

    def test_delete_rename_and_kit_code(self):
        """리뷰 A F5·F1: 보호 경로 삭제·이름 변경과 `.harness/`(CI 검사 코드) 변경도 확인을 요구한다"""
        (self.repo / "old.txt").rename(self.repo / "package-lock.json")  # 이름 변경으로 보호 경로를 만든다
        (self.repo / ".harness" / "mr-lint").mkdir(parents=True)
        (self.repo / ".harness" / "mr-lint" / "mr_lint.py").write_text("# 무력화\n", encoding="utf-8")
        failure = self.protected_failures(self.lint(filled(), self.commit())[1])[0]
        self.assertIn("`package-lock.json`", failure)
        self.assertIn("`.harness/mr-lint/mr_lint.py`(키트 CI 검사 코드)", failure)
        git(self.repo, "switch", "-q", "main")  # 기준에 보호 경로를 두고 MR에서 지운다
        (self.repo / "yarn.lock").write_text("x\n", encoding="utf-8")
        self.base = self.commit("lock")
        git(self.repo, "switch", "-q", "-c", "delete-lock")
        git(self.repo, "rm", "-q", "yarn.lock")
        self.assertIn("`yarn.lock`", self.protected_failures(self.lint(filled(), self.commit())[1])[0])

    def test_checkbox_in_code_block_does_not_count(self):
        (self.repo / "yarn.lock").write_text("x\n", encoding="utf-8")
        head = self.commit()
        body = filled().replace("- [ ] 보호 경로 변경을 사람이 확인했다", "```\n" + CONFIRMED + "\n```")
        self.assertEqual(len(self.protected_failures(self.lint(body, head)[1])), 1)

    def test_malformed_base_config_falls_back_to_defaults(self):
        """리뷰 B F1·A F2: 기준 커밋 설정이 잘못돼도 traceback 없이 기본 보호 경로로 검사한다"""
        git(self.repo, "switch", "-q", "main")
        config = json.loads((self.repo / "harness.json").read_text(encoding="utf-8"))
        config["hooks"] = {"protected_paths": [{"pattern": "x", "mode": "warn"}]}
        (self.repo / "harness.json").write_text(json.dumps(config), encoding="utf-8")
        self.base = self.commit("bad rules")
        git(self.repo, "switch", "-q", "-c", "fix-rules")
        (self.repo / "pnpm-lock.yaml").write_text("x\n", encoding="utf-8")
        code, report, out = self.lint(filled(), self.commit())
        self.assertIn("기준 설정이 잘못돼 기본 보호 경로로 검사한다", out)
        self.assertIn("`pnpm-lock.yaml`", self.protected_failures(report)[0])
        self.assertEqual(code, 1)

    def test_base_without_config_uses_mr_config(self):
        git(self.repo, "switch", "-q", "main")
        git(self.repo, "rm", "-q", "harness.json")
        self.base = self.commit("no config")
        git(self.repo, "switch", "-q", "-c", "add-config")
        (self.repo / "harness.json").write_text(json.dumps(self.config), encoding="utf-8")
        failure = self.protected_failures(self.lint(filled(), self.commit())[1])[0]
        self.assertIn("`harness.json`", failure)  # 설정 추가 자체가 끌 수 없는 규칙에 걸린다

    def test_no_protected_paths_unaffected(self):
        """T5"""
        (self.repo / "src.py").write_text("x\n", encoding="utf-8")
        code, report, _ = self.lint(filled(), self.commit())
        self.assertEqual((code, report["failures"]), (0, []))

    def test_rule_change_follows_hooks(self):
        """T6: 대상 쪽 설정 규칙을 바꾸면 lint 결과도 같이 바뀐다(hook과 같은 함수)"""
        git(self.repo, "switch", "-q", "main")
        config = json.loads((self.repo / "harness.json").read_text(encoding="utf-8"))
        config["hooks"] = {"protected_paths": [{"pattern": "docs/api/**", "mode": "ask", "reason": "API 계약"}]}
        (self.repo / "harness.json").write_text(json.dumps(config), encoding="utf-8")
        self.base = self.commit("rules")
        git(self.repo, "switch", "-q", "-c", "feature2")
        (self.repo / "docs" / "api").mkdir(parents=True)
        (self.repo / "docs" / "api" / "x.md").write_text("x\n", encoding="utf-8")
        (self.repo / "package-lock.json").write_text("{}", encoding="utf-8")  # 기본 목록을 대체했으므로 보호 아님
        _, report, _ = self.lint(filled(), self.commit())
        [failure] = self.protected_failures(report)
        self.assertIn("`docs/api/x.md`(API 계약)", failure)
        self.assertNotIn("package-lock.json", failure)


class VerifyChangedTest(RepoCase):
    def run_verify(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = verify.run(["--project", str(self.repo), *args])
        return code, out.getvalue(), err.getvalue()

    def set_config(self, **changes):
        config = json.loads((self.repo / "harness.json").read_text(encoding="utf-8"))
        config.update(changes)
        (self.repo / "harness.json").write_text(json.dumps(config), encoding="utf-8")

    def test_runs_changed_area_commands_only(self):
        """T8"""
        py = Path(sys.executable).as_posix()
        self.set_config(areas=[{"dir": "backend", "verify": [f'"{py}" -c "print(\'backend-ok\')"']},
                               {"dir": "web", "verify": [f'"{py}" -c "raise SystemExit(3)"']}],
                        hooks={"stop_verify": [f'"{py}" -c "print(\'root-ok\')"']})
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "config")
        (self.repo / "backend" / "src" / "auth" / "a.py").write_text("b\n", encoding="utf-8")
        self.commit()
        code, out, err = self.run_verify("--base", self.base)
        self.assertEqual(code, 0, err)
        self.assertIn("실행할 검증 명령 2개", out)  # backend 영역 + harness.json(영역 밖)
        (self.repo / "web").mkdir()
        (self.repo / "web" / "a.ts").write_text("x\n", encoding="utf-8")
        self.commit()
        code, _, err = self.run_verify("--base", self.base)
        self.assertEqual(code, 1)
        self.assertIn("실패", err)

    def test_bash_syntax_command(self):
        """#115: CI 검증도 종료 검증과 같은 셸(Windows는 Git Bash)에서 bash 문법 명령을 실행한다"""
        self.set_config(hooks={"stop_verify": ["test -d . && cd backend && test \"$(printf %s 'a\\\\b' | wc -c)\" -eq 4"]})
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "config")
        (self.repo / "top.txt").write_text("x\n", encoding="utf-8")
        self.commit()
        code, out, err = self.run_verify("--base", self.base)
        self.assertEqual(code, 0, out + err)

    def test_no_changes_and_errors(self):
        code, out, _ = self.run_verify("--base", "HEAD")
        self.assertEqual(code, 0)
        self.assertIn("실행할 검증 명령 0개", out)
        self.assertEqual(self.run_verify("--base", "origin/nope")[0], 2)
        self.assertEqual(self.run_verify("--base=--evil")[0], 2)  # git 옵션으로 해석되지 않게 거절


class VerifyLoadTest(unittest.TestCase):
    def test_broken_common_is_exit_2(self):
        """리뷰 B F3"""
        import tempfile
        from tempdirs import remove_tree
        tmp = Path(tempfile.mkdtemp(prefix="harness-verify-"))
        self.addCleanup(remove_tree, tmp)
        (tmp / "bad.py").write_text("def (:\n", encoding="utf-8")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            code = verify.run(["--project", str(tmp), "--base", "HEAD", "--common", str(tmp / "bad.py")])
        self.assertEqual(code, 2)
        self.assertIn("읽을 수 없다", err.getvalue())


class FragmentTest(unittest.TestCase):
    def test_verify_fragment_header_and_manifest(self):
        """T9"""
        text = (ROOT / "core" / "ci" / "gitlab" / "verify.yml").read_text(encoding="utf-8")
        for needle in ("include:", "- remote:", "HARNESS_VERIFY_IMAGE", "필요 조건:", "예외:", "harness-verify:",
                       ".harness/verify/verify_changed.py"):
            self.assertIn(needle, text)
        self.assertIn("CI_MERGE_REQUEST_DIFF_BASE_SHA", text)  # fork MR도 대상 쪽 기준과 비교한다(리뷰 B F2)
        self.assertNotIn("origin/$CI_MERGE_REQUEST_TARGET_BRANCH_NAME", text)

    def test_init_installs_verify_for_gitlab_only(self):
        """리뷰 B F4: 실제 init·check로 GitLab 소비자에만 설치되고 드리프트를 본다"""
        import tempfile
        from test_harness import run
        from tempdirs import remove_tree
        tmp = Path(tempfile.mkdtemp(prefix="harness-verify-init-"))
        self.addCleanup(remove_tree, tmp)
        gitlab, github = tmp / "gl", tmp / "gh"
        self.assertEqual(run(["init", str(gitlab), "--platform", "gitlab", "--tracker", "jira",
                              "--issue-prefix", "DEMO"])[0], 0)
        installed = gitlab / ".harness" / "verify" / "verify_changed.py"
        self.assertEqual(installed.read_bytes(), (ROOT / "core" / "ci" / "verify" / "verify_changed.py").read_bytes())
        self.assertEqual(run(["check", str(gitlab)])[0], 0)
        installed.write_text("# 변조\n", encoding="utf-8")
        code, out = run(["check", str(gitlab)])
        self.assertEqual(code, 1)
        self.assertIn("불일치: .harness/verify/verify_changed.py", out)
        self.assertEqual(run(["init", str(github), "--platform", "github", "--tracker", "github"])[0], 0)
        self.assertFalse((github / ".harness").exists())


if __name__ == "__main__":
    unittest.main()
