"""
Which generated files are the *effective* output of a run?

`editor_results` is append-only (an additive reducer, see graph_state.py),
so after retries it holds several attempts for the same task. Both the
sandbox (what gets tested) and the PR publisher (what gets shipped) MUST
derive their file set from this one module — if they computed it
independently, the code that passed the tests could differ from the code
in the pull request.

Rules:
  * Only the LATEST attempt per task counts (list order is chronological;
    a task appears at most once per wave, so per-task order is preserved
    even though order within a parallel wave is arbitrary).
  * Only tasks currently SUCCEEDED count. A task escalated to NEEDS_HUMAN
    or still RETRYING has code we don't stand behind.
  * `stale_paths` are files some attempt wrote that the effective set no
    longer contains (e.g. a retry that stopped emitting a helper file).
    The sandbox removes them so the tested workspace matches what ships.
"""

from __future__ import annotations

from app.graph.state import EditorResult, FileDiff, MigrationTask, TaskStatus


def latest_results_by_task(editor_results: list[EditorResult]) -> dict[str, EditorResult]:
    latest: dict[str, EditorResult] = {}
    for result in editor_results:
        latest[result.task_id] = result
    return latest


def effective_results(editor_results: list[EditorResult], tasks: list[MigrationTask]) -> list[EditorResult]:
    succeeded = {t.id for t in tasks if t.status == TaskStatus.SUCCEEDED}
    latest = latest_results_by_task(editor_results)
    return [result for task_id, result in latest.items() if task_id in succeeded]


def effective_diffs(editor_results: list[EditorResult], tasks: list[MigrationTask]) -> list[FileDiff]:
    """Flattened, de-duplicated by path (last one wins), in stable first-seen order."""
    by_path: dict[str, FileDiff] = {}
    for result in effective_results(editor_results, tasks):
        for diff in result.diffs:
            by_path[diff.path] = diff
    return list(by_path.values())


def stale_paths(editor_results: list[EditorResult], tasks: list[MigrationTask]) -> list[str]:
    ever_written = {diff.path for result in editor_results for diff in result.diffs}
    effective = {diff.path for diff in effective_diffs(editor_results, tasks)}
    return sorted(ever_written - effective)
