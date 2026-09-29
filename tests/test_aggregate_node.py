from __future__ import annotations

import pytest

from app.graph.nodes.aggregate import build_aggregate_node
from app.graph.state import (
    ComprehensionResult,
    EditorResult,
    FileDiff,
    MigrationPhase,
    MigrationTask,
    TargetFileSpec,
    TaskStatus,
)


def _task(id_: str, path: str, status=TaskStatus.SUCCEEDED) -> MigrationTask:
    return MigrationTask(
        id=id_,
        source_unit_ids=["x"],
        target_files=[TargetFileSpec(path=path, purpose="order endpoints")],
        synthesis_instructions="x",
        status=status,
    )


def _base_config() -> dict:
    return {
        "run_id": "run-abc123",
        "source_repo_url": "x",
        "source_ref": "main",
        "source_stack": "springboot",
        "target_stack": "fastapi",
        "scope_description": "migrate the OrderController module only",
        "github_target_repo": None,
        "require_human_approval_before_pr": True,
        "local_checkout_path": "/x",
        "target_workspace_path": "/y",
    }


@pytest.mark.asyncio
async def test_aggregate_builds_pr_draft_from_effective_diffs():
    node = build_aggregate_node()
    state = {
        "config": _base_config(),
        "tasks": [_task("t1", "app/routers/order.py")],
        "editor_results": [EditorResult(task_id="t1", diffs=[FileDiff(path="app/routers/order.py", content="x")])],
        "comprehension": None,
    }

    update = await node(state)

    assert update["phase"] == MigrationPhase.AGGREGATING
    assert update["aggregated_diff_paths"] == ["app/routers/order.py"]
    draft = update["pr_draft"]
    assert "app/routers/order.py" in draft.body
    assert draft.branch_name == "stackmigrate/run-abc123"
    assert "springboot" in draft.title and "fastapi" in draft.title


@pytest.mark.asyncio
async def test_aggregate_includes_unresolved_ambiguities_in_pr_body():
    node = build_aggregate_node()
    comprehension = ComprehensionResult(
        source_stack="springboot",
        repo_root="/x",
        units=[],
        unresolved_ambiguities=["Could not resolve reference to 'LegacyHelper'"],
    )
    state = {
        "config": _base_config(),
        "tasks": [_task("t1", "app/routers/order.py")],
        "editor_results": [EditorResult(task_id="t1", diffs=[FileDiff(path="app/routers/order.py", content="x")])],
        "comprehension": comprehension,
    }

    update = await node(state)
    assert "LegacyHelper" in update["pr_draft"].body


@pytest.mark.asyncio
async def test_aggregate_fails_hard_on_unpublishable_path():
    node = build_aggregate_node()
    state = {
        "config": _base_config(),
        "tasks": [_task("t1", ".github/workflows/evil.yml")],
        "editor_results": [
            EditorResult(task_id="t1", diffs=[FileDiff(path=".github/workflows/evil.yml", content="x")])
        ],
        "comprehension": None,
    }

    update = await node(state)

    assert update["phase"] == MigrationPhase.FAILED
    assert "pr_draft" not in update
    assert any(".github" in e for e in update["error_log"])


@pytest.mark.asyncio
async def test_aggregate_fails_when_no_effective_diffs():
    node = build_aggregate_node()
    state = {"config": _base_config(), "tasks": [], "editor_results": [], "comprehension": None}

    update = await node(state)

    assert update["phase"] == MigrationPhase.FAILED
    assert "pr_draft" not in update


@pytest.mark.asyncio
async def test_aggregate_excludes_non_succeeded_tasks_from_pr_body():
    node = build_aggregate_node()
    state = {
        "config": _base_config(),
        "tasks": [
            _task("t1", "app/routers/order.py", status=TaskStatus.SUCCEEDED),
            _task("t2", "app/services/order_service.py", status=TaskStatus.NEEDS_HUMAN),
        ],
        "editor_results": [
            EditorResult(task_id="t1", diffs=[FileDiff(path="app/routers/order.py", content="x")]),
            EditorResult(task_id="t2", diffs=[FileDiff(path="app/services/order_service.py", content="y")]),
        ],
        "comprehension": None,
    }

    update = await node(state)
    assert update["aggregated_diff_paths"] == ["app/routers/order.py"]
    assert "order_service.py" not in update["pr_draft"].body
