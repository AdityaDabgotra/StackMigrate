"""
Applies `FileDiff` objects (produced by editor nodes — LLM output) onto
a target workspace directory on disk.

Security note: `FileDiff.path` originates from an LLM, which means it
must be treated as untrusted input. Without validation, a diff path
like `"../../../etc/cron.d/evil"` or an absolute path would let a
compromised or simply-confused editor write outside the intended
workspace. `_resolve_safe_path` is the one function in this whole
project that exists purely for that reason — every call site in this
module MUST go through it rather than `os.path.join` directly.
"""

from __future__ import annotations

import os

from app.graph.state import FileDiff


class UnsafeDiffPathError(ValueError):
    """Raised when a FileDiff's path would write outside the workspace root."""


def _resolve_safe_path(workspace_root: str, relative_path: str) -> str:
    if os.path.isabs(relative_path):
        raise UnsafeDiffPathError(f"Diff path must be relative, got absolute path: {relative_path!r}")

    workspace_root_real = os.path.realpath(workspace_root)
    candidate = os.path.realpath(os.path.join(workspace_root_real, relative_path))

    # realpath resolves ".." components, so this catches both
    # "../outside.txt" and symlink-based escapes in one check.
    if candidate != workspace_root_real and not candidate.startswith(workspace_root_real + os.sep):
        raise UnsafeDiffPathError(
            f"Diff path {relative_path!r} resolves outside the workspace root ({workspace_root})"
        )
    return candidate


def apply_diffs(workspace_root: str, diffs: list[FileDiff]) -> list[str]:
    """
    Writes each diff's content to workspace_root/<diff.path>, creating
    parent directories as needed. Returns the list of relative paths
    actually written, in order. Raises `UnsafeDiffPathError` (and
    writes NOTHING, including diffs before the offending one — see
    below) if any diff path is unsafe.

    All-or-nothing behavior: paths are validated in a first pass before
    any file is written, so a single bad diff in a batch can't leave
    the workspace partially modified with no clear record of what
    happened — the caller gets a clean exception and an untouched
    workspace, not a half-applied migration.
    """
    os.makedirs(workspace_root, exist_ok=True)

    resolved: list[tuple[str, FileDiff]] = [
        (_resolve_safe_path(workspace_root, diff.path), diff) for diff in diffs
    ]

    written: list[str] = []
    for full_path, diff in resolved:
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(diff.content)
        written.append(diff.path)

    return written
