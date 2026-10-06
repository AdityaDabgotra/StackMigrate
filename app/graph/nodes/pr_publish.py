"""
PR publish node.

Reached only after `aggregate` (and, when approval is required, after the
human approval gate — see app/graph/nodes/approval.py). Outcomes:
  1. No `pr_draft` -> aggregate failed upstream; leave its FAILED state alone.
  2. No `github_target_repo` configured -> nothing to publish to. Ends at
     DONE with the draft still in state (a dry run).
  3. Approval required but no recorded approval -> REFUSE (FAILED). The
     router should make this unreachable; this check exists so a wiring
     mistake can never open a PR nobody approved.
  4. Otherwise publish.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from app.graph.routing import needs_human_approval
from app.graph.state import MigrationPhase, PRDraft
from app.vcs.interface import PRPublisher, PublishedPR


class Publisher(Protocol):
    def publish(self, **kwargs) -> PublishedPR: ...


async def publish_approved_pr(state: dict, publisher: PRPublisher) -> dict:
    """
    Actually calls the publisher. Kept standalone so the publish logic is
    testable on its own, separate from the approval policy in the node.
    """
    config = state["config"]
    draft: PRDraft = state["pr_draft"]
    diffs = state.get("_effective_diffs_cache")
    if diffs is None:
        from app.graph.diffs import effective_diffs

        diffs = effective_diffs(state.get("editor_results", []), state.get("tasks", []))

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None,
            lambda: publisher.publish(
                repo_full_name=config["github_target_repo"],
                base_branch=config.get("target_base_branch", "main"),
                branch_name=draft.branch_name,
                commit_message=draft.commit_message,
                pr_title=draft.title,
                pr_body=draft.body,
                files=diffs,
            ),
        )
    except Exception as exc:  # noqa: BLE001 — publish failure must produce a clean terminal state, not a crash
        return {
            "phase": MigrationPhase.FAILED,
            "error_log": [f"PR publish failed: {exc!r}"],
        }

    return {
        "phase": MigrationPhase.DONE,
        "pr_url": result.url,
        "pr_branch_name": result.branch_name,
    }


def build_pr_publish_node(publisher: PRPublisher):
    async def pr_publish_node(state: dict) -> dict:
        config = state["config"]

        if state.get("pr_draft") is None:
            # aggregate() failed upstream (empty diffs or an unpublishable path)
            # and already set phase=FAILED with an explanatory error_log entry —
            # nothing to do here but leave that terminal state untouched. The
            # fixed aggregate -> pr_publish edge always runs this node, so this
            # guard is what keeps a failed aggregation from crashing on a
            # missing pr_draft instead of just... staying failed.
            return {}

        if not config.get("github_target_repo"):
            return {
                "phase": MigrationPhase.DONE,
                "error_log": ["No github_target_repo configured — migration complete, PR not opened."],
            }

        if needs_human_approval(state) and state.get("approval_status") != "approved":
            return {
                "phase": MigrationPhase.FAILED,
                "error_log": ["Refusing to publish: human approval is required but was not recorded."],
            }

        return await publish_approved_pr(state, publisher)

    return pr_publish_node
