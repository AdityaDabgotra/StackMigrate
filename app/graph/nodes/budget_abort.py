"""
Budget abort node: the single terminal exit for a budget-exhausted run.

Reached when the dispatch gate (`route_to_editors`) finds work left to do
but no budget to do it with. Marks every unfinished task SKIPPED so the
status endpoint shows exactly what didn't get migrated, records WHY in
`error_log`, and sets `ABORTED_BUDGET`. Deliberately goes to END, not on to
the sandbox/PR: a partial migration is never published automatically.
"""

from __future__ import annotations

from app.graph.state import BudgetState, MigrationPhase, TaskStatus

_UNFINISHED = (TaskStatus.PENDING, TaskStatus.RETRYING, TaskStatus.IN_PROGRESS)


def build_budget_abort_node():
    async def budget_abort_node(state: dict) -> dict:
        budget: BudgetState | None = state.get("budget")
        reason = (budget.exhausted_reason() if budget else None) or "budget exhausted"
        skipped = [
            t.model_copy(update={"status": TaskStatus.SKIPPED})
            for t in state.get("tasks", [])
            if t.status in _UNFINISHED
        ]
        update: dict = {
            "phase": MigrationPhase.ABORTED_BUDGET,
            "error_log": [f"Run aborted: {reason}. {len(skipped)} task(s) were not completed."],
        }
        if skipped:
            update["tasks"] = skipped
        return update

    return budget_abort_node
