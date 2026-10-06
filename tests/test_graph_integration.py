"""
Runs the ACTUAL compiled LangGraph StateGraph (real `Send()` fanout,
real reducers, real conditional edges) end-to-end — comprehension
through synthesis through the parallel editor fanout through budget
reconciliation through the sandbox test run. Every LLM/Docker
collaborator is a fake; the graph orchestration itself is real.

This is the test that would have caught a mistake in the Send() payload
shape, a reducer misconfiguration, or the budget-delta double-counting
bug that the design docs in graph_state.py warn about — none of the
unit tests for individual nodes could catch a wiring bug at the graph
level, only running the graph can.

As of Step 5, this file also proves two things no unit test can:
  1. Multi-wave dependency scheduling actually completes a 3-level
     chain (repository -> service -> router) in one graph run — the
     limitation Step 4 explicitly documented as unresolved.
  2. The retry loop actually recovers from a failed sandbox run AND
     the round cap actually terminates a run that never stops failing
     — i.e. the loop is provably not infinite, not just "should be
     fine on paper."
"""

from __future__ import annotations

import os
import re
import tempfile

import pytest

from app.core.llm import (
    EditorGenerationResult,
    EditorResponse,
    ErrorAnalysisLLMResult,
    ErrorAnalysisResponse,
)
from app.graph.build import build_graph
from app.graph.state import (
    BudgetState,
    ErrorAnalysis,
    FileDiff,
    MigrationPhase,
    TaskStatus,
    TestOutcome,
    TestResult,
)
from app.sandbox.interface import SandboxRunResult
from tests.test_comprehension_node import FakeExtractor, _write_spring_repo
from tests.test_pr_publish_node import FakePublisher


class FakeCodeGenerator:
    """Extracts target file paths straight out of the editor prompt's own
    "- path: purpose" lines and generates trivial content for each — works
    for any task without needing to know its specifics in advance."""

    def __init__(self):
        self.calls: list[str] = []

    async def generate(self, prompt: str) -> EditorResponse:
        self.calls.append(prompt)
        # Only scan the "Target file(s) to produce:" block, not the whole
        # prompt — `synthesis_instructions` embeds IR unit ids containing a
        # literal "::" (e.g. "repository::order_repository"), which a
        # whole-prompt regex can misparse as a bogus extra path.
        section = prompt.split("Target file(s) to produce:\n", 1)[1]
        section = section.split("\nInstructions:", 1)[0]
        paths = re.findall(r"^- (\S+):", section, re.MULTILINE)
        files = [FileDiff(path=p, content=f"# generated for {p}\n") for p in paths]

        return EditorResponse(
            result=EditorGenerationResult(files=files, notes="fake"),
            cost_usd=0.02,
            input_tokens=600,
            output_tokens=300,
        )

class FakeSandboxRunner:
    """
    `outcomes`, when given, is consumed one entry per call (the last
    entry repeats if there are more calls than entries) — lets a test
    simulate "fails the first time, passes after the retry" without
    needing real Docker/pytest underneath.
    """

    def __init__(self, outcome: TestOutcome = TestOutcome.PASSED, outcomes: list[TestOutcome] | None = None):
        self.outcome = outcome
        self.outcomes = outcomes
        self.calls: list[dict] = []

    def run(self, *, workspace_root, adapter, task_id=None) -> SandboxRunResult:
        self.calls.append({"workspace_root": workspace_root, "task_id": task_id})
        if self.outcomes:
            idx = min(len(self.calls) - 1, len(self.outcomes) - 1)
            outcome = self.outcomes[idx]
        else:
            outcome = self.outcome
        failing = [] if outcome == TestOutcome.PASSED else ["tests/test_orders.py::test_something"]
        return SandboxRunResult(
            test_result=TestResult(
                task_id=task_id, outcome=outcome, raw_output="fake output", failing_tests=failing, duration_seconds=0.5
            ),
            setup_output="fake install",
            setup_succeeded=True,
        )


class FakeErrorAnalyzer:
    """Blames every candidate task the prompt mentions (extracted from the
    node's own "Task '<id>' (attempt N..." formatting) — good enough to
    drive the retry loop in tests without depending on real LLM reasoning."""

    def __init__(self, retryable: bool = True):
        self.retryable = retryable
        self.calls: list[str] = []

    async def analyze(self, prompt: str) -> ErrorAnalysisResponse:
        self.calls.append(prompt)
        task_ids = re.findall(r"Task '([^']+)'", prompt)
        analyses = [
            ErrorAnalysis(
                task_id=tid,
                root_cause_summary="fake root cause",
                is_retryable=self.retryable,
                suggested_fix_instructions="fake fix instructions",
            )
            for tid in task_ids
        ]
        return ErrorAnalysisResponse(
            result=ErrorAnalysisLLMResult(analyses=analyses), cost_usd=0.03, input_tokens=500, output_tokens=100
        )


def _base_config(source_root: str, target_root: str) -> dict:
    return {
        "run_id": "integration-test-run",
        "source_repo_url": "https://example.com/repo.git",
        "source_ref": "main",
        "source_stack": "springboot",
        "target_stack": "fastapi",
        "scope_description": "migrate the OrderController module only",
        "github_target_repo": None,
        "require_human_approval_before_pr": True,
        "local_checkout_path": source_root,
        "target_workspace_path": target_root,
    }


@pytest.mark.asyncio
async def test_full_pipeline_comprehension_through_sandbox():
    with tempfile.TemporaryDirectory() as source_root, tempfile.TemporaryDirectory() as target_root:
        _write_spring_repo(source_root)

        extractor = FakeExtractor()
        code_generator = FakeCodeGenerator()
        sandbox_runner = FakeSandboxRunner(outcome=TestOutcome.PASSED)

        graph = build_graph(extractor=extractor, code_generator=code_generator, sandbox_runner=sandbox_runner)

        initial_state = {
            "config": _base_config(source_root, target_root),
            "budget": BudgetState(max_usd=10.0),
        }

        final_state = await graph.ainvoke(initial_state)

        # --- comprehension actually ran ---
        assert final_state["comprehension"] is not None
        assert len(final_state["comprehension"].units) == 3  # endpoint, service, repository

        # --- synthesis produced tasks with real dependency ordering ---
        tasks = final_state["tasks"]
        assert len(tasks) == 3  # router (1 endpoint here), service, repository

        # --- multi-wave dependency scheduling ran to full completion ---
        # (this is the Step 4 limitation Step 5's budget_reconcile loop
        # resolves: repository -> service -> router, 3 sequential waves,
        # all within this single graph.ainvoke() call)
        assert all(t.status == TaskStatus.SUCCEEDED for t in tasks)
        assert len(code_generator.calls) == 3  # exactly one editor call per task, no wasted re-dispatch

        # --- budget was correctly reconciled from the fanout's deltas ---
        # 3 comprehension extraction calls (0.01 each) + 3 editor calls (0.02 each)
        assert final_state["budget"].spent_usd == pytest.approx(0.03 + 3 * 0.02)

        # --- sandbox ran once, against ALL THREE migrated files on disk ---
        assert len(sandbox_runner.calls) == 1
        assert final_state["full_suite_result"].outcome == TestOutcome.PASSED
        assert final_state["phase"] == MigrationPhase.DONE
        assert final_state["pr_draft"] is not None

        for task in tasks:
            path = task.target_files[0].path
            assert os.path.exists(os.path.join(target_root, path)), f"{path} was never written to disk"
            with open(os.path.join(target_root, path)) as f:
                assert "generated for" in f.read()


@pytest.mark.asyncio
async def test_pipeline_routes_to_sandbox_directly_when_synthesis_yields_no_tasks():
    """
    An empty/failed comprehension produces zero tasks; the graph must
    still terminate cleanly via the NO_READY_TASKS sentinel edge rather
    than erroring out on an empty Send() dispatch. With Step 5's wiring,
    "no diffs" flows onward from sandbox_test into error_analysis (since
    there's no full_suite_result to call a pass), which correctly
    recognizes it has nothing to analyze and escalates to NEEDS_HUMAN
    rather than dead-ending at sandbox_test's own ERROR_ANALYSIS phase
    the way Step 4's shorter graph did.
    """
    with tempfile.TemporaryDirectory() as source_root, tempfile.TemporaryDirectory() as target_root:
        # deliberately do NOT write any Spring files -> discovery finds nothing
        extractor = FakeExtractor()
        code_generator = FakeCodeGenerator()
        sandbox_runner = FakeSandboxRunner()

        graph = build_graph(extractor=extractor, code_generator=code_generator, sandbox_runner=sandbox_runner)
        initial_state = {
            "config": _base_config(source_root, target_root),
            "budget": BudgetState(max_usd=10.0),
        }

        final_state = await graph.ainvoke(initial_state)

        assert len(code_generator.calls) == 0  # editor never invoked
        assert final_state["phase"] == MigrationPhase.NEEDS_HUMAN
        assert any("could not run" in e.lower() for e in final_state["error_log"])


@pytest.mark.asyncio
async def test_retry_loop_recovers_after_error_analysis_marks_tasks_retryable():
    """
    Sandbox fails once, error_analysis blames every task that had code
    in the run (all 3, in this fixture) and marks them retryable. That
    flips all 3 back to non-SUCCEEDED simultaneously, which — correctly
    — means NONE of them are immediately ready again (each one's
    dependency is ALSO no longer SUCCEEDED). The multi-wave loop has to
    re-establish the dependency order from scratch: repository, then
    service, then router, exactly like the first attempt. Only then
    does sandbox_test run a second time, and this time it passes.
    """
    with tempfile.TemporaryDirectory() as source_root, tempfile.TemporaryDirectory() as target_root:
        _write_spring_repo(source_root)

        extractor = FakeExtractor()
        code_generator = FakeCodeGenerator()
        sandbox_runner = FakeSandboxRunner(outcomes=[TestOutcome.FAILED, TestOutcome.PASSED])
        error_analyzer = FakeErrorAnalyzer(retryable=True)

        graph = build_graph(
            extractor=extractor,
            code_generator=code_generator,
            sandbox_runner=sandbox_runner,
            error_analyzer=error_analyzer,
        )
        initial_state = {
            "config": _base_config(source_root, target_root),
            "budget": BudgetState(max_usd=10.0),
        }

        final_state = await graph.ainvoke(initial_state)

        assert len(sandbox_runner.calls) == 2  # failed once, recovered on retry
        assert len(error_analyzer.calls) == 1
        assert final_state["retry_round"] == 1
        assert final_state["full_suite_result"].outcome == TestOutcome.PASSED
        assert all(t.status == TaskStatus.SUCCEEDED for t in final_state["tasks"])
        assert all(t.retry_count == 1 for t in final_state["tasks"])  # every task actually got re-generated once

        # 3 tasks generated twice each (initial attempt + full retry re-migration)
        assert len(code_generator.calls) == 6

        # budget reflects everything: 3 extraction + 6 editor + 1 error-analysis call
        expected_spent = 0.03 + 6 * 0.02 + 0.03
        assert final_state["budget"].spent_usd == pytest.approx(expected_spent)


@pytest.mark.asyncio
async def test_round_cap_terminates_a_perpetually_failing_run_instead_of_looping_forever():
    """
    The sandbox NEVER passes and the analyzer ALWAYS says "retry me" —
    the worst case for the retry loop. `max_retry_rounds=2` (set low so
    the test runs fast) must force termination at exactly round 2,
    proving the loop is bounded rather than merely "expected to be" bounded.
    """
    with tempfile.TemporaryDirectory() as source_root, tempfile.TemporaryDirectory() as target_root:
        _write_spring_repo(source_root)

        extractor = FakeExtractor()
        code_generator = FakeCodeGenerator()
        sandbox_runner = FakeSandboxRunner(outcome=TestOutcome.FAILED)  # never passes, ever
        error_analyzer = FakeErrorAnalyzer(retryable=True)  # always claims it's fixable

        graph = build_graph(
            extractor=extractor,
            code_generator=code_generator,
            sandbox_runner=sandbox_runner,
            error_analyzer=error_analyzer,
        )
        initial_state = {
            "config": _base_config(source_root, target_root),
            "budget": BudgetState(max_usd=10.0),
            "max_retry_rounds": 2,
        }

        final_state = await graph.ainvoke(initial_state, config={"recursion_limit": 200})

        assert final_state["phase"] == MigrationPhase.NEEDS_HUMAN
        assert final_state["retry_round"] == 2  # stopped exactly at the cap
        assert len(sandbox_runner.calls) == 2  # exactly 2 full-suite attempts — not 3, not infinite
        assert all(t.status == TaskStatus.NEEDS_HUMAN for t in final_state["tasks"])


@pytest.mark.asyncio
async def test_publishes_pr_with_exactly_the_effective_diffs_when_approval_not_required():
    with tempfile.TemporaryDirectory() as source_root, tempfile.TemporaryDirectory() as target_root:
        _write_spring_repo(source_root)
        publisher = FakePublisher()
        graph = build_graph(
            extractor=FakeExtractor(),
            code_generator=FakeCodeGenerator(),
            sandbox_runner=FakeSandboxRunner(outcome=TestOutcome.PASSED),
            pr_publisher=publisher,
        )
        config = _base_config(source_root, target_root)
        config["github_target_repo"] = "acme/orders-api"
        config["require_human_approval_before_pr"] = False

        final_state = await graph.ainvoke({"config": config, "budget": BudgetState(max_usd=10.0)})

        assert final_state["phase"] == MigrationPhase.DONE
        assert final_state["pr_url"] == "https://github.com/acme/orders-api/pull/42"
        assert len(publisher.calls) == 1
        published = sorted(f.path for f in publisher.calls[0]["files"])
        expected = sorted(t.target_files[0].path for t in final_state["tasks"])
        assert published == expected