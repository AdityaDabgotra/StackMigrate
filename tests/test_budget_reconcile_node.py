from __future__ import annotations

import pytest

from app.graph.nodes.budget_reconcile import build_budget_reconcile_node
from app.graph.state import BudgetState


@pytest.mark.asyncio
async def test_reconcile_folds_all_deltas_on_first_call():
    node = build_budget_reconcile_node()
    budget = BudgetState(max_usd=10.0)
    state = {"budget": budget, "budget_deltas": [0.01, 0.02, 0.015], "budget_deltas_consumed": 0}

    update = await node(state)

    assert update["budget"].spent_usd == pytest.approx(0.045)
    assert update["budget_deltas_consumed"] == 3


@pytest.mark.asyncio
async def test_reconcile_does_not_double_count_across_two_waves():
    node = build_budget_reconcile_node()
    budget = BudgetState(max_usd=10.0)

    # Wave 1: two editor branches spend.
    state = {"budget": budget, "budget_deltas": [0.01, 0.02], "budget_deltas_consumed": 0}
    update1 = await node(state)
    assert update1["budget"].spent_usd == pytest.approx(0.03)
    assert update1["budget_deltas_consumed"] == 2

    # Wave 2: additive reducer means the list keeps growing (old entries
    # remain, new ones appended) — reconcile must only sum the NEW tail.
    state2 = {
        "budget": update1["budget"],
        "budget_deltas": [0.01, 0.02, 0.05],  # 0.01, 0.02 already consumed; 0.05 is new
        "budget_deltas_consumed": update1["budget_deltas_consumed"],
    }
    update2 = await node(state2)

    assert update2["budget"].spent_usd == pytest.approx(0.08)  # 0.03 + 0.05, NOT 0.03 + 0.08
    assert update2["budget_deltas_consumed"] == 3


@pytest.mark.asyncio
async def test_reconcile_is_a_no_op_when_no_new_deltas():
    node = build_budget_reconcile_node()
    budget = BudgetState(max_usd=10.0, spent_usd=0.5)
    state = {"budget": budget, "budget_deltas": [], "budget_deltas_consumed": 0}

    update = await node(state)

    assert update["budget"].spent_usd == pytest.approx(0.5)
    assert update["budget_deltas_consumed"] == 0
