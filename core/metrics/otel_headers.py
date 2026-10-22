#!/usr/bin/env python3
"""Claude Code `otelHeadersHelper`가 실행하는 인증 헤더 출력(#145). 표준 라이브러리만 쓰고 다른 키트 모듈을 읽지 않는다.

`harness telemetry setup`이 이 파일을 사용자 설정 디렉터리에 복사하고 Claude Code 사용자 설정에 등록한다. Claude Code
사용자 설정은 모든 프로젝트에 적용되므로 저장소를 지워도 동작하도록 저장소 밖에 둔다.

    python otel_headers.py <수집기 기본 주소>

사용자 자격 파일(`credentials.json`)에서 그 주소의 헤더를 찾아 JSON 객체 하나를 표준 출력에 쓴다. 찾지 못하거나 파일이
깨졌으면 아무것도 출력하지 않고 종료 1이다(Claude Code는 경고를 한 번 보이고 그 세션의 텔레메트리를 보내지 않는다).
헤더 값은 오류 메시지에 쓰지 않는다.

자격 파일 위치와 주소 키 규칙은 `.claude/hooks/harness_common.py`의 `config_dir`·`endpoint_key`와 같다(테스트가 대조한다).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
from pathlib import Path

CREDENTIALS_NAME = "credentials.json"


def config_dir(env=None, os_name: str | None = None, home: Path | None = None) -> Path:
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    if (os_name or os.name) == "nt":
        base = env.get("APPDATA")
        return (Path(base) if base else home / "AppData" / "Roaming") / "harness"
    base = env.get("XDG_CONFIG_HOME")
    return (Path(base) if base and Path(base).is_absolute() else home / ".config") / "harness"


def endpoint_key(url: str) -> str:
    parsed = urllib.parse.urlsplit(url.strip())
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", ""))


def headers_for(endpoint: str, path: Path) -> dict[str, str]:
    """자격 파일에서 주소의 헤더. 형식이 틀렸으면 ValueError(값은 메시지에 넣지 않는다)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    endpoints = data.get("endpoints") if isinstance(data, dict) else None
    if not isinstance(endpoints, dict):
        raise ValueError("endpoints 객체가 없다")
    wanted = endpoint_key(endpoint)
    for url, entry in endpoints.items():
        if isinstance(url, str) and endpoint_key(url) == wanted:
            headers = entry.get("headers") if isinstance(entry, dict) else None
            if not isinstance(headers, dict) or not headers or not all(
                    isinstance(name, str) and isinstance(value, str) and value and "\r" not in value and "\n" not in value
                    for name, value in headers.items()):
                raise ValueError("headers가 문자열 맵이 아니다")
            return headers
    raise LookupError("이 주소의 자격 정보가 없다")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows 콘솔·파이프에서도 한국어 메시지가 깨지지 않게
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("사용법: otel_headers.py <수집기 기본 주소>", file=sys.stderr)
        return 2
    path = config_dir() / CREDENTIALS_NAME
    try:
        headers = headers_for(args[0], path)
    except FileNotFoundError:
        print(f"harness otel_headers: 자격 파일이 없다: {path}. `harness telemetry setup`으로 만든다", file=sys.stderr)
        return 1
    except LookupError:
        print(f"harness otel_headers: {path}에 {endpoint_key(args[0])}의 자격 정보가 없다", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"harness otel_headers: {path}를 읽지 못했다({type(exc).__name__}). 값은 출력하지 않는다", file=sys.stderr)
        return 1
    sys.stdout.write(json.dumps(headers))
    return 0


if __name__ == "__main__":
    sys.exit(main())
