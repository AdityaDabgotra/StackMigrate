"""
Routes ready MigrationTasks to parallel editor branches via LangGraph's
`Send()` primitive.

Single-wave scope note: this dispatches every task that's ready RIGHT
NOW (no unmet dependencies) in one parallel batch. A task whose
dependency is still PENDING simply isn't dispatched this wave — there
is no re-checking loop that fires a second wave once that dependency
finishes. Multi-wave scheduling (dispatch wave 1, wait, dispatch wave 2
once wave 1's dependencies are satisfied, ...) is explicitly Step 5's
job, folded into the retry/error-analysis loop that already needs to
re-enter the editor fanout for failed tasks — building a second,
separate wave mechanism here would just be replaced by Step 5's anyway.
For a typical migration task graph (endpoint -> service -> repository,
2-3 levels deep), this means a fresh `stackmigrate` run today correctly
migrates every task with NO dependencies in one shot; deeper chains
need Step 5 wired in before they complete. This is stated plainly
rather than silently left as a surprise.
"""

from __future__ import annotations

from langgraph.types import Send

from app.graph.state import MigrationTask, TaskStatus, TestOutcome

NO_READY_TASKS = "no_ready_tasks"
SANDBOX_PASSED = "sandbox_passed"
SANDBOX_NEEDS_ANALYSIS = "sandbox_needs_analysis"


def get_ready_tasks(tasks: list[MigrationTask]) -> list[MigrationTask]:
    """
    A task is ready when it's PENDING or RETRYING and every task it
    depends on has already SUCCEEDED. SKIPPED/NEEDS_HUMAN dependencies
    permanently block a task from ever becoming ready — that's
    intentional (see Step 5's error-analysis node for how a task
    escalates to NEEDS_HUMAN, and note that a task blocked this way
    stays blocked until a human resolves it, not indefinitely retried).
    """
    status_by_id = {t.id: t.status for t in tasks}
    ready = []
    for task in tasks:
        if task.status not in (TaskStatus.PENDING, TaskStatus.RETRYING):
            continue
        if all(status_by_id.get(dep_id) == TaskStatus.SUCCEEDED for dep_id in task.depends_on_task_ids):
            ready.append(task)
    return ready


def route_to_editors(state: dict) -> list[Send] | str:
    tasks = state.get("tasks", [])
    ready = get_ready_tasks(tasks)

    if not ready:
        return NO_READY_TASKS

    return [
        Send(
            "editor",
            {
                "config": state["config"],
                "comprehension": state["comprehension"],
                "budget": state["budget"],
                "current_task": task,
            },
        )
        for task in ready
    ]


def route_after_sandbox(state: dict) -> str:
    """
    Routes on `full_suite_result.outcome`, not on `phase` — the sandbox
    node's phase field is informational, this is the actual control
    signal. `None` (sandbox never ran, e.g. zero diffs were available)
    routes to analysis rather than treating it as a pass, since "no
    result" is never the same as "passed."
    """
    result = state.get("full_suite_result")
    if result is not None and result.outcome == TestOutcome.PASSED:
        return SANDBOX_PASSED
    return SANDBOX_NEEDS_ANALYSIS
