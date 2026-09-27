"""
Sandbox runner interface. Exists so the Sandbox node (and, in Step 5,
the retry loop) depends on this Protocol, not on `DockerSandboxRunner`
directly — production wiring uses the real Docker-backed implementation,
tests use a fake. Same pattern as `Extractor` in Step 2.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.adapters.target_test_base import TargetTestAdapter
from app.graph.state import TestResult


@dataclass
class SandboxRunResult:
    test_result: TestResult
    setup_output: str
    setup_succeeded: bool


class SandboxRunner(Protocol):
    def run(
        self, *, workspace_root: str, adapter: TargetTestAdapter, task_id: str | None = None
    ) -> SandboxRunResult: ...
