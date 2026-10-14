"""
Sandbox test-run node.

Runs the FULL test suite against the *effective* generated files: the
latest attempt of every SUCCEEDED task (`app/graph/diffs.py`), the same set
`aggregate` puts in the PR. Files an earlier attempt wrote that the effective
set no longer contains are deleted first, so the tested workspace matches what
ships and a retry can't leave a stale copy that quietly satisfies the tests.

`DockerSandboxRunner.run` is a blocking call (the Docker SDK is sync),
so it's offloaded to a thread executor to avoid blocking the event loop
other graph nodes may be running on.
"""

from __future__ import annotations

import asyncio

from app.adapters.target_registry import get_target_test_adapter
from app.graph.state import MigrationPhase, TestOutcome
from app.sandbox.interface import SandboxRunner
from app.graph.diffs import effective_diffs, stale_paths
from app.sandbox.workspace import UnsafeDiffPathError, apply_diffs, remove_paths


def build_sandbox_test_node(sandbox_runner: SandboxRunner):
    async def sandbox_test_node(state: dict) -> dict:
        config = state["config"]
        editor_results = state.get("editor_results", [])
        tasks = state.get("tasks", [])
        diffs = effective_diffs(editor_results, tasks)

        if not diffs:
            return {
                "phase": MigrationPhase.ERROR_ANALYSIS,
                "error_log": ["Sandbox skipped: no editor diffs were available to apply."],
            }

        try:
            remove_paths(config["target_workspace_path"], stale_paths(editor_results, tasks))
            apply_diffs(config["target_workspace_path"], diffs)
        except UnsafeDiffPathError as exc:
            # This is a hard stop, not a retryable test failure — an editor
            # producing an out-of-workspace path is a bug in synthesis/editor
            # logic, not something a test-retry loop should attempt to fix.
            return {
                "phase": MigrationPhase.FAILED,
                "error_log": [f"Refused to apply editor diffs — unsafe path: {exc}"],
            }

        adapter = get_target_test_adapter(config["target_stack"])
        loop = asyncio.get_running_loop()
        run_result = await loop.run_in_executor(
            None,
            lambda: sandbox_runner.run(workspace_root=config["target_workspace_path"], adapter=adapter),
        )

        next_phase = (
            MigrationPhase.AGGREGATING
            if run_result.test_result.outcome == TestOutcome.PASSED
            else MigrationPhase.ERROR_ANALYSIS
        )

        return {
            "test_results": [run_result.test_result],
            "full_suite_result": run_result.test_result,
            "phase": next_phase,
        }

    return sandbox_test_node
