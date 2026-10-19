#!/usr/bin/env python3
"""SessionStart hook: 세션이 시작될 때의 `HEAD`를 종료 검증 기준선으로 남기고 세션 시작을 기록한다.

- 종료 검증(stop-verify)은 이 기준선 이후 커밋된 변경도 검증 대상에 넣는다. 세션이 커밋하고 끝나도 검증이 돈다.
- 같은 세션 ID가 이미 있으면 덮지 않는다(resume·compact가 기준선을 앞당기지 않게).
- 세션 기록(`sessions.jsonl`)에는 시작마다 한 줄을 남긴다(resume·compact 포함, `source`로 구분, #96).
- 아무것도 출력하지 않는다. SessionStart의 표준 출력은 모델 문맥에 들어간다.
- 무엇이 실패해도 종료 코드 0이다. 기준선이 없으면 종료 검증은 미커밋 변경만 본다.
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # 대상 저장소의 .claude/hooks/에 __pycache__를 남기지 않는다
import harness_common as common  # noqa: E402


def text(value):
    """문자열이면 그대로, 아니면 None(기록하지 않는다). `model`은 버전에 따라 객체일 수 있어 `id`·`display_name`을 쓴다."""
    if isinstance(value, dict):
        value = value.get("id") or value.get("display_name")
    return value if isinstance(value, str) and value else None


def main() -> int:
    try:
        payload = common.read_payload()
        session = payload.get("session_id")
        project = common.project_dir(payload)
        if isinstance(session, str) and session:
            common.record_baseline(project, session)
        common.log_session(project, "start", session, source=text(payload.get("source")),
                           model=text(payload.get("model")), transcript=text(payload.get("transcript_path")))
    except Exception:  # noqa: BLE001 - 입력·경로가 깨져도 세션을 막지 않는다
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
