#!/usr/bin/env python3
"""SessionEnd hook: 세션 종료를 세션 기록(`.git/harness/sessions.jsonl`)에 한 줄 남긴다(#96).

- 아무것도 출력하지 않고 무엇이 실패해도 종료 코드 0이다. 세션 종료를 막지 않는다.
- 기록에는 종료 사유(`reason`), 그때의 `HEAD`, transcript의 누적 토큰 수(#97)만 남긴다. 프롬프트·코드 내용은 쓰지 않는다.
- OTLP 엔드포인트가 설정돼 있으면 기록 뒤 저장소 안 telemetry push를 분리 실행한다. 전송을 기다리지 않는다.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # 대상 저장소의 .claude/hooks/에 __pycache__를 남기지 않는다
import harness_common as common  # noqa: E402


def push_in_background(project: Path) -> None:
    """기존 push의 잠금·전송 위치를 공유한다. 설정이 없는 PC와 갱신 전 저장소에서는 아무것도 하지 않는다."""
    if not (os.environ.get("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")):
        return
    script = project / ".harness" / "telemetry.py"
    if not script.is_file():
        return
    common.spawn_background([sys.executable, str(script), "push"], project)


def main() -> int:
    payload = {}
    try:
        payload = common.read_payload()
        reason = payload.get("reason")
        common.log_session_usage(common.project_dir(payload), "end", payload,
                                 reason=reason if isinstance(reason, str) and reason else None)
    except Exception:  # noqa: BLE001 - 입력·경로가 깨져도 종료를 막지 않는다
        pass
    try:
        # 기록 실패도 전송을 막지 않는다. 이전에 남은 기록은 다음 세션 종료 때 다시 보낸다.
        push_in_background(common.project_dir(payload))
    except Exception:  # noqa: BLE001 - 프로세스를 띄우지 못해도 세션 종료에 영향이 없다
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
