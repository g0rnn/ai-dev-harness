"""react-ts 프로필(M3-3): 실제 `profiles/react-ts`로 init·scaffold·컨벤션 파일을 검사한다(plans/50.md T1~T7)."""

import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import process_support  # noqa: E402,F401 — test-only Windows child launch policy
from tempdirs import remove_tree  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("harness_react_ts", ROOT / "bin" / "harness.py")
harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)

PROFILE = "react-ts"
ESLINT = "frontend/eslint.harness.js"
RULES = ("no-mocks-import", "no-cross-screen-import", "jsx-a11y-recommended")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = harness.main(argv)
    return code, out.getvalue(), err.getvalue()


def js_constant(text: str, name: str):
    """렌더된 `const NAME = {...};` 한 줄의 JSON 값."""
    match = re.search(rf"^const {name} = (.*);$", text, re.MULTILINE)
    if match is None:
        raise AssertionError(f"{name} 상수가 없다")
    return json.loads(match.group(1))


def without_block(text: str, rule: str) -> str:
    """`harness:rule <rule>` ~ `harness:end` 줄을 뺀 텍스트(기대값 계산용)."""
    out, skipping = [], False
    for line in text.split("\n"):
        if f"harness:rule {rule}" in line:
            skipping = True
        if not skipping:
            out.append(line)
        if skipping and "harness:end" in line:
            skipping = False
    return "\n".join(out)


class ReactProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-react-ts-"))
        self.addCleanup(remove_tree, self.tmp)
        self.target = self.tmp / "consumer"
        code, _out, err = run(["init", str(self.target), "--platform", "github", "--tracker", "github"])
        self.assertEqual(code, 0, err)

    def init_frontend(self, *extra):
        code, out, err = run(["init", str(self.target), "--area", "frontend", *extra])
        self.assertEqual(code, 0, err)
        return out

    def scaffold(self, *argv):
        code, out, err = run(["scaffold", "frontend", *argv, "--target", str(self.target)])
        self.assertEqual(code, 0, err)
        return out

    def config(self):
        return json.loads((self.target / "harness.json").read_text(encoding="utf-8"))

    def save_config(self, config):
        (self.target / "harness.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n",
                                                  encoding="utf-8")

    def area(self):
        return next(a for a in self.config()["areas"] if a["dir"] == "frontend")

    def read(self, rel):
        return (self.target / rel).read_text(encoding="utf-8")

    def files(self):
        return sorted(p.relative_to(self.target).as_posix() for p in self.target.rglob("*") if p.is_file())

    def check(self):
        code, out, err = run(["check", str(self.target)])
        return code, out + err

    # T1
    def test_profile_loads(self):
        profile, base = harness.load_profile(PROFILE)
        self.assertEqual(base, ROOT / "profiles" / PROFILE)
        self.assertEqual(list(profile["scaffold"]), ["screen", "api", "api-client"])
        self.assertEqual(set(profile["conventions"]["rules"]), set(RULES))
        self.assertEqual(list(profile["vars"]), ["src_root", "api_client_import", "api_client_fn", "lint_script",
                                                 "format_script", "test_script", "build_script"])
        self.assertTrue((base / "README.md").is_file())

    # T2
    def test_init_area_defaults(self):
        out = self.init_frontend("--profile", PROFILE)
        area = self.area()
        profile, _base = harness.load_profile(PROFILE)
        self.assertEqual(area["profile"], PROFILE)
        # #55: 영역 검증 명령은 저장소 루트에서 실행되므로 --prefix로 영역 package.json을 쓴다
        self.assertEqual(area["verify"], [f"npm --prefix frontend run {s}"
                                          for s in ("lint", "format:check", "test:run", "build")])
        self.assertEqual(area["vars"], {"src_root": "src", "api_client_import": "./client", "api_client_fn": "request",
                                        "lint_script": "lint", "format_script": "format:check",
                                        "test_script": "test:run", "build_script": "build"})
        strict = area["trigger_paths"]["strict"]
        for pattern in ("/package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "/vite.config.*",
                        "/eslint.config.*"):
            with self.subTest(strict=pattern):
                self.assertIn(pattern, strict)
        self.assertEqual(area["trigger_paths"]["standard"], ["/src/routes/", "/src/components/"])
        self.assertEqual(area["test_paths"], ["*.test.ts", "*.test.tsx"])
        self.assertEqual(area["triggers"], [*profile["triggers"], harness.PROFILE_TRIGGER])
        self.assertEqual(len(profile["triggers"]), 4)
        self.assertEqual(list(area["layers"]), ["routes", "pages", "components", "api", "types", "utils"])
        self.assertEqual(area["allow"]["pages"], ["components", "api", "types", "utils", "routes"])
        self.assertEqual(area["allow"]["components"], ["types", "utils"])
        self.assertEqual(area["disabled_rules"], {})
        self.assertIn(ESLINT, self.files())
        self.assertNotIn("{{", self.read(ESLINT))
        self.assertNotIn("}}", self.read(ESLINT))
        self.assertIn("npm --prefix frontend run test:run", self.read("frontend/AGENTS.md"))
        self.assertIn("안내: eslint.harness.js 는 devDependency eslint-plugin-jsx-a11y 가 필요하다", out)
        self.assertIn("...harness", out)
        self.assertEqual(self.check()[0], 0)
        # 스크립트 이름은 변수로 바꾼다(R-9). 재실행은 굳힌 verify를 유지하므로 새 영역에서 본다
        code, _out, err = run(["init", str(self.target), "--area", "web", "--profile", PROFILE,
                               "--var", "test_script=test:ci"])
        self.assertEqual(code, 0, err)
        web = next(a for a in self.config()["areas"] if a["dir"] == "web")
        self.assertEqual(web["verify"][2], "npm --prefix web run test:ci")

    # T3
    def test_scaffold_screen(self):
        self.init_frontend("--profile", PROFILE)
        before = set(self.files())
        out = self.scaffold("screen", "MovieDetail", "--var", "area=movie")
        page = "frontend/src/pages/movie/MovieDetailPage"
        created = sorted(set(self.files()) - before)
        self.assertEqual(created, sorted([f"{page}/index.tsx", f"{page}/MovieDetailPage.module.css",
                                          f"{page}/MovieDetailPage.test.tsx"]))
        test = self.read(f"{page}/MovieDetailPage.test.tsx")
        for word in ("vi.mock('/src/api/client'", "request: vi.fn()", "createMemoryRouter", "RouterProvider",
                     "getByRole", "import MovieDetailPage from './index'"):
            with self.subTest(word=word):
                self.assertIn(word, test)
        self.assertIn("export default function MovieDetailPage()", self.read(f"{page}/index.tsx"))
        self.assertIn("./MovieDetailPage.module.css", self.read(f"{page}/index.tsx"))
        for rel in created:
            with self.subTest(rel=rel):
                text = self.read(rel)
                self.assertNotIn("{{", text)
                self.assertNotIn(".skip", text)
                self.assertNotIn(".only", text)
        self.assertIn("movieDetail: '/movie-detail',", out)
        self.assertIn("movieDetail: () => ROUTE_PATTERN.movieDetail,", out)
        self.assertIn("import MovieDetailPage from '../pages/movie/MovieDetailPage';", out)
        self.assertIn("{ path: ROUTE_PATTERN.movieDetail, element: <MovieDetailPage /> },", out)
        # area를 비우면 경로 구간이 접힌다
        self.scaffold("screen", "Home")
        self.assertTrue((self.target / "frontend/src/pages/HomePage/HomePage.test.tsx").is_file())
        # 생성물은 팀 소유라 check 대상이 아니다
        self.assertEqual(self.check()[0], 0)

    # T4
    def test_scaffold_api_and_client(self):
        self.init_frontend("--profile", PROFILE)
        self.scaffold("api", "movie-review")
        module = self.read("frontend/src/api/movieReview.ts")
        self.assertIn("import { request } from './client';", module)
        self.assertIn("// API-ID:", module)
        self.assertIn("export async function getMovieReviewList(): Promise<MovieReview[]>", module)
        self.assertIn("request<MovieReview>(`/movie-review/${id}`)", module)
        test = self.read("frontend/src/api/movieReview.test.ts")
        self.assertIn("vi.stubGlobal('fetch', fetchMock)", test)
        self.assertIn("from './movieReview'", test)
        self.scaffold("api-client", "Client")
        client = self.read("frontend/src/api/client.ts")
        self.assertIn("export async function request<T>(path: string, options: RequestOptions = {}): Promise<T>",
                      client)
        self.assertIn("throw new ApiError(response.status, data);", client)
        for rel in ("frontend/src/api/movieReview.ts", "frontend/src/api/movieReview.test.ts",
                    "frontend/src/api/client.ts"):
            with self.subTest(rel=rel):
                self.assertNotIn("{{", self.read(rel))
                self.assertNotIn(".skip", self.read(rel))
        # 변수로 클라이언트 함수를 바꾼다
        self.scaffold("api", "watch", "--var", "api_client_fn=http", "--var", "api_client_import=../lib/http")
        self.assertIn("import { http } from '../lib/http';", self.read("frontend/src/api/watch.ts"))

    # T5
    def test_disable_each_fixed_rule(self):
        self.init_frontend("--profile", PROFILE)
        full = self.read(ESLINT)
        for rule in RULES:
            self.assertIn(f"// harness:rule {rule}", full)
        for rule in RULES:
            with self.subTest(rule=rule):
                config = self.config()
                config["areas"][0]["disabled_rules"] = {rule: "검증용으로 끈다(#50)"}
                self.save_config(config)
                self.init_frontend("--force")
                text = self.read(ESLINT)
                self.assertEqual(text, without_block(full, rule))
                self.assertNotIn(f"harness:rule {rule}", text)
                for other in set(RULES) - {rule}:
                    self.assertIn(f"// harness:rule {other}", text)
                self.assertEqual(self.check()[0], 0)
        self.assertNotIn("eslint-plugin-jsx-a11y", self.read(ESLINT))  # 마지막으로 끈 규칙은 import까지 빠진다

    # T6
    def test_layer_config_in_eslint(self):
        self.init_frontend("--profile", PROFILE)
        area = self.area()
        text = self.read(ESLINT)
        self.assertEqual(js_constant(text, "LAYERS"), area["layers"])
        self.assertEqual(js_constant(text, "ALLOW"), area["allow"])
        self.assertIn("const SRC_ROOT = 'src';", text)
        config = self.config()
        config["areas"][0]["allow"]["components"] = ["types", "utils", "api"]
        config["areas"][0]["layers"]["hooks"] = ["hooks"]
        self.save_config(config)
        code, out = self.check()
        self.assertEqual(code, 1)
        self.assertIn(f"불일치: {ESLINT}", out)
        self.init_frontend("--force")
        text = self.read(ESLINT)
        self.assertEqual(js_constant(text, "ALLOW")["components"], ["types", "utils", "api"])
        self.assertEqual(js_constant(text, "LAYERS")["hooks"], ["hooks"])
        self.assertEqual(self.check()[0], 0)

    # T7
    def test_eslint_harness_is_valid_js(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node가 없어 eslint.harness.js 구문 검사를 건너뛴다(대체: M3-5 리허설의 eslint 실행)")
        self.init_frontend("--profile", PROFILE)
        variants = {"default": self.read(ESLINT)}
        config = self.config()
        config["areas"][0]["disabled_rules"] = {rule: "구문 검사" for rule in RULES}
        config["areas"][0]["layers"] = {}
        config["areas"][0]["allow"] = {}
        self.save_config(config)
        self.init_frontend("--force")
        variants["all disabled"] = self.read(ESLINT)
        for label, text in variants.items():
            with self.subTest(label):
                path = self.tmp / f"eslint-harness-{label.replace(' ', '-')}.mjs"  # ESM으로 검사한다
                path.write_text(text, encoding="utf-8")
                result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True,
                                        encoding="utf-8", errors="replace", timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_nested_layer_keeps_outer_restrictions(self):
        """리뷰 F1: components 안에 components/shared 계층을 두면 한 파일에 두 묶음이 맞고 마지막 객체만 남는다.
        안쪽 묶음이 바깥 묶음의 고정 규칙·계층 제한을 모두 담고 뒤에 와야 한다."""
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node가 없어 생성된 설정을 실행해 볼 수 없다(대체: M3-5 리허설의 eslint 실행)")
        self.init_frontend("--profile", PROFILE)
        config = self.config()
        area = config["areas"][0]
        area["layers"]["shared"] = ["components/shared"]
        area["allow"]["shared"] = ["types", "utils"]
        area["disabled_rules"] = {"jsx-a11y-recommended": "플러그인 없이 설정만 읽는다"}
        self.save_config(config)
        self.init_frontend("--force")
        module = self.tmp / "eslint-harness.mjs"
        module.write_text(self.read(ESLINT), encoding="utf-8")
        dump = self.tmp / "dump.mjs"
        dump.write_text(
            "import configs from './eslint-harness.mjs';\n"
            "console.log(JSON.stringify(configs.map((c) => ({ files: c.files, "
            "messages: (c.rules?.['no-restricted-imports']?.[1]?.patterns ?? []).map((p) => p.message), "
            "regexes: (c.rules?.['no-restricted-imports']?.[1]?.patterns ?? []).map((p) => p.regex) }))));\n",
            encoding="utf-8")
        result = subprocess.run([node, str(dump)], capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        configs = json.loads(result.stdout)
        index = {c["files"][0]: i for i, c in enumerate(configs)}
        inner = "src/components/shared/*.{js,jsx,ts,tsx}"
        outer = "src/components/*/*.{js,jsx,ts,tsx}"  # 같은 파일(깊이 2)에 맞는 바깥 묶음
        self.assertGreater(index[inner], index[outer])
        messages = " ".join(configs[index[inner]]["messages"])
        for expected in ("[no-mocks-import]", "[layers] 계층 components은", "[layers] 계층 shared은"):
            self.assertIn(expected, messages)
        # PR #53 Codex: components가 shared를 허용하지 않으면 바깥 계층 파일의 하위 상대 import(`./shared`)도 막는다
        def blocked(glob, source):
            return any(re.search(regex, source) for regex in configs[index[glob]]["regexes"])

        self.assertTrue(blocked("src/components/*.{js,jsx,ts,tsx}", "./shared/Card"))
        self.assertTrue(blocked("src/components/*/*.{js,jsx,ts,tsx}", "../shared/Card"))
        self.assertFalse(blocked("src/components/*.{js,jsx,ts,tsx}", "./Button"))
        self.assertFalse(blocked("src/components/*/*.{js,jsx,ts,tsx}", "./shared/Card"))  # components/X/shared는 다른 곳


if __name__ == "__main__":
    unittest.main()
