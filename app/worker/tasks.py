"""
The actual background job: repo prep -> build the real graph -> run it
under the run's own thread_id so its progress is checkpointed to
Postgres and pollable via the API's status endpoint throughout, not just
at the end.

`run_migration` is the sync Celery entrypoint (Celery tasks are plain
functions); `_run_migration_async` is where the real work happens,
driven via `asyncio.run` since everything below it — the graph, the
checkpointer, repo prep — is async. Split out specifically so it's
unit-testable with `asyncio.run(_run_migration_async(...))` directly,
without needing a running Celery worker/broker.
"""

from __future__ import annotations

from app.api.schemas import MigrationRequest
from app.core.observability import run_config
from app.db.checkpointer import get_checkpointer
from app.graph.build import build_graph
from app.graph.state import BudgetState
from app.services.repo_prep import GitRepoPreparer, RepoPreparer
from app.worker.celery_app import celery_app


async def _run_migration_async(
    run_id: str,
    request: MigrationRequest,
    *,
    repo_preparer: RepoPreparer | None = None,
    checkpointer_cm=None,
    **graph_kwargs,
):
    """
    `checkpointer_cm`, when given, replaces `get_checkpointer()` — an
    async context manager factory (called with no args). Exists so tests
    can substitute an in-memory `MemorySaver` instead of the real
    Postgres-backed checkpointer, which this environment has no instance
    of to test against (see app/api/app.py's module docstring for the
    same gap, stated once there in more detail rather than repeated).

    `**graph_kwargs` passes straight through to `build_graph` (extractor,
    code_generator, error_analyzer, sandbox_runner, pr_publisher) — real
    implementations by default, fakes in tests, exactly like every other
    layer of this project.
    """
    repo_preparer = repo_preparer or GitRepoPreparer()
    checkpointer_cm = checkpointer_cm or get_checkpointer

    local_checkout_path = await repo_preparer.prepare_source(
        repo_url=request.source_repo_url, ref=request.source_ref, run_id=run_id
    )
    target_workspace_path = await repo_preparer.prepare_target(
        repo_url=request.target_repo_url, target_stack=request.target_stack, run_id=run_id
    )

    config = {
        "run_id": run_id,
        "source_repo_url": request.source_repo_url,
        "source_ref": request.source_ref,
        "source_stack": request.source_stack,
        "target_stack": request.target_stack,
        "scope_description": request.scope_description,
        "github_target_repo": request.github_target_repo,
        "require_human_approval_before_pr": request.require_human_approval_before_pr,
        "local_checkout_path": local_checkout_path,
        "target_workspace_path": target_workspace_path,
    }

    async with checkpointer_cm() as checkpointer:
        if hasattr(checkpointer, "setup"):  # the real AsyncPostgresSaver needs this; MemorySaver (tests) doesn't
            await checkpointer.setup()
        graph = build_graph(checkpointer=checkpointer, **graph_kwargs)
        initial_state = {
            "config": config,
            "budget": BudgetState(
                max_usd=request.max_usd,
                max_llm_calls=request.max_llm_calls,
                max_wall_clock_seconds=request.max_wall_clock_seconds,
            ),
            "max_retry_rounds": request.max_retry_rounds,
        }
        return await graph.ainvoke(
            initial_state,
            config=run_config(
                run_id, source_stack=request.source_stack, target_stack=request.target_stack, max_usd=request.max_usd
            ),
        )


@celery_app.task(name="stackmigrate.run_migration")
def run_migration(run_id: str, request_payload: dict) -> None:
    import asyncio

    from app.core.observability import configure_logging

    configure_logging()

    request = MigrationRequest(**request_payload)
    asyncio.run(_run_migration_async(run_id, request))
