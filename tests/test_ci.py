"""core/ci/gitlab/ 보안 검사 조각의 구조 규칙. 동작은 GitLab MR 파이프라인에서 확인한다.

표준 라이브러리에 YAML 파서가 없으므로 텍스트 수준에서 본다. 조각은 최상위 job 키 아래에 두 칸
들여쓰기로 쓴다는 형식을 전제로 한다.
"""

import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tempdirs import remove_tree  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
GITLAB = ROOT / "core" / "ci" / "gitlab"
FRAGMENTS = ["secret-detection", "dependency-audit", "sast", "image-scan"]
BLOCKING = ["secret-detection", "dependency-audit", "image-scan"]


def text(name):
    return (GITLAB / f"{name}.yml").read_text(encoding="utf-8")


def header(name):
    lines = []
    for line in text(name).splitlines():
        if not line.startswith("#"):
            break
        lines.append(line)
    return "\n".join(lines)


def jobs(name):
    """{최상위 키: 본문}. 주석과 빈 줄은 뺀다."""
    result, current = {}, None
    for line in text(name).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        top = re.match(r"^([^\s#][^:]*):\s*$", line)
        if top:
            current = top.group(1)
            result[current] = []
        elif current is not None:
            result[current].append(line)
    return {k: "\n".join(v) for k, v in result.items()}


class CiFragmentTest(unittest.TestCase):
    def test_fragments_exist(self):
        for name in FRAGMENTS:
            self.assertTrue((GITLAB / f"{name}.yml").is_file(), name)

    def test_header_documents_usage(self):
        for name in FRAGMENTS:
            head = header(name)
            for needle in ("include:", "- remote: https://", f"core/ci/gitlab/{name}.yml", "필요 조건:", "예외:"):
                self.assertIn(needle, head, name)

    def test_images_pinned_by_digest(self):
        for name in FRAGMENTS:
            images = re.findall(r"^\s+name:\s*(\S+)", text(name), re.M)
            self.assertTrue(images, name)
            for image in images:
                self.assertRegex(image, r":[\w.-]+@sha256:[0-9a-f]{64}$", name)

    def test_blocking_jobs_not_allowed_to_fail(self):
        for name in BLOCKING:
            for job, body in jobs(name).items():
                self.assertNotIn("allow_failure", body, f"{name}:{job}")

    def test_sast_allows_only_findings_code(self):
        body = jobs("sast")["harness-sast"]
        self.assertRegex(body, r"allow_failure:\n\s+exit_codes: \[3\]")
        self.assertNotRegex(body, r"allow_failure:\s*true")
        self.assertRegex(body, r"0\|1\) ;;\n\s+\*\) fail ")  # semgrep의 0·1 밖은 실패
        self.assertIn('fail() { echo "harness: $*"; exit 2; }', body)
        self.assertEqual(len(re.findall(r"exit 3", body)), 1)  # 경고는 요약 단계 한 곳에서만
        self.assertIn(">/dev/null", body)  # 코드 줄을 그대로 보여 주는 기본 출력은 버린다
        self.assertIn("CI_MERGE_REQUEST_TARGET_BRANCH_SHA", body)
        self.assertIn('git merge-base --is-ancestor "$base" HEAD', body)
        self.assertIn('git diff --name-only "$base" HEAD -- "$c"', body)

    def test_no_error_swallowing(self):
        for name in FRAGMENTS:
            body = text(name)
            self.assertNotIn("set +e", body, name)
            self.assertNotIn("--exit-code 0", body, name)

    def test_tool_flags(self):
        secret = text("secret-detection")
        for flag in ("--redact", "--exit-code 1", "--remerge-diff", "merge-base --is-ancestor", "useDefault"):
            self.assertIn(flag, secret)
        self.assertIn("in_extend &&", secret)
        self.assertIn("/^[[:space:]]*\\[/ { in_extend=0 }", secret)
        for name in ("dependency-audit", "image-scan"):
            body = text(name)
            for flag in ("--exit-code 1", "--config /dev/null", "--ignore-unfixed", "--severity HIGH,CRITICAL"):
                self.assertIn(flag, body, name)
            self.assertRegex(body, r"cache:\n\s+key: harness-trivy\n\s+when: always", name)
        self.assertRegex(text("image-scan"), r"artifacts:\n\s+when: always\n\s+access: developer")
        image = text("image-scan")
        self.assertIn('set -- --input "$HARNESS_SCAN_ARCHIVE"', image)
        self.assertIn('if [ -n "${HARNESS_SCAN_IMAGE:-}" ]; then fail', image)
        self.assertIn('if [ ! -f "$HARNESS_SCAN_ARCHIVE" ] || [ ! -s "$HARNESS_SCAN_ARCHIVE" ]', image)

    def test_merge_request_only(self):
        for name in FRAGMENTS:
            for job, body in jobs(name).items():
                rules = re.search(r"^  rules:\n((?:    .*\n)+)", body + "\n", re.M)
                self.assertIsNotNone(rules, f"{name}:{job}")
                lines = rules.group(1).splitlines()
                self.assertEqual(lines[0], '    - if: $CI_PIPELINE_SOURCE == "merge_request_event"', f"{name}:{job}")
                rest = [line for line in lines[1:] if not re.match(r"^      (exists:|  - )", line)]
                self.assertEqual(rest, [], f"{name}:{job}")  # 다른 규칙이나 when으로 MR 밖에서 돌지 않는다

    def test_job_names_prefixed(self):
        for name in FRAGMENTS:
            keys = list(jobs(name))
            self.assertTrue(keys, name)
            for key in keys:
                self.assertTrue(key.startswith("harness-"), f"{name}:{key}")

    def test_scripts_fail_fast(self):
        for name in FRAGMENTS:
            for job, body in jobs(name).items():
                script = re.search(r"^  script:\n\s+- \|\n\s+(.+)", body, re.M)
                self.assertIsNotNone(script, f"{name}:{job}")
                self.assertRegex(script.group(1), r"^set -eu", f"{name}:{job}")


RUNNER_SPEC = importlib.util.spec_from_file_location("run_fragment", ROOT / ".github" / "scripts" / "run_fragment.py")
runner = importlib.util.module_from_spec(RUNNER_SPEC)
RUNNER_SPEC.loader.exec_module(runner)
WORKFLOW = ROOT / ".github" / "workflows" / "security.yml"


def git(repo, *args):
    """git 명령을 실행하고 커밋을 만드는 명령이면 새 HEAD를 돌려준다."""
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    if args[0] in ("commit", "merge"):
        return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                              capture_output=True, text=True).stdout.strip()
    return None


class RunFragmentTest(unittest.TestCase):
    """키트 GitHub Actions가 조각을 그대로 실행하는 러너(M1-7). docker 실행은 PR의 실제 job으로 본다."""

    def parse(self, name):
        return runner.parse_fragment(text(name), name)

    def test_extracts_all_fragments(self):
        for name in FRAGMENTS:
            frag = self.parse(name)
            self.assertEqual(frag["job"], f"harness-{name}")
            self.assertRegex(frag["image"], r":[\w.-]+@sha256:[0-9a-f]{64}$", name)
            self.assertTrue(frag["script"].startswith("set -eu"), name)
            self.assertGreater(len(frag["script"].splitlines()), 10, name)
        self.assertEqual(self.parse("sast")["allow_codes"], {3})
        for name in BLOCKING:
            self.assertEqual(self.parse(name)["allow_codes"], set(), name)
        self.assertEqual(self.parse("secret-detection")["variables"], {"GIT_DEPTH": "0"})
        self.assertEqual(self.parse("dependency-audit")["variables"]["TRIVY_CACHE_DIR"], ".trivycache")

    def test_script_matches_fragment(self):
        for name in FRAGMENTS:
            block = re.search(r"^  script:\n    - \|\n((?:(?:      .*)?\n)+)", text(name), re.M).group(1)
            expected = "\n".join(line[6:] for line in block.splitlines()).strip("\n") + "\n"
            self.assertEqual(self.parse(name)["script"], expected, name)
        self.assertIn("\nPY\n", self.parse("sast")["script"])  # heredoc 끝 표시가 0칸으로 돌아온다

    def test_rejects_unexpected_shape(self):
        base = text("secret-detection")
        cases = {
            "script 없음": re.sub(r"  script:\n(?:(?:    .*)?\n)+", "", base),
            "블록 두 개": base.replace("  artifacts:", "    - echo extra\n  artifacts:"),
            "job 두 개": base + "\nharness-other:\n  script:\n    - |\n      set -eu\n",
            "allow_failure: true": base.replace("  script:", "  allow_failure: true\n  script:"),
            "set -eu 없음": base.replace("      set -eu\n", "      echo start\n", 1),
            "이미지 없음": re.sub(r"  image:\n(?:    .*\n)+", "", base),
            # 아래는 리뷰 #16 B-F1·F7, A-F2: 조용히 스크립트를 자르거나 값을 바꿔 넘기던 모양
            "스크립트 중간의 0칸 주석": base.replace("      fail() {", "# 메모\n      fail() {", 1),
            "스크립트 중간의 2칸 줄": base.replace("      fail() {", "  x\n      fail() {", 1),
            "before_script": base.replace("  script:", "  before_script:\n    - echo hi\n  script:"),
            "extends": base.replace("  stage: test", "  stage: test\n  extends: .base"),
            "한 줄 variables": base.replace('  variables:\n    GIT_DEPTH: "0"', '  variables: {GIT_DEPTH: "0"}'),
            "작은따옴표 값": base.replace('GIT_DEPTH: "0"', "GIT_DEPTH: '0'"),
            "$ 값": base.replace('GIT_DEPTH: "0"', 'GIT_DEPTH: "$X"'),
            "탭 들여쓰기": base.replace("      fail() {", "\t  fail() {", 1),
            "CRLF": base.replace("\n", "\r\n"),
            "image 모르는 줄": base.replace('    entrypoint: [""]', '    entrypoint: [""]\n    pull_policy: always'),
        }
        for label, broken in cases.items():
            self.assertNotEqual(broken, base, label)  # 변형이 실제로 들어갔다
            with self.assertRaises(runner.FragmentError, msg=label):
                runner.parse_fragment(broken, label)
        tolerant = base.replace("  variables:\n", "  variables:   \n")  # 키 줄 끝 공백은 같은 뜻이다
        self.assertEqual(runner.parse_fragment(tolerant)["variables"], {"GIT_DEPTH": "0"})

    def pr_repo(self):
        """base 위에 feature(source)와 main(target)이 갈라진 임시 저장소. HEAD는 merge 전 main."""
        repo = Path(tempfile.mkdtemp(prefix="harness-runner-"))
        self.addCleanup(remove_tree, repo)
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "t@t")
        git(repo, "config", "user.name", "t")
        shas = {}
        for name, checkout in (("base", None), ("source", ["-qb", "feature"]), ("target", ["-q", "main"])):
            if checkout:
                git(repo, "checkout", *checkout)
            (repo / name).write_text(name)
            git(repo, "add", ".")
            shas[name] = git(repo, "commit", "-qm", name)
        return repo, shas

    def event_env(self, head_sha):
        event = Path(tempfile.mkdtemp(prefix="harness-event-")) / "event.json"
        self.addCleanup(remove_tree, event.parent)
        event.write_text(json.dumps({"pull_request": {"head": {"sha": head_sha}}}), encoding="utf-8")
        return {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_PATH": str(event)}

    def test_gitlab_env_from_pr(self):
        repo, shas = self.pr_repo()
        with self.assertRaises(runner.FragmentError):  # merge 커밋이 아니면 범위를 만들지 않는다
            runner.gitlab_env(repo)
        merge = git(repo, "merge", "-q", "--no-ff", "feature", "-m", "merge")
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": ""}):
            env = runner.gitlab_env(repo)
        self.assertEqual(env["CI_PIPELINE_SOURCE"], "merge_request_event")
        self.assertEqual(env["CI_COMMIT_SHA"], merge)
        self.assertEqual(env["CI_MERGE_REQUEST_TARGET_BRANCH_SHA"], shas["target"])
        self.assertEqual(env["CI_MERGE_REQUEST_SOURCE_BRANCH_SHA"], shas["source"])
        self.assertEqual(env["CI_MERGE_REQUEST_DIFF_BASE_SHA"], shas["base"])
        self.assertEqual(env["CI_PROJECT_DIR"], runner.PROJECT_DIR)
        self.assertEqual(runner.self_changes(repo, env), [])

    def test_checks_parent_order_against_event(self):
        repo, shas = self.pr_repo()
        git(repo, "merge", "-q", "--no-ff", "feature", "-m", "merge")
        with mock.patch.dict(os.environ, self.event_env(shas["source"])):
            self.assertEqual(runner.gitlab_env(repo)["CI_MERGE_REQUEST_SOURCE_BRANCH_SHA"], shas["source"])
        git(repo, "checkout", "-q", "feature")  # 리뷰 #16 B-F5: feature에 main을 합친 커밋(부모 순서 반대)
        git(repo, "merge", "-q", "--no-ff", "main", "-m", "reverse")
        with mock.patch.dict(os.environ, self.event_env(shas["source"])):
            with self.assertRaises(runner.FragmentError):
                runner.gitlab_env(repo)
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_PATH": str(repo / "none.json")}):
            with self.assertRaises(runner.FragmentError):  # 이벤트를 못 읽으면 순서 확인을 건너뛰지 않는다
                runner.gitlab_env(repo)

    def test_rejects_incomplete_clones_and_octopus(self):
        repo, shas = self.pr_repo()
        git(repo, "checkout", "-qb", "other", shas["base"])
        (repo / "o").write_text("o")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "other")
        git(repo, "checkout", "-q", "main")
        git(repo, "merge", "-q", "--no-ff", "feature", "other", "-m", "octopus")
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": ""}):
            with self.assertRaises(runner.FragmentError):  # 부모 3개
                runner.gitlab_env(repo)
            git(repo, "reset", "-q", "--hard", "HEAD~1")
            git(repo, "merge", "-q", "--no-ff", "feature", "-m", "merge")
            runner.gitlab_env(repo)  # 정상 merge로 돌아오면 통과한다
            git(repo, "config", "extensions.partialclone", "origin")  # 리뷰 #16 B-F6
            with self.assertRaises(runner.FragmentError):
                runner.gitlab_env(repo)
            git(repo, "config", "--unset", "extensions.partialclone")
            shallow = repo.parent / (repo.name + "-shallow")
            self.addCleanup(remove_tree, shallow)
            subprocess.run(["git", "clone", "-q", "--depth", "1", repo.resolve().as_uri(), str(shallow)],
                           check=True, capture_output=True)
            with self.assertRaises(runner.FragmentError):
                runner.gitlab_env(shallow)

    def test_main_runs_docker_and_keeps_exit_code(self):
        """리뷰 #16 B-F2: 종료 코드·환경 전달·스크립트 전달을 main 경로에서 본다(docker는 가짜)."""
        mr = {"CI_PIPELINE_SOURCE": "merge_request_event", "CI_PROJECT_DIR": runner.PROJECT_DIR,
              "CI_COMMIT_SHA": "m" * 40, "CI_MERGE_REQUEST_TARGET_BRANCH_SHA": "t" * 40,
              "CI_MERGE_REQUEST_SOURCE_BRANCH_SHA": "s" * 40, "CI_MERGE_REQUEST_DIFF_BASE_SHA": "b" * 40}
        outer = {"GITHUB_TOKEN": "tok", "AWS_SECRET_ACCESS_KEY": "aws", "CI_COMMIT_SHA": "evil",
                 "CI_MERGE_REQUEST_DIFF_BASE_SHA": "evil", "HARNESS_SEMGREP_CONFIG": "p/x",
                 "TRIVY_DB_REPOSITORY": "r", "XHARNESS_SEMGREP_CONFIG": "no"}
        cases = [("secret-detection", 0, 0), ("secret-detection", 1, 1), ("secret-detection", 3, 3),
                 ("secret-detection", 125, 125), ("secret-detection", -9, -9),
                 ("sast", 3, 0), ("sast", 2, 2), ("sast", 1, 1)]
        for name, docker_code, expected in cases:
            frag = self.parse(name)
            with self.subTest(name=name, code=docker_code), \
                    mock.patch.dict(os.environ, outer), \
                    mock.patch.object(runner, "gitlab_env", return_value=dict(mr)), \
                    mock.patch.object(runner, "self_changes", return_value=[]), \
                    mock.patch.object(runner.subprocess, "run",
                                      return_value=subprocess.CompletedProcess([], docker_code)) as run, \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(runner.main(["run_fragment.py", name, str(ROOT)]), expected)
                cmd, env = run.call_args.args[0], run.call_args.kwargs["env"]
                self.assertEqual(cmd[:2], ["docker", "run"])
                self.assertEqual(cmd[-5:], [frag["image"], "-c", runner.SHELL_WRAPPER, "harness-runner",
                                            frag["script"]])
                keys = {cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-e"}
                self.assertEqual(keys, set(mr) | set(frag["variables"])
                                 | {"HARNESS_SEMGREP_CONFIG", "TRIVY_DB_REPOSITORY"})
                self.assertNotIn("tok", cmd)
                self.assertEqual(env["CI_COMMIT_SHA"], "m" * 40)  # 러너 환경으로 MR 범위를 덮지 못한다
                self.assertEqual(env["CI_MERGE_REQUEST_DIFF_BASE_SHA"], "b" * 40)

    def test_main_warns_on_self_change_and_fails_on_bad_input(self):
        mr = {"CI_MERGE_REQUEST_DIFF_BASE_SHA": "b" * 40, "CI_MERGE_REQUEST_SOURCE_BRANCH_SHA": "s" * 40,
              "CI_MERGE_REQUEST_TARGET_BRANCH_SHA": "t" * 40}
        out = io.StringIO()
        with mock.patch.object(runner, "gitlab_env", return_value=mr), \
                mock.patch.object(runner, "self_changes", return_value=[".github/scripts/run_fragment.py"]), \
                mock.patch.object(runner.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)), \
                redirect_stdout(out):
            self.assertEqual(runner.main(["run_fragment.py", "secret-detection", str(ROOT)]), 0)
        self.assertIn("::warning title=harness-secret-detection::이 PR이 검사 정의를 바꿨다", out.getvalue())
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(["run_fragment.py", "../secret-detection", str(ROOT)]), 2)
            self.assertEqual(runner.main(["run_fragment.py", "no-such", str(ROOT)]), 2)
            with mock.patch.object(runner, "gitlab_env", side_effect=runner.FragmentError("x")):
                self.assertEqual(runner.main(["run_fragment.py", "sast", str(ROOT)]), 2)

    def test_shell_matches_gitlab(self):
        """리뷰 #16 A-F3: GitLab Docker executor처럼 bash가 있으면 pipefail을 건다."""
        self.assertTrue(runner.SHELL_WRAPPER.startswith("if command -v bash >/dev/null 2>&1; then "))
        self.assertIn('exec bash -eo pipefail -c "$1"; fi; ', runner.SHELL_WRAPPER)
        self.assertTrue(runner.SHELL_WRAPPER.endswith('exec sh -e -c "$1"'))

    def test_warning_code_mapping(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(runner.outcome(3, {3}, "harness-sast"), 0)
        self.assertIn("::warning title=harness-sast::", out.getvalue())
        with redirect_stdout(io.StringIO()) as quiet:
            self.assertEqual(runner.outcome(0, {3}, "harness-sast"), 0)
            self.assertEqual(runner.outcome(2, {3}, "harness-sast"), 2)
            self.assertEqual(runner.outcome(1, {3}, "harness-sast"), 1)
            self.assertEqual(runner.outcome(3, set(), "harness-secret-detection"), 3)
        self.assertEqual(quiet.getvalue(), "")


class SecurityWorkflowTest(unittest.TestCase):
    def test_structure(self):
        body = WORKFLOW.read_text(encoding="utf-8")
        self.assertRegex(body, r"\non:\n  pull_request:\n\n")  # PR 이벤트 하나뿐
        self.assertRegex(body, r"\npermissions:\n  contents: read\n")
        jobs_ = re.findall(r"^  ([\w-]+):\n    runs-on:", body, re.M)
        self.assertEqual(jobs_, ["secret-detection", "sast"])  # 필수 체크 이름
        for job in jobs_:
            self.assertIn(f"run: python3 .github/scripts/run_fragment.py {job}\n", body)
        self.assertEqual(body.count("fetch-depth: 0"), 2)
        self.assertEqual(body.count("persist-credentials: false"), 2)
        self.assertNotIn("continue-on-error", body)
        for use in re.findall(r"uses: (\S+)", body):
            self.assertRegex(use, r"@[0-9a-f]{40}$")  # 액션도 커밋으로 고정한다

    def test_job_steps_exact(self):
        """리뷰 #16 B-F8: step 조건, checkout ref·filter, 주석 속 실행 줄, job 간 조각 바꿔치기를 막는다.
        보안 workflow라 job 본문을 줄 단위로 고정한다. 바꾸려면 이 기대값도 함께 바꾼다(엄격 리뷰 대상)."""
        body = WORKFLOW.read_text(encoding="utf-8")
        reports = {"secret-detection": "gitleaks-report", "sast": "semgrep-report"}
        for job, report in reports.items():
            block = re.search(rf"^  {job}:\n((?:(?:    .*)?\n)+)", body, re.M).group(1)
            lines = [line.split("  #")[0].rstrip() for line in block.splitlines()
                     if line.strip() and not line.lstrip().startswith("#")]
            lines = [re.sub(r"^      - name: .*$", "      - name: <이름>", line) for line in lines]
            expected = [
                "    runs-on: ubuntu-latest",
                "    timeout-minutes: 15",
                "    steps:",
                "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                "        with:",
                "          fetch-depth: 0",
                "          persist-credentials: false",
                "      - name: <이름>",
                f"        run: python3 .github/scripts/run_fragment.py {job}",
                "      - uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
                "        if: always()",
                "        with:",
                f"          name: {report}",
                f"          path: {report}.json",
                "          retention-days: 30",
                "          if-no-files-found: ignore",
            ]
            self.assertEqual(lines, expected, job)


if __name__ == "__main__":
    unittest.main()
