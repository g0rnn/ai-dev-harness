"""core/hooks 스크립트 테스트. 스크립트를 실제 hook처럼 subprocess로 실행하고 stdin에 JSON을 넣는다."""

import importlib.util
import io
import json
import locale
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / "core" / "hooks"
sys.path.insert(0, str(HOOKS))
import harness_common as common  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tempdirs import remove_tree  # noqa: E402

PY = Path(sys.executable).as_posix()


def load_stop_module():
    """stop-verify.py를 프로세스 안에서 새로 불러온다. 상수를 바꿔 끼워도 다른 테스트에 번지지 않게
    공통 모듈도 별도 사본을 쓴다."""
    spec = importlib.util.spec_from_file_location("stop_verify_under_test", HOOKS / "stop-verify.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    common_spec = importlib.util.spec_from_file_location("harness_common_copy", HOOKS / "harness_common.py")
    module.common = importlib.util.module_from_spec(common_spec)
    common_spec.loader.exec_module(module.common)
    return module


def call_main(module, payload, project, ceiling):
    stdin = io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode("utf-8")), encoding="utf-8")
    out, err = io.StringIO(), io.StringIO()
    env = {"CLAUDE_PROJECT_DIR": str(project), "GIT_CEILING_DIRECTORIES": str(ceiling)}
    with mock.patch.dict(os.environ, env), mock.patch.object(sys, "stdin", stdin), \
            mock.patch.object(module.common, "setup_streams", lambda: None), \
            redirect_stdout(out), redirect_stderr(err):
        code = module.main()
    return code, out.getvalue(), err.getvalue()


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def git_path(project, name):
    out = subprocess.run(["git", "rev-parse", "--git-path", f"harness/{name}"], cwd=project,
                         check=True, capture_output=True, text=True).stdout.strip()
    path = Path(out)
    return path if path.is_absolute() else project / path


class HookCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-hooks-")).resolve()
        self.project = self.tmp / "project"
        self.project.mkdir()
        self.outside = self.tmp / "outside"
        self.outside.mkdir()

    def tearDown(self):
        remove_tree(self.tmp)

    def write_config(self, config):
        (self.project / "harness.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    def init_git(self):
        git(self.project, "init", "-q")
        git(self.project, "config", "user.email", "t@example.com")
        git(self.project, "config", "user.name", "t")
        git(self.project, "config", "core.autocrlf", "false")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-q", "--allow-empty", "-m", "init")

    def hook(self, name, payload, raw=None, project=None, env=None):
        project = project or self.project
        env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project),
               "GIT_CEILING_DIRECTORIES": str(self.tmp), **(env or {})}
        data = raw if raw is not None else json.dumps(payload)
        return subprocess.run([sys.executable, str(HOOKS / name)], input=data.encode("utf-8"),
                              capture_output=True, cwd=project, env=env, timeout=60)


class ProtectPathsCase(HookCase):
    def edit(self, path, tool="Edit", key="file_path"):
        result = self.hook("protect-paths.py", {"tool_name": tool, "tool_input": {key: str(path)},
                                                "cwd": str(self.project), "session_id": "s1"})
        out = result.stdout.decode("utf-8").strip()
        decision = json.loads(out)["hookSpecificOutput"] if out else None
        return result, decision


class ProtectPathsTest(ProtectPathsCase):
    def test_block_rule_denies(self):
        result, decision = self.edit(self.project / "package-lock.json", tool="Write")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("lock 파일", decision["permissionDecisionReason"])
        self.assertIn("package-lock.json", decision["permissionDecisionReason"])

    def test_ask_rule_asks(self):
        _, decision = self.edit(self.project / ".github" / "workflows" / "ci.yml")
        self.assertEqual(decision["permissionDecision"], "ask")

    def test_unprotected_path_silent(self):
        result, decision = self.edit(self.project / "src" / "app.py")
        self.assertEqual(result.returncode, 0)
        self.assertIsNone(decision)
        self.assertEqual(result.stdout, b"")

    def test_tool_input_keys(self):
        _, decision = self.edit(self.project / "yarn.lock", tool="MultiEdit")
        self.assertEqual(decision["permissionDecision"], "deny")
        _, decision = self.edit(self.project / "migrations" / "a.ipynb", tool="NotebookEdit", key="notebook_path")
        self.assertEqual(decision["permissionDecision"], "ask")
        _, decision = self.edit(self.project / "yarn.lock", tool="Read")
        self.assertIsNone(decision)

    def test_path_normalization(self):
        variants = ["src/../package-lock.json", "src\\..\\package-lock.json",
                    str(self.project / "package-lock.json"), "./frontend/package-lock.json"]
        for raw in variants:
            with self.subTest(raw=raw):
                _, decision = self.edit(raw)
                self.assertEqual(decision["permissionDecision"], "deny")
        link = self.project / "innocent.txt"
        (self.project / "package-lock.json").write_text("{}", encoding="utf-8")
        try:
            link.symlink_to(self.project / "package-lock.json")
        except (OSError, NotImplementedError):
            self.skipTest("이 환경에서는 심볼릭 링크를 만들 수 없다")
        _, decision = self.edit(link)
        self.assertEqual(decision["permissionDecision"], "deny")

    def test_outside_project_ignored(self):
        _, decision = self.edit(self.outside / "package-lock.json")
        self.assertIsNone(decision)
        _, decision = self.edit("../outside/package-lock.json")
        self.assertIsNone(decision)

    def test_self_protection_not_overridable(self):
        self.write_config({"hooks": {"protected_paths": []}})
        for rel in (".claude/settings.json", ".claude/settings.local.json", ".claude/hooks/protect-paths.py"):
            with self.subTest(rel=rel):
                _, decision = self.edit(self.project / rel)
                self.assertEqual(decision["permissionDecision"], "deny")

    def test_override_replaces_defaults(self):
        self.write_config({"hooks": {"protected_paths": [{"pattern": "secret/**", "mode": "ask"}]}})
        _, decision = self.edit(self.project / "secret" / "a.txt")
        self.assertEqual(decision["permissionDecision"], "ask")
        _, decision = self.edit(self.project / "package-lock.json")
        self.assertIsNone(decision)

    def test_strongest_rule_wins(self):
        self.write_config({"hooks": {"protected_paths": [
            {"pattern": "cfg/**", "mode": "ask"}, {"pattern": "cfg/prod.yml", "mode": "block"}]}})
        _, decision = self.edit(self.project / "cfg" / "prod.yml")
        self.assertEqual(decision["permissionDecision"], "deny")

    def test_area_docs_are_asked(self):
        self.write_config({"areas": [{"dir": "backend", "verify": ["x"], "docs": ["docs/api.md"]}]})
        _, decision = self.edit(self.project / "docs" / "api.md")
        self.assertEqual(decision["permissionDecision"], "ask")
        self.assertIn("계약 문서", decision["permissionDecisionReason"])
        _, decision = self.edit(self.project / "backend" / "docs" / "api.md")
        self.assertIsNone(decision)

    def test_fail_open_on_bad_config(self):
        (self.project / "harness.json").write_text("{not json", encoding="utf-8")
        result, decision = self.edit(self.project / "package-lock.json")
        self.assertEqual(result.returncode, 1)
        self.assertIsNone(decision)
        self.assertIn("꺼진 상태", result.stderr.decode("utf-8"))
        # 자기 보호는 설정 오류와 무관하게 유지된다
        result, decision = self.edit(self.project / ".claude" / "settings.json")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(decision["permissionDecision"], "deny")

    def test_fail_open_on_bad_stdin(self):
        result = self.hook("protect-paths.py", None, raw="not json")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(b"deny", result.stdout)
        self.assertIn("꺼진 상태", result.stderr.decode("utf-8"))

    def test_invalid_hooks_config_fails_open(self):
        self.write_config({"hooks": {"protected_paths": [{"pattern": "x", "mode": "warn"}]}})
        result, decision = self.edit(self.project / "x")
        self.assertEqual(result.returncode, 1)
        self.assertIsNone(decision)

    def test_null_hooks_config_fails_open(self):
        self.write_config({"hooks": None})
        result, decision = self.edit(self.project / "x")
        self.assertEqual(result.returncode, 1)
        self.assertIsNone(decision)

    def test_event_logged(self):
        self.init_git()
        self.edit(self.project / "package-lock.json")
        lines = git_path(self.project, "events.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        event = json.loads(lines[0])
        self.assertEqual((event["hook"], event["decision"], event["path"], event["session"]),
                         ("protect-paths", "block", "package-lock.json", "s1"))
        branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=self.project, check=True,
                                capture_output=True, text=True).stdout.strip()
        self.assertEqual(event["branch"], branch)

    def test_event_branch_and_summary(self):
        """#75: 기록에 브랜치가 남고 `harness events summary`가 현재 브랜치 것만 MR 본문 절로 낸다."""
        self.init_git()
        git(self.project, "switch", "-q", "-c", "feat/1")
        self.edit(self.project / "package-lock.json")  # block
        self.edit(self.project / ".gitlab-ci.yml")  # ask
        git(self.project, "switch", "-q", "-c", "feat/2")
        self.edit(self.project / "yarn.lock")  # 다른 브랜치
        log = git_path(self.project, "events.jsonl")
        with log.open("a", encoding="utf-8") as handle:  # branch 필드 이전의 옛 기록과 깨진 줄
            handle.write(json.dumps({"ts": "t", "hook": "protect-paths", "decision": "block"}) + "\n{x\n")
        harness = [sys.executable, str(ROOT / "bin" / "harness.py"), "events", "summary", str(self.project)]
        result = subprocess.run([*harness, "--branch", "feat/1"], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.decode("utf-8").splitlines(), [
            "## hooks 기록", "", "- 차단(block): 1", "- 확인(ask): 1", "- 종료 검증 실패: 0",
            "- 규칙: `.gitlab-ci.yml` ask 1; `package-lock.json` block 1"])
        self.assertIn("옛 기록 1줄", result.stderr.decode("utf-8"))
        self.assertIn("읽을 수 없는 줄 1줄", result.stderr.decode("utf-8"))
        current = subprocess.run(harness, capture_output=True)  # 기본은 현재 브랜치(feat/2)
        self.assertIn("- 차단(block): 1", current.stdout.decode("utf-8").splitlines())
        self.assertIn("`yarn.lock` block 1", current.stdout.decode("utf-8"))
        git(self.project, "switch", "-q", "--detach")
        self.assertIsNone(common.current_branch(self.project))
        detached = subprocess.run(harness, capture_output=True)
        self.assertEqual(detached.returncode, 2)
        self.assertIn("--branch", detached.stderr.decode("utf-8"))

    def test_event_branch_before_first_commit(self):
        """첫 커밋 전(unborn HEAD)에도 기록에 초기 브랜치가 남는다(빈 저장소 부트스트랩)."""
        git(self.project, "init", "-q", "-b", "main")
        self.assertEqual(common.current_branch(self.project), "main")
        self.edit(self.project / "package-lock.json")
        event = json.loads(git_path(self.project, "events.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(event["branch"], "main")

    def test_log_failure_does_not_change_decision(self):
        self.init_git()
        blocker = git_path(self.project, "events.jsonl").parent
        blocker.write_text("not a directory", encoding="utf-8")  # 로그 디렉터리를 만들 수 없게 한다
        result, decision = self.edit(self.project / "package-lock.json")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(decision["permissionDecision"], "deny")


class ProtectPathsReviewTest(ProtectPathsCase):
    """분리 리뷰(R1·R2·R6·R7·R11·R14) 회귀 테스트."""

    @unittest.skipUnless(os.name == "nt", "Windows 경로 형식")
    def test_extended_and_unc_paths(self):
        _, decision = self.edit("\\\\?\\" + str(self.project / ".claude" / "settings.json"))
        self.assertEqual(decision["permissionDecision"], "deny")
        drive, rest = str(self.project).split(":", 1)
        _, decision = self.edit(f"\\\\localhost\\{drive}$" + rest + "\\.claude\\settings.json")
        self.assertEqual(decision["permissionDecision"], "ask")

    def test_case_variants_blocked(self):
        for rel in (".Claude/Settings.json", ".CLAUDE/hooks/x.py", "Package-Lock.json"):
            with self.subTest(rel=rel):
                _, decision = self.edit(self.project / rel)
                self.assertEqual(decision["permissionDecision"], "deny")

    def test_bad_area_types_keep_self_protection(self):
        self.write_config({"areas": [{"dir": "a", "verify": ["x"], "docs": 5}]})
        result, decision = self.edit(self.project / ".claude" / "settings.json")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("주의", decision["permissionDecisionReason"])  # 설정 오류 알림이 사유에 붙는다
        result, decision = self.edit(self.project / "package-lock.json")
        self.assertEqual(result.returncode, 1)
        self.assertIsNone(decision)

    def test_harness_json_always_asked(self):
        self.write_config({"hooks": {"protected_paths": []}})
        _, decision = self.edit(self.project / "harness.json")
        self.assertEqual(decision["permissionDecision"], "ask")

    def test_dot_slash_doc_path(self):
        self.write_config({"areas": [{"dir": "a", "verify": ["x"], "docs": ["./docs/api.md"]}]})
        _, decision = self.edit(self.project / "docs" / "api.md")
        self.assertEqual(decision["permissionDecision"], "ask")

    def test_user_settings_asked(self):
        home = self.tmp / "home"
        (home / ".claude").mkdir(parents=True)
        env = {"HOME": str(home), "USERPROFILE": str(home)}
        for name in ("settings.json", "settings.local.json"):
            with self.subTest(name=name):
                result = self.hook("protect-paths.py", {"tool_name": "Edit", "tool_input": {
                    "file_path": str(home / ".claude" / name)}}, env=env)
                decision = json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"]
                self.assertEqual(decision, "ask")
        result = self.hook("protect-paths.py", {"tool_name": "Edit", "tool_input": {
            "file_path": str(home / ".claude" / "CLAUDE.md")}}, env=env)
        self.assertEqual(result.stdout, b"")


class SymlinkTest(ProtectPathsCase):
    """PR #9 Codex 리뷰(#10 C1): 링크를 따라가기 전의 논리 경로와 실제 경로를 모두 판정한다."""

    def link(self, name, target):
        path = self.project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("이 환경에서는 심볼릭 링크를 만들 수 없다")
        return path

    def dir_link(self, path, target):
        """디렉터리 링크. Windows는 권한 없이 만들 수 있는 junction을 쓴다."""
        path.parent.mkdir(parents=True, exist_ok=True)
        target.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(target), str(path))
        else:
            path.symlink_to(target, target_is_directory=True)
        return path

    def test_protected_link_to_outside(self):
        target = self.outside / "x.json"
        target.write_text("{}", encoding="utf-8")
        link = self.link(".claude/settings.json", target)
        _, decision = self.edit(link)
        self.assertEqual(decision["permissionDecision"], "deny")

    def test_protected_link_to_unprotected(self):
        (self.project / "src").mkdir()
        (self.project / "src" / "data.json").write_text("{}", encoding="utf-8")
        link = self.link("package-lock.json", self.project / "src" / "data.json")
        _, decision = self.edit(link)
        self.assertEqual(decision["permissionDecision"], "deny")

    def test_file_link_into_protected(self):
        (self.project / "package-lock.json").write_text("{}", encoding="utf-8")
        link = self.link("innocent.txt", self.project / "package-lock.json")
        _, decision = self.edit(link)
        self.assertEqual(decision["permissionDecision"], "deny")

    def test_dir_link_into_protected(self):
        alias = self.dir_link(self.project / "alias", self.project / ".claude")
        _, decision = self.edit(alias / "settings.json")
        self.assertEqual(decision["permissionDecision"], "deny")
        chain = self.dir_link(self.project / "chain", alias)  # 링크를 거친 링크
        _, decision = self.edit(chain / "hooks" / "x.py")
        self.assertEqual(decision["permissionDecision"], "deny")

    def test_protected_dir_is_link(self):
        """#10 리뷰 R1: `.claude/hooks`가 링크면 그 대상 경로로 직접 쓰거나 다른 링크를 거쳐도 막는다."""
        real = self.dir_link(self.project / ".claude" / "hooks", self.outside / "hooksreal")
        _, decision = self.edit(real / "protect-paths.py")
        self.assertEqual(decision["permissionDecision"], "deny")
        _, decision = self.edit(self.outside / "hooksreal" / "protect-paths.py")
        self.assertEqual(decision["permissionDecision"], "deny")
        _, decision = self.edit(self.outside / "other.txt")
        self.assertIsNone(decision)  # 보호 대상이 아닌 곳은 그대로 조용하다

    def test_claude_dir_is_link(self):
        self.dir_link(self.project / ".claude", self.project / "shared")
        _, decision = self.edit(self.project / "shared" / "settings.json")
        self.assertEqual(decision["permissionDecision"], "deny")
        _, decision = self.edit(self.project / "shared" / "notes.md")
        self.assertIsNone(decision)

    def test_hardlink_to_settings(self):
        settings = self.project / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text("{}", encoding="utf-8")
        alias = self.project / "notes.txt"
        try:
            os.link(settings, alias)
        except (OSError, NotImplementedError):
            self.skipTest("이 환경에서는 하드링크를 만들 수 없다")
        _, decision = self.edit(alias)
        self.assertEqual(decision["permissionDecision"], "deny")

    def test_link_to_user_settings(self):
        home = self.tmp / "home"
        self.dir_link(self.project / "userlink", home / ".claude")
        env = {"HOME": str(home), "USERPROFILE": str(home)}
        result = self.hook("protect-paths.py", {"tool_name": "Edit", "tool_input": {
            "file_path": str(self.project / "userlink" / "settings.json")}}, env=env)
        self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_unprotected_link_silent(self):
        (self.project / "b.txt").write_text("b", encoding="utf-8")
        link = self.link("a.txt", self.project / "b.txt")
        result, decision = self.edit(link)
        self.assertEqual(result.returncode, 0)
        self.assertIsNone(decision)


class ProcessKwargsTest(unittest.TestCase):
    """OS별 하위 프로세스 인자(#145). Windows 플래그 값은 다른 OS에서도 os_name으로 확인한다."""

    def test_windows_hides_console_without_detaching(self):
        self.assertEqual(common.hidden_kwargs("nt"), {"creationflags": common.CREATE_NO_WINDOW})
        flags = common.new_group_kwargs("nt")["creationflags"]
        self.assertTrue(flags & common.CREATE_NO_WINDOW)
        self.assertTrue(flags & common.CREATE_NEW_PROCESS_GROUP)
        self.assertFalse(flags & 0x00000008)  # DETACHED_PROCESS: 함께 주면 CREATE_NO_WINDOW가 무시된다

    def test_posix_uses_new_session_and_no_flags(self):
        self.assertEqual(common.hidden_kwargs("posix"), {})
        self.assertEqual(common.new_group_kwargs("posix"), {"start_new_session": True})

    @unittest.skipUnless(os.name == "nt", "Windows 상수 대조")
    def test_constants_match_subprocess(self):
        self.assertEqual(common.CREATE_NO_WINDOW, subprocess.CREATE_NO_WINDOW)
        self.assertEqual(common.CREATE_NEW_PROCESS_GROUP, subprocess.CREATE_NEW_PROCESS_GROUP)

    def test_spawn_background_closes_streams_and_does_not_wait(self):
        with mock.patch.object(common.subprocess, "Popen") as spawn:
            common.spawn_background(["prog", "arg"], Path("work"))
        args, kwargs = spawn.call_args
        self.assertEqual(args, (["prog", "arg"],))
        self.assertEqual(kwargs["cwd"], Path("work"))
        self.assertTrue(kwargs["close_fds"])
        self.assertNotIn("shell", kwargs)
        self.assertNotIn("env", kwargs)
        for stream in ("stdin", "stdout", "stderr"):
            self.assertEqual(kwargs[stream], subprocess.DEVNULL)
        for key, value in common.new_group_kwargs().items():
            self.assertEqual(kwargs[key], value)
        spawn.return_value.wait.assert_not_called()

    def test_git_hides_console(self):
        with mock.patch.object(common.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, b"out", b"")
            self.assertEqual(common.git_result(Path("."), "status"), ("out", ""))
        for key, value in common.hidden_kwargs().items():
            self.assertEqual(run.call_args.kwargs[key], value)

    def test_scripts_do_not_set_creation_flags_directly(self):
        """플래그를 스크립트마다 따로 쓰다 일부가 틀려 창이 떴다(#145). 실행 인자는 harness_common 함수로만 만든다."""
        offenders = [path.name for path in sorted(HOOKS.glob("*.py")) + sorted((ROOT / "core" / "metrics").glob("*.py"))
                     if path.name != "harness_common.py"
                     and any(word in path.read_text(encoding="utf-8")
                             for word in ("creationflags", "DETACHED_PROCESS", "start_new_session"))]
        self.assertEqual(offenders, [])


class TelemetrySettingsTest(unittest.TestCase):
    """harness.json telemetry 블록과 사용자 자격 파일(#145)."""

    ENDPOINT = "https://collector.example"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(remove_tree, self.tmp)

    def test_settings_fill_defaults(self):
        self.assertIsNone(common.telemetry_settings({}))
        self.assertEqual(common.telemetry_settings({"telemetry": {"endpoint": f" {self.ENDPOINT} "}}),
                         {"endpoint": self.ENDPOINT, "tools": ["claude-code", "codex"], "harness_events": True})
        self.assertEqual(common.telemetry_settings({"telemetry": {"endpoint": "http://127.0.0.1:4318", "tools": ["codex"],
                                                                  "harness_events": False}}),
                         {"endpoint": "http://127.0.0.1:4318", "tools": ["codex"], "harness_events": False})

    def test_invalid_blocks_rejected(self):
        bad = [None, [], {}, {"endpoint": ""}, {"endpoint": self.ENDPOINT, "typo": 1},
               {"endpoint": "http://collector.example"},  # 원격 http: 헤더가 평문으로 나간다
               {"endpoint": "https://user:pw@collector.example"}, {"endpoint": f"{self.ENDPOINT}?token=x"},
               {"endpoint": "ftp://collector.example"}, {"endpoint": "https://collector.example:bad"},
               {"endpoint": self.ENDPOINT, "tools": ["cursor"]}, {"endpoint": self.ENDPOINT, "tools": ["codex", "codex"]},
               {"endpoint": self.ENDPOINT, "tools": "codex"}, {"endpoint": self.ENDPOINT, "harness_events": "yes"}]
        for block in bad:
            with self.subTest(block=block), self.assertRaises(common.ConfigError):
                common.telemetry_settings({"telemetry": block})

    def test_load_config_ignores_broken_telemetry(self):
        """블록의 오타가 보호 경로 검사·종료 검증 설정 읽기를 깨지 않는다."""
        (self.tmp / "harness.json").write_text(json.dumps({"telemetry": {"endpoint": 1}}), encoding="utf-8")
        config = common.load_config(self.tmp)
        with self.assertRaises(common.ConfigError):
            common.telemetry_settings(config)

    def test_endpoint_key_normalizes(self):
        for url in ("HTTPS://Collector.Example/", "https://collector.example", " https://collector.example// "):
            self.assertEqual(common.endpoint_key(url), "https://collector.example")
        self.assertEqual(common.endpoint_key("https://c.example:8443/otlp/"), "https://c.example:8443/otlp")

    def test_config_dir_per_os(self):
        home = Path("/home/me")
        self.assertEqual(common.config_dir({"APPDATA": r"C:\Users\me\AppData\Roaming"}, "nt", home),
                         Path(r"C:\Users\me\AppData\Roaming") / "harness")
        self.assertEqual(common.config_dir({}, "nt", home), home / "AppData" / "Roaming" / "harness")
        xdg = self.tmp / "xdg"
        self.assertEqual(common.config_dir({"XDG_CONFIG_HOME": str(xdg)}, "posix", home), xdg / "harness")
        # XDG 규격: 상대 경로는 무시한다
        self.assertEqual(common.config_dir({"XDG_CONFIG_HOME": "rel"}, "posix", home), home / ".config" / "harness")
        self.assertEqual(common.config_dir({}, "posix", home), home / ".config" / "harness")

    def write_credentials(self, data):
        path = self.tmp / "credentials.json"
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return path

    def test_headers_looked_up_by_endpoint(self):
        path = self.write_credentials({"endpoints": {"https://Collector.Example/": {"headers": {"Authorization": "Bearer t"}}}})
        self.assertEqual(common.telemetry_headers(self.ENDPOINT, path), {"Authorization": "Bearer t"})
        self.assertEqual(common.telemetry_headers("https://other.example", path), {})
        self.assertEqual(common.telemetry_headers(self.ENDPOINT, self.tmp / "missing.json"), {})

    def test_malformed_credentials_rejected_without_values(self):
        secret = "secret-sentinel"
        bad = [f'{{"endpoints": {{"{self.ENDPOINT}": "{secret}"', {"endpoints": []}, [],
               {"endpoints": {self.ENDPOINT: {"headers": {}}}},
               {"endpoints": {self.ENDPOINT: {"headers": {"Authorization": f"Bearer {secret}\r\nX-Injected: 1"}}}},
               {"endpoints": {self.ENDPOINT: {"headers": {"Bad Name": secret}}}},
               {"endpoints": {"http://remote.example": {"headers": {"Authorization": secret}}}}]
        for data in bad:
            with self.subTest(data=data):
                path = self.write_credentials(data)
                with self.assertRaises(common.ConfigError) as caught:
                    common.load_credentials(path)
                self.assertNotIn(secret, str(caught.exception))

    def test_headers_helper_matches_common_rules(self):
        spec = importlib.util.spec_from_file_location("otel_headers_under_test", ROOT / "core" / "metrics" / "otel_headers.py")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        home = Path("/home/me")
        for env, os_name in (({"APPDATA": r"C:\A"}, "nt"), ({}, "nt"), ({"XDG_CONFIG_HOME": str(self.tmp)}, "posix"),
                             ({"XDG_CONFIG_HOME": "rel"}, "posix"), ({}, "posix")):
            self.assertEqual(helper.config_dir(env, os_name, home), common.config_dir(env, os_name, home))
        for url in ("HTTPS://Collector.Example/", "https://c.example:8443/otlp/", "http://127.0.0.1:4318"):
            self.assertEqual(helper.endpoint_key(url), common.endpoint_key(url))
        self.assertEqual(helper.CREDENTIALS_NAME, common.CREDENTIALS_NAME)

    def run_helper(self, *args):
        env = {**os.environ, "APPDATA": str(self.tmp), "XDG_CONFIG_HOME": str(self.tmp)}
        return subprocess.run([sys.executable, str(ROOT / "core" / "metrics" / "otel_headers.py"), *args],
                              capture_output=True, text=True, encoding="utf-8", env=env)

    def test_headers_helper_prints_json_or_fails_quietly(self):
        directory = self.tmp / "harness"
        result = self.run_helper(self.ENDPOINT)
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn("자격 파일이 없다", result.stderr)
        directory.mkdir()
        secret = "Bearer secret-sentinel"
        (directory / "credentials.json").write_text(
            json.dumps({"endpoints": {self.ENDPOINT: {"headers": {"Authorization": secret}}}}), encoding="utf-8")
        result = self.run_helper(self.ENDPOINT + "/")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {"Authorization": secret})
        result = self.run_helper("https://other.example")
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        (directory / "credentials.json").write_text('{"endpoints": {"' + secret, encoding="utf-8")
        result = self.run_helper(self.ENDPOINT)
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertNotIn("secret-sentinel", result.stderr)
        self.assertEqual(self.run_helper().returncode, 2)


class GlobTest(unittest.TestCase):
    def match(self, pattern, path):
        return bool(common.compile_glob(pattern).fullmatch(path))

    def test_double_star_semantics(self):
        self.assertTrue(self.match("**/migrations/**", "migrations/1.sql"))
        self.assertTrue(self.match("**/migrations/**", "a/b/migrations/1.sql"))
        self.assertFalse(self.match("**/migrations/**", "migrationsx/1.sql"))
        self.assertFalse(self.match("**/migrations/**", "a/migrations"))

    def test_basename_and_anchor(self):
        self.assertTrue(self.match("*.lock", "a/b.lock"))  # 슬래시 없는 패턴은 모든 깊이(gitignore 방식)
        self.assertTrue(self.match("*.lock", "b.lock"))
        self.assertFalse(self.match("docs/*.md", "docs/a/b.md"))  # `*`는 `/`를 넘지 않는다
        self.assertTrue(self.match("docs/*.md", "docs/a.md"))
        self.assertFalse(self.match("docs/*.md", "x/docs/a.md"))  # 중간에 `/`가 있으면 루트 기준
        self.assertTrue(self.match("/harness.json", "harness.json"))
        self.assertFalse(self.match("/harness.json", "sub/harness.json"))
        self.assertTrue(self.match("build/", "build/x/y"))
        self.assertTrue(self.match("a?.txt", "ab.txt"))
        self.assertFalse(self.match("a?.txt", "a/.txt"))
        self.assertFalse(self.match("a.b", "axb"))  # 정규식 특수 문자는 그대로

    def test_gitignore_directory_rules(self):
        self.assertTrue(self.match("./docs/api.md", "docs/api.md"))
        self.assertTrue(self.match("migrations/", "a/migrations/1.sql"))  # 끝의 `/`만 있으면 모든 깊이
        self.assertFalse(self.match("migrations/", "migrations"))
        self.assertTrue(self.match("vendor", "vendor/x/y.js"))  # 디렉터리에 맞으면 그 아래도
        self.assertTrue(self.match("**/db/migration/", "backend/src/db/migration/V1.sql"))
        self.assertTrue(self.match("/.claude/hooks/", ".claude/hooks/a.py"))
        self.assertTrue(self.match("PACKAGE-lock.json", "package-lock.json"))


class StopVerifyTest(HookCase):
    def marker_cmd(self, name, code=0):
        marker = (self.outside / name).as_posix()
        return f"\"{PY}\" -c \"open('{marker}','a').write('x'); raise SystemExit({code})\""

    def runs(self, name):
        path = self.outside / name
        return len(path.read_text()) if path.exists() else 0

    def setup_repo(self, config):
        self.write_config(config)
        self.init_git()

    def stop(self, active=False):
        return self.hook("stop-verify.py", {"hook_event_name": "Stop", "stop_hook_active": active,
                                            "session_id": "s1", "cwd": str(self.project)})

    def test_no_changes_runs_nothing(self):
        self.setup_repo({"hooks": {"stop_verify": [self.marker_cmd("root", 1)]}})
        result = self.stop()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.runs("root"), 0)

    def test_runs_only_changed_areas(self):
        self.setup_repo({
            "areas": [{"dir": "a", "verify": [self.marker_cmd("a")]},
                      {"dir": "b", "verify": [self.marker_cmd("b")]}],
            "hooks": {"stop_verify": [self.marker_cmd("root")]},
        })
        (self.project / "a").mkdir()
        (self.project / "a" / "x.txt").write_text("1", encoding="utf-8")
        self.assertEqual(self.stop().returncode, 0)
        self.assertEqual((self.runs("a"), self.runs("b"), self.runs("root")), (1, 0, 0))
        (self.project / "top.txt").write_text("1", encoding="utf-8")
        self.assertEqual(self.stop().returncode, 0)
        self.assertEqual(self.runs("root"), 1)

    def test_nested_area_owns_its_files(self):
        self.setup_repo({"areas": [{"dir": "a", "verify": [self.marker_cmd("a")]},
                                   {"dir": "a/inner", "verify": [self.marker_cmd("inner")]}]})
        (self.project / "a" / "inner").mkdir(parents=True)
        (self.project / "a" / "inner" / "x.txt").write_text("1", encoding="utf-8")
        self.stop()
        self.assertEqual((self.runs("a"), self.runs("inner")), (0, 1))

    def test_bash_syntax_command_runs_on_every_os(self):
        """#115: `cd sub && ./script.sh`처럼 bash 문법 명령도 실행된다(Windows에서는 cmd가 아니라 Git Bash로)"""
        marker = (self.outside / "sh").as_posix()
        self.setup_repo({"hooks": {"stop_verify": ["cd sub && ./check.sh"]}})
        script = self.project / "sub" / "check.sh"
        script.parent.mkdir()
        script.write_bytes(f"#!/bin/sh\necho x >> '{marker}'\n".encode("utf-8"))
        script.chmod(0o755)
        (self.project / "x.txt").write_text("1", encoding="utf-8")
        result = self.stop()
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertEqual(self.runs("sh"), 2)  # "x\n" 한 줄

    def test_failure_blocks_once(self):
        self.setup_repo({"hooks": {"stop_verify": [f"\"{PY}\" -c \"print('boom-output'); raise SystemExit(3)\""]}})
        (self.project / "x.txt").write_text("1", encoding="utf-8")
        result = self.stop()
        self.assertEqual(result.returncode, 2)
        err = result.stderr.decode("utf-8")
        self.assertIn("종료 코드 3", err)
        self.assertIn("boom-output", err)
        event = json.loads(git_path(self.project, "events.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual((event["hook"], event["decision"]), ("stop-verify", "fail"))

    def test_no_reblock_when_active(self):
        self.setup_repo({"hooks": {"stop_verify": [self.marker_cmd("root", 1)]}})
        (self.project / "x.txt").write_text("1", encoding="utf-8")
        result = self.stop(active=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("사람이 확인", json.loads(result.stdout.decode("utf-8"))["systemMessage"])

    def test_success_cached(self):
        self.setup_repo({"hooks": {"stop_verify": [self.marker_cmd("root")]}})
        target = self.project / "x.txt"
        target.write_text("1", encoding="utf-8")
        self.stop()
        self.stop()
        self.assertEqual(self.runs("root"), 1)
        target.write_text("2", encoding="utf-8")
        self.stop()
        self.assertEqual(self.runs("root"), 2)

    def test_same_failing_state_not_reblocked(self):
        self.setup_repo({"hooks": {"stop_verify": [self.marker_cmd("root", 1)]}})
        target = self.project / "x.txt"
        target.write_text("1", encoding="utf-8")
        self.assertEqual(self.stop().returncode, 2)
        second = self.stop()  # 세션과 무관한 기존 변경이어도 매 턴 보류하지 않는다
        self.assertEqual(second.returncode, 0)
        self.assertIn("같은 상태", json.loads(second.stdout.decode("utf-8"))["systemMessage"])
        third = self.stop()
        self.assertEqual((third.returncode, third.stdout), (0, b""))  # 알림은 한 번만
        self.assertEqual(self.runs("root"), 1)
        target.write_text("2", encoding="utf-8")  # 모델이 무언가를 바꾸면 다시 검증하고 보류한다
        self.assertEqual(self.stop().returncode, 2)
        self.assertEqual(self.runs("root"), 2)

    def test_project_in_subdirectory(self):
        app = self.project / "app"
        (app / "a").mkdir(parents=True)
        check = f"\"{PY}\" -c \"import sys; sys.exit('bad' in open('a/x.txt').read())\""
        (app / "harness.json").write_text(json.dumps({"areas": [{"dir": "a", "verify": [check]}]}),
                                          encoding="utf-8")
        (app / "a" / "x.txt").write_text("good", encoding="utf-8")
        self.init_git()
        (self.project / "outside.txt").write_text("1", encoding="utf-8")  # 프로젝트 밖 변경은 무시한다

        def stop():
            return self.hook("stop-verify.py", {"stop_hook_active": False}, project=app)

        self.assertEqual(stop().returncode, 0)
        (app / "a" / "x.txt").write_text("good2", encoding="utf-8")
        self.assertEqual(stop().returncode, 0)
        (app / "a" / "x.txt").write_text("bad", encoding="utf-8")  # 통과 캐시로 실패를 덮지 않는다
        self.assertEqual(stop().returncode, 2)

    def test_nested_repo_changes_rehashed(self):
        self.setup_repo({"hooks": {"stop_verify": [self.marker_cmd("root")]}})
        sub = self.project / "sub"
        sub.mkdir()
        git(sub, "init", "-q")
        (sub / "f.txt").write_text("1", encoding="utf-8")
        self.stop()
        self.assertEqual(self.runs("root"), 1)
        (sub / "f.txt").write_text("2", encoding="utf-8")
        self.stop()
        self.assertEqual(self.runs("root"), 2)

    def test_nested_repo_ignored_files_not_hashed(self):
        self.setup_repo({"hooks": {"stop_verify": [self.marker_cmd("root")]}})
        sub = self.project / "sub"
        (sub / "junk").mkdir(parents=True)
        git(sub, "init", "-q")
        (sub / ".gitignore").write_text("junk/\n", encoding="utf-8")
        (sub / "junk" / "big.bin").write_bytes(b"0" * 1024)
        self.stop()
        self.assertEqual(self.runs("root"), 1)
        (sub / "junk" / "big.bin").write_bytes(b"1" * 1024)  # ignore된 파일은 읽지도 해시하지도 않는다
        self.stop()
        self.assertEqual(self.runs("root"), 1)

    def test_nested_repo_inside_nested_repo(self):
        """#10 리뷰 R2: 중첩 저장소 안의 중첩 저장소 변경도 해시에 들어간다."""
        self.setup_repo({"hooks": {"stop_verify": [self.marker_cmd("root")]}})
        inner = self.project / "sub" / "inner"
        inner.mkdir(parents=True)
        git(self.project / "sub", "init", "-q")
        git(inner, "init", "-q")
        (inner / "f.txt").write_text("1", encoding="utf-8")
        self.stop()
        (inner / "f.txt").write_text("2", encoding="utf-8")
        self.stop()
        self.assertEqual(self.runs("root"), 2)

    def test_nesting_limit_not_cached(self):
        """PR #11 Codex 리뷰: 중첩 한도를 넘으면 안쪽 변경을 반영할 수 없으므로 캐시하지 않는다."""
        self.setup_repo({"hooks": {"stop_verify": [self.marker_cmd("root")]}})
        repos = [self.project / "l1"]
        for name in ("l2", "l3", "l4"):
            repos.append(repos[-1] / name)
        repos[-1].mkdir(parents=True)
        for repo in repos:
            git(repo, "init", "-q")
        deep = repos[-1]
        (deep / "f.txt").write_text("1", encoding="utf-8")
        module = load_stop_module()
        module.MAX_NEST_DEPTH = 2  # l1(0) → l2(1) → l3(2) 안의 l4는 한도를 넘는다
        call_main(module, {"stop_hook_active": False}, self.project, self.tmp)
        self.assertFalse(git_path(self.project, "stop-verify.json").exists())
        (deep / "f.txt").write_text("2", encoding="utf-8")
        call_main(module, {"stop_hook_active": False}, self.project, self.tmp)
        self.assertEqual(self.runs("root"), 2)

    def test_nested_git_failure_not_cached(self):
        """#10 리뷰 R4: 중첩 저장소의 git이 실패하면 상태를 확정할 수 없으므로 캐시하지 않는다."""
        self.setup_repo({"hooks": {"stop_verify": [self.marker_cmd("root")]}})
        sub = self.project / "sub"
        sub.mkdir()
        git(sub, "init", "-q")
        (sub / "f.txt").write_text("1", encoding="utf-8")
        module = load_stop_module()
        real = module.common.git_result

        def failing(repo, *args, **kwargs):
            if Path(repo).resolve() == sub.resolve() and args[:1] == ("status",):
                return None, "fatal: 주입한 실패"
            return real(repo, *args, **kwargs)

        module.common.git_result = failing
        for _ in range(2):
            call_main(module, {"stop_hook_active": False}, self.project, self.tmp)
        self.assertEqual(self.runs("root"), 2)
        self.assertFalse(git_path(self.project, "stop-verify.json").exists())

    def test_slow_nested_git_counts_against_budget(self):
        """#10 리뷰 R3: 해시 중 git 호출도 예산 안에서 하고, 예산을 넘기면 캐시하지 않는다."""
        self.setup_repo({"hooks": {"stop_verify": [self.marker_cmd("root")]}})
        sub = self.project / "sub"
        sub.mkdir()
        git(sub, "init", "-q")
        (sub / "f.txt").write_text("1", encoding="utf-8")
        module = load_stop_module()
        module.common.MAX_STOP_TIMEOUT = 1
        real = module.common.git_result

        def slow(repo, *args, **kwargs):
            if Path(repo).resolve() == sub.resolve():
                time.sleep(1.2)
            return real(repo, *args, **kwargs)

        module.common.git_result = slow
        code, _, err = call_main(module, {"stop_hook_active": False}, self.project, self.tmp)
        self.assertEqual(code, 2)
        self.assertIn("전체 시간 예산", err)
        self.assertFalse(git_path(self.project, "stop-verify.json").exists())
        fresh = load_stop_module()  # 예산이 충분하면 다음 실행에서 명령이 실제로 돈다
        call_main(fresh, {"stop_hook_active": False}, self.project, self.tmp)
        self.assertEqual(self.runs("root"), 1)

    def test_hash_within_budget(self):
        self.setup_repo({"hooks": {"stop_verify": [self.marker_cmd("root")]}})
        (self.project / "x.txt").write_text("1", encoding="utf-8")
        module = load_stop_module()
        module.common.MAX_STOP_TIMEOUT = 0
        code, _, err = call_main(module, {"stop_hook_active": False}, self.project, self.tmp)
        self.assertEqual(code, 2)
        self.assertIn("전체 시간 예산", err)
        self.assertEqual(self.runs("root"), 0)
        cache = git_path(self.project, "stop-verify.json")
        self.assertFalse(cache.exists())  # 상태를 확정하지 못했으므로 캐시하지 않는다

    def test_rename_counts_both_areas(self):
        self.write_config({"areas": [{"dir": "a", "verify": [self.marker_cmd("a")]},
                                     {"dir": "b", "verify": [self.marker_cmd("b")]}]})
        (self.project / "a").mkdir()
        (self.project / "b").mkdir()
        (self.project / "a" / "f.txt").write_text("1", encoding="utf-8")
        self.init_git()
        git(self.project, "mv", "a/f.txt", "b/f.txt")
        self.stop()
        self.assertEqual((self.runs("a"), self.runs("b")), (1, 1))

    def test_bad_area_type_fails_open(self):
        self.setup_repo({"areas": [{"dir": "a", "verify": "ab"}]})
        (self.project / "a").mkdir()
        (self.project / "a" / "x").write_text("1", encoding="utf-8")
        result = self.stop()
        self.assertEqual(result.returncode, 1)
        self.assertIn("꺼진 상태", result.stderr.decode("utf-8"))
        self.write_config({"areas": [{"dir": "a/", "verify": ["x"]}]})
        self.assertEqual(self.stop().returncode, 1)

    def test_orphan_grandchild_does_not_stall(self):
        # 손자의 작업 디렉터리를 테스트 폴더 밖에 둔다. Windows는 사용 중인 디렉터리를 지우지 못해 임시 폴더가 남는다(#93)
        spawn = ("import subprocess,sys,tempfile; "
                 "subprocess.Popen([sys.executable,'-c','import time; time.sleep(8)'], cwd=tempfile.gettempdir())")
        self.setup_repo({"hooks": {"stop_verify": [f"\"{PY}\" -c \"{spawn}\""]}})
        (self.project / "x.txt").write_text("1", encoding="utf-8")
        started = time.monotonic()
        result = self.stop()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(time.monotonic() - started, 6)

    def test_total_budget(self):
        self.setup_repo({"hooks": {"stop_verify": [f"\"{PY}\" -c \"import time; time.sleep(3)\"",
                                                   self.marker_cmd("second")]}})
        (self.project / "x.txt").write_text("1", encoding="utf-8")
        module = load_stop_module()
        module.common.MAX_STOP_TIMEOUT = 1
        code, _, err = call_main(module, {"stop_hook_active": False}, self.project, self.tmp)
        self.assertEqual(code, 2)
        self.assertIn("전체 시간 예산", err)
        self.assertEqual(self.runs("second"), 0)

    def test_decode_output_falls_back_to_locale(self):
        text = common.decode_output("검증 실패".encode("cp949"))
        self.assertIsInstance(text, str)
        if locale.getpreferredencoding(False).lower() in ("cp949", "euc-kr", "ms949"):
            self.assertEqual(text, "검증 실패")
        self.assertEqual(common.decode_output("ok".encode("utf-8")), "ok")

    def test_timeout_is_failure(self):
        self.setup_repo({"hooks": {"stop_timeout_sec": 1,
                                   "stop_verify": [f"\"{PY}\" -c \"import time; time.sleep(5)\""]}})
        (self.project / "x.txt").write_text("1", encoding="utf-8")
        result = self.stop()
        self.assertEqual(result.returncode, 2)
        self.assertIn("시간 초과", result.stderr.decode("utf-8"))

    def test_backslashes_reach_the_shell_intact(self):
        """#115 리뷰 B F2: 명령의 `\\\\`가 Windows 인자 규칙에 줄지 않는다(bash에서 4글자 `x\\\\y`)"""
        self.setup_repo({"hooks": {"stop_verify": ["test \"$(printf %s 'x\\\\y' | wc -c)\" -eq 4"]}})
        (self.project / "x.txt").write_text("1", encoding="utf-8")
        result = self.stop()
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))

    @unittest.skipUnless(os.name == "nt", "Windows 작업 개체")
    def test_timeout_kills_exec_chain_children(self):
        """#115 리뷰 B F1: sh 스크립트가 exec로 띄운 프로세스도 시간 초과에 끝난다(taskkill /T가 놓치던 경로)"""
        pid_file = (self.outside / "child.pid").as_posix()
        child = self.outside / "child.py"
        child.write_text(f"import os, time\nopen({pid_file!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)\n",
                         encoding="utf-8")
        self.setup_repo({"hooks": {"stop_timeout_sec": 2, "stop_verify": ["./run.sh"]}})
        script = self.project / "run.sh"
        script.write_bytes(f"#!/bin/sh\nexec \"{Path(PY).as_posix()}\" \"{child.as_posix()}\"\n".encode("utf-8"))
        (self.project / "x.txt").write_text("1", encoding="utf-8")
        result = self.stop()
        self.assertEqual(result.returncode, 2)
        self.assertIn("시간 초과", result.stderr.decode("utf-8", "replace"))
        pid = (self.outside / "child.pid").read_text(encoding="utf-8").strip()
        time.sleep(1)
        alive = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        self.assertNotIn(pid, alive)

    def test_not_git_repo(self):
        self.write_config({"hooks": {"stop_verify": [self.marker_cmd("root", 1)]}})
        result = self.stop()
        self.assertEqual(result.returncode, 0)
        self.assertIn("git 상태를 읽지 못해", json.loads(result.stdout.decode("utf-8"))["systemMessage"])
        self.assertEqual(self.runs("root"), 0)

    def test_bad_config_fails_open(self):
        self.init_git()
        (self.project / "harness.json").write_text("{", encoding="utf-8")
        result = self.stop()
        self.assertEqual(result.returncode, 1)
        self.assertIn("꺼진 상태", result.stderr.decode("utf-8"))


class SessionBaselineCase(HookCase):
    """#82: 세션 시작 기준선(session-start.py)과 그 이후 커밋된 변경의 종료 검증."""

    # StopVerifyTest를 상속하면 그 테스트가 다시 돌므로 도우미만 가져온다
    marker_cmd = StopVerifyTest.marker_cmd
    runs = StopVerifyTest.runs
    setup_repo = StopVerifyTest.setup_repo
    stop = StopVerifyTest.stop

    def start(self, session="s1", source="startup", project=None):
        return self.hook("session-start.py", {"hook_event_name": "SessionStart", "session_id": session,
                                              "source": source, "cwd": str(project or self.project)},
                         project=project)

    def baselines(self, project=None):
        path = git_path(project or self.project, "sessions.json")
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def head(self, project=None):
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=project or self.project, check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, rel, text="1", project=None):
        project = project or self.project
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        git(project, "add", "-A")
        git(project, "commit", "-q", "-m", f"change {rel}")


class SessionStartTest(SessionBaselineCase):
    def test_records_head(self):
        self.init_git()
        result = self.start()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))
        self.assertEqual(self.baselines()["s1"]["head"], self.head())

    def test_resume_keeps_baseline(self):
        self.init_git()
        self.start()
        first = self.head()
        self.commit("x.txt")
        self.assertEqual(self.start(source="resume").returncode, 0)
        self.assertEqual(self.baselines()["s1"]["head"], first)
        self.start(session="s2")
        self.assertEqual(self.baselines()["s2"]["head"], self.head())

    def test_non_git_and_bad_payload_silent(self):
        for raw in (None, "{", "[1]", json.dumps({"source": "startup"})):
            with self.subTest(raw=raw):
                result = self.start() if raw is None else self.hook("session-start.py", None, raw=raw)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))
        self.assertFalse((self.project / ".git").exists())
        self.init_git()
        self.hook("session-start.py", None, raw=json.dumps({"source": "startup"}))  # session_id 없음
        self.assertEqual(self.baselines(), {})

    def test_prunes_old_entries(self):
        self.init_git()
        path = git_path(self.project, "sessions.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        now = common.datetime.datetime.now(common.datetime.timezone.utc)
        old = (now - common.datetime.timedelta(days=31)).isoformat(timespec="seconds")
        data = {"old": {"head": "x", "ts": old}, "broken": "not-an-object"}
        data.update({f"k{i}": {"head": "x", "ts": (now - common.datetime.timedelta(minutes=i)).isoformat()}
                     for i in range(1, 202)})
        path.write_text(json.dumps(data), encoding="utf-8")
        self.start(session="new")
        kept = self.baselines()
        self.assertEqual(len(kept), common.BASELINE_MAX_ENTRIES)
        self.assertIn("new", kept)
        self.assertNotIn("old", kept)
        self.assertNotIn("broken", kept)
        self.assertNotIn("k201", kept)  # 가장 오래된 것부터 상한 밖으로

    def test_prunes_by_age_and_shape_below_cap(self):
        """리뷰 A-3: 상한(200)과 무관하게 30일 지난 항목과 객체가 아닌 항목을 지운다."""
        self.init_git()
        path = git_path(self.project, "sessions.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        now = common.datetime.datetime.now(common.datetime.timezone.utc)
        path.write_text(json.dumps({
            "old": {"head": "x", "ts": (now - common.datetime.timedelta(days=31)).isoformat()},
            "recent": {"head": "x", "ts": (now - common.datetime.timedelta(days=29)).isoformat()},
            "broken": "not-an-object",
        }), encoding="utf-8")
        self.start(session="new")
        self.assertEqual(set(self.baselines()), {"recent", "new"})

    def test_replace_failure_leaves_no_temp(self):
        """리뷰 A-1·B-5: 교체가 실패해도(Windows에서 다른 프로세스가 읽는 중) 임시 파일을 남기지 않는다."""
        self.init_git()
        self.start(session="first")
        with mock.patch.object(common.os, "replace", side_effect=PermissionError("in use")):
            common.record_baseline(self.project, "second")
        state_dir = git_path(self.project, "sessions.json").parent
        self.assertEqual([p.name for p in state_dir.iterdir() if p.name.endswith(".tmp")], [])
        self.assertEqual(set(self.baselines()), {"first"})

    def test_bad_project_dir_silent(self):
        """리뷰 A-2: 프로젝트 경로를 만들 수 없어도 종료 0, 출력 없음."""
        result = self.hook("session-start.py", {"session_id": "s1", "cwd": 123}, env={"CLAUDE_PROJECT_DIR": ""})
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))

    def test_unborn_head_recorded_as_null(self):
        git(self.project, "init", "-q")
        self.start()
        self.assertEqual(self.baselines()["s1"], {"head": None, "ts": self.baselines()["s1"]["ts"]})


class CommittedStopVerifyTest(SessionBaselineCase):
    def areas(self, *names, code=0):
        return {"areas": [{"dir": name, "verify": [self.marker_cmd(name, code)]} for name in names]}

    def test_committed_change_verified(self):
        self.setup_repo(self.areas("a", "b"))
        self.start()
        self.commit("a/x.txt")
        self.assertEqual(self.stop().returncode, 0)  # 작업 트리는 깨끗하다
        self.assertEqual((self.runs("a"), self.runs("b")), (1, 0))

    def test_committed_failure_blocks(self):
        self.setup_repo(self.areas("a", code=1))
        self.start()
        self.commit("a/x.txt")
        result = self.stop()
        self.assertEqual(result.returncode, 2)
        self.assertIn("종료 코드 1", result.stderr.decode("utf-8"))
        event = json.loads(git_path(self.project, "events.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual((event["hook"], event["decision"]), ("stop-verify", "fail"))

    def test_no_baseline_keeps_old_behavior(self):
        self.setup_repo(self.areas("a"))
        self.start(session="other")  # 다른 세션의 기준선(커밋 전 HEAD)은 쓰지 않는다(리뷰 B-4)
        self.commit("a/x.txt")  # s1은 SessionStart가 없던 세션(이전 설치)
        self.assertEqual(self.stop().returncode, 0)
        self.assertEqual(self.runs("a"), 0)

    def test_unknown_baseline_falls_back(self):
        self.setup_repo(self.areas("a", "b"))
        path = git_path(self.project, "sessions.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"s1": {"head": "0" * 40, "ts": "2026-10-13T00:00:00+00:00"}}), encoding="utf-8")
        self.commit("a/x.txt")
        (self.project / "b").mkdir()
        (self.project / "b" / "y.txt").write_text("1", encoding="utf-8")
        result = self.stop()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.runs("a"), self.runs("b")), (0, 1))

    def test_committed_and_uncommitted_union(self):
        self.setup_repo(self.areas("a", "b"))
        self.start()
        self.commit("a/x.txt")
        (self.project / "b").mkdir()
        (self.project / "b" / "y.txt").write_text("1", encoding="utf-8")
        self.stop()
        self.assertEqual((self.runs("a"), self.runs("b")), (1, 1))

    def test_committed_rename_counts_both_paths(self):
        self.setup_repo(self.areas("a", "b"))
        self.commit("a/x.txt", "same content")
        self.start()
        (self.project / "b").mkdir()
        git(self.project, "mv", "a/x.txt", "b/x.txt")
        git(self.project, "commit", "-q", "-m", "move")
        self.stop()
        self.assertEqual((self.runs("a"), self.runs("b")), (1, 1))

    def test_committed_outside_subproject_ignored(self):
        app = self.project / "app"
        app.mkdir()
        config = {**self.areas("a"), "hooks": {"stop_verify": [self.marker_cmd("root")]}}
        (app / "harness.json").write_text(json.dumps(config), encoding="utf-8")
        self.init_git()
        self.start(project=app)

        def stop():
            return self.hook("stop-verify.py", {"stop_hook_active": False, "session_id": "s1"}, project=app)

        self.commit("outside.txt")  # 프로젝트 밖 커밋은 루트 stop_verify도 부르지 않는다(리뷰 B-3)
        self.assertEqual(stop().returncode, 0)
        self.assertEqual((self.runs("a"), self.runs("root")), (0, 0))
        self.commit("app/a/x.txt")
        self.assertEqual(stop().returncode, 0)
        self.assertEqual((self.runs("a"), self.runs("root")), (1, 0))

    def test_committed_state_cached(self):
        self.setup_repo(self.areas("a"))
        self.start()
        self.commit("a/x.txt")
        self.stop()
        self.stop()
        self.assertEqual(self.runs("a"), 1)
        self.commit("a/x.txt", "2")
        self.stop()
        self.assertEqual(self.runs("a"), 2)

    def test_unborn_baseline_counts_all_commits(self):
        self.write_config(self.areas("a"))
        git(self.project, "init", "-q")
        git(self.project, "config", "user.email", "t@example.com")
        git(self.project, "config", "user.name", "t")
        self.start()  # 첫 커밋 전에 시작한 세션
        self.commit("a/x.txt")
        self.stop()
        self.assertEqual(self.runs("a"), 1)


if __name__ == "__main__":
    unittest.main()
