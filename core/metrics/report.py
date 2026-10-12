#!/usr/bin/env python3
"""주간 HTML 리포트(M4-6). 표준 라이브러리만 쓰고 다른 키트 모듈을 import하지 않는다.

수집기(`collect.py`)가 쓴 `metrics.json`(`version` 1)을 읽어 5개 지표의 주별 추세(인라인 SVG와 같은 값의 표),
이번 주 요약, 원자료 표(MR·결함·job)를 정적 HTML 한 파일로 쓴다(README "리포트" 절). 외부 스크립트·CDN·폰트·
이미지가 없어 사내망·오프라인에서도 열리고, XHTML 호환이라 XML 파서로도 읽힌다.

    python core/metrics/report.py [--in metrics.json] [--out report.html] [--title 제목]

입력 문자열은 모두 이스케이프하고, 링크는 URL 스킴이 http·https일 때만 `<a href>`로 만든다. 네트워크를 쓰지 않는다.

종료 코드: 성공 0, 입력·출력 오류 2.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import os
import re
import sys
import tempfile
import urllib.parse
from pathlib import Path

SCHEMA_VERSION = 1
EXIT_OK, EXIT_ERROR = 0, 2
JSON_MAX_BYTES = 50_000_000
DEFAULT_IN = "metrics.json"
DEFAULT_OUT = "report.html"
LINK_SCHEMES = ("http", "https")
# CI 실패 범주. classify_ci.CATEGORIES와 같은 순서지만 import하지 않고 고정 목록으로 둔다(README에 고정된 계약)
CATEGORIES = ("test", "format", "infra", "timeout", "security", "unclassified")
CATEGORY_LABELS = {"test": "테스트 실패", "format": "포맷", "infra": "인프라", "timeout": "타임아웃",
                   "security": "보안", "unclassified": "미분류"}
TIER_LABELS = {"lite": "경량", "standard": "표준", "strict": "엄격"}
SIDE_LABELS = {"body": "본문", "judge": "재판정"}
HOOKS_KEYS = ("block", "ask", "verify_fail", "reported_mrs", "unreported_mrs")  # collect.py 주별 `hooks`
KIND_LABELS = {"issue": "이슈", "mr": "MR"}
STATUS_LABELS = {"linked": "원인 MR 연결", "unlinked": "원인 미기재", "out_of_window": "기간 밖",
                 "cause_not_merged": "원인 MR 미병합"}
OFFSET_FORMAT = re.compile(r"^([+-])(\d\d):(\d\d)$")
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")  # XML 문서에 들어갈 수 없는 제어 문자
EMPTY = '<p class="empty">데이터 없음</p>'

# SVG: viewBox는 고정하고 표시 폭은 CSS로 맞춘다
SVG_WIDTH, SVG_HEIGHT = 720, 240
PAD_LEFT, PAD_RIGHT, PAD_TOP, PAD_BOTTOM = 44, 16, 20, 44
PLOT_WIDTH = SVG_WIDTH - PAD_LEFT - PAD_RIGHT
PLOT_HEIGHT = SVG_HEIGHT - PAD_TOP - PAD_BOTTOM
BASELINE = PAD_TOP + PLOT_HEIGHT
BAR_MAX_WIDTH = 24
BAR_GAP = 2
MAX_LABELS = 10  # x축·값 라벨은 주가 많으면 이 수 안팎으로 간격을 두고 표시한다(표에는 모든 값이 있다)
SVG_NS = "http://www.w3.org/2000/svg"

# 색은 CSS 변수 한 곳에서 정한다(라이트·다크). 범주 6개는 고정 순서의 팔레트로, 색각 이상 시뮬레이션 검증을 통과한 조합이다
CSS = """
:root{color-scheme:light;--bg:#fcfcfb;--fg:#0b0b0b;--muted:#52514e;--grid:#e6e5e1;--border:#d6d5d0;--row:#f3f2ef;
--c-test:#2a78d6;--c-format:#eb6834;--c-infra:#1baf7a;--c-timeout:#eda100;--c-security:#e87ba4;--c-unclassified:#008300}
@media (prefers-color-scheme: dark){:root{color-scheme:dark;--bg:#1a1a19;--fg:#f4f3ee;--muted:#c3c2b7;--grid:#34342f;
--border:#454540;--row:#232322;--c-test:#3987e5;--c-format:#d95926;--c-infra:#199e70;--c-timeout:#c98500;
--c-security:#d55181;--c-unclassified:#008300}}
html{background:var(--bg);color:var(--fg)}
body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans KR","Malgun Gothic",sans-serif;
font-size:14px;line-height:1.5}
main{max-width:960px;margin:0 auto;padding:16px}
h1{font-size:22px;margin:0 0 8px}
h2{font-size:17px;margin:32px 0 8px;padding-top:12px;border-top:1px solid var(--border)}
dl.meta{display:grid;grid-template-columns:max-content 1fr;gap:2px 12px;margin:0 0 8px}
dl.meta dt{color:var(--muted)} dl.meta dd{margin:0}
figure{margin:0 0 8px;overflow-x:auto}
svg{display:block;width:100%;height:auto;max-width:720px}
svg text{fill:var(--fg);font-size:11px}
svg .axis{fill:var(--muted);font-size:10px}
svg .grid{stroke:var(--grid);stroke-width:1}
svg .base{stroke:var(--border);stroke-width:1}
svg .line{fill:none;stroke:var(--c-test);stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
svg .dot{fill:var(--c-test);stroke:var(--bg);stroke-width:2}
svg .seg{stroke:var(--bg);stroke-width:1}
.s1{fill:var(--c-test);background:var(--c-test)} .s2{fill:var(--c-format);background:var(--c-format)}
.s3{fill:var(--c-infra);background:var(--c-infra)}
.c-test{fill:var(--c-test);background:var(--c-test)} .c-format{fill:var(--c-format);background:var(--c-format)}
.c-infra{fill:var(--c-infra);background:var(--c-infra)} .c-timeout{fill:var(--c-timeout);background:var(--c-timeout)}
.c-security{fill:var(--c-security);background:var(--c-security)}
.c-unclassified{fill:var(--c-unclassified);background:var(--c-unclassified)}
ul.legend{list-style:none;display:flex;flex-wrap:wrap;gap:4px 16px;margin:0 0 4px;padding:0}
.swatch{display:inline-block;width:12px;height:12px;border-radius:2px;vertical-align:-1px;margin-right:6px}
table{border-collapse:collapse;margin:8px 0;font-size:13px}
th,td{border:1px solid var(--border);padding:4px 8px;text-align:left;vertical-align:top}
th{background:var(--row)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
details{margin:8px 0} summary{cursor:pointer;font-weight:600} .wrap{overflow-x:auto}
.note,.empty{color:var(--muted)}
a{color:inherit}
""".strip()


class ReportError(Exception):
    """리포트를 만들 수 없는 입력·출력 오류(종료 2). 메시지에는 파일 경로만 넣고 입력 내용은 옮기지 않는다."""


# ---------------------------------------------------------------------------
# 값 정규화와 이스케이프
# ---------------------------------------------------------------------------


def _text(value) -> str:
    """문자열 필드. 문자열이 아니면 빈 값(번호·id 같은 정수는 그대로 보인다)."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _int(value) -> int:
    """숫자 필드. 없거나 숫자가 아니면 0."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    return 0


def _label(mapping: dict, value, default=None):
    """고정 라벨 조회. 문자열이 아닌 값(목록·객체 등)은 해시하지 않고 default를 돌려준다."""
    return mapping.get(value, default) if isinstance(value, str) else default


def esc(value) -> str:
    return html.escape(CONTROL_CHARS.sub("", _text(value)), quote=True)


def link(url, label) -> str:
    """URL 스킴이 http·https일 때만 링크, 아니면 라벨 텍스트만(javascript:·data: 등은 URL도 보이지 않는다)."""
    text = esc(label)
    if isinstance(url, str):
        target = url.strip()
        try:
            scheme = urllib.parse.urlsplit(target).scheme.lower()
        except ValueError:
            scheme = ""
        if scheme in LINK_SCHEMES:
            return f'<a href="{esc(target)}">{text}</a>'
    return text


def records(value) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def num(value: float) -> str:
    """SVG 좌표. 소수 한 자리, 정수면 소수점 없이."""
    text = f"{value:.1f}"
    return text[:-2] if text.endswith(".0") else text


# ---------------------------------------------------------------------------
# 시간
# ---------------------------------------------------------------------------


def parse_time(value) -> dt.datetime | None:
    """ISO 8601 시각(`2026-10-04T16:00:00Z`, `+09:00`). 형식이 아니면 None. 시간대가 없으면 UTC로 본다."""
    if not isinstance(value, str):
        return None
    try:
        moment = dt.datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)


def parse_date(value) -> dt.date | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.date.fromisoformat(value.strip())
    except ValueError:
        return None


def report_timezone(window: dict) -> dt.timezone:
    """표시 시간대: `window.utc_offset`, 없으면 `window.until`의 오프셋, 없으면 UTC."""
    match = OFFSET_FORMAT.match(_text(window.get("utc_offset")))
    if match and int(match.group(2)) <= 14 and int(match.group(3)) < 60:
        minutes = int(match.group(2)) * 60 + int(match.group(3))
        return dt.timezone(dt.timedelta(minutes=-minutes if match.group(1) == "-" else minutes))
    until = parse_time(window.get("until"))
    if until is not None and isinstance(until.tzinfo, dt.timezone):
        return until.tzinfo
    return dt.timezone.utc


def offset_label(tz: dt.timezone) -> str:
    seconds = int(tz.utcoffset(None).total_seconds())
    sign, seconds = ("-" if seconds < 0 else "+"), abs(seconds)
    return f"{sign}{seconds // 3600:02d}:{seconds % 3600 // 60:02d}"


def fmt_time(value, tz: dt.timezone) -> str:
    moment = parse_time(value)
    return moment.astimezone(tz).strftime("%Y-%m-%d %H:%M") if moment else _text(value)


# ---------------------------------------------------------------------------
# 주별 값
# ---------------------------------------------------------------------------


def normalize_weeks(raw: list) -> list[dict]:
    """주 항목을 고정 필드로 맞춘다. 객체가 아닌 항목은 건너뛰고, ci_failures는 범주 6개를 항상 둔다."""
    weeks = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        failures = item.get("ci_failures") if isinstance(item.get("ci_failures"), dict) else {}
        hooks = item.get("hooks") if isinstance(item.get("hooks"), dict) else {}  # 지표 5 이전 JSON에는 없다
        weeks.append({"week": _text(item.get("week")), "start": _text(item.get("start")),
                      "merged_mrs": _int(item.get("merged_mrs")), "strict": _int(item.get("strict")),
                      "mismatch": _int(item.get("mismatch")), "escaped_defects": _int(item.get("escaped_defects")),
                      "unlinked_defects": _int(item.get("unlinked_defects")),
                      "ci_failures": {category: _int(failures.get(category)) for category in CATEGORIES},
                      **{f"hooks_{key}": _int(hooks.get(key)) for key in HOOKS_KEYS}})
    return weeks


def ratio(week: dict) -> tuple[int | None, str]:
    """엄격 비율(% 정수, 병합 MR이 없으면 None)과 `strict/merged` 표기."""
    merged, strict = week["merged_mrs"], week["strict"]
    fraction = f"{strict}/{merged}"
    if merged <= 0:
        return None, fraction
    return int(100 * strict / merged + 0.5), fraction


def ratio_text(pct: int | None, fraction: str) -> str:
    return "-" if pct is None else f"{pct}% ({fraction})"


def ci_total(week: dict) -> int:
    return sum(week["ci_failures"].values())


def hooks_total(week: dict) -> int:
    """지표 5 요약 값: hooks가 개입한 수(차단 + 확인 + 종료 검증 실패)."""
    return week["hooks_block"] + week["hooks_ask"] + week["hooks_verify_fail"]


def hooks_reported_text(week: dict) -> str:
    """`## hooks 기록`을 보고한 MR 수 / 집계 MR 수."""
    return f"{week['hooks_reported_mrs']}/{week['hooks_reported_mrs'] + week['hooks_unreported_mrs']}"


def delta_text(current: int, previous: int | None) -> str:
    if previous is None:
        return "-"
    diff = current - previous
    return "±0" if diff == 0 else f"{diff:+d}"


def delta_ratio_text(current: int | None, previous: int | None) -> str:
    if current is None or previous is None:
        return "-"
    diff = current - previous
    return "±0%p" if diff == 0 else f"{diff:+d}%p"


def in_progress(week: dict, window: dict) -> bool:
    """마지막 주 시작일부터 7일이 수집 기간 끝(`window.until`)보다 뒤면 진행 중."""
    start, until = parse_date(week["start"]), parse_time(window.get("until"))
    if start is None or until is None:
        return False
    end = dt.datetime.combine(start + dt.timedelta(days=7), dt.time(), tzinfo=until.tzinfo)
    return end > until


# ---------------------------------------------------------------------------
# SVG 그래프
# ---------------------------------------------------------------------------


def nice_scale(y_max: int) -> tuple[int, list[int]]:
    """y축 최댓값(눈금 5개 이하의 깔끔한 수)과 눈금 값. 최소 1."""
    y_max = max(int(y_max), 1)
    step, unit = 1, 1
    while y_max / step > 5:  # 1 → 2 → 5 → 10 → 20 → 50 → ...
        if step == unit:
            step = 2 * unit
        elif step == 2 * unit:
            step = 5 * unit
        else:
            unit *= 10
            step = unit
    top = max(step, math.ceil(y_max / step) * step)
    return top, list(range(0, top + 1, step))


def y_pos(value: float, top: int) -> float:
    return BASELINE - PLOT_HEIGHT * value / top


def label_step(count: int) -> int:
    return max(1, math.ceil(count / MAX_LABELS))


def show_label(index: int, count: int, step: int) -> bool:
    """간격을 둘 때 가장 최근 주(마지막)를 기준으로 라벨을 남긴다. 첫 주 기준이면 주 수에 따라 마지막 주 라벨이 빠진다."""
    return (count - 1 - index) % step == 0


def svg_open(title: str) -> str:
    return (f'<svg xmlns="{SVG_NS}" role="img" viewBox="0 0 {SVG_WIDTH} {SVG_HEIGHT}">'
            f"<title>{esc(title)}</title>")


def axes(weeks: list[dict], ticks: list[int], top: int, tick_text=str) -> str:
    """가로 눈금선·y 라벨·기준선·x 라벨(ISO 주와 시작일). 주가 많으면 x 라벨은 간격을 둔다."""
    parts = []
    right = SVG_WIDTH - PAD_RIGHT
    for tick in ticks:
        y = y_pos(tick, top)
        parts.append(f'<line class="grid" x1="{PAD_LEFT}" y1="{num(y)}" x2="{right}" y2="{num(y)}"/>')
        parts.append(f'<text class="axis" x="{PAD_LEFT - 6}" y="{num(y + 3.5)}" text-anchor="end">'
                     f"{esc(tick_text(tick))}</text>")
    parts.append(f'<line class="base" x1="{PAD_LEFT}" y1="{BASELINE}" x2="{right}" y2="{BASELINE}"/>')
    slot, step = PLOT_WIDTH / len(weeks), label_step(len(weeks))
    for index, week in enumerate(weeks):
        if not show_label(index, len(weeks), step):
            continue
        x = num(PAD_LEFT + slot * (index + 0.5))
        name, start = esc(week["week"]), esc(week["start"])
        parts.append(f'<text class="axis" x="{x}" y="{BASELINE + 14}" text-anchor="middle">{name}</text>')
        parts.append(f'<text class="axis" x="{x}" y="{BASELINE + 27}" text-anchor="middle">{start}</text>')
    return "".join(parts)


def value_label(x: float, y: float, text: str) -> str:
    return f'<text class="value" x="{num(x)}" y="{num(y)}" text-anchor="middle">{esc(text)}</text>'


def bar_chart(weeks: list[dict], series: list[tuple[str, str, str]], title: str) -> str:
    """막대 그래프. series는 (필드, 라벨, CSS 클래스) 목록이며 둘 이상이면 주마다 나란히 그린다."""
    count, size = len(weeks), len(series)
    top, ticks = nice_scale(max([week[key] for week in weeks for key, _, _ in series] + [1]))
    slot = PLOT_WIDTH / count
    group = min(slot * 0.7, size * BAR_MAX_WIDTH + (size - 1) * BAR_GAP)
    width = max(1.0, (group - (size - 1) * BAR_GAP) / size)
    step = label_step(count)
    parts = [svg_open(title), axes(weeks, ticks, top)]
    for index, week in enumerate(weeks):
        left = PAD_LEFT + slot * index + (slot - group) / 2
        name = esc(week["week"])
        for order, (key, label, css) in enumerate(series):
            value = week[key]
            x, y = left + order * (width + BAR_GAP), y_pos(value, top)
            parts.append(f'<rect class="bar {css}" x="{num(x)}" y="{num(y)}" width="{num(width)}" '
                         f'height="{num(BASELINE - y)}" data-week="{name}" data-series="{key}" '
                         f'data-value="{value}"><title>{name} {esc(label)} {value}</title></rect>')
            if show_label(index, count, step):
                parts.append(value_label(x + width / 2, y - 4, str(value)))
    parts.append("</svg>")
    return "".join(parts)


def line_chart(weeks: list[dict], title: str) -> str:
    """엄격 비율(%) 선 그래프. 병합 MR이 없는 주는 점을 찍지 않고 선을 끊는다. 점마다 `strict/merged`를 적는다."""
    count = len(weeks)
    slot, step, top = PLOT_WIDTH / count, label_step(count), 100
    parts = [svg_open(title), axes(weeks, [0, 25, 50, 75, 100], top, lambda tick: f"{tick}%")]
    points, segments, segment = [], [], []
    for index, week in enumerate(weeks):
        pct, fraction = ratio(week)
        x = PAD_LEFT + slot * (index + 0.5)
        if pct is None:
            if segment:
                segments.append(segment)
            segment = []
            if show_label(index, count, step):
                parts.append(f'<text class="axis" x="{num(x)}" y="{BASELINE - 6}" text-anchor="middle">-</text>')
            continue
        y = y_pos(100 * week["strict"] / week["merged_mrs"], top)
        segment.append((x, y))
        points.append((index, x, y, pct, fraction, week["week"]))
    if segment:
        segments.append(segment)
    for chain in segments:
        if len(chain) >= 2:
            coordinates = " ".join(f"{num(x)},{num(y)}" for x, y in chain)
            parts.append(f'<polyline class="line" points="{coordinates}"/>')
    for index, x, y, pct, fraction, name in points:
        name = esc(name)
        parts.append(f'<circle class="dot" cx="{num(x)}" cy="{num(y)}" r="4" data-week="{name}" '
                     f'data-value="{pct}"><title>{name} 엄격 비율 {pct}% ({esc(fraction)})</title></circle>')
        if show_label(index, count, step):
            parts.append(value_label(x, y - 9, f"{pct}% ({fraction})"))
    parts.append("</svg>")
    return "".join(parts)


def stacked_chart(weeks: list[dict], title: str) -> str:
    """CI 실패 원인 누적 막대. 범주는 고정 순서로 아래부터 쌓고 0인 범주는 그리지 않는다. 막대 위에 합계를 적는다."""
    count = len(weeks)
    slot, step = PLOT_WIDTH / count, label_step(count)
    top, ticks = nice_scale(max([ci_total(week) for week in weeks] + [1]))
    width = max(1.0, min(slot * 0.7, BAR_MAX_WIDTH))
    parts = [svg_open(title), axes(weeks, ticks, top)]
    for index, week in enumerate(weeks):
        x, bottom = PAD_LEFT + slot * (index + 0.5) - width / 2, float(BASELINE)
        name = esc(week["week"])
        for category in CATEGORIES:
            value = week["ci_failures"][category]
            if value <= 0:
                continue
            height = PLOT_HEIGHT * value / top
            bottom -= height
            parts.append(f'<rect class="seg c-{category}" x="{num(x)}" y="{num(bottom)}" width="{num(width)}" '
                         f'height="{num(height)}" data-week="{name}" data-category="{category}" '
                         f'data-value="{value}"><title>{name} {CATEGORY_LABELS[category]} {value}</title></rect>')
        if show_label(index, count, step):
            parts.append(value_label(x + width / 2, bottom - 4, str(ci_total(week))))
    parts.append("</svg>")
    return "".join(parts)


def legend(items: list[tuple[str, str]]) -> str:
    entries = "".join(f'<li><span class="swatch {css}"></span>{esc(label)}</li>' for css, label in items)
    return f'<ul class="legend">{entries}</ul>'


# ---------------------------------------------------------------------------
# HTML 조각
# ---------------------------------------------------------------------------


def table(headers: list[str], rows: list[list[str]], numeric=frozenset(), css: str = "") -> str:
    """표. rows의 셀은 이미 이스케이프된 HTML이다. numeric 열은 오른쪽 정렬."""
    def cell(tag: str, index: int, content: str) -> str:
        attr = ' class="num"' if index in numeric else ""
        return f"<{tag}{attr}>{content}</{tag}>"

    head = "".join(cell("th", index, esc(header)) for index, header in enumerate(headers))
    body = "".join("<tr>" + "".join(cell("td", index, content) for index, content in enumerate(row)) + "</tr>"
                   for row in rows)
    attr = f' class="{css}"' if css else ""
    return f"<table{attr}><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def header_html(heading: str, data: dict, window: dict, tz: dt.timezone, week_count: int) -> str:
    since, until = fmt_time(window.get("since"), tz), fmt_time(window.get("until"), tz)
    period = f"{since} ~ {until} ({offset_label(tz)})" if since or until else "-"
    items = (("프로젝트", _text(data.get("project")) or "-"), ("플랫폼", _text(data.get("platform")) or "-"),
             ("기간", period), ("생성 시각", fmt_time(data.get("generated_at"), tz) or "-"), ("주 수", str(week_count)))
    meta = "".join(f"<dt>{esc(name)}</dt><dd>{esc(value)}</dd>" for name, value in items)
    return f'<h1>{esc(heading)}</h1>\n<dl class="meta">{meta}</dl>\n'


def summary_html(weeks: list[dict], window: dict) -> str:
    """마지막 주의 5개 지표와 직전 주 대비 증감. 주가 하나면 증감은 `-`."""
    parts = ["<h2>이번 주 요약</h2>\n"]
    if not weeks:
        return "".join(parts + [EMPTY, "\n"])
    current, previous = weeks[-1], weeks[-2] if len(weeks) > 1 else None
    progress = in_progress(current, window)
    current_pct, current_fraction = ratio(current)
    previous_pct, previous_fraction = ratio(previous) if previous else (None, "")

    def pair(key) -> tuple[str, str, str]:
        value = key(current)
        before = key(previous) if previous else None
        return str(value), "-" if before is None else str(before), delta_text(value, before)

    rows = [["1 병합 후 누락 수", *pair(lambda week: week["escaped_defects"])],
            ["2 엄격 비율", ratio_text(current_pct, current_fraction),
             ratio_text(previous_pct, previous_fraction) if previous else "-",
             delta_ratio_text(current_pct, previous_pct)],
            ["3 CI 실패 수", *pair(ci_total)],
            ["4 판정 불일치 수", *pair(lambda week: week["mismatch"])],
            ["5 hooks 개입 수", *pair(hooks_total)],
            ["hooks 기록 보고 MR (참고)", hooks_reported_text(current),
             hooks_reported_text(previous) if previous else "-", "-"],
            ["병합 MR 수 (참고)", *pair(lambda week: week["merged_mrs"])]]
    rows = [[esc(cell) for cell in row] for row in rows]
    current_head = f"이번 주 {current['week']}" + (" (진행 중)" if progress else "")
    previous_head = f"직전 주 {previous['week']}" if previous else "직전 주"
    parts.append(table(["지표", current_head, previous_head, "증감"], rows, {1, 2, 3}, css="summary"))
    parts.append(f'\n<p class="note">이번 주 {esc(current["week"])}은 {esc(current["start"])}에 시작한다.')
    if progress:
        parts.append(" (진행 중): 수집 기간 끝까지 7일을 채우지 못한 주라 값이 더 늘 수 있다.")
    parts.append("</p>\n")
    return "".join(parts)


def metric_html(heading: str, weeks: list[dict], chart: str, table_html: str, legend_html: str = "") -> str:
    body = f"<figure>{legend_html}{chart}{table_html}</figure>" if weeks else EMPTY
    return f"<h2>{esc(heading)}</h2>\n{body}\n"


def week_cells(week: dict) -> list[str]:
    return [esc(week["week"]), esc(week["start"])]


def metrics_html(weeks: list[dict]) -> str:
    """지표 5개. 그래프마다 바로 뒤에 같은 값의 표를 둔다. weeks가 비면 그래프 대신 '데이터 없음'."""
    parts = []
    series = [("escaped_defects", "병합 후 누락", "s1"), ("unlinked_defects", "원인 미연결", "s2")]
    rows = [week_cells(w) + [str(w["escaped_defects"]), str(w["unlinked_defects"])] for w in weeks]
    parts.append(metric_html("지표 1 병합 후 누락 수", weeks,
                             bar_chart(weeks, series, "주별 병합 후 누락 수와 원인 미연결 결함 수") if weeks else "",
                             table(["주", "시작일", "병합 후 누락", "원인 미연결"], rows, {2, 3}),
                             legend([(css, label) for _, label, css in series])))
    rows = []
    for week in weeks:
        pct, fraction = ratio(week)
        rows.append(week_cells(week) + ["-" if pct is None else f"{pct}%", esc(fraction)])
    parts.append(metric_html("지표 2 엄격 비율", weeks, line_chart(weeks, "주별 엄격 단계 MR 비율") if weeks else "",
                             table(["주", "시작일", "엄격 비율", "엄격/병합 MR"], rows, {2, 3})))
    rows = [week_cells(w) + [str(w["ci_failures"][c]) for c in CATEGORIES] + [str(ci_total(w))] for w in weeks]
    parts.append(metric_html("지표 3 CI 실패 원인", weeks, stacked_chart(weeks, "주별 CI 실패 원인 분류") if weeks else "",
                             table(["주", "시작일", *(CATEGORY_LABELS[c] for c in CATEGORIES), "합계"], rows,
                                   set(range(2, 2 + len(CATEGORIES) + 1))),
                             legend([(f"c-{c}", f"{CATEGORY_LABELS[c]} ({c})") for c in CATEGORIES])))
    rows = [week_cells(w) + [str(w["mismatch"]), str(w["merged_mrs"])] for w in weeks]
    parts.append(metric_html("지표 4 판정 불일치 수", weeks,
                             bar_chart(weeks, [("mismatch", "판정 불일치", "s1")], "주별 판정 불일치 수") if weeks else "",
                             table(["주", "시작일", "판정 불일치", "병합 MR"], rows, {2, 3})))
    series = [("hooks_block", "차단(block)", "s1"), ("hooks_ask", "확인(ask)", "s2"),
              ("hooks_verify_fail", "종료 검증 실패", "s3")]
    rows = [week_cells(w) + [str(w[key]) for key, _, _ in series] + [esc(hooks_reported_text(w))] for w in weeks]
    parts.append(metric_html("지표 5 hooks 기록", weeks,
                             bar_chart(weeks, series, "주별 hooks 차단·확인·종료 검증 실패 수") if weeks else "",
                             table(["주", "시작일", "차단", "확인", "종료 검증 실패", "보고 MR"], rows, {2, 3, 4, 5}),
                             legend([(css, label) for _, label, css in series])))
    parts.append('<p class="note">MR 본문 "## hooks 기록" 절의 합계다. 보고 MR은 그 절을 채운 MR 수 / 집계 MR 수이며, '
                 '미보고는 hooks를 끄고 작업했거나 절차를 건너뛴 MR일 수 있다.</p>\n')
    return "".join(parts)


def tier_label(value) -> str:
    return TIER_LABELS.get(value, "-") if isinstance(value, str) else "-"


def hooks_cell(mr: dict) -> str:
    hooks = mr.get("hooks")
    if isinstance(hooks, dict):
        return f"차단 {_int(hooks.get('block'))}·확인 {_int(hooks.get('ask'))}·검증 실패 {_int(hooks.get('verify_fail'))}"
    if mr.get("integration") is True or not isinstance(mr.get("tier"), dict) or "hooks" not in mr:
        return "-"  # 통합 MR, 또는 지표 5 이전 JSON
    return "미보고"


def mr_row(mr: dict, tz: dt.timezone) -> list[str]:
    tier = mr.get("tier") if isinstance(mr.get("tier"), dict) else {}
    if not tier:
        mismatch = "-"
    elif tier.get("mismatch") is True:
        side = _label(SIDE_LABELS, mr.get("mismatch_higher"))
        mismatch = f"예 ({side} 높음)" if side else "예"
    else:
        mismatch = "아니오"
    return [link(mr.get("url"), _text(mr.get("number")) or "-"), esc(mr.get("title")),
            esc(fmt_time(mr.get("merged_at"), tz)), esc(mr.get("week")), esc(tier_label(tier.get("body"))),
            esc(tier_label(tier.get("judge"))), esc(tier_label(tier.get("effective"))), esc(mismatch),
            esc(hooks_cell(mr)), "예" if mr.get("integration") is True else "아니오"]


def defect_row(defect: dict, tz: dt.timezone) -> list[str]:
    kind, status, cause = defect.get("kind"), defect.get("status"), defect.get("cause")
    return [link(defect.get("url"), _text(defect.get("number")) or "-"), esc(_label(KIND_LABELS, kind, _text(kind)) or "-"),
            esc(defect.get("title")), esc(fmt_time(defect.get("created_at"), tz)),
            esc(str(cause) if isinstance(cause, int) and not isinstance(cause, bool) else "-"),
            esc(_label(STATUS_LABELS, status, _text(status)) or "-"), esc(defect.get("week")) or "-"]


def job_row(job: dict, tz: dt.timezone) -> list[str]:
    category = job.get("category")
    known = _label(CATEGORY_LABELS, category)
    label = f"{known} ({category})" if known else _text(category) or "-"
    rules = job.get("rules") if isinstance(job.get("rules"), list) else []
    return [link(job.get("url"), _text(job.get("id")) or "-"), esc(job.get("name")), esc(label),
            esc(", ".join(r for r in rules if isinstance(r, str)) or "-"), esc(fmt_time(job.get("created_at"), tz)),
            esc(job.get("week"))]


def details(summary_text: str, body: str) -> str:
    return f'<details><summary>{esc(summary_text)}</summary><div class="wrap">{body}</div></details>\n'


def raw_html(mrs: list[dict], defects: list[dict], jobs: list[dict], tz: dt.timezone) -> str:
    none = '<p class="empty">없음</p>'
    parts = ["<h2>원자료</h2>\n"]
    parts.append(details(f"병합 MR ({len(mrs)})", table(
        ["번호", "제목", "병합 시각", "주", "본문 판정", "재판정", "유효 판정", "불일치", "hooks 기록", "통합 MR"],
        [mr_row(mr, tz) for mr in mrs], {0}) if mrs else none))
    parts.append(details(f"병합 후 결함 ({len(defects)})", table(
        ["번호", "종류", "제목", "생성 시각", "원인 MR", "상태", "주"],
        [defect_row(defect, tz) for defect in defects], {0, 4}) if defects else none))
    parts.append(details(f"실패 CI job ({len(jobs)})", table(
        ["job id", "이름", "범주", "규칙", "생성 시각", "주"], [job_row(job, tz) for job in jobs], {0}) if jobs else none))
    return "".join(parts)


def render(data: dict, title: str | None = None) -> str:
    """수집 결과 dict를 XHTML 호환 HTML 문자열로 만든다. 입력은 `load_input`으로 검증된 것으로 본다."""
    weeks = normalize_weeks(data.get("weeks") if isinstance(data.get("weeks"), list) else [])
    window = data.get("window") if isinstance(data.get("window"), dict) else {}
    tz = report_timezone(window)
    project = _text(data.get("project"))
    heading = title or (f"{project} 측정 리포트" if project else "측정 리포트")
    mrs, defects, jobs = (records(data.get(key)) for key in ("mrs", "defects", "jobs"))
    return "".join([
        "<!DOCTYPE html>\n",
        '<html lang="ko"><head><meta charset="utf-8"/>',
        '<meta name="viewport" content="width=device-width, initial-scale=1"/>',
        f"<title>{esc(heading)}</title>\n<style>\n{CSS}\n</style></head>\n<body><main>\n",
        header_html(heading, data, window, tz, len(weeks)),
        summary_html(weeks, window),
        metrics_html(weeks),
        raw_html(mrs, defects, jobs, tz),
        "</main></body></html>\n",
    ])


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """report.py와 `harness metrics report`가 같은 옵션을 쓰게 한다."""
    parser.add_argument("--in", dest="input", default=DEFAULT_IN, help=f"수집기 출력 JSON(기본 {DEFAULT_IN})")
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"출력 HTML(기본 {DEFAULT_OUT})")
    parser.add_argument("--title", help="문서 제목(기본 '<project> 측정 리포트')")


def load_input(path: Path) -> dict:
    """metrics.json을 읽어 최소 구조(version 1, weeks 목록)를 검사한다. 오류 메시지에는 경로만 넣는다."""
    if not path.is_file():
        raise ReportError(f"입력 파일이 없다: {path}")
    try:
        if path.stat().st_size > JSON_MAX_BYTES:
            raise ReportError(f"입력 파일이 너무 크다({JSON_MAX_BYTES // 1_000_000} MB 상한): {path}")
        data = json.loads(path.read_bytes().decode("utf-8-sig"))
    except OSError as exc:
        raise ReportError(f"입력 파일을 읽을 수 없다({type(exc).__name__}): {path}") from None
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ReportError(f"입력이 JSON이 아니다: {path}") from None
    if not isinstance(data, dict):
        raise ReportError(f"입력 최상위는 객체여야 한다: {path}")
    if isinstance(data.get("version"), bool) or data.get("version") != SCHEMA_VERSION:
        raise ReportError(f"입력 version이 {SCHEMA_VERSION}이 아니다(수집기 출력만 읽는다): {path}")
    if not isinstance(data.get("weeks"), list):
        raise ReportError(f"입력의 weeks는 목록이어야 한다: {path}")
    return data


def write_atomic(path: Path, text: str) -> None:
    """임시 파일에 쓴 뒤 교체한다. 실패하면 기존 파일을 남기고 임시 파일을 지운다."""
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def run(args) -> dict:
    """입력을 읽어 HTML을 쓰고 요약용 dict를 돌려준다. 오류는 ReportError."""
    data = load_input(Path(args.input))
    text = render(data, args.title)
    try:
        write_atomic(Path(args.out), text)
    except OSError as exc:
        raise ReportError(f"출력 파일을 쓸 수 없다({type(exc).__name__}): {args.out}") from None
    return {"weeks": len(normalize_weeks(data["weeks"])), "mrs": len(records(data.get("mrs"))),
            "defects": len(records(data.get("defects"))), "jobs": len(records(data.get("jobs"))),
            "bytes": len(text.encode("utf-8"))}


def summary(result: dict, out: str) -> str:
    return (f"주 {result['weeks']}개, MR {result['mrs']}개, 결함 {result['defects']}개, 실패 job {result['jobs']}개의 "
            f"리포트({result['bytes'] / 1024:.0f} KB)를 {out}에 썼다.")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="report", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    add_arguments(parser)
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except ReportError as exc:
        print(f"harness: 리포트를 만들 수 없다: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(f"harness: {summary(result, args.out)}", file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
