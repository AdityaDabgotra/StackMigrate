from __future__ import annotations

import json
import logging

import pytest

from app.core.observability import JsonFormatter, observed_node, run_config
from app.graph.state import BudgetState, MigrationPhase


@pytest.mark.asyncio
async def test_observed_node_logs_one_structured_line_and_returns_result_untouched(caplog):
    async def node(state):
        return {"phase": MigrationPhase.EDITING, "budget_deltas": [0.02, 0.03]}

    state = {"config": {"run_id": "run-7"}, "budget": BudgetState(max_usd=1.0, spent_usd=0.25)}
    with caplog.at_level(logging.INFO, logger="stackmigrate"):
        update = await observed_node("editor", node)(state)

    assert update == {"phase": MigrationPhase.EDITING, "budget_deltas": [0.02, 0.03]}
    rec = next(r for r in caplog.records if r.getMessage() == "node_finished")
    assert (rec.run_id, rec.node, rec.phase) == ("run-7", "editor", "editing")
    assert rec.cost_delta_usd == pytest.approx(0.05)
    assert rec.budget_remaining_usd == pytest.approx(0.75)


@pytest.mark.asyncio
async def test_observed_node_logs_and_reraises_failures(caplog):
    async def boom(state):
        raise RuntimeError("kaput")

    with caplog.at_level(logging.INFO, logger="stackmigrate"):
        with pytest.raises(RuntimeError, match="kaput"):
            await observed_node("sandbox_test", boom)({"config": {"run_id": "r"}})

    assert any(r.getMessage() == "node_failed" and r.node == "sandbox_test" for r in caplog.records)


def test_json_formatter_emits_extra_fields_as_top_level_keys():
    record = logging.LogRecord("stackmigrate", logging.INFO, __file__, 1, "node_finished", (), None)
    record.run_id = "r1"
    out = json.loads(JsonFormatter().format(record))
    assert out["event"] == "node_finished" and out["run_id"] == "r1"


def test_run_config_carries_thread_id_and_searchable_trace_metadata():
    cfg = run_config("run-9", source_stack="springboot", target_stack="fastapi", max_usd=5.0)
    assert cfg["configurable"] == {"thread_id": "run-9"}
    assert "springboot->fastapi" in cfg["tags"]
    assert cfg["metadata"]["run_id"] == "run-9"
