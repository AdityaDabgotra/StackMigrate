"""
Target-side adapter interface, mirroring `app.adapters.base.SourceAdapter`.

A `SourceAdapter` knows how to READ one stack's code (used by
Comprehension). A `TargetTestAdapter` knows how to INSTALL and TEST one
stack's code inside the sandbox (used by the Sandbox runner and, in
Step 4, by editor nodes that need to know target-stack conventions).
Kept as a separate interface rather than bolted onto `SourceAdapter`
because a stack can be a valid migration target long before anyone
writes a reader for it as a source (and vice versa) — see the
discussion in `app/adapters/base.py`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.graph.state import TestOutcome


class TargetTestAdapter(ABC):
    #: adapter registry key — must match MigrationRunConfig.target_stack
    stack_key: str

    #: human-readable, used in logs and PR descriptions
    display_name: str

    #: Docker image the sandbox runs this stack's containers from.
    #: Pin to a specific tag (not `latest`) for reproducible runs.
    base_docker_image: str

    @abstractmethod
    def install_command(self) -> list[str]:
        """Argv (not a shell string — avoids shell-injection surface entirely) to install dependencies."""

    @abstractmethod
    def test_command(self) -> list[str]:
        """Argv to run the test suite."""

    @abstractmethod
    def parse_test_output(self, stdout: str, exit_code: int) -> tuple[TestOutcome, list[str]]:
        """
        Interpret this stack's test runner output. Returns
        (outcome, failing_test_identifiers). Every stack's test runner
        has different exit-code conventions and failure-line formats,
        which is exactly why this is a per-adapter method rather than
        one generic regex in the sandbox runner.
        """
