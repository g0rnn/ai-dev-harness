"""Test launch policy without starting Git, a shell, or any child process."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import process_support as support


class Startup:
    def __init__(self):
        self.dwFlags = 0
        self.wShowWindow = 1
        self.lpAttributeList = {"handle_list": [123]}


def windows_api():
    return SimpleNamespace(DETACHED_PROCESS=8, CREATE_NO_WINDOW=0x08000000,
                           CREATE_NEW_CONSOLE=16, STARTF_USESHOWWINDOW=1,
                           SW_HIDE=0, STARTUPINFO=Startup)


class ProcessSupportTest(unittest.TestCase):
    def test_git_and_shell_tree_gets_hidden_console_without_changing_io(self):
        env = {"TEST": "value"}
        startup = Startup()
        startup.dwFlags = 0x100  # Existing redirected standard handles.
        options = {"creationflags": 0x200, "startupinfo": startup, "stdin": -1,
                   "stdout": -1, "stderr": -1, "cwd": "repo", "env": env}
        with patch.object(support, "subprocess", windows_api()):
            result = support.window_options(options, "nt")
        self.assertEqual(result["creationflags"], 0x210)
        self.assertEqual(result["startupinfo"].dwFlags, 0x101)
        self.assertEqual(result["startupinfo"].wShowWindow, 0)
        self.assertEqual(startup.wShowWindow, 1)  # Caller-owned state unchanged.
        self.assertEqual(result["startupinfo"].lpAttributeList, startup.lpAttributeList)
        for name in ("stdin", "stdout", "stderr", "cwd", "env"):
            self.assertEqual(result[name], options[name])

    def test_explicit_detached_and_no_window_contracts_are_preserved(self):
        for flags in (8, 0x08000000, 0x08000008):
            with self.subTest(flags=flags), patch.object(support, "subprocess", windows_api()):
                options = {"creationflags": flags, "stdout": -3}
                self.assertEqual(support.window_options(options, "nt"), options)

    def test_posix_options_are_unchanged(self):
        options = {"start_new_session": True, "env": {"TEST": "value"}}
        self.assertEqual(support.window_options(options, "posix"), options)

    def test_install_covers_direct_popen_once_without_spawning(self):
        calls = []

        class FakePopen:
            def __init__(self, *args, **kwargs):
                calls.append((args, kwargs))

        api = windows_api()
        api.Popen = FakePopen
        with patch.object(support, "subprocess", api), patch.object(support, "os", SimpleNamespace(name="nt")):
            support.install()
            installed = api.Popen
            support.install()
            self.assertIs(api.Popen, installed)
            api.Popen(["git", "status"], stdout=-1)
        self.assertEqual(calls[0][0], (["git", "status"],))
        self.assertEqual(calls[0][1]["creationflags"], 16)
        self.assertEqual(calls[0][1]["startupinfo"].wShowWindow, 0)
        self.assertEqual(calls[0][1]["stdout"], -1)


    def test_run_preserves_exit_code_and_communicated_streams_without_spawning(self):
        import subprocess
        calls = []

        class FakePopen:
            def __init__(self, args, **kwargs):
                self.args, self.returncode = args, 7
                calls.append(kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                pass

            def communicate(self, input=None, timeout=None):
                calls.append((input, timeout))
                return "output", "error"

            def poll(self):
                return self.returncode

        with patch.object(subprocess, "Popen", FakePopen), \
                patch.object(support, "os", SimpleNamespace(name="nt")), \
                patch.multiple(subprocess, CREATE_NEW_CONSOLE=16, CREATE_NO_WINDOW=0x08000000,
                               DETACHED_PROCESS=8, STARTUPINFO=Startup, STARTF_USESHOWWINDOW=1,
                               SW_HIDE=0, create=True):
            support.install()
            result = subprocess.run(["git", "status"], input="payload", capture_output=True,
                                    text=True, timeout=30, env={"TEST": "value"})
        self.assertEqual((result.returncode, result.stdout, result.stderr), (7, "output", "error"))
        self.assertEqual(calls[1], ("payload", 30))
        self.assertEqual(calls[0]["env"], {"TEST": "value"})
        self.assertEqual(calls[0]["startupinfo"].wShowWindow, 0)


if __name__ == "__main__":
    unittest.main()
