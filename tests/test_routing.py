from __future__ import annotations

from langgraph.types import Send

from app.graph.routing import (
    NO_READY_TASKS,
    SANDBOX_NEEDS_ANALYSIS,
    SANDBOX_PASSED,
    get_ready_tasks,
    route_after_sandbox,
    route_to_editors,
)
from app.graph.state import BudgetState, MigrationTask, TargetFileSpec, TaskStatus, TestOutcome, TestResult


def _task(id_: str, status: TaskStatus = TaskStatus.PENDING, depends_on=None, **kw) -> MigrationTask:
    return MigrationTask(
        id=id_,
        source_unit_ids=["x"],
        target_files=[TargetFileSpec(path=f"{id_}.py", purpose="x")],
        synthesis_instructions="x",
        status=status,
        depends_on_task_ids=depends_on or [],
        **kw,
    )


def test_task_with_no_dependencies_is_ready():
    tasks = [_task("a")]
    assert get_ready_tasks(tasks) == tasks


def test_task_blocked_by_pending_dependency_is_not_ready():
    tasks = [_task("a", depends_on=["b"]), _task("b", status=TaskStatus.PENDING)]
    ready = get_ready_tasks(tasks)
    assert [t.id for t in ready] == ["b"]  # only the unblocked one


def test_task_unblocked_once_dependency_succeeded():
    tasks = [_task("a", depends_on=["b"]), _task("b", status=TaskStatus.SUCCEEDED)]
    ready = get_ready_tasks(tasks)
    assert [t.id for t in ready] == ["a"]


def test_already_succeeded_task_is_not_re_dispatched():
    tasks = [_task("a", status=TaskStatus.SUCCEEDED)]
    assert get_ready_tasks(tasks) == []


def test_task_blocked_by_skipped_dependency_stays_blocked():
    tasks = [_task("a", depends_on=["b"]), _task("b", status=TaskStatus.SKIPPED)]
    assert get_ready_tasks(tasks) == []


def test_retrying_task_with_satisfied_deps_is_ready():
    tasks = [_task("a", status=TaskStatus.RETRYING, retry_count=1)]
    ready = get_ready_tasks(tasks)
    assert [t.id for t in ready] == ["a"]


def test_route_to_editors_returns_sentinel_when_nothing_ready():
    state = {"tasks": [], "config": {}, "comprehension": None, "budget": BudgetState(max_usd=1.0)}
    assert route_to_editors(state) == NO_READY_TASKS


def test_route_to_editors_returns_send_per_ready_task_with_correct_payload():
    task_a = _task("a")
    task_b = _task("b")
    budget = BudgetState(max_usd=5.0)
    state = {"tasks": [task_a, task_b], "config": {"k": "v"}, "comprehension": "IR", "budget": budget}

    sends = route_to_editors(state)

    assert isinstance(sends, list)
    assert all(isinstance(s, Send) for s in sends)
    assert {s.node for s in sends} == {"editor"}
    dispatched_task_ids = {s.arg["current_task"].id for s in sends}
    assert dispatched_task_ids == {"a", "b"}
    # every branch gets the shared config/comprehension/budget context
    for s in sends:
        assert s.arg["config"] == {"k": "v"}
        assert s.arg["comprehension"] == "IR"
        assert s.arg["budget"] is budget


def test_route_after_sandbox_passed():
    state = {"full_suite_result": TestResult(outcome=TestOutcome.PASSED, raw_output="ok")}
    assert route_after_sandbox(state) == SANDBOX_PASSED


def test_route_after_sandbox_failed():
    state = {"full_suite_result": TestResult(outcome=TestOutcome.FAILED, raw_output="boom")}
    assert route_after_sandbox(state) == SANDBOX_NEEDS_ANALYSIS


def test_route_after_sandbox_error_outcome_also_routes_to_analysis():
    state = {"full_suite_result": TestResult(outcome=TestOutcome.ERROR, raw_output="import error")}
    assert route_after_sandbox(state) == SANDBOX_NEEDS_ANALYSIS


def test_route_after_sandbox_no_result_routes_to_analysis_not_treated_as_pass():
    assert route_after_sandbox({}) == SANDBOX_NEEDS_ANALYSIS
