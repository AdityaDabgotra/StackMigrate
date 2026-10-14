from __future__ import annotations

import os
import tempfile

import pytest

from app.graph.state import FileDiff
from app.sandbox.workspace import UnsafeDiffPathError, apply_diffs


def test_apply_diffs_writes_files_and_creates_dirs():
    with tempfile.TemporaryDirectory() as tmp:
        diffs = [
            FileDiff(path="app/routers/orders.py", content="# orders router\n"),
            FileDiff(path="app/models.py", content="# models\n"),
        ]
        written = apply_diffs(tmp, diffs)

        assert set(written) == {"app/routers/orders.py", "app/models.py"}
        with open(os.path.join(tmp, "app", "routers", "orders.py")) as f:
            assert f.read() == "# orders router\n"


def test_apply_diffs_overwrites_existing_file():
    with tempfile.TemporaryDirectory() as tmp:
        apply_diffs(tmp, [FileDiff(path="x.py", content="old")])
        apply_diffs(tmp, [FileDiff(path="x.py", content="new")])
        with open(os.path.join(tmp, "x.py")) as f:
            assert f.read() == "new"


def test_apply_diffs_rejects_absolute_path():
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(UnsafeDiffPathError):
            apply_diffs(tmp, [FileDiff(path="/etc/passwd", content="pwned")])


def test_apply_diffs_rejects_parent_traversal():
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(UnsafeDiffPathError):
            apply_diffs(tmp, [FileDiff(path="../../etc/cron.d/evil", content="pwned")])


def test_apply_diffs_rejects_traversal_disguised_mid_path():
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(UnsafeDiffPathError):
            apply_diffs(tmp, [FileDiff(path="app/../../outside.py", content="pwned")])


def test_apply_diffs_is_all_or_nothing_on_bad_batch():
    """
    A batch with one good diff and one path-traversal diff must write
    NEITHER — otherwise a partially-applied migration could look
    successful for the files that did land.
    """
    with tempfile.TemporaryDirectory() as tmp:
        diffs = [
            FileDiff(path="good.py", content="fine"),
            FileDiff(path="../escape.py", content="pwned"),
        ]
        with pytest.raises(UnsafeDiffPathError):
            apply_diffs(tmp, diffs)

        assert not os.path.exists(os.path.join(tmp, "good.py"))


def test_remove_paths_deletes_files_ignores_missing_and_never_removes_directories():
    import os
    import tempfile

    from app.sandbox.workspace import remove_paths

    with tempfile.TemporaryDirectory() as root:
        os.makedirs(os.path.join(root, "pkg"))
        with open(os.path.join(root, "pkg", "old.py"), "w") as f:
            f.write("x")

        removed = remove_paths(root, ["pkg/old.py", "pkg/never_existed.py", "pkg"])

        assert removed == ["pkg/old.py"]
        assert os.path.isdir(os.path.join(root, "pkg"))


def test_remove_paths_is_all_or_nothing_on_an_unsafe_path():
    import os
    import tempfile

    import pytest

    from app.sandbox.workspace import UnsafeDiffPathError, remove_paths

    with tempfile.TemporaryDirectory() as root:
        keep = os.path.join(root, "keep.py")
        with open(keep, "w") as f:
            f.write("x")

        with pytest.raises(UnsafeDiffPathError):
            remove_paths(root, ["keep.py", "../../etc/passwd"])

        assert os.path.exists(keep)  # nothing was deleted before the bad path was caught
