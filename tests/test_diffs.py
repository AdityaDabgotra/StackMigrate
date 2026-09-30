from __future__ import annotations

from app.graph.diffs import effective_diffs, effective_results, stale_paths
from app.graph.state import EditorResult, FileDiff, MigrationTask, TargetFileSpec, TaskStatus


def _task(id_: str, status: TaskStatus, path: str) -> MigrationTask:
    return MigrationTask(
        id=id_,
        source_unit_ids=["x"],
        target_files=[TargetFileSpec(path=path, purpose="x")],
        synthesis_instructions="x",
        status=status,
    )


def test_only_latest_attempt_per_task_counts():
    tasks = [_task("t1", TaskStatus.SUCCEEDED, "a.py")]
    results = [
        EditorResult(task_id="t1", diffs=[FileDiff(path="a.py", content="attempt 1")]),
        EditorResult(task_id="t1", diffs=[FileDiff(path="a.py", content="attempt 2 (retry)")]),
    ]
    diffs = effective_diffs(results, tasks)
    assert len(diffs) == 1
    assert diffs[0].content == "attempt 2 (retry)"


def test_only_succeeded_tasks_contribute():
    tasks = [
        _task("t1", TaskStatus.SUCCEEDED, "a.py"),
        _task("t2", TaskStatus.NEEDS_HUMAN, "b.py"),
    ]
    results = [
        EditorResult(task_id="t1", diffs=[FileDiff(path="a.py", content="x")]),
        EditorResult(task_id="t2", diffs=[FileDiff(path="b.py", content="y")]),
    ]
    diffs = effective_diffs(results, tasks)
    assert [d.path for d in diffs] == ["a.py"]


def test_stale_paths_finds_files_a_retry_stopped_emitting():
    tasks = [_task("t1", TaskStatus.SUCCEEDED, "a.py")]
    results = [
        EditorResult(task_id="t1", diffs=[FileDiff(path="a.py", content="v1"), FileDiff(path="helper.py", content="h1")]),
        # retry only re-emits a.py, not helper.py
        EditorResult(task_id="t1", diffs=[FileDiff(path="a.py", content="v2")]),
    ]
    assert stale_paths(results, tasks) == ["helper.py"]


def test_stale_paths_empty_when_nothing_superseded():
    tasks = [_task("t1", TaskStatus.SUCCEEDED, "a.py")]
    results = [EditorResult(task_id="t1", diffs=[FileDiff(path="a.py", content="v1")])]
    assert stale_paths(results, tasks) == []


def test_effective_results_excludes_tasks_not_in_editor_results_at_all():
    tasks = [_task("t1", TaskStatus.SUCCEEDED, "a.py")]
    assert effective_results([], tasks) == []
    assert effective_diffs([], tasks) == []
