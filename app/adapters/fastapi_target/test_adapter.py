"""
FastAPI target test adapter.

pytest exit code reference (this is what `parse_test_output` decodes):
  0 - all tests passed
  1 - some tests failed (normal failure, not an environment problem)
  2 - test run interrupted (e.g. Ctrl-C) — treated as ERROR here, a
      sandbox run should never be interrupted externally
  3 - internal pytest error (e.g. a plugin crashed) — ERROR
  4 - usage error (bad CLI args/config) — ERROR, indicates a synthesis
      bug (malformed pytest.ini/pyproject config), not a code defect
  5 - no tests collected — ERROR, near-certainly means the migrated
      code has import errors preventing test discovery, which is a
      more fundamental problem than a normal test failure and should
      route the error-analyzer down a different path in Step 5
"""

from __future__ import annotations

import re

from app.adapters.target_test_base import TargetTestAdapter
from app.graph.state import TestOutcome

_FAILED_LINE_RE = re.compile(r"^FAILED (\S+)", re.MULTILINE)


class FastAPITargetAdapter(TargetTestAdapter):
    stack_key = "fastapi"
    display_name = "FastAPI"
    base_docker_image = "python:3.12-slim"

    def install_command(self) -> list[str]:
        return ["pip", "install", "--no-cache-dir", "-r", "requirements.txt"]

    def test_command(self) -> list[str]:
        return ["pytest", "-q", "--tb=short"]

    def parse_test_output(self, stdout: str, exit_code: int) -> tuple[TestOutcome, list[str]]:
        failing = _FAILED_LINE_RE.findall(stdout)

        if exit_code == 0:
            return TestOutcome.PASSED, []
        if exit_code == 5:
            return TestOutcome.ERROR, []  # no tests collected — see docstring
        if exit_code in (2, 3, 4):
            return TestOutcome.ERROR, failing
        return TestOutcome.FAILED, failing
