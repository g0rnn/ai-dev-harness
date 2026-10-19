#!/usr/bin/env python3
"""SessionEnd hook: 세션 종료를 세션 기록(`.git/harness/sessions.jsonl`)에 한 줄 남긴다(#96).

- 아무것도 출력하지 않고 무엇이 실패해도 종료 코드 0이다. 세션 종료를 막지 않는다.
- 기록에는 종료 사유(`reason`)와 그때의 `HEAD`만 남긴다. 프롬프트·코드 내용은 쓰지 않는다.
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # 대상 저장소의 .claude/hooks/에 __pycache__를 남기지 않는다
import harness_common as common  # noqa: E402


def main() -> int:
    try:
        payload = common.read_payload()
        reason = payload.get("reason")
        common.log_session(common.project_dir(payload), "end", payload.get("session_id"),
                           reason=reason if isinstance(reason, str) and reason else None)
    except Exception:  # noqa: BLE001 - 입력·경로가 깨져도 종료를 막지 않는다
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
