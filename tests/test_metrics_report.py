"""core/metrics/report.py와 `harness metrics report` 테스트(M4-6). 정적 fixture와 수집기 fixture(가짜 전송)만 쓴다."""

import importlib.util
import io
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "metrics"
SAMPLE = FIXTURES / "report-sample.json"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


report = load("metrics_report", ROOT / "core" / "metrics" / "report.py")
harness = load("harness_for_report", ROOT / "bin" / "harness.py")
# 계약 테스트(T8)는 수집기 테스트의 가짜 API·실행 도우미를 그대로 쓴다. 모듈 속성으로만 두어 그 테스트가 여기서 다시 돌지 않는다
collect_tests = load("metrics_collect_tests_for_report", ROOT / "tests" / "test_metrics_collect.py")

H2 = ["이번 주 요약", "지표 1 병합 후 누락 수", "지표 2 엄격 비율", "지표 3 CI 실패 원인", "지표 4 판정 불일치 수",
      "지표 5 hooks 기록", "지표 6 에이전트 기록", "지표 7 도구별 결과 (기간 전체)", "지표 8 리뷰 효과", "원자료"]


def sample():
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


def parse(text):
    return ET.fromstring(text)


def local(tag):
    return tag.rsplit("}", 1)[-1]


def find_all(root, name):
    return [element for element in root.iter() if local(element.tag) == name]


def text_of(element):
    return "".join(element.itertext()).strip()


def rows(table):
    body = next(child for child in table if local(child.tag) == "tbody")
    return [[text_of(cell) for cell in row] for row in body]


def header_cells(table):
    head = next(child for child in table if local(child.tag) == "thead")
    return [text_of(cell) for cell in head[0]]


def summary_table(root):
    return next(table for table in find_all(root, "table") if table.get("class") == "summary")


def figure_tables(root):
    """지표 그림 5개의 (svg, 바로 뒤 형제 table)."""
    pairs = []
    for figure in find_all(root, "figure"):
        children = list(figure)
        index = next(i for i, child in enumerate(children) if local(child.tag) == "svg")
        pairs.append((children[index], children[index + 1]))
    return pairs


def raw_tables(root):
    """원자료 <details> 3개(MR·결함·job) 안의 table. 비어 있으면 None."""
    found = []
    for detail in find_all(root, "details"):
        tables = find_all(detail, "table")
        found.append(tables[0] if tables else None)
    return found


def value_labels(svg):
    return [text_of(t) for t in svg.iter() if local(t.tag) == "text" and t.get("class") == "value"]


class RenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = sample()
        cls.html = report.render(cls.data, None)
        cls.root = parse(cls.html)

    def test_wellformed_and_sections(self):
        """T1: 출력이 XML로 파싱되고 <h2> 절이 고정 순서이며 <script>·src=·외부 <link>가 없다."""
        self.assertTrue(self.html.startswith('<!DOCTYPE html>\n<html lang="ko"><head><meta charset="utf-8"/>'))
        self.assertIn('<meta name="viewport"', self.html)
        self.assertEqual([text_of(h) for h in find_all(self.root, "h2")], H2)
        self.assertEqual(text_of(find_all(self.root, "title")[0]), "group/demo 측정 리포트")
        self.assertEqual(find_all(self.root, "script"), [])
        self.assertEqual(find_all(self.root, "link"), [])
        self.assertEqual(find_all(self.root, "img"), [])
        self.assertEqual(len(find_all(self.root, "style")), 1)
        for element in self.root.iter():
            self.assertNotIn("src", element.attrib)
            self.assertFalse(any(name.startswith("on") for name in element.attrib), element.attrib)
        self.assertNotIn("src=", self.html)
        self.assertNotIn("<link", self.html)
        self.assertNotIn("url(", self.html)  # 인라인 CSS도 외부 리소스를 부르지 않는다
        self.assertNotIn("@import", self.html)
        for svg in find_all(self.root, "svg"):
            self.assertEqual(svg.get("role"), "img")
            self.assertEqual(local(svg[0].tag), "title")
            self.assertTrue(text_of(svg[0]))
        meta = {text_of(dt): text_of(dd) for dt, dd in zip(find_all(self.root, "dt"), find_all(self.root, "dd"))}
        self.assertEqual(meta, {"프로젝트": "group/demo", "플랫폼": "gitlab",
                                "기간": "2026-09-14 00:00 ~ 2026-10-01 12:00 (+09:00)",
                                "생성 시각": "2026-10-01 12:05", "주 수": "3"})
        titled = report.render(self.data, "냠냠코치 주간 리포트")
        self.assertIn("<title>냠냠코치 주간 리포트</title>", titled)
        self.assertIn("<h1>냠냠코치 주간 리포트</h1>", titled)
        self.assertNotIn("\r", self.html)

    def test_charts_have_matching_tables(self):
        """T2: SVG 7개(지표 1~6·8), 각 SVG 바로 뒤 table의 숫자가 weeks 값과 같다(행 수 = 주 수)."""
        pairs = figure_tables(self.root)
        self.assertEqual(len(pairs), 7)
        self.assertEqual(len(find_all(self.root, "svg")), 7)
        weeks = self.data["weeks"]
        for _svg, table in pairs:
            self.assertEqual(len(rows(table)), len(weeks))
            self.assertEqual([row[:2] for row in rows(table)], [[w["week"], w["start"]] for w in weeks])
        self.assertEqual(header_cells(pairs[0][1]), ["주", "시작일", "병합 후 누락", "원인 미연결"])
        self.assertEqual([[int(r[2]), int(r[3])] for r in rows(pairs[0][1])],
                         [[w["escaped_defects"], w["unlinked_defects"]] for w in weeks])
        self.assertEqual(header_cells(pairs[1][1]), ["주", "시작일", "엄격 비율", "엄격/병합 MR"])
        self.assertEqual([r[2:] for r in rows(pairs[1][1])], [["-", "0/0"], ["33%", "1/3"], ["100%", "1/1"]])
        self.assertEqual(header_cells(pairs[2][1]),
                         ["주", "시작일", "테스트 실패", "포맷", "인프라", "타임아웃", "보안", "미분류", "합계"])
        for row, week in zip(rows(pairs[2][1]), weeks):
            self.assertEqual([int(v) for v in row[2:8]], [week["ci_failures"][c] for c in report.CATEGORIES])
            self.assertEqual(int(row[8]), sum(week["ci_failures"].values()))
        self.assertEqual(header_cells(pairs[3][1]), ["주", "시작일", "판정 불일치", "병합 MR"])
        self.assertEqual([[int(r[2]), int(r[3])] for r in rows(pairs[3][1])],
                         [[w["mismatch"], w["merged_mrs"]] for w in weeks])
        # 지표 5: hooks가 없는 옛 주(W38)는 0으로 그린다
        self.assertEqual(header_cells(pairs[4][1]), ["주", "시작일", "차단", "확인", "종료 검증 실패", "보고 MR"])
        self.assertEqual([r[2:] for r in rows(pairs[4][1])],
                         [["0", "0", "0", "0/0"], ["2", "1", "1", "2/3"], ["0", "1", "0", "1/1"]])
        self.assertEqual(value_labels(pairs[4][0]), ["0", "0", "0", "2", "1", "1", "0", "1", "0"])
        figure = find_all(self.root, "figure")[4]
        legend = next(child for child in figure if local(child.tag) == "ul")
        self.assertEqual([text_of(li) for li in legend], ["차단(block)", "확인(ask)", "종료 검증 실패"])
        # 막대의 값(data-value)과 값 라벨도 표와 같다
        bars = [r for r in pairs[0][0].iter() if local(r.tag) == "rect" and r.get("data-series") == "escaped_defects"]
        self.assertEqual([int(b.get("data-value")) for b in bars], [w["escaped_defects"] for w in weeks])
        self.assertEqual(value_labels(pairs[0][0]), ["0", "1", "1", "0", "1", "0"])  # 주마다 누락·미연결
        self.assertEqual(value_labels(pairs[3][0]), ["0", "1", "0"])
        # x 라벨은 ISO 주와 시작일
        axis = [text_of(t) for t in pairs[3][0].iter() if local(t.tag) == "text" and t.get("class") == "axis"]
        for week in weeks:
            self.assertIn(week["week"], axis)
            self.assertIn(week["start"], axis)
        # 주가 많아도(52개) 네 그래프가 그려지고 파싱된다. x 라벨은 간격을 두고, 표에는 모든 주가 있다
        many = dict(self.data, weeks=[dict(weeks[1], week=f"2026-W{n:02d}") for n in range(1, 53)])
        root = parse(report.render(many, None))
        self.assertEqual(len(find_all(root, "svg")), 7)
        self.assertEqual(len(rows(figure_tables(root)[0][1])), 52)
        axis = [t for t in figure_tables(root)[3][0].iter() if local(t.tag) == "text" and t.get("class") == "axis"]
        self.assertLess(len(axis), 52)
        # 간격을 두어도 가장 최근 주의 x 라벨과 값 라벨은 네 그래프 모두에 남는다(리뷰 F2: 첫 주 기준이면 52·12·20주에서 빠진다)
        for svg, _table in figure_tables(root):
            texts = [text_of(t) for t in svg.iter() if local(t.tag) == "text" and t.get("class") == "axis"]
            self.assertIn("2026-W52", texts)  # 마지막 주 기준이라 첫 주(W01)는 간격에 따라 빠질 수 있다
        self.assertTrue(value_labels(figure_tables(root)[3][0]))
        bars = [r for r in figure_tables(root)[3][0].iter() if local(r.tag) == "rect"]
        last_label_x = float(figure_tables(root)[3][0].findall(".//{*}text[@class='value']")[-1].get("x"))
        self.assertAlmostEqual(last_label_x, float(bars[-1].get("x")) + float(bars[-1].get("width")) / 2, places=1)
        for count in (12, 20, 52, 100):
            step = report.label_step(count)
            shown = [i for i in range(count) if report.show_label(i, count, step)]
            self.assertEqual(shown[-1], count - 1, count)
            self.assertLessEqual(len(shown), report.MAX_LABELS + 1)

    def test_summary_and_delta(self):
        """T3: 요약 값 = 마지막 주, 증감 = 마지막 − 직전(부호 표시), 마지막 주가 기간 끝을 넘으면 (진행 중). 주 1개면 증감 -."""
        summary = summary_table(self.root)
        self.assertEqual(header_cells(summary), ["지표", "이번 주 2026-W40 (진행 중)", "직전 주 2026-W39", "증감"])
        by_label = {row[0]: row[1:] for row in rows(summary)}
        self.assertEqual(by_label["1 병합 후 누락 수"], ["1", "1", "±0"])
        self.assertEqual(by_label["2 엄격 비율"], ["100% (1/1)", "33% (1/3)", "+67%p"])
        self.assertEqual(by_label["3 CI 실패 수"], ["2", "4", "-2"])
        self.assertEqual(by_label["4 판정 불일치 수"], ["0", "1", "-1"])
        self.assertEqual(by_label["5 hooks 개입 수"], ["1", "4", "-3"])
        self.assertEqual(by_label["hooks 기록 보고 MR (참고)"], ["1/1", "2/3", "-"])
        self.assertIn("(진행 중)", self.html)
        # 증가 방향, 완료된 주(W39 끝 09-28이 until 10-01보다 앞)
        two = report.render(dict(self.data, weeks=self.data["weeks"][:2]), None)
        summary = summary_table(parse(two))
        self.assertEqual(header_cells(summary)[1], "이번 주 2026-W39")
        by_label = {row[0]: row[1:] for row in rows(summary)}
        self.assertEqual(by_label["1 병합 후 누락 수"], ["1", "0", "+1"])
        self.assertEqual(by_label["2 엄격 비율"], ["33% (1/3)", "-", "-"])
        self.assertEqual(by_label["3 CI 실패 수"], ["4", "1", "+3"])
        self.assertEqual(by_label["4 판정 불일치 수"], ["1", "0", "+1"])
        self.assertEqual(by_label["5 hooks 개입 수"], ["4", "0", "+4"])  # W38은 hooks 없는 옛 JSON 주
        self.assertNotIn("(진행 중)", two)
        # 주 1개면 직전 주·증감 모두 -
        one = parse(report.render(dict(self.data, weeks=self.data["weeks"][:1]), None))
        summary = summary_table(one)
        self.assertEqual(header_cells(summary), ["지표", "이번 주 2026-W38", "직전 주", "증감"])
        self.assertTrue(all(row[2] == "-" and row[3] == "-" for row in rows(summary)))
        # until이 없거나 마지막 주 끝과 같으면 진행 중이 아니다
        no_window = dict(self.data)
        no_window.pop("window")
        self.assertNotIn("(진행 중)", report.render(no_window, None))
        exact = dict(self.data, window=dict(self.data["window"], until="2026-10-05T00:00:00+09:00"))
        self.assertNotIn("(진행 중)", report.render(exact, None))
        self.assertEqual((report.delta_text(3, 3), report.delta_text(5, 2), report.delta_text(0, 2),
                          report.delta_text(1, None)), ("±0", "+3", "-2", "-"))
        self.assertEqual((report.delta_ratio_text(50, 50), report.delta_ratio_text(100, 33),
                          report.delta_ratio_text(None, 33)), ("±0%p", "+67%p", "-"))

    def test_strict_ratio(self):
        """T4: 병합 MR 0이면 "-", 3 중 엄격 1이면 "33%"와 "1/3". 선 그래프는 병합 MR 없는 주에 점을 찍지 않는다."""
        svg, table = figure_tables(self.root)[1]
        self.assertEqual([row[2:] for row in rows(table)], [["-", "0/0"], ["33%", "1/3"], ["100%", "1/1"]])
        dots = [c for c in svg.iter() if local(c.tag) == "circle"]
        self.assertEqual([(d.get("data-week"), d.get("data-value")) for d in dots],
                         [("2026-W39", "33"), ("2026-W40", "100")])
        self.assertEqual(value_labels(svg), ["33% (1/3)", "100% (1/1)"])
        self.assertEqual(len([p for p in svg.iter() if local(p.tag) == "polyline"]), 1)
        # 100%는 그림 위쪽, 33%는 그 아래
        self.assertLess(float(dots[1].get("cy")), float(dots[0].get("cy")))
        self.assertEqual(report.ratio({"merged_mrs": 3, "strict": 2}), (67, "2/3"))
        self.assertEqual(report.ratio({"merged_mrs": 8, "strict": 1}), (13, "1/8"))
        self.assertEqual(report.ratio({"merged_mrs": 0, "strict": 0}), (None, "0/0"))
        # 병합 MR이 있는 주 사이에 없는 주가 끼면 선이 끊긴다
        gap = [dict(self.data["weeks"][1]), dict(self.data["weeks"][0], week="2026-W39b"), dict(self.data["weeks"][2])]
        svg = figure_tables(parse(report.render(dict(self.data, weeks=gap), None)))[1][0]
        self.assertEqual([p for p in svg.iter() if local(p.tag) == "polyline"], [])
        self.assertEqual(len([c for c in svg.iter() if local(c.tag) == "circle"]), 2)

    def test_escaping_and_links(self):
        """T5: 제목의 <script>는 &lt;script&gt;로, javascript: URL은 href 없음, https URL만 href."""
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", self.html)
        self.assertNotIn("<script", self.html)
        self.assertNotIn("javascript:", self.html)
        anchors = find_all(self.root, "a")
        self.assertEqual(len(anchors), 2 + 3 + 3)  # MR 21·24, 결함 3개, job 3개
        for anchor in anchors:
            self.assertTrue(anchor.get("href").startswith("https://gitlab.example/"), anchor.get("href"))
        mr_table = raw_tables(self.root)[0]
        body = next(child for child in mr_table if local(child.tag) == "tbody")
        by_number = {text_of(row[0]): row for row in body}
        self.assertEqual([local(c.tag) for c in by_number["21"][0]], ["a"])
        self.assertEqual(list(by_number["22"][0]), [])  # javascript: → 텍스트만
        self.assertEqual(list(by_number["23"][0]), [])  # url null
        self.assertEqual(text_of(by_number["21"][1]), "feat: 로그인 화면 <script>alert(1)</script>")
        self.assertEqual([text_of(c) for c in by_number["21"][2:]],
                         ["2026-09-22 10:00", "2026-W39", "경량", "엄격", "엄격", "예 (재판정 높음)",
                          "차단 2·확인 1·검증 실패 0", "-", "아니오"])  # 지표 6 이전 JSON은 에이전트 기록 `-`
        self.assertEqual(text_of(by_number["23"][8]), "미보고")
        self.assertEqual(report.link("javascript:alert(1)", "22"), "22")
        self.assertEqual(report.link("data:text/html,x", "1"), "1")
        self.assertEqual(report.link("//evil.example/x", "1"), "1")
        self.assertEqual(report.link("ftp://files.example/x", "1"), "1")
        self.assertEqual(report.link(None, "1"), "1")
        self.assertEqual(report.link(" HTTPS://gitlab.example/a?b=1&c=2 ", "a<b"),
                         '<a href="HTTPS://gitlab.example/a?b=1&amp;c=2">a&lt;b</a>')
        self.assertEqual(report.esc("x\"y'z"), "x&quot;y&#x27;z")
        # XML에 들어갈 수 없는 제어 문자는 지운다. 어느 필드에 와도 파싱된다
        nasty = dict(self.data, project="데모\x01", window={"until": "\x02"},
                     mrs=[{"number": 1, "title": "a\x00b", "url": "https://gitlab.example/\x07", "tier": "strict"},
                          {"number": 2, "tier": {"mismatch": True, "body": ["lite"], "judge": {"x": 1}},
                           "mismatch_higher": ["judge"]}],
                     defects=[{"kind": 3, "number": True, "status": None}, {"kind": ["issue"], "status": {"a": 1}, "cause": [1]}],
                     jobs=[{"id": 9, "rules": "x", "category": 7}, {"id": 10, "category": ["test"], "rules": [1, "a"]}])
        root = parse(report.render(nasty, None))
        self.assertEqual(text_of(find_all(root, "title")[0]), "데모 측정 리포트")
        # 문자열 자리에 목록·객체가 와도(리뷰 F1) 해시하지 않고 대체값으로 보인다
        mr_table, defect_table, job_table = raw_tables(root)
        self.assertEqual(rows(mr_table)[1][4:], ["-", "-", "-", "예", "-", "-", "아니오"])
        self.assertEqual(rows(defect_table)[1][1:], ["-", "", "", "-", "-", "-"])
        self.assertEqual(rows(job_table)[1][2:4], ["-", "a"])

    def test_empty_weeks(self):
        """T6: weeks가 비면 종료 0, 그래프 대신 "데이터 없음" 안내. 원자료 표는 그대로 보인다."""
        empty = dict(self.data, weeks=[])
        html = report.render(empty, None)
        root = parse(html)
        self.assertEqual(find_all(root, "svg"), [])
        self.assertEqual(find_all(root, "figure"), [])
        self.assertEqual(html.count("데이터 없음"), 9)  # 요약 + 지표 1~8
        self.assertEqual([text_of(h) for h in find_all(root, "h2")], H2)
        self.assertEqual(len([t for t in raw_tables(root) if t is not None]), 3)
        with tempfile.TemporaryDirectory() as tmp:
            source, out = Path(tmp) / "metrics.json", Path(tmp) / "report.html"
            source.write_text(json.dumps(empty), encoding="utf-8")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                self.assertEqual(report.main(["--in", str(source), "--out", str(out)]), 0)
            self.assertIn("주 0개", stderr.getvalue())
            parse(out.read_text(encoding="utf-8"))
        # 최소 입력(원자료·window·project 없음)도 렌더한다
        root = parse(report.render({"version": 1, "weeks": []}, None))
        self.assertEqual(text_of(find_all(root, "title")[0]), "측정 리포트")
        self.assertEqual(raw_tables(root), [None, None, None])

    def test_ci_categories_stacked(self):
        """T7: 누적 막대 세그먼트 수 = 0이 아닌 범주 수, 높이는 값에 비례, 범례 6개 고정 순서."""
        svg, _table = figure_tables(self.root)[2]
        segments = [r for r in svg.iter() if local(r.tag) == "rect" and "seg" in r.get("class", "").split()]
        by_week = {}
        for segment in segments:
            by_week.setdefault(segment.get("data-week"), []).append(segment)
        self.assertEqual(len(segments), 6)
        self.assertEqual([s.get("data-category") for s in by_week["2026-W38"]], ["test"])
        self.assertEqual([s.get("data-category") for s in by_week["2026-W39"]], ["test", "format", "timeout"])
        self.assertEqual([s.get("data-category") for s in by_week["2026-W40"]], ["infra", "unclassified"])
        heights = {s.get("data-category"): float(s.get("height")) for s in by_week["2026-W39"]}
        self.assertAlmostEqual(heights["test"], 2 * heights["format"], places=1)
        self.assertAlmostEqual(heights["format"], heights["timeout"], places=1)
        self.assertAlmostEqual(heights["format"], float(by_week["2026-W38"][0].get("height")), places=1)
        # 아래부터 위로 쌓인다: 첫 세그먼트 아래가 기준선, 다음 세그먼트 아래가 앞 세그먼트 위
        first, second = by_week["2026-W39"][0], by_week["2026-W39"][1]
        self.assertAlmostEqual(float(first.get("y")) + float(first.get("height")), report.BASELINE, places=1)
        self.assertAlmostEqual(float(second.get("y")) + float(second.get("height")), float(first.get("y")), places=1)
        for segment in segments:
            self.assertEqual(segment.get("class"), f"seg c-{segment.get('data-category')}")
        self.assertEqual(value_labels(svg), ["1", "4", "2"])  # 주별 합계
        figure = find_all(self.root, "figure")[2]
        legend = next(child for child in figure if local(child.tag) == "ul")
        self.assertEqual([text_of(li) for li in legend], ["테스트 실패 (test)", "포맷 (format)", "인프라 (infra)",
                                                         "타임아웃 (timeout)", "보안 (security)", "미분류 (unclassified)"])
        self.assertEqual([li[0].get("class") for li in legend], [f"swatch c-{c}" for c in report.CATEGORIES])
        self.assertEqual(report.CATEGORIES, ("test", "format", "infra", "timeout", "security", "unclassified"))


class UnitTest(unittest.TestCase):
    def test_scale_and_normalization(self):
        """리뷰 F3: y축 눈금(1·2·5 단위, 최댓값을 덮음), 숫자 정규화(bool·float·문자열), 막대가 그림 안에 있다."""
        self.assertEqual(report.nice_scale(0), (1, [0, 1]))
        self.assertEqual(report.nice_scale(1), (1, [0, 1]))
        self.assertEqual(report.nice_scale(3), (3, [0, 1, 2, 3]))
        self.assertEqual(report.nice_scale(7), (8, [0, 2, 4, 6, 8]))
        self.assertEqual(report.nice_scale(23), (25, [0, 5, 10, 15, 20, 25]))
        self.assertEqual(report.nice_scale(130), (150, [0, 50, 100, 150]))
        for value in (1, 7, 23, 130):
            top, ticks = report.nice_scale(value)
            self.assertGreaterEqual(top, value)
            self.assertEqual(ticks[-1], top)
            self.assertLessEqual(len(ticks), 6)
        self.assertEqual([report._int(v) for v in (True, False, 2.7, float("nan"), float("inf"), "3", None, -4)],
                         [0, 0, 2, 0, 0, 0, 0, -4])
        weeks = [{"week": "2026-W40", "start": "2026-09-28", "escaped_defects": 7, "merged_mrs": 23, "strict": 23,
                  "mismatch": 130, "ci_failures": {"test": 130}}]
        root = parse(report.render({"version": 1, "weeks": weeks}, None))
        for svg, _table in figure_tables(root):
            for rect in svg.iter():
                if local(rect.tag) == "rect":
                    self.assertGreaterEqual(float(rect.get("y")), report.PAD_TOP - 0.01)
                    self.assertGreaterEqual(float(rect.get("height")), 0)  # 값 0인 막대는 높이 0
        axis_texts = [text_of(t) for t in figure_tables(root)[3][0].iter() if local(t.tag) == "text" and t.get("class") == "axis"]
        self.assertIn("150", axis_texts)

    def test_timezone_precedence(self):
        """리뷰 F3: 표시 시간대는 window.utc_offset, 없으면 until의 오프셋, 둘 다 없거나 잘못되면 UTC."""
        tz = report.report_timezone({"utc_offset": "+09:00", "until": "2026-10-01T00:00:00+00:00"})
        self.assertEqual(report.offset_label(tz), "+09:00")
        self.assertEqual(report.offset_label(report.report_timezone({"until": "2026-10-01T00:00:00-05:00"})), "-05:00")
        self.assertEqual(report.offset_label(report.report_timezone({})), "+00:00")
        self.assertEqual(report.offset_label(report.report_timezone({"utc_offset": "+15:00"})), "+00:00")
        self.assertEqual(report.fmt_time("2026-10-04T16:00:00Z", tz), "2026-10-05 01:00")
        self.assertEqual(report.fmt_time("어제", tz), "어제")

    def test_header_cells_escaped(self):
        """리뷰 F3: 표 머리글(이번 주 <week>)에 들어가는 입력도 이스케이프된다."""
        weeks = [{"week": 'W<"x>', "start": "'s'&", "merged_mrs": 1, "strict": 1}]
        html = report.render({"version": 1, "weeks": weeks}, None)
        root = parse(html)
        self.assertNotIn('W<"x>', html)
        self.assertIn("W&lt;&quot;x&gt;", html)
        self.assertEqual(header_cells(summary_table(root))[1], '이번 주 W<"x>')
        self.assertEqual(rows(figure_tables(root)[0][1])[0][:2], ['W<"x>', "'s'&"])


class ContractTest(unittest.TestCase):
    def test_collect_output_renders(self):
        """T8: 수집기(GitLab fixture, 가짜 전송) 출력을 그대로 렌더한다. MR 번호·job id가 원자료 표에 나온다."""
        code, result, text, stderr = collect_tests.run_collect("gitlab", collect_tests.gitlab_api())
        self.assertEqual(code, 0, stderr)
        html = report.render(result, None)
        root = parse(html)
        self.assertEqual(len(find_all(root, "svg")), 7)
        self.assertEqual(text_of(find_all(root, "title")[0]), "1 측정 리포트")
        mr_table, defect_table, job_table = raw_tables(root)
        self.assertEqual(sorted(int(r[0]) for r in rows(mr_table)), sorted(mr["number"] for mr in result["mrs"]))
        self.assertEqual(sorted(int(r[0]) for r in rows(job_table)), [501, 502])
        self.assertEqual(len(rows(defect_table)), len(result["defects"]))
        by_number = {row[0]: row for row in rows(mr_table)}
        self.assertEqual(by_number["11"][4:], ["경량", "엄격", "엄격", "예 (재판정 높음)", "미보고", "미보고", "아니오"])
        self.assertEqual(by_number["10"][8], "차단 2·확인 1·검증 실패 0")
        self.assertEqual(by_number["13"][4:], ["-", "-", "-", "-", "-", "-", "예"])  # 통합 MR은 판정이 없다
        by_job = {row[0]: row for row in rows(job_table)}
        self.assertEqual(by_job["501"][2], "테스트 실패 (test)")
        summary = summary_table(root)
        self.assertEqual(header_cells(summary)[1:3], ["이번 주 2026-W40", "직전 주 2026-W39"])
        by_label = {row[0]: row[1:] for row in rows(summary)}
        self.assertEqual(by_label["2 엄격 비율"], ["-", "33% (1/3)", "-"])
        self.assertEqual(by_label["5 hooks 개입 수"], ["0", "3", "-3"])
        self.assertEqual(by_label["hooks 기록 보고 MR (참고)"], ["0/0", "1/3", "-"])
        # 수집기가 쓴 파일을 그대로 CLI에 넣어도 같다
        with tempfile.TemporaryDirectory() as tmp:
            source, out = Path(tmp) / "metrics.json", Path(tmp) / "report.html"
            source.write_text(text, encoding="utf-8")
            with redirect_stderr(io.StringIO()):
                self.assertEqual(report.main(["--in", str(source), "--out", str(out)]), 0)
            self.assertEqual(out.read_text(encoding="utf-8"), html)


class CliTest(unittest.TestCase):
    def test_harness_metrics_report(self):
        """T9: `harness metrics report --in --out` → 종료 0, 파일 생성, stderr 요약. 없는 파일·JSON 아님·version 2 → 종료 2, 출력 없음."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "nested" / "report.html"
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = harness.main(["metrics", "report", "--in", str(SAMPLE), "--out", str(out), "--title", "데모 리포트"])
            self.assertEqual(code, 0, stderr.getvalue())
            self.assertIn("주 3개, MR 4개, 결함 3개, 실패 job 3개", stderr.getvalue())
            self.assertIn(str(out), stderr.getvalue())
            html = out.read_text(encoding="utf-8")
            self.assertIn("<title>데모 리포트</title>", html)
            parse(html)
            self.assertEqual(out.read_bytes().count(b"\r"), 0)
            self.assertEqual(sorted(p.name for p in out.parent.iterdir()), ["report.html"])  # 임시 파일이 남지 않는다

            bad_inputs = {"missing.json": None, "text.json": "not json {", "array.json": "[1, 2]",
                          "v2.json": json.dumps({"version": 2, "weeks": []}),
                          "no-weeks.json": json.dumps({"version": 1, "weeks": {}})}
            for name, content in bad_inputs.items():
                with self.subTest(name=name):
                    source, bad_out = Path(tmp) / name, Path(tmp) / f"{name}.html"
                    if content is not None:
                        source.write_text(content, encoding="utf-8")
                    stderr = io.StringIO()
                    with redirect_stderr(stderr):
                        code = harness.main(["metrics", "report", "--in", str(source), "--out", str(bad_out)])
                    self.assertEqual(code, 2)
                    self.assertIn("오류: 리포트 생성 실패", stderr.getvalue())
                    self.assertIn(str(source), stderr.getvalue())
                    self.assertNotIn("not json", stderr.getvalue())  # 입력 내용은 메시지에 옮기지 않는다
                    self.assertFalse(bad_out.exists())
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = report.main(["--in", str(Path(tmp) / "v2.json"), "--out", str(Path(tmp) / "x.html")])
            self.assertEqual(code, 2)
            self.assertIn("harness: 리포트를 만들 수 없다", stderr.getvalue())
            self.assertFalse((Path(tmp) / "x.html").exists())
        with mock.patch.object(report, "JSON_MAX_BYTES", 10):
            with self.assertRaises(report.ReportError) as caught:
                report.load_input(SAMPLE)
        self.assertIn("너무 크다", str(caught.exception))

    def test_atomic_write(self):
        """T10: 교체(os.replace)가 실패하면 기존 report.html을 남기고 임시 파일을 지운다(종료 2)."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.html"
            out.write_text("previous", encoding="utf-8")
            stderr = io.StringIO()
            with mock.patch.object(harness.metrics_report.os, "replace", side_effect=OSError("busy")), \
                    redirect_stderr(stderr):
                code = harness.main(["metrics", "report", "--in", str(SAMPLE), "--out", str(out)])
            self.assertEqual(code, 2)
            self.assertIn("출력 파일을 쓸 수 없다", stderr.getvalue())
            self.assertEqual(out.read_text(encoding="utf-8"), "previous")
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["report.html"])
            with redirect_stderr(io.StringIO()):
                self.assertEqual(harness.main(["metrics", "report", "--in", str(SAMPLE), "--out", str(out)]), 0)
            self.assertTrue(out.read_text(encoding="utf-8").startswith("<!DOCTYPE html>"))
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["report.html"])


if __name__ == "__main__":
    unittest.main()
