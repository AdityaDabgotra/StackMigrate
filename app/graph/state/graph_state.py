"""
Top-level LangGraph state.

Design notes (read before touching this file):

1. We use a TypedDict, not a pure Pydantic model, because LangGraph's
   state-merging machinery (the `Annotated[..., reducer]` pattern) is
   built around TypedDict + `operator.add`-style reducers. Individual
   fields still hold Pydantic models for validation; only the container
   is a TypedDict.

2. Any field written by PARALLEL nodes (the editor fanout via `Send()`)
   MUST use a reducer that merges lists rather than overwrites them —
   otherwise concurrent branches racing to update the same key silently
   drop each other's results. `editor_results`, `test_results`, and
   `error_analyses` are all fanout outputs and are annotated accordingly.

3. Fields written by exactly one node at a time (planner, aggregator,
   PR generator) don't need a reducer — last-write-wins is correct
   because there's no concurrency on them.

4. This state is checkpointed to Postgres (see app/db/checkpointer.py)
   after every node transition. Keep it JSON-serializable — Pydantic
   models satisfy this via `.model_dump()`, which LangGraph's Postgres
   checkpointer handles automatically via our custom serializer.
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from app.graph.state.budget import BudgetState
from app.graph.state.enums import MigrationPhase
from app.graph.state.ir import ComprehensionResult
from app.graph.state.tasks import ErrorAnalysis, EditorResult, MigrationTask, TestResult
from app.graph.state.pr import PRDraft


def _merge_task_lists(existing: list[MigrationTask], new: list[MigrationTask]) -> list[MigrationTask]:
    """
    Reducer for the task list: tasks are looked up by id and updated in
    place (status/retry_count changes) rather than duplicated. A plain
    `operator.add` would append duplicate copies of the same task every
    time a node touches its status.
    """
    by_id = {t.id: t for t in existing}
    for t in new:
        by_id[t.id] = t
    return list(by_id.values())


class MigrationRunConfig(TypedDict):
    """Immutable-ish run configuration, set once at graph invocation."""

    run_id: str
    source_repo_url: str
    source_ref: str
    source_stack: str          # adapter registry key, e.g. "springboot"
    target_stack: str          # adapter registry key, e.g. "fastapi"
    scope_description: str     # e.g. "migrate the OrderController module only" — enforces the strangler-fig scope
    github_target_repo: str | None
    require_human_approval_before_pr: bool
    local_checkout_path: str   # where source_repo_url has already been cloned to on disk.
                                # Populated by a repo-fetch step that runs before this graph is
                                # invoked. Comprehension reads from here rather than cloning
                                # itself, keeping the node free of git/network concerns.
    target_workspace_path: str # working directory containing a skeleton/checkout of the TARGET
                                # stack project (e.g. an existing FastAPI app the migrated code
                                # gets written into). Editor diffs are applied here before the
                                # Sandbox runs the test suite against it.


class GraphState(TypedDict, total=False):
    # ---- run config (set once) ----
    config: MigrationRunConfig

    # ---- phase tracking ----
    phase: MigrationPhase

    # ---- comprehension output (single-writer) ----
    comprehension: ComprehensionResult | None

    # ---- synthesis output (single-writer) ----
    tasks: Annotated[list[MigrationTask], _merge_task_lists]

    # ---- parallel fanout outputs (multi-writer, need additive reducers) ----
    editor_results: Annotated[list[EditorResult], operator.add]
    test_results: Annotated[list[TestResult], operator.add]
    error_analyses: Annotated[list[ErrorAnalysis], operator.add]

    # ---- per-task input to a single editor branch (Send() arg only; not meaningful outside one branch) ----
    current_task: MigrationTask | None

    # ---- budget accounting for the parallel fanout ----
    # Editor branches never mutate `budget` directly — under Send()-based
    # fanout, state may be copied/serialized per branch, so an in-place
    # mutation on one branch's copy could be silently lost when another
    # branch's update is merged afterward. Instead each branch appends its
    # LLM call cost here (safe under any serialization, since it's a plain
    # additive reducer), and a dedicated single-writer `budget_reconcile`
    # node folds only the NOT-YET-CONSUMED entries into `budget` afterward.
    # See app/graph/nodes/budget_reconcile.py for the consume-index logic.
    budget_deltas: Annotated[list[float], operator.add]
    budget_deltas_consumed: int

    # ---- aggregation output (single-writer) ----
    aggregated_diff_paths: list[str]
    full_suite_result: TestResult | None

    # ---- PR output (single-writer) ----
    pr_draft: PRDraft | None
    pr_url: str | None
    pr_branch_name: str | None

    # ---- human approval (single-writer: only approval_gate sets it) ----
    approval_status: str | None   # "approved" | "rejected" | None (no decision / not required)

    # ---- cross-cutting ----
    budget: BudgetState
    retry_round: int             # global retry-round counter, separate from per-task retry_count
    max_retry_rounds: int
    human_feedback: str | None   # populated after an interrupt() resume
    error_log: Annotated[list[str], operator.add]
