"""
Budget reconcile node.

Runs once, single-threaded, after all parallel editor branches for a
wave have joined back together. Folds `budget_deltas` (append-only,
written by concurrent editor branches — see the reducer comment in
graph_state.py) into the authoritative `budget`, tracking how many
entries it has already consumed via `budget_deltas_consumed` so a
later wave's reconcile never double-counts an earlier wave's spend.
"""

from __future__ import annotations

from app.graph.state import BudgetState


def build_budget_reconcile_node():
    async def budget_reconcile_node(state: dict) -> dict:
        deltas: list[float] = state.get("budget_deltas", [])
        consumed: int = state.get("budget_deltas_consumed", 0)
        budget: BudgetState = state["budget"]

        for cost in deltas[consumed:]:
            budget.record_call(cost_usd=cost)

        return {"budget": budget, "budget_deltas_consumed": len(deltas)}

    return budget_reconcile_node
