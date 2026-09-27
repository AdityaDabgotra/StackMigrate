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
"""

from __future__ import annotations

import os
import re
import tempfile

import pytest

from app.core.llm import EditorGenerationResult, EditorResponse
from app.graph.build import build_graph
from app.graph.state import BudgetState, FileDiff, MigrationPhase, TaskStatus, TestOutcome, TestResult
from app.sandbox.interface import SandboxRunResult
from tests.test_comprehension_node import FakeExtractor, _write_spring_repo


class FakeCodeGenerator:
    """Extracts target file paths straight out of the editor prompt's own
    "- path: purpose" lines and generates trivial content for each — works
    for any task without needing to know its specifics in advance."""

    def __init__(self):
        self.calls: list[str] = []

    async def generate(self, prompt: str) -> EditorResponse:
        self.calls.append(prompt)
        paths = re.findall(r"^- (\S+):", prompt, re.MULTILINE)
        files = [FileDiff(path=p, content=f"# generated for {p}\n") for p in paths]

        return EditorResponse(
            result=EditorGenerationResult(files=files, notes="fake"),
            cost_usd=0.02,
            input_tokens=600,
            output_tokens=300,
        )


class FakeSandboxRunner:
    def __init__(self, outcome: TestOutcome = TestOutcome.PASSED):
        self.outcome = outcome
        self.calls: list[dict] = []

    def run(self, *, workspace_root, adapter, task_id=None) -> SandboxRunResult:
        self.calls.append({"workspace_root": workspace_root, "task_id": task_id})
        return SandboxRunResult(
            test_result=TestResult(task_id=task_id, outcome=self.outcome, raw_output="fake", duration_seconds=0.5),
            setup_output="fake install",
            setup_succeeded=True,
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

        # --- editor fanout ran for every dependency-satisfied task ---
        # Root-level tasks (no deps, i.e. the repository task) definitely ran this
        # single wave; deeper chains need Step 5's multi-wave loop (documented
        # limitation) so we only assert on what THIS wave guarantees.
        repo_task = next(t for t in tasks if "repository" in t.target_files[0].path)
        assert repo_task.status == TaskStatus.SUCCEEDED

        # --- budget was correctly reconciled from the fanout's deltas ---
        # 3 comprehension extraction calls (0.01 each per FakeExtractor) +
        # N editor calls (0.02 each) for whichever tasks were ready this wave
        editor_calls_made = len(code_generator.calls)
        expected_spent = 0.03 + editor_calls_made * 0.02
        assert final_state["budget"].spent_usd == pytest.approx(expected_spent)

        # --- sandbox actually ran against real files written to disk ---
        assert len(sandbox_runner.calls) == 1
        assert final_state["full_suite_result"].outcome == TestOutcome.PASSED
        assert final_state["phase"] == MigrationPhase.AGGREGATING

        # at least the repository file (no deps -> guaranteed ready this wave)
        # actually landed on disk in the target workspace
        repo_path = repo_task.target_files[0].path
        assert os.path.exists(os.path.join(target_root, repo_path))
        with open(os.path.join(target_root, repo_path)) as f:
            assert "generated for" in f.read()


@pytest.mark.asyncio
async def test_pipeline_routes_to_sandbox_directly_when_synthesis_yields_no_tasks():
    """
    An empty/failed comprehension produces zero tasks; the graph must
    still terminate cleanly via the NO_READY_TASKS sentinel edge rather
    than erroring out on an empty Send() dispatch.
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
        assert final_state["phase"] == MigrationPhase.ERROR_ANALYSIS  # sandbox's "no diffs" branch (Step 3)
