"""bin/harness.py 단위·리허설 테스트. `python -m unittest discover tests -v`로 실행한다."""

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tempdirs import remove_tree  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("harness", ROOT / "bin" / "harness.py")
harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)


def run(argv):
    out = io.StringIO()
    with redirect_stdout(out):
        code = harness.main(argv)
    return code, out.getvalue()


class RenderTest(unittest.TestCase):
    def test_placeholders_are_replaced(self):
        ctx = harness.build_context({
            "project_name": "demo", "platform": "gitlab", "tracker": "jira",
            "issue_prefix": "ABC123", "default_branch": "main", "integration_branch": "develop",
            "related_docs": [{"label": "규칙", "path": "docs/a.md"}],
        })
        text = harness.render("{{pr_noun}} {{issue_key_example}}\n{{related_docs}}", ctx)
        self.assertEqual(text, "MR ABC123-52\n- [규칙](docs/a.md)")

    def test_github_context(self):
        ctx = harness.build_context({
            "project_name": "demo", "platform": "github", "tracker": "github",
            "default_branch": "main", "integration_branch": "main", "related_docs": [],
        })
        self.assertEqual(ctx["pr_noun"], "PR")
        self.assertEqual(ctx["issue_key_example"], "#52")
        self.assertEqual(ctx["related_docs"], "")

    def test_unknown_placeholder_fails(self):
        ctx = harness.build_context({
            "project_name": "demo", "platform": "github", "tracker": "github",
            "default_branch": "main", "integration_branch": "main",
        })
        with self.assertRaises(harness.HarnessError):
            harness.render("{{no_such_key}}", ctx)

    def test_malformed_placeholders_fail(self):
        for template in ("{{Bad-Name}}", "{{Missing}}", "{{unfinished", "orphan }}"):
            with self.subTest(template=template), self.assertRaises(harness.HarnessError):
                harness.render(template, {})

    def test_jira_requires_prefix(self):
        with self.assertRaises(harness.HarnessError):
            harness.validate_config({
                "project_name": "demo", "platform": "gitlab", "tracker": "jira",
                "default_branch": "main", "integration_branch": "develop",
            }, "test")

    def test_manifest_rejects_invalid_entries(self):
        cases = [{"files": {}}, {"files": [{"src": "../secret", "dest": "x"}]},
                 {"files": [{"src": "AGENTS.md", "dest": "x"},
                            {"src": "CLAUDE.md", "dest": "x"}]},
                 {"files": [{"src": "AGENTS.md", "dest": "/absolute"}]},
                 {"files": [{"src": "AGENTS.md", "dest": "x", "base": []}]},
                 {"files": [{"src": "AGENTS.md", "dest": "x", "platform": []}]}]
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "manifest.json"
            with patch.object(harness, "MANIFEST_PATH", manifest):
                for data in cases:
                    with self.subTest(data=data):
                        manifest.write_text(json.dumps(data), encoding="utf-8")
                        with self.assertRaises(harness.HarnessError):
                            harness.load_manifest()

    def test_every_template_renders_for_both_platforms(self):
        for platform, tracker in (("gitlab", "jira"), ("github", "github")):
            config = {
                "project_name": "demo", "platform": platform, "tracker": tracker,
                "issue_prefix": "ABC123", "default_branch": "main",
                "integration_branch": "develop", "related_docs": harness.CONSUMER_RELATED_DOCS,
            }
            outputs = harness.render_all(config, self_mode=False)
            self.assertTrue(outputs)
            for dest, content in outputs.items():
                self.assertNotIn("{{", content, f"{dest}에 치환되지 않은 자리표시자가 남았다")


class InitAndCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-test-"))

    def tearDown(self):
        remove_tree(self.tmp)

    def test_init_then_check_is_clean_for_gitlab_jira(self):
        target = self.tmp / "consumer"
        code, _ = run(["init", str(target), "--platform", "gitlab", "--tracker", "jira",
                       "--issue-prefix", "DEMO"])
        self.assertEqual(code, 0)
        self.assertTrue((target / "AGENTS.md").is_file())
        self.assertTrue((target / ".gitlab" / "merge_request_templates" / "Default.md").is_file())
        self.assertFalse((target / ".github").exists())
        config = json.loads((target / "harness.json").read_text(encoding="utf-8"))
        self.assertEqual(config["harness_version"], harness.VERSION)
        self.assertIn("DEMO-52", (target / "docs" / "git-convention.md").read_text(encoding="utf-8"))

        code, out = run(["check", str(target)])
        self.assertEqual(code, 0, out)

    def test_init_github_writes_pull_request_template(self):
        target = self.tmp / "gh"
        code, _ = run(["init", str(target), "--platform", "github", "--tracker", "github"])
        self.assertEqual(code, 0)
        self.assertTrue((target / ".github" / "PULL_REQUEST_TEMPLATE.md").is_file())
        self.assertFalse((target / ".gitlab").exists())

    def test_init_refuses_to_overwrite_without_force(self):
        target = self.tmp / "existing"
        target.mkdir()
        (target / "AGENTS.md").write_text("mine", encoding="utf-8")
        code, _ = run(["init", str(target), "--tracker", "github", "--platform", "github"])
        self.assertEqual(code, 2)
        self.assertEqual((target / "AGENTS.md").read_text(encoding="utf-8"), "mine")

    def test_check_detects_drift(self):
        target = self.tmp / "drift"
        run(["init", str(target), "--platform", "github", "--tracker", "github"])
        agents = target / "AGENTS.md"
        agents.write_text(agents.read_text(encoding="utf-8") + "\n손으로 고친 줄\n", encoding="utf-8")
        code, out = run(["check", str(target)])
        self.assertEqual(code, 1)
        self.assertIn("불일치: AGENTS.md", out)

    def test_existing_config_rejects_root_flags_without_changes(self):
        target = self.tmp / "existing-config"
        self.assertEqual(run(["init", str(target), "--platform", "github", "--tracker", "github"])[0], 0)
        before = (target / "harness.json").read_bytes()
        err = io.StringIO()
        with redirect_stderr(err):
            code, _ = run(["init", str(target), "--force", "--platform", "gitlab"])
        self.assertEqual(code, 2)
        self.assertIn("기존 harness.json", err.getvalue())
        self.assertEqual((target / "harness.json").read_bytes(), before)


class GitignoreInitTest(unittest.TestCase):
    """T20: 결정 D-13. init은 `.gitignore`에 `/plans/`와 개인 설정 `/.claude/settings.local.json`이 없을 때만
    줄을 추가하고 기존 내용을 보존한다(#85)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-gitignore-"))
        self.addCleanup(remove_tree, self.tmp)

    def init(self, target, *extra):
        return run(["init", str(target), "--platform", "github", "--tracker", "github", *extra])

    def test_created_when_absent(self):
        target = self.tmp / "new"
        code, out = self.init(target)
        self.assertEqual(code, 0)
        lines = (target / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("/plans/", lines)
        self.assertIn("/.claude/settings.local.json", lines)
        self.assertIn(".gitignore 에 /plans/ 를 추가했다.", out)
        self.assertIn(".gitignore 에 /.claude/settings.local.json 를 추가했다.", out)
        code, out = run(["init", str(target), "--force"])  # 두 번째 실행은 줄을 늘리지 않는다
        self.assertEqual(code, 0)
        self.assertEqual((target / ".gitignore").read_text(encoding="utf-8").splitlines(), lines)
        self.assertNotIn(".gitignore", out)

    def test_settings_local_is_ignored_by_git(self):
        target = self.tmp / "git"
        target.mkdir()
        subprocess.run(["git", "init", "-q", str(target)], check=True)
        self.assertEqual(self.init(target)[0], 0)
        (target / ".claude" / "settings.local.json").write_text("{}", encoding="utf-8")
        result = subprocess.run(["git", "-C", str(target), "check-ignore", "-q", ".claude/settings.local.json"])
        self.assertEqual(result.returncode, 0)

    def test_appended_preserving_content(self):
        target = self.tmp / "existing"
        target.mkdir()
        (target / ".gitignore").write_bytes(b"node_modules/\r\n.env")  # CRLF, 끝 개행 없음
        self.assertEqual(self.init(target)[0], 0)
        data = (target / ".gitignore").read_bytes()
        self.assertTrue(data.startswith(b"node_modules/\r\n.env\r\n"))
        self.assertIn(b"\r\n/.claude/settings.local.json\r\n", data)
        self.assertTrue(data.endswith(b"\r\n/plans/\r\n"))

    def test_only_missing_line_appended(self):
        target = self.tmp / "partial"
        target.mkdir()
        (target / ".gitignore").write_bytes(b"/plans/\n")
        code, out = self.init(target)
        self.assertEqual(code, 0)
        self.assertEqual((target / ".gitignore").read_text(encoding="utf-8").splitlines().count("/plans/"), 1)
        self.assertIn(".gitignore 에 /.claude/settings.local.json 를 추가했다.", out)
        self.assertNotIn("/plans/ 를 추가했다", out)

    def test_untouched_when_present(self):
        target = self.tmp / "present"
        target.mkdir()
        original = b"# mine\n  /plans/  \n.claude/settings.local.json\n"
        (target / ".gitignore").write_bytes(original)
        code, out = self.init(target)
        self.assertEqual(code, 0)
        self.assertEqual((target / ".gitignore").read_bytes(), original)
        self.assertNotIn(".gitignore", out)

    def test_not_written_when_init_aborts(self):
        target = self.tmp / "collide"
        target.mkdir()
        (target / "AGENTS.md").write_text("mine", encoding="utf-8")
        (target / ".gitignore").write_bytes(b"dist/\n")
        self.assertEqual(self.init(target)[0], 2)
        self.assertEqual((target / ".gitignore").read_bytes(), b"dist/\n")

    def test_gitlab_consumer_gets_mr_lint_module(self):
        """T21: GitLab 조각이 실행할 모듈은 gitlab 소비자에만 복사되고 check가 드리프트를 본다."""
        gitlab = self.tmp / "gl"
        self.assertEqual(run(["init", str(gitlab), "--platform", "gitlab", "--tracker", "jira",
                              "--issue-prefix", "DEMO"])[0], 0)
        copied = gitlab / ".harness" / "mr-lint" / "mr_lint.py"
        self.assertEqual(copied.read_bytes(), (ROOT / "core" / "ci" / "mr-lint" / "mr_lint.py").read_bytes())
        self.assertEqual(run(["check", str(gitlab)])[0], 0)
        copied.write_text(copied.read_text(encoding="utf-8") + "\n# 변경\n", encoding="utf-8")
        code, out = run(["check", str(gitlab)])
        self.assertEqual(code, 1)
        self.assertIn("불일치: .harness/mr-lint/mr_lint.py", out)
        github = self.tmp / "gh"
        self.assertEqual(self.init(github)[0], 0)
        self.assertFalse((github / ".harness").exists())


class AreaTestBase(unittest.TestCase):
    """루트 init을 마친 대상 저장소에서 영역 AGENTS.md를 다룬다."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-area-"))
        self.target = self.tmp / "consumer"
        code, _ = run(["init", str(self.target), "--platform", "gitlab", "--tracker", "jira",
                       "--issue-prefix", "DEMO"])
        self.assertEqual(code, 0)

    def tearDown(self):
        remove_tree(self.tmp)

    def init_area(self, *extra):
        return run(["init", str(self.target), "--area", "backend", *extra])

    def config(self):
        return json.loads((self.target / "harness.json").read_text(encoding="utf-8"))

    def area_text(self):
        return (self.target / "backend" / "AGENTS.md").read_text(encoding="utf-8")


class AreaInitTest(AreaTestBase):
    def test_area_file_rendered(self):
        code, _ = self.init_area("--verify-cmd", "./gradlew build")
        self.assertEqual(code, 0)
        text = self.area_text()
        self.assertIn("- `./gradlew build`", text)
        for i, trigger in enumerate(harness.DEFAULT_TRIGGERS, 1):
            self.assertIn(f"{i}. {trigger}", text)
        self.assertIn("plans/<Jira Issue Key>.md", text)
        self.assertIn("MR 본문", text)
        self.assertNotIn("{{", text)

    def test_area_recorded_in_config(self):
        before = self.config()
        self.init_area("--verify-cmd", "./gradlew build")
        after = self.config()
        self.assertEqual(after["areas"], [{"dir": "backend", "verify": ["./gradlew build"],
                                           "triggers": harness.DEFAULT_TRIGGERS,
                                           "review_focus": harness.DEFAULT_REVIEW_FOCUS,
                                           "trigger_paths": harness.hooks_common.DEFAULT_TRIGGER_PATHS,
                                           "test_paths": harness.hooks_common.DEFAULT_TEST_PATHS}])
        for key, value in before.items():
            self.assertEqual(after[key], value)

    def test_saved_defaults_survive_kit_default_change(self):
        self.init_area("--verify-cmd", "make test")
        with patch.object(harness, "DEFAULT_TRIGGERS", ["changed"]), \
                patch.object(harness, "DEFAULT_REVIEW_FOCUS", ["changed"]):
            code, out = run(["check", str(self.target)])
        self.assertEqual(code, 0, out)

    def test_judge_rules_preserved_on_reinit(self):
        self.init_area("--verify-cmd", "make test")
        config = self.config()
        config["areas"][0]["trigger_paths"] = {"strict": ["/src/auth/"]}
        config["areas"][0]["test_paths"] = ["it/"]
        (self.target / "harness.json").write_text(json.dumps(config), encoding="utf-8")
        code, _ = self.init_area("--verify-cmd", "make check", "--force")
        self.assertEqual(code, 0)
        area = self.config()["areas"][0]
        self.assertEqual(area["trigger_paths"], {"strict": ["/src/auth/"]})
        self.assertEqual(area["test_paths"], ["it/"])
        self.assertEqual(area["verify"], ["make check"])

    def test_trigger_override(self):
        self.init_area("--verify-cmd", "make test", "--trigger", "A 변경", "--trigger", "B 변경")
        text = self.area_text()
        self.assertIn("1. A 변경\n2. B 변경\n", text)
        for trigger in harness.DEFAULT_TRIGGERS:
            self.assertNotIn(trigger, text)

    def test_reinit_updates_entry(self):
        self.init_area("--verify-cmd", "old", "--trigger", "custom", "--review-focus", "security",
                       "--area-doc", "docs/api.md")
        code, _ = self.init_area("--verify-cmd", "new", "--force")
        self.assertEqual(code, 0)
        areas = self.config()["areas"]
        self.assertEqual(len(areas), 1)
        self.assertEqual(areas[0]["verify"], ["new"])
        self.assertEqual(areas[0]["triggers"], ["custom"])
        self.assertEqual(areas[0]["review_focus"], ["security"])
        self.assertEqual(areas[0]["docs"], ["docs/api.md"])
        self.assertIn("- `new`", self.area_text())

    def test_existing_file_needs_force(self):
        area = self.target / "backend"
        area.mkdir()
        (area / "AGENTS.md").write_text("mine", encoding="utf-8")
        code, _ = self.init_area("--verify-cmd", "make test")
        self.assertEqual(code, 2)
        self.assertEqual((area / "AGENTS.md").read_text(encoding="utf-8"), "mine")
        self.assertNotIn("areas", self.config())

    def test_requires_config(self):
        bare = self.tmp / "bare"
        bare.mkdir()
        err = io.StringIO()
        with redirect_stderr(err):
            code, _ = run(["init", str(bare), "--area", "backend", "--verify-cmd", "make test"])
        self.assertEqual(code, 2)
        self.assertIn("루트 init", err.getvalue())
        self.assertEqual(list(bare.iterdir()), [])

    def test_requires_verify_cmd(self):
        code, _ = self.init_area()
        self.assertEqual(code, 2)
        self.assertFalse((self.target / "backend").exists())

    def test_rejects_escaping_dir(self):
        for bad in ("../x", "a/../../x", "/abs", "C:\\abs", "C:/abs", ".", ""):
            with self.subTest(dir=bad):
                code, _ = run(["init", str(self.target), "--area", bad, "--verify-cmd", "t"])
                self.assertEqual(code, 2)
        self.assertFalse((self.tmp / "x").exists())
        self.assertNotIn("areas", self.config())

    def test_default_docs_do_not_reference_missing_files(self):
        self.init_area("--verify-cmd", "make test")
        text = self.area_text()
        self.assertIn(harness.NO_AREA_DOCS, text)
        self.assertNotIn("backend/README.md", text)

    def test_area_doc_rendered(self):
        self.init_area("--verify-cmd", "make test", "--area-doc", "docs/api/README.md")
        text = self.area_text()
        self.assertIn("- `docs/api/README.md`", text)
        self.assertNotIn(harness.NO_AREA_DOCS, text)

    def test_rejects_root_flags(self):
        cases = (["--config", "other.json"], ["--platform", "github"], ["--tracker", "github"],
                 ["--project-name", "x"], ["--issue-prefix", "X"],
                 ["--default-branch", "main"], ["--integration-branch", "main"])
        for extra in cases:
            with self.subTest(flag=extra[0]):
                code, _ = self.init_area("--verify-cmd", "t", *extra)
                self.assertEqual(code, 2)
        self.assertFalse((self.target / "backend").exists())
        self.assertNotIn("areas", self.config())

    def test_area_flags_need_area(self):
        code, _ = run(["init", str(self.tmp / "other"), "--verify-cmd", "t"])
        self.assertEqual(code, 2)
        self.assertFalse((self.tmp / "other").exists())


class ConfigTest(unittest.TestCase):
    BASE = {"project_name": "demo", "platform": "github", "tracker": "github",
            "default_branch": "main", "integration_branch": "main"}

    def test_invalid_root_fields_rejected(self):
        bad = [None, [], {**self.BASE, "platfrom": "gitlab"},
               {**self.BASE, "project_name": 1}, {**self.BASE, "default_branch": []},
               {**self.BASE, "harness_version": 2}, {**self.BASE, "issue_prefix": []},
               {**self.BASE, "hooks": None},
               {**self.BASE, "tracker": "jira", "issue_prefix": "   "},
               {**self.BASE, "related_docs": [{"label": "docs", "path": "a", "typo": True}]}]
        for config in bad:
            with self.subTest(config=config), self.assertRaises(harness.HarnessError):
                harness.validate_config(config, "test")

    def test_removed_claude_review_rejected(self):
        """#43: Claude MR 리뷰를 제거해 claude_review 블록과 매니페스트 requires는 더 이상 받지 않는다."""
        config = {**self.BASE, "platform": "gitlab", "tracker": "jira", "issue_prefix": "DEMO",
                  "claude_review": {"target_branch": "develop"}}
        with self.assertRaisesRegex(harness.HarnessError, "알 수 없는 설정 키: claude_review.*제거됐다.*\.harness/claude-review/"):
            harness.validate_config(config, "test")
        real = harness.read_manifest()
        entry = {"src": "AGENTS.md", "dest": "X.md", "requires": "claude_review"}
        with patch.object(harness, "read_manifest", return_value={**real, "files": [entry]}), \
                self.assertRaises(harness.HarnessError):
            harness.load_manifest()

    def test_invalid_areas_rejected(self):
        bad_areas = [
            "backend",
            [{"verify": ["t"]}],
            [{"dir": "backend"}],
            [{"dir": "backend", "verify": []}],
            [{"dir": "backend", "verify": [1]}],
            [{"dir": "backend", "verify": ["t"], "triggers": []}],
            [{"dir": "backend", "verify": ["t"], "docs": [""]}],
            [{"dir": "../backend", "verify": ["t"]}],
            [{"dir": "./backend", "verify": ["t"]}],
            [{"dir": "backend", "verify": ["t"]}, {"dir": "backend", "verify": ["u"]}],
            [{"dir": "backend", "verify": ["t"], "trigger": ["오타"]}],
            [{"dir": "backend", "verify": ["t"], "trigger_paths": ["/src/"]}],
            [{"dir": "backend", "verify": ["t"], "trigger_paths": {"lite": ["x"]}}],
            [{"dir": "backend", "verify": ["t"], "trigger_paths": {"strict": "/src/"}}],
            [{"dir": "backend", "verify": ["t"], "trigger_paths": {"strict": [""]}}],
            [{"dir": "backend", "verify": ["t"], "test_paths": "tests/"}],
            [{"dir": "backend", "verify": ["t"], "test_paths": [" "]}],
        ]
        for areas in bad_areas:
            with self.subTest(areas=areas):
                with self.assertRaises(harness.HarnessError):
                    harness.validate_config({**self.BASE, "areas": areas}, "test")

    def test_valid_areas_accepted(self):
        harness.validate_config({**self.BASE, "areas": [
            {"dir": "apps/web", "verify": ["npm test"], "review_focus": ["접근성"]},
            {"dir": "backend", "verify": ["t"], "trigger_paths": {"strict": ["/src/auth/"], "standard": []},
             "test_paths": []},
            {"dir": "infra", "verify": ["t"], "trigger_paths": {"standard": ["*.tf"]}},
        ]}, "test")

    def test_invalid_hooks_rejected(self):
        bad_hooks = [
            [],
            {"protected_path": []},
            {"protected_paths": "x"},
            {"protected_paths": [{"pattern": "x", "mode": "warn"}]},
            {"protected_paths": [{"pattern": "", "mode": "ask"}]},
            {"protected_paths": [{"pattern": "x", "mode": "ask", "why": "오타"}]},
            {"protected_paths": [{"pattern": "x", "mode": "ask", "reason": ""}]},
            {"stop_verify": []},
            {"stop_verify": [1]},
            {"stop_timeout_sec": 0},
            {"stop_timeout_sec": 841},
            {"stop_timeout_sec": True},
            {"stop_timeout_sec": "60"},
            {"python": ""},
            {"python": 'py "x'},
            {"python": "C:\\py.exe"},
            {"python": "python\nrm"},
            {"python": "python; true"},
            {"python": "python && true"},
            {"python": "py -3; true"},
        ]
        for hooks in bad_hooks:
            with self.subTest(hooks=hooks):
                with self.assertRaises(harness.HarnessError):
                    harness.validate_config({**self.BASE, "hooks": hooks}, "test")

    def test_valid_hooks_accepted(self):
        harness.validate_config(self.BASE, "test")
        harness.validate_config({**self.BASE, "hooks": {
            "protected_paths": [{"pattern": "docs/api/**", "mode": "block", "reason": "API 계약"},
                                {"pattern": "infra/", "mode": "ask"}],
            "stop_verify": ["npm test"], "stop_timeout_sec": 840, "python": "py -3",
        }}, "test")
        harness.validate_config({**self.BASE, "hooks": {"protected_paths": []}}, "test")


class HookInstallTest(unittest.TestCase):
    HOOK_FILES = ("protect-paths.py", "stop-verify.py", "session-start.py", "session-end.py", "harness_common.py")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-hook-install-"))

    def tearDown(self):
        remove_tree(self.tmp)

    def init(self, name, *extra):
        target = self.tmp / name
        code, out = run(["init", str(target), *extra])
        self.assertEqual(code, 0, out)
        return target

    def test_init_installs_hooks(self):
        for platform, tracker in (("gitlab", "jira"), ("github", "github")):
            with self.subTest(platform=platform):
                target = self.init(platform, "--platform", platform, "--tracker", tracker,
                                   "--issue-prefix", "DEMO")
                text = (target / ".claude" / "settings.json").read_text(encoding="utf-8")
                self.assertNotIn("{{", text)
                settings = json.loads(text)["hooks"]
                self.assertEqual(settings["PreToolUse"][0]["matcher"], "Edit|Write|MultiEdit|NotebookEdit")
                self.assertEqual(settings["Stop"][0]["hooks"][0]["timeout"], 900)
                self.assertEqual(settings["SessionStart"][0]["hooks"][0]["timeout"], 30)  # #82 종료 검증 기준선
                self.assertNotIn("matcher", settings["SessionStart"][0])  # startup·resume·clear·compact 모두
                self.assertEqual(settings["SessionStart"][0]["hooks"][0]["command"],
                                 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/session-start.py"')
                self.assertEqual(settings["SessionEnd"][0]["hooks"][0]["timeout"], 30)  # #96 세션 기록
                self.assertNotIn("matcher", settings["SessionEnd"][0])
                self.assertEqual(settings["SessionEnd"][0]["hooks"][0]["command"],
                                 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/session-end.py"')
                self.assertTrue(settings["PreToolUse"][0]["hooks"][0]["command"].startswith(
                    'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/protect-paths.py"'))
                for name in self.HOOK_FILES:
                    installed = (target / ".claude" / "hooks" / name).read_text(encoding="utf-8")
                    original = (harness.HOOKS_DIR / name).read_text(encoding="utf-8")
                    self.assertEqual(installed, original)
                code, out = run(["check", str(target)])
                self.assertEqual(code, 0, out)

    def test_check_detects_script_tamper(self):
        target = self.init("tamper", "--platform", "github", "--tracker", "github")
        script = target / ".claude" / "hooks" / "protect-paths.py"
        script.write_text(script.read_text(encoding="utf-8").replace("deny", "allow"), encoding="utf-8")
        code, out = run(["check", str(target)])
        self.assertEqual(code, 1)
        self.assertIn("불일치: .claude/hooks/protect-paths.py", out)

    def test_check_detects_extra_hook_file(self):
        target = self.init("extra", "--platform", "github", "--tracker", "github")
        (target / ".claude" / "hooks" / "harness_common").mkdir()
        code, out = run(["check", str(target)])
        self.assertEqual(code, 1)
        self.assertIn("여분:   .claude/hooks/harness_common", out)

    def test_check_detects_version_mismatch(self):
        target = self.init("version", "--platform", "github", "--tracker", "github")
        path = target / "harness.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["harness_version"] = "0.1.0"
        path.write_text(json.dumps(config), encoding="utf-8")
        code, out = run(["check", str(target)])
        self.assertEqual(code, 1)
        self.assertIn("버전 불일치", out)

    def test_hook_python_rendered(self):
        target = self.init("py", "--platform", "github", "--tracker", "github")
        config_path = target / "harness.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["hooks"] = {"python": "python"}
        config_path.write_text(json.dumps(config), encoding="utf-8")
        code, out = run(["init", str(target), "--force"])
        self.assertEqual(code, 0, out)
        settings = json.loads((target / ".claude" / "settings.json").read_text(encoding="utf-8"))
        command = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
        self.assertTrue(command.startswith('python "$CLAUDE_PROJECT_DIR/.claude/hooks/stop-verify.py"'))

    def test_existing_settings_need_force(self):
        target = self.tmp / "existing"
        (target / ".claude").mkdir(parents=True)
        (target / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
        code, _ = run(["init", str(target), "--platform", "github", "--tracker", "github"])
        self.assertEqual(code, 2)
        self.assertEqual((target / ".claude" / "settings.json").read_text(encoding="utf-8"), "{}")

    def test_installed_hook_runs_from_target(self):
        target = self.init("run", "--platform", "github", "--tracker", "github")
        payload = {"tool_name": "Write", "tool_input": {"file_path": str(target / "package-lock.json")}}
        result = subprocess.run(
            [sys.executable, str(target / ".claude" / "hooks" / "protect-paths.py")],
            input=json.dumps(payload).encode("utf-8"), capture_output=True, timeout=60,
            env={**os.environ, "CLAUDE_PROJECT_DIR": str(target)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        decision = json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(decision, "deny")
        self.assertFalse((target / ".claude" / "hooks" / "__pycache__").exists())  # 대상에 부산물을 남기지 않는다


class AreaCheckTest(AreaTestBase):
    def setUp(self):
        super().setUp()
        code, _ = self.init_area("--verify-cmd", "./gradlew build")
        self.assertEqual(code, 0)
        self.agents = self.target / "backend" / "AGENTS.md"

    def test_clean_after_area_init(self):
        code, out = run(["check", str(self.target)])
        self.assertEqual(code, 0, out)

    def test_area_drift_detected(self):
        self.agents.write_text(self.area_text() + "\n손으로 고친 줄\n", encoding="utf-8")
        code, out = run(["check", str(self.target)])
        self.assertEqual(code, 1)
        self.assertIn("불일치: backend/AGENTS.md", out)

    def test_area_missing_detected(self):
        self.agents.unlink()
        code, out = run(["check", str(self.target)])
        self.assertEqual(code, 1)
        self.assertIn("없음:   backend/AGENTS.md", out)


class AreaTemplateTest(unittest.TestCase):
    LINE_BUDGET = 71  # M2-5: 리뷰 관점 원본의 공통 절(노이즈 제외·코드 대조)을 영역 문서에도 넣는다

    def rendered(self):
        ctx = harness.build_context({
            "project_name": "demo", "platform": "gitlab", "tracker": "jira",
            "issue_prefix": "DEMO", "default_branch": "main", "integration_branch": "develop",
        })
        return harness.render_area({"dir": "backend", "verify": ["./gradlew build"]}, ctx)

    def test_line_budget(self):
        lines = self.rendered().splitlines()
        self.assertLessEqual(len(lines), self.LINE_BUDGET)

    def test_rules_are_tagged(self):
        """3장 이후의 규칙 목록 항목마다 강제 주체 표시가 있어야 한다."""
        text = self.rendered()
        body = text[text.index("## 3. 판정"):]
        rules = [line for line in body.splitlines() if line.startswith("- ")]
        self.assertTrue(rules)
        for line in rules:
            with self.subTest(rule=line[:40]):
                self.assertRegex(line, r"\[(hook|ci|사람)\]$")


class IncludeTest(unittest.TestCase):
    """manifest includes: 리뷰 관점 원본(review-perspectives.md)의 절을 자리표시자로 넣는다."""

    CONFIG = {"project_name": "demo", "platform": "gitlab", "tracker": "jira", "issue_prefix": "DEMO",
              "default_branch": "main", "integration_branch": "develop"}

    def source_sections(self):
        text = (harness.TEMPLATES_DIR / "review-perspectives.md").read_text(encoding="utf-8")
        return harness.markdown_sections(text)

    def first_bullet(self, section):
        line = next(l for l in self.source_sections()[section].splitlines() if l.startswith("- "))
        return line.split("{{")[0][:30]  # 자리표시자 앞까지만 비교한다

    def test_sections_composed(self):
        self.assertEqual(list(self.source_sections()), ["공통", "로컬"])  # CI 절은 #43에서 제거했다
        ctx = harness.build_context(self.CONFIG)
        self.assertNotIn("review_perspectives_ci", ctx)
        local = ctx["review_perspectives"]
        for section in ("공통", "로컬"):
            self.assertIn(self.first_bullet(section), local, section)
        self.assertLess(local.index(self.first_bullet("공통")), local.index(self.first_bullet("로컬")))
        self.assertNotIn("## ", local)
        self.assertNotIn("\n\n", local)  # 절을 한 목록으로 잇는다
        self.assertTrue(all(line.startswith("- ") for line in local.splitlines()))
        self.assertTrue(all(line.endswith(" [사람]") for line in local.splitlines()))
        # tag가 없는 include에는 강제 주체 표시를 붙이지 않는다
        untagged = {"name": "x_inc", "src": "review-perspectives.md", "sections": ["공통"]}
        real = harness.read_manifest()
        with patch.object(harness, "read_manifest", return_value={**real, "includes": [untagged]}):
            plain = harness.build_context(self.CONFIG)["x_inc"]
        self.assertIn(self.first_bullet("공통"), plain)
        self.assertNotIn("[사람]", plain)

    def test_include_rendered_with_context(self):
        for platform, noun in (("gitlab", "MR"), ("github", "PR")):
            ctx = harness.build_context({**self.CONFIG, "platform": platform})
            self.assertNotIn("{{", ctx["review_perspectives"])
            # 원본 절에 자리표시자가 있으면 같은 컨텍스트로 렌더한다
            with patch.object(harness, "include_text", lambda inc: "- {{pr_noun}} 설명"):
                ctx = harness.build_context({**self.CONFIG, "platform": platform})
            self.assertEqual(ctx["review_perspectives"], f"- {noun} 설명")

    def test_invalid_includes_rejected(self):
        good = {"name": "x_inc", "src": "review-perspectives.md", "sections": ["공통"]}
        bad = [
            "x",
            [{**good, "sections": ["없는 절"]}],
            [{**good, "sections": []}],
            [{**good, "sections": [""]}],
            [{**good, "name": "project_name"}],
            [{**good, "name": "area_review_focus"}],
            [{**good, "name": "area_new"}],
            [{**good, "name": "Bad-Name"}],
            [{**good, "src": "../AGENTS.md"}],
            [{**good, "src": "/abs.md"}],
            [{**good, "src": "missing.md"}],
            [good, good],
            [{**good, "extra": 1}],
            [{**good, "tag": "a b"}],
            [{**good, "tag": "[x]"}],
        ]
        real = harness.read_manifest()
        for includes in bad:
            with self.subTest(includes=includes), \
                    patch.object(harness, "read_manifest", return_value={**real, "includes": includes}), \
                    self.assertRaises(harness.HarnessError):
                harness.build_context(self.CONFIG)
        with patch.object(harness, "read_manifest", return_value={**real, "includes": [good]}):
            self.assertIn("x_inc", harness.build_context(self.CONFIG))

    def test_duplicate_section_rejected(self):
        with self.assertRaises(harness.HarnessError):
            harness.markdown_sections("## a\n- x\n## a\n- y\n")

    def test_generated_files_use_source(self):
        tmp = Path(tempfile.mkdtemp(prefix="harness-include-"))
        try:
            target = tmp / "consumer"
            self.assertEqual(run(["init", str(target), "--platform", "gitlab", "--tracker", "jira",
                                  "--issue-prefix", "DEMO"])[0], 0)
            self.assertEqual(run(["init", str(target), "--area", "backend", "--verify-cmd", "make t",
                                  "--review-focus", "접근성"])[0], 0)
            review = (target / "docs/templates/review.md").read_text(encoding="utf-8")
            area = (target / "backend/AGENTS.md").read_text(encoding="utf-8")
            for text in (review, area):
                self.assertIn(self.first_bullet("공통"), text)
                self.assertIn(self.first_bullet("로컬"), text)
                self.assertNotIn("관점: 계약 정합성 · 정확성 · 보안(입력 검증, Secret 전체 1회)", text)
            self.assertIn("이 영역에서 특히 볼 관점(공통 관점에 덧붙임): 접근성. [사람]", area)
            self.assertGreater(area.index("이 영역에서 특히 볼 관점"), area.index(self.first_bullet("로컬")))
        finally:
            remove_tree(tmp)

    def test_source_change_is_drift(self):
        tmp = Path(tempfile.mkdtemp(prefix="harness-include-"))
        try:
            target = tmp / "consumer"
            run(["init", str(target), "--platform", "github", "--tracker", "github"])
            self.assertEqual(run(["check", str(target)])[0], 0)
            original = harness.include_text
            with patch.object(harness, "include_text", lambda inc: original(inc) + "\n- 새 관점"):
                code, out = run(["check", str(target)])
            self.assertEqual(code, 1)
            self.assertIn("불일치: docs/templates/review.md", out)
        finally:
            remove_tree(tmp)


class TierNameTest(unittest.TestCase):
    """판정 단계의 옛 이름(검증만·기본·게이트)이 템플릿에 남지 않아야 한다."""

    def test_no_old_tier_names(self):
        for path in harness.TEMPLATES_DIR.rglob("*.md"):
            text = path.read_text(encoding="utf-8")
            for old in ("검증만", "게이트"):
                with self.subTest(file=path.name, word=old):
                    self.assertNotIn(old, text)


class SelfApplicationTest(unittest.TestCase):
    """키트 저장소 자신이 자기 템플릿과 일치해야 한다(드리프트 0)."""

    def test_self_check_is_clean(self):
        code, out = run(["check", "--self"])
        self.assertEqual(code, 0, out)

    def test_hooks_in_self_manifest(self):
        config = harness.load_config(ROOT / "harness.json")
        outputs = harness.render_all(config, self_mode=True)
        for dest in (".claude/settings.json", ".claude/hooks/protect-paths.py",
                     ".claude/hooks/stop-verify.py", ".claude/hooks/session-start.py",
                     ".claude/hooks/session-end.py", ".claude/hooks/harness_common.py"):
            self.assertIn(dest, outputs)
        commands = [hook["command"] for groups in json.loads(outputs[".claude/settings.json"])["hooks"].values()
                    for group in groups for hook in group["hooks"]]
        self.assertEqual(len(commands), 4)
        for command in commands:
            self.assertTrue(command.startswith('python "'), command)  # Windows에서 python3은 Store 별칭이다
        # 리뷰 #16 B-F9: 종료 검증을 의미 없는 명령으로 약화하면 잡는다. 기준은 docs/contributing.md "검증"
        self.assertEqual(config["hooks"]["stop_verify"],
                         ["python -m unittest discover tests -q", "python bin/harness.py check --self"])
        verify = (ROOT / "docs" / "contributing.md").read_text(encoding="utf-8")
        self.assertIn("python -m unittest discover tests -v\npython bin/harness.py check --self\n", verify)


class BranchModelTest(unittest.TestCase):
    """integration_branch == default_branch 이면 Git 컨벤션·개발 흐름 문서를 단일 브랜치(태그 릴리스) 내용으로 만든다."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-branch-"))
        self.addCleanup(remove_tree, self.tmp)

    def init(self, name, integration):
        target = self.tmp / name
        code, out = run(["init", str(target), "--platform", "gitlab", "--tracker", "jira", "--issue-prefix", "DEMO",
                         "--integration-branch", integration])
        self.assertEqual(code, 0, out)
        return target

    def docs(self, target):
        return tuple((target / "docs" / name).read_text(encoding="utf-8")
                     for name in ("git-convention.md", "development-workflow.md"))

    def test_two_branch_docs_keep_integration_sections(self):
        git, flow = self.docs(self.init("two", "develop"))
        self.assertIn("### develop에서 main으로 병합", git)
        self.assertIn("| `develop` | 팀 개발 결과가 합쳐지는 통합 브랜치이자 기본 브랜치 |", git)
        self.assertIn("## 7. develop에서 main으로 통합", flow)
        self.assertEqual(harness.branch_mode({"default_branch": "main", "integration_branch": "develop"}),
                         harness.TWO_BRANCH)

    def test_trunk_docs_have_no_integration_branch(self):
        target = self.init("trunk", "main")
        git, flow = self.docs(target)
        for text in (git, flow):
            self.assertNotIn("main에서 main", text)
            self.assertNotIn("`main`과 `main`", text)
            self.assertNotIn("{{", text)
        self.assertEqual(git.count("| `main` |"), 1)  # 브랜치 표에 main 행 하나
        self.assertIn("### 릴리스 태그", git)
        self.assertIn("## 7. 릴리스 태그", flow)
        self.assertIn("git tag -a", flow)
        self.assertEqual(run(["check", str(target)])[0], 0)

    def test_include_when_requires_both_variants(self):
        config = {"project_name": "demo", "platform": "gitlab", "tracker": "jira", "issue_prefix": "DEMO",
                  "default_branch": "main", "integration_branch": "develop"}
        good = {"name": "x_inc", "src": "review-perspectives.md", "sections": ["공통"]}
        real = harness.read_manifest()
        bad = [
            [{**good, "when": "trunk"}],  # 한쪽 변형만
            [{**good, "when": "nope"}, {**good, "when": "two-branch"}],
            [good, {**good, "when": "trunk"}],  # 무조건 변형과 모델별 변형을 섞음
            [{**good, "when": "trunk"}, {**good, "when": "trunk"}, {**good, "when": "two-branch"}],
        ]
        for includes in bad:
            with self.subTest(includes=includes), \
                    patch.object(harness, "read_manifest", return_value={**real, "includes": includes}), \
                    self.assertRaises(harness.HarnessError):
                harness.build_context(config)
        both = [{**good, "when": "two-branch"}, {**good, "sections": ["로컬"], "when": "trunk"}]
        source = (harness.TEMPLATES_DIR / "review-perspectives.md").read_text(encoding="utf-8")
        sections = harness.markdown_sections(source)
        with patch.object(harness, "read_manifest", return_value={**real, "includes": both}):
            two = harness.build_context(config)["x_inc"]
            trunk = harness.build_context({**config, "integration_branch": "main"})["x_inc"]
        self.assertEqual(two.split("{{")[0][:20], sections["공통"].split("{{")[0][:20])
        self.assertEqual(trunk.split("{{")[0][:20], sections["로컬"].split("{{")[0][:20])


class SeedFileTest(unittest.TestCase):
    """seed=true 파일(Git 컨벤션·ADR 목록)은 처음 한 번만 생성하고, 그 뒤는 소비자가 채운 내용을 덮어쓰지도 비교하지도 않는다."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-seed-"))
        self.addCleanup(remove_tree, self.tmp)
        self.target = self.tmp / "consumer"
        code, out = run(["init", str(self.target), "--platform", "github", "--tracker", "github"])
        self.assertEqual(code, 0, out)
        self.adr = self.target / "docs" / "adr" / "README.md"
        self.git = self.target / "docs" / "git-convention.md"

    def force_init(self):
        return run(["init", str(self.target), "--force"])  # 기존 harness.json 이 있으면 루트 설정 인자를 주지 않는다

    def test_seed_path_that_is_not_a_file_aborts(self):
        """PR #63 Codex: 시드 경로가 디렉터리면 시드 파일처럼 유지하지 않고 --force여도 아무것도 쓰지 않고 중단한다."""
        self.git.unlink()
        self.git.mkdir()
        (self.target / "AGENTS.md").write_text("손 수정\n", encoding="utf-8")
        err = io.StringIO()
        with redirect_stderr(err):
            code, _out = self.force_init()
        self.assertEqual(code, 2)
        self.assertIn("docs/git-convention.md", err.getvalue())
        self.assertTrue(self.git.is_dir())
        self.assertEqual((self.target / "AGENTS.md").read_text(encoding="utf-8"), "손 수정\n")  # 쓰기 전에 중단

    def test_seed_files_kept_and_not_compared(self):
        for path, extra in ((self.adr, "| [ADR-0001](0001-x.md) | 결정 | 채택 |"), (self.git, "| `web` | `web/` |")):
            path.write_text(path.read_text(encoding="utf-8") + extra + "\n", encoding="utf-8")
        code, out = run(["check", str(self.target)])
        self.assertEqual(code, 0, out)
        code, out = self.force_init()
        self.assertEqual(code, 0, out)
        self.assertIn("| [ADR-0001](0001-x.md)", self.adr.read_text(encoding="utf-8"))
        self.assertIn("| `web` | `web/` |", self.git.read_text(encoding="utf-8"))
        self.assertIn("docs/adr/README.md (유지: 소비자 소유 시드 파일)", out)
        # 시드가 아닌 파일은 여전히 덮어쓴다
        agents = self.target / "AGENTS.md"
        agents.write_text(agents.read_text(encoding="utf-8") + "\n손으로 고친 줄\n", encoding="utf-8")
        code, out = self.force_init()
        self.assertEqual(code, 0, out)
        self.assertNotIn("손으로 고친 줄", agents.read_text(encoding="utf-8"))

    def test_missing_seed_file_is_reported_and_recreated(self):
        self.adr.unlink()
        code, out = run(["check", str(self.target)])
        self.assertEqual(code, 1)
        self.assertIn("없음:   docs/adr/README.md", out)
        code, out = self.force_init()
        self.assertEqual(code, 0, out)
        self.assertTrue(self.adr.is_file())
        self.assertEqual(run(["check", str(self.target)])[0], 0)

    def test_existing_seed_file_does_not_block_init(self):
        fresh = self.tmp / "fresh"
        (fresh / "docs" / "adr").mkdir(parents=True)
        (fresh / "docs" / "adr" / "README.md").write_text("# 우리 ADR 목록\n", encoding="utf-8")
        code, out = run(["init", str(fresh), "--platform", "github", "--tracker", "github"])
        self.assertEqual(code, 0, out)  # 시드 파일만 있으면 --force 없이도 진행하고 그 파일은 유지한다
        self.assertEqual((fresh / "docs" / "adr" / "README.md").read_text(encoding="utf-8"), "# 우리 ADR 목록\n")


if __name__ == "__main__":
    unittest.main()
