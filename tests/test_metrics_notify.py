"""core/metrics/notify.py, `harness metrics notify`, core/ci/gitlab/metrics.yml, 매니페스트 복사 테스트(M4-2).
네트워크 없이 가짜 전송과 정적 fixture만 쓴다."""

import importlib.util
import io
import json
import os
import re
import secrets
import shutil
import tempfile
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "metrics"
SAMPLE = FIXTURES / "report-sample.json"
FRAGMENT = ROOT / "core" / "ci" / "gitlab" / "metrics.yml"
CI_README = ROOT / "core" / "ci" / "README.md"
METRICS_README = ROOT / "core" / "metrics" / "README.md"
METRICS_DIR = ROOT / "core" / "metrics"
SCRIPTS = ("collect.py", "report.py", "notify.py", "classify_ci.py")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


notify = load("metrics_notify", METRICS_DIR / "notify.py")
harness = load("harness_for_notify", ROOT / "bin" / "harness.py")

# 실제 토큰·비밀번호 형태를 쓰면 gitleaks(secret-detection 필수 체크)가 잡는다. 값은 모듈 로드 때 난수로 만든다
PASSWORD = "TESTONLY-" + secrets.token_hex(12)
SESSION = "TESTONLY-" + secrets.token_hex(12)
WEBHOOK_PATH = "/T/B/" + secrets.token_hex(8)
WEBHOOK_URL = f"https://hooks.example{WEBHOOK_PATH}"
MATTERMOST_URL = "https://mm.example"
REPORT_URL = "https://gitlab.example/group/demo/-/jobs/77/artifacts/file/report.html"
MATTERMOST_ENV = {"HARNESS_MATTERMOST_URL": MATTERMOST_URL, "HARNESS_MATTERMOST_LOGIN_ID": "bot@example",
                  "HARNESS_MATTERMOST_PASSWORD": PASSWORD, "HARNESS_MATTERMOST_TEAM": "demo-team",
                  "HARNESS_MATTERMOST_CHANNEL": "metrics"}
HARNESS_VARS = re.compile(r"\bHARNESS_[A-Z0-9]+(?:_[A-Z0-9]+)*\b")


def sample():
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


def json_response(status, data, headers=None):
    return notify.Response(status, {"Content-Type": "application/json", **(headers or {})},
                           json.dumps(data).encode("utf-8"))


class FakeTransport:
    """전송 함수 대역. (메서드, 호스트+경로 정규식, 처리 함수) 목록으로 응답하고 요청을 기록한다."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, headers, body=None):
        self.calls.append(SimpleNamespace(method=method, url=url, headers=dict(headers), body=body,
                                          payload=json.loads(body.decode("utf-8")) if body else None))
        parts = urllib.parse.urlsplit(url)
        for wanted, pattern, handler in self.routes:
            match = re.fullmatch(pattern, parts.netloc + parts.path)
            if match and wanted == method:
                return handler(self.calls[-1], *match.groups())
        return notify.Response(404)

    def paths(self):
        return [urllib.parse.urlsplit(call.url).path for call in self.calls]


def webhook_transport(status=200):
    return FakeTransport([("POST", r"hooks\.example/T/B/[0-9a-f]+", lambda call: notify.Response(status, {}, b"ok"))])


def mattermost_transport(login_status=200, post_data=None, logout_status=200):
    post_data = {"id": "p1", "channel_id": "c1"} if post_data is None else post_data
    return FakeTransport([
        ("POST", r"mm\.example/api/v4/users/login",
         lambda call: json_response(login_status, {"id": "u1"}, {"Token": SESSION})),
        ("GET", r"mm\.example/api/v4/teams/name/([^/]+)", lambda call, name: json_response(200, {"id": "t1", "name": name})),
        ("GET", r"mm\.example/api/v4/teams/t1/channels/name/([^/]+)",
         lambda call, name: json_response(200, {"id": "c1", "name": name})),
        ("POST", r"mm\.example/api/v4/posts", lambda call: json_response(201, post_data)),
        ("POST", r"mm\.example/api/v4/users/logout", lambda call: json_response(logout_status, {"status": "OK"})),
    ])


def run_notify(argv, env, transport):
    """notify.main을 실행하고 (종료 코드, stdout, stderr)를 돌려준다."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = notify.main(argv, env=env, transport=transport)
    return code, out.getvalue(), err.getvalue()


def header_of(path):
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("#"):
            break
        lines.append(line)
    return "\n".join(lines)


def jobs_of(path):
    """{최상위 키: 본문}. 주석과 빈 줄은 뺀다(tests/test_ci.py와 같은 방식)."""
    result, current = {}, None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        top = re.match(r"^([^\s#][^:]*):\s*$", line)
        if top:
            current = top.group(1)
            result[current] = []
        elif current is not None:
            result[current].append(line)
    return {k: "\n".join(v) for k, v in result.items()}


class MessageTest(unittest.TestCase):
    def test_build_message(self):
        """T1: 제목에 project·2026-W40·(진행 중), 지표 4줄에 값·지난주 값·증감, 마지막 줄 리포트 URL, 4,000자 이하."""
        message = notify.build_message(sample(), REPORT_URL)
        lines = message.split("\n")
        self.assertEqual(lines[0], "group/demo 주간 측정 요약 2026-W40 (진행 중)")
        self.assertEqual(lines[1:5], ["- 1 병합 후 누락 수: 1 (지난주 1, ±0)",
                                      "- 2 엄격 비율: 100% (1/1) (지난주 33% (1/3), +67%p)",
                                      "- 3 CI 실패 수: 2 (지난주 4, -2)",
                                      "- 4 판정 불일치 수: 0 (지난주 1, -1)"])
        self.assertEqual(lines[-1], f"리포트: {REPORT_URL}")
        self.assertEqual(len(lines), 6)
        self.assertLessEqual(len(message), notify.MAX_MESSAGE_LENGTH)
        # 서식 차이를 피해 강조·머리글 기호를 쓰지 않는다. 리포트 URL이 없으면 마지막 줄을 생략한다
        self.assertNotIn("**", message)
        self.assertFalse(any(line.startswith("#") for line in lines))
        without = notify.build_message(sample(), None)
        self.assertNotIn("리포트:", without)
        self.assertEqual(without.split("\n")[0], lines[0])
        # 완료된 주(W39 끝이 until보다 앞)면 (진행 중)이 없고 증가 방향은 +
        two = notify.build_message(dict(sample(), weeks=sample()["weeks"][:2]), None)
        self.assertEqual(two.split("\n")[0], "group/demo 주간 측정 요약 2026-W39")
        self.assertIn("- 1 병합 후 누락 수: 1 (지난주 0, +1)", two)
        self.assertIn("- 2 엄격 비율: 33% (1/3) (지난주 -, -)", two)
        self.assertIn("- 3 CI 실패 수: 4 (지난주 1, +3)", two)
        # 프로젝트 이름의 줄바꿈은 한 칸으로 접어 제목이 한 줄을 넘지 않는다. 프로젝트가 없으면 제목만 짧아진다
        folded = notify.build_message(dict(sample(), project="a\nb"), None)
        self.assertEqual(folded.split("\n")[0], "a b 주간 측정 요약 2026-W40 (진행 중)")
        self.assertEqual(notify.build_message({"version": 1, "weeks": sample()["weeks"]}, None).split("\n")[0],
                         "주간 측정 요약 2026-W40")
        # 4,000자 상한: 긴 프로젝트 이름이어도 상한 안이고 리포트 줄은 끝에 남는다
        long = notify.build_message(dict(sample(), project="x" * 5000), REPORT_URL)
        self.assertLessEqual(len(long), notify.MAX_MESSAGE_LENGTH)
        self.assertTrue(long.endswith(f"리포트: {REPORT_URL}"))
        self.assertIn("생략", long)

    def test_single_and_empty_weeks(self):
        """T2: 주 1개면 증감 `-`, weeks가 비면 "데이터 없음" 한 줄. 둘 다 --dry-run 종료 0."""
        one = notify.build_message(dict(sample(), weeks=sample()["weeks"][:1]), REPORT_URL)
        lines = one.split("\n")
        self.assertEqual(lines[0], "group/demo 주간 측정 요약 2026-W38")
        self.assertEqual(lines[1:5], ["- 1 병합 후 누락 수: 0 (지난주 -, -)", "- 2 엄격 비율: - (지난주 -, -)",
                                      "- 3 CI 실패 수: 1 (지난주 -, -)", "- 4 판정 불일치 수: 0 (지난주 -, -)"])
        empty = notify.build_message(dict(sample(), weeks=[]), REPORT_URL)
        self.assertEqual(empty.split("\n"), ["group/demo 주간 측정 요약", "데이터 없음", f"리포트: {REPORT_URL}"])
        self.assertEqual(notify.build_message({"version": 1, "weeks": []}, None), "주간 측정 요약\n데이터 없음")
        with tempfile.TemporaryDirectory() as tmp:
            for name, weeks in (("one.json", sample()["weeks"][:1]), ("empty.json", [])):
                with self.subTest(name=name):
                    source = Path(tmp) / name
                    source.write_text(json.dumps(dict(sample(), weeks=weeks)), encoding="utf-8")
                    transport = webhook_transport()
                    code, out, err = run_notify(["--in", str(source), "--dry-run"], {}, transport)
                    self.assertEqual(code, 0, err)
                    self.assertEqual(out, notify.build_message(dict(sample(), weeks=weeks), None) + "\n")
                    self.assertEqual(err, "")
                    self.assertEqual(transport.calls, [])


class WebhookTest(unittest.TestCase):
    def test_post_json(self):
        """T3: 호출 1회, POST, Content-Type JSON, 본문 {"text": 메시지}, 종료 0, stderr 요약에 호스트만."""
        transport = webhook_transport()
        code, out, err = run_notify(["--in", str(SAMPLE), "--report-url", REPORT_URL],
                                    {"HARNESS_NOTIFY_WEBHOOK_URL": WEBHOOK_URL}, transport)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "")
        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        self.assertEqual((call.method, call.url), ("POST", WEBHOOK_URL))
        self.assertEqual(call.headers["Content-Type"], "application/json")
        self.assertEqual(call.payload, {"text": notify.build_message(sample(), REPORT_URL)})
        self.assertNotIn("Authorization", call.headers)
        self.assertIn("hooks.example", err)
        self.assertIn("webhook", err)
        self.assertNotIn(WEBHOOK_PATH, err)
        self.assertNotIn("/T/B/", err)
        # auto 모드에서 webhook 변수가 Mattermost 변수보다 우선한다
        transport = webhook_transport()
        code, _out, err = run_notify(["--in", str(SAMPLE)],
                                     {"HARNESS_NOTIFY_WEBHOOK_URL": WEBHOOK_URL, **MATTERMOST_ENV}, transport)
        self.assertEqual(code, 0, err)
        self.assertEqual(transport.paths(), [WEBHOOK_PATH])

    def test_https_only_and_error_hides_path(self):
        """T4: http URL / 500 / 302 → 각각 종료 2. 메시지에 https 안내·`HTTP 500 hooks.example`·리다이렉트 오류, 경로 없음."""
        http_url = "http://hooks.example" + WEBHOOK_PATH
        transport = webhook_transport()
        code, _out, err = run_notify(["--in", str(SAMPLE)], {"HARNESS_NOTIFY_WEBHOOK_URL": http_url}, transport)
        self.assertEqual(code, 2)
        self.assertIn("https", err)
        self.assertIn("HARNESS_NOTIFY_WEBHOOK_URL", err)
        self.assertEqual(transport.calls, [])
        self.assertNotIn(WEBHOOK_PATH, err)

        transport = webhook_transport(500)
        code, _out, err = run_notify(["--in", str(SAMPLE)], {"HARNESS_NOTIFY_WEBHOOK_URL": WEBHOOK_URL}, transport)
        self.assertEqual(code, 2)
        self.assertIn("HTTP 500 hooks.example", err)
        self.assertNotIn(WEBHOOK_PATH, err)
        self.assertNotIn("/T/B/", err)
        self.assertEqual(len(transport.calls), 1)  # 재시도하지 않는다

        redirect = FakeTransport([("POST", r"hooks\.example/T/B/[0-9a-f]+",
                                   lambda call: notify.Response(302, {"Location": "https://other.example/x"}))])
        code, _out, err = run_notify(["--in", str(SAMPLE)], {"HARNESS_NOTIFY_WEBHOOK_URL": WEBHOOK_URL}, redirect)
        self.assertEqual(code, 2)
        self.assertIn("리다이렉트", err)
        self.assertIn("302", err)
        self.assertNotIn(WEBHOOK_PATH, err)
        self.assertNotIn("other.example", err)
        self.assertEqual(len(redirect.calls), 1)  # 다른 주소로 POST를 다시 보내지 않는다
        # 기본 전송도 리다이렉트를 따르지 않는다. 전송 예외 메시지에도 경로가 없다
        self.assertIsNone(notify._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://o.example/"))
        with self.assertRaises(notify.NotifyError) as caught:
            notify.https_url(http_url, "HARNESS_NOTIFY_WEBHOOK_URL")
        self.assertNotIn(WEBHOOK_PATH, str(caught.exception))

        class BrokenOpener:
            def open(self, request, timeout):
                raise OSError("connection refused to " + WEBHOOK_URL)

        with mock.patch.object(notify, "_OPENER", BrokenOpener()):
            with self.assertRaises(notify.NotifyError) as caught:
                notify.urllib_transport("POST", WEBHOOK_URL, {}, b"{}")
        self.assertIn("요청 실패(OSError): hooks.example", str(caught.exception))
        self.assertNotIn(WEBHOOK_PATH, str(caught.exception))


class MattermostTest(unittest.TestCase):
    def test_login_post_logout(self):
        """T5: login → teams/name → channels/name → posts → logout 순서, posts에 Bearer 세션 토큰·channel_id·message, 종료 0."""
        transport = mattermost_transport()
        code, out, err = run_notify(["--in", str(SAMPLE), "--report-url", REPORT_URL], MATTERMOST_ENV, transport)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "")
        self.assertEqual(transport.paths(), ["/api/v4/users/login", "/api/v4/teams/name/demo-team",
                                             "/api/v4/teams/t1/channels/name/metrics", "/api/v4/posts",
                                             "/api/v4/users/logout"])
        login, team, channel, post, logout = transport.calls
        self.assertEqual((login.method, login.payload), ("POST", {"login_id": "bot@example", "password": PASSWORD}))
        self.assertNotIn("Authorization", login.headers)
        self.assertEqual(login.headers["Content-Type"], "application/json")
        for call in (team, channel, post, logout):
            self.assertEqual(call.headers["Authorization"], f"Bearer {SESSION}")
        self.assertEqual((team.method, channel.method), ("GET", "GET"))
        self.assertEqual(post.method, "POST")
        self.assertEqual(post.payload, {"channel_id": "c1", "message": notify.build_message(sample(), REPORT_URL)})
        self.assertEqual((logout.method, logout.payload), ("POST", {}))
        self.assertIn("mm.example", err)
        self.assertIn("mattermost", err)
        self.assertNotIn(SESSION, err)
        self.assertNotIn(PASSWORD, err)
        # 채널 ID를 주면 팀·채널 조회를 건너뛴다. --mode mattermost는 webhook 변수가 있어도 Mattermost로 보낸다
        transport = mattermost_transport()
        env = {**MATTERMOST_ENV, "HARNESS_MATTERMOST_CHANNEL_ID": "c9", "HARNESS_NOTIFY_WEBHOOK_URL": WEBHOOK_URL}
        code, _out, err = run_notify(["--in", str(SAMPLE), "--mode", "mattermost"], env, transport)
        self.assertEqual(code, 0, err)
        self.assertEqual(transport.paths(), ["/api/v4/users/login", "/api/v4/posts", "/api/v4/users/logout"])
        self.assertEqual(transport.calls[1].payload["channel_id"], "c9")
        # 서버 주소 끝의 /는 떼고, 팀·채널 이름은 URL 인코딩한다
        transport = mattermost_transport()
        env = {**MATTERMOST_ENV, "HARNESS_MATTERMOST_URL": MATTERMOST_URL + "/", "HARNESS_MATTERMOST_TEAM": "a b/c"}
        transport.routes[1] = ("GET", r"mm\.example/api/v4/teams/name/a%20b%2Fc", lambda call: json_response(200, {"id": "t1"}))
        code, _out, err = run_notify(["--in", str(SAMPLE)], env, transport)
        self.assertEqual(code, 0, err)
        self.assertEqual(transport.calls[1].url, f"{MATTERMOST_URL}/api/v4/teams/name/a%20b%2Fc")

    def test_failure_paths_hide_secrets(self):
        """T6: login 401 / posts에 id 없음 → 종료 2, logout 실패 → 경고만·종료 0. 어디에도 비밀번호·세션 토큰 없음."""
        transport = mattermost_transport(login_status=401)
        code, _out, err = run_notify(["--in", str(SAMPLE)], MATTERMOST_ENV, transport)
        self.assertEqual(code, 2)
        self.assertIn("HTTP 401 mm.example", err)
        self.assertEqual(transport.paths(), ["/api/v4/users/login"])  # 토큰이 없으니 logout도 없다
        self.assertNotIn(PASSWORD, err)
        self.assertNotIn(SESSION, err)

        transport = mattermost_transport(post_data={"message": "ok"})
        code, _out, err = run_notify(["--in", str(SAMPLE)], MATTERMOST_ENV, transport)
        self.assertEqual(code, 2)
        self.assertIn("post ID", err)
        self.assertEqual(transport.paths()[-1], "/api/v4/users/logout")  # 게시 실패 뒤에도 로그아웃한다
        self.assertNotIn(PASSWORD, err)
        self.assertNotIn(SESSION, err)

        # 팀·채널 조회 응답에 id가 없으면 변수 이름만 안내하고 팀·채널 이름 값은 쓰지 않는다
        for index, name in ((1, "HARNESS_MATTERMOST_TEAM"), (2, "HARNESS_MATTERMOST_CHANNEL")):
            transport = mattermost_transport()
            method, pattern, _handler = transport.routes[index]
            transport.routes[index] = (method, pattern, lambda call, _name: json_response(200, {}))
            code, _out, err = run_notify(["--in", str(SAMPLE)], MATTERMOST_ENV, transport)
            self.assertEqual(code, 2)
            self.assertIn(name, err)
            self.assertNotIn(MATTERMOST_ENV["HARNESS_MATTERMOST_TEAM"], err)
            self.assertNotIn(MATTERMOST_ENV["HARNESS_MATTERMOST_CHANNEL"], err)
            self.assertEqual(transport.paths()[-1], "/api/v4/users/logout")

        transport = mattermost_transport(logout_status=500)
        code, out, err = run_notify(["--in", str(SAMPLE)], MATTERMOST_ENV, transport)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "")
        self.assertIn("경고", err)
        self.assertIn("로그아웃", err)
        self.assertIn("HTTP 500 mm.example", err)
        self.assertEqual(transport.paths()[-2:], ["/api/v4/posts", "/api/v4/users/logout"])
        self.assertNotIn(PASSWORD, err)
        self.assertNotIn(SESSION, err)
        # 로그인 응답에 Token 헤더가 없거나 응답이 JSON이 아니면 종료 2
        no_token = mattermost_transport()
        no_token.routes[0] = ("POST", r"mm\.example/api/v4/users/login", lambda call: json_response(200, {"id": "u1"}))
        code, _out, err = run_notify(["--in", str(SAMPLE)], MATTERMOST_ENV, no_token)
        self.assertEqual(code, 2)
        self.assertIn("세션 토큰", err)
        not_json = mattermost_transport()
        not_json.routes[3] = ("POST", r"mm\.example/api/v4/posts", lambda call: notify.Response(201, {}, b"<html>"))
        code, _out, err = run_notify(["--in", str(SAMPLE)], MATTERMOST_ENV, not_json)
        self.assertEqual(code, 2)
        self.assertIn("JSON", err)
        self.assertNotIn("<html>", err)
        # 설정 오류: 변수 이름만 메시지에 쓴다. http 주소는 거부한다
        for env, needle in (({k: v for k, v in MATTERMOST_ENV.items() if k != "HARNESS_MATTERMOST_PASSWORD"},
                             "HARNESS_MATTERMOST_PASSWORD"),
                            ({k: v for k, v in MATTERMOST_ENV.items() if k != "HARNESS_MATTERMOST_CHANNEL"},
                             "HARNESS_MATTERMOST_CHANNEL_ID"),
                            ({**MATTERMOST_ENV, "HARNESS_MATTERMOST_URL": "http://mm.example"}, "https")):
            with self.subTest(needle=needle):
                transport = mattermost_transport()
                code, _out, err = run_notify(["--in", str(SAMPLE)], env, transport)
                self.assertEqual(code, 2)
                self.assertIn(needle, err)
                self.assertEqual(transport.calls, [])
                self.assertNotIn(PASSWORD, err)
                self.assertNotIn("bot@example", err)


class SecretTest(unittest.TestCase):
    def test_dry_run_no_network(self):
        """T7: --dry-run(설정 변수 없음) → 본문을 표준 출력, 전송 호출 0, 종료 0."""
        transport = webhook_transport()
        code, out, err = run_notify(["--in", str(SAMPLE), "--dry-run", "--report-url", REPORT_URL], {}, transport)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, notify.build_message(sample(), REPORT_URL) + "\n")
        self.assertEqual(err, "")
        self.assertEqual(transport.calls, [])
        # 기본 전송(urllib)도 호출되지 않는다. --mode를 줘도 dry-run은 설정을 보지 않는다
        with mock.patch.object(notify, "TRANSPORT", side_effect=AssertionError("network")):
            code, out, _err = run_notify(["--in", str(SAMPLE), "--dry-run", "--mode", "mattermost"], {}, None)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("group/demo 주간 측정 요약 2026-W40 (진행 중)\n"))
        # --report-url은 http(s)만 받는다(본문에 그대로 들어간다)
        code, _out, err = run_notify(["--in", str(SAMPLE), "--dry-run", "--report-url", "javascript:alert(1)"], {}, None)
        self.assertEqual(code, 2)
        self.assertIn("--report-url", err)
        # 소스에 실제 값처럼 보이는 토큰·URL이 없다
        source = (METRICS_DIR / "notify.py").read_text(encoding="utf-8")
        self.assertNotRegex(source, r"https://hooks\.slack\.com/services/\S+")
        self.assertNotRegex(source, r"xox[abp]-")


class CliTest(unittest.TestCase):
    def clean_env(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in [key for key in os.environ if key.startswith("HARNESS_")]:
            os.environ.pop(name, None)

    def test_harness_metrics_notify(self):
        """T8: `harness metrics notify` 가짜 전송 → 0 / 설정 없음 → 2와 변수 이름 / --mode webhook URL 없음 → 2 /
        --mode mattermost 비밀번호 없음 → 2. 입력 파일 없음·version 2 → 2."""
        self.clean_env()
        transport = webhook_transport()
        err = io.StringIO()
        with mock.patch.object(harness.metrics_notify, "TRANSPORT", transport), \
                mock.patch.dict(os.environ, {"HARNESS_NOTIFY_WEBHOOK_URL": WEBHOOK_URL}), redirect_stderr(err):
            code = harness.main(["metrics", "notify", "--in", str(SAMPLE), "--report-url", REPORT_URL])
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0].payload, {"text": notify.build_message(sample(), REPORT_URL)})
        self.assertIn("hooks.example", err.getvalue())
        self.assertNotIn(WEBHOOK_PATH, err.getvalue())

        err = io.StringIO()
        with mock.patch.object(harness.metrics_notify, "TRANSPORT", webhook_transport()), redirect_stderr(err):
            code = harness.main(["metrics", "notify", "--in", str(SAMPLE)])
        self.assertEqual(code, 2)
        self.assertIn("오류: 주간 요약 발송 실패", err.getvalue())
        self.assertIn("HARNESS_NOTIFY_WEBHOOK_URL", err.getvalue())
        self.assertIn("HARNESS_MATTERMOST_URL", err.getvalue())

        err = io.StringIO()
        with mock.patch.object(harness.metrics_notify, "TRANSPORT", webhook_transport()), \
                mock.patch.dict(os.environ, MATTERMOST_ENV), redirect_stderr(err):
            code = harness.main(["metrics", "notify", "--in", str(SAMPLE), "--mode", "webhook"])
        self.assertEqual(code, 2)
        self.assertIn("HARNESS_NOTIFY_WEBHOOK_URL", err.getvalue())

        err = io.StringIO()
        env = {k: v for k, v in MATTERMOST_ENV.items() if k != "HARNESS_MATTERMOST_PASSWORD"}
        with mock.patch.object(harness.metrics_notify, "TRANSPORT", mattermost_transport()), \
                mock.patch.dict(os.environ, env), redirect_stderr(err):
            code = harness.main(["metrics", "notify", "--in", str(SAMPLE), "--mode", "mattermost"])
        self.assertEqual(code, 2)
        self.assertIn("HARNESS_MATTERMOST_PASSWORD", err.getvalue())

        with tempfile.TemporaryDirectory() as tmp:
            v2 = Path(tmp) / "v2.json"
            v2.write_text(json.dumps({"version": 2, "weeks": []}), encoding="utf-8")
            for source in (Path(tmp) / "missing.json", v2):
                with self.subTest(source=source.name):
                    transport = webhook_transport()
                    err = io.StringIO()
                    with mock.patch.object(harness.metrics_notify, "TRANSPORT", transport), \
                            mock.patch.dict(os.environ, {"HARNESS_NOTIFY_WEBHOOK_URL": WEBHOOK_URL}), redirect_stderr(err):
                        code = harness.main(["metrics", "notify", "--in", str(source)])
                    self.assertEqual(code, 2)
                    self.assertIn(str(source), err.getvalue())
                    self.assertEqual(transport.calls, [])  # 입력이 틀리면 보내지 않는다
        # --dry-run은 본문을 표준 출력에 쓴다
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = harness.main(["metrics", "notify", "--in", str(SAMPLE), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), notify.build_message(sample(), None) + "\n")
        self.assertIn("metrics notify", harness.__doc__)


class FragmentTest(unittest.TestCase):
    def test_metrics_yml_structure(self):
        """T9: 머리 주석 항목, job harness-metrics, digest 고정 이미지, schedule rule, set -eu, allow_failure 없음,
        collect·report·notify 호출, expire_in 90 days, expose_as, 리포트 링크."""
        head = header_of(FRAGMENT)
        for needle in ("include:", "- remote: https://", "core/ci/gitlab/metrics.yml", "변수", "필요 조건:", "예외:",
                       "0 0 * * 1", "Pipeline schedules", "Masked"):
            self.assertIn(needle, head, needle)
        jobs = jobs_of(FRAGMENT)
        self.assertEqual(list(jobs), ["harness-metrics"])
        body = jobs["harness-metrics"]
        images = re.findall(r"^\s+name:\s*(\S+)", body, re.M)
        self.assertEqual(len(images), 1)
        self.assertRegex(images[0], r"^python:[\w.-]+@sha256:[0-9a-f]{64}$")
        mr_lint = (ROOT / "core" / "ci" / "gitlab" / "mr-lint.yml").read_text(encoding="utf-8")
        self.assertIn(images[0], mr_lint)  # mr-lint.yml과 같은 이미지 digest
        rules = re.search(r"^  rules:\n((?:    .*\n)+)", body + "\n", re.M).group(1).splitlines()
        self.assertEqual(rules[0], '    - if: $CI_PIPELINE_SOURCE == "schedule"')
        self.assertEqual(rules[1:], ['    - if: $CI_PIPELINE_SOURCE == "web"', "      when: manual"])
        self.assertNotIn("merge_request_event", body)
        self.assertNotIn("allow_failure", body)
        self.assertIn("  interruptible: false", body)
        self.assertIn("  resource_group: harness-metrics", body)
        self.assertIn("  timeout: 15m", body)
        script = re.search(r"^  script:\n\s+- \|\n\s+(.+)", body, re.M)
        self.assertRegex(script.group(1), r"^set -eu")
        self.assertNotIn("set +e", body)
        for needle in ("python3 -I -B .harness/metrics/collect.py --platform gitlab", "--out metrics.json",
                       "python3 -I -B .harness/metrics/report.py --in metrics.json --out report.html",
                       "python3 -I -B .harness/metrics/notify.py --in metrics.json",
                       '--report-url "$CI_JOB_URL/artifacts/file/report.html"',
                       "HARNESS_METRICS_SINCE:-$(date -u -d '-8 weeks' +%F)", "HARNESS_METRICS_UTC_OFFSET:-+09:00",
                       '--title "$CI_PROJECT_PATH 측정 리포트"', ".harness/metrics/classify_ci.py",
                       'if [ -n "${HARNESS_NOTIFY_WEBHOOK_URL:-}" ] || [ -n "${HARNESS_MATTERMOST_URL:-}" ]'):
            self.assertIn(needle, body, needle)
        # notify는 발송 변수가 있을 때만 돌고, 없으면 건너뛴 사실을 로그로 남긴다
        self.assertRegex(body, r"else\n\s+echo \"harness: .*건너뛴다")
        # 스크립트는 소비자 복사본만 실행한다(키트 경로 없음)
        self.assertNotIn("core/metrics/", body)
        artifacts = re.search(r"^  artifacts:\n((?:    .*\n?)+)", body, re.M).group(1)
        self.assertIn("    when: always", artifacts)
        self.assertIn("    expire_in: 90 days", artifacts)
        self.assertIn("    expose_as: 'harness metrics report'", artifacts)
        self.assertIn("      - metrics.json", artifacts)
        self.assertIn("      - report.html", artifacts)
        # 변수 값에 Secret을 출력하는 echo가 없다
        for line in body.splitlines():
            if "echo" in line:
                self.assertNotRegex(line, r"\$\{?HARNESS_(METRICS_TOKEN|NOTIFY_WEBHOOK_URL|MATTERMOST_PASSWORD)", line)
        self.assertEqual(FRAGMENT.read_bytes().count(b"\r"), 0)

    def test_header_lists_every_variable(self):
        """T12: notify.py·collect.py가 읽는 HARNESS_* 변수가 모두 조각 머리 주석과 core/ci/README.md 표에 있다."""
        used = set()
        for name in ("notify.py", "collect.py"):
            used |= set(HARNESS_VARS.findall((METRICS_DIR / name).read_text(encoding="utf-8")))
        self.assertIn("HARNESS_METRICS_TOKEN", used)
        self.assertIn("HARNESS_NOTIFY_WEBHOOK_URL", used)
        self.assertIn("HARNESS_MATTERMOST_PASSWORD", used)
        self.assertEqual(len(used), 8, used)  # 수집 토큰 1 + webhook 1 + Mattermost 6
        head = header_of(FRAGMENT)
        header_vars = set(HARNESS_VARS.findall(head))
        readme = CI_README.read_text(encoding="utf-8")
        section = readme.split("## 측정 예약 파이프라인", 1)[1].split("\n## ", 1)[0]
        table_vars = {m for line in section.splitlines() if line.startswith("| `HARNESS_")
                      for m in HARNESS_VARS.findall(line.split("|")[1])}
        self.assertEqual(used - header_vars, set())
        self.assertEqual(used - table_vars, set())
        # 조각 스크립트만 읽는 변수(기간·시간대)도 머리 주석과 표에 있다. 머리 주석의 변수는 모두 표에 있다
        script_vars = set(HARNESS_VARS.findall(jobs_of(FRAGMENT)["harness-metrics"]))
        self.assertEqual(script_vars - header_vars, set())
        self.assertEqual(header_vars - table_vars, set(), header_vars - table_vars)
        # core/metrics/README.md 발송 절에도 notify 변수가 모두 있다
        metrics_readme = METRICS_README.read_text(encoding="utf-8")
        notify_vars = set(HARNESS_VARS.findall((METRICS_DIR / "notify.py").read_text(encoding="utf-8")))
        self.assertEqual(notify_vars - set(HARNESS_VARS.findall(metrics_readme)), set())
        self.assertNotIn("예정 내용", metrics_readme)


class ManifestTest(unittest.TestCase):
    def run_harness(self, argv):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = harness.main(argv)
        return code, out.getvalue()

    def test_metrics_scripts_copied(self):
        """T10: GitLab init → .harness/metrics/{collect,report,notify,classify_ci}.py가 core/metrics/와 바이트 동일,
        check 드리프트 없음. GitHub init → .harness/metrics/ 없음."""
        tmp = Path(tempfile.mkdtemp(prefix="harness-notify-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        gitlab = tmp / "gl"
        code, out = self.run_harness(["init", str(gitlab), "--platform", "gitlab", "--tracker", "jira",
                                      "--issue-prefix", "DEMO"])
        self.assertEqual(code, 0, out)
        for name in SCRIPTS:
            with self.subTest(name=name):
                copied = gitlab / ".harness" / "metrics" / name
                self.assertTrue(copied.is_file(), name)
                self.assertEqual(copied.read_bytes(), (METRICS_DIR / name).read_bytes())
                self.assertIn(f".harness/metrics/{name}", out)
        self.assertEqual(sorted(p.name for p in (gitlab / ".harness" / "metrics").iterdir()), sorted(SCRIPTS))
        code, out = self.run_harness(["check", str(gitlab)])
        self.assertEqual(code, 0, out)
        self.assertIn("드리프트 없음", out)
        # 복사본을 고치면 check가 불일치로, 지우면 없음으로 알린다(기존 소비자가 init --force로 받는 경로)
        target = gitlab / ".harness" / "metrics" / "notify.py"
        target.write_text(target.read_text(encoding="utf-8") + "\n# 변경\n", encoding="utf-8")
        code, out = self.run_harness(["check", str(gitlab)])
        self.assertEqual(code, 1)
        self.assertIn("불일치: .harness/metrics/notify.py", out)
        target.unlink()
        code, out = self.run_harness(["check", str(gitlab)])
        self.assertEqual(code, 1)
        self.assertIn("없음:   .harness/metrics/notify.py", out)
        code, out = self.run_harness(["init", str(gitlab), "--force"])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.run_harness(["check", str(gitlab)])[0], 0)
        # 복사본은 단독 실행된다(report.py를 같은 디렉터리에서 읽는다)
        copied_notify = load("copied_notify", gitlab / ".harness" / "metrics" / "notify.py")
        self.assertEqual(copied_notify.build_message(sample(), None), notify.build_message(sample(), None))
        github = tmp / "gh"
        code, out = self.run_harness(["init", str(github), "--platform", "github", "--tracker", "github"])
        self.assertEqual(code, 0, out)
        self.assertFalse((github / ".harness" / "metrics").exists())
        self.assertFalse((github / ".harness").exists())
        self.assertNotIn(".harness/metrics", out)
        # 매니페스트 항목: metrics base, gitlab 전용, 렌더하지 않음, 키트 자신에는 적용하지 않음
        entries = [e for e in harness.load_manifest() if e.get("base") == "metrics"]
        self.assertEqual(sorted(e["src"] for e in entries), sorted(SCRIPTS))
        for entry in entries:
            self.assertEqual((entry["platform"], entry["self"], entry["render"]), ("gitlab", False, False))
            self.assertEqual(entry["dest"], f".harness/metrics/{entry['src']}")
        self.assertEqual(harness.SOURCE_DIRS["metrics"], METRICS_DIR)


if __name__ == "__main__":
    unittest.main()
