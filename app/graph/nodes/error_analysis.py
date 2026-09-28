"""
Error analysis node.

Runs when the full test suite failed (routed here by
`route_after_sandbox`). Job: figure out which already-migrated task(s)
are likely responsible, and for each one, decide retry vs. escalate to
a human. Tasks it marks RETRYING become eligible for re-dispatch the
next time `route_to_editors` runs (see app/graph/build.py — the same
edge that handles Step 4's multi-wave dependency scheduling also
carries retry waves, by design, not by accident).

Two independent bounds prevent an infinite retry loop:
  1. Per-task: `MigrationTask.retry_count` vs `.max_retries` (checked here,
     same fields the editor node's own generation-failure retries use —
     one shared counter regardless of WHICH stage caused the failure).
  2. Global: `retry_round` vs `max_retry_rounds` — even if every
     individual task is still under its own retry cap, this stops the
     whole run after N full-suite attempts. Without this, a task that
     fails differently each round (never exceeding ITS cap on any
     single round because each "failure" looks like a fresh problem)
     could loop indefinitely.
Hitting either bound escalates the task(s) to NEEDS_HUMAN, which
`get_ready_tasks` (app/graph/routing.py) permanently excludes from
future dispatch — the loop provably terminates once every candidate
task is SUCCEEDED or NEEDS_HUMAN, both terminal with respect to dispatch.
"""

from __future__ import annotations

from typing import Protocol

from app.core.llm import ErrorAnalysisResponse
from app.graph.state import (
    BudgetState,
    EditorResult,
    ErrorAnalysis,
    MigrationPhase,
    MigrationTask,
    TaskStatus,
    TestResult,
)

DEFAULT_MAX_RETRY_ROUNDS = 5


class ErrorAnalyzer(Protocol):
    async def analyze(self, prompt: str) -> ErrorAnalysisResponse: ...


def _build_analysis_prompt(
    test_result: TestResult, candidate_tasks: list[MigrationTask], editor_results: list[EditorResult]
) -> str:
    diffs_by_task = {er.task_id: er for er in editor_results}
    task_sections = []
    for task in candidate_tasks:
        editor_result = diffs_by_task.get(task.id)
        file_previews = ""
        if editor_result:
            for diff in editor_result.diffs:
                # Truncate generously but boundedly — full file content matters for
                # root-causing, but an unbounded prompt risks the budget on one call.
                preview = diff.content if len(diff.content) <= 3000 else diff.content[:3000] + "\n... [truncated]"
                file_previews += f"\n  --- {diff.path} ---\n{preview}\n"
        task_sections.append(
            f"Task '{task.id}' (attempt {task.retry_count + 1}, target file(s): "
            f"{', '.join(tf.path for tf in task.target_files)}):{file_previews}"
        )

    failing = "\n".join(f"- {t}" for t in test_result.failing_tests) or "(none listed individually)"
    output_preview = (
        test_result.raw_output if len(test_result.raw_output) <= 8000 else test_result.raw_output[:8000] + "\n... [truncated]"
    )

    return f"""A migrated codebase's test suite just ran with outcome: {test_result.outcome.value}.

Failing tests:
{failing}

Full test runner output:
{output_preview}

Below are the migration tasks whose generated code is present in this test run (only these
are candidates — nothing else was recently changed):
{"".join(task_sections)}

For each task you can confidently implicate in the failure, provide a root cause, whether
it's worth retrying (a clear, fixable code issue) vs. not (e.g. the failure reveals a
fundamental misunderstanding of requirements that a retry won't fix), and concrete fix
instructions to feed into the next generation attempt. If you cannot confidently attribute
the failure to any specific task above, leave `analyses` empty and explain why in
`unattributable_summary` instead of guessing.
"""


def build_error_analysis_node(analyzer: ErrorAnalyzer, *, max_retry_rounds_default: int = DEFAULT_MAX_RETRY_ROUNDS):
    async def error_analysis_node(state: dict) -> dict:
        test_result = state.get("full_suite_result")
        tasks = state.get("tasks", [])
        editor_results = state.get("editor_results", [])
        budget: BudgetState = state["budget"]

        retry_round = state.get("retry_round", 0) + 1
        max_rounds = state.get("max_retry_rounds") or max_retry_rounds_default
        round_cap_hit = retry_round >= max_rounds

        # Only tasks that actually have code in this test run are viable
        # suspects — a PENDING/blocked task never ran, can't be at fault.
        implicated_ids_with_code = {er.task_id for er in editor_results}
        candidate_tasks = [t for t in tasks if t.status == TaskStatus.SUCCEEDED and t.id in implicated_ids_with_code]

        if test_result is None or not candidate_tasks:
            return {
                "phase": MigrationPhase.NEEDS_HUMAN,
                "retry_round": retry_round,
                "error_log": [
                    "Error analysis could not run: no test result and/or no candidate tasks with code to blame."
                ],
            }

        if budget.remaining_usd() <= 0:
            escalated = [t.model_copy(update={"status": TaskStatus.NEEDS_HUMAN}) for t in candidate_tasks]
            return {
                "tasks": escalated,
                "phase": MigrationPhase.NEEDS_HUMAN,
                "retry_round": retry_round,
                "error_log": ["Error analysis skipped and all candidate tasks escalated: budget exhausted."],
            }

        prompt = _build_analysis_prompt(test_result, candidate_tasks, editor_results)
        try:
            response = await analyzer.analyze(prompt)
        except Exception as exc:  # noqa: BLE001 — a failed analysis call must still let the run terminate cleanly
            escalated = [t.model_copy(update={"status": TaskStatus.NEEDS_HUMAN}) for t in candidate_tasks]
            return {
                "tasks": escalated,
                "phase": MigrationPhase.NEEDS_HUMAN,
                "retry_round": retry_round,
                "error_log": [f"Error analysis call failed, escalating all candidates: {exc!r}"],
            }

        analyses: list[ErrorAnalysis] = response.result.analyses
        candidate_by_id = {t.id: t for t in candidate_tasks}
        updated_tasks: list[MigrationTask] = []

        for analysis in analyses:
            task = candidate_by_id.get(analysis.task_id)
            if task is None:
                continue  # LLM referenced a task_id we didn't offer as a candidate — ignore, don't guess

            new_retry_count = task.retry_count + 1
            exceeds_retry_cap = new_retry_count > task.max_retries
            if round_cap_hit or not analysis.is_retryable or exceeds_retry_cap:
                status = TaskStatus.NEEDS_HUMAN
            else:
                status = TaskStatus.RETRYING
            updated_tasks.append(task.model_copy(update={"status": status, "retry_count": new_retry_count}))

        any_retrying = any(t.status == TaskStatus.RETRYING for t in updated_tasks)
        next_phase = MigrationPhase.EDITING if any_retrying else MigrationPhase.NEEDS_HUMAN

        # Unlike the editor node, this node is NOT part of a parallel Send()
        # fanout — it's a single, ordinary node invocation, so there's no
        # concurrent-mutation hazard and no need for the budget_deltas
        # indirection. Reconciling directly here also guarantees the cost is
        # captured even on the path where the run terminates immediately
        # after this node (no RETRYING tasks -> straight to END, meaning
        # budget_reconcile never runs again to pick up a delta).
        budget.record_call(cost_usd=response.cost_usd)

        update: dict = {
            "phase": next_phase,
            "retry_round": retry_round,
            "budget": budget,
        }
        if updated_tasks:
            update["tasks"] = updated_tasks
        if not analyses:
            update["error_log"] = [
                f"Error analysis could not attribute the failure to a specific task: "
                f"{response.result.unattributable_summary or '(no explanation given)'}"
            ]

        return update

    return error_analysis_node
