from __future__ import annotations

import tempfile

import pytest

from app.graph.nodes.budget_abort import build_budget_abort_node
from app.graph.routing import BUDGET_EXHAUSTED, NO_READY_TASKS, route_to_editors
from app.graph.state import (
    BudgetState,
    MigrationPhase,
    MigrationTask,
    TargetFileSpec,
    TaskStatus,
    TestOutcome,
)
from app.graph.build import build_graph
from tests.test_comprehension_node import FakeExtractor, _write_spring_repo
from tests.test_graph_integration import FakeCodeGenerator, FakeSandboxRunner, _base_config


def _task(tid: str, status=TaskStatus.PENDING, deps=()) -> MigrationTask:
    return MigrationTask(
        id=tid,
        source_unit_ids=[tid],
        target_files=[TargetFileSpec(path=f"app/{tid}.py", purpose="x")],
        depends_on_task_ids=list(deps),
        synthesis_instructions="x",
        status=status,
    )


# ---- BudgetState ----------------------------------------------------------

def test_reason_for_each_ceiling():
    assert BudgetState(max_usd=1.0).exhausted_reason() is None
    assert "cost ceiling" in BudgetState(max_usd=1.0, spent_usd=1.0).exhausted_reason()
    assert "call cap" in BudgetState(max_usd=9.0, max_llm_calls=3, llm_calls_made=3).exhausted_reason()
    b = BudgetState(max_usd=9.0, max_wall_clock_seconds=60, started_at=1000.0)
    assert b.exhausted_reason(now=1059.0) is None
    assert "wall-clock" in b.exhausted_reason(now=1060.0)


def test_record_call_counts_calls_and_tracks_largest_single_call():
    b = BudgetState(max_usd=10.0)
    b.record_call(0.10)
    b.record_call(0.30)
    b.record_call(0.50, calls=5, track_max=False)  # an aggregate of 5 calls must not skew the estimate
    assert b.llm_calls_made == 7
    assert b.max_call_cost_usd == pytest.approx(0.30)


def test_affordable_calls_caps_parallel_wave_but_always_allows_progress():
    b = BudgetState(max_usd=1.0, spent_usd=0.60, max_call_cost_usd=0.10)  # 0.40 left, 0.10 per call
    assert b.affordable_calls(10) == 4
    assert b.affordable_calls(2) == 2
    tight = BudgetState(max_usd=1.0, spent_usd=0.95, max_call_cost_usd=0.10)  # less than one call left
    assert tight.affordable_calls(3) == 1  # still one, so the run can finish
    assert BudgetState(max_usd=1.0, spent_usd=1.0).affordable_calls(3) == 0


# ---- routing gate ---------------------------------------------------------

def _state(tasks, budget):
    return {"tasks": tasks, "budget": budget, "config": {}, "comprehension": None}


def test_gate_aborts_when_work_remains_but_budget_is_gone():
    state = _state([_task("a")], BudgetState(max_usd=1.0, spent_usd=1.0))
    assert route_to_editors(state) == BUDGET_EXHAUSTED


def test_gate_lets_a_finished_run_proceed_even_if_budget_is_exactly_spent():
    state = _state([_task("a", TaskStatus.SUCCEEDED)], BudgetState(max_usd=1.0, spent_usd=1.0))
    assert route_to_editors(state) == NO_READY_TASKS


def test_gate_caps_wave_size_by_remaining_budget():
    budget = BudgetState(max_usd=1.0, spent_usd=0.70, max_call_cost_usd=0.10)  # room for 3
    sends = route_to_editors(_state([_task(f"t{i}") for i in range(6)], budget))
    assert len(sends) == 3


# ---- abort node -----------------------------------------------------------

@pytest.mark.asyncio
async def test_abort_node_skips_unfinished_tasks_and_records_reason():
    node = build_budget_abort_node()
    tasks = [_task("done", TaskStatus.SUCCEEDED), _task("todo"), _task("retry", TaskStatus.RETRYING)]
    update = await node({"tasks": tasks, "budget": BudgetState(max_usd=1.0, spent_usd=2.0)})

    assert update["phase"] == MigrationPhase.ABORTED_BUDGET
    assert {t.id: t.status for t in update["tasks"]} == {"todo": TaskStatus.SKIPPED, "retry": TaskStatus.SKIPPED}
    assert "cost ceiling" in update["error_log"][0] and "2 task(s)" in update["error_log"][0]


# ---- real compiled graph --------------------------------------------------

@pytest.mark.asyncio
async def test_run_aborts_mid_migration_when_budget_runs_out_and_never_reaches_sandbox_or_pr():
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
        _write_spring_repo(src)
        sandbox = FakeSandboxRunner(outcome=TestOutcome.PASSED)
        generator = FakeCodeGenerator()
        graph = build_graph(extractor=FakeExtractor(), code_generator=generator, sandbox_runner=sandbox)

        # extraction costs 0.03 total, each editor call 0.02: enough for exactly one task.
        final = await graph.ainvoke({"config": _base_config(src, dst), "budget": BudgetState(max_usd=0.05)})

        statuses = [t.status for t in final["tasks"]]
        assert final["phase"] == MigrationPhase.ABORTED_BUDGET
        assert statuses.count(TaskStatus.SUCCEEDED) == 1
        assert statuses.count(TaskStatus.SKIPPED) == 2
        assert len(generator.calls) == 1
        assert sandbox.calls == []  # a partial migration is never tested or published
        assert final.get("pr_draft") is None
        assert any("Run aborted" in e for e in final["error_log"])


@pytest.mark.asyncio
async def test_budget_exhausted_before_comprehension_aborts_instead_of_falling_through():
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
        _write_spring_repo(src)
        extractor = FakeExtractor()
        sandbox = FakeSandboxRunner()
        graph = build_graph(extractor=extractor, code_generator=FakeCodeGenerator(), sandbox_runner=sandbox)

        final = await graph.ainvoke(
            {"config": _base_config(src, dst), "budget": BudgetState(max_usd=10.0, max_wall_clock_seconds=0)}
        )

        assert final["phase"] == MigrationPhase.ABORTED_BUDGET
        assert "wall-clock" in " ".join(final["error_log"])
        assert sandbox.calls == []
