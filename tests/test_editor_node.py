from __future__ import annotations

import pytest

from app.core.llm import EditorGenerationResult, EditorResponse
from app.graph.nodes.editor import build_editor_node
from app.graph.state import BudgetState, FileDiff, MigrationTask, TargetFileSpec, TaskStatus


def _sample_task(**overrides) -> MigrationTask:
    defaults = dict(
        id="task::app_routers_order_py",
        source_unit_ids=["endpoint::create_order"],
        target_files=[TargetFileSpec(path="app/routers/order.py", purpose="order endpoints")],
        synthesis_instructions="Use APIRouter. Implement createOrder.",
    )
    defaults.update(overrides)
    return MigrationTask(**defaults)


class FakeCodeGenerator:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[str] = []

    async def generate(self, prompt: str) -> EditorResponse:
        self.calls.append(prompt)
        if self.fail:
            raise RuntimeError("simulated generation failure")
        return EditorResponse(
            result=EditorGenerationResult(
                files=[FileDiff(path="app/routers/order.py", content="# generated router\n")],
                notes="looks good",
            ),
            cost_usd=0.02,
            input_tokens=800,
            output_tokens=400,
        )


@pytest.mark.asyncio
async def test_editor_node_success_path():
    generator = FakeCodeGenerator()
    node = build_editor_node(generator)
    state = {"current_task": _sample_task(), "budget": BudgetState(max_usd=10.0)}

    update = await node(state)

    assert update["tasks"][0].status == TaskStatus.SUCCEEDED
    assert update["editor_results"][0].task_id == "task::app_routers_order_py"
    assert update["editor_results"][0].diffs[0].content == "# generated router\n"
    assert update["budget_deltas"] == [0.02]


@pytest.mark.asyncio
async def test_editor_node_skips_when_budget_exhausted():
    generator = FakeCodeGenerator()
    node = build_editor_node(generator)
    state = {"current_task": _sample_task(), "budget": BudgetState(max_usd=1.0, spent_usd=1.0)}

    update = await node(state)

    assert update["tasks"][0].status == TaskStatus.SKIPPED
    assert len(generator.calls) == 0  # never even called the LLM
    assert "editor_results" not in update


@pytest.mark.asyncio
async def test_editor_node_marks_retrying_on_failure_below_max_retries():
    generator = FakeCodeGenerator(fail=True)
    node = build_editor_node(generator)
    state = {
        "current_task": _sample_task(retry_count=0, max_retries=3),
        "budget": BudgetState(max_usd=10.0),
    }

    update = await node(state)

    assert update["tasks"][0].status == TaskStatus.RETRYING
    assert update["tasks"][0].retry_count == 1
    assert "editor_results" not in update
    assert "budget_deltas" not in update  # failed call: no cost recorded (real client would still incur cost
    # for a partial call in practice, but the fake never "spends" on
    # failure here since it raises before returning a response)


@pytest.mark.asyncio
async def test_editor_node_escalates_to_needs_human_after_max_retries():
    generator = FakeCodeGenerator(fail=True)
    node = build_editor_node(generator)
    state = {
        "current_task": _sample_task(retry_count=3, max_retries=3),
        "budget": BudgetState(max_usd=10.0),
    }

    update = await node(state)

    assert update["tasks"][0].status == TaskStatus.NEEDS_HUMAN
    assert update["tasks"][0].retry_count == 4


@pytest.mark.asyncio
async def test_editor_prompt_includes_retry_context_on_subsequent_attempts():
    generator = FakeCodeGenerator()
    node = build_editor_node(generator)
    state = {"current_task": _sample_task(retry_count=1), "budget": BudgetState(max_usd=10.0)}

    await node(state)

    assert "retry attempt 1" in generator.calls[0]
