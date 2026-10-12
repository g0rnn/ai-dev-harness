"""core/metrics/collect.py와 `harness metrics collect` 테스트(M4-1). 네트워크 없이 합성 fixture 응답만 쓴다."""

import http.client
import importlib.util
import io
import json
import os
import re
import secrets
import tempfile
import unittest
import urllib.parse
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "metrics"
COMMON = ROOT / "core" / "hooks" / "harness_common.py"
CONFIG = FIXTURES / "harness.json"

SPEC = importlib.util.spec_from_file_location("metrics_collect", ROOT / "core" / "metrics" / "collect.py")
collect = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collect)
MR_LINT_SPEC = importlib.util.spec_from_file_location("mr_lint_for_collect", ROOT / "core" / "ci" / "mr-lint" / "mr_lint.py")
mr_lint = importlib.util.module_from_spec(MR_LINT_SPEC)
MR_LINT_SPEC.loader.exec_module(mr_lint)
HARNESS_SPEC = importlib.util.spec_from_file_location("harness_for_metrics", ROOT / "bin" / "harness.py")
harness = importlib.util.module_from_spec(HARNESS_SPEC)
HARNESS_SPEC.loader.exec_module(harness)

# 실제 토큰 형태(GitLab·GitHub PAT 접두사)를 쓰면 gitleaks(secret-detection 필수 체크)가 잡는다. 값은 모듈 로드 때 난수로 만든다
TOKEN = "TESTONLY-" + secrets.token_hex(12)
GITLAB_API = "https://gitlab.example/api/v4"
GITHUB_API = "https://api.github.example"
WINDOW = ["--since", "2026-09-21", "--until", "2026-10-05"]  # 2026-W39, W40 (KST)


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def json_response(data, headers=None):
    return collect.Response(200, {"Content-Type": "application/json", **(headers or {})},
                            json.dumps(data).encode("utf-8"))


def text_response(text):
    return collect.Response(200, {"Content-Type": "text/plain"}, text.encode("utf-8"))


class FakeApi:
    """전송 함수 대역. (호스트+경로 정규식, 처리 함수) 목록으로 응답하고 요청을 기록한다."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, headers, tail=None):
        self.calls.append(SimpleNamespace(method=method, url=url, headers=dict(headers), tail=tail))
        parts = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parts.query)
        for pattern, handler in self.routes:
            match = re.fullmatch(pattern, parts.netloc + parts.path)
            if match:
                return handler(query, *match.groups())
        return collect.Response(404)

    def paths(self):
        return [urllib.parse.urlsplit(call.url).path for call in self.calls]


def gitlab_api(data=None):
    data = data or fixture("gitlab.json")
    base = r"gitlab\.example/api/v4/projects/1"

    def mr_list(query):
        if "labels" in query:
            return json_response(data["labeled_mrs"])
        items = data["merge_requests"]
        if query.get("page") == ["2"]:
            return json_response(items[2:])
        # 첫 페이지는 둘만 주고 Link로 다음 페이지를 알린다
        next_url = f"{GITLAB_API}/projects/1/merge_requests?state=merged&page=2"
        return json_response(items[:2], {"Link": f'<{next_url}>; rel="next", <{next_url}>; rel="last"'})

    def single(query, number):
        item = data["mr"].get(number)
        return json_response(item) if item else collect.Response(404)

    return FakeApi([
        (base + r"/merge_requests", mr_list),
        (base + r"/merge_requests/(\d+)", single),
        (base + r"/merge_requests/(\d+)/diffs", lambda q, n: json_response(data["diffs"].get(n, []))),
        (base + r"/issues", lambda q: json_response(data["issues"])),
        (base + r"/pipelines", lambda q: json_response(data["pipelines"])),
        (base + r"/pipelines/(\d+)/jobs", lambda q, n: json_response(data["jobs"].get(n, []))),
        (base + r"/jobs/(\d+)/trace",
         lambda q, n: text_response(data["traces"][n]) if n in data["traces"] else collect.Response(404)),
    ])


def github_api():
    data = fixture("github.json")
    base = r"api\.github\.example/repos/owner/demo"
    return FakeApi([
        (base + r"/pulls", lambda q: json_response(data["pulls"])),
        (base + r"/pulls/(\d+)", lambda q, n: collect.Response(404)),
        (base + r"/pulls/(\d+)/files", lambda q, n: json_response(data["files"].get(n, []))),
        (base + r"/issues", lambda q: json_response(data["issues"])),
        (base + r"/actions/runs", lambda q: json_response({"total_count": 2, "workflow_runs": data["runs"]})),
        (base + r"/actions/runs/(\d+)/jobs", lambda q, n: json_response({"jobs": data["jobs"].get(n, [])})),
        # 로그는 GitHub처럼 다른 호스트(저장소 서버)로 302 리다이렉트한다
        (base + r"/actions/jobs/(\d+)/logs",
         lambda q, n: collect.Response(302, {"Location": f"https://logs.example.net/job/{n}?sig=signed"})),
        (r"logs\.example\.net/job/(\d+)", lambda q, n: text_response(data["logs"][n])),
    ])


def argv(platform, out, *extra):
    api, project = (GITLAB_API, "1") if platform == "gitlab" else (GITHUB_API, "owner/demo")
    return ["--platform", platform, *WINDOW, "--out", str(out), "--api-url", api, "--project", project,
            "--common", str(COMMON), "--config", str(CONFIG), *extra]


def run_collect(platform, api, *extra, env=None):
    """collect.main을 실행하고 (종료 코드, 출력 dict 또는 None, stderr)를 돌려준다."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "metrics.json"
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = collect.main(argv(platform, out, *extra), env={"HARNESS_METRICS_TOKEN": TOKEN} if env is None
                                else env, transport=api)
        text = out.read_text(encoding="utf-8") if out.exists() else None
    return code, (json.loads(text) if text else None), text, stderr.getvalue()


def week(result, name):
    return next(entry for entry in result["weeks"] if entry["week"] == name)


def judge():
    lint = collect.load_mr_lint(None)
    common = lint.load_common(COMMON)
    return collect.Judge(lint, common, collect.load_judge_config(CONFIG, common))


class GitLabCollectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = gitlab_api()
        cls.code, cls.result, cls.text, cls.stderr = run_collect("gitlab", cls.api)

    def test_weeks(self):
        """T1: 2주, 병합 MR 3개(표준·엄격·판정 없음), 실패 job 2개. 빈 주도 0으로 나온다."""
        self.assertEqual(self.code, 0, self.stderr)
        self.assertEqual(self.result["version"], 1)
        self.assertEqual([w["week"] for w in self.result["weeks"]], ["2026-W39", "2026-W40"])
        self.assertEqual([w["start"] for w in self.result["weeks"]], ["2026-09-21", "2026-09-28"])
        first, second = week(self.result, "2026-W39"), week(self.result, "2026-W40")
        self.assertEqual((first["merged_mrs"], first["strict"], first["mismatch"]), (3, 1, 1))
        self.assertEqual((second["merged_mrs"], second["strict"], second["mismatch"]), (0, 0, 0))
        self.assertEqual(first["ci_failures"], {"test": 1, "format": 0, "infra": 0, "timeout": 0, "security": 0,
                                                "unclassified": 0})
        self.assertEqual(second["ci_failures"], {"test": 0, "format": 1, "infra": 0, "timeout": 0, "security": 0,
                                                 "unclassified": 0})
        self.assertEqual(set(first), {"week", "start", "merged_mrs", "strict", "mismatch", "escaped_defects",
                                      "unlinked_defects", "ci_failures"})
        # 기간 끝 이후 job(503)과 기간 전 병합 MR(9)은 빠진다
        self.assertEqual(sorted(job["id"] for job in self.result["jobs"]), [501, 502])
        self.assertNotIn(9, [mr["number"] for mr in self.result["mrs"]])
        self.assertEqual(set(self.result), {"version", "platform", "project", "window", "generated_at", "weeks",
                                            "mrs", "defects", "jobs"})
        self.assertEqual(self.result["window"], {"since": "2026-09-21T00:00:00+09:00",
                                                 "until": "2026-10-05T00:00:00+09:00", "utc_offset": "+09:00"})

    def test_mr_records(self):
        """원자료: 옛·새 경로 모두 재판정, 높은 쪽 기록, 본문 원문은 넣지 않는다."""
        mrs = {mr["number"]: mr for mr in self.result["mrs"]}
        self.assertEqual(mrs[11]["tier"], {"body": "lite", "judge": "strict", "effective": "strict",
                                           "mismatch": True})
        self.assertEqual(mrs[11]["mismatch_higher"], "judge")
        self.assertEqual(mrs[11]["files"], 3)  # docs/guide.md, secure/token.py(옛 경로), src/token.py
        self.assertEqual(sorted(mrs[11]["paths"]), ["docs/guide.md", "secure/token.py", "src/token.py"])
        self.assertNotIn("paths", mrs[13])  # 통합 MR은 변경 파일을 읽지 않는다
        self.assertEqual(mrs[10]["tier"]["effective"], "standard")
        self.assertNotIn("body", mrs[10])
        self.assertNotIn("문서만", self.text)  # 본문 원문이 출력에 없다
        self.assertNotIn("AssertionError", self.text)  # 로그 원문도 없다

    def test_integration_mr_not_counted(self):
        """통합 MR(develop → main)은 원자료에만 남고 지표에 세지 않는다(변경 파일도 읽지 않는다)."""
        mrs = {mr["number"]: mr for mr in self.result["mrs"]}
        self.assertTrue(mrs[13]["integration"])
        self.assertIsNone(mrs[13]["tier"])
        self.assertNotIn("/api/v4/projects/1/merge_requests/13/diffs", self.api.paths())

    def test_pagination_followed(self):
        self.assertEqual(sum(1 for p in self.api.paths() if p.endswith("/merge_requests")), 2 + 1)  # 2쪽 + 라벨 MR

    def test_token_only_in_header(self):
        for call in self.api.calls:
            self.assertEqual(call.headers.get("PRIVATE-TOKEN"), TOKEN)
            self.assertNotIn(TOKEN, call.url)
        self.assertNotIn(TOKEN, self.text)
        self.assertNotIn(TOKEN, self.stderr)

    def queries(self, suffix):
        found = [urllib.parse.parse_qs(urllib.parse.urlsplit(c.url).query) for c in self.api.calls
                 if urllib.parse.urlsplit(c.url).path.endswith(suffix)]
        self.assertTrue(found, suffix)
        return found

    def test_queries_match_readme(self):
        """쿼리가 README와 같다. scope=all이 빠지면 GitLab 기본값(created_by_me)으로 조용히 과소 집계되고,
        파이프라인에 updated_before를 두면 기간 뒤에 끝난 파이프라인의 기간 내 실패 job이 빠진다(리뷰 F3·F4)."""
        mrs = self.queries("/projects/1/merge_requests")
        self.assertEqual(mrs[0], {"state": ["merged"], "scope": ["all"], "updated_after": ["2026-09-21T00:00:00+09:00"],
                                  "order_by": ["updated_at"], "sort": ["asc"], "per_page": ["100"]})
        labeled = [q for q in mrs if "labels" in q]
        self.assertEqual(labeled, [{"labels": ["escaped-defect"], "scope": ["all"],
                                    "created_after": ["2026-09-21T00:00:00+09:00"], "per_page": ["100"]}])
        self.assertEqual(self.queries("/projects/1/issues"), labeled)
        pipelines = self.queries("/projects/1/pipelines")
        self.assertEqual(pipelines, [{"updated_after": ["2026-09-21T00:00:00+09:00"], "per_page": ["100"]}])
        self.assertNotIn("updated_before", pipelines[0])
        jobs = self.queries("/pipelines/100/jobs")[0]
        self.assertEqual((jobs["scope[]"], jobs["include_retried"], jobs["per_page"]), (["failed"], ["true"], ["100"]))
        self.assertEqual(self.queries("/merge_requests/11/diffs"), [{"per_page": ["100"]}])


class TierTest(unittest.TestCase):
    def test_effective_and_mismatch(self):
        """T2: 본문 경량 + 재판정 엄격 → 유효 엄격, 불일치. lint_body와 같은 결과."""
        body = "## 판정\n\n- 경량 — 근거\n"
        tier = judge().tier(body, ["secure/token.py"])
        self.assertEqual(tier, {"body": "lite", "judge": "strict", "effective": "strict", "mismatch": True})
        self.assertEqual(tier, mr_lint.lint_body(body, "strict")["tier"])
        self.assertEqual(collect.higher_side(tier), "judge")

    def test_missing_tier(self):
        """T3: `## 판정` 없음 → body null, 유효 판정은 재판정 값, 불일치 아님."""
        for body in ("본문만 있다", None):
            tier = judge().tier(body, ["src/app.py"])
            self.assertEqual(tier, {"body": None, "judge": "standard", "effective": "standard", "mismatch": False})
            self.assertIsNone(collect.higher_side(tier))


class DefectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = gitlab_api()
        _code, cls.result, _text, _stderr = run_collect("gitlab", cls.api)
        cls.defects = {d["number"]: d for d in cls.result["defects"]}

    def test_cause_week(self):
        """T4: `원인: !12`, MR 12는 1주차 병합 → 1주차 escaped_defects 1."""
        self.assertEqual(week(self.result, "2026-W39")["escaped_defects"], 1)
        self.assertEqual(week(self.result, "2026-W40")["escaped_defects"], 0)
        self.assertEqual(self.defects[40]["status"], "linked")
        self.assertEqual((self.defects[40]["cause"], self.defects[40]["week"]), (12, "2026-W39"))

    def test_unlinked_and_out_of_window(self):
        """T5: 원인 없는 이슈(코드 블록 안 표기는 무시) → 생성 주 unlinked, 기간 밖 MR 원인은 제외."""
        self.assertEqual(week(self.result, "2026-W40")["unlinked_defects"], 1)
        self.assertEqual(week(self.result, "2026-W39")["unlinked_defects"], 0)
        self.assertEqual((self.defects[41]["status"], self.defects[41]["week"]), ("unlinked", "2026-W40"))
        self.assertIsNone(self.defects[41]["cause"])
        self.assertEqual(self.defects[42]["status"], "out_of_window")
        self.assertIsNone(self.defects[42]["week"])
        self.assertEqual(sum(w["escaped_defects"] for w in self.result["weeks"]), 1)
        # 기간 밖 원인 MR은 목록에 없어 단건 조회한다
        self.assertIn("/api/v4/projects/1/merge_requests/3", self.api.paths())

    def test_cause_pattern_per_platform(self):
        j = judge()
        self.assertEqual(j.cause("gitlab", "원인: !12에서 빠짐"), 12)
        self.assertEqual(j.cause("github", "원인 ： #7"), 7)
        self.assertIsNone(j.cause("gitlab", "원인: #12"))
        self.assertIsNone(j.cause("gitlab", "<!-- 원인: !12 -->"))

    def test_labeled_mr_without_merged_cause(self):
        """라벨 붙은 수정 MR도 결함으로 읽는다. 원인 MR이 없으면(404) cause_not_merged로 남기고 어느 주에도 세지 않는다."""
        record = self.defects[14]
        self.assertEqual((record["kind"], record["cause"], record["status"]), ("mr", 99, "cause_not_merged"))
        self.assertIsNone(record["week"])
        self.assertIsNone(record["cause_merged_at"])
        self.assertIn("/api/v4/projects/1/merge_requests/99", self.api.paths())
        self.assertEqual(sum(w["escaped_defects"] + w["unlinked_defects"] for w in self.result["weeks"]), 2)

    def test_defect_created_after_until_not_counted(self):
        """Codex P2(PR #66): --until 뒤에 만든 결함은 원인 MR(11, W39)이 기간 안이어도 세지 않고 out_of_window로만 남긴다.
        같은 창을 다시 수집해도 결과가 같아야 한다(T4의 W39 escaped_defects 1이 이를 고정한다)."""
        record = self.defects[43]
        self.assertEqual((record["cause"], record["status"], record["week"]), (11, "out_of_window", None))
        self.assertIsNone(record["cause_merged_at"])
        self.assertEqual(week(self.result, "2026-W39")["escaped_defects"], 1)


class GitHubCollectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = github_api()
        cls.code, cls.result, cls.text, cls.stderr = run_collect("github", cls.api, "--security-jobs", "sast, codeql")

    def test_jobs_conversion(self):
        """T6: failure·timed_out·success job, --security-jobs sast → 실패만, timeout과 security.
        취소된 run(902)의 실패 job과 다시 실행한 run(903, 결론 성공)의 앞 시도 실패 job도 센다(리뷰 F3)."""
        self.assertEqual(self.code, 0, self.stderr)
        jobs = {job["id"]: job for job in self.result["jobs"]}
        self.assertEqual(sorted(jobs), [1, 2, 4, 5])
        self.assertEqual((jobs[1]["name"], jobs[1]["category"]), ("harness-sast", "security"))
        self.assertEqual((jobs[2]["failure_reason"], jobs[2]["category"]), ("job_execution_timeout", "timeout"))
        self.assertEqual((jobs[4]["pipeline"], jobs[4]["category"]), (902, "test"))
        self.assertEqual((jobs[5]["pipeline"], jobs[5]["category"], jobs[5]["week"]), (903, "infra", "2026-W40"))
        self.assertEqual(week(self.result, "2026-W40")["ci_failures"],
                         {"test": 1, "format": 0, "infra": 1, "timeout": 1, "security": 1, "unclassified": 0})
        self.assertNotIn("/repos/owner/demo/actions/runs/901/jobs", self.api.paths())  # 성공 run은 job을 읽지 않는다

    def test_convert_matches_readme_adapter(self):
        convert = collect.convert_github_job
        self.assertEqual(convert({"id": 4, "name": "lint", "conclusion": "cancelled", "status": "completed"}, set()),
                         {"id": 4, "name": "lint", "stage": None, "failure_reason": None, "status": "cancelled"})
        self.assertEqual(convert({"id": 5, "name": "e2e", "conclusion": None, "status": "in_progress"}, set())["status"],
                         "in_progress")

    def test_prs_defects_and_log_redirect(self):
        self.assertEqual([mr["number"] for mr in self.result["mrs"]], [7])
        self.assertEqual(self.result["mrs"][0]["files"], 3)
        self.assertEqual(week(self.result, "2026-W40")["merged_mrs"], 1)
        self.assertEqual(week(self.result, "2026-W40")["escaped_defects"], 1)
        defects = {d["number"]: d for d in self.result["defects"]}
        self.assertEqual((defects[20]["kind"], defects[20]["status"], defects[20]["week"]), ("issue", "linked", "2026-W40"))
        # since는 갱신 시각 기준이라 기간 전에 만든 이슈(19)도 목록에 나오지만 생성 시각으로 뺀다
        self.assertNotIn(19, defects)
        # 라벨 붙은 PR은 kind mr. 원인 PR이 없으면(pulls/99 → 404) cause_not_merged
        self.assertEqual((defects[8]["kind"], defects[8]["cause"], defects[8]["status"]), ("mr", 99, "cause_not_merged"))
        log_calls = [c for c in self.api.calls if c.url.startswith("https://logs.example.net/")]
        self.assertEqual(len(log_calls), 4)
        for call in log_calls:
            self.assertNotIn("Authorization", call.headers)
        api_calls = [c for c in self.api.calls if c.url.startswith(GITHUB_API)]
        self.assertTrue(all(c.headers["Authorization"] == f"Bearer {TOKEN}" for c in api_calls))

    def test_github_token_fallback(self):
        code, result, _text, stderr = run_collect("github", github_api(), env={"GITHUB_TOKEN": TOKEN})
        self.assertEqual(code, 0, stderr)
        self.assertEqual(result["platform"], "github")

    def test_queries_match_readme(self):
        """PR은 갱신 내림차순으로 받아 기간 전 항목에서 멈추고, 이슈는 since(UTC)+생성 시각, run은 created 범위(UTC)로 받는다."""
        queries = {}
        for call in self.api.calls:
            parts = urllib.parse.urlsplit(call.url)
            if parts.path.endswith(("/pulls", "/issues", "/actions/runs")):
                queries.setdefault(parts.path.rsplit("/", 1)[-1], urllib.parse.parse_qs(parts.query))
        self.assertEqual(queries["pulls"], {"state": ["closed"], "sort": ["updated"], "direction": ["desc"],
                                            "per_page": ["100"]})
        self.assertEqual(queries["issues"], {"labels": ["escaped-defect"], "state": ["all"],
                                             "since": ["2026-09-20T15:00:00Z"], "per_page": ["100"]})
        self.assertEqual(queries["runs"], {"created": ["2026-09-20T15:00:00Z..2026-10-04T14:59:59Z"], "per_page": ["100"]})
        run_jobs = sorted(c.url.rsplit("/", 2)[1] for c in self.api.calls if c.url.endswith("/jobs?filter=all&per_page=100"))
        self.assertEqual(run_jobs, ["900", "902", "903"])  # 실패·취소·재실행 run만. 성공 run(901)은 읽지 않는다


class HttpTest(unittest.TestCase):
    def client(self, api, auth=None):
        return collect.ApiClient("https://api.example/v1", auth or {"PRIVATE-TOKEN": TOKEN}, {}, api)

    def test_pagination(self):
        """T7: `Link: rel="next"` 2페이지 → 두 페이지 항목 모두."""
        def items(query):
            if query.get("page") == ["2"]:
                return json_response([{"id": 3}])
            return json_response([{"id": 1}, {"id": 2}],
                                 {"Link": '<https://api.example/v1/items?page=2&per_page=2>; rel="next"'})

        api = FakeApi([(r"api\.example/v1/items", items)])
        self.assertEqual([i["id"] for i in self.client(api).get_list("https://api.example/v1/items?per_page=2")],
                         [1, 2, 3])
        wrapped = FakeApi([(r"api\.example/v1/runs", lambda q: json_response({"workflow_runs": [{"id": 9}]}))])
        self.assertEqual(self.client(wrapped).get_list("https://api.example/v1/runs", key="workflow_runs"), [{"id": 9}])
        self.assertEqual(collect.next_link('<https://a/x?page=3>; rel="last", <https://a/x?page=2>; rel="next"'),
                         "https://a/x?page=2")
        self.assertIsNone(collect.next_link('<https://a/x?page=1>; rel="prev"'))

    def test_cross_host_redirect_drops_auth(self):
        """T8: 302로 다른 호스트 → 두 번째 요청에 인증 헤더 없음. 같은 호스트면 유지."""
        api = FakeApi([
            (r"api\.example/v1/far", lambda q: collect.Response(302, {"Location": "https://other.example/blob"})),
            (r"api\.example/v1/near", lambda q: collect.Response(302, {"Location": "/v1/moved"})),
            (r"api\.example/v1/plain", lambda q: collect.Response(301, {"Location": "http://api.example/v1/moved"})),
            (r"api\.example/v1/moved", lambda q: text_response("ok")),
            (r"other\.example/blob", lambda q: collect.Response(302, {"Location": "https://api.example/v1/moved"})),
        ])
        for auth, name in (({"PRIVATE-TOKEN": TOKEN}, "PRIVATE-TOKEN"), ({"Authorization": f"Bearer {TOKEN}"},
                                                                          "Authorization")):
            with self.subTest(header=name):
                client = self.client(api, auth)
                api.calls.clear()
                self.assertEqual(client.request("https://api.example/v1/far").body, b"ok")
                self.assertEqual([c.url for c in api.calls], ["https://api.example/v1/far", "https://other.example/blob",
                                                              "https://api.example/v1/moved"])
                self.assertIn(name, api.calls[0].headers)
                # 다른 호스트로 넘어간 뒤에는 원래 호스트로 돌아와도 붙이지 않는다
                self.assertNotIn(name, api.calls[1].headers)
                self.assertNotIn(name, api.calls[2].headers)
                api.calls.clear()
                client.request("https://api.example/v1/near")
                self.assertEqual([c.url for c in api.calls][-1], "https://api.example/v1/moved")
                self.assertTrue(all(name in c.headers for c in api.calls))
                api.calls.clear()
                client.request("https://api.example/v1/plain")  # https → http 하향도 다른 origin
                self.assertNotIn(name, api.calls[1].headers)

    def test_redirect_loop_and_link_to_other_host(self):
        loop = FakeApi([(r"api\.example/v1/loop", lambda q: collect.Response(302, {"Location": "/v1/loop"}))])
        with self.assertRaises(collect.CollectError):
            self.client(loop).request("https://api.example/v1/loop")
        other = FakeApi([
            (r"api\.example/v1/items", lambda q: json_response([{"id": 1}], {"Link": '<https://other.example/p2>; rel="next"'})),
            (r"other\.example/p2", lambda q: json_response([{"id": 2}])),
        ])
        self.assertEqual(len(self.client(other).get_list("https://api.example/v1/items")), 2)
        self.assertNotIn("PRIVATE-TOKEN", other.calls[1].headers)

    def test_default_transport_does_not_follow_redirects(self):
        handler = collect._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://other.example/"))

    def test_transport_wraps_http_exceptions(self):
        """리뷰 F2: 전송·읽기 중 http.client 예외(IncompleteRead 등)도 CollectError(종료 2)다. 메시지에는 경로만 남는다."""
        class BrokenOpener:
            def open(self, request, timeout):
                raise http.client.IncompleteRead(b"partial")

        class PartialResponse:
            status, headers = 200, {}

            def read(self, size=-1):
                raise http.client.IncompleteRead(b"partial")

            def close(self):
                pass

        class PartialOpener:
            def open(self, request, timeout):
                return PartialResponse()

        url = f"https://api.example/v1/items?private_token={TOKEN}"
        for opener, text in ((BrokenOpener(), "API 요청 실패(IncompleteRead)"),
                             (PartialOpener(), "API 응답을 읽을 수 없다(IncompleteRead)")):
            with self.subTest(text=text), mock.patch.object(collect, "_OPENER", opener):
                with self.assertRaises(collect.CollectError) as caught:
                    collect.urllib_transport("GET", url, {})
                self.assertIn(text, str(caught.exception))
                self.assertNotIn(TOKEN, str(caught.exception))
                self.assertNotIn("api.example", str(caught.exception))

    def test_non_json_or_non_list_response(self):
        api = FakeApi([
            (r"api\.example/v1/text", lambda q: text_response("<html>")),
            (r"api\.example/v1/object", lambda q: json_response({"id": 1})),
        ])
        with self.assertRaises(collect.CollectError) as caught:
            self.client(api).get_json("https://api.example/v1/text")
        self.assertIn("JSON이 아니다", str(caught.exception))
        with self.assertRaises(collect.CollectError) as caught:
            self.client(api).get_list("https://api.example/v1/object")
        self.assertIn("목록이 아니다", str(caught.exception))
        with self.assertRaises(collect.CollectError):
            self.client(api).get_list("https://api.example/v1/text")


class SecretTest(unittest.TestCase):
    def test_token_not_in_errors_or_output(self):
        """T9: 401 응답 → 예외 메시지·stderr·출력 파일에 토큰 없음."""
        denied = FakeApi([(r".*", lambda q, *groups: collect.Response(401, {}, f"bad token {TOKEN}".encode()))])
        code, result, text, stderr = run_collect("gitlab", denied)
        self.assertEqual(code, 2)
        self.assertIsNone(result)  # 실패하면 출력 파일을 쓰지 않는다
        self.assertIn("HTTP 401", stderr)
        self.assertNotIn(TOKEN, stderr)
        client = collect.ApiClient(GITLAB_API, {"PRIVATE-TOKEN": TOKEN}, {}, denied)
        with self.assertRaises(collect.HttpStatusError) as caught:
            client.get_json(f"{GITLAB_API}/projects/1/merge_requests?private_token={TOKEN}")
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertIn("/api/v4/projects/1/merge_requests", str(caught.exception))

    def test_existing_output_kept_on_failure(self):
        denied = FakeApi([(r".*", lambda q, *groups: collect.Response(500))])
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "metrics.json"
            out.write_text("previous", encoding="utf-8")
            with redirect_stderr(io.StringIO()):
                code = collect.main(argv("gitlab", out), env={"HARNESS_METRICS_TOKEN": TOKEN}, transport=denied)
            self.assertEqual(code, 2)
            self.assertEqual(out.read_text(encoding="utf-8"), "previous")
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["metrics.json"])  # 임시 파일이 남지 않는다

    def test_replace_failure_keeps_existing_and_removes_temp(self):
        """임시 파일 교체(os.replace)가 실패해도 기존 출력 파일을 남기고 임시 파일을 지운다(종료 2)."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "metrics.json"
            out.write_text("previous", encoding="utf-8")
            stderr = io.StringIO()
            with mock.patch.object(collect.os, "replace", side_effect=OSError("busy")), redirect_stderr(stderr):
                code = collect.main(argv("gitlab", out), env={"HARNESS_METRICS_TOKEN": TOKEN}, transport=gitlab_api())
            self.assertEqual(code, 2)
            self.assertIn("출력 파일을 쓸 수 없다", stderr.getvalue())
            self.assertEqual(out.read_text(encoding="utf-8"), "previous")
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["metrics.json"])

    def test_http_api_url_rejected(self):
        args = SimpleNamespace(platform="gitlab", api_url="http://gitlab.example/api/v4", project="1")
        with self.assertRaises(collect.CollectError) as caught:
            collect.resolve_target(args, {"HARNESS_METRICS_TOKEN": TOKEN})
        self.assertIn("https", str(caught.exception))
        self.assertNotIn(TOKEN, str(caught.exception))


class TimeTest(unittest.TestCase):
    def test_iso_week_offset(self):
        """T10: 2026-10-04T16:00:00Z(KST 10-05 월) → 2026-W41, +00:00이면 2026-W40."""
        moment = collect.parse_ts("2026-10-04T16:00:00Z")
        self.assertEqual(collect.iso_week(moment, collect.parse_offset("+09:00")), "2026-W41")
        self.assertEqual(collect.iso_week(moment, collect.parse_offset("+00:00")), "2026-W40")
        self.assertEqual(collect.parse_ts("2026-10-04T16:00:00.123+09:00").isoformat(),
                         "2026-10-04T16:00:00.123000+09:00")
        self.assertIsNone(collect.parse_ts("어제"))

    def test_window_weeks_and_inputs(self):
        tz = collect.parse_offset("+09:00")
        since = collect.parse_date("2026-09-30", tz, "--since")  # 수요일
        until = collect.parse_date("2026-10-13", tz, "--until")  # 화요일(제외)
        self.assertEqual([w["week"] for w in collect.window_weeks(since, until, tz)],
                         ["2026-W40", "2026-W41", "2026-W42"])
        until_monday = collect.parse_date("2026-10-12", tz, "--until")
        self.assertEqual(len(collect.window_weeks(since, until_monday, tz)), 2)
        for bad in ("+9:00", "0900", "+15:00", "+09:60"):
            with self.assertRaises(collect.CollectError):
                collect.parse_offset(bad)
        with self.assertRaises(collect.CollectError):
            collect.parse_date("2026/09/30", tz, "--since")
        code, _result, _text, stderr = run_collect("gitlab", gitlab_api(), "--until", "2026-09-21")
        self.assertEqual(code, 2)
        self.assertIn("--until", stderr)


class CliTest(unittest.TestCase):
    def test_harness_metrics_collect(self):
        """T11: `harness metrics collect` 가짜 클라이언트 주입 → version 1, 토큰 없으면 종료 2와 안내."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "nested" / "metrics.json"
            stderr = io.StringIO()
            with mock.patch.object(harness.metrics_collect, "TRANSPORT", gitlab_api()), \
                    mock.patch.dict(os.environ, {"HARNESS_METRICS_TOKEN": TOKEN}), redirect_stderr(stderr):
                code = harness.main(["metrics", "collect", *argv("gitlab", out)])
            self.assertEqual(code, 0, stderr.getvalue())
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(data["version"], 1)
            self.assertEqual(data["platform"], "gitlab")
            self.assertNotIn(TOKEN, out.read_text(encoding="utf-8"))

            missing = Path(tmp) / "missing.json"
            stderr = io.StringIO()
            with mock.patch.object(harness.metrics_collect, "TRANSPORT", gitlab_api()), \
                    mock.patch.dict(os.environ), redirect_stderr(stderr):
                os.environ.pop("HARNESS_METRICS_TOKEN", None)
                code = harness.main(["metrics", "collect", *argv("gitlab", missing)])
            self.assertEqual(code, 2)
            self.assertIn("HARNESS_METRICS_TOKEN", stderr.getvalue())
            self.assertFalse(missing.exists())

    def test_github_without_any_token(self):
        code, _result, _text, stderr = run_collect("github", github_api(), env={})
        self.assertEqual(code, 2)
        self.assertIn("GITHUB_TOKEN", stderr)

    def test_env_defaults(self):
        env = {"HARNESS_METRICS_TOKEN": TOKEN, "CI_API_V4_URL": GITLAB_API, "CI_PROJECT_ID": "1"}
        args = SimpleNamespace(platform="gitlab", api_url=None, project=None)
        self.assertEqual(collect.resolve_target(args, env), (GITLAB_API, "1", {"PRIVATE-TOKEN": TOKEN}))
        args = SimpleNamespace(platform="github", api_url=None, project=None)
        self.assertEqual(collect.resolve_target(args, {"GITHUB_TOKEN": TOKEN, "GITHUB_REPOSITORY": "owner/demo"}),
                         ("https://api.github.com", "owner/demo", {"Authorization": f"Bearer {TOKEN}"}))


class TraceTest(unittest.TestCase):
    def test_trace_tail(self):
        """T12: 2 MiB trace → 끝 1 MiB만 분류에 쓰인다."""
        head = "There were failing tests. See the report.\n"
        tail_line = "Could not resolve host: registry.example\n"
        size = 2 * collect.TRACE_TAIL_BYTES
        trace = head + "x" * (size - len(head) - len(tail_line) - 1) + "\n" + tail_line
        self.assertEqual(len(trace.encode("utf-8")), size)
        api = FakeApi([(r"gitlab\.example/api/v4/projects/1/jobs/(\d+)/trace", lambda q, n: text_response(trace))])
        adapter = collect.GitLabAdapter(collect.ApiClient(GITLAB_API, {"PRIVATE-TOKEN": TOKEN}, {}, api), "1")
        text = adapter.trace(501)
        self.assertEqual(len(text.encode("utf-8")), collect.TRACE_TAIL_BYTES)
        self.assertEqual(api.calls[0].tail, collect.TRACE_TAIL_BYTES)
        record = collect.classify_ci.classify_job({"id": 501, "name": "build", "failure_reason": "script_failure"}, text)
        self.assertEqual(record["category"], "infra")
        self.assertNotIn("test.gradle", {m["rule"] for m in record["matches"]})
        # 기본 전송이 쓰는 읽기 함수도 끝 1 MiB만 남긴다
        kept = collect.read_body(io.BytesIO(trace.encode("utf-8")), collect.TRACE_TAIL_BYTES)
        self.assertEqual(kept, trace.encode("utf-8")[-collect.TRACE_TAIL_BYTES:])

    def test_missing_trace_classifies_metadata(self):
        api = FakeApi([])
        adapter = collect.GitLabAdapter(collect.ApiClient(GITLAB_API, {"PRIVATE-TOKEN": TOKEN}, {}, api), "1")
        self.assertIsNone(adapter.trace(1))


if __name__ == "__main__":
    unittest.main()
