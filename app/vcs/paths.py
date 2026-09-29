"""
Publishability check for paths that will be sent to a git host.

Stricter than the workspace traversal guard (app/sandbox/workspace.py):
that one asks "does this stay inside the workspace?"; this one asks "is it
acceptable to put this in a pull request opened with OUR credentials?".
Notably it refuses `.github/` — a generated CI workflow in a PR is a
privilege-escalation vector (workflows can run with repository secrets),
and no migration of application code has a legitimate reason to touch it.
"""

from __future__ import annotations

import re

_SAFE_CHARS = re.compile(r"^[\w./-]+$")
_FORBIDDEN_SEGMENTS = {".git", ".github"}
_FORBIDDEN_FILES = {".gitmodules", ".gitattributes"}


def publishable_path_problem(path: str) -> str | None:
    """Returns a human-readable reason the path is refused, or None if it's fine."""
    if not path or path.startswith("/") or "\\" in path:
        return "empty, absolute, or contains a backslash"
    if not _SAFE_CHARS.match(path):
        return "contains characters outside [A-Za-z0-9_./-]"
    segments = path.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        return "contains an empty, '.', or '..' path segment"
    lowered = [seg.lower() for seg in segments]
    if any(seg in _FORBIDDEN_SEGMENTS for seg in lowered):
        return "targets .git or .github"
    if lowered[-1] in _FORBIDDEN_FILES:
        return "targets a protected git metadata file"
    return None
