"""spring-java 프로필(M3-2): 실제 profiles/spring-java 로 init·컨벤션 렌더·scaffold 결과를 확인한다.

생성 Java 코드의 컴파일·ArchUnit 실행은 JDK가 필요해 M3-5 리허설에서 본다. 여기서는 파일 목록, 자리표시자 잔존,
규칙 블록, 계층 서식, AOSP 형식의 정적 조건만 본다.
"""

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
SPEC = importlib.util.spec_from_file_location("harness_spring_java", ROOT / "bin" / "harness.py")
harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)

PROFILE = "spring-java"
ARCH = "backend/src/test/java/com/acme/app/architecture"
FIXED = f"{ARCH}/FixedRulesArchitectureTest.java"
LAYERS = f"{ARCH}/LayerRulesArchitectureTest.java"
MAIN = "backend/src/main/java/com/acme/app/domain/moviereview"
TEST = "backend/src/test/java/com/acme/app/domain/moviereview"
SCAFFOLD_FILES = [
    f"{MAIN}/controller/MovieReviewController.java",
    f"{MAIN}/service/MovieReviewService.java",
    f"{MAIN}/repository/MovieReviewRepository.java",
    f"{MAIN}/entity/MovieReview.java",
    f"{MAIN}/dto/request/MovieReviewCreateRequest.java",
    f"{MAIN}/dto/response/MovieReviewResponse.java",
    f"{TEST}/controller/MovieReviewControllerTest.java",
    f"{TEST}/service/MovieReviewServiceTest.java",
]
# 규칙 ID → 고정 규칙 파일의 @ArchTest 필드 이름
RULE_FIELDS = {
    "transactional-in-service-only": "TRANSACTIONAL_IN_SERVICE_ONLY",
    "controller-no-entity-return": "CONTROLLER_NO_ENTITY_RETURN",
    "no-field-injection": "NO_FIELD_INJECTION",
    "service-no-web-types": "SERVICE_NO_WEB_TYPES",
    "no-service-cycle-between-domains": "NO_DOMAIN_SERVICE_CYCLE",
    "no-cross-domain-persistence": "NO_CROSS_DOMAIN_PERSISTENCE",
    "event-publisher-in-service-only": "EVENT_PUBLISHER_IN_SERVICE_ONLY",
}
J8_STRICT = [
    "/src/main/resources/db/migration/", "/build.gradle", "/settings.gradle",
    "/src/main/resources/application*.yml", "**/global/", "**/infrastructure/", "**/*SecurityConfig.java",
    "gradle.lockfile",
    # 리뷰 F2: 영역 strict가 키트 기본값을 대체하므로 같은 성격의 Gradle 버전 카탈로그·wrapper도 넣는다
    "/gradle/libs.versions.toml", "/gradle/wrapper/",
]
IMPORT = re.compile(r"import (static )?([\w.]+);")


def code_only(text):
    """주석 줄을 뺀 코드(주석의 금지 예시 `@Setter` 등을 코드로 세지 않는다)."""
    return "\n".join(line for line in text.split("\n") if not line.lstrip().startswith("//"))


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = harness.main(argv)
    return code, out.getvalue(), err.getvalue()


class SpringProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-spring-"))
        self.addCleanup(remove_tree, self.tmp)
        self.target = self.tmp / "consumer"
        code, _out, err = run(["init", str(self.target), "--platform", "github", "--tracker", "github"])
        self.assertEqual(code, 0, err)

    def init_area(self, *extra):
        return run(["init", str(self.target), "--area", "backend", *extra])

    def init_spring(self):
        code, out, err = self.init_area("--profile", PROFILE, "--var", "base_package=com.acme.app")
        self.assertEqual(code, 0, err)
        return out

    def scaffold(self, name="MovieReview"):
        code, out, err = run(["scaffold", "backend", "domain", name, "--target", str(self.target)])
        self.assertEqual(code, 0, err)
        return out

    def config(self):
        return json.loads((self.target / "harness.json").read_text(encoding="utf-8"))

    def save_config(self, config):
        (self.target / "harness.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n",
                                                  encoding="utf-8")

    def area(self):
        return next(a for a in self.config()["areas"] if a["dir"] == "backend")

    def update_area(self, **values):
        config = self.config()
        next(a for a in config["areas"] if a["dir"] == "backend").update(values)
        self.save_config(config)

    def read(self, rel):
        return (self.target / rel).read_text(encoding="utf-8")

    def check(self):
        code, out, err = run(["check", str(self.target)])
        return code, out + err

    def test_profile_loads(self):
        profile, base = harness.load_profile(PROFILE)
        self.assertEqual(base, ROOT / "profiles" / PROFILE)
        self.assertEqual(list(profile["scaffold"]), ["domain"])
        self.assertEqual(set(profile["conventions"]["rules"]), set(RULE_FIELDS))
        self.assertEqual(list(profile["vars"])[0], "base_package")
        self.assertNotIn("default", profile["vars"]["base_package"])  # 필수 변수
        self.assertEqual(profile["vars"]["lombok"]["default"], "true")

    def test_init_area_defaults(self):
        out = self.init_spring()
        area = self.area()
        # #55: 영역 검증 명령은 저장소 루트에서 실행되므로 영역(Gradle 빌드 루트)으로 들어가 실행한다
        self.assertEqual(area["verify"], ["cd backend && ./gradlew build"])
        for pattern in J8_STRICT:
            with self.subTest(strict=pattern):
                self.assertIn(pattern, area["trigger_paths"]["strict"])
        # PR #52 Codex: java_test를 바꿔도 테스트 파일이 경량으로 판정되게 파일 이름 패턴을 함께 둔다
        self.assertEqual(area["test_paths"], ["/src/test/", "*Test.java", "*Tests.java"])
        self.assertEqual(len(area["triggers"]), 6)  # feelm 게이트 문장 5개 + 프로필 설정 변경
        self.assertEqual(area["triggers"][-1], harness.PROFILE_TRIGGER)
        for word in ("계약 문서", "다른 담당자", "@Transactional", "토큰", "캐시"):
            with self.subTest(trigger=word):
                self.assertTrue(any(word in t for t in area["triggers"]), area["triggers"])
        self.assertEqual(area["layers"], {name: [f"..{name}.."]
                                          for name in ("controller", "service", "repository", "entity", "dto")})
        self.assertEqual(area["allow"], {"controller": ["service", "dto"], "service": ["repository", "entity", "dto"],
                                         "repository": ["entity"], "dto": ["entity"]})
        self.assertEqual(area["vars"], {
            "base_package": "com.acme.app", "java_src": "src/main/java", "java_test": "src/test/java",
            "lombok": "true", "error_package": "com.acme.app.global.error",
            "webmvc_test_package": "org.springframework.boot.webmvc.test.autoconfigure"})
        files = sorted(p.relative_to(self.target).as_posix() for p in (self.target / ARCH).iterdir())
        self.assertEqual(files, sorted([FIXED, LAYERS]))
        for rel in files:
            text = self.read(rel)
            self.assertNotIn("{{", text)
            self.assertNotIn("}}", text)
            self.assertTrue(text.startswith("package com.acme.app.architecture;\n"), rel)
            self.assertIn('@AnalyzeClasses(packages = "com.acme.app", importOptions = DoNotIncludeTests.class)',
                          text)
        self.assertIn('.matching("com.acme.app.domain.(*).service..")', self.read(FIXED))
        self.assertIn("com.tngtech.archunit:archunit-junit5", out)
        self.assertIn("spotlessApply", out)
        self.assertEqual(self.check()[0], 0)

    def test_scaffold_domain(self):
        self.init_spring()
        before = {p.relative_to(self.target).as_posix() for p in self.target.rglob("*") if p.is_file()}
        out = self.scaffold()
        after = {p.relative_to(self.target).as_posix() for p in self.target.rglob("*") if p.is_file()}
        self.assertEqual(sorted(after - before), sorted(SCAFFOLD_FILES))
        texts = {rel: self.read(rel) for rel in SCAFFOLD_FILES}
        for rel, text in texts.items():
            with self.subTest(file=rel):
                package = rel.split("/java/", 1)[1].rsplit("/", 1)[0].replace("/", ".")
                self.assertTrue(text.startswith(f"package {package};\n"), text[:80])
                self.assertRegex(text, rf"\n(public )?(class|interface|record) {Path(rel).stem}\b")  # 파일 이름 = 타입
                self.assertNotIn("{{", text)
                self.assertNotIn("}}", text)
                self.assertNotIn("@Disabled", code_only(text))
                self.assertNotIn("@MockBean", code_only(text))
        service = texts[f"{MAIN}/service/MovieReviewService.java"]
        self.assertIn("@Transactional(readOnly = true)\npublic class MovieReviewService {", service)
        self.assertIn("    @Transactional\n    public MovieReviewResponse create(", service)
        self.assertIn("com.acme.app.global.error", service)  # error_package는 주석 예시에만
        self.assertNotIn("import com.acme.app.global", service)
        entity = texts[f"{MAIN}/entity/MovieReview.java"]
        self.assertIn("@NoArgsConstructor(access = AccessLevel.PROTECTED)", entity)
        self.assertIn("@Getter", entity)
        self.assertNotIn("@Setter", code_only(entity))
        self.assertIn("extends JpaRepository<MovieReview, Long>", texts[f"{MAIN}/repository/MovieReviewRepository.java"])
        self.assertIn("public record MovieReviewCreateRequest(", texts[f"{MAIN}/dto/request/MovieReviewCreateRequest.java"])
        response = texts[f"{MAIN}/dto/response/MovieReviewResponse.java"]
        self.assertIn("public record MovieReviewResponse(", response)
        self.assertIn("public static MovieReviewResponse from(MovieReview movieReview)", response)
        controller_test = texts[f"{TEST}/controller/MovieReviewControllerTest.java"]
        self.assertIn("@WebMvcTest(MovieReviewController.class)", controller_test)
        self.assertIn("import org.springframework.boot.webmvc.test.autoconfigure.WebMvcTest;", controller_test)
        self.assertIn("@TestConfiguration", controller_test)
        self.assertNotIn("MockitoBean", code_only(controller_test))
        self.assertIn("@ExtendWith(MockitoExtension.class)", texts[f"{TEST}/service/MovieReviewServiceTest.java"])
        self.assertIn('@RequestMapping("/api/v1/movie-review")', texts[f"{MAIN}/controller/MovieReviewController.java"])
        # 공유 파일은 고치지 않고 조각과 형식 안내를 출력한다(P-6, J-7)
        self.assertIn("공유 파일에 직접 붙일 코드 조각", out)
        self.assertIn("./gradlew spotlessApply", out)
        self.assertIn('.requestMatchers("/api/v1/movie-review/**")', out)
        # Boot 3.x 패키지로 바꾸면 import만 바뀐다(J-1)
        code, _out, err = run(["scaffold", "backend", "domain", "Ticket", "--target", str(self.target),
                               "--var", "webmvc_test_package=org.springframework.boot.test.autoconfigure.web.servlet"])
        self.assertEqual(code, 0, err)
        self.assertIn("import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;",
                      self.read("backend/src/test/java/com/acme/app/domain/ticket/controller/TicketControllerTest.java"))
        self.assertEqual(self.check()[0], 0)  # 생성물은 check 대상이 아니다

    def test_disable_each_fixed_rule(self):
        self.init_spring()
        original = self.read(FIXED)
        for rule, field in RULE_FIELDS.items():
            self.assertIn(f"// harness:rule {rule}\n", original)
            self.assertIn(f"@ArchTest static final ArchRule {field} =", original)
        for rule, field in RULE_FIELDS.items():
            with self.subTest(rule=rule):
                self.update_area(disabled_rules={rule: "테스트: 규칙 하나만 끈다"})
                self.assertEqual(self.check()[0], 1)
                code, _out, err = self.init_area("--force")
                self.assertEqual(code, 0, err)
                text = self.read(FIXED)
                self.assertNotIn(f"harness:rule {rule}\n", text)
                self.assertNotIn(f" {field} =", text)
                for other, other_field in RULE_FIELDS.items():
                    if other != rule:
                        self.assertIn(f"// harness:rule {other}\n", text)
                        self.assertIn(f"@ArchTest static final ArchRule {other_field} =", text)
                # 규칙 정의 메서드는 남아 import가 모두 쓰인다(spotless removeUnusedImports에 걸리지 않게)
                self.assertEqual(text.count("private static ArchRule "), len(RULE_FIELDS))
                self.assertNotIn("\n\n\n", text)
                self.assertEqual(self.check()[0], 0)
        self.update_area(disabled_rules={})
        code, _out, err = self.init_area("--force")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.read(FIXED), original)

    def test_layer_config_renders_archunit(self):
        self.init_spring()
        chain = "\n".join([
            "        return layeredArchitecture()",
            "                .consideringOnlyDependenciesInLayers()",
            "                .withOptionalLayers(true)",
            '                .layer("controller")',
            '                .definedBy("..controller..")',
            '                .layer("service")',
            '                .definedBy("..service..")',
            '                .layer("repository")',
            '                .definedBy("..repository..")',
            '                .layer("entity")',
            '                .definedBy("..entity..")',
            '                .layer("dto")',
            '                .definedBy("..dto..")',
            '                .whereLayer("controller")',
            '                .mayOnlyAccessLayers("service", "dto")',
            '                .whereLayer("service")',
            '                .mayOnlyAccessLayers("repository", "entity", "dto")',
            '                .whereLayer("repository")',
            '                .mayOnlyAccessLayers("entity")',
            '                .whereLayer("entity")',
            "                .mayNotAccessAnyLayer()",
            '                .whereLayer("dto")',
            '                .mayOnlyAccessLayers("entity")',
            "                .allowEmptyShould(true);",
        ])
        self.assertIn(chain, self.read(LAYERS))
        layers = self.area()["layers"]
        layers["web"] = ["..web..", "..api.."]
        self.update_area(layers=layers, allow={"controller": ["service"], "web": ["service", "dto"],
                                               "repository": []})
        code, out = self.check()
        self.assertEqual(code, 1)
        self.assertIn(f"불일치: {LAYERS}", out)
        self.assertNotIn(f"불일치: {FIXED}", out)
        code, _out, err = self.init_area("--force")
        self.assertEqual(code, 0, err)
        text = self.read(LAYERS)
        self.assertIn('                .layer("web")\n                .definedBy("..web..", "..api..")\n', text)
        self.assertIn('                .whereLayer("controller")\n                .mayOnlyAccessLayers("service")\n', text)
        self.assertIn('                .whereLayer("web")\n                .mayOnlyAccessLayers("service", "dto")\n',
                      text)
        for layer in ("service", "repository", "entity", "dto"):  # allow에 없거나 빈 계층은 어디에도 의존하지 않는다
            with self.subTest(layer=layer):
                self.assertIn(f'                .whereLayer("{layer}")\n                .mayNotAccessAnyLayer()\n', text)
        self.assertNotIn("{{", text)
        self.assertEqual(self.check()[0], 0)

    def test_templates_aosp_style(self):
        self.init_spring()
        self.scaffold()
        rendered = [p for p in self.target.rglob("*.java")]
        self.assertEqual(len(rendered), len(SCAFFOLD_FILES) + 2)
        for path in rendered:
            rel = path.relative_to(self.target).as_posix()
            lines = path.read_text(encoding="utf-8").split("\n")
            with self.subTest(file=rel):
                self.assertEqual(lines[-1], "")  # 마지막 줄바꿈 하나
                self.assertNotEqual(lines[-2], "")
                imports = {False: [], True: []}
                for number, line in enumerate(lines, 1):
                    where = f"{rel}:{number}: {line!r}"
                    self.assertNotIn("\t", line, where)
                    self.assertEqual(line, line.rstrip(), where)
                    self.assertLessEqual(len(line), 100, where)
                    indent = len(line) - len(line.lstrip(" "))
                    if line.strip() and not line.lstrip().startswith("*"):  # Javadoc 연속 줄은 한 칸 더 들어간다
                        self.assertEqual(indent % 4, 0, where)
                    match = IMPORT.fullmatch(line)
                    if line.startswith("import "):
                        self.assertIsNotNone(match, where)
                        self.assertFalse(match.group(2).endswith("*"), where)
                        imports[bool(match.group(1))].append(match.group(2))
                # google-java-format 순서: static 묶음 → 일반 묶음, 묶음 안은 사전순
                for static, names in imports.items():
                    self.assertEqual(names, sorted(names), f"{rel} static={static}")
                if imports[True] and imports[False]:
                    text = "\n".join(lines)
                    self.assertLess(text.index("import static "), text.index(f"import {imports[False][0]};"))


if __name__ == "__main__":
    unittest.main()
