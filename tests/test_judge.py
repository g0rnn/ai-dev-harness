"""`harness judge`와 hooks 공통 판정 함수 테스트."""

import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import process_support  # noqa: E402,F401 — test-only Windows child launch policy
from tempdirs import remove_tree  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("harness", ROOT / "bin" / "harness.py")
harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)
common = harness.hooks_common


def judge(paths, areas=()):
    return common.judge(paths, {"areas": list(areas)})


def tiers(result):
    return {f["path"]: (f["tier"], f["rule"]) for f in result["files"]}


class JudgeTest(unittest.TestCase):
    def test_docs_only_is_lite(self):
        result = judge(["README.md", "docs/a.txt", "core/templates/x.md"])
        self.assertEqual(result["tier"], "lite")
        self.assertEqual(tiers(result)["README.md"], ("lite", "docs:*.md"))
        self.assertEqual(tiers(result)["docs/a.txt"], ("lite", "docs:/docs/"))

    def test_nested_docs_dir_is_not_doc(self):
        self.assertEqual(judge(["src/docs/a.txt"])["tier"], "standard")

    def test_tests_only_is_lite(self):
        paths = ["tests/test_x.py", "web/src/a.test.ts", "app/src/test/java/A.java", "pkg/b_test.go"]
        result = judge(paths)
        self.assertEqual(result["tier"], "lite")
        for path in paths:
            self.assertTrue(tiers(result)[path][1].startswith("test_paths:"), path)

    def test_default_strict(self):
        for path in ["package-lock.json", "web/yarn.lock", ".github/workflows/ci.yml", ".gitlab-ci.yml",
                     "db/migrations/1.sql", "src/main/resources/db/migration/V1__a.sql", "Jenkinsfile"]:
            with self.subTest(path=path):
                result = judge([path])
                self.assertEqual(result["tier"], "strict")
                self.assertTrue(result["files"][0]["rule"].startswith("trigger_paths.strict:"))

    def test_nested_workflows_not_default_strict(self):
        self.assertEqual(judge(["sub/.github/workflows/ci.yml"])["tier"], "standard")

    def test_other_is_standard(self):
        self.assertEqual(tiers(judge(["src/app.py"])), {"src/app.py": ("standard", "default")})

    def test_area_rules_relative_and_replace_default(self):
        area = {"dir": "backend", "verify": ["t"], "trigger_paths": {"strict": ["/src/auth/"]}}
        result = judge(["backend/src/auth/A.java", "backend/package-lock.json", "src/auth/B.java"], [area])
        got = tiers(result)
        self.assertEqual(got["backend/src/auth/A.java"], ("strict", "trigger_paths.strict:/src/auth/"))
        self.assertEqual(got["backend/package-lock.json"], ("standard", "default"))
        self.assertEqual(got["src/auth/B.java"], ("standard", "default"))  # 영역 밖은 공통 기본
        self.assertEqual(result["files"][0]["area"], "backend")
        self.assertIsNone(result["files"][2]["area"])

    def test_area_without_rules_uses_default(self):
        result = judge(["backend/package-lock.json"], [{"dir": "backend", "verify": ["t"]}])
        self.assertEqual(result["tier"], "strict")

    def test_deepest_area_owns(self):
        areas = [{"dir": "a", "verify": ["t"], "trigger_paths": {"strict": ["x"]}},
                 {"dir": "a/b", "verify": ["t"], "trigger_paths": {"strict": []}}]
        result = judge(["a/b/x", "a/x"], areas)
        self.assertEqual(tiers(result), {"a/b/x": ("standard", "default"),
                                         "a/x": ("strict", "trigger_paths.strict:x")})
        self.assertEqual(result["files"][0]["area"], "a/b")

    def test_area_without_triggers_uses_default_triggers(self):
        """Codex P2(PR #34): 영역 문서는 triggers가 없으면 기본 트리거를 렌더한다. 판정도 같은 문장으로 사람 확인을 낸다."""
        result = judge(["backend/a.py"], [{"dir": "backend", "verify": ["t"]}])
        self.assertEqual([item["trigger"] for item in result["human_check"]], harness.DEFAULT_TRIGGERS)
        self.assertEqual(harness.DEFAULT_TRIGGERS, common.DEFAULT_AREA_TRIGGERS)

    def test_exact_area_path_is_area_owned(self):
        """Codex P2(PR #34): 하위 모듈 커밋 변경은 영역 이름 그대로 나온다. 영역 트리거가 빠지면 안 된다."""
        area = {"dir": "backend", "verify": ["t"], "triggers": ["인가 변경"]}
        result = judge(["backend"], [area])
        self.assertEqual(result["files"][0]["area"], "backend")
        self.assertEqual(result["files"][0]["tier"], "standard")
        self.assertEqual(result["human_check"], [{"area": "backend", "trigger": "인가 변경"}])

    def test_area_prefix_is_directory_boundary(self):
        area = {"dir": "app", "verify": ["t"], "trigger_paths": {"strict": ["*.py"]}}
        self.assertIsNone(judge(["apple/x.py"], [area])["files"][0]["area"])

    def test_standard_pattern_blocks_lite(self):
        area = {"dir": "backend", "verify": ["t"], "trigger_paths": {"standard": ["*.md"]}}
        self.assertEqual(judge(["backend/x.md"], [area])["tier"], "standard")

    def test_custom_test_paths(self):
        area = {"dir": "backend", "verify": ["t"], "test_paths": ["it/"]}
        result = judge(["backend/it/A.java", "backend/tests/B.java"], [area])
        self.assertEqual(tiers(result)["backend/it/A.java"][0], "lite")
        self.assertEqual(tiers(result)["backend/tests/B.java"][0], "standard")

    def test_strict_beats_doc(self):
        self.assertEqual(judge(["migrations/README.md"])["tier"], "strict")

    def test_max_tier_and_empty(self):
        self.assertEqual(judge(["README.md", "tests/a.py", "src/a.py", "go.sum"])["tier"], "strict")
        self.assertEqual(judge(["README.md", "src/a.py"])["tier"], "standard")
        empty = judge([])
        self.assertEqual((empty["tier"], empty["files"], empty["human_check"]), ("lite", [], []))

    def test_case_insensitive(self):
        self.assertEqual(judge(["Package-Lock.JSON"])["tier"], "strict")

    def test_human_check_lists_area_triggers(self):
        areas = [{"dir": "backend", "verify": ["t"], "triggers": ["인증·인가", "트랜잭션 경계"]},
                 {"dir": "web", "verify": ["t"], "triggers": ["접근성"]}]
        result = judge(["backend/a.java", "backend/b.java", "README.md"], areas)
        self.assertEqual(result["human_check"], [{"area": "backend", "trigger": "인증·인가"},
                                                 {"area": "backend", "trigger": "트랜잭션 경계"}])
        self.assertEqual(result["tier"], "standard")

    def test_spec_and_api_files_not_lite(self):
        """분리 리뷰 F3: API 명세 파일이 테스트로 분류돼 경량이 되면 안 된다."""
        for path in ["spec/openapi.yaml", "api/openapi.spec.yaml", "src/a.test.yaml"]:
            with self.subTest(path=path):
                self.assertEqual(judge([path])["tier"], "standard")
        for path in ["web/a.spec.ts", "web/b.test.jsx", "spec/models/user_spec.rb"]:
            with self.subTest(path=path):
                self.assertEqual(judge([path])["tier"], "lite")

    def test_more_default_strict(self):
        """분리 리뷰 F4: 흔한 lock 파일과 마이그레이션 경로."""
        for path in ["Gemfile.lock", "php/composer.lock", "Pipfile.lock", "npm-shrinkwrap.json", "bun.lockb",
                     "db/migrate/20240101_add.rb", "src/main/resources/db/changelog/1.xml"]:
            with self.subTest(path=path):
                self.assertEqual(judge([path])["tier"], "strict")

    def test_path_normalization(self):
        result = judge([".\\backend\\x.java", "./README.md", "README.md", "", "  "],
                       [{"dir": "backend", "verify": ["t"]}])
        self.assertEqual([f["path"] for f in result["files"]], ["backend/x.java", "README.md"])
        self.assertEqual(result["files"][0]["area"], "backend")


class RootJudgeTest(unittest.TestCase):
    """최상위 judge(결정표 9장 Q1 b): 영역 밖 파일에 적용하는 규칙."""

    def test_root_rules_replace_default_for_files_outside_areas(self):
        config = {"judge": {"trigger_paths": {"strict": ["/infra/"]}, "test_paths": ["/it/"],
                            "triggers": ["인가 변경"]},
                  "areas": [{"dir": "backend", "verify": ["t"], "triggers": ["영역 트리거"]}]}
        result = common.judge(["infra/a.tf", "package-lock.json", "it/x.sh", "backend/package-lock.json"], config)
        self.assertEqual(tiers(result), {"infra/a.tf": ("strict", "trigger_paths.strict:/infra/"),
                                         "package-lock.json": ("standard", "default"),
                                         "it/x.sh": ("lite", "test_paths:/it/"),
                                         "backend/package-lock.json": ("strict", "trigger_paths.strict:package-lock.json")})
        self.assertEqual(result["human_check"], [{"area": None, "trigger": "인가 변경"},
                                                 {"area": "backend", "trigger": "영역 트리거"}])

    def test_root_triggers_only_when_outside_file_changed(self):
        config = {"judge": {"triggers": ["인가 변경"]},
                  "areas": [{"dir": "backend", "verify": ["t"], "triggers": ["영역 트리거"]}]}
        self.assertEqual(common.judge(["backend/a.py"], config)["human_check"],
                         [{"area": "backend", "trigger": "영역 트리거"}])

    def test_invalid_root_judge_rejected(self):
        base = {"project_name": "d", "platform": "github", "tracker": "github",
                "default_branch": "main", "integration_branch": "main"}
        for block in ([], {"trigger_path": {}}, {"trigger_paths": {"lite": []}}, {"test_paths": [""]},
                      {"triggers": []}, {"triggers": [1]}, {"dir": "x"}):
            with self.subTest(block=block), self.assertRaises(harness.HarnessError):
                harness.validate_config({**base, "judge": block}, "test")
        harness.validate_config({**base, "judge": {"trigger_paths": {"strict": []}, "test_paths": [],
                                                   "triggers": ["x"]}}, "test")


class KitSelfJudgeTest(unittest.TestCase):
    """결정 D-7: 키트 harness.json의 judge가 docs/contributing.md 판정표와 맞는지. 문서표가 원본이다."""

    EXPECTED = {
        # 엄격: hooks 차단·수정 로직, 보안 검사 조각·단계·러너
        "core/hooks/protect-paths.py": "strict", "core/hooks/stop-verify.py": "strict",
        ".claude/hooks/stop-verify.py": "strict", ".claude/settings.json": "strict",
        # #82: 종료 검증 기준선. 무엇을 검증할지 정하므로 차단 로직의 일부다
        "core/hooks/session-start.py": "strict", ".claude/hooks/session-start.py": "strict",
        # #96: 세션 종료 기록만 남긴다. 무엇도 막거나 검증 대상을 바꾸지 않는다
        "core/hooks/session-end.py": "standard", ".claude/hooks/session-end.py": "standard",
        "core/ci/gitlab/secret-detection.yml": "strict", "core/ci/gitlab/sast.yml": "strict",
        "core/ci/gitlab/dependency-audit.yml": "strict", "core/ci/gitlab/image-scan.yml": "strict",
        ".github/workflows/security.yml": "strict", ".github/scripts/run_fragment.py": "strict",
        # 표준: CLI, 템플릿, 보안이 아닌 CI 조각·워크플로, 생성 파일
        "bin/harness.py": "standard", "core/templates/AGENTS.md": "standard",
        "core/hooks/harness_common.py": "standard", ".claude/hooks/harness_common.py": "standard",
        ".github/workflows/ci.yml": "standard", ".github/scripts/rehearse.py": "standard",
        ".github/scripts/rehearse_profiles.py": "standard",
        ".github/scripts/observability_smoke.py": "standard",
        "core/metrics/classify_ci.py": "standard", "AGENTS.md": "standard",
        "docs/templates/plan.md": "standard", "docs/secret-environment-variables.md": "standard",
        "core/ci/README.md": "standard", "harness.json": "standard", "README.md": "standard",
        "core/templates/review-perspectives.md": "standard",
        "profiles/README.md": "standard", "core/hooks/README.md": "standard",
        "core/hooks/settings.template.json": "strict", ".claude/hooks/protect-paths.py": "strict",
        ".github/PULL_REQUEST_TEMPLATE.md": "standard",
        # M2-2 MR 본문 lint: 보안 검사가 아닌 CI 조각과 그 모듈(결정표 1장 표준)
        "core/ci/gitlab/mr-lint.yml": "standard", "core/ci/mr-lint/mr_lint.py": "standard",
        "core/ci/gitlab/verify.yml": "standard",
        # M4-2 측정 예약 파이프라인 조각과 주간 요약 발송: 보안 검사가 아닌 CI 조각·측정 스크립트(결정표 1장 표준)
        "core/ci/gitlab/metrics.yml": "standard", "core/metrics/notify.py": "standard",
        # 경량: docs/ 아래 손으로 관리하는 문서, 테스트
        "docs/roadmap.md": "lite", "docs/contributing.md": "lite", "docs/install.md": "lite",
        "tests/test_judge.py": "lite", "tests/fixtures/classify_ci/jest.log": "lite",
    }

    # 엄격 판정이 걸린 디렉터리. 새 파일이 생기면 EXPECTED에 판정을 적어야 이 테스트가 통과한다
    WATCHED_DIRS = ("core/hooks/", "core/ci/gitlab/", ".github/workflows/", ".github/scripts/", ".claude/hooks/")

    def config(self):
        return harness.load_config(ROOT / "harness.json")

    def test_kit_paths_match_contributing_table(self):
        result = common.judge(list(self.EXPECTED), self.config())
        self.assertEqual({f["path"]: f["tier"] for f in result["files"]}, self.EXPECTED)

    def test_new_files_in_watched_dirs_need_a_decision(self):
        """리뷰 F9: 보안 조각·hook·워크플로가 새로 생기면 표준으로 조용히 떨어지지 않게 판정을 명시한다."""
        tracked = subprocess.run(["git", "ls-files", *self.WATCHED_DIRS], cwd=ROOT, capture_output=True,
                                 check=True).stdout.decode("utf-8").split()
        missing = [path for path in tracked if path not in self.EXPECTED and not path.endswith("README.md")]
        self.assertEqual(missing, [], "docs/contributing.md 판정표를 보고 EXPECTED와 harness.json judge에 추가한다")

    def test_every_strict_rule_is_exercised(self):
        """리뷰 F10: 엄격 규칙을 지우면 어느 경로든 판정이 바뀌어 실패해야 한다."""
        config = self.config()
        result = common.judge([p for p, tier in self.EXPECTED.items() if tier == "strict"], config)
        used = {f["rule"] for f in result["files"]}
        for pattern in config["judge"]["trigger_paths"]["strict"]:
            self.assertIn(f"trigger_paths.strict:{pattern}", used, pattern)

    def test_human_check_lists_unpathable_strict_conditions(self):
        """리뷰 F8·F10: 경로로 가를 수 없는 엄격 조건은 사람 확인 문장으로 나와야 한다."""
        triggers = " ".join(item["trigger"] for item in common.judge(["bin/harness.py"], self.config())["human_check"])
        for name in ("core/hooks/harness_common.py", ".github/workflows/ci.yml", "rehearse.py", "harness.json 스키마"):
            self.assertIn(name, triggers)

    def test_contributing_strict_row_names_the_rule_targets(self):
        """판정표 엄격 행이 바뀌면 이 테스트로 harness.json judge도 같이 고친다."""
        text = (ROOT / "docs" / "contributing.md").read_text(encoding="utf-8")
        strict_row = next(line for line in text.splitlines() if line.startswith("| 엄격 |"))
        for name in ("core/hooks/", "core/ci/", ".github/workflows/", ".github/scripts/run_fragment.py"):
            self.assertIn(name, strict_row)

    def test_generated_self_files_are_not_lite(self):
        """자기 적용 생성 파일을 고치는 일은 템플릿 변경이라 경량이 아니다."""
        config = harness.load_config(ROOT / "harness.json")
        generated = [entry["dest"] for entry in harness.load_manifest() if entry.get("self")]
        result = common.judge(generated, config)
        self.assertNotIn("lite", {f["tier"] for f in result["files"]}, result["files"])


def run(argv, stdin=None):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err), patch("sys.stdin", io.StringIO(stdin or "")):
        code = harness.main(argv)
    return code, out.getvalue(), err.getvalue()


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   cwd=cwd, check=True, capture_output=True)


class JudgeCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-judge-"))
        self.repo = self.tmp / "repo"
        code, _, _ = run(["init", str(self.repo), "--platform", "github", "--tracker", "github",
                          "--integration-branch", "main"])
        self.assertEqual(code, 0)

    def tearDown(self):
        remove_tree(self.tmp)

    def write(self, rel, text="x\n"):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def commit_all(self, message="base"):
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)

    def judge_json(self, *extra, stdin=None):
        code, out, err = run(["judge", str(self.repo), "--json", *extra], stdin=stdin)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_git_base(self):
        self.write("src/old.py")
        self.write("src/keep.py")
        self.commit_all()
        git(self.repo, "checkout", "-q", "-b", "feature")
        git(self.repo, "mv", "src/old.py", "src/new.py")
        git(self.repo, "commit", "-q", "-m", "rename")
        git(self.repo, "checkout", "-q", "main")
        self.write("main-only.py")                      # 기준 브랜치에서만 생긴 변경은 빠져야 한다
        git(self.repo, "add", "main-only.py")
        git(self.repo, "commit", "-q", "-m", "main moves on")
        git(self.repo, "checkout", "-q", "feature")
        self.write("src/keep.py", "changed\n")          # 커밋하지 않은 수정
        self.write("package-lock.json")                 # 추적 안 된 새 파일
        result = self.judge_json("--base", "main")
        self.assertEqual({f["path"] for f in result["files"]},
                         {"src/old.py", "src/new.py", "src/keep.py", "package-lock.json"})
        self.assertEqual(result["tier"], "strict")
        self.assertRegex(result["base"], r"^[0-9a-f]{40}$")

    def test_target_below_git_top_level(self):
        """Codex P2(PR #34): 대상이 git 최상위의 하위 디렉터리면 대상 기준 경로만, 대상 밖 변경은 빼고 본다."""
        top = self.tmp / "mono"
        service = top / "service"
        code, _, _ = run(["init", str(service), "--platform", "github", "--tracker", "github",
                          "--integration-branch", "main"])
        self.assertEqual(code, 0)
        run(["init", str(service), "--area", "backend", "--verify-cmd", "make test"])
        config = json.loads((service / "harness.json").read_text(encoding="utf-8"))
        config["areas"][0]["trigger_paths"] = {"strict": ["/src/auth/"]}
        (service / "harness.json").write_text(json.dumps(config), encoding="utf-8")
        (top / "sibling").mkdir()
        (top / "sibling" / "a.py").write_text("x\n", encoding="utf-8")
        git(top, "init", "-q", "-b", "main")
        git(top, "add", "-A")
        git(top, "commit", "-q", "-m", "base")
        (service / "backend" / "src" / "auth").mkdir(parents=True)
        (service / "backend" / "src" / "auth" / "x.py").write_text("x\n", encoding="utf-8")
        git(top, "add", "-A")
        git(top, "commit", "-q", "-m", "auth")
        (top / "sibling" / "a.py").write_text("changed\n", encoding="utf-8")  # 대상 밖 수정
        (top / "sibling" / "new.py").write_text("x\n", encoding="utf-8")      # 대상 밖 새 파일
        code, out, err = run(["judge", str(service), "--json", "--base", "HEAD~1"])
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual({f["path"] for f in result["files"]}, {"backend/src/auth/x.py"})
        self.assertEqual(result["tier"], "strict")

    def test_default_base_needs_origin(self):
        self.commit_all()
        code, _, err = run(["judge", str(self.repo)])
        self.assertEqual(code, 2)
        self.assertIn("origin/main", err)

    def test_files_and_json(self):
        self.write("list.txt", "README.md\nsrc/a.py\n\n")
        result = self.judge_json("--files", str(self.repo / "list.txt"))
        self.assertEqual(set(result), {"base", "tier", "files", "human_check"})
        self.assertIsNone(result["base"])
        self.assertEqual(result["tier"], "standard")
        self.assertEqual(set(result["files"][0]), {"path", "area", "tier", "rule"})
        self.assertEqual(self.judge_json("--files", "-", stdin="docs/x.md\n")["tier"], "lite")

    def test_git_quoted_paths_from_pipe(self):
        """분리 리뷰 F1: git diff --name-only가 따옴표로 감싼 비ASCII 경로도 판정한다."""
        git(self.repo, "init", "-q", "-b", "main")
        self.write(".github/workflows/배포.yml")
        self.write("README.md")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        empty_tree = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
        names = subprocess.run(["git", "-c", "core.quotepath=true", "diff", "--name-only", empty_tree, "HEAD"],
                               cwd=self.repo, capture_output=True, check=True).stdout.decode("utf-8")
        self.assertIn('"', names)  # git 기본 설정은 비ASCII 경로를 따옴표로 감싼다
        result = self.judge_json("--files", "-", stdin=names)
        self.assertIn(".github/workflows/배포.yml", {f["path"] for f in result["files"]})
        self.assertEqual(result["tier"], "strict")

    def test_unquote_git_path(self):
        self.assertEqual(harness.unquote_git_path('"a/\\354\\240\\225 b\\t.md"'), "a/정 b\t.md")
        self.assertEqual(harness.unquote_git_path('"say \\"hi\\".md"'), 'say "hi".md')
        self.assertEqual(harness.unquote_git_path("plain.md"), "plain.md")
        self.assertEqual(harness.unquote_git_path('"bad\\q"'), '"bad\\q"')

    def test_file_list_encoding(self):
        """분리 리뷰 F2: BOM은 무시하고 UTF-8이 아니면 종료 2."""
        listing = self.repo / "list.txt"
        listing.write_bytes(b"\xef\xbb\xbfpackage-lock.json\n")
        self.assertEqual(self.judge_json("--files", str(listing))["tier"], "strict")
        listing.write_bytes("package-lock.json\n".encode("utf-16"))
        code, _, err = run(["judge", str(self.repo), "--files", str(listing)])
        self.assertEqual(code, 2)
        self.assertIn("UTF-8", err)

    def test_base_option_like_rejected(self):
        self.commit_all()
        code, _, err = run(["judge", str(self.repo), "--base=--output=x"])
        self.assertEqual(code, 2)

    def test_human_output(self):
        run(["init", str(self.repo), "--area", "backend", "--verify-cmd", "make test"])
        code, out, _ = run(["judge", str(self.repo), "--files", "-"], stdin="backend/a.py\nREADME.md\n")
        self.assertEqual(code, 0)
        self.assertIn("판정: standard (표준)", out)
        self.assertIn("backend/a.py [backend]  default", out)
        self.assertIn("사람 확인 필요", out)
        self.assertIn(harness.DEFAULT_TRIGGERS[0], out)

    def test_saved_area_defaults_apply(self):
        run(["init", str(self.repo), "--area", "backend", "--verify-cmd", "make test"])
        result = self.judge_json("--files", "-", stdin="backend/gradle.lockfile\nbackend/src/test/A.java\n")
        self.assertEqual([f["tier"] for f in result["files"]], ["strict", "lite"])

    def test_errors(self):
        self.commit_all()
        code, _, err = run(["judge", str(self.repo), "--base", "no-such-ref"])
        self.assertEqual(code, 2)
        self.assertIn("no-such-ref", err)
        code, _, _ = run(["judge", str(self.repo), "--files", str(self.repo / "missing.txt")])
        self.assertEqual(code, 2)
        config = json.loads((self.repo / "harness.json").read_text(encoding="utf-8"))
        config["areas"] = [{"dir": "backend", "verify": ["t"], "trigger_paths": {"lite": []}}]
        (self.repo / "harness.json").write_text(json.dumps(config), encoding="utf-8")
        code, _, err = run(["judge", str(self.repo), "--files", "-"], stdin="a\n")
        self.assertEqual(code, 2)
        self.assertIn("trigger_paths", err)

    def test_base_and_files_exclusive(self):
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            harness.main(["judge", str(self.repo), "--base", "x", "--files", "-"])


if __name__ == "__main__":
    unittest.main()
