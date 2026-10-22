"""core/metrics/telemetry_setup.py 테스트(#145). 사용자 설정 디렉터리는 모두 임시 디렉터리로 돌린다."""

import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core" / "hooks"))
import harness_common as common  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tempdirs import remove_tree  # noqa: E402
from test_telemetry import Collector  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("telemetry_setup_under_test", ROOT / "core" / "metrics" / "telemetry_setup.py")
setup_module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(setup_module)

HELPER = ROOT / "core" / "metrics" / "otel_headers.py"
ENDPOINT = "https://collector.example"
TOKEN = "secret-sentinel-token"
HARNESS_JSON = '{\n  "project_name": "demo",\n  "areas": [{"dir": "web", "verify": ["npm test"]}]\n}\n'


class SetupCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(remove_tree, self.tmp)
        self.project = self.tmp / "proj"
        self.project.mkdir()
        (self.project / "harness.json").write_text(HARNESS_JSON, encoding="utf-8")
        self.home = self.tmp / "home"
        # 자격 파일 위치(APPDATA·XDG)와 도구 설정 위치를 모두 임시 디렉터리로 돌린다
        self.env = {**os.environ, "APPDATA": str(self.tmp / "appdata"), "XDG_CONFIG_HOME": str(self.tmp / "xdg"),
                    "CLAUDE_CONFIG_DIR": str(self.tmp / "claude"), "CODEX_HOME": str(self.tmp / "codex")}
        self.paths = setup_module.user_paths(common, self.env, self.home)
        self.out = []

    def setup(self, **kwargs):
        options = {"endpoint": ENDPOINT, "token": TOKEN, "helper_source": HELPER, "yes": True}
        options.update(kwargs)
        return setup_module.setup(common, self.project, env=self.env, home=self.home, out=self.out.append, **options)

    def doctor(self, **kwargs):
        options = {"helper_source": HELPER, "offline": True}
        options.update(kwargs)
        self.out = []
        return setup_module.doctor(common, self.project, env=self.env, home=self.home, out=self.out.append, **options)

    def write(self, path: Path, text: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def assert_no_token_in_output(self):
        self.assertNotIn(TOKEN, "\n".join(self.out))


class ProjectPlanTest(SetupCase):
    def test_block_appended_without_reformatting(self):
        self.assertEqual(self.setup(), 0)
        text = (self.project / "harness.json").read_text(encoding="utf-8")
        self.assertTrue(text.startswith(HARNESS_JSON.rstrip()[:-1].rstrip()))  # 원래 줄은 그대로다
        self.assertEqual(json.loads(text)["telemetry"], {"endpoint": ENDPOINT})
        self.assertFalse((self.project / "harness.json.harness-backup").exists())  # 커밋 대상이라 백업하지 않는다

    def test_endpoint_rules(self):
        self.assertEqual(self.setup(endpoint=None), 2)
        self.assertIn("--endpoint", self.out[-1])
        self.assertEqual(self.setup(endpoint="http://collector.example"), 2)
        self.assertEqual(self.setup(endpoint=ENDPOINT + "/"), 0)
        self.assertEqual(json.loads((self.project / "harness.json").read_text(encoding="utf-8"))["telemetry"],
                         {"endpoint": ENDPOINT})
        self.assertEqual(self.setup(endpoint="https://other.example"), 2)  # 팀 공통 값은 MR로 바꾼다
        self.assertEqual(self.setup(endpoint=None), 0)  # 블록이 있으면 인자가 없어도 된다

    def test_missing_harness_json(self):
        (self.project / "harness.json").unlink()
        self.assertEqual(self.setup(), 2)


class CredentialsTest(SetupCase):
    def test_token_saved_privately_and_reused(self):
        self.assertEqual(self.setup(), 0)
        self.assertEqual(common.telemetry_headers(ENDPOINT, self.paths.credentials), {"Authorization": f"Bearer {TOKEN}"})
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.paths.credentials.stat().st_mode), 0o600)
        self.assertEqual(self.setup(token=None), 0)  # 저장된 토큰을 쓴다
        self.assertIn("바꿀 것이 없다", "\n".join(self.out))
        self.assert_no_token_in_output()

    def test_missing_token_is_error(self):
        self.assertEqual(self.setup(token=None), 2)
        self.assertIn("토큰이 없다", self.out[-1])

    def test_token_rotation_rewrites_codex_too(self):
        self.assertEqual(self.setup(), 0)
        self.assertEqual(self.setup(token="rotated-token"), 0)
        otel = tomllib.loads(self.paths.codex_config.read_text(encoding="utf-8"))["otel"]
        self.assertEqual(otel["exporter"]["otlp-http"]["headers"], {"Authorization": "Bearer rotated-token"})
        self.assertNotIn("rotated-token", "\n".join(self.out))


class ClaudeTest(SetupCase):
    def test_keeps_other_settings_and_drops_plaintext_headers(self):
        self.write(self.paths.claude_settings, json.dumps({
            "model": "opus", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x"}]}]},
            "env": {"KEEP": "1", "OTEL_EXPORTER_OTLP_HEADERS": f"Authorization=Bearer {TOKEN}",
                    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": ENDPOINT + "/v1/logs"}}))
        self.assertEqual(self.setup(), 0)
        data = json.loads(self.paths.claude_settings.read_text(encoding="utf-8"))
        self.assertEqual((data["model"], data["hooks"]["Stop"][0]["hooks"][0]["command"]), ("opus", "x"))
        self.assertEqual(data["env"]["KEEP"], "1")
        self.assertEqual(data["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"], ENDPOINT)
        self.assertEqual(data["env"]["CLAUDE_CODE_ENABLE_TELEMETRY"], "1")
        self.assertNotIn("OTEL_EXPORTER_OTLP_HEADERS", data["env"])
        self.assertNotIn("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", data["env"])
        self.assertIn(setup_module.HELPER_NAME, data["otelHeadersHelper"])
        backup = self.paths.claude_settings.with_name("settings.json" + setup_module.BACKUP_SUFFIX)
        self.assertEqual(json.loads(backup.read_text(encoding="utf-8"))["model"], "opus")
        self.assert_no_token_in_output()

    def test_other_collector_needs_force(self):
        self.write(self.paths.claude_settings, json.dumps({"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://other.example"}}))
        self.assertEqual(self.setup(), 2)
        self.assertIn("다른 수집기 설정", self.out[-1])
        self.assertEqual(json.loads(self.paths.claude_settings.read_text(encoding="utf-8"))["env"]
                         ["OTEL_EXPORTER_OTLP_ENDPOINT"], "https://other.example")  # 아무것도 쓰지 않았다
        self.assertFalse(self.paths.credentials.exists())
        self.assertEqual(self.setup(force=True), 0)

    def test_own_helper_command_is_not_conflict(self):
        self.write(self.paths.claude_settings, json.dumps({"otelHeadersHelper": "custom-script --flag"}))
        self.assertEqual(self.setup(), 2)
        self.assertEqual(self.setup(force=True), 0)
        self.assertEqual(self.setup(), 0)  # 키트가 넣은 명령은 충돌로 보지 않는다

    def test_broken_settings_untouched(self):
        self.write(self.paths.claude_settings, "{not json")
        self.assertEqual(self.setup(), 2)
        self.assertEqual(self.paths.claude_settings.read_text(encoding="utf-8"), "{not json")

    def test_helper_command_quoting(self):
        helper = Path("C:/Program Files/harness/otel_headers.py")
        nt = setup_module.helper_command("C:/Python 3/python.exe", helper, ENDPOINT, "nt")
        self.assertTrue(nt.startswith('"C:/Python 3/python.exe" "'))
        posix = setup_module.helper_command("/usr/bin/python3", PurePosixPath("/home/a b/otel_headers.py"), ENDPOINT,
                                            "posix")
        self.assertEqual(posix, f"/usr/bin/python3 '/home/a b/otel_headers.py' {ENDPOINT}")


class CodexTest(SetupCase):
    USER = 'model = "gpt-5"\n\n[projects."C:/x"]\ntrust_level = "trusted"\n'

    def test_block_appended_and_parses(self):
        self.write(self.paths.codex_config, self.USER)
        self.assertEqual(self.setup(), 0)
        text = self.paths.codex_config.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(self.USER))
        data = tomllib.loads(text)
        self.assertEqual(data["projects"]["C:/x"]["trust_level"], "trusted")
        for key, signal in (("exporter", "logs"), ("metrics_exporter", "metrics")):
            exporter = data["otel"][key]["otlp-http"]
            self.assertEqual((exporter["endpoint"], exporter["protocol"]), (f"{ENDPOINT}/v1/{signal}", "binary"))
            self.assertEqual(exporter["headers"], {"Authorization": f"Bearer {TOKEN}"})
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.paths.codex_config.stat().st_mode), 0o600)
        self.assertEqual(self.setup(), 0)
        self.assertEqual(self.paths.codex_config.read_text(encoding="utf-8"), text)  # 다시 실행해도 같다

    def test_user_otel_table_is_not_overwritten(self):
        for text in ('[otel]\nenvironment = "mine"\n', 'otel.environment = "mine"\n'):
            with self.subTest(text=text):
                self.write(self.paths.codex_config, text)
                self.assertEqual(self.setup(force=True), 2)
                self.assertIn("직접 둔 [otel]", self.out[-1])
                self.assertEqual(self.paths.codex_config.read_text(encoding="utf-8"), text)

    def test_broken_marker_or_toml_stops(self):
        for text in (setup_module.CODEX_BEGIN + "\n[otel]\n", "model = \n"):
            with self.subTest(text=text):
                self.write(self.paths.codex_config, text)
                self.assertEqual(self.setup(), 2)

    def test_only_selected_tools(self):
        harness = json.loads(HARNESS_JSON)
        harness["telemetry"] = {"endpoint": ENDPOINT, "tools": ["codex"]}
        (self.project / "harness.json").write_text(json.dumps(harness), encoding="utf-8")
        self.assertEqual(self.setup(endpoint=None), 0)
        self.assertFalse(self.paths.claude_settings.exists())
        self.assertFalse(self.paths.helper.exists())
        self.assertTrue(self.paths.codex_config.exists())


class ApplyTest(SetupCase):
    def test_dry_run_and_unconfirmed_write_nothing(self):
        before = (self.project / "harness.json").read_text(encoding="utf-8")
        self.assertEqual(self.setup(dry_run=True), 0)
        self.assertEqual(self.setup(yes=False), 1)  # 승인 수단이 없으면 쓰지 않는다
        self.assertEqual(self.setup(yes=False, confirm=lambda prompt: False), 1)
        self.assertEqual((self.project / "harness.json").read_text(encoding="utf-8"), before)
        for path in (self.paths.credentials, self.paths.claude_settings, self.paths.codex_config):
            self.assertFalse(path.exists(), path)
        self.assertEqual(self.setup(yes=False, confirm=lambda prompt: True), 0)
        self.assertTrue(self.paths.credentials.exists())
        self.assert_no_token_in_output()


class DoctorTest(SetupCase):
    def levels(self):
        return [line.split("]")[0].lstrip("[") for line in self.out if line.startswith("[")]

    def test_reports_missing_pieces(self):
        self.assertEqual(self.doctor(), 1)
        self.assertIn("telemetry 블록이 없다", self.out[0])
        self.assertEqual(self.setup(), 0)
        self.assertEqual(self.doctor(), 0, self.out)
        self.assertNotIn("실패", self.levels())
        self.paths.helper.unlink()
        data = json.loads(self.paths.claude_settings.read_text(encoding="utf-8"))
        data["env"]["OTEL_EXPORTER_OTLP_HEADERS"] = f"Authorization=Bearer {TOKEN}"
        self.write(self.paths.claude_settings, json.dumps(data))
        self.write(self.paths.codex_config, self.paths.codex_config.read_text(encoding="utf-8").replace(TOKEN, "stale"))
        self.assertEqual(self.doctor(), 1)
        text = "\n".join(self.out)
        for expected in ("스크립트가 없다", "평문 토큰 변수", "토큰이 자격 파일과 다르다"):
            self.assertIn(expected, text)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("stale", text)

    def test_probe_uses_token(self):
        collector = Collector()
        self.addCleanup(collector.close)
        harness = json.loads(HARNESS_JSON)
        harness["telemetry"] = {"endpoint": collector.url, "tools": ["codex"]}
        (self.project / "harness.json").write_text(json.dumps(harness), encoding="utf-8")
        self.assertEqual(self.setup(endpoint=None), 0)
        self.assertEqual(self.doctor(offline=False), 0, self.out)
        self.assertEqual(collector.headers[-1]["Authorization"], f"Bearer {TOKEN}")
        self.assertIn("수집기가 요청을 받았다", "\n".join(self.out))
        collector.fail = True
        self.assertEqual(self.doctor(offline=False), 1)
        self.assertIn("수집기 응답 500", "\n".join(self.out))


class TokenInputTest(unittest.TestCase):
    class Stream(io.StringIO):
        def __init__(self, text="", tty=False):
            super().__init__(text)
            self.tty = tty

        def isatty(self):
            return self.tty

    def test_stdin_token(self):
        self.assertEqual(setup_module.read_token(True, self.Stream("tok-1\n")), "tok-1")
        for bad in ("\n", "two words\n"):
            with self.subTest(bad=bad), self.assertRaises(setup_module.SetupError):
                setup_module.read_token(True, self.Stream(bad))

    def test_no_prompt_without_terminal(self):
        """Windows는 NUL 입력에도 isatty()가 참이다. 출력까지 터미널이 아니면 묻지 않는다(CI·AI 도구에서 멈추지 않게)."""
        self.assertIsNone(setup_module.read_token(False, self.Stream(tty=True), self.Stream(tty=False)))
        self.assertIsNone(setup_module.read_token(False, self.Stream(tty=False), self.Stream(tty=True)))
        self.assertTrue(setup_module.is_terminal(self.Stream(tty=True), self.Stream(tty=True)))


if __name__ == "__main__":
    unittest.main()
