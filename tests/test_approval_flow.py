"""
Human-in-the-loop approval through the REAL compiled graph: a real `interrupt()`
pause, a real checkpointed resume (MemorySaver), fake LLM/sandbox/publisher.
"""

from __future__ import annotations

import asyncio
import tempfile

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.api.app import create_app
from app.graph.build import build_graph
from app.graph.state import BudgetState, MigrationPhase, TestOutcome
from tests.test_api import FakeTaskSubmitter
from tests.test_comprehension_node import FakeExtractor, _write_spring_repo
from tests.test_graph_integration import FakeCodeGenerator, FakeSandboxRunner, _base_config
from tests.test_pr_publish_node import FakePublisher

PAUSED_AT = ("approval_gate",)


def _thread(run_id: str) -> dict:
    return {"configurable": {"thread_id": run_id}}


async def _run_until_paused(run_id: str, source_root: str, target_root: str, *, require_approval: bool = True):
    publisher = FakePublisher()
    graph = build_graph(
        checkpointer=MemorySaver(),
        extractor=FakeExtractor(),
        code_generator=FakeCodeGenerator(),
        sandbox_runner=FakeSandboxRunner(outcome=TestOutcome.PASSED),
        pr_publisher=publisher,
    )
    config = _base_config(source_root, target_root)
    config.update(run_id=run_id, github_target_repo="acme/orders-api", require_human_approval_before_pr=require_approval)
    result = await graph.ainvoke({"config": config, "budget": BudgetState(max_usd=10.0)}, _thread(run_id))
    return graph, publisher, result


@pytest.mark.asyncio
async def test_run_pauses_at_the_gate_with_nothing_published_and_a_reviewable_payload():
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
        _write_spring_repo(src)
        graph, publisher, result = await _run_until_paused("run-pause", src, dst)

        snapshot = await graph.aget_state(_thread("run-pause"))
        assert snapshot.next == PAUSED_AT
        assert snapshot.values["phase"] == MigrationPhase.AWAITING_APPROVAL
        assert publisher.calls == []

        payload = result["__interrupt__"][0].value
        assert payload["kind"] == "pr_approval" and payload["run_id"] == "run-pause"
        assert payload["title"] and payload["branch_name"] and payload["files"]


@pytest.mark.asyncio
async def test_approve_resumes_and_publishes_exactly_once_with_the_effective_files():
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
        _write_spring_repo(src)
        graph, publisher, paused = await _run_until_paused("run-ok", src, dst)

        final = await graph.ainvoke(Command(resume={"approved": True, "feedback": "lgtm"}), _thread("run-ok"))

        assert final["phase"] == MigrationPhase.DONE
        assert final["approval_status"] == "approved" and final["human_feedback"] == "lgtm"
        assert final["pr_url"] == "https://github.com/acme/orders-api/pull/42"
        assert len(publisher.calls) == 1
        assert sorted(f.path for f in publisher.calls[0]["files"]) == sorted(paused["__interrupt__"][0].value["files"])


@pytest.mark.asyncio
async def test_reject_ends_the_run_without_publishing_and_keeps_the_feedback():
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
        _write_spring_repo(src)
        graph, publisher, _ = await _run_until_paused("run-no", src, dst)

        final = await graph.ainvoke(Command(resume={"approved": False, "feedback": "wrong module"}), _thread("run-no"))

        assert final["phase"] == MigrationPhase.REJECTED
        assert final["approval_status"] == "rejected"
        assert any("wrong module" in e for e in final["error_log"])
        assert publisher.calls == []
        assert (await graph.aget_state(_thread("run-no"))).next == ()  # finished, not still paused


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_resume", [{"approved": "true"}, {"approved": 1}, {"approved": None}, {"feedback": "no decision key"}, "yes"])
async def test_gate_fails_closed_on_anything_that_is_not_exactly_approved_true(bad_resume):
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
        _write_spring_repo(src)
        graph, publisher, _ = await _run_until_paused("run-bad", src, dst)

        final = await graph.ainvoke(Command(resume=bad_resume), _thread("run-bad"))

        assert final["phase"] == MigrationPhase.REJECTED
        assert publisher.calls == []


@pytest.mark.asyncio
async def test_no_pause_when_approval_not_required_or_no_target_repo():
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
        _write_spring_repo(src)
        graph, publisher, result = await _run_until_paused("run-auto", src, dst, require_approval=False)
        assert "__interrupt__" not in result
        assert result["phase"] == MigrationPhase.DONE and len(publisher.calls) == 1

    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
        _write_spring_repo(src)
        publisher = FakePublisher()
        graph = build_graph(
            checkpointer=MemorySaver(),
            extractor=FakeExtractor(),
            code_generator=FakeCodeGenerator(),
            sandbox_runner=FakeSandboxRunner(outcome=TestOutcome.PASSED),
            pr_publisher=publisher,
        )
        result = await graph.ainvoke(
            {"config": _base_config(src, dst), "budget": BudgetState(max_usd=10.0)}, _thread("run-norepo")
        )
        assert "__interrupt__" not in result and result["phase"] == MigrationPhase.DONE
        assert publisher.calls == []


# ---- over HTTP ------------------------------------------------------------

def _client(graph) -> TestClient:
    return TestClient(create_app(graph=graph, task_submitter=FakeTaskSubmitter()))


@pytest.mark.asyncio
async def test_http_reject_then_409_on_a_second_decision():
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
        _write_spring_repo(src)
        graph, publisher, _ = await _run_until_paused("run-http", src, dst)
        client = _client(graph)

        rejected = client.post("/migrations/run-http/reject", json={"feedback": "not ready"})
        assert rejected.status_code == 200 and rejected.json()["phase"] == "rejected"

        status = client.get("/migrations/run-http").json()
        assert status["approval_status"] == "rejected" and status["human_feedback"] == "not ready"
        assert status["pending_approval"] is None

        assert client.post("/migrations/run-http/approve").status_code == 409  # already decided
        assert publisher.calls == []


@pytest.mark.asyncio
async def test_concurrent_double_approve_publishes_only_once():
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
        _write_spring_repo(src)
        graph, publisher, _ = await _run_until_paused("run-race", src, dst)
        app = create_app(graph=graph, task_submitter=FakeTaskSubmitter())

        import httpx

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            first, second = await asyncio.gather(
                client.post("/migrations/run-race/approve"), client.post("/migrations/run-race/approve")
            )

        assert sorted([first.status_code, second.status_code]) == [200, 409]
        assert len(publisher.calls) == 1
