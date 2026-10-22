"""#123 저장소 안 요약 명령 `.harness/summary.py`: 키트 없이 키트 명령과 같은 MR 본문 절을 만든다."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tempdirs import remove_tree  # noqa: E402
from test_hooks import git  # noqa: E402
from test_review_metric import STRICT_TABLE  # noqa: E402
from test_sessions import SessionRecordCase, run_cli  # noqa: E402
import test_usage  # noqa: E402  # UsageHookTest를 이름으로 가져오면 그 테스트가 여기서도 돈다
from test_usage import assistant  # noqa: E402

SUMMARY_FILES = (".harness/summary.py", ".harness/metrics/collect.py", ".harness/metrics/classify_ci.py", ".harness/metrics/attribution.py")


def init_target(target: Path, platform: str):
    tracker = ("--tracker", "jira", "--issue-prefix", "DEMO") if platform == "gitlab" else ("--tracker", "github")
    return run_cli(["init", str(target), "--platform", platform, *tracker])


class InitSummaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-summary-"))
        self.addCleanup(remove_tree, self.tmp)

    def test_all_platforms_get_summary(self):
        """T1: GitLab·GitHub 모두 요약 명령과 그것이 읽는 코드가 생기고 check가 통과한다"""
        for platform in ("gitlab", "github"):
            with self.subTest(platform=platform):
                target = self.tmp / platform
                code, out, err = init_target(target, platform)
                self.assertEqual(code, 0, err)
                for rel in SUMMARY_FILES:
                    self.assertTrue((target / rel).is_file(), rel)
                self.assertEqual((target / ".harness" / "metrics" / "report.py").is_file(), platform == "gitlab")
                code, out, err = run_cli(["check", str(target)])
                self.assertEqual(code, 0, out + err)

    def test_generated_rules_point_to_in_repo_command(self):
        """T2: MR을 만드는 쪽(AI 포함)이 따르는 문서가 키트 경로가 아니라 저장소 안 명령을 가리킨다"""
        target = self.tmp / "gh"
        self.assertEqual(init_target(target, "github")[0], 0)
        agents = (target / "AGENTS.md").read_text(encoding="utf-8")
        # #135: 리뷰 요약은 최종 리뷰 파일 저장(`--save`)과 함께 실행한다
        for command in ("events", "agent", "review <리뷰 파일> --save"):
            self.assertIn(f"`python .harness/summary.py {command}`", agents)
        self.assertIn("docs/reviews/", agents)
        for rel in ("docs/development-workflow.md", ".github/PULL_REQUEST_TEMPLATE.md"):
            text = (target / rel).read_text(encoding="utf-8")
            self.assertIn("python .harness/summary.py agent", text, rel)
            self.assertIn("python .harness/summary.py review <리뷰 파일> --save", text, rel)
            self.assertIn("docs/reviews/", text, rel)
            self.assertNotIn("<키트>/bin/harness.py", text, rel)


class InRepoSummaryTest(SessionRecordCase):
    def test_explicit_task_pr_scope_works_from_installed_cli(self):
        self.prepare()
        stamp='2026-10-20T10:00:00+00:00'
        rows=[dict(session='scoped',tool='codex',branch='different-checkout',ts=stamp,event='start'),
              dict(session='scoped',tool='codex',branch='different-checkout',ts='2026-10-20T10:01:00+00:00',event='stop',
                   usage={'fixture':dict(input=10,output=2,cache_read=0,cache_write_5m=0,cache_write_1h=0)})]
        state=self.project/'.git/harness';state.mkdir(parents=True,exist_ok=True)
        (state/'sessions.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf8')
        for command in [('bind','--task','TASK','--session','scoped','--since',stamp),('link','--task','TASK','--pr','143')]:
            result=self.in_repo(*command)
            self.assertEqual(result.returncode,0,result.stderr)
        result=self.in_repo('agent','--pr','143')
        code,out,err=run_cli(['agent','summary',str(self.project),'--pr','143'])
        self.assertEqual((result.returncode,code),(0,0),result.stderr+err)
        self.assertEqual(result.stdout,out)
        self.assertIn('입력 10',out)
        self.assertIn('응답: 1',out)
        self.assertNotIn('기록 없는 커밋',out)
        legacy=self.in_repo('agent')
        self.assertEqual(legacy.returncode,2)
        self.assertIn('--task',legacy.stderr)

    transcript = test_usage.UsageHookTest.transcript
    stop_with = test_usage.UsageHookTest.stop_with

    def in_repo(self, *args):
        env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(self.tmp)}
        return subprocess.run([sys.executable, str(Path(".harness") / "summary.py"), *args], cwd=self.project, env=env,
                              capture_output=True, text=True, encoding="utf-8", timeout=120)

    def prepare(self, platform="github"):
        code, out, err = init_target(self.project, platform)
        self.assertEqual(code, 0, err)
        self.init_git()
        git(self.project, "branch", "-M", "main")
        git(self.project, "checkout", "-q", "-b", "feat/x")

    def test_same_output_as_kit(self):
        """T3: 실제 hook 기록(세션·토큰·보호 경로 판정)으로 agent·sessions·events가 키트 명령과 같은 출력"""
        self.prepare()
        self.start()
        self.commit("a.txt")
        self.stop_with(self.transcript([assistant("m1", output=100_000)]))
        denied = self.hook("protect-paths.py", {"tool_name": "Write", "session_id": "s1", "cwd": str(self.project),
                                                "tool_input": {"file_path": str(self.project / "package-lock.json")}})
        self.assertIn("deny", denied.stdout.decode("utf-8"))
        for command, extra, expect in (("agent", ["--base", "main"], "- 도구: claude-code 1\n"),
                                       ("sessions", ["--base", "main"], "- 세션: 1 (claude-code 1)"),
                                       ("events", [], "- 차단(block): 1\n")):
            with self.subTest(command=command):
                result = self.in_repo(command, *extra)
                code, out, err = run_cli([command, "summary", str(self.project), *extra])
                self.assertEqual((result.returncode, code), (0, 0), result.stderr + err)
                self.assertIn(expect, out)
                self.assertEqual(result.stdout, out)
                self.assertEqual(result.stderr, err)
        # 관리 디렉터리에 __pycache__를 남기지 않아 check가 여분으로 보지 않는다
        code, out, err = run_cli(["check", str(self.project)])
        self.assertEqual(code, 0, out + err)

    def test_fallbacks_match(self):
        """키트처럼 기준을 구하지 못하면 커밋 비교를 빼고, 깨진 pricing이면 기본값으로 요약한다"""
        self.prepare("gitlab")
        config = json.loads((self.project / "harness.json").read_text(encoding="utf-8"))
        config["pricing"] = {"models": "x"}
        (self.project / "harness.json").write_text(json.dumps(config), encoding="utf-8")
        result = self.in_repo("agent", "--base", "origin/nope")
        code, out, err = run_cli(["agent", "summary", str(self.project), "--base", "origin/nope"])
        self.assertEqual((result.returncode, code), (0, 0))
        self.assertEqual(result.stdout, out)
        self.assertIn("기록 없는 커밋은 세지 않았다", result.stderr)
        self.assertIn("기본값(기준 origin/main, 키트 단가)으로 요약한다", result.stderr)
        self.assertIn("기본값(기준 origin/main, 키트 단가)으로 요약한다", err)

    def test_config_checks_match_kit(self):
        """#123 리뷰 F3: 키트와 저장소 안이 같은 설정 검사를 쓴다(알 수 없는 키는 둘 다 무시, 틀린 기준 브랜치는 둘 다 기본값)"""
        self.prepare()
        self.commit("a.txt")
        git(self.project, "update-ref", "refs/remotes/origin/main", "HEAD~1")
        git(self.project, "update-ref", "refs/remotes/origin/develop", "HEAD")
        path = self.project / "harness.json"
        base = json.loads(path.read_text(encoding="utf-8"))
        for change, expect, warned in (({"foo": 1, "integration_branch": "develop"}, "- 기록 없는 커밋: 0/0", False),
                                       ({"integration_branch": 5}, "- 기록 없는 커밋: 1/1", True)):
            with self.subTest(change=change):
                path.write_text(json.dumps({**base, **change}), encoding="utf-8")
                result = self.in_repo("agent")
                code, out, err = run_cli(["agent", "summary", str(self.project)])
                self.assertEqual((result.returncode, code), (0, 0), result.stderr + err)
                self.assertIn(expect, out)
                self.assertEqual(result.stdout, out)
                self.assertEqual(result.stderr, err)
                self.assertEqual("기본값(기준 origin/main, 키트 단가)으로 요약한다" in err, warned)

    def test_review_matches_kit(self):
        """T4: review는 리뷰 파일만 읽고 키트 `review summary`와 같은 두 줄, 오류는 종료 2"""
        self.prepare()
        path = self.tmp / "12-review.md"
        path.write_text(STRICT_TABLE, encoding="utf-8")
        result = self.in_repo("review", str(path))
        code, out, err = run_cli(["review", "summary", str(path)])
        self.assertEqual((result.returncode, code), (0, 0), result.stderr)
        self.assertEqual(result.stdout, out)
        self.assertEqual(out, "- 리뷰 발견: 5 (채택 2 · 기각 1 · 후속 1 · 판단 필요 1)\n- 독립 중복 발견: 1\n")
        missing = self.in_repo("review", str(self.tmp / "none.md"))
        self.assertEqual((missing.returncode, missing.stdout), (2, ""))
        self.assertIn("리뷰 파일이 없다", missing.stderr)

    def test_review_save_in_repo(self):
        """#135 T10: 대상 저장소에서 `review plans/<키>-review.md --save`가 키트와 같은 두 줄을 내고 docs/reviews/<키>.md를 쓴다"""
        self.prepare()
        (self.project / "plans").mkdir()
        path = self.project / "plans" / "12-review.md"
        path.write_text(STRICT_TABLE, encoding="utf-8")
        result = self.in_repo("review", str(Path("plans") / "12-review.md"), "--save")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "- 리뷰 발견: 5 (채택 2 · 기각 1 · 후속 1 · 판단 필요 1)\n- 독립 중복 발견: 1\n")
        self.assertEqual((self.project / "docs" / "reviews" / "12.md").read_bytes(), path.read_bytes())

    def test_missing_collect_exits_2(self):
        """T5: 요약이 읽는 복사본이 없으면 종료 2와 다시 생성하라는 안내"""
        self.prepare()
        (self.project / ".harness" / "metrics" / "collect.py").unlink()
        for args in (["agent"], ["events"], ["review", "x.md"]):
            with self.subTest(args=args):
                result = self.in_repo(*args)
                self.assertEqual((result.returncode, result.stdout), (2, ""))
                self.assertIn("collect.py", result.stderr)
                self.assertIn("init <저장소> --force", result.stderr)

    def test_missing_sibling_exits_2(self):
        """#123 리뷰 F6: collect.py가 함께 읽는 classify_ci.py가 없어도 같은 안내와 종료 2"""
        self.prepare()
        (self.project / ".harness" / "metrics" / "classify_ci.py").unlink()
        result = self.in_repo("agent")
        self.assertEqual((result.returncode, result.stdout), (2, ""))
        self.assertIn("classify_ci.py", result.stderr)
        self.assertIn("init <저장소> --force", result.stderr)


if __name__ == "__main__":
    unittest.main()
