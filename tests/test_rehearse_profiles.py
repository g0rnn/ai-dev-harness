"""프로필 리허설 스크립트(.github/scripts/rehearse_profiles.py, M3-5) 테스트(plans/56.md T4).

Gradle·npm 빌드는 CI `profiles` job이 맡는다(결정 H-4). 여기서는 시나리오 표, fixture 모양, 외부 도구 없이 도는
준비 단계(fixture 복사 → init → scaffold → 조각 붙이기)와 렌더된 영역 검증 명령, workflow job 정의를 본다.
"""

import importlib.util
import io
import json
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import process_support  # noqa: E402,F401 — test-only Windows child launch policy

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / ".github" / "scripts" / "rehearse_profiles.py"
FIXTURES = ROOT / "tests" / "fixtures" / "profiles"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rp = load("rehearse_profiles", SCRIPT)
harness = load("harness_rehearse_profiles", ROOT / "bin" / "harness.py")

# 영역 검증 명령은 저장소 루트에서 실행된다(stop-verify, 결정 Q2 답 (a)). 프로필이 렌더한 명령의 기대 모양
ROOT_COMMANDS = {
    "spring-java": [r"cd backend && \./gradlew build"],
    "react-ts": [rf"npm --prefix frontend run {script}" for script in ("lint", "format:check", "test:run", "build")],
    "android-kotlin": [r"android/gradlew -p android :app:check", r"android/gradlew -p android :core:check",
                       r"android/gradlew -p android :feature:meal:check :feature:workout:check",
                       r"android/gradlew -p android :wear:check", r"android/gradlew -p android :konsist-test:test"],
}


def job_lines(body, name):
    """ci.yml에서 job 하나의 본문 줄(주석·빈 줄·줄 끝 주석 제외)."""
    block = re.search(rf"^  {re.escape(name)}:\n((?:(?:    .*)?\n)+)", body, re.M).group(1)
    return [line.split("  #")[0].rstrip() for line in block.splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


class ScenarioTableTest(unittest.TestCase):
    def test_every_profile_has_scenario_and_fixture(self):
        """모든 프로필이 어느 시나리오의 영역에 쓰인다. android-konsist는 android-kotlin 시나리오의 영역이다(#59)."""
        profiles = {path.parent.name for path in (ROOT / "profiles").glob("*/profile.json")}
        table = rp.scenarios()
        used = {area.get("profile", name) for name, scenario in table.items() for area in scenario["areas"]}
        self.assertEqual(used, profiles)
        self.assertLessEqual(set(table), profiles)
        self.assertEqual({path.name for path in FIXTURES.iterdir() if path.is_dir()}, set(table))
        for name, scenario in table.items():
            with self.subTest(name):
                self.assertTrue(scenario["scaffold"])
                area_profiles = {a["dir"]: a.get("profile", name) for a in scenario["areas"]}
                for area, kind, _item, _vars in scenario["scaffold"]:
                    self.assertIn(area, area_profiles)
                    profile = json.loads((ROOT / "profiles" / area_profiles[area] / "profile.json")
                                         .read_text(encoding="utf-8"))
                    self.assertIn(kind, profile["scaffold"])
                self.assertTrue(scenario["negatives"])  # 음성 시나리오가 없으면 규칙이 꺼져도 모른다(H-3)
                for negative in scenario["negatives"]:
                    self.assertTrue(negative["files"] and negative["expect"], negative["name"])
                    for rel in negative["files"]:
                        self.assertFalse((FIXTURES / name / rel).exists(), rel)  # 음성 파일은 실행 중에만 만든다
                for rel in scenario["gradle_roots"]:
                    self.assertTrue(any((FIXTURES / name / rel / f).is_file()
                                        for f in ("settings.gradle", "settings.gradle.kts")), rel)

    def test_negative_scenarios_cover_decided_rules(self):
        """H-3·A-12: controller→repository, mocks import, alt 없는 img. #59: ViewModel의 Retrofit, feature의 Room,
        contentDescription. #59 리뷰 F2: feature domain→ui 계층 의존. spring-java no-cross-domain-persistence:
        다른 기능의 repository를 쓰는 service."""
        names = {n["name"] for scenario in rp.scenarios().values() for n in scenario["negatives"]}
        self.assertEqual(names, {"controller-calls-repository", "service-uses-other-domain-repository",
                                 "page-imports-mocks", "img-without-alt",
                                 "viewmodel-imports-retrofit", "feature-imports-room", "domain-imports-ui",
                                 "image-without-content-description"})

    def test_cross_domain_negative_names_its_rule(self):
        """#61 리뷰 F1: 음성은 규칙의 실패 문구까지 요구해야 다른 규칙이 대신 잡아 통과하는 일이 없다."""
        negative = next(n for n in rp.scenarios()["spring-java"]["negatives"]
                        if n["name"] == "service-uses-other-domain-repository")
        template = (ROOT / "profiles" / "spring-java" / "conventions" / "FixedRulesArchitectureTest.java").read_text(
            encoding="utf-8")
        rule_texts = [text for text in negative["expect"] if text in template]
        self.assertEqual(rule_texts, ["another domain's repository or entity"])

    def test_android_scenario_matches_adr_modules(self):
        """#59 T7: 영역 5개(app·core·feature·wear + konsist-test), 스캐폴드, 음성(Konsist 3개 + Lint 1개)이 규칙·파일을 가리킨다."""
        scenario = rp.scenarios()["android-kotlin"]
        self.assertEqual([(a["dir"], a.get("profile", "android-kotlin")) for a in scenario["areas"]], [
            ("android/app", "android-kotlin"), ("android/core", "android-kotlin"),
            ("android/feature", "android-kotlin"), ("android/wear", "android-kotlin"),
            ("android/konsist-test", "android-konsist")])
        self.assertEqual([(area, kind) for area, kind, _item, _vars in scenario["scaffold"]],
                         [("android/feature", "screen"), ("android/feature", "repository"),
                          ("android/wear", "wear-screen")])
        negatives = {n["name"]: n for n in scenario["negatives"]}
        rules = (ROOT / "profiles/android-konsist/conventions/HarnessFixedRulesTest.kt").read_text(encoding="utf-8")
        for name, test_name in (("viewmodel-imports-retrofit", "viewModelUsesRepositoryOnly"),
                                ("feature-imports-room", "featureDoesNotUseRoom")):
            with self.subTest(name):
                negative = negatives[name]
                self.assertEqual(negative["expect"][0], test_name)
                self.assertIn(f"fun {test_name}()", rules)
                (rel, _content), = negative["files"].items()
                self.assertTrue(rel.startswith("android/feature/meal/src/main/java/"), rel)
                self.assertIn(negative["expect"][1], rel)  # 출력에서 위반 파일 이름을 찾는다
        self.assertIn("import retrofit2.Retrofit", rp.ANDROID_RETROFIT_VIEWMODEL)
        self.assertIn("ViewModel()", rp.ANDROID_RETROFIT_VIEWMODEL)
        self.assertIn("import androidx.room.Entity", rp.ANDROID_ROOM_ENTITY)
        # 음성 import가 컴파일되어야 Konsist까지 간다: :core가 Retrofit·Room을 api로 노출하고 feature가 :core에 의존한다
        android = FIXTURES / "android-kotlin" / "android"
        core = (android / "core/build.gradle.kts").read_text(encoding="utf-8")
        self.assertIn("api(libs.retrofit)", core)
        self.assertIn("api(libs.androidx.room.runtime)", core)
        self.assertIn('implementation(project(":core"))',
                      (android / "feature/meal/build.gradle.kts").read_text(encoding="utf-8"))
        # #59 리뷰 F2: allow에 없는 계층 의존(domain→ui)은 계층 규칙에서만 실패한다
        layered = negatives["domain-imports-ui"]
        self.assertEqual(layered["expect"], ["featureLayersDependOnlyOnAllowedLayers", "WorkoutSessionLabel"])
        layer_test = (ROOT / "profiles/android-konsist/conventions/HarnessLayerArchitectureTest.kt").read_text(
            encoding="utf-8")
        self.assertIn("fun featureLayersDependOnlyOnAllowedLayers()", layer_test)
        (rel, content), = layered["files"].items()
        source = "android/feature/workout/src/main/java/com/example/rehearsal/feature/workout/"
        self.assertEqual(rel, f"{source}domain/WorkoutSessionLabel.kt")
        self.assertIn("package com.example.rehearsal.feature.workout.domain\n", content)
        # 컴파일되는 import: 같은 모듈 fixture의 ViewModel이다. 다른 고정 규칙(이름 *ViewModel·*UseCase·*Screen)에 걸리지 않는다
        self.assertIn("import com.example.rehearsal.feature.workout.ui.session.WorkoutSessionViewModel\n", content)
        self.assertTrue((FIXTURES / "android-kotlin" / f"{source}ui/session/WorkoutSessionViewModel.kt").is_file())
        self.assertIn("val volume: StateFlow<Int>", (FIXTURES / "android-kotlin" /
                                                     f"{source}ui/session/WorkoutSessionViewModel.kt").read_text(
            encoding="utf-8"))
        self.assertIsNone(re.search(r"\b(class|fun) \w*(ViewModel|UseCase|Screen)\b", content))
        self.assertEqual(negatives["image-without-content-description"]["expect"],
                         ["[ContentDescription]", "negative_image.xml"])

    def test_judge_negative_needs_failure_and_reason(self):
        negative = {"name": "x", "files": {}, "expect": ["[no-mocks-import]"]}
        self.assertEqual(rp.judge_negative(negative, 1, "error [no-mocks-import] ..."), [])
        self.assertTrue(rp.judge_negative(negative, 0, "[no-mocks-import]"))  # 통과하면 실패
        self.assertTrue(rp.judge_negative(negative, 1, "npm error ENOENT"))  # 도구 오류는 기대한 실패가 아니다


class FixtureTest(unittest.TestCase):
    def test_no_gradle_wrapper_committed(self):
        """H-2: wrapper jar를 넣지 않고 CI의 고정 버전 Gradle로 만든다."""
        for path in FIXTURES.rglob("*"):
            self.assertNotIn(path.name, ("gradlew", "gradlew.bat", "gradle-wrapper.jar", "gradle-wrapper.properties"),
                             path)

    def test_versions_are_pinned(self):
        """H-5: 동적 버전(+, latest, ^, ~)을 쓰지 않는다. npm은 lock 파일과 함께 둔다."""
        frontend = FIXTURES / "react-ts" / "frontend"
        package = json.loads((frontend / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((frontend / "package-lock.json").read_text(encoding="utf-8"))
        for section in ("dependencies", "devDependencies"):
            for name, version in package[section].items():
                self.assertRegex(version, r"^\d+\.\d+\.\d+$", name)
                self.assertEqual(lock["packages"][f"node_modules/{name}"]["version"], version, name)
        self.assertEqual(lock["packages"][""]["devDependencies"], package["devDependencies"])
        # KSP는 Kotlin 버전에 맞춘다(<kotlin>-<ksp>). Hilt 2.59+ Gradle 플러그인은 AGP 9를 요구한다
        catalog = (FIXTURES / "android-kotlin/android/gradle/libs.versions.toml").read_text(encoding="utf-8")
        versions = dict(re.findall(r'^([\w-]+) = "([^"]+)"$', catalog, re.M))
        self.assertTrue(versions["ksp"].startswith(versions["kotlin"] + "-"), versions["ksp"])
        self.assertTrue(versions["agp"].startswith("8.") and versions["hilt"].startswith("2.57."), versions)
        gradle_files = [*FIXTURES.rglob("*.gradle"), *FIXTURES.rglob("*.gradle.kts"), *FIXTURES.rglob("*.toml")]
        self.assertTrue(gradle_files)
        for path in gradle_files:
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"""version\s*=?\s*['"][^'"]*(\+|latest)""", path)
            self.assertNotRegex(text, r"""['"][\w.-]+:[\w.-]+:[^'"]*\+['"]""", path)

    def test_gradle_version_matches_ci(self):
        body = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(f'gradle-version: "{rp.GRADLE_VERSION}"', body)


class PrepareTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-rehearse-profiles-"))
        self.addCleanup(rp.remove_tree, self.tmp)

    def prepare(self, name):
        repo = self.tmp / name
        rp.prepare(name, repo)
        return repo

    def assert_no_placeholders(self, repo):
        for path in repo.rglob("*"):
            if path.is_file() and path.suffix in (".java", ".kt", ".ts", ".tsx", ".js", ".json", ".md"):
                self.assertNotIn("{{", path.read_text(encoding="utf-8"), path)

    def assert_check_clean(self, repo):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = harness.main(["check", str(repo)])
        self.assertEqual(code, 0, out.getvalue() + err.getvalue())

    def test_spring_java(self):
        repo = self.prepare("spring-java")
        base = repo / "backend/src/main/java/com/example/rehearsal"
        for rel in ("domain/moviereview/controller/MovieReviewController.java",
                    "domain/moviereview/repository/MovieReviewRepository.java"):
            self.assertTrue((base / rel).is_file(), rel)
        tests = repo / "backend/src/test/java/com/example/rehearsal"
        for rel in ("architecture/FixedRulesArchitectureTest.java", "architecture/LayerRulesArchitectureTest.java",
                    "domain/moviereview/controller/MovieReviewControllerTest.java"):
            self.assertTrue((tests / rel).is_file(), rel)
        self.assert_no_placeholders(repo)
        self.assert_check_clean(repo)

    def test_react_ts_pastes_routes(self):
        repo = self.prepare("react-ts")
        src = repo / "frontend/src"
        self.assertTrue((src / "pages/movies/MovieDetailPage/index.tsx").is_file())
        self.assertTrue((src / "api/movieReview.ts").is_file())
        paths = (src / "routes/paths.ts").read_text(encoding="utf-8")
        self.assertIn("  movieDetail: '/movie-detail',\n  // rehearsal:route-pattern", paths)
        self.assertIn("  movieDetail: () => ROUTE_PATTERN.movieDetail,\n  // rehearsal:path", paths)
        router = (src / "routes/router.tsx").read_text(encoding="utf-8")
        self.assertIn("import MovieDetailPage from '../pages/movies/MovieDetailPage';\n// rehearsal:import", router)
        self.assertIn("  { path: ROUTE_PATTERN.movieDetail, element: <MovieDetailPage /> },\n  // rehearsal:route",
                      router)
        self.assertTrue((repo / "frontend/eslint.harness.js").is_file())
        self.assert_no_placeholders(repo)
        self.assert_check_clean(repo)

    def test_android_kotlin_pastes_nav_graphs(self):
        repo = self.prepare("android-kotlin")
        android = repo / "android"
        meal = android / "feature/meal/src/main/java/com/example/rehearsal/feature/meal"
        for rel in ("ui/mealsummary/MealSummaryViewModel.kt", "ui/mealsummary/MealSummaryRoute.kt",
                    "data/MealLogRepository.kt", "data/DefaultMealLogRepository.kt", "di/MealLogRepositoryModule.kt"):
            self.assertTrue((meal / rel).is_file(), rel)
        self.assertTrue((android / "feature/meal/src/test/java/com/example/rehearsal/feature/meal/data/"
                         "FakeMealLogRepository.kt").is_file())
        # feature 화면 조각은 :app의 내비게이션 그래프에 붙는다(ADR-05)
        host = (android / "app/src/main/java/com/example/rehearsal/RehearsalNavHost.kt").read_text(encoding="utf-8")
        self.assertIn("import com.example.rehearsal.feature.meal.ui.mealsummary.MealSummaryRoute\n", host)
        self.assertEqual(host.count("import androidx.navigation.compose.composable\n"), 1)  # 있는 import는 더하지 않는다
        self.assertIn('        composable(route = "meal-summary") {\n            MealSummaryRoute()\n        }\n'
                      "        // rehearsal:destinations", host)
        wear = (android / "wear/src/main/java/com/example/rehearsal/wear/WearNavHost.kt").read_text("utf-8")
        self.assertIn("import com.example.rehearsal.wear.ui.heartrate.HeartRateRoute\n", wear)
        self.assertIn('            composable(route = "heart-rate") {\n                HeartRateRoute()\n', wear)
        # Konsist 컨벤션 파일은 konsist-test 한 곳에만 있다(9장 Q3 (가))
        generated = sorted(p.relative_to(android).as_posix() for p in android.rglob("Harness*Test.kt"))
        arch = "konsist-test/src/test/kotlin/com/example/rehearsal/konsist"
        self.assertEqual(generated, [f"{arch}/HarnessFixedRulesTest.kt", f"{arch}/HarnessLayerArchitectureTest.kt"])
        # 범위 확인 테스트가 통과하려면 app·core·feature·wear마다 생산 소스가 있어야 한다
        for module in ("app", "core", "feature", "wear"):
            self.assertTrue([p for p in (android / module).rglob("*.kt") if "/src/main/" in p.as_posix()], module)
        # 계층 규칙(feature 생산 코드)은 계층마다 파일이 있어야 한다. domain은 두 ViewModel이 공유하는 UseCase
        feature_main = [p for p in (android / "feature").rglob("*.kt") if "/src/main/" in p.as_posix()]
        packages = {p.parent.name for p in feature_main} | {p.parent.parent.name for p in feature_main}
        self.assertTrue({"ui", "data", "domain"} <= packages, packages)
        users = [p.name for p in feature_main if p.name.endswith("ViewModel.kt")
                 and "WorkoutVolumeUseCase" in p.read_text(encoding="utf-8")]
        self.assertEqual(len(users), 2, users)
        settings = (android / "settings.gradle.kts").read_text(encoding="utf-8")
        for module in (":app", ":core", ":feature:meal", ":feature:workout", ":wear", ":konsist-test"):
            self.assertIn(f'"{module}"', settings)
        konsist = (android / "konsist-test/build.gradle.kts").read_text(encoding="utf-8")
        self.assertIn("outputs.upToDateWhen { false }", konsist)  # 음성 시나리오에서 UP-TO-DATE로 건너뛰지 않게
        for module in ("app", "feature/meal", "feature/workout", "wear"):
            build = (android / module / "build.gradle.kts").read_text(encoding="utf-8")
            for line in ("alias(libs.plugins.ksp)", "alias(libs.plugins.hilt)", "ksp(libs.hilt.compiler)"):
                self.assertIn(line, build, module)
        self.assert_no_placeholders(repo)
        self.assert_check_clean(repo)

    def test_verify_commands_run_from_repo_root(self):
        """Q2 답 (a): 렌더된 영역 검증 명령은 저장소 루트에서 실행할 수 있어야 한다(stop-verify와 같은 위치)."""
        for name, expected in ROOT_COMMANDS.items():
            with self.subTest(name):
                repo = self.prepare(name)
                commands = rp.area_verify(repo, rp.scenarios()[name])
                self.assertEqual(len(commands), len(expected), commands)
                for command, pattern in zip(commands, expected):
                    self.assertRegex(command, f"^{pattern}$")

    def test_paste_needs_marker(self):
        path = self.tmp / "router.tsx"
        path.write_text("export const x = 1;\n", encoding="utf-8")
        with self.assertRaises(rp.RehearsalError):
            rp.insert_at_marker(path, "rehearsal:route", ["a"])
        with self.assertRaises(rp.RehearsalError):
            rp.snippet_of("3개 파일을 생성했다")


class MainTest(unittest.TestCase):
    def test_usage(self):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(rp.main(["rehearse_profiles.py"]), 2)
            self.assertEqual(rp.main(["rehearse_profiles.py", "vue"]), 2)
            self.assertEqual(rp.main(["rehearse_profiles.py", "react-ts", "spring-java"]), 2)


class ProfilesJobTest(unittest.TestCase):
    def test_profiles_job(self):
        """H-1·H-5: 필수 체크가 아닌 별도 job, 읽기 권한, 자격 증명 미보존, SHA 고정 액션, 프로필마다 단계 하나."""
        lines = job_lines(WORKFLOW.read_text(encoding="utf-8"), "profiles")
        self.assertEqual(lines, [
            "    runs-on: ubuntu-latest",
            "    timeout-minutes: 45",
            "    concurrency:",
            "      group: profiles-${{ github.event.pull_request.number || github.ref }}",
            "      cancel-in-progress: true",
            "    permissions:",
            "      contents: read",
            "    steps:",
            "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
            "        with:",
            "          persist-credentials: false",
            "      - uses: actions/setup-java@de7274f081f381c8f8158605e0321c36c376e2e6",
            "        with:",
            "          distribution: temurin",
            '          java-version: "21"',
            "          cache: gradle",
            "          cache-dependency-path: |",
            "            tests/fixtures/profiles/**/*.gradle*",
            "            tests/fixtures/profiles/**/libs.versions.toml",
            "      - uses: gradle/actions/setup-gradle@0723195856401067f7a2779048b490ace7a47d7c",
            "        with:",
            f'          gradle-version: "{rp.GRADLE_VERSION}"',
            "          cache-disabled: true",
            "      - uses: actions/setup-node@820762786026740c76f36085b0efc47a31fe5020",
            "        with:",
            '          node-version: "22"',
            "          cache: npm",
            "          cache-dependency-path: tests/fixtures/profiles/react-ts/frontend/package-lock.json",
            "      - name: spring-java",
            "        run: python3 .github/scripts/rehearse_profiles.py spring-java",
            "      - name: react-ts",
            "        if: ${{ !cancelled() }}",
            "        run: python3 .github/scripts/rehearse_profiles.py react-ts",
            "      - name: android-kotlin",
            "        if: ${{ !cancelled() }}",
            "        run: python3 .github/scripts/rehearse_profiles.py android-kotlin",
        ])
        joined = "\n".join(lines)
        for needle in ("secrets.", "token", "TOKEN", "continue-on-error"):
            self.assertNotIn(needle, joined)


if __name__ == "__main__":
    unittest.main()
