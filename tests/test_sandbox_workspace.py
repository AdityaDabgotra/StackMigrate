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
