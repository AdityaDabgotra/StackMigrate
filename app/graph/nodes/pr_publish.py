"""
PR publish node.

Three outcomes, in order of precedence:
  1. No `github_target_repo` configured -> nothing to publish to. Ends at
     DONE with the diffs/draft still in state (a caller can still read
     `pr_draft` and `aggregated_diff_paths` — useful for a dry run, or for
     a caller that wants to publish somewhere other than GitHub).
  2. `require_human_approval_before_pr` is True -> stop at AWAITING_APPROVAL
     without calling the publisher. Real interrupt/resume wiring
     (LangGraph's `interrupt()`, pausing this exact point for a human to
     confirm) is Step 9's job; for now this node simply doesn't publish,
     and `publish_approved_pr` below is the function a caller (e.g. the
     Step 7 API layer, once a human approves) invokes afterward — kept
     as a standalone function specifically so it's the same code path
     this node itself uses in case 3, not a second implementation.
  3. Approval not required -> publish immediately.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from app.graph.state import MigrationPhase, PRDraft
from app.vcs.interface import PRPublisher, PublishedPR


class Publisher(Protocol):
    def publish(self, **kwargs) -> PublishedPR: ...


async def publish_approved_pr(state: dict, publisher: PRPublisher) -> dict:
    """
    Actually calls the publisher. Standalone so both the "approval not
    required" path in `pr_publish_node` and a future external caller
    (once a human approves a paused run) go through the exact same logic.
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

        if config.get("require_human_approval_before_pr", True):
            return {"phase": MigrationPhase.AWAITING_APPROVAL}

        return await publish_approved_pr(state, publisher)

    return pr_publish_node
