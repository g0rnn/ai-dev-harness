"""#115 Windows 검증 명령 셸 선택(harness_common.verify_shell)."""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import process_support  # noqa: E402,F401 — test-only Windows child launch policy

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("harness_common_shell", ROOT / "core" / "hooks" / "harness_common.py")
common = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(common)

# Windows에서는 hook과 같은 규칙으로 고른다(PATH의 WSL 실행기는 환경 변수를 넘기지 않는다, #131 리뷰 F1)
REAL_BASH = common.verify_shell("nt", os.environ, shutil.which) if os.name == "nt" else shutil.which("bash")


class VerifyShellTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def touch(self, *parts):
        path = self.tmp.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
        return path

    def test_posix_uses_default_shell(self):
        self.assertIsNone(common.verify_shell("posix", {}, lambda name: None))
        self.assertEqual(common.shell_command("a && b", None), ("a && b", True, None))

    def test_windows_prefers_claude_code_variable(self):
        bash = self.touch("custom", "bash.exe")
        self.assertEqual(common.verify_shell("nt", {"CLAUDE_CODE_GIT_BASH_PATH": str(bash)}, lambda name: None),
                         str(bash))
        args, shell, env = common.shell_command("a && b", str(bash))
        self.assertEqual((args, shell, env[common.VERIFY_CMD_ENV]),
                         ([str(bash), "-c", '__harness_cmd=$HARNESS_VERIFY_CMD; unset HARNESS_VERIFY_CMD; '
                                            'eval "$__harness_cmd"'], False, "a && b"))

    @unittest.skipUnless(REAL_BASH, "bash가 없다")
    def test_command_does_not_leak_variable(self):
        # #131: 검증 명령의 하위 프로세스(테스트 등)에 HARNESS_VERIFY_CMD가 남으면 HARNESS_*를 넘기는 도구의 테스트가 깨진다
        cmd = 'printf "%s|%s\\n" "${HARNESS_VERIFY_CMD-none}" "a\\\\b"; exit 3'
        args, shell, env = common.shell_command(cmd, REAL_BASH)
        result = subprocess.run(args, shell=shell, env=env, capture_output=True, text=True)
        self.assertEqual((result.returncode, result.stdout.strip()), (3, "none|a\\b"))

    def test_windows_finds_bash_next_to_git(self):
        for git in (("Git", "cmd", "git.exe"), ("Git2", "mingw64", "bin", "git.exe")):
            git_exe = self.touch(*git)
            bash = self.touch(git[0], "bin", "bash.exe")
            found = common.verify_shell("nt", {}, lambda name, exe=git_exe: str(exe) if name == "git" else None)
            self.assertEqual(Path(found).resolve(), bash.resolve())

    def test_windows_skips_wsl_launcher_and_missing(self):
        wsl = self.touch("Windows", "System32", "bash.exe")
        self.assertIsNone(common.verify_shell("nt", {}, lambda name: str(wsl) if name == "bash" else None))
        store = self.touch("Microsoft", "WindowsApps", "bash.exe")  # #115 리뷰 A F1: 스토어 WSL 실행기
        self.assertIsNone(common.verify_shell("nt", {}, lambda name: str(store) if name == "bash" else None))
        not_bash = self.touch("Windows", "cmd.exe")  # #115 리뷰 B F3: bash가 아닌 실행 파일
        self.assertIsNone(common.verify_shell("nt", {"CLAUDE_CODE_GIT_BASH_PATH": str(not_bash)}, lambda name: None))
        self.assertIsNone(common.verify_shell("nt", {"CLAUDE_CODE_GIT_BASH_PATH": str(self.tmp / "none.exe")},
                                              lambda name: None))
        other = self.touch("tools", "bash.exe")
        self.assertEqual(common.verify_shell("nt", {}, lambda name: str(other) if name == "bash" else None), str(other))


if __name__ == "__main__":
    unittest.main()
