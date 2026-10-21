#!/usr/bin/env python3
"""Additive, content-free AgentOps evidence. Standard library; no guard decisions.

Installed as .harness/agentops.py. Records live under the worktree Git directory,
never in tracked source. Costs/tokens belong to the existing telemetry pipeline.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
import hashlib
import html
import json
import math
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

VERSION = 1
MAX_LINE = 2_000_000
ID = re.compile(r"[\w./:#@+-]{1,160}\Z", re.ASCII)
SECRET = re.compile(r"(?i)(?:bearer\s+\S+|(?:sk-|ghp_|github_pat_|glpat-|AKIA)[A-Za-z0-9_-]{12,}|(?:token|password|secret|api[_-]?key)\s*[:=]\s*\S+)")
KINDS = {
    "binding": {"pr", "role"},
    "configuration": {"model", "effort", "agent_version", "harness_version", "rules_digest", "environment", "skill", "revision"},
    "prompt": {"length", "message_id"},
    "turn": {"status", "reason"},
    "tool": {"call_id", "name", "status", "duration_ms", "error", "retry_of", "exit_code"},
    "verification": {"check_id", "status", "duration_ms", "exit_code", "error", "cached", "state_digest", "run_id", "evidence"},
    "outcome": {"criterion", "status", "evidence", "run_id", "revision"},
    "intervention": {"action", "reason", "related_turn", "retry_of"},
    "wait": {"wait_id", "status", "reason", "duration_ms"},
    "context": {"action", "input_tokens", "limit_tokens", "truncated", "resource", "digest"},
    "review": {"finding_id", "status", "severity", "revision", "run_id", "defect_id", "evidence"},
    "heartbeat": {"status", "interval_sec"},
    "transport": {"status", "pending", "rejected", "duration_ms"},
}
ENUMS = {
    "source": {"observed", "human", "agent"},
    "role": {"implementation", "review", "helper"},
    "tool": {"codex", "claude-code", "human", "unknown"},
    "effort": {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra", "unknown"},
    "severity": {"critical", "high", "medium", "low", "info", "unknown"},
}
STATUS = {
    "turn": {"started", "completed", "cancelled", "failed", "unknown"},
    "tool": {"started", "success", "failed", "cancelled", "unknown"},
    "verification": {"pass", "fail", "timeout", "error", "not_run", "unsupported", "unknown"},
    "outcome": {"pass", "fail", "unverified"},
    "wait": {"started", "ended"},
    "review": {"accepted", "rejected", "followup", "pending", "removed", "fixed", "verified", "escaped"},
    "heartbeat": {"watching"},
    "transport": {"idle", "delivered", "partial", "error", "unavailable"},
}
ACTIONS = {
    "intervention": {"approve", "reject", "answer", "redirect", "direct_edit", "handoff", "retry", "cancel"},
    "context": {"compact", "resume", "clear", "load_skill", "load_resource", "usage"},
}
REASONS = {"unknown", "network", "rate_limit", "timeout", "environment", "verification", "requirements", "permission", "user_request", "code", "cancelled", "approval", "clarification", "unavailable"}
COMMON = {"id", "ts", "kind", "task", "session", "turn", "parent", "tool", "source", "branch", "head", "schema_version"}
NUMBERS = {"length", "duration_ms", "input_tokens", "limit_tokens", "interval_sec", "pending", "rejected"}


class EvidenceError(ValueError):
    pass


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")


def moment(value):
    if not isinstance(value, str):
        raise EvidenceError("timestamp must be an ISO timestamp with timezone")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed
    except ValueError:
        raise EvidenceError("timestamp must be an ISO timestamp with timezone") from None


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def git(root, *args, binary=False):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=20)
    if result.returncode:
        raise EvidenceError("Git operation failed: " + args[0])
    return result.stdout if binary else result.stdout.decode("utf-8", "replace").strip()


@contextmanager
def lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with Path(str(path) + ".lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"\0"); handle.flush()
        deadline = time.monotonic() + 5
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise EvidenceError("evidence store is busy") from None
                time.sleep(.02)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_json(path, default=None):
    if not path.exists():
        return {} if default is None else default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise EvidenceError("invalid local state: " + path.name) from None


def atomic_json(path, value):
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def interval_binding(bound, ts):
    now = moment(ts)
    return next((b for b in reversed([*bound.get("history", []), bound]) if now >= moment(b["since"]) and
                 (not b.get("until") or now < moment(b["until"]))), None)


def validate(event):
    kind = event.get("kind")
    if kind not in KINDS or set(event) - COMMON - KINDS[kind]:
        raise EvidenceError("unknown event kind or fields")
    if event.get("schema_version") != VERSION:
        raise EvidenceError("unsupported evidence schema")
    moment(event.get("ts"))
    for key, value in event.items():
        if value is None:
            continue
        if key in NUMBERS:
            if type(value) is not int or value < 0:
                raise EvidenceError("invalid nonnegative number: " + key)
        elif key in ("cached", "truncated"):
            if type(value) is not bool:
                raise EvidenceError("invalid boolean: " + key)
        elif key in ("schema_version", "exit_code"):
            if type(value) is not int:
                raise EvidenceError("invalid integer: " + key)
        elif key == "ts":
            continue
        elif not isinstance(value, str) or not ID.fullmatch(value) or SECRET.search(value):
            raise EvidenceError("invalid identifier: " + key)
        if key in ENUMS and value not in ENUMS[key]:
            raise EvidenceError("invalid enumeration: " + key)
        if key == "status" and value not in STATUS.get(kind, set()):
            raise EvidenceError("invalid status")
        if key == "action" and value not in ACTIONS.get(kind, set()):
            raise EvidenceError("invalid action")
        if key in ("reason", "error") and value not in REASONS:
            raise EvidenceError("invalid reason/error category")
    for key in ("id", "task", "session", "tool", "source"):
        if not event.get(key):
            raise EvidenceError("missing identifier: " + key)
    if kind in STATUS and "status" not in event:
        raise EvidenceError("missing status")
    if kind in ACTIONS and "action" not in event:
        raise EvidenceError("missing action")
    required = {"tool": "call_id", "outcome": "criterion", "verification": "check_id", "wait": "wait_id", "review": "finding_id"}
    if kind in required and not event.get(required[kind]):
        raise EvidenceError("missing identifier: " + required[kind])
    return event


class Store:
    def __init__(self, root):
        self.root = Path(git(root, "rev-parse", "--show-toplevel")).resolve()
        self.path = Path(git(self.root, "rev-parse", "--path-format=absolute", "--git-path", "harness/agentops"))
        self.path.mkdir(parents=True, exist_ok=True)
        self.events = self.path / "events.jsonl"
        self.binding = self.path / "bindings.json"

    def records(self):
        if not self.events.exists():
            return []
        result = []
        with self.events.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if len(line) > MAX_LINE:
                    raise EvidenceError("oversized evidence row")
                try:
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError()
                    result.append(validate(value))
                except (ValueError, TypeError):
                    raise EvidenceError(f"invalid evidence row {number}; report/export stopped") from None
        return result

    def append_many(self, rows):
        for row in rows:
            validate(row)
        with lock(self.events):
            known = {r["id"] for r in self.records()}
            added = [r for r in rows if r["id"] not in known]
            with self.events.open("a", encoding="utf-8", newline="\n") as handle:
                for row in added:
                    if row["id"] not in known:
                        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                        known.add(row["id"])
        return len(added)

    def context(self, session=None, tool=None):
        session = session or os.environ.get("CODEX_THREAD_ID") or "local"
        bound = read_json(self.binding).get(session, {})
        branch = git(self.root, "branch", "--show-current")
        match = re.search(r"[A-Z][A-Z0-9]+-\d+", branch)
        task = bound.get("task") or (match.group() if match else "unbound")
        return {"task": task, "session": session, "tool": tool or bound.get("tool", "unknown"),
                "branch": branch or "detached", "head": git(self.root, "rev-parse", "HEAD"),
                **({"parent": bound["parent"]} if bound.get("parent") else {})}

    def emit(self, kind, data, *, session=None, tool=None, source="observed", ts=None, event_id=None, context=None):
        row = {**(context or self.context(session, tool)), "kind": kind, "schema_version": VERSION,
               "id": event_id or uuid.uuid4().hex, "ts": ts or stamp(), "source": source, **data}
        row = validate(row)
        with lock(self.events):
            if event_id and any(item["id"] == event_id for item in self.records()):
                return row
            with self.events.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    def bind(self, task, session, tool, pr=None, role="implementation", parent=None, since=None, source="human"):
        # Validate before mutating bindings.
        context = {**self.context(session, tool), "task": task, "parent": parent}
        validate({**context, "kind": "binding", "schema_version": VERSION, "id": uuid.uuid4().hex,
                  "ts": stamp(), "source": source, "pr": pr, "role": role})
        with lock(self.binding):
            state = read_json(self.binding)
            previous = state.get(session, {})
            since = since or (previous.get("since") if previous.get("task") == task else None) or stamp()
            moment(since)
            history = list(previous.get("history", []))
            if previous and previous["task"] != task:
                if moment(since) < moment(previous["since"]):
                    raise EvidenceError("new binding cannot predate active binding")
                history.append({k: v for k, v in previous.items() if k != "history"} | {"until": since})
            elif previous and since != previous["since"]:
                raise EvidenceError("existing binding boundary is immutable; use a new session interval")
            state[session] = {"task": task, "tool": tool, "pr": pr, "role": role, "parent": parent, "since": since, "history": history}
            atomic_json(self.binding, state)
        return self.emit("binding", {"pr": pr, "role": role}, session=session, tool=tool, source=source, context=context)


def state_digest(root):
    """Fingerprint tracked changes AND untracked files, excluding ignored files."""
    h = hashlib.sha256()
    h.update(git(root, "rev-parse", "HEAD").encode())
    h.update(git(root, "diff", "HEAD", "--binary", binary=True))
    for name in git(root, "ls-files", "--others", "--exclude-standard", "-z", binary=True).split(b"\0"):
        if not name:
            continue
        path = Path(root) / os.fsdecode(name)
        h.update(len(name).to_bytes(8, "big")); h.update(name)
        if path.is_symlink():
            h.update(b"symlink\0" + digest(os.readlink(path)).encode())
            continue
        if path.stat().st_size > 200_000_000:
            raise EvidenceError("untracked file too large to fingerprint")
        file_hash = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                file_hash.update(chunk)
        h.update(b"file\0" + file_hash.digest())
    return h.hexdigest()


def verify(store, check_id, command, timeout=300, session=None, tool=None):
    if not command or not math.isfinite(timeout) or timeout <= 0:
        raise EvidenceError("verification needs an argv command and positive timeout")
    before = state_digest(store.root)
    started = time.monotonic()
    code, error = None, None
    try:
        # No shell interpolation; command output never enters the evidence store.
        proc = subprocess.Popen(command, cwd=store.root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=os.name != "nt",
                                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
        try:
            code = proc.wait(timeout=timeout)
            status = "pass" if code == 0 else "fail"
        except subprocess.TimeoutExpired:
            status, error = "timeout", "timeout"
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=20)
            else:
                import signal
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=20)
    except OSError:
        status, error = "error", "environment"
    after = state_digest(store.root)
    if before != after:
        status, error = "error", "environment"
    row = store.emit("verification", {"check_id": check_id, "status": status, "exit_code": code,
                     "duration_ms": int((time.monotonic() - started) * 1000), "error": error,
                     "cached": False, "state_digest": before, "run_id": uuid.uuid4().hex}, session=session, tool=tool)
    sync_outcomes(store)
    return (0 if status == "pass" else 1), row


def safe_id(value, fallback="unknown"):
    return value if isinstance(value, str) and ID.fullmatch(value) and not SECRET.search(value) else fallback


def classify_error(code):
    if code == 0:
        return None
    return "unknown"


def hook(store, payload, tool="claude-code"):
    """Only extract allowlisted metadata. Inputs/results/prompts never persisted here."""
    if not isinstance(payload, dict):
        return
    session = safe_id(payload.get("session_id"), "local")
    event = payload.get("hook_event_name", "unknown")
    turn = safe_id(payload.get("turn_id"), "unknown")
    base = {"turn": turn}
    if event == "SessionStart":
        context = store.context(session, tool)
        if context["task"] != "unbound" and session not in read_json(store.binding):
            store.bind(context["task"], session, tool, source="observed")
    if event == "UserPromptSubmit":
        prompt = payload.get("prompt")
        if isinstance(prompt, str):
            store.emit("prompt", {**base, "length": len(prompt)}, session=session, tool=tool)
    elif event in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
        status = {"PreToolUse": "started", "PostToolUse": "success", "PostToolUseFailure": "failed"}[event]
        response = payload.get("tool_response")
        if isinstance(response, dict) and response.get("is_error"):
            status = "failed"
        store.emit("tool", {**base, "call_id": safe_id(payload.get("tool_use_id"), "unknown"),
                            "name": safe_id(payload.get("tool_name")), "status": status,
                            "error": "unknown" if status == "failed" else None}, session=session, tool=tool)
        if event == "PreToolUse" and payload.get("tool_name") in ("Read", "Skill"):
            inputs = payload.get("tool_input") or {}
            if isinstance(inputs, dict):
                resource = inputs.get("file_path") or inputs.get("skill")
                if isinstance(resource, str):
                    store.emit("context", {**base, "action": "load_skill" if payload["tool_name"] == "Skill" else "load_resource",
                                           "resource": digest(resource)}, session=session, tool=tool)
    elif event in ("PreCompact", "SessionStart"):
        action = "compact" if event == "PreCompact" else payload.get("source")
        if action in ("compact", "resume", "clear"):
            store.emit("context", {**base, "action": action}, session=session, tool=tool)
        extra = {"model": safe_id(payload.get("model"))} if payload.get("model") else None
        configuration(store, session, tool, extra)
    elif event == "Stop":
        store.emit("turn", {**base, "status": "completed"}, session=session, tool=tool)


def configuration(store, session=None, tool=None, extra=None):
    rules = []
    for name in ("AGENTS.md", "CLAUDE.md", "harness.json", ".claude/settings.json", ".codex/config.toml"):
        path = store.root / name
        if path.is_file():
            rules.append(name + ":" + digest(path.read_bytes()))
    config = read_json(store.root / "harness.json")
    return store.emit("configuration", {"rules_digest": digest("\n".join(rules)),
                      "environment": safe_id(platform.system() + "-" + platform.machine() + "-py" + platform.python_version()),
                      "harness_version": safe_id(config.get("harness_version")),
                      "revision": git(store.root, "rev-parse", "HEAD"), **(extra or {})}, session=session, tool=tool)


def transcript(store, path, tool, session=None, capture_content=False):
    """Explicit local transcript import. Stable IDs make repeated imports idempotent.

    Unbound foreign transcripts are refused. Explicit session bindings permit a
    session opened in another repo. Historic events without binding are unbound;
    present-day branch/HEAD are never attributed to historic transcript rows.
    """
    path = Path(path)
    if path.stat().st_size > 200_000_000:
        raise EvidenceError("transcript too large")
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if len(line) > MAX_LINE:
                raise EvidenceError("oversized transcript row")
            try:
                rows.append(json.loads(line))
            except ValueError:
                if not line.endswith("\n"):
                    break  # A live writer may be in the middle of its last row.
                raise EvidenceError("invalid transcript row; import stopped") from None
    if any(not isinstance(r, dict) for r in rows):
        raise EvidenceError("transcript rows must be objects")
    meta = next((r.get("payload", {}) for r in rows if r.get("type") == "session_meta"), {})
    if not isinstance(meta, dict):
        raise EvidenceError("invalid transcript metadata")
    session = session or meta.get("id") or next((r.get("sessionId") for r in rows if r.get("sessionId")), None)
    if not session:
        raise EvidenceError("transcript session ID unavailable")
    session = safe_id(session)
    bound = read_json(store.binding).get(session)
    cwd = meta.get("cwd") or next((r.get("cwd") for r in rows if r.get("cwd")), None)
    if not bound:
        raise EvidenceError("transcript import needs an explicit session binding")
    context = {"session": session, "task": bound.get("task", "unbound") if bound else "unbound", "tool": tool}
    if bound and bound.get("parent"):
        context["parent"] = bound["parent"]
    emitted, current_turn, questions, resolved = [], "unknown", {}, set()
    def binding_at(ts):
        return interval_binding(bound, ts)
    def emit(index, kind, data, ts, context_ts=None):
        active = binding_at(context_ts or ts)
        if active is None:
            return
        assigned = {"session": session, "task": active["task"], "tool": tool}
        if active.get("parent"):
            assigned["parent"] = active["parent"]
        event_id = digest(f"{tool}:{session}:{index}:{kind}")
        emitted.append(validate({**assigned, "turn": current_turn, **data, "ts": ts, "kind": kind,
                                 "schema_version": VERSION, "source": "observed", "id": event_id}))
    for index, row in enumerate(rows):
        ts = row.get("timestamp")
        try:
            moment(ts)
        except EvidenceError:
            continue
        payload = row.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        kind = row.get("type")
        if kind == "turn_context":
            current_turn = safe_id(payload.get("turn_id"))
            emit(index, "configuration", {"model": safe_id(payload.get("model")),
                 "effort": payload.get("effort") if payload.get("effort") in ENUMS["effort"] else "unknown"}, ts)
        elif kind == "session_meta":
            emit(index, "configuration", {"agent_version": safe_id(payload.get("cli_version"))}, ts)
        elif kind == "event_msg":
            event = payload.get("type")
            if event in ("task_started", "turn_started", "task_complete", "turn_aborted"):
                current_turn = safe_id(payload.get("turn_id"), current_turn)
                emit(index, "turn", {"status": "started" if event in ("task_started", "turn_started") else "cancelled" if event == "turn_aborted" else "completed"}, ts)
            elif event == "user_message":
                text = payload.get("message", "")
                emit(index, "prompt", {"length": len(text) if isinstance(text, str) else 0}, ts)
                if capture_content and isinstance(text, str) and binding_at(ts):
                    save_content(store, session, index, text)
            elif event == "context_compacted":
                emit(index, "context", {"action": "compact"}, ts)
            elif event == "token_count":
                info = payload.get("info")
                info = info if isinstance(info, dict) else {}
                counts = info.get("last_token_usage")
                counts = counts if isinstance(counts, dict) else {}
                count, limit = counts.get("input_tokens"), info.get("model_context_window")
                data = {"action": "usage"}
                if type(count) is int and count >= 0:
                    data["input_tokens"] = count
                if type(limit) is int and limit >= 0:
                    data["limit_tokens"] = limit
                emit(index, "context", data, ts)
        elif kind == "response_item" and payload.get("type") in ("function_call", "custom_tool_call", "function_call_output", "custom_tool_call_output"):
            call_type = payload["type"]
            name = str(payload.get("name", "")).rsplit(".", 1)[-1]
            if call_type == "function_call" and name in ("request_user_input_async", "request_user_input"):
                try:
                    args = json.loads(payload.get("arguments", "{}"))
                    asked = args.get("questions") if isinstance(args, dict) else None
                    call_id = payload.get("call_id")
                    if isinstance(asked, list) and 0 < len(asked) <= 20 and isinstance(call_id, str):
                        questions[call_id] = {"ts": ts, "count": len(asked), "name": name, "turn": current_turn}
                        for question_no in range(len(asked)):
                            wait_id = digest(f"question:{session}:{call_id}:{question_no}")
                            emit(f"{index}.q{question_no}", "wait", {"wait_id": wait_id, "status": "started", "reason": "clarification"}, ts)
                except (ValueError, TypeError):
                    pass
            emit(index, "tool", {"call_id": safe_id(payload.get("call_id")), "name": safe_id(payload.get("name")),
                 "status": "unknown" if call_type.endswith("output") else "started"}, ts)
        elif kind == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
            # Only the native structured answer envelope, never arbitrary prose
            # or a quoted XML fragment, is treated as a response to a question.
            for block_no, block in enumerate(payload.get("content", []) if isinstance(payload.get("content"), list) else []):
                if not isinstance(block, dict) or not isinstance(block.get("text"), str):
                    continue
                match = re.fullmatch(r"\s*<send_user_message_question_reply>(.*?)</send_user_message_question_reply>\s*", block["text"], re.S)
                if not match:
                    continue
                try:
                    replies = json.loads(match.group(1))
                    if not isinstance(replies, list):
                        continue
                    for reply_no, reply in enumerate(replies):
                        if not isinstance(reply, dict) or not isinstance(reply.get("answer"), str) or not reply["answer"].strip():
                            continue
                        if not isinstance(reply.get("questionItemId"), str):
                            continue
                        try:
                            ref = json.loads(reply["questionItemId"])
                        except ValueError:
                            continue
                        if not isinstance(ref, list) or len(ref) != 3 or type(ref[2]) is not int:
                            continue
                        asked = questions.get(ref[1]) if isinstance(ref[1], str) else None
                        if not asked or ref[0] != asked["name"] or not 0 <= ref[2] < asked["count"]:
                            continue
                        wait_id = digest(f"question:{session}:{ref[1]}:{ref[2]}")
                        if wait_id in resolved:
                            continue
                        elapsed = int((moment(ts) - moment(asked["ts"])).total_seconds() * 1000)
                        if elapsed < 0:
                            continue
                        resolved.add(wait_id)
                        key = f"{index}.reply{block_no}.{reply_no}"
                        emit(key, "intervention", {"action": "answer", "reason": "clarification", "related_turn": asked["turn"]}, ts, asked["ts"])
                        emit(key, "wait", {"wait_id": wait_id, "status": "ended", "reason": "clarification", "duration_ms": elapsed}, ts, asked["ts"])
                except (ValueError, TypeError):
                    continue
        elif kind == "compacted":
            emit(index, "context", {"action": "compact"}, ts)
        elif kind in ("user", "assistant") and isinstance(row.get("message"), dict):
            msg = row["message"]
            if kind == "assistant" and msg.get("model"):
                emit(index, "configuration", {"model": safe_id(msg["model"])}, ts)
            content = msg.get("content", [])
            text = content if isinstance(content, str) else "".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
            if kind == "user" and text:
                emit(index, "prompt", {"length": len(text)}, ts)
                if capture_content and binding_at(ts):
                    save_content(store, session, index, text)
            for part_no, part in enumerate(content if isinstance(content, list) else []):
                if not isinstance(part, dict) or part.get("type") not in ("tool_use", "tool_result"):
                    continue
                output = part["type"] == "tool_result"
                # Use a distinct index for multiple tool blocks in a message.
                emit(f"{index}.{part_no}", "tool", {"call_id": safe_id(part.get("tool_use_id") or part.get("id")),
                     "name": safe_id(part.get("name")), "status": ("failed" if part.get("is_error") else "success") if output else "started",
                     "error": "unknown" if part.get("is_error") else None}, ts)
    return store.append_many(emitted)


def save_content(store, session, index, text):
    """Explicit opt-in only; always local, best-effort redaction, never exported."""
    private = store.path / "private"
    private.mkdir(exist_ok=True)
    try:
        private.chmod(0o700)
    except OSError:
        pass
    path = private / (digest(f"{session}:{index}") + ".json")
    with lock(path):
        atomic_json(path, {"session": session, "content": SECRET.sub("[REDACTED]", text), "redaction": "best_effort"})
    try:
        path.chmod(0o600)
    except OSError:
        pass


def sync_outcomes(store):
    """A check result is a check criterion, never a claim of whole-task completion."""
    events = []
    for row in store.records():
        if row["kind"] != "verification":
            continue
        check = "check:" + row["check_id"]
        if not ID.fullmatch(check):
            check = "check:" + digest(row["check_id"])
        status = "pass" if row["status"] == "pass" else "fail" if row["status"] in ("fail", "timeout") else "unverified"
        events.append(validate({k: v for k, v in row.items() if k in COMMON} | {
            "kind": "outcome", "id": digest("outcome:" + row["id"]), "criterion": check,
            "status": status, "run_id": row.get("run_id"), "evidence": "verification:" + row["id"]}))
    return store.append_many(events) if events else 0


def interaction(store, wait_id, action=None, reason="clarification", session=None, tool=None, source="human"):
    context = store.context(session, tool)
    key = digest(f"interaction:{context['session']}:{wait_id}")
    rows = store.records()
    starts = [r for r in rows if r["kind"] == "wait" and r["wait_id"] == key and r["status"] == "started" and r["session"] == context["session"]]
    if action is None:
        if starts:
            raise EvidenceError("wait ID already exists; use a new ID")
        return store.emit("wait", {"wait_id": key, "status": "started", "reason": reason}, context=context, source=source, event_id=digest(key + ":started"))
    if not starts:
        raise EvidenceError("matching wait start is unavailable")
    if any(r["kind"] == "wait" and r["wait_id"] == key and r["status"] == "ended" for r in rows):
        raise EvidenceError("wait already resolved")
    start = starts[-1]
    context = {k: v for k, v in start.items() if k in ("task", "session", "tool", "parent", "branch", "head")}
    now = stamp()
    elapsed = max(0, int((moment(now) - moment(start["ts"])).total_seconds() * 1000))
    events = [validate({**context, "schema_version": VERSION, "ts": now, "source": source,
                       "kind": "intervention", "id": digest(key + ":intervention"), "action": action, "reason": reason}),
              validate({**context, "schema_version": VERSION, "ts": now, "source": source,
                       "kind": "wait", "id": digest(key + ":ended"), "wait_id": key,
                       "status": "ended", "reason": reason, "duration_ms": elapsed})]
    store.append_many(events)
    return {"wait_id": key, "status": "ended", "duration_ms": elapsed}


def track_review(store, path, session=None):
    path = Path(path).resolve()
    context = store.context(session)
    task = context["task"]
    if task == "unbound" or path.suffix != ".md" or path.stem not in (task, task + "-review") or not path.is_file():
        raise EvidenceError("review must be a final review file named for the bound task")
    if git(path.parent, "rev-parse", "--path-format=absolute", "--git-common-dir") != git(store.root, "rev-parse", "--path-format=absolute", "--git-common-dir"):
        raise EvidenceError("review belongs to a different Git repository")
    context["branch"] = git(path.parent, "branch", "--show-current") or "detached"
    context["head"] = git(path.parent, "rev-parse", "HEAD")
    key = digest(str(path))
    target = store.path / "reviews.json"
    with lock(target):
        state = read_json(target)
        if not isinstance(state, dict):
            raise EvidenceError("invalid review registry")
        state[key] = {"path": str(path), "context": context}
        atomic_json(target, state)
    return {"review_id": key, "task": task}


def review_events(store, path, context):
    """Read only the final treatment table. No finding prose leaves this parser."""
    path = Path(path)
    if path.stat().st_size > MAX_LINE:
        raise EvidenceError("review too large")
    lines, fenced = [], False
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if re.match(r"^\s*(`{3,}|~{3,})", line):
            fenced = not fenced
            lines.append("")
        else:
            lines.append("" if fenced else line)
    section = next((i for i, line in enumerate(lines) if re.match(r"^##\s*7\.\s*처리", line)), None)
    if section is None:
        raise EvidenceError("final review treatment section unavailable")
    section_end = next((i for i in range(section + 1, len(lines)) if re.match(r"^##\s", lines[i])), len(lines))
    header_at = next((i for i in range(section + 1, section_end) if lines[i].startswith("|") and "처리" in lines[i]), None)
    if header_at is None or header_at + 1 >= len(lines) or not re.fullmatch(r"[\s:|\-]+", lines[header_at + 1]):
        raise EvidenceError("final review treatment table unavailable")
    cells = lambda line: [cell.strip() for cell in line.strip().strip("|").split("|")]
    header = cells(lines[header_at])
    if "#" not in header or "처리" not in header:
        raise EvidenceError("review identifiers unavailable")
    findings, seen = {}, set()
    for line in lines[header_at + 2:]:
        if not line.startswith("|"):
            break
        row = cells(line)
        if not any(row):
            continue
        if len(row) != len(header):
            raise EvidenceError("incomplete review row")
        values = dict(zip(header, row))
        if not values.get("#") and not values.get("처리"):
            continue
        finding = values["#"]
        state = next((v for label, v in {"채택": "accepted", "기각": "rejected", "후속": "followup", "판단 필요": "pending"}.items() if values["처리"].startswith(label)), None)
        if state is None or finding in seen or not ID.fullmatch(finding):
            raise EvidenceError("invalid or duplicate review finding")
        seen.add(finding)
        severity = next((v for label, v in {"높음": "high", "중간": "medium", "낮음": "low", "참고": "info"}.items() if values.get("심각도", "").startswith(label)), "unknown")
        data = {"finding_id": finding, "status": state, "severity": severity, "evidence": "review:" + digest(str(path))}
        for label, field in {"수정 커밋": "revision", "검증 ID": "run_id", "사후 결함": "defect_id"}.items():
            if values.get(label):
                data[field] = values[label]
        findings[finding] = data
    cursor_path = store.path / "review-cursors.json"
    review_key = digest(str(path) + ":" + context["task"])
    with lock(cursor_path):
        state = read_json(cursor_path)
        if not isinstance(state, dict):
            raise EvidenceError("invalid review cursor")
        previous = state.get(review_key, {})
        if not isinstance(previous, dict):
            raise EvidenceError("invalid review cursor entry")
        events, following = [], dict(previous)
        for finding in previous.keys() - findings.keys():
            findings[finding] = {"finding_id": finding, "status": "removed", "severity": "unknown", "evidence": "review:" + digest(str(path))}
        for finding, data in findings.items():
            signature = digest(json.dumps(data, sort_keys=True))
            old = previous.get(finding, {})
            if old.get("signature") == signature:
                continue
            generation = old.get("generation", 0) + 1
            identity = digest(f"{review_key}:{finding}:{generation}:{signature}")
            events.append(validate({**context, **data, "kind": "review", "schema_version": VERSION,
                                  "id": identity, "ts": dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc).isoformat(), "source": "observed"}))
            following[finding] = {"signature": signature, "generation": generation}
        added = store.append_many(events) if events else 0
        state[review_key] = following
        atomic_json(cursor_path, state)
        return added


def sync_workflow(store):
    result = {"outcomes": sync_outcomes(store), "reviews": 0, "errors": []}
    try:
        registry = read_json(store.path / "reviews.json")
        if not isinstance(registry, dict):
            raise EvidenceError("invalid review registry")
    except EvidenceError:
        result["errors"].append("review-registry")
        return result
    for key, record in registry.items():
        try:
            result["reviews"] += review_events(store, record["path"], record["context"])
        except (OSError, EvidenceError, KeyError, TypeError, AttributeError):
            result["errors"].append("review:" + key)
    return result


def report(store, task=None):
    rows = sorted((r for r in store.records() if task is None or r["task"] == task), key=lambda r: moment(r["ts"]))
    counts = {}
    durations, calls, waits, wait_intervals = [], {}, {}, {}
    wait_ms = 0
    for row in rows:
        counts[row["kind"]] = counts.get(row["kind"], 0) + 1
        key = (row["session"], row.get("call_id"))
        if row["kind"] == "tool":
            if row["status"] == "started":
                calls[key] = row
            elif key in calls:
                durations.append(max(0, int((moment(row["ts"]) - moment(calls.pop(key)["ts"])).total_seconds() * 1000)))
        if row["kind"] == "wait":
            key = (row["session"], row["wait_id"])
            if row["status"] == "started":
                waits[key] = row
            elif key in waits:
                start = moment(waits.pop(key)["ts"])
                end = moment(row["ts"])
                if end >= start:
                    wait_intervals.setdefault(row["session"], []).append((start, end))
    for intervals in wait_intervals.values():
        merged = []
        for start, end in sorted(intervals):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        wait_ms += sum(int((end - start).total_seconds() * 1000) for start, end in merged)
    criteria = {}
    for row in rows:
        if row["kind"] == "outcome":
            criteria[(row["task"], row["criterion"])] = row
    outcomes = list(criteria.values())
    verifications = [r for r in rows if r["kind"] == "verification"]
    runs = {r.get("run_id"): r for r in verifications if r.get("run_id")}
    backed = 0
    for row in outcomes:
        run = runs.get(row.get("run_id"))
        if row["status"] == "pass" and run and run["task"] == row["task"] and run["status"] == "pass" and run.get("state_digest") and run["source"] == "observed":
            backed += 1
    by_task = {}
    for row in verifications:
        by_task.setdefault(row["task"], []).append(row)
    elapsed = None if not rows else max(0, int((max(moment(r["ts"]) for r in rows) - min(moment(r["ts"]) for r in rows)).total_seconds() * 1000))
    return {"schema_version": VERSION, "task": task, "events": len(rows), "by_kind": counts,
            "sessions": len({(r["tool"], r["session"]) for r in rows}), "observed_span_ms": elapsed,
            "observed_tool_duration_ms": sum(durations) if durations else None,
            "reported_wait_ms": wait_ms if counts.get("wait") else None, "open_waits": len(waits),
            "interventions": [r for r in rows if r["kind"] == "intervention"],
            "outcomes": outcomes, "verified_criteria": backed,
            "verification": verifications,
            "first_verification_pass_rate": sum(v[0]["status"] == "pass" for v in by_task.values()) / len(by_task) if by_task else None,
            "reviews": [r for r in rows if r["kind"] == "review"],
            "coverage": {"automatic": ["prompt_length", "configuration", "tool_lifecycle", "context", "check_outcome", "structured_question_answer", "question_wait", "tracked_review_treatment"],
                         "explicit": ["acceptance_criterion", "intervention_reason", "unstructured_wait", "review_evidence_links", "verification_runner"],
                         "unknown": ["unreported_human_edits", "unobserved_waits", "unbound_historical_tasks"]},
            "transport": read_json(store.path / "transport.json")}


def otlp(store, rows):
    common = Path(git(store.root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    repo = common.parent.name if common.name == ".git" else store.root.name
    groups = {}
    for row in rows:
        validate(row)
        attrs = []
        for key, value in row.items():
            if value is None:
                continue
            encoded = {"boolValue": value} if type(value) is bool else {"intValue": str(value)} if type(value) is int else {"stringValue": value}
            attrs.append({"key": "session.id" if key == "session" else "harness.evidence." + key, "value": encoded})
        observed = time.time_ns()
        # Derived workflow facts can be discovered hours after the source event.
        # Query these by observation time so Loki's out-of-order window does not
        # discard backfills. Original occurrence remains harness.evidence.ts;
        # local reports and wait duration continue using the original timestamp.
        when = observed if row["kind"] in ("outcome", "intervention", "wait", "review") else int(moment(row["ts"]).timestamp() * 1_000_000_000)
        groups.setdefault(row["session"], []).append({"timeUnixNano": str(when),
            "observedTimeUnixNano": str(observed), "severityNumber": 9,
            "body": {"stringValue": "harness.evidence." + row["kind"]}, "attributes": attrs})
    return {"resourceLogs": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "harness-agentops"}},
              {"key": "harness.repo", "value": {"stringValue": repo}}, {"key": "session.id", "value": {"stringValue": session}}]},
              "scopeLogs": [{"scope": {"name": "harness.evidence", "version": str(VERSION)}, "logRecords": records}]}
              for session, records in groups.items()]}


def push(store, endpoint=None, dry_run=False, event_ids=None):
    base = endpoint or os.environ.get("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not base:
        raise EvidenceError("OTLP endpoint unavailable")
    url = base if base.rstrip("/").endswith("/v1/logs") else base.rstrip("/") + "/v1/logs"
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
        raise EvidenceError("invalid OTLP endpoint")
    if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise EvidenceError("remote OTLP endpoints require HTTPS")
    headers = {"Content-Type": "application/json"}
    for pair in (os.environ.get("OTEL_EXPORTER_OTLP_LOGS_HEADERS") or os.environ.get("OTEL_EXPORTER_OTLP_HEADERS") or "").split(","):
        if pair.strip():
            key, sep, val = pair.partition("=")
            if not sep:
                raise EvidenceError("invalid OTLP headers; values suppressed")
            headers[urllib.parse.unquote(key.strip())] = urllib.parse.unquote(val.strip())
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            # Never forward authentication or evidence to a redirected host.
            return None
    opener = urllib.request.build_opener(NoRedirect())
    path = store.path / "transport.json"
    with lock(path):
        state = read_json(path)
        rows = store.records()
        offset = state.get("offset", 0)
        if type(offset) is not int or not 0 <= offset <= len(rows):
            raise EvidenceError("invalid transport cursor")
        pending = [r for r in rows if r["id"] in set(event_ids)] if event_ids else rows[offset:]
        if event_ids and {r["id"] for r in pending} != set(event_ids):
            raise EvidenceError("replay event IDs must exist in the local store")
        if dry_run:
            return {"pending": len(pending), "payload": otlp(store, pending[:100])}
        state.update({"last_attempt": stamp(), "pending": len(pending)})
        state.setdefault("offset", offset)
        for index in range(0, len(pending), 100):
            batch = pending[index:index + 100]
            try:
                req = urllib.request.Request(url, data=json.dumps(otlp(store, batch)).encode(), headers=headers, method="POST")
                with opener.open(req, timeout=20) as response:
                    raw = response.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise ValueError()
                reply = json.loads(raw) if raw else {}
                rejected = (reply.get("partialSuccess") or reply.get("partial_success") or {}).get("rejectedLogRecords", 0)
                rejected = int(rejected)
                if not 0 <= rejected <= len(batch):
                    raise ValueError()
            except (OSError, ValueError, TypeError, AttributeError):
                state.update({"status": "error", "error": "transport", "pending": len(rows) - state.get("offset", 0)})
                atomic_json(path, state)
                return state
            if event_ids:
                state["replayed"] = state.get("replayed", 0) + len(batch)
            else:
                state["offset"] = offset + index + len(batch)
            state["rejected"] = state.get("rejected", 0) + rejected
            state["last_success"] = stamp()
            state["pending"] = len(rows) - state["offset"]
            state["status"] = "partial" if state["rejected"] else "delivered"
            state.pop("error", None)
            atomic_json(path, state)
        if not pending:
            state.setdefault("status", "idle")
            atomic_json(path, state)
        return state


def install_hooks(store, python="python"):
    if python not in ("python", "python3", "py -3", "py"):
        raise EvidenceError("unsupported Python launcher")
    path = store.root / ".claude/settings.json"
    with lock(store.path / "install-settings"):
        settings = read_json(path)
        hooks = settings.setdefault("hooks", {})
        for name in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "PreCompact", "Stop"):
            entries = hooks.setdefault(name, [])
            if any("/.harness/agentops.py" in h.get("command", "") for e in entries for h in e.get("hooks", [])):
                continue
            entry = {"hooks": [{"type": "command", "command": python + ' "$CLAUDE_PROJECT_DIR/.harness/agentops.py" hook --tool claude-code', "timeout": 10}]}
            if name in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
                entry["matcher"] = ".*"
            entries.append(entry)
        atomic_json(path, settings)


def sync(store):
    """Discover only bound Codex sessions; avoid importing arbitrary local chats."""
    bindings = read_json(store.binding)
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "sessions"
    state = {"ts": stamp(), "imported": 0, "missing_sessions": [], "errors": []}
    for session, binding in bindings.items():
        if binding["tool"] != "codex":
            continue
        matches = list(home.rglob("*" + session + "*.jsonl")) if home.is_dir() else []
        if len(matches) != 1:
            state["missing_sessions"].append(session)
            continue
        try:
            state["imported"] += transcript(store, matches[0], "codex", session)
        except (OSError, EvidenceError, TypeError, AttributeError):
            state["errors"].append(session)
    # Existing Claude stop verification is observed, but has no command-level
    # fingerprint. Do not pretend it is equivalent to the explicit verify runner.
    sessions = store.path.parent / "sessions.jsonl"
    if sessions.is_file():
        items = []
        with sessions.open(encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                try:
                    row = json.loads(line)
                    session = row.get("session")
                    bound = bindings.get(session)
                    status = row.get("verify")
                    if not bound or not isinstance(status, str):
                        continue
                    active = interval_binding(bound, row["ts"])
                    if active is None:
                        continue
                    mapped = {"pass": "pass", "fail": "fail", "cached-pass": "pass", "cached-fail": "fail", "none": "not_run", "error": "error"}.get(status, "unknown")
                    items.append(validate({"schema_version": VERSION, "id": digest(f"claude-verify:{session}:{index}"),
                        "ts": row["ts"], "kind": "verification", "task": active["task"], "session": session,
                        "tool": "claude-code", "source": "observed", "check_id": "stop-verify", "status": mapped,
                        "cached": status.startswith("cached-")}))
                except (ValueError, TypeError, KeyError, AttributeError):
                    state["errors"].append("claude-verification-row")
        state["imported"] += store.append_many(items)
    workflow = sync_workflow(store)
    state["workflow"] = workflow
    state["errors"].extend(workflow["errors"])
    atomic_json(store.path / "sync.json", state)
    return state


def watch(store, interval=30, iterations=None):
    if not 5 <= interval <= 3600:
        raise EvidenceError("interval must be 5..3600 seconds")
    path = store.path / "watch.json"
    # Lifetime lock prevents duplicate watchers in this worktree.
    with lock(path):
        number = 0
        try:
            while iterations is None or number < iterations:
                status = {"pid": os.getpid(), "ts": stamp(), "interval_sec": interval, "status": "watching"}
                atomic_json(path, status)
                sync(store)
                store.emit("heartbeat", {"status": "watching", "interval_sec": interval})
                started = time.monotonic()
                try:
                    transport = push(store)
                except EvidenceError:
                    transport = {"status": "unavailable"}
                    status["transport"] = "unavailable"
                    atomic_json(path, status)
                store.emit("transport", {"status": transport.get("status", "unknown"),
                           "pending": transport.get("pending", 0), "rejected": transport.get("rejected", 0),
                           "duration_ms": int((time.monotonic() - started) * 1000)})
                number += 1
                if iterations is None or number < iterations:
                    time.sleep(interval)
        finally:
            atomic_json(path, {"pid": os.getpid(), "ts": stamp(), "status": "stopped"})


def main(argv=None):
    sys.dont_write_bytecode = True
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default=".")
    sub = parser.add_subparsers(dest="command", required=True)
    bind = sub.add_parser("bind")
    bind.add_argument("--task", required=True); bind.add_argument("--session", default=os.environ.get("CODEX_THREAD_ID", "local"))
    bind.add_argument("--tool", choices=sorted(ENUMS["tool"]), default="unknown")
    bind.add_argument("--pr"); bind.add_argument("--parent"); bind.add_argument("--role", choices=sorted(ENUMS["role"]), default="implementation")
    bind.add_argument("--since", help="explicit historical binding boundary (ISO timestamp)")
    record = sub.add_parser("record")
    record.add_argument("--kind", choices=sorted(KINDS), required=True)
    record.add_argument("--data", required=True, help="JSON object of identifiers and enumerations, never content")
    record.add_argument("--source", choices=["human", "agent"], default="agent")
    record.add_argument("--session"); record.add_argument("--tool", choices=sorted(ENUMS["tool"]))
    verify_cmd = sub.add_parser("verify")
    verify_cmd.add_argument("--check-id", required=True); verify_cmd.add_argument("--timeout", type=float, default=300)
    verify_cmd.add_argument("--session"); verify_cmd.add_argument("--tool", choices=sorted(ENUMS["tool"]))
    verify_cmd.add_argument("argv", nargs=argparse.REMAINDER)
    ingest = sub.add_parser("import")
    ingest.add_argument("--transcript", required=True); ingest.add_argument("--tool", choices=["codex", "claude-code"], required=True)
    ingest.add_argument("--session"); ingest.add_argument("--capture-local-content", action="store_true")
    hook_cmd = sub.add_parser("hook"); hook_cmd.add_argument("--tool", default="claude-code", choices=["codex", "claude-code"])
    sub.add_parser("install-hooks").add_argument("--python", default="python")
    report_cmd = sub.add_parser("report"); report_cmd.add_argument("--task"); report_cmd.add_argument("--html", help="local HTML output")
    sub.add_parser("status")
    sub.add_parser("sync")
    tracked = sub.add_parser("track-review")
    tracked.add_argument("--file", required=True); tracked.add_argument("--session")
    discussion = sub.add_parser("interaction")
    discussion.add_argument("phase", choices=["begin", "resolve"])
    discussion.add_argument("--wait-id", required=True)
    discussion.add_argument("--action", choices=sorted(ACTIONS["intervention"]))
    discussion.add_argument("--reason", choices=sorted(REASONS), default="clarification")
    discussion.add_argument("--source", choices=["human", "agent"], default="agent")
    discussion.add_argument("--session"); discussion.add_argument("--tool", choices=sorted(ENUMS["tool"]))
    export = sub.add_parser("push"); export.add_argument("--endpoint"); export.add_argument("--dry-run", action="store_true")
    export.add_argument("--event-id", action="append", help="replay only confirmed missing event IDs; normal cursor is preserved")
    watcher = sub.add_parser("watch"); watcher.add_argument("--interval", type=int, default=30); watcher.add_argument("--iterations", type=int)
    args = parser.parse_args(argv)
    try:
        store = Store(args.target)
        if args.command == "bind":
            result = store.bind(args.task, args.session, args.tool, args.pr, args.role, args.parent, args.since)
            configuration(store, args.session, args.tool)
        elif args.command == "record":
            data = json.loads(args.data)
            if not isinstance(data, dict) or set(data) - KINDS[args.kind] - {"turn"}:
                raise EvidenceError("data must contain only kind-specific fields")
            result = store.emit(args.kind, data, session=args.session, tool=args.tool, source=args.source)
        elif args.command == "verify":
            command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
            code, result = verify(store, args.check_id, command, args.timeout, args.session, args.tool)
            print(json.dumps(result, ensure_ascii=False)); return code
        elif args.command == "import":
            result = {"processed": transcript(store, args.transcript, args.tool, args.session, args.capture_local_content)}
        elif args.command == "hook":
            raw = sys.stdin.read(MAX_LINE + 1)
            if len(raw) <= MAX_LINE:
                hook(store, json.loads(raw), args.tool)
            return 0
        elif args.command == "install-hooks":
            install_hooks(store, args.python); result = {"installed": True}
        elif args.command == "sync":
            result = sync(store)
        elif args.command == "track-review":
            result = track_review(store, args.file, args.session)
        elif args.command == "interaction":
            if (args.phase == "resolve") != (args.action is not None):
                raise EvidenceError("resolve requires action; begin does not accept action")
            result = interaction(store, args.wait_id, args.action, args.reason, args.session, args.tool, args.source)
        elif args.command == "report":
            result = report(store, args.task)
            if args.html:
                Path(args.html).write_text('<!doctype html><meta charset="utf-8"><title>AgentOps evidence</title><h1>AgentOps evidence</h1><pre>' + html.escape(json.dumps(result, ensure_ascii=False, indent=2)) + '</pre>', encoding="utf-8")
        elif args.command == "push":
            result = push(store, args.endpoint, args.dry_run, args.event_id)
            print(json.dumps(result, ensure_ascii=False)); return 1 if result.get("status") in ("error", "partial") else 0
        elif args.command == "watch":
            watch(store, args.interval, args.iterations); return 0
        else:
            watcher = read_json(store.path / "watch.json")
            if watcher.get("status") == "watching":
                watcher["fresh"] = (dt.datetime.now(dt.timezone.utc) - moment(watcher["ts"])).total_seconds() <= watcher.get("interval_sec", 30) * 3
            result = {"events": len(store.records()), "transport": read_json(store.path / "transport.json"), "watcher": watcher,
                      "sync": read_json(store.path / "sync.json"),
                      "capabilities": report(store)["coverage"]}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (EvidenceError, OSError, ValueError, subprocess.SubprocessError):
        if args.command == "hook":
            return 0
        print("agentops: operation failed; invalid input, unavailable state, or transport. Sensitive details suppressed.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
