from __future__ import annotations

import tempfile

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from app.api.app import create_app
from app.api.schemas import MigrationRequest
from app.graph.build import build_graph
from app.graph.state import BudgetState, MigrationPhase, TaskStatus, TestOutcome
from app.vcs.interface import PublishedPR
from tests.test_comprehension_node import FakeExtractor, _write_spring_repo
from tests.test_graph_integration import FakeCodeGenerator, FakeSandboxRunner


class FakeTaskSubmitter:
    def __init__(self):
        self.calls: list[dict] = []

    def submit(self, *, run_id: str, request: MigrationRequest) -> None:
        self.calls.append({"run_id": run_id, "request": request})


class FakePRPublisher:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[dict] = []

    def publish(self, **kwargs) -> PublishedPR:
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("simulated publish failure")
        return PublishedPR(url="https://github.com/acme/orders-api/pull/7", branch_name=kwargs["branch_name"], commit_sha="cafebabe")


def _request_payload(**overrides) -> dict:
    base = dict(
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
    base.update(overrides)
    return base


def test_submit_migration_returns_202_and_a_run_id_and_hands_off_to_the_submitter():
    submitter = FakeTaskSubmitter()
    graph = build_graph(checkpointer=MemorySaver())
    app = create_app(graph=graph, task_submitter=submitter)
    client = TestClient(app)

    response = client.post("/migrations", json=_request_payload())

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["run_id"].startswith("run-")
    assert len(submitter.calls) == 1
    assert submitter.calls[0]["run_id"] == body["run_id"]
    assert submitter.calls[0]["request"].source_stack == "springboot"


def test_submit_migration_rejects_invalid_payload():
    submitter = FakeTaskSubmitter()
    graph = build_graph(checkpointer=MemorySaver())
    app = create_app(graph=graph, task_submitter=submitter)
    client = TestClient(app)

    response = client.post("/migrations", json={"source_repo_url": "x"})  # missing required fields

    assert response.status_code == 422
    assert len(submitter.calls) == 0


def test_status_for_unknown_run_returns_404():
    graph = build_graph(checkpointer=MemorySaver())
    app = create_app(graph=graph, task_submitter=FakeTaskSubmitter())
    client = TestClient(app)

    response = client.get("/migrations/run-does-not-exist")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_status_reflects_real_graph_state_after_a_completed_run():
    with tempfile.TemporaryDirectory() as source_root, tempfile.TemporaryDirectory() as target_root:
        _write_spring_repo(source_root)
        saver = MemorySaver()
        graph = build_graph(
            checkpointer=saver,
            extractor=FakeExtractor(),
            code_generator=FakeCodeGenerator(),
            sandbox_runner=FakeSandboxRunner(outcome=TestOutcome.PASSED),
        )

        config = {
            "run_id": "run-status-test",
            "source_repo_url": "x",
            "source_ref": "main",
            "source_stack": "springboot",
            "target_stack": "fastapi",
            "scope_description": "migrate the OrderController module only",
            "github_target_repo": None,
            "require_human_approval_before_pr": True,
            "local_checkout_path": source_root,
            "target_workspace_path": target_root,
        }
        await graph.ainvoke(
            {"config": config, "budget": BudgetState(max_usd=10.0)},
            config={"configurable": {"thread_id": "run-status-test"}},
        )

        app = create_app(graph=graph, task_submitter=FakeTaskSubmitter())
        client = TestClient(app)

        response = client.get("/migrations/run-status-test")
        assert response.status_code == 200
        body = response.json()
        assert body["phase"] == "done"
        assert len(body["tasks"]) == 3
        assert all(t["status"] == "succeeded" for t in body["tasks"])
        assert body["full_suite_outcome"] == "passed"
        assert body["budget_spent_usd"] > 0


@pytest.mark.asyncio
async def test_approve_publishes_pr_when_run_is_awaiting_approval():
    with tempfile.TemporaryDirectory() as source_root, tempfile.TemporaryDirectory() as target_root:
        _write_spring_repo(source_root)
        saver = MemorySaver()
        publisher = FakePRPublisher()
        graph = build_graph(
            checkpointer=saver,
            extractor=FakeExtractor(),
            code_generator=FakeCodeGenerator(),
            sandbox_runner=FakeSandboxRunner(outcome=TestOutcome.PASSED),
            pr_publisher=publisher,
        )

        config = {
            "run_id": "run-approve-test",
            "source_repo_url": "x",
            "source_ref": "main",
            "source_stack": "springboot",
            "target_stack": "fastapi",
            "scope_description": "migrate the OrderController module only",
            "github_target_repo": "acme/orders-api",
            "require_human_approval_before_pr": True,
            "local_checkout_path": source_root,
            "target_workspace_path": target_root,
        }
        await graph.ainvoke(
            {"config": config, "budget": BudgetState(max_usd=10.0)},
            config={"configurable": {"thread_id": "run-approve-test"}},
        )

        app = create_app(graph=graph, task_submitter=FakeTaskSubmitter())
        client = TestClient(app)

        # the run is genuinely PAUSED at the gate (nothing published yet) and exposes what to review
        pre = client.get("/migrations/run-approve-test").json()
        assert pre["phase"] == MigrationPhase.AWAITING_APPROVAL.value
        assert pre["pending_approval"]["files"] and pre["pending_approval"]["title"]
        assert publisher.calls == []

        response = client.post("/migrations/run-approve-test/approve")

        assert response.status_code == 200
        body = response.json()
        assert body["phase"] == "done"
        assert body["pr_url"] == "https://github.com/acme/orders-api/pull/7"
        assert len(publisher.calls) == 1

        # status reflects the resumed run, and the pending-approval block is gone
        after = client.get("/migrations/run-approve-test").json()
        assert after["phase"] == "done"
        assert after["approval_status"] == "approved"
        assert after["pending_approval"] is None
        assert after["pr_url"] == "https://github.com/acme/orders-api/pull/7"


def test_approve_on_unknown_run_returns_404():
    graph = build_graph(checkpointer=MemorySaver())
    app = create_app(graph=graph, task_submitter=FakeTaskSubmitter())
    client = TestClient(app)

    response = client.post("/migrations/run-does-not-exist/approve")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_approve_on_run_not_awaiting_approval_returns_409():
    with tempfile.TemporaryDirectory() as source_root, tempfile.TemporaryDirectory() as target_root:
        _write_spring_repo(source_root)
        saver = MemorySaver()
        publisher = FakePRPublisher()
        graph = build_graph(
            checkpointer=saver,
            extractor=FakeExtractor(),
            code_generator=FakeCodeGenerator(),
            sandbox_runner=FakeSandboxRunner(outcome=TestOutcome.PASSED),
            pr_publisher=publisher,
        )
        config = {
            "run_id": "run-not-awaiting",
            "source_repo_url": "x",
            "source_ref": "main",
            "source_stack": "springboot",
            "target_stack": "fastapi",
            "scope_description": "migrate the OrderController module only",
            "github_target_repo": None,  # no target repo -> ends DONE, never AWAITING_APPROVAL
            "require_human_approval_before_pr": True,
            "local_checkout_path": source_root,
            "target_workspace_path": target_root,
        }
        await graph.ainvoke(
            {"config": config, "budget": BudgetState(max_usd=10.0)},
            config={"configurable": {"thread_id": "run-not-awaiting"}},
        )

        app = create_app(graph=graph, task_submitter=FakeTaskSubmitter())
        client = TestClient(app)

        response = client.post("/migrations/run-not-awaiting/approve")

        assert response.status_code == 409
        assert len(publisher.calls) == 0
