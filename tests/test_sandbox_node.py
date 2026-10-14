from __future__ import annotations

import os
import tempfile

import pytest

from app.graph.nodes.sandbox_test import build_sandbox_test_node
from app.graph.state import (
    EditorResult,
    FileDiff,
    MigrationPhase,
    MigrationTask,
    TargetFileSpec,
    TaskStatus,
    TestOutcome,
    TestResult,
)
from app.sandbox.interface import SandboxRunResult


class FakeSandboxRunner:
    def __init__(self, canned_outcome: TestOutcome, failing_tests: list[str] | None = None):
        self.canned_outcome = canned_outcome
        self.failing_tests = failing_tests or []
        self.calls: list[dict] = []

    def run(self, *, workspace_root, adapter, task_id=None) -> SandboxRunResult:
        self.calls.append({"workspace_root": workspace_root, "adapter": adapter, "task_id": task_id})
        return SandboxRunResult(
            test_result=TestResult(
                task_id=task_id,
                outcome=self.canned_outcome,
                raw_output="fake output",
                failing_tests=self.failing_tests,
                duration_seconds=1.23,
            ),
            setup_output="fake install output",
            setup_succeeded=True,
        )


def _task(task_id: str, status: TaskStatus = TaskStatus.SUCCEEDED) -> MigrationTask:
    return MigrationTask(
        id=task_id,
        source_unit_ids=[task_id],
        target_files=[TargetFileSpec(path="a.py", purpose="x")],
        depends_on_task_ids=[],
        synthesis_instructions="x",
        status=status,
    )


def _base_config(workspace: str) -> dict:
    return {
        "run_id": "test-run",
        "source_repo_url": "x",
        "source_ref": "main",
        "source_stack": "springboot",
        "target_stack": "fastapi",
        "scope_description": "x",
        "github_target_repo": None,
        "require_human_approval_before_pr": True,
        "local_checkout_path": "/irrelevant",
        "target_workspace_path": workspace,
    }


@pytest.mark.asyncio
async def test_sandbox_node_applies_diffs_and_reports_pass():
    with tempfile.TemporaryDirectory() as workspace:
        runner = FakeSandboxRunner(canned_outcome=TestOutcome.PASSED)
        node = build_sandbox_test_node(runner)

        state = {
            "config": _base_config(workspace),
            "tasks": [_task("task::a")],
            "editor_results": [
                EditorResult(
                    task_id="task::a",
                    diffs=[FileDiff(path="app/routers/orders.py", content="# router")],
                )
            ],
        }

        update = await node(state)

        assert update["phase"] == MigrationPhase.AGGREGATING
        assert update["test_results"][0].outcome == TestOutcome.PASSED
        assert update["full_suite_result"].outcome == TestOutcome.PASSED
        assert os.path.exists(os.path.join(workspace, "app", "routers", "orders.py"))
        assert len(runner.calls) == 1


@pytest.mark.asyncio
async def test_sandbox_node_routes_to_error_analysis_on_failure():
    with tempfile.TemporaryDirectory() as workspace:
        runner = FakeSandboxRunner(canned_outcome=TestOutcome.FAILED, failing_tests=["tests/x.py::test_y"])
        node = build_sandbox_test_node(runner)

        state = {
            "config": _base_config(workspace),
            "tasks": [_task("task::a")],
            "editor_results": [
                EditorResult(task_id="task::a", diffs=[FileDiff(path="a.py", content="x")])
            ],
        }

        update = await node(state)
        assert update["phase"] == MigrationPhase.ERROR_ANALYSIS
        assert update["test_results"][0].failing_tests == ["tests/x.py::test_y"]


@pytest.mark.asyncio
async def test_sandbox_node_skips_run_when_no_diffs():
    with tempfile.TemporaryDirectory() as workspace:
        runner = FakeSandboxRunner(canned_outcome=TestOutcome.PASSED)
        node = build_sandbox_test_node(runner)

        state = {"config": _base_config(workspace), "editor_results": []}
        update = await node(state)

        assert update["phase"] == MigrationPhase.ERROR_ANALYSIS
        assert "test_results" not in update
        assert len(runner.calls) == 0  # never even tried to run the sandbox


@pytest.mark.asyncio
async def test_sandbox_node_refuses_unsafe_diff_path_without_calling_sandbox():
    with tempfile.TemporaryDirectory() as workspace:
        runner = FakeSandboxRunner(canned_outcome=TestOutcome.PASSED)
        node = build_sandbox_test_node(runner)

        state = {
            "config": _base_config(workspace),
            "tasks": [_task("task::a")],
            "editor_results": [
                EditorResult(task_id="task::a", diffs=[FileDiff(path="../../escape.py", content="pwned")])
            ],
        }

        update = await node(state)
        assert update["phase"] == MigrationPhase.FAILED
        assert any("unsafe" in e.lower() for e in update["error_log"])
        assert len(runner.calls) == 0  # sandbox never invoked for an unsafe batch


@pytest.mark.asyncio
async def test_sandbox_tests_the_effective_set_and_deletes_files_a_retry_stopped_emitting():
    """The retry rewrote the task WITHOUT its old helper file. That stale copy must not survive to be tested."""
    with tempfile.TemporaryDirectory() as workspace:
        runner = FakeSandboxRunner(canned_outcome=TestOutcome.PASSED)
        node = build_sandbox_test_node(runner)

        # a previous sandbox run left the first attempt's files on disk
        for rel in ("app/main.py", "app/helper.py"):
            os.makedirs(os.path.dirname(os.path.join(workspace, rel)), exist_ok=True)
            with open(os.path.join(workspace, rel), "w") as f:
                f.write("# attempt 1")

        state = {
            "config": _base_config(workspace),
            "tasks": [_task("task::a")],
            "editor_results": [
                EditorResult(
                    task_id="task::a",
                    diffs=[FileDiff(path="app/main.py", content="# 1"), FileDiff(path="app/helper.py", content="# 1")],
                ),
                EditorResult(task_id="task::a", diffs=[FileDiff(path="app/main.py", content="# attempt 2")]),
            ],
        }

        await node(state)

        assert not os.path.exists(os.path.join(workspace, "app", "helper.py"))  # stale, removed
        with open(os.path.join(workspace, "app", "main.py")) as f:
            assert f.read() == "# attempt 2"  # latest attempt wins


@pytest.mark.asyncio
async def test_sandbox_does_not_apply_code_from_tasks_that_did_not_succeed():
    with tempfile.TemporaryDirectory() as workspace:
        runner = FakeSandboxRunner(canned_outcome=TestOutcome.PASSED)
        node = build_sandbox_test_node(runner)

        state = {
            "config": _base_config(workspace),
            "tasks": [_task("task::ok"), _task("task::bad", TaskStatus.NEEDS_HUMAN)],
            "editor_results": [
                EditorResult(task_id="task::ok", diffs=[FileDiff(path="ok.py", content="x")]),
                EditorResult(task_id="task::bad", diffs=[FileDiff(path="bad.py", content="x")]),
            ],
        }

        await node(state)

        assert os.path.exists(os.path.join(workspace, "ok.py"))
        assert not os.path.exists(os.path.join(workspace, "bad.py"))
