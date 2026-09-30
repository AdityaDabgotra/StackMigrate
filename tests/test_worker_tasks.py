from __future__ import annotations

import os
import re
import tempfile
from contextlib import asynccontextmanager

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.api.schemas import MigrationRequest
from app.graph.state import MigrationPhase, TaskStatus, TestOutcome
from app.worker.tasks import _run_migration_async
from tests.test_comprehension_node import FakeExtractor, _write_spring_repo
from tests.test_graph_integration import FakeCodeGenerator, FakeSandboxRunner


class FakeRepoPreparer:
    def __init__(self, source_dir: str, target_dir: str):
        self.source_dir = source_dir
        self.target_dir = target_dir
        self.source_calls: list[dict] = []
        self.target_calls: list[dict] = []

    async def prepare_source(self, *, repo_url: str, ref: str, run_id: str) -> str:
        self.source_calls.append({"repo_url": repo_url, "ref": ref, "run_id": run_id})
        return self.source_dir

    async def prepare_target(self, *, repo_url: str | None, target_stack: str, run_id: str) -> str:
        self.target_calls.append({"repo_url": repo_url, "target_stack": target_stack, "run_id": run_id})
        return self.target_dir


@asynccontextmanager
async def _memory_checkpointer_cm():
    yield MemorySaver()


def _request(**overrides) -> MigrationRequest:
    defaults = dict(
        source_repo_url="https://example.com/repo.git",
        source_ref="main",
        source_stack="springboot",
        target_stack="fastapi",
        scope_description="migrate the OrderController module only",
        github_target_repo=None,
        require_human_approval_before_pr=True,
        max_usd=10.0,
        max_retry_rounds=5,
    )
    defaults.update(overrides)
    return MigrationRequest(**defaults)


@pytest.mark.asyncio
async def test_run_migration_async_calls_repo_prep_with_the_right_arguments():
    with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as target_dir:
        _write_spring_repo(source_dir)
        repo_preparer = FakeRepoPreparer(source_dir, target_dir)

        await _run_migration_async(
            "run-xyz",
            _request(),
            repo_preparer=repo_preparer,
            checkpointer_cm=_memory_checkpointer_cm,
            extractor=FakeExtractor(),
            code_generator=FakeCodeGenerator(),
            sandbox_runner=FakeSandboxRunner(outcome=TestOutcome.PASSED),
        )

        assert repo_preparer.source_calls == [
            {"repo_url": "https://example.com/repo.git", "ref": "main", "run_id": "run-xyz"}
        ]
        assert repo_preparer.target_calls == [
            {"repo_url": None, "target_stack": "fastapi", "run_id": "run-xyz"}
        ]


@pytest.mark.asyncio
async def test_run_migration_async_drives_the_graph_to_completion():
    with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as target_dir:
        _write_spring_repo(source_dir)
        repo_preparer = FakeRepoPreparer(source_dir, target_dir)

        final_state = await _run_migration_async(
            "run-xyz",
            _request(),
            repo_preparer=repo_preparer,
            checkpointer_cm=_memory_checkpointer_cm,
            extractor=FakeExtractor(),
            code_generator=FakeCodeGenerator(),
            sandbox_runner=FakeSandboxRunner(outcome=TestOutcome.PASSED),
        )

        assert all(t.status == TaskStatus.SUCCEEDED for t in final_state["tasks"])
        assert final_state["full_suite_result"].outcome == TestOutcome.PASSED
        # no github_target_repo in the default request -> DONE without a PR
        assert final_state["phase"] == MigrationPhase.DONE


@pytest.mark.asyncio
async def test_run_migration_async_state_is_pollable_via_the_same_checkpointer():
    """
    Proves the whole point of using the graph's own checkpointer for
    status: a status lookup against the SAME checkpointer + thread_id
    the worker used sees the final state, exactly the way the API's
    status endpoint (app/api/app.py) will.
    """
    with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as target_dir:
        _write_spring_repo(source_dir)
        repo_preparer = FakeRepoPreparer(source_dir, target_dir)
        shared_saver = MemorySaver()

        @asynccontextmanager
        async def shared_cm():
            yield shared_saver

        from app.graph.build import build_graph

        await _run_migration_async(
            "run-poll",
            _request(),
            repo_preparer=repo_preparer,
            checkpointer_cm=shared_cm,
            extractor=FakeExtractor(),
            code_generator=FakeCodeGenerator(),
            sandbox_runner=FakeSandboxRunner(outcome=TestOutcome.PASSED),
        )

        # a fresh graph instance, same checkpointer, same thread_id
        graph = build_graph(checkpointer=shared_saver)
        snapshot = await graph.aget_state({"configurable": {"thread_id": "run-poll"}})

        assert snapshot.values["phase"] == MigrationPhase.DONE
        assert len(snapshot.values["tasks"]) == 3
