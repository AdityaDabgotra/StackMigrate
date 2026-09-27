"""
Editor node.

Invoked once per parallel `Send()` branch (see app/graph/routing.py),
each branch handling exactly one MigrationTask. Reads `state["current_task"]`
— populated only by the Send() arg, not part of the graph's steady-state
— generates complete file content for that task's target files, and
returns the task's updated status alongside the new EditorResult.

Budget handling: does NOT mutate `state["budget"]` — see the long
comment on `budget_deltas` in app/graph/state/graph_state.py for why an
in-place mutation is unsafe here. This node only reads `budget` for a
soft pre-check and emits its own spend as an additive delta.
"""

from __future__ import annotations

from typing import Protocol

from app.core.llm import EditorResponse
from app.graph.state import BudgetState, EditorResult, MigrationTask, TaskStatus


class CodeGenerator(Protocol):
    async def generate(self, prompt: str) -> EditorResponse: ...


def _build_editor_prompt(task: MigrationTask) -> str:
    files_desc = "\n".join(f"- {tf.path}: {tf.purpose}" for tf in task.target_files)
    retry_context = ""
    if task.retry_count > 0:
        retry_context = (
            f"\nNOTE: this is retry attempt {task.retry_count} for this task. "
            "A previous attempt failed — pay close attention to the instructions below "
            "and avoid repeating whatever caused the prior failure.\n"
        )

    return f"""You are migrating code to a target tech stack. Generate COMPLETE, working file
content for the file(s) below — no placeholders, no "// TODO: implement", no markdown
code fences in the content field (it must be the literal raw file content).

Target file(s) to produce:
{files_desc}
{retry_context}
Instructions:
{task.synthesis_instructions}
"""


def build_editor_node(generator: CodeGenerator):
    async def editor_node(state: dict) -> dict:
        task: MigrationTask = state["current_task"]
        budget: BudgetState = state["budget"]

        # Soft pre-check only: under concurrent fanout this can't see other
        # in-flight branches' spend precisely (see budget_deltas comment in
        # graph_state.py), so a small overshoot past the cap is possible in
        # the worst case. Hardening this into a hard, exact ceiling is
        # tracked for Step 8 (budget guard + observability).
        if budget.remaining_usd() <= 0:
            skipped_task = task.model_copy(update={"status": TaskStatus.SKIPPED})
            return {
                "tasks": [skipped_task],
                "error_log": [f"Editor skipped '{task.id}': budget already exhausted."],
            }

        prompt = _build_editor_prompt(task)

        try:
            response = await generator.generate(prompt)
        except Exception as exc:  # noqa: BLE001 — one task's generation failure must not abort the whole fanout
            retry_count = task.retry_count + 1
            status = TaskStatus.NEEDS_HUMAN if retry_count > task.max_retries else TaskStatus.RETRYING
            updated_task = task.model_copy(update={"status": status, "retry_count": retry_count})
            return {
                "tasks": [updated_task],
                "error_log": [f"Editor generation failed for '{task.id}': {exc!r}"],
            }

        succeeded_task = task.model_copy(update={"status": TaskStatus.SUCCEEDED})
        editor_result = EditorResult(
            task_id=task.id,
            diffs=response.result.files,
            editor_notes=response.result.notes,
            tokens_used=response.input_tokens + response.output_tokens,
        )
        return {
            "tasks": [succeeded_task],
            "editor_results": [editor_result],
            "budget_deltas": [response.cost_usd],
        }

    return editor_node
