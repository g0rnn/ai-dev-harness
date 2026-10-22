"""리허설 스크립트(.github/scripts/rehearse.py, M1-8) 테스트. docker를 쓰는 보안 시나리오 실행은 CI의
rehearsal job이 맡고, 여기서는 시나리오 표·fixture·판정 규칙과 docker 없는 hooks 리허설을 본다."""

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import process_support  # noqa: E402,F401 — test-only Windows child launch policy

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / ".github" / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rehearse = load("rehearse")
runner = load("run_fragment")
TOKEN = "ghp_" + "A1" * 18


def scenario(name):
    return next(s for s in rehearse.scenarios(TOKEN) if s["name"] == name)


class RehearseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-rehearse-test-"))
        self.addCleanup(rehearse.remove_tree, self.tmp)

    def test_scenarios_cover_all_fragments(self):
        table = rehearse.scenarios(TOKEN)
        self.assertEqual(len({s["name"] for s in table}), len(table))
        detections = {}
        for s in table:
            self.assertIn(s["fragment"], rehearse.REPORTS)
            self.assertTrue((runner.GITLAB / f"{s['fragment']}.yml").is_file(), s["name"])
            self.assertTrue(s.get("ids") or s.get("output") or s.get("empty"), s["name"])  # 무엇을 볼지 없으면 안 된다
            if s.get("ids"):
                detections.setdefault(s["fragment"], []).append(s["name"])
            if s["code"] == 0 and not s.get("empty"):
                self.assertTrue(s.get("output"), s["name"])  # 통과로 끝나는 검출(경고)은 경고 출력을 본다
        self.assertEqual(set(detections), set(rehearse.REPORTS))  # 조각 4종 모두 실제 검출 사례가 있다
        self.assertIn("HARNESS_SCAN_IMAGE", scenario("image-vulnerable")["env"])
        self.assertRegex(rehearse.IMAGE_FIXTURE, r"@sha256:[0-9a-f]{64}$")

    def test_fixture_is_pr_merge(self):
        repo = rehearse.make_pr_repo(self.tmp / "fx", rehearse.npm_project(lock=True))
        parents = rehearse.git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()
        self.assertEqual(len(parents), 3)
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": ""}):
            env = runner.gitlab_env(repo)
        changed = rehearse.git(repo, "diff", "--name-only", env["CI_MERGE_REQUEST_DIFF_BASE_SHA"],
                               env["CI_MERGE_REQUEST_SOURCE_BRANCH_SHA"]).splitlines()
        self.assertEqual(sorted(changed), ["web/package-lock.json", "web/package.json"])
        target_only = rehearse.git(repo, "diff", "--name-only", env["CI_MERGE_REQUEST_DIFF_BASE_SHA"],
                                   env["CI_MERGE_REQUEST_TARGET_BRANCH_SHA"]).splitlines()
        self.assertEqual(target_only, ["docs/changelog.md"])
        self.assertTrue((repo / "AGENTS.md").is_file())  # init 결과 위에 만든다
        self.assertEqual(rehearse.git(repo, "status", "--porcelain"), "")

    def test_leak_fixture_keeps_token_only_in_history(self):
        repo = rehearse.make_pr_repo(self.tmp / "leak", rehearse.leak_then_remove(TOKEN))
        self.assertFalse((repo / "src" / "config.py").exists())  # 지운 Secret도 잡는지 보는 시나리오
        history = rehearse.git(repo, "log", "-p", "--all")
        self.assertIn(TOKEN, history)

    def test_secret_fixture_not_in_kit(self):
        tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True,
                                 check=True).stdout.splitlines()
        # M3-5(#56): 프로필 리허설 fixture의 npm lock은 의존성 고정(결정 H-5)이라 커밋한다. 이 경로 하나만 예외다
        profile_lock = "tests/fixtures/profiles/react-ts/frontend/package-lock.json"
        lockfiles = [f for f in tracked if Path(f).name in ("package-lock.json", "gradle.lockfile", "yarn.lock")
                     and f != profile_lock]
        self.assertEqual(lockfiles, [])  # 취약 lockfile은 실행 중에만 만든다
        pattern = re.compile(r"ghp_[A-Za-z0-9]{36}")
        for rel in tracked:
            path = ROOT / rel
            if path.suffix in (".py", ".md", ".yml", ".json", ".txt") and path.is_file():
                self.assertIsNone(pattern.search(path.read_text(encoding="utf-8", errors="replace")), rel)
        token = rehearse.fake_token()
        self.assertRegex(token, r"^ghp_[A-Za-z0-9]{36}$")
        self.assertNotEqual(token, rehearse.fake_token())

    def test_judge_requires_report_id(self):
        npm = scenario("npm-vulnerable")
        found = json.dumps({"Results": [{"Vulnerabilities": [{"VulnerabilityID": "CVE-2021-23337"}]}]})
        other = json.dumps({"Results": [{"Vulnerabilities": [{"VulnerabilityID": "CVE-1999-0001"}]}]})
        self.assertEqual(rehearse.judge(npm, 1, "", found, TOKEN), [])
        self.assertTrue(rehearse.judge(npm, 1, "", other, TOKEN))  # 다른 이유의 실패는 기대대로가 아니다
        self.assertTrue(rehearse.judge(npm, 1, "", None, TOKEN))  # 리포트 없음(도구 오류)
        self.assertTrue(rehearse.judge(npm, 1, "", "{not json", TOKEN))
        self.assertTrue(rehearse.judge(npm, 0, "", found, TOKEN))  # 종료 코드가 다르다
        self.assertTrue(rehearse.judge(npm, 2, "", found, TOKEN))

        secret = scenario("secret-leak")
        redacted = {"RuleID": "github-pat", "Secret": "REDACTED", "Match": "REDACTED"}
        self.assertEqual(rehearse.judge(secret, 1, "", json.dumps([redacted]), TOKEN), [])
        self.assertTrue(rehearse.judge(secret, 1, "", json.dumps({"RuleID": "github-pat"}), TOKEN))
        clean = scenario("secret-clean")
        self.assertEqual(rehearse.judge(clean, 0, "1 commits scanned", "[]", TOKEN), [])
        self.assertTrue(rehearse.judge(clean, 0, "0 commits scanned", "[]", TOKEN))
        self.assertTrue(rehearse.judge(clean, 0, "1 commits scanned", json.dumps([{"RuleID": "x"}]), TOKEN))
        self.assertTrue(rehearse.judge(clean, 0, "", None, TOKEN))

        sast = scenario("sast-finding")
        report = json.dumps({"results": [{"check_id": "python.lang.security.audit.subprocess-shell-true"}]})
        warned = "::warning title=harness-sast::경고"
        self.assertEqual(rehearse.judge(sast, 0, warned, report, TOKEN), [])
        self.assertTrue(rehearse.judge(sast, 0, "", report, TOKEN))  # 경고 없이 통과하면 안 된다

        missing = scenario("npm-no-lockfile")
        self.assertEqual(rehearse.judge(missing, 1, "harness: ...\nweb/package.json: lockfile 없음(...)", None, TOKEN), [])
        self.assertTrue(rehearse.judge(missing, 1, "trivy error", None, TOKEN))

    def test_token_absence_checked(self):
        secret = scenario("secret-leak")
        report = json.dumps([{"RuleID": "github-pat", "Secret": "REDACTED", "Match": "REDACTED"}])
        self.assertTrue(rehearse.judge(secret, 1, f"leak {TOKEN}", report, TOKEN))
        self.assertTrue(rehearse.judge(secret, 1, "", json.dumps([{"RuleID": "github-pat", "Match": TOKEN}]), TOKEN))
        self.assertTrue(rehearse.judge(secret, 1, "", json.dumps([{"RuleID": "github-pat",
            "Secret": TOKEN[:20], "Match": "REDACTED"}]), TOKEN))
        self.assertEqual(rehearse.judge(secret, 1, "", report, TOKEN), [])
        self.assertFalse(rehearse.token_fragments(rehearse.redact_token("leak " + TOKEN[:20], TOKEN), TOKEN))

    def test_clean_sast_requires_scanned_file(self):
        clean = scenario("sast-clean")
        self.assertEqual(rehearse.judge(clean, 0, "", json.dumps({"scanned": 1, "results": []}), TOKEN), [])
        self.assertTrue(rehearse.judge(clean, 0, "", json.dumps({"scanned": 0, "results": []}), TOKEN))
        self.assertTrue(rehearse.judge(clean, 0, "::warning", json.dumps({"scanned": 1, "results": []}), TOKEN))

    def test_installed_hook_command_and_matcher(self):
        target = rehearse.make_target(self.tmp / "consumer")
        settings = target / ".claude" / "settings.json"
        original = json.loads(settings.read_text(encoding="utf-8"))
        changed = json.loads(settings.read_text(encoding="utf-8"))
        changed["hooks"]["Stop"][0]["hooks"][0]["command"] += " || exit 0"
        settings.write_text(json.dumps(changed), encoding="utf-8")
        self.assertTrue(rehearse.check_hooks(target))
        changed = original
        changed["hooks"]["PreToolUse"][0]["matcher"] = "NoSuchTool"
        settings.write_text(json.dumps(changed), encoding="utf-8")
        self.assertTrue(rehearse.check_hooks(target))

    def test_hooks_rehearsal(self):
        target = rehearse.make_target(self.tmp / "consumer")
        self.assertEqual(rehearse.check_hooks(target), [])

    def test_hooks_rehearsal_detects_missing_install(self):
        target = rehearse.make_target(self.tmp / "consumer")
        (target / ".claude" / "hooks" / "stop-verify.py").unlink()
        self.assertTrue(rehearse.check_hooks(target))

    def test_cli_arguments(self):
        with mock.patch("sys.stderr"):
            self.assertEqual(rehearse.main(["rehearse.py"]), 2)
            self.assertEqual(rehearse.main(["rehearse.py", "other"]), 2)
            self.assertEqual(rehearse.main(["rehearse.py", "hooks", "extra"]), 2)
        with mock.patch("builtins.print"):
            self.assertEqual(rehearse.main(["rehearse.py", "security", "no-such-scenario"]), 2)


class CiWorkflowTest(unittest.TestCase):
    def test_rehearsal_job(self):
        body = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        # M2-2(#37): PR 본문 수정에도 mr-lint가 다시 돌도록 edited를 더했다. 다른 job도 같은 트리거로 돈다
        self.assertRegex(body, r"^name: ci\n\non:\n  push:\n    branches: \[main\]\n(?:  #.*\n)?"
                               r"  pull_request:\n    types: \[opened, synchronize, reopened, edited\]\n\njobs:\n")
        self.assertNotRegex(body, r"(?m)^(?:defaults|env):")
        block = re.search(r"^  rehearsal:\n((?:(?:    .*)?\n)+)", body, re.M).group(1)
        lines = [line.split("  #")[0].rstrip() for line in block.splitlines()
                 if line.strip() and not line.lstrip().startswith("#")]
        self.assertEqual(lines, [
            "    runs-on: ubuntu-latest",
            "    timeout-minutes: 20",
            "    permissions:",
            "      contents: read",
            "    steps:",
            "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
            "        with:",
            "          persist-credentials: false",
            "      - name: hooks 리허설",
            "        run: python3 .github/scripts/rehearse.py hooks",
            "      - name: 보안 검사 리허설",
            "        run: python3 .github/scripts/rehearse.py security",
        ])
        # M2-2(#37): 실패를 가리는 continue-on-error는 판정 댓글 job(PR 체크가 아닌 선택 기능)의 아티팩트 내려받기에만 둔다
        comment_job = re.search(r"^  mr-lint-comment:\n(?:(?:    .*)?\n)+", body, re.M).group(0)
        self.assertNotIn("continue-on-error", body.replace(comment_job, ""))
        self.assertEqual(comment_job.count("continue-on-error"), 1)


if __name__ == "__main__":
    unittest.main()
