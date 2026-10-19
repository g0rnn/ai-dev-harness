#!/usr/bin/env python3
"""PreToolUse hook: 보호 경로를 고치려는 파일 도구 호출을 거부(block)하거나 사용자 확인(ask)으로 돌린다.

규칙은 대상 저장소의 `harness.json` `hooks.protected_paths`(없으면 기본값)와 `areas[].docs`,
그리고 끌 수 없는 규칙(자기 보호, `harness.json`)이다. Bash 명령을 통한 쓰기는 보지 않는다
(README "알려진 우회"). 설정을 읽지 못하면 끌 수 없는 규칙만 적용하고 알린다(fail-open).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # 대상 저장소의 .claude/hooks/에 __pycache__를 남기지 않는다
import harness_common as common  # noqa: E402

# 도구별로 대상 파일 경로가 들어 있는 입력 키
TOOL_PATH_KEYS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path",
                  "NotebookEdit": "notebook_path"}

OFF_NOTICE = "보호 경로 검사가 끌 수 없는 규칙만 남기고 꺼진 상태다"


# 규칙 선택(`rules_for`·`decide`)은 MR lint도 같이 쓰므로 `harness_common`에 둔다(#100)
rules_for = common.rules_for
decide = common.decide


def respond(target: str, rule: dict, notice: str = "") -> None:
    label = f"{rule.get('reason', '보호 경로')}, 규칙 `{rule['pattern']}`"
    if rule["mode"] == "block":
        reason = (f"보호 경로({label}): {target}. AI가 직접 고치지 않는 파일이다. "
                  "변경이 필요하면 이유를 사용자에게 설명하고 사람이 직접 수정하게 한다.")
        decision = "deny"
    else:
        reason = f"보호 경로({label}): {target}. 사용자 확인 후 진행한다."
        decision = "ask"
    if notice:
        reason += f" (주의: {notice})"
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason,
    }}, ensure_ascii=False))


def main() -> int:
    common.setup_streams()
    try:
        payload = common.read_payload()
    except ValueError as exc:
        print(f"harness protect-paths: 입력을 읽지 못해 보호 경로 검사가 꺼진 상태다: {exc}", file=sys.stderr)
        return 1
    key = TOOL_PATH_KEYS.get(payload.get("tool_name"))
    tool_input = payload.get("tool_input")
    raw = tool_input.get(key) if key and isinstance(tool_input, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        return 0
    project = common.project_dir(payload)
    project_lexical = common.lexical(Path(common.project_root_raw(payload)))
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    kind, inside, outside, candidates = common.classify_path(project, raw, cwd, project_lexical)

    if kind == "unc":
        rule = {"pattern": "\\\\server\\share", "mode": "ask", "reason": "프로젝트 소속을 확인할 수 없는 네트워크 경로"}
        respond(raw, rule)
        log(project, payload, rule, raw)
        return 0

    # (규칙, 대상) 후보. 논리 경로와 실제 경로 중 가장 강한 결정을 택한다
    matches = [({"pattern": "~/.claude/settings*.json", "mode": "ask", "reason": "사용자 hooks 설정"}, str(p))
               for p in outside if common.is_user_settings(p)]
    real = common.self_protected_real(project, candidates)  # 설정과 무관하게 적용한다
    if real is not None:
        matches.append((real, raw))
    notice = ""
    if inside:
        try:
            rules = rules_for(common.load_config(project))
        except Exception as exc:  # noqa: BLE001 - 어떤 설정 오류에서도 끌 수 없는 규칙은 남긴다
            rules, notice = rules_for(None), f"{OFF_NOTICE}: {exc}"
        matches += [(rule, rel) for rel in inside if (rule := decide(rel, rules)) is not None]
    if not matches:
        if notice:
            print(f"harness protect-paths: {notice}", file=sys.stderr)
            return 1
        return 0
    rule, target = max(matches, key=lambda m: common.MODES.index(m[0]["mode"]))  # 동률이면 먼저 나온 후보
    respond(target, rule, notice)
    log(project, payload, rule, target)
    return 0


def log(project, payload, rule, target) -> None:
    common.log_event(project, {
        "hook": "protect-paths", "decision": rule["mode"], "rule": rule["pattern"],
        "path": target, "tool": payload.get("tool_name"), "session": payload.get("session_id"),
    })


if __name__ == "__main__":
    sys.exit(main())
