"""로컬 실시간 대시보드(M5-7, #102). 표준 라이브러리만 쓰고 키트에서 실행한다(대상 저장소에 복사하지 않는다).

    python bin/harness.py dashboard [<target>|--self] [--port 8765]

- 같은 저장소의 모든 worktree 세션 기록(`sessions.jsonl`)과 hooks 기록(`events.jsonl`)을 읽어 진행 중 세션, 최근 개입,
  오늘·이번 주 합계, 기록 없는 커밋 경고를 보여 준다. 진행 중 세션의 토큰·추정 비용은 transcript에서 바로 센다.
- `127.0.0.1`에만 바인딩하고 Host 헤더가 localhost가 아니면 거절한다(DNS rebinding 방지). 외부 리소스를 부르지 않는다.
- 기록에는 프롬프트·코드 내용이 없고 화면에도 내지 않는다. transcript는 토큰 수만 센다.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
POLL_SEC = 1.0  # SSE가 상태를 다시 읽는 간격
UNRECORDED_SEC = 30.0  # 기록 없는 커밋(git 호출)을 다시 세는 간격
ACTIVE_WINDOW = dt.timedelta(hours=2)  # 마지막 기록이 이보다 오래되면 진행 중이 아니다(collect.IDLE_GAP과 같다)
RECENT_EVENTS = 30
TOP_BRANCHES = 10
DEFAULT_CHECKOUT = "(기본)"
ALLOWED_HOSTS = ("127.0.0.1", "localhost")
INDEX = Path(__file__).resolve().parent / "index.html"


def parse_lines(lines, collect) -> list[dict]:
    """JSON 객체이고 시각을 읽을 수 있는 줄만. 깨진 줄(범위 밖 월·오프셋 포함)은 건너뛴다."""
    records = []
    for line in lines:
        try:
            record = json.loads(line)
            ok = isinstance(record, dict) and collect.parse_ts(record.get("ts")) is not None
        except (ValueError, OverflowError, RecursionError):
            continue
        if ok:
            records.append(record)
    return records


def _str(value) -> str | None:
    return value if isinstance(value, str) and value else None


def session_rows(records: list[tuple[str, dict]], collect) -> dict[str, dict]:
    """세션 ID → 요약. records는 (worktree 이름, sessions.jsonl 기록).

    `deltas`는 사건마다 직전 누적과의 토큰 차(`collect.usage_delta`, `sessions summary`와 같은 계산)와 그 사건의 시각·브랜치,
    `stops`는 응답이 끝난 시각, `segment`는 현재 작업 구간의 시작(`start`나 긴 공백에서 끊는다)이다.
    """
    sessions: dict[str, dict] = {}
    for worktree, record in sorted(records, key=lambda item: collect.parse_ts(item[1]["ts"])):
        session = _str(record.get("session"))
        if session is None:
            continue
        moment, event = collect.parse_ts(record["ts"]), _str(record.get("event"))
        row = sessions.setdefault(session, {
            "session": session, "worktree": worktree, "tool": _str(record.get("tool")) or "?", "model": None,
            "transcript": None, "first": moment, "last": moment, "segment": moment, "event": None, "branch": None,
            "stops": [], "verify": None, "usage": {}, "usage_error": None, "deltas": [],
            "helper": isinstance(record.get("parent"), str)})
        if not collect.continues(row["last"], moment, event):
            row["segment"] = moment
        row["last"], row["event"] = moment, event
        row["branch"] = _str(record.get("branch")) or row["branch"]
        row["worktree"] = worktree
        if event == "start":
            row["model"] = _str(record.get("model")) or row["model"]
            row["transcript"] = _str(record.get("transcript")) or row["transcript"]
        if event == "stop":
            row["stops"].append(moment)
            row["verify"] = _str(record.get("verify"))
        row["usage_error"] = _str(record.get("usage_error"))
        usage = collect.clean_usage(record.get("usage"))
        if usage is not None:
            delta, row["usage"] = collect.usage_delta(usage, row["usage"], row["usage_error"] is not None)
            row["deltas"].append((moment, row["branch"], delta))
    return sessions


class UsageCache:
    """transcript 토큰 수 캐시. 본 transcript와 서브에이전트 transcript(`<transcript>/subagents/*.jsonl`)의 크기·수정 시각이
    모두 같으면 다시 읽지 않는다(서브에이전트만 쓰는 동안에도 갱신, #113 Codex 리뷰). (usage, 읽기 오류)를 돌려준다."""

    def __init__(self, transcript_usage):
        self.transcript_usage, self.cache, self.lock = transcript_usage, {}, threading.Lock()

    @staticmethod
    def stamp(path: Path) -> tuple:
        stat = path.stat()
        subagents = path.with_suffix("") / "subagents"
        children = []
        if subagents.is_dir():
            for child in sorted(subagents.glob("*.jsonl")):
                try:
                    child_stat = child.stat()
                except OSError:
                    continue
                children.append((child.name, child_stat.st_size, child_stat.st_mtime_ns))
        return stat.st_size, stat.st_mtime_ns, tuple(children)

    def get(self, path: str | None) -> tuple[dict | None, str | None]:
        if not path:
            return None, None
        try:
            key = self.stamp(Path(path))
        except OSError:
            return None, None
        with self.lock:
            cached = self.cache.get(path)
            if cached and cached[0] == key:
                return cached[1]
        result = self.transcript_usage(path)
        with self.lock:
            self.cache[path] = (key, result)
        return result


def build_state(session_records, event_records, now: dt.datetime, tz: dt.timezone, collect, pricing: dict,
                live_usage=None, unrecorded: list[dict] | None = None) -> dict:
    """화면에 낼 상태(JSON으로 직렬화할 수 있는 값만). 순수 함수라 테스트에서 시각과 입력을 정한다.

    오늘·이번 주 비용과 응답 수는 사건 단위로 그 사건의 날짜에 넣는다. 자정이나 월요일을 넘는 세션도 그날 쓴 만큼만 든다.
    진행 중 세션이 마지막 기록 뒤 transcript에 더 쓴 토큰은 지금 시각에 넣는다.
    """
    sessions = session_rows(session_records, collect)
    today = now.astimezone(tz).date()
    week_start = today - dt.timedelta(days=today.weekday())
    totals = {name: {"sessions": 0, "turns": 0, "interventions": 0, "cost": 0.0, "branches": {}, "unpriced": set(),
                     "excluded": {}} for name in ("today", "week")}

    def buckets(moment: dt.datetime) -> list[dict]:
        day = moment.astimezone(tz).date()
        return [totals[name] for name, start in (("today", today), ("week", week_start)) if day >= start]

    def spend(moment: dt.datetime, branch: str | None, delta: dict) -> None:
        cost, unpriced = collect.estimate_cost(delta, pricing)
        excluded = {model: sum(counts.values()) for model, counts in delta.items()
                    if model in collect.EXCLUDED_MODELS and collect.model_prices(model, pricing) is None}
        for bucket in buckets(moment):  # 단가 없는 모델의 토큰은 비용에 빠진다. 화면에 알린다(#116 리뷰 F3, #119 리뷰 F1)
            bucket["unpriced"].update(unpriced)
            for model, tokens in excluded.items():
                bucket["excluded"][model] = bucket["excluded"].get(model, 0) + tokens
        if cost <= 0:
            return
        for bucket in buckets(moment):
            bucket["cost"] += cost
            bucket["branches"][branch or "-"] = bucket["branches"].get(branch or "-", 0.0) + cost

    active = []
    for row in sessions.values():
        if row["helper"]:  # Codex 승인 검토·서브에이전트: 부모 세션의 토큰으로만 센다(#116)
            for moment, branch, delta in row["deltas"]:
                spend(moment, branch, delta)
            continue
        for bucket in buckets(row["last"]):
            bucket["sessions"] += 1
        for moment in row["stops"]:
            for bucket in buckets(moment):
                bucket["turns"] += 1
        for moment, branch, delta in row["deltas"]:
            spend(moment, branch, delta)
        if row["event"] == "end" or now - row["last"] > ACTIVE_WINDOW:
            continue
        usage = row["usage"]
        live, live_error = live_usage(row["transcript"]) if live_usage is not None else (None, None)
        usage_error = row["usage_error"]
        if live:
            # 일부 파일을 못 읽은 스냅숏은 줄어도 다시 시작으로 보지 않는다(같은 계산, #113 Codex 리뷰)
            spend(now, row["branch"], collect.usage_delta(live, usage, live_error is not None)[0])
            usage, usage_error = live, live_error
        elif live_error:  # 본 transcript를 못 읽으면 기록된 누적을 보이되 읽기 오류를 함께 낸다(#113 Codex 리뷰)
            usage_error = live_error
        cost, unpriced = collect.estimate_cost(usage, pricing)
        tokens = {field: sum(counts[field] for counts in usage.values()) for field in collect.USAGE_FIELDS}
        active.append({"session": row["session"][:8], "worktree": row["worktree"], "branch": row["branch"],
                       "tool": row["tool"], "model": row["model"], "started": row["segment"].isoformat(),
                       "last": row["last"].isoformat(),
                       "elapsed_min": int((now - row["segment"]).total_seconds() // 60),
                       "idle_min": int((now - row["last"]).total_seconds() // 60), "turns": len(row["stops"]),
                       "verify": row["verify"], "tokens": tokens, "cost": round(cost, 4), "unpriced": unpriced,
                       "usage_error": usage_error})
    events = []
    for worktree, record in event_records:
        moment = collect.parse_ts(record["ts"])
        decision = _str(record.get("decision"))
        if decision not in ("block", "ask", "fail"):
            continue
        for bucket in buckets(moment):
            bucket["interventions"] += 1
        events.append({"ts": moment.isoformat(), "worktree": worktree, "branch": _str(record.get("branch")),
                       "hook": _str(record.get("hook")), "decision": decision,
                       "rule": _str(record.get("rule")), "path": _str(record.get("path"))})
    events.sort(key=lambda item: item["ts"], reverse=True)
    # 브랜치는 이번 주 비용 상위 TOP_BRANCHES개를 고르고 오늘 값은 같은 브랜치로 맞춘다(따로 자르면 이번 주 목록의
    # 브랜치가 오늘 목록에서 빠져 0으로 보인다, #113 Codex 리뷰). 오늘은 이번 주 안이므로 오늘 브랜치는 모두 이번 주에 있다
    week_top = [branch for branch, _ in sorted(totals["week"]["branches"].items(),
                                               key=lambda item: (-item[1], item[0]))[:TOP_BRANCHES]]
    for bucket in totals.values():
        bucket["cost"] = round(bucket["cost"], 4)
        bucket["unpriced"] = sorted(bucket["unpriced"])
        bucket["excluded"] = [{"model": model, "tokens": tokens} for model, tokens in sorted(bucket["excluded"].items())
                              if tokens]
        bucket["branches"] = [{"branch": branch, "cost": round(bucket["branches"][branch], 4)}
                              for branch in week_top if branch in bucket["branches"]]
    active.sort(key=lambda item: item["last"], reverse=True)
    return {"now": now.isoformat(), "currency": pricing["currency"], "pricing_as_of": pricing["as_of"],
            "active": active, "events": events[:RECENT_EVENTS], "totals": totals, "unrecorded": unrecorded or []}


class Source:
    """대상 저장소의 모든 worktree 기록을 읽는다. `harness_common`·`collect` 모듈을 주입받는다.

    여러 스트림이 같은 상태를 나눠 쓴다(POLL_SEC 안에 다시 부르면 계산한 값을 돌려준다). 기록 없는 커밋은 git을 여러 번
    부르므로 별도 스레드가 UNRECORDED_SEC마다 다시 세고, 상태 계산은 그 결과를 기다리지 않는다.
    """

    def __init__(self, target: Path, common, collect, pricing: dict, tz: dt.timezone, base: str | None,
                 codex=None):
        self.target, self.common, self.collect, self.pricing, self.tz, self.base = \
            target, common, collect, pricing, tz, base
        self.codex = codex  # collect.CodexSessions: hooks가 없는 Codex 세션 로그(#116)
        self._roots: tuple[float, list[str], dict] = (0.0, [], {})
        self.usage = UsageCache(common.transcript_usage)
        self.lock = threading.Lock()
        self._state: tuple[float, dict | None] = (0.0, None)
        self._unrecorded: tuple[float | None, list] = (None, [])
        self._refreshing = False

    def state_dirs(self) -> list[tuple[str, Path]]:
        """(worktree 이름, `.git/harness` 경로). 기본 checkout은 `(기본)`, 나머지는 작업 디렉터리 이름이다."""
        common_dir = self.common.git(self.target, "rev-parse", "--path-format=absolute", "--git-common-dir") \
            or self.common.git(self.target, "rev-parse", "--git-common-dir")
        if not common_dir:
            return []
        root = Path(common_dir.strip())
        root = root if root.is_absolute() else self.target / root
        dirs = [(DEFAULT_CHECKOUT, root / "harness")]
        if (root / "worktrees").is_dir():
            dirs += [(worktree_name(admin), admin / "harness") for admin in sorted((root / "worktrees").iterdir())]
        return dirs

    def read(self, name: str, dirs: list[tuple[str, Path]]) -> list[tuple[str, dict]]:
        found = []
        for worktree, directory in dirs:
            path = directory / name
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.is_file() else []
            except OSError:
                continue
            found += [(worktree, record) for record in parse_lines(lines, self.collect)]
        return found

    def unrecorded(self, sessions: list[tuple[str, dict]]) -> list[dict]:
        """worktree마다 현재 브랜치의 기록 없는 커밋 수. 분리된 HEAD와 merge-base가 없는 worktree는 건너뛴다."""
        if not self.base:
            return []
        intervals = self.collect.summarize_sessions([json.dumps(record) for _, record in sessions], "")["intervals"]
        result = []
        out = self.common.git(self.target, "worktree", "list", "--porcelain") or ""
        for index, block in enumerate(out.strip().split("\n\n")):
            fields = dict(line.split(" ", 1) for line in block.splitlines() if " " in line)
            path, branch = fields.get("worktree"), fields.get("branch", "").removeprefix("refs/heads/")
            if not path or not branch:
                continue
            merge_base = self.common.git(Path(path), "merge-base", self.base, "HEAD")
            log = self.common.git(Path(path), "log", "--format=%H %cI", f"{merge_base.strip()}..HEAD") \
                if merge_base else None
            commits = [(sha, self.collect.parse_ts(stamp)) for sha, _, stamp in
                       (line.partition(" ") for line in (log or "").splitlines())]
            commits = [(sha, moment) for sha, moment in commits if moment is not None]
            missing = self.collect.unrecorded_commits(commits, intervals)
            if missing:
                result.append({"worktree": DEFAULT_CHECKOUT if index == 0 else Path(path).name, "branch": branch,
                               "unrecorded": len(missing), "commits": len(commits)})
        return result

    def codex_sessions(self) -> list[tuple[str, dict]]:
        """(worktree 이름, Codex 세션 사건). worktree 목록(git)은 UNRECORDED_SEC마다 다시 읽는다."""
        if self.codex is None:
            return []
        stamp, roots, timelines = self._roots
        if not roots or time.monotonic() - stamp >= UNRECORDED_SEC:
            out = self.common.git(self.target, "worktree", "list", "--porcelain") or ""
            roots = [line[len("worktree "):] for line in out.splitlines() if line.startswith("worktree ")]
            timelines = {root: self.collect.branch_timeline((self.common.git(
                Path(root), "reflog", "show", "--date=iso-strict", "--format=%gd%x09%gs", "HEAD") or "").splitlines())
                for root in roots}
            self._roots = (time.monotonic(), roots, timelines)
        names = {root: DEFAULT_CHECKOUT if index == 0 else Path(root).name for index, root in enumerate(roots)}
        try:
            return [(names.get(root, Path(root).name), event)
                    for root, event in self.codex.records(roots, timelines)]
        except OSError:
            return []

    def _refresh_unrecorded(self, sessions) -> None:
        try:
            result = self.unrecorded(sessions)
        except Exception:  # noqa: BLE001 - git 실패로 스레드가 죽어 경고가 영영 멈추지 않게 이전 값을 남긴다
            result = self._unrecorded[1]
        self._unrecorded = (time.monotonic(), result)
        self._refreshing = False

    def state(self, wait: bool = False) -> dict:
        """지금 상태. wait면 기록 없는 커밋을 이 자리에서 센다(`--once`)."""
        with self.lock:
            stamp, cached = self._state
            if cached is not None and not wait and time.monotonic() - stamp < POLL_SEC / 2:
                return cached
            dirs = self.state_dirs()
            sessions = self.read(self.common.SESSION_LOG, dirs) + self.codex_sessions()
            if wait:
                self._unrecorded = (time.monotonic(), self.unrecorded(sessions))
            else:
                counted = self._unrecorded[0]
                if self.base and not self._refreshing and \
                        (counted is None or time.monotonic() - counted >= UNRECORDED_SEC):
                    self._refreshing = True
                    threading.Thread(target=self._refresh_unrecorded, args=(sessions,), daemon=True).start()
            state = build_state(sessions, self.read("events.jsonl", dirs), dt.datetime.now(dt.timezone.utc),
                                self.tz, self.collect, self.pricing, self.usage.get, self._unrecorded[1])
            self._state = (time.monotonic(), state)
            return state


def worktree_name(admin: Path) -> str:
    """`.git/worktrees/<관리 이름>`의 작업 디렉터리 이름(`git worktree list`와 같은 이름). 읽지 못하면 관리 이름."""
    try:
        gitdir = (admin / "gitdir").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return admin.name
    return Path(gitdir).parent.name or admin.name


def host_allowed(header: str | None) -> bool:
    """Host 헤더가 localhost·127.0.0.1(포트는 무관)일 때만. 없으면 거절한다."""
    if not header:
        return False
    host = header.rsplit(":", 1)[0] if header.count(":") == 1 else header
    return host.strip().lower() in ALLOWED_HOSTS


def make_handler(source: Source):
    page = INDEX.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        server_version = "harness-dashboard"

        def log_message(self, *args):  # 요청마다 표준 오류에 쓰지 않는다
            pass

        def send_bytes(self, status, body: bytes, content_type: str):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; "
                             "script-src 'unsafe-inline'; connect-src 'self'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 - http.server 규약
            if not host_allowed(self.headers.get("Host")):
                self.send_bytes(HTTPStatus.FORBIDDEN, b"forbidden host", "text/plain; charset=utf-8")
                return
            if self.path == "/":
                self.send_bytes(HTTPStatus.OK, page, "text/html; charset=utf-8")
            elif self.path == "/api/state":
                try:
                    body = json.dumps(source.state(), ensure_ascii=False).encode("utf-8")
                except Exception as exc:  # noqa: BLE001 - 상태 계산 실패를 연결 끊김이 아니라 응답으로 알린다
                    self.send_bytes(HTTPStatus.INTERNAL_SERVER_ERROR, f"state error: {type(exc).__name__}".encode(),
                                    "text/plain; charset=utf-8")
                    return
                self.send_bytes(HTTPStatus.OK, body, "application/json; charset=utf-8")
            elif self.path == "/api/stream":
                self.stream()
            else:
                self.send_bytes(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")

        def stream(self):
            """Server-Sent Events. 상태가 바뀔 때만 보내고, 15초마다 주석 줄로 연결을 유지한다.
            상태 계산이 한 번 실패하면 그 틱만 건너뛴다(연결을 끊으면 브라우저가 다시 붙어 같은 실패를 반복한다)."""
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            last_digest, last_sent = None, 0.0
            try:
                while True:
                    try:
                        state = source.state()
                    except Exception:  # noqa: BLE001
                        state = None
                    digest = None if state is None else \
                        hashlib.sha256(json.dumps({**state, "now": None}).encode()).hexdigest()
                    if digest is not None and digest != last_digest:
                        self.wfile.write(f"data: {json.dumps(state, ensure_ascii=False)}\n\n".encode("utf-8"))
                        last_digest, last_sent = digest, time.monotonic()
                    elif time.monotonic() - last_sent > 15:
                        self.wfile.write(b": keep-alive\n\n")
                        last_sent = time.monotonic()
                    self.wfile.flush()
                    time.sleep(POLL_SEC)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                return

    return Handler


def make_server(source: Source, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((HOST, port), make_handler(source))
    server.daemon_threads = True
    return server
