"""What a check in a CI image runs on is what git tracks, as the working tree has it, and no
untracked file, cache or `.git`.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from scripts.check_in_image import archive, tracked


def _names(copy: bytes) -> set[str]:
    with tarfile.open(fileobj=io.BytesIO(copy)) as tar:
        return {member.name for member in tar.getmembers() if member.isfile()}


@pytest.mark.skipif(shutil.which("git") is None, reason="this image has no git")
def test_only_what_git_tracks_is_copied_as_the_working_tree_has_it(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)  # noqa: S603, S607 -- fixed arguments
    (tmp_path / "pkg").mkdir()
    for name in ("pkg/kept.py", "gone.py", "local.py"):
        (tmp_path / name).write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "pkg/kept.py", "gone.py"], check=True)  # noqa: S603, S607
    (tmp_path / "pkg" / "kept.py").write_text("x = 2\n", encoding="utf-8")
    (tmp_path / "gone.py").unlink()

    files = tracked(tmp_path)
    assert files == ["pkg/kept.py"]
    copy = archive(tmp_path, files)
    assert _names(copy) == {"work/pkg/kept.py"}
    with tarfile.open(fileobj=io.BytesIO(copy)) as tar:
        member = tar.extractfile("work/pkg/kept.py")
        assert member is not None
        assert member.read() == b"x = 2\n"


def test_where_git_cannot_say_all_but_git_s_own_directory_is_copied(tmp_path: Path) -> None:
    assert tracked(tmp_path) is None
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref\n", encoding="utf-8")
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "ci.yml").write_text("on: push\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
    assert _names(archive(tmp_path, None)) == {"work/.github/ci.yml", "work/main.py"}
