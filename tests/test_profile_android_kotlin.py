"""android-kotlin·android-konsist 프로필(M3-4a #51, M3-4b #59): 실제 profiles/를 임시 대상 저장소에 적용한다(plans/59.md T1~T6).

생성된 Kotlin·Konsist 코드의 컴파일·실행은 여기서 하지 않는다(로컬 JDK·SDK 없음). 실제 빌드는 리허설(M3-5)이 맡는다.
"""

import ast
import importlib.util
import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tempdirs import remove_tree  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("harness_android_profile", ROOT / "bin" / "harness.py")
harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)

PROFILE = "android-kotlin"
KONSIST = "android-konsist"
RULES = ("no-global-scope", "viewmodel-no-android-context", "viewmodel-repository-only", "screen-no-viewmodel-param",
         "usecase-shared-by-two-viewmodels", "feature-no-room", "wear-no-network", "app-no-data-layer")
# 규칙 ID → 테스트 함수(리허설 음성 시나리오의 기대 문자열이 이 이름을 쓴다)
RULE_TESTS = {
    "no-global-scope": "fun noGlobalScope()",
    "viewmodel-no-android-context": "fun viewModelDoesNotReferenceAndroidUi()",
    "viewmodel-repository-only": "fun viewModelUsesRepositoryOnly()",
    "screen-no-viewmodel-param": "fun screenDoesNotTakeViewModel()",
    "usecase-shared-by-two-viewmodels": "fun useCaseIsSharedByTwoViewModels()",
    "feature-no-room": "fun featureDoesNotUseRoom()",
    "wear-no-network": "fun wearDoesNotCallNetwork()",
    "app-no-data-layer": "fun appDoesNotDeclareDataLayer()",
}
APP, CORE, FEATURE, WEAR, KONSIST_AREA = ("android/app", "android/core", "android/feature", "android/wear",
                                          "android/konsist-test")
ARCH = f"{KONSIST_AREA}/src/test/kotlin/com/acme/fit/konsist"
FIXED = "HarnessFixedRulesTest.kt"
LAYERS = "HarnessLayerArchitectureTest.kt"
FEATURE_TASKS = ":feature:meal:check :feature:workout:check"


# 리뷰 F1(#59) 근사 검사용: 가장 안쪽 괄호 묶음(안에 빈 묶음만 있는 것)과 연쇄 호출 덩어리
_GROUP = re.compile(r"\((?:[^(){}\[\]]|\(\)|\{\}|\[\])*\)|\{(?:[^(){}\[\]]|\(\)|\{\}|\[\])*\}|"
                    r"\[(?:[^(){}\[\]]|\(\)|\{\}|\[\])*\]")
_CHAIN = re.compile(r"[A-Za-z_]\w*(?:\s*(?:\(\)|\{\}|\[\]))*(?:\s*\??\.\s*[A-Za-z_]\w*(?:\s*(?:\(\)|\{\}|\[\]))*)+")
_LITERAL = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')


def chain_operator_counts(line):
    """한 줄의 연쇄 식마다 연쇄 연산자(`.`·`?.`) 수(ktlint_official `chain-method-continuation`의 근사).

    ktlint 1.5.0은 한 식의 연쇄 연산자가 4개 이상이면 한 줄에 맞아도 연산자마다 줄을 바꾼다(첫 `a.b` 참조도 개수에 든다).
    근사 규칙: 주석 줄·import·package는 보지 않는다. 문자열·문자 리터럴과 줄 끝 주석을 지운다. 가장 안쪽 괄호 묶음
    `(…)`·`{…}`·`[…]`부터 안의 식을 따로 센 뒤 빈 묶음으로 접는다(인자·람다 안의 식은 바깥 연쇄와 별개다). 접은 줄에서
    `식별자(묶음)*`에 `.`·`?.` + `식별자(묶음)*`가 이어지는 덩어리를 연쇄 하나로 보고 연산자를 센다. 여러 줄로 이미 나눈
    연쇄(`.`으로 시작하는 줄)는 그 줄의 덩어리만 보므로 걸리지 않는다. 연산자 결합(`a.b() + c.d()`)은 따로 센다.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith(("//", "*", "/*", "import ", "package ", "@file:")):
        return []
    code = re.sub(r"//.*", "", _LITERAL.sub('""', line))
    counts = []

    def fold(match):
        counts.extend(chain_operator_counts_flat(match.group(0)[1:-1]))
        return match.group(0)[0] + match.group(0)[-1]

    while True:
        folded = _GROUP.sub(fold, code)
        if folded == code:
            break
        code = folded
    return counts + chain_operator_counts_flat(code)


def chain_operator_counts_flat(code):
    return [len(re.findall(r"\??\.", chain.group(0))) for chain in _CHAIN.finditer(code)]


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = harness.main(argv)
    return code, out.getvalue(), err.getvalue()


class AndroidProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-android-"))
        self.addCleanup(remove_tree, self.tmp)
        self.target = self.tmp / "consumer"
        code, _out, err = run(["init", str(self.target), "--platform", "github", "--tracker", "github"])
        self.assertEqual(code, 0, err)

    # --- 도우미 -------------------------------------------------------------

    def init_area(self, area, *extra):
        code, out, err = run(["init", str(self.target), "--area", area, *extra])
        self.assertEqual(code, 0, err)
        return out

    def init_module(self, area, *extra):
        return self.init_area(area, "--profile", PROFILE, "--var", "base_package=com.acme.fit", *extra)

    def init_feature(self):
        return self.init_module(FEATURE, "--var", "module=feature", "--var", f"gradle_tasks={FEATURE_TASKS}")

    def init_wear(self):
        return self.init_area(WEAR, "--profile", PROFILE, "--var", "module=wear",
                              "--var", "base_package=com.acme.fit.wear", "--var", "min_sdk=30")

    def init_konsist(self, *extra):
        return self.init_area(KONSIST_AREA, "--profile", KONSIST, "--var", "base_package=com.acme.fit.konsist", *extra)

    def scaffold(self, *argv):
        return run(["scaffold", *argv, "--target", str(self.target)])

    def config(self):
        return json.loads((self.target / "harness.json").read_text(encoding="utf-8"))

    def save_config(self, config):
        (self.target / "harness.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n",
                                                  encoding="utf-8")

    def area(self, dir_):
        return next(a for a in self.config()["areas"] if a["dir"] == dir_)

    def set_area(self, dir_, **values):
        config = self.config()
        next(a for a in config["areas"] if a["dir"] == dir_).update(values)
        self.save_config(config)

    def read(self, rel):
        return (self.target / rel).read_text(encoding="utf-8")

    def files(self):
        return sorted(p.relative_to(self.target).as_posix() for p in self.target.rglob("*") if p.is_file())

    def check(self):
        code, out, err = run(["check", str(self.target)])
        return code, out + err

    # --- T1 ---------------------------------------------------------------

    def test_profiles_load(self):
        profile, base = harness.load_profile(PROFILE)
        self.assertEqual(base, ROOT / "profiles" / PROFILE)
        self.assertEqual(list(profile["scaffold"]), ["screen", "repository", "usecase", "wear-screen"])
        self.assertNotIn("conventions", profile)  # 컨벤션은 android-konsist 한 곳(Q3 (가))
        self.assertNotIn("layers", profile)
        self.assertEqual(profile["vars"]["gradle_tasks"]["default"], ":{{module}}:check")
        self.assertNotIn("default", profile["vars"]["base_package"])  # 필수 변수
        for kind in ("screen", "repository", "usecase"):
            self.assertNotIn("default", profile["scaffold"][kind]["vars"]["feature"])  # 종류 변수 feature 필수
        self.assertNotIn("vars", profile["scaffold"]["wear-screen"])

        konsist, base = harness.load_profile(KONSIST)
        self.assertEqual(base, ROOT / "profiles" / KONSIST)
        self.assertEqual(tuple(konsist["conventions"]["rules"]), RULES)
        self.assertEqual({entry["kind"] for entry in konsist["conventions"]["files"]}, {"fixed", "configured"})
        self.assertNotIn("scaffold", konsist)
        self.assertEqual(konsist["vars"]["module"]["default"], "konsist-test")
        self.assertEqual({name: konsist["vars"][name]["default"] for name in ("app_dir", "core_dir", "feature_dir",
                                                                             "wear_dir")},
                         {"app_dir": "app", "core_dir": "core", "feature_dir": "feature", "wear_dir": "wear"})
        # ADR-04: ui → data·domain, domain → data, data → 없음
        self.assertEqual(konsist["layers"], {"ui": ["..ui.."], "data": ["..data.."], "domain": ["..domain.."]})
        self.assertEqual(konsist["allow"], {"ui": ["data", "domain"], "data": [], "domain": ["data"]})

    # --- T2 ---------------------------------------------------------------

    def test_init_module_areas_and_konsist_area(self):
        out = self.init_module(APP)
        self.assertIn("android-konsist", out)  # 컨벤션 테스트 영역 안내
        # 리뷰 F3(#59): 잠금·Hilt·ktlint 안내는 gradle_tasks에 적은 모듈마다다
        self.assertIn("의존성 잠금이 필수다: gradle_tasks 에 적은 모듈마다(:app:check)", out)
        self.assertIn("android/gradlew -p android <모듈 경로>:dependencies … --write-locks", out)
        self.assertIn("Hilt·KSP: gradle_tasks 에 적은 모듈마다(:app:check", out)
        self.init_module(CORE, "--var", "module=core")
        out = self.init_feature()
        # feature 영역의 module(feature)은 상위 컨테이너라 잠금·Hilt 대상이 아니다. :feature:<이름> 모듈마다 안내한다
        self.assertIn(f"gradle_tasks 에 적은 모듈마다({FEATURE_TASKS})", out)
        self.assertIn(":feature:<이름>:dependencies 를 모듈마다", out)
        self.assertNotIn(":feature:dependencies", out)
        self.assertNotIn("모듈 feature ", out)
        self.init_wear()
        out = self.init_konsist()
        self.assertIn("android/gradlew -p android :konsist-test:dependencies --write-locks", out)
        self.assertIn('include(":konsist-test")', out)
        self.assertIn("outputs.upToDateWhen { false }", out)
        expected = {APP: ":app:check", CORE: ":core:check", FEATURE: FEATURE_TASKS, WEAR: ":wear:check"}
        for dir_, tasks in expected.items():
            with self.subTest(area=dir_):
                area = self.area(dir_)
                self.assertEqual(area["verify"], [f"android/gradlew -p android {tasks}"])
                strict = area["trigger_paths"]["strict"]
                for pattern in ("**/build.gradle.kts", "**/AndroidManifest.xml", "**/proguard-rules.pro",
                                "*.jks", "*.keystore", "gradle.lockfile"):
                    self.assertIn(pattern, strict)
                self.assertIn("/src/test/", area["test_paths"])
                self.assertEqual(len(area["triggers"]), 6)  # 프로필 5개 + 프로필 설정 변경 트리거
                self.assertEqual(area["triggers"][-1], harness.PROFILE_TRIGGER)
                # ViewModel→Repository·UseCase 2곳 규칙은 Konsist 고정 규칙이 강제하므로 사람 확인 트리거로 두지 않는다
                self.assertFalse(any("UseCase" in t for t in area["triggers"]))
                self.assertEqual((area["layers"], area["allow"], area["disabled_rules"]), ({}, {}, {}))
                # 모듈 영역에는 컨벤션 파일이 없다(Konsist는 konsist-test 한 곳)
                self.assertEqual([f for f in self.files() if f.startswith(f"{dir_}/")], [f"{dir_}/AGENTS.md"])
                self.assertIn(f"android/gradlew -p android {tasks}", self.read(f"{dir_}/AGENTS.md"))
        area = self.area(KONSIST_AREA)
        self.assertEqual(area["verify"], ["android/gradlew -p android :konsist-test:test"])
        self.assertEqual(area["vars"]["feature_dir"], "feature")
        generated = [f for f in self.files() if f.endswith(".kt")]
        self.assertEqual(generated, [f"{ARCH}/{FIXED}", f"{ARCH}/{LAYERS}"])
        for rel in generated:
            text = self.read(rel)
            self.assertNotIn("{{", text)
            self.assertNotIn("}}", text)
            self.assertRegex(text, r"(^|\n)package com\.acme\.fit\.konsist\n")
        fixed = self.read(f"{ARCH}/{FIXED}")
        for line in ('private val appDir = "app"', 'private val coreDir = "core"', 'private val featureDir = "feature"',
                     'private val wearDir = "wear"', "Konsist.scopeFromProduction()"):
            self.assertIn(line, fixed)
        self.assertIn('.startsWith("feature/")', self.read(f"{ARCH}/{LAYERS}"))
        self.assertEqual(self.check()[0], 0)

    def test_konsist_dirs_are_configurable(self):
        self.init_konsist("--var", "feature_dir=features", "--var", "app_dir=phone/app")
        fixed = self.read(f"{ARCH}/{FIXED}")
        self.assertIn('private val featureDir = "features"', fixed)
        self.assertIn('private val appDir = "phone/app"', fixed)
        self.assertIn('.startsWith("features/")', self.read(f"{ARCH}/{LAYERS}"))
        for bad in ("../app", "/app", "a b", "app/"):
            with self.subTest(bad=bad):
                code, _out, _err = run(["init", str(self.target), "--area", KONSIST_AREA, "--force",
                                        "--var", f"app_dir={bad}"])
                self.assertEqual(code, 2)

    def test_scope_checks_every_directory(self):
        """범위가 비면 규칙이 거짓 통과한다. 경로 변수 네 개를 모두 확인하는 테스트는 규칙을 꺼도 남는다."""
        self.init_konsist()
        text = self.read(f"{ARCH}/{FIXED}")
        self.assertIn("fun scopesAreNotEmpty()", text)
        self.assertIn("for (dir in listOf(appDir, coreDir, featureDir, wearDir))", text)
        before_rules = text[:text.index("// harness:rule")]
        self.assertIn("fun scopesAreNotEmpty()", before_rules)

    # --- T3 ---------------------------------------------------------------

    def test_scaffold_feature_screen(self):
        self.init_feature()
        before = self.files()
        code, out, err = self.scaffold(FEATURE, "screen", "MealSummary", "--var", "feature=meal")
        self.assertEqual(code, 0, err)
        src = f"{FEATURE}/meal/src/main/java/com/acme/fit/feature/meal/ui/mealsummary"
        test = f"{FEATURE}/meal/src/test/java/com/acme/fit/feature/meal/ui/mealsummary"
        expected = [f"{src}/MealSummaryScreen.kt", f"{src}/MealSummaryRoute.kt",
                    f"{src}/MealSummaryViewModel.kt", f"{src}/MealSummaryUiState.kt",
                    f"{test}/MealSummaryViewModelTest.kt"]
        self.assertEqual(sorted(set(self.files()) - set(before)), sorted(expected))
        for rel in expected:
            text = self.read(rel)
            self.assertTrue(text.startswith("package com.acme.fit.feature.meal.ui.mealsummary\n"), rel)
            self.assertNotIn("{{", text)

        screen = self.read(f"{src}/MealSummaryScreen.kt")
        signature = re.search(r"fun MealSummaryScreen\((.*?)\n\)", screen, re.S)
        self.assertIsNotNone(signature)
        self.assertIn("uiState: MealSummaryUiState", signature.group(1))
        self.assertNotIn("ViewModel", signature.group(1))
        self.assertIn("@Preview", screen)

        route = self.read(f"{src}/MealSummaryRoute.kt")
        self.assertIn("collectAsStateWithLifecycle()", route)
        self.assertIn("viewModel: MealSummaryViewModel = hiltViewModel(),", route)
        self.assertIn("import androidx.hilt.lifecycle.viewmodel.compose.hiltViewModel\n", route)

        view_model = self.read(f"{src}/MealSummaryViewModel.kt")
        self.assertIn("@HiltViewModel\nclass MealSummaryViewModel\n    @Inject\n    constructor() : ViewModel() {", view_model)
        self.assertIn("import javax.inject.Inject\n", view_model)
        self.assertIn("val uiState: StateFlow<MealSummaryUiState>", view_model)
        for forbidden in ("android.content.Context", "android.view.View", "GlobalScope", "retrofit2", "androidx.room"):
            self.assertNotIn(forbidden, view_model)

        self.assertIn("data class MealSummaryUiState(", self.read(f"{src}/MealSummaryUiState.kt"))
        test_text = self.read(f"{test}/MealSummaryViewModelTest.kt")
        self.assertIn("val viewModel = MealSummaryViewModel()", test_text)  # Hilt 없이 생성자(ADR-06)
        for word in ("runTest {", "StandardTestDispatcher()", "Dispatchers.setMain(dispatcher)"):
            self.assertIn(word, test_text)
        for hilt in ("HiltAndroidTest", "HiltAndroidRule", "dagger"):
            self.assertNotIn(hilt, test_text)

        self.assertIn('composable(route = "meal-summary") {\n    MealSummaryRoute()\n}', out)
        self.assertIn("import com.acme.fit.feature.meal.ui.mealsummary.MealSummaryRoute", out)
        self.assertIn('implementation(project(":feature:meal"))', out)
        self.assertEqual(self.check()[0], 0)  # 생성물은 check 대상이 아니다

    def test_scaffold_feature_needs_feature_var(self):
        self.init_feature()
        for kind in ("screen", "repository", "usecase"):
            with self.subTest(kind=kind):
                code, _out, err = self.scaffold(FEATURE, kind, "Meal")
                self.assertEqual(code, 2)
                self.assertIn("feature", err)
                code, _out, _err = self.scaffold(FEATURE, kind, "Meal", "--var", "feature=Bad-Name")
                self.assertEqual(code, 2)

    def test_scaffold_repository(self):
        self.init_feature()
        before = self.files()
        code, out, err = self.scaffold(FEATURE, "repository", "MealLog", "--var", "feature=meal")
        self.assertEqual(code, 0, err)
        main = f"{FEATURE}/meal/src/main/java/com/acme/fit/feature/meal"
        test = f"{FEATURE}/meal/src/test/java/com/acme/fit/feature/meal"
        expected = [f"{main}/data/MealLogRepository.kt", f"{main}/data/DefaultMealLogRepository.kt",
                    f"{main}/di/MealLogRepositoryModule.kt", f"{test}/data/FakeMealLogRepository.kt"]
        self.assertEqual(sorted(set(self.files()) - set(before)), sorted(expected))
        self.assertIn("interface MealLogRepository {", self.read(expected[0]))
        default = self.read(expected[1])
        self.assertIn("class DefaultMealLogRepository\n    @Inject\n    constructor() : MealLogRepository {", default)
        module = self.read(expected[2])
        self.assertTrue(module.startswith("package com.acme.fit.feature.meal.di\n"))
        for word in ("@Module\n@InstallIn(SingletonComponent::class)\nabstract class MealLogRepositoryModule {",
                     "    @Binds\n    abstract fun bindMealLogRepository(repository: DefaultMealLogRepository): "
                     "MealLogRepository",
                     "import dagger.hilt.components.SingletonComponent",
                     "import com.acme.fit.feature.meal.data.MealLogRepository"):
            self.assertIn(word, module)
        fake = self.read(expected[3])
        self.assertIn(") : MealLogRepository {", fake)
        self.assertNotIn("Inject", fake)  # 테스트 대역은 Hilt 없이 생성자로 넘긴다
        self.assertNotIn("공유 파일에 직접 붙일", out)
        self.assertEqual(self.check()[0], 0)

    def test_scaffold_usecase(self):
        self.init_feature()
        code, _out, err = self.scaffold(FEATURE, "usecase", "WorkoutVolume", "--var", "feature=workout")
        self.assertEqual(code, 0, err)
        rel = f"{FEATURE}/workout/src/main/java/com/acme/fit/feature/workout/domain/WorkoutVolumeUseCase.kt"
        text = self.read(rel)
        self.assertTrue(text.startswith("package com.acme.fit.feature.workout.domain\n"))
        self.assertIn("class WorkoutVolumeUseCase\n    @Inject\n    constructor() {", text)
        self.assertIn("usecase-shared-by-two-viewmodels", text)
        self.assertIn("사용처 2곳", text)

    # --- T4 ---------------------------------------------------------------

    def test_scaffold_wear_screen(self):
        self.init_module(APP)
        self.init_wear()
        before = self.files()
        code, out, err = self.scaffold(WEAR, "wear-screen", "HeartRate")
        self.assertEqual(code, 0, err)
        src = f"{WEAR}/src/main/java/com/acme/fit/wear/ui/heartrate"
        test = f"{WEAR}/src/test/java/com/acme/fit/wear/ui/heartrate"
        expected = [f"{src}/HeartRateScreen.kt", f"{src}/HeartRateRoute.kt", f"{src}/HeartRateViewModel.kt",
                    f"{src}/HeartRateUiState.kt", f"{test}/HeartRateViewModelTest.kt"]
        self.assertEqual(sorted(set(self.files()) - set(before)), sorted(expected))
        screen = self.read(f"{src}/HeartRateScreen.kt")
        for word in ("import androidx.wear.compose.foundation.lazy.ScalingLazyColumn",
                     "import androidx.wear.compose.material3.AppScaffold",
                     "import androidx.wear.compose.material3.ScreenScaffold",
                     "ScreenScaffold(scrollState = listState", "ScalingLazyColumn(", "AppScaffold {", "@Preview"):
            self.assertIn(word, screen)
        self.assertNotIn("androidx.compose.material3", screen)  # 폰 Material 3가 아니라 Wear Material 3
        signature = re.search(r"fun HeartRateScreen\((.*?)\n\)", screen, re.S)
        self.assertNotIn("ViewModel", signature.group(1))
        view_model = self.read(f"{src}/HeartRateViewModel.kt")
        self.assertIn("@HiltViewModel\nclass HeartRateViewModel\n    @Inject\n    constructor() : ViewModel() {", view_model)
        for network in ("retrofit2", "okhttp3", "io.ktor"):
            self.assertNotIn(network, view_model)
        self.assertIn("hiltViewModel()", self.read(f"{src}/HeartRateRoute.kt"))
        self.assertNotIn("androidx.hilt.navigation", self.read(f"{src}/HeartRateRoute.kt"))  # 폰 내비게이션 의존 없음
        self.assertIn("val viewModel = HeartRateViewModel()", self.read(f"{test}/HeartRateViewModelTest.kt"))
        self.assertIn("SwipeDismissableNavHost", out)
        self.assertIn("AppScaffold", out)
        self.assertIn('composable(route = "heart-rate") {\n    HeartRateRoute()\n}', out)
        self.assertIn("import androidx.wear.compose.navigation.composable", out)
        # 워치 스캐폴드는 폰 모듈 영역에 파일을 만들지 않는다
        self.assertFalse(any(f.startswith(f"{APP}/src/") for f in self.files()))

    # --- T5 ---------------------------------------------------------------

    def test_disable_each_fixed_rule(self):
        self.init_konsist()
        rel = f"{ARCH}/{FIXED}"
        full = self.read(rel)
        for rule in RULES:
            self.assertIn(f"// harness:rule {rule}\n", full)
            self.assertIn(RULE_TESTS[rule], full)
        for rule in RULES:
            with self.subTest(rule=rule):
                self.set_area(KONSIST_AREA, disabled_rules={rule: "테스트에서 끈다"})
                self.init_area(KONSIST_AREA, "--force")
                text = self.read(rel)
                self.assertNotIn(f"harness:rule {rule}", text)
                self.assertNotIn(RULE_TESTS[rule], text)
                for other in RULES:
                    if other != rule:
                        self.assertIn(f"// harness:rule {other}\n", text)
                        self.assertIn(RULE_TESTS[other], text)
                self.assertIn("fun scopesAreNotEmpty()", text)  # 항상 남는 범위 확인
                self.assertNotIn("\n\n\n", text)  # 블록이 빠져도 빈 줄이 겹치지 않는다(ktlint)
                self.assertTrue(text.endswith("\n}\n"))
                self.assertEqual(self.check()[0], 0)
        self.set_area(KONSIST_AREA, disabled_rules={rule: "전부 끈다" for rule in RULES})
        self.init_area(KONSIST_AREA, "--force")
        text = self.read(rel)
        self.assertNotIn("harness:", text)
        self.assertNotIn("\n\n\n", text)
        self.assertIn("fun scopesAreNotEmpty()", text)
        # 리뷰 F1(#51): 전부 끄면 쓰지 않는 import가 남는다. check 관리 파일이라 ktlint 검사를 파일에서 꺼 둬야 한다
        code = "\n".join(line for line in text.splitlines()
                         if not line.startswith(("import ", "//", "@file:")))
        unused = [line for line in text.splitlines() if line.startswith("import ")
                  and line.rsplit(".", 1)[-1] not in code]
        self.assertEqual(unused, ["import com.lemonappdev.konsist.api.verify.assertFalse"])
        self.assertIn('@file:Suppress("ktlint:standard:no-unused-imports")', text)

    def test_rule_scopes(self):
        """규칙마다 범위가 ADR대로인지: 전체(app·core·feature·wear), feature, wear, app."""
        self.init_konsist()
        text = self.read(f"{ARCH}/{FIXED}")
        blocks = dict(re.findall(r"// harness:rule (\S+)\n(.*?)// harness:end", text, re.S))
        self.assertEqual(tuple(blocks), RULES)
        for rule in ("no-global-scope", "viewmodel-no-android-context", "viewmodel-repository-only",
                     "screen-no-viewmodel-param"):
            self.assertIn("moduleFiles().assertFalse", blocks[rule], rule)
        self.assertIn("filesIn(featureDir).assertFalse", blocks["usecase-shared-by-two-viewmodels"])
        self.assertIn("filesIn(featureDir).assertFalse", blocks["feature-no-room"])
        self.assertIn("filesIn(wearDir).assertFalse", blocks["wear-no-network"])
        self.assertIn("filesIn(appDir).assertFalse", blocks["app-no-data-layer"])
        self.assertIn('listOf("retrofit2.", "androidx.room.")', blocks["viewmodel-repository-only"])
        self.assertIn('listOf("DataSource", "Dao")', blocks["viewmodel-repository-only"])
        self.assertIn('"androidx.room."', blocks["feature-no-room"])
        self.assertIn('listOf("retrofit2.", "okhttp3.", "io.ktor.")', blocks["wear-no-network"])
        self.assertIn('listOf("Repository", "DataSource", "Dao", "UseCase")', blocks["app-no-data-layer"])
        self.assertIn("< 2", blocks["usecase-shared-by-two-viewmodels"])
        self.assertIn("\"domain\" in packageName.split('.')", blocks["usecase-shared-by-two-viewmodels"])
        self.assertNotIn("wear-not-depend-on-app", text)  # Gradle 모듈 의존이 컴파일 에러로 막는다(ADR-05)

    # --- T6 ---------------------------------------------------------------

    # 생성 코드의 계층 반복문(계층·allow 맵을 읽어 Konsist 호출을 고른다). 리뷰 F2(#59): Konsist dependsOn은 "의존해도 된다"만
    # 뜻하므로 allow에 없는 나머지 계층을 doesNotDependOn으로 막아야 한다
    LAYER_LOOP = "\n".join([
        '            .assertArchitecture(testName = "featureLayersDependOnlyOnAllowedLayers") {',
        "                for ((name, layer) in layers) {",
        "                    val targets = allowed.getValue(name).map { layers.getValue(it) }.toSet()",
        "                    val others = layers.values.toSet() - targets - layer",
        "                    if (targets.isEmpty()) {",
        "                        layer.dependsOnNothing()",
        "                    } else {",
        "                        layer.dependsOn(targets)",
        "                        if (others.isNotEmpty()) {",
        "                            layer.doesNotDependOn(others)",
        "                        }",
        "                    }",
        "                }",
        "            }",
    ])

    def konsist_calls(self, text):
        """생성 파일의 두 맵을 읽어 반복문이 계층마다 부를 Konsist 호출을 계산한다(LAYER_LOOP와 같은 규칙)."""
        layers = dict(re.findall(r'^ {16}"(\w+)" to Layer\("\1", "([^"]+)"\),$', text, re.M))
        allowed = {}
        for name, args in re.findall(r'^ {16}"(\w+)" to (?:listOf\(([^)]*)\)|emptyList<String>\(\)),$', text, re.M):
            allowed[name] = re.findall(r'"(\w+)"', args)
        self.assertEqual(list(layers), list(allowed))
        calls = {}
        for name in layers:
            targets = allowed[name]
            others = [other for other in layers if other not in targets and other != name]
            if not targets:
                calls[name] = ("dependsOnNothing",)
            elif others:
                calls[name] = ("dependsOn", tuple(targets), "doesNotDependOn", tuple(others))
            else:
                calls[name] = ("dependsOn", tuple(targets))
        return layers, calls

    def test_layer_config_renders_konsist(self):
        self.init_konsist()
        rel = f"{ARCH}/{LAYERS}"
        text = self.read(rel)
        self.assertIn("\n".join([
            "        val layers: Map<String, Layer> =",
            "            mapOf(",
            '                "ui" to Layer("ui", "..ui.."),',
            '                "data" to Layer("data", "..data.."),',
            '                "domain" to Layer("domain", "..domain.."),',
            "            )",
            "        // 계층 이름 → 의존해도 되는 계층 이름(allow)",
            "        val allowed: Map<String, List<String>> =",
            "            mapOf(",
            '                "ui" to listOf("data", "domain"),',
            '                "data" to emptyList<String>(),',
            '                "domain" to listOf("data"),',
            "            )",
        ]), text)
        self.assertIn(self.LAYER_LOOP, text)
        self.assertIn("import com.lemonappdev.konsist.api.architecture.KoArchitectureCreator.assertArchitecture", text)
        # 기본값(ADR-04): data는 아무것도, domain은 ui를 의존하지 못한다(리뷰 F2의 빈틈이 domain→ui였다)
        layers, calls = self.konsist_calls(text)
        self.assertEqual(layers, {"ui": "..ui..", "data": "..data..", "domain": "..domain.."})
        self.assertEqual(calls, {"ui": ("dependsOn", ("data", "domain")), "data": ("dependsOnNothing",),
                                 "domain": ("dependsOn", ("data",), "doesNotDependOn", ("ui",))})

        # allow를 부분 목록으로 바꾸면(ADR-04 승격 등) 빠진 계층이 금지로 바뀐다. 다시 생성하기 전까지 check 불일치다
        self.set_area(KONSIST_AREA, allow={"ui": ["data"], "data": [], "domain": ["data"]})
        code, out = self.check()
        self.assertEqual(code, 1)
        self.assertIn(f"불일치: {rel}", out)
        self.init_area(KONSIST_AREA, "--force")
        self.assertEqual(self.check()[0], 0)
        text = self.read(rel)
        self.assertIn('                "ui" to listOf("data"),\n', text)
        self.assertIn(self.LAYER_LOOP, text)
        self.assertEqual(self.konsist_calls(text)[1], {
            "ui": ("dependsOn", ("data",), "doesNotDependOn", ("domain",)), "data": ("dependsOnNothing",),
            "domain": ("dependsOn", ("data",), "doesNotDependOn", ("ui",))})

        # 계층을 지우면 맵에서도 빠진다. allow에 없는 계층은 아무것도 의존하지 않는다
        config = self.config()
        area = next(a for a in config["areas"] if a["dir"] == KONSIST_AREA)
        del area["layers"]["domain"]
        area["allow"] = {"ui": ["data"]}
        self.save_config(config)
        self.init_area(KONSIST_AREA, "--force")
        self.assertEqual(self.check()[0], 0)
        text = self.read(rel)
        self.assertNotIn('"domain"', text)
        self.assertIn('                "data" to emptyList<String>(),\n', text)
        self.assertEqual(self.konsist_calls(text)[1], {"ui": ("dependsOn", ("data",)), "data": ("dependsOnNothing",)})

    # --- 검증 명령 ---------------------------------------------------------

    def test_verify_runs_gradle_root_wrapper_from_repo_root(self):
        """PR #54 Codex P1·결정표 Q2 (a): 영역 검증 명령은 저장소 루트에서 실행되므로 Gradle 빌드 루트의 wrapper를 부른다."""
        self.init_module(APP, "--var", "gradle_root=mobile")
        self.assertEqual(self.config()["areas"][0]["verify"], ["mobile/gradlew -p mobile :app:check"])
        for root in (".", "apps/android"):
            with self.subTest(root=root):
                # 재실행은 이전 verify를 유지하므로(엔진 규칙) 영역 항목을 지우고 다시 만든다
                config = self.config()
                config["areas"] = []
                self.save_config(config)
                self.init_area(APP, "--force", "--profile", PROFILE, "--var", "base_package=com.acme.fit",
                               "--var", f"gradle_root={root}")
                self.assertEqual(self.config()["areas"][0]["verify"], [f"{root}/gradlew -p {root} :app:check"])
        for bad in ("..", "../android", "/abs", "a/../b", "a\\b"):
            with self.subTest(bad=bad):
                code, _out, _err = run(["init", str(self.target), "--area", APP, "--force", "--var", f"gradle_root={bad}"])
                self.assertEqual(code, 2)

    def test_gradle_tasks_are_shell_safe(self):
        for bad in ("check", ":app:check; rm -rf /", ":app:check && x", ":app:check  :core:check", ""):
            with self.subTest(bad=bad):
                code, _out, _err = run(["init", str(self.target), "--area", APP, "--profile", PROFILE,
                                        "--var", "base_package=com.acme.fit", "--var", f"gradle_tasks={bad}"])
                self.assertEqual(code, 2)

    def test_global_scope_ignores_comments_and_strings(self):
        """PR #54 Codex: 주석·KDoc·문자열의 GlobalScope 언급은 위반이 아니고 실제 사용만 잡는다."""
        self.init_konsist()
        text = self.read(f"{ARCH}/{FIXED}")
        literals = re.findall(r'val (commentsAndStrings|globalScope) = Regex\(("(?:\\.|[^"\\])*")\)', text)
        patterns = {name: ast.literal_eval(literal) for name, literal in literals}  # Kotlin 문자열 이스케이프는 Python과 같다
        self.assertEqual(set(patterns), {"commentsAndStrings", "globalScope"})

        def violates(source):
            return re.search(patterns["globalScope"], re.sub(patterns["commentsAndStrings"], "", source)) is not None

        self.assertTrue(violates("fun f() {\n    GlobalScope.launch { }\n}\n"))
        self.assertTrue(violates('val s = "x" + GlobalScope.toString()\n'))
        self.assertFalse(violates("/**\n * GlobalScope 대신 viewModelScope를 쓴다\n */\nfun f() = Unit\n"))
        self.assertFalse(violates("// GlobalScope 금지\nfun f() = Unit\n"))
        self.assertFalse(violates('val message = "GlobalScope는 쓰지 않는다"\n'))
        self.assertFalse(violates('val message = "escaped \\" GlobalScope"\n'))
        # PR #60 Codex: 여러 줄 원시 문자열 안의 언급도 사용이 아니다. 원시 문자열 뒤의 실제 사용은 잡는다
        self.assertFalse(violates('val doc = """\n    GlobalScope 대신\n    viewModelScope\n"""\n'))
        self.assertTrue(violates('val doc = """\nx\n"""\nfun f() = GlobalScope.launch { }\n'))

    def test_chain_operator_heuristic(self):
        """연쇄 연산자 근사 검사 자체: 리뷰 F1(#59)이 찾은 두 줄은 4개 이상, 나눈 형태와 별개 식은 4개 미만이다."""
        def longest(line):
            return max(chain_operator_counts(line), default=0)

        self.assertEqual(longest('            val inDomain = file.packagee?.name.orEmpty().split(\'.\').contains("domain")'), 5)
        self.assertEqual(longest("            .slice { file -> file.projectPath.replace('\\\\', '/').trimStart('/')"
                                 '.startsWith("feature/") }'), 4)
        self.assertEqual(longest("    private fun KoFileDeclaration.isIn(dir: String): Boolean = "
                                 "projectPath.replace('\\\\', '/').trimStart('/').startsWith(\"$dir/\")"), 3)
        self.assertEqual(longest("            val names = file.classes().map { it.name } + file.interfaces().map { it.name }"), 2)
        self.assertEqual(longest("                    val targets = allowed.getValue(name).map { layers.getValue(it) }"
                                 ".toSet()"), 3)
        self.assertEqual(longest('        assertTrue("a.b.c.d.e 는 문자열", x.y)  // a.b.c.d.e 주석'), 1)
        self.assertEqual(longest("            .assertArchitecture {"), 0)
        self.assertEqual(longest("import com.lemonappdev.konsist.api.architecture.Layer"), 0)
        self.assertEqual(longest("        call(a.b.c.d.e())"), 4)  # 인자 안의 식도 따로 센다

    def test_templates_follow_ktlint_basics(self):
        self.init_feature()
        self.init_wear()
        self.init_konsist()
        for argv in ((FEATURE, "screen", "MealSummary", "--var", "feature=meal"),
                     (FEATURE, "repository", "MealLog", "--var", "feature=meal"),
                     (FEATURE, "usecase", "WorkoutVolume", "--var", "feature=workout"),
                     (WEAR, "wear-screen", "HeartRate")):
            code, _out, err = self.scaffold(*argv)
            self.assertEqual(code, 0, err)
        rendered = [f for f in self.files() if f.endswith(".kt")]
        self.assertEqual(len(rendered), 17)  # 컨벤션 2개 + screen 5 + repository 4 + usecase 1 + wear-screen 5
        for rel in rendered:
            with self.subTest(file=rel):
                text = self.read(rel)
                self.assertNotIn("\t", text)
                self.assertNotIn("\r", text)
                self.assertTrue(text.endswith("\n") and not text.endswith("\n\n"), "파일 끝 줄바꿈 하나")
                self.assertNotIn("\n\n\n", text)
                self.assertIsNone(re.search(r"^import .*\*$", text, re.M), "와일드카드 import")
                imports = re.findall(r"^import (\S+)$", text, re.M)
                # ktlint_official import 순서: 사전순, java·javax·kotlin은 뒤로
                tail = tuple(f"{p}." for p in ("java", "javax", "kotlin"))
                ordered = sorted(i for i in imports if not i.startswith(tail)) + \
                    sorted(i for i in imports if i.startswith(tail))
                self.assertEqual(imports, ordered, "import 순서")
                self.assertEqual(len(imports), len(set(imports)))
                for number, line in enumerate(text.split("\n"), 1):
                    self.assertEqual(line, line.rstrip(), f"{number}행 끝 공백")
                    self.assertLessEqual(len(line), 140, f"{number}행 길이")
                    # 리뷰 F1(#59): ktlintFormat이 check 관리 파일을 고치면 harness check 드리프트가 된다
                    self.assertLess(max(chain_operator_counts(line), default=0), 4, f"{number}행 연쇄 4개 이상: {line!r}")
                    stripped = line.lstrip(" ")
                    if stripped.startswith("*"):  # KDoc 본문은 " * " 정렬이라 한 칸 더 들어간다
                        continue
                    self.assertEqual((len(line) - len(stripped)) % 4, 0, f"{number}행 들여쓰기: {line!r}")


if __name__ == "__main__":
    unittest.main()
