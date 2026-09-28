from __future__ import annotations

import pytest

from app.core.llm import ErrorAnalysisLLMResult, ErrorAnalysisResponse
from app.graph.nodes.error_analysis import build_error_analysis_node
from app.graph.state import (
    BudgetState,
    EditorResult,
    ErrorAnalysis,
    FileDiff,
    MigrationPhase,
    MigrationTask,
    TargetFileSpec,
    TaskStatus,
    TestOutcome,
    TestResult,
)


def _task(id_: str, **overrides) -> MigrationTask:
    defaults = dict(
        id=id_,
        source_unit_ids=["x"],
        target_files=[TargetFileSpec(path=f"{id_}.py", purpose="x")],
        synthesis_instructions="x",
        status=TaskStatus.SUCCEEDED,
    )
    defaults.update(overrides)
    return MigrationTask(**defaults)


def _editor_result(task_id: str) -> EditorResult:
    return EditorResult(task_id=task_id, diffs=[FileDiff(path=f"{task_id}.py", content="broken code")])


def _test_result(outcome=TestOutcome.FAILED, failing=None) -> TestResult:
    return TestResult(outcome=outcome, raw_output="AssertionError: boom", failing_tests=failing or ["t1"])


class FakeAnalyzer:
    def __init__(self, analyses: list[ErrorAnalysis], fail: bool = False, unattributable: str = ""):
        self.analyses = analyses
        self.fail = fail
        self.unattributable = unattributable
        self.calls: list[str] = []

    async def analyze(self, prompt: str) -> ErrorAnalysisResponse:
        self.calls.append(prompt)
        if self.fail:
            raise RuntimeError("simulated analyzer failure")
        return ErrorAnalysisResponse(
            result=ErrorAnalysisLLMResult(analyses=self.analyses, unattributable_summary=self.unattributable),
            cost_usd=0.03,
            input_tokens=1000,
            output_tokens=200,
        )


def _base_state(**overrides) -> dict:
    task = _task("task::a")
    state = {
        "tasks": [task],
        "editor_results": [_editor_result("task::a")],
        "full_suite_result": _test_result(),
        "budget": BudgetState(max_usd=10.0),
    }
    state.update(overrides)
    return state


@pytest.mark.asyncio
async def test_retryable_failure_marks_task_retrying_and_increments_retry_count():
    analyzer = FakeAnalyzer(
        [ErrorAnalysis(task_id="task::a", root_cause_summary="off by one", is_retryable=True,
                        suggested_fix_instructions="fix it")]
    )
    node = build_error_analysis_node(analyzer)
    update = await node(_base_state())

    assert update["phase"] == MigrationPhase.EDITING
    assert update["tasks"][0].status == TaskStatus.RETRYING
    assert update["tasks"][0].retry_count == 1
    assert update["retry_round"] == 1
    assert update["budget"].spent_usd == pytest.approx(0.03)


@pytest.mark.asyncio
async def test_non_retryable_failure_escalates_to_needs_human():
    analyzer = FakeAnalyzer(
        [ErrorAnalysis(task_id="task::a", root_cause_summary="fundamental design mismatch", is_retryable=False)]
    )
    node = build_error_analysis_node(analyzer)
    update = await node(_base_state())

    assert update["phase"] == MigrationPhase.NEEDS_HUMAN
    assert update["tasks"][0].status == TaskStatus.NEEDS_HUMAN


@pytest.mark.asyncio
async def test_exceeding_per_task_retry_cap_escalates():
    analyzer = FakeAnalyzer(
        [ErrorAnalysis(task_id="task::a", root_cause_summary="still broken", is_retryable=True)]
    )
    node = build_error_analysis_node(analyzer)
    state = _base_state(tasks=[_task("task::a", retry_count=3, max_retries=3)])

    update = await node(state)

    assert update["tasks"][0].status == TaskStatus.NEEDS_HUMAN
    assert update["tasks"][0].retry_count == 4


@pytest.mark.asyncio
async def test_round_cap_forces_escalation_even_if_individually_retryable():
    analyzer = FakeAnalyzer(
        [ErrorAnalysis(task_id="task::a", root_cause_summary="fixable", is_retryable=True)]
    )
    node = build_error_analysis_node(analyzer, max_retry_rounds_default=2)
    state = _base_state(retry_round=1)  # this call will be round 2 == cap

    update = await node(state)

    assert update["phase"] == MigrationPhase.NEEDS_HUMAN
    assert update["tasks"][0].status == TaskStatus.NEEDS_HUMAN
    assert update["retry_round"] == 2


@pytest.mark.asyncio
async def test_unattributable_failure_leaves_tasks_untouched_and_escalates_phase():
    analyzer = FakeAnalyzer([], unattributable="could be a pre-existing test flake")
    node = build_error_analysis_node(analyzer)
    update = await node(_base_state())

    assert update["phase"] == MigrationPhase.NEEDS_HUMAN
    assert "tasks" not in update  # nothing touched — we didn't guess
    assert any("could be a pre-existing test flake" in e for e in update["error_log"])


@pytest.mark.asyncio
async def test_analyzer_exception_escalates_all_candidates_without_crashing():
    analyzer = FakeAnalyzer([], fail=True)
    node = build_error_analysis_node(analyzer)
    update = await node(_base_state())

    assert update["phase"] == MigrationPhase.NEEDS_HUMAN
    assert update["tasks"][0].status == TaskStatus.NEEDS_HUMAN


@pytest.mark.asyncio
async def test_no_candidate_tasks_escalates_immediately_without_calling_analyzer():
    analyzer = FakeAnalyzer([])
    node = build_error_analysis_node(analyzer)
    state = _base_state(tasks=[_task("task::a", status=TaskStatus.PENDING)])  # never ran, no code to blame

    update = await node(state)

    assert update["phase"] == MigrationPhase.NEEDS_HUMAN
    assert len(analyzer.calls) == 0


@pytest.mark.asyncio
async def test_budget_exhausted_escalates_without_calling_analyzer():
    analyzer = FakeAnalyzer([ErrorAnalysis(task_id="task::a", root_cause_summary="x", is_retryable=True)])
    node = build_error_analysis_node(analyzer)
    state = _base_state(budget=BudgetState(max_usd=1.0, spent_usd=1.0))

    update = await node(state)

    assert update["phase"] == MigrationPhase.NEEDS_HUMAN
    assert update["tasks"][0].status == TaskStatus.NEEDS_HUMAN
    assert len(analyzer.calls) == 0


@pytest.mark.asyncio
async def test_hallucinated_task_id_is_ignored_not_crashed_on():
    analyzer = FakeAnalyzer(
        [ErrorAnalysis(task_id="task::does_not_exist", root_cause_summary="x", is_retryable=True)]
    )
    node = build_error_analysis_node(analyzer)
    update = await node(_base_state())

    # the real task is untouched (still SUCCEEDED, no matching analysis) —
    # phase escalates since nothing ended up RETRYING
    assert update["phase"] == MigrationPhase.NEEDS_HUMAN
    assert "tasks" not in update
