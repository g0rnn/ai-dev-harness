"""테스트 임시 디렉터리 정리. 테스트 모듈이 아니다(`test*.py` 패턴 밖).

`shutil.rmtree(..., ignore_errors=True)`는 Windows에서 git 객체 파일(읽기 전용)을 지우지 못하고 실패를 숨겨
`%TEMP%`에 임시 저장소가 쌓였다(#93). 리허설 스크립트의 `remove_tree`와 같이 쓰기 권한을 주고 다시 지운다.
"""

from __future__ import annotations

import os
import shutil
import stat
import warnings
from pathlib import Path


def remove_tree(path: str | Path) -> None:
    """읽기 전용 파일까지 지운다. 없으면 넘어가고, 그래도 지우지 못하면 테스트를 실패시키지 않고 경고를 남긴다."""
    def writable_remove(func, name, _exc):
        os.chmod(name, stat.S_IWRITE)
        func(name)

    try:
        shutil.rmtree(path, onexc=writable_remove)
    except FileNotFoundError:
        pass
    except OSError as exc:
        warnings.warn(f"임시 디렉터리를 지우지 못했다: {path}: {exc}", ResourceWarning, stacklevel=2)
