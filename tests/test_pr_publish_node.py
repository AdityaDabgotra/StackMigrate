from __future__ import annotations

import pytest

from app.graph.nodes.pr_publish import build_pr_publish_node
from app.graph.state import EditorResult, FileDiff, MigrationPhase, PRDraft
from app.vcs.interface import PublishedPR


def _draft() -> PRDraft:
    return PRDraft(
        title="Migrate orders",
        body="body",
        branch_name="StackMigrate/run-1",
        commit_message="StackMigrate: migrate orders",
    )


def _config(**overrides) -> dict:
    base = {
        "run_id": "run-1",
        "source_repo_url": "x",
        "source_ref": "main",
        "source_stack": "springboot",
        "target_stack": "fastapi",
        "scope_description": "x",
        "github_target_repo": "acme/orders-api",
        "require_human_approval_before_pr": True,
        "local_checkout_path": "/x",
        "target_workspace_path": "/y",
    }
    base.update(overrides)
    return base


class FakePublisher:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[dict] = []

    def publish(self, **kwargs) -> PublishedPR:
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("simulated GitHub API failure")
        return PublishedPR(url="https://github.com/acme/orders-api/pull/42", branch_name=kwargs["branch_name"], commit_sha="deadbeef")


def _base_state(**overrides) -> dict:
    state = {
        "config": _config(),
        "pr_draft": _draft(),
        "editor_results": [EditorResult(task_id="t1", diffs=[FileDiff(path="a.py", content="x")])],
        "tasks": [],
    }
    state.update(overrides)
    return state


@pytest.mark.asyncio
async def test_no_target_repo_configured_ends_done_without_publishing():
    publisher = FakePublisher()
    node = build_pr_publish_node(publisher)
    state = _base_state(config=_config(github_target_repo=None))

    update = await node(state)

    assert update["phase"] == MigrationPhase.DONE
    assert len(publisher.calls) == 0


@pytest.mark.asyncio
async def test_approval_required_stops_without_publishing():
    publisher = FakePublisher()
    node = build_pr_publish_node(publisher)
    state = _base_state(config=_config(require_human_approval_before_pr=True))

    update = await node(state)

    assert update["phase"] == MigrationPhase.AWAITING_APPROVAL
    assert len(publisher.calls) == 0


@pytest.mark.asyncio
async def test_approval_not_required_publishes_immediately():
    publisher = FakePublisher()
    node = build_pr_publish_node(publisher)
    state = _base_state(config=_config(require_human_approval_before_pr=False))

    update = await node(state)

    assert update["phase"] == MigrationPhase.DONE
    assert update["pr_url"] == "https://github.com/acme/orders-api/pull/42"
    assert update["pr_branch_name"] == "stackmigrate/run-1"
    assert len(publisher.calls) == 1
    assert publisher.calls[0]["repo_full_name"] == "acme/orders-api"
    assert publisher.calls[0]["pr_title"] == "Migrate orders"


@pytest.mark.asyncio
async def test_publish_failure_produces_clean_failed_state_not_a_crash():
    publisher = FakePublisher(fail=True)
    node = build_pr_publish_node(publisher)
    state = _base_state(config=_config(require_human_approval_before_pr=False))

    update = await node(state)

    assert update["phase"] == MigrationPhase.FAILED
    assert any("simulated GitHub API failure" in e for e in update["error_log"])


@pytest.mark.asyncio
async def test_missing_pr_draft_is_a_no_op_preserving_upstream_failure():
    """
    aggregate() failing upstream leaves no pr_draft in state; this node
    must not crash trying to read one — it should just do nothing and
    let aggregate's own FAILED phase / error_log stand.
    """
    publisher = FakePublisher()
    node = build_pr_publish_node(publisher)
    state = {"config": _config(), "pr_draft": None}

    update = await node(state)

    assert update == {}
    assert len(publisher.calls) == 0
