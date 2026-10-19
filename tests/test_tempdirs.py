"""테스트 임시 디렉터리 정리(#93)."""

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tempdirs import remove_tree  # noqa: E402


class RemoveTreeTest(unittest.TestCase):
    def test_removes_read_only_files(self):
        """git 객체처럼 읽기 전용인 파일이 있어도 디렉터리가 남지 않는다."""
        tmp = Path(tempfile.mkdtemp(prefix="harness-tempdirs-"))
        obj = tmp / ".git" / "objects" / "ab" / "cdef"
        obj.parent.mkdir(parents=True)
        obj.write_bytes(b"x")
        os.chmod(obj, stat.S_IREAD)
        remove_tree(tmp)
        self.assertFalse(tmp.exists())

    def test_missing_path_is_ignored(self):
        remove_tree(Path(tempfile.gettempdir()) / "harness-tempdirs-missing-0f3c")

    def test_failure_warns_instead_of_hiding(self):
        tmp = Path(tempfile.mkdtemp(prefix="harness-tempdirs-"))
        self.addCleanup(remove_tree, tmp)
        with mock.patch("shutil.rmtree", side_effect=PermissionError("locked")):
            with self.assertWarns(ResourceWarning):
                remove_tree(tmp)


if __name__ == "__main__":
    unittest.main()
