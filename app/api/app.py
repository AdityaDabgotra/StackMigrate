"""
FastAPI app factory.

Deliberately a factory (`create_app(...)`), not a module-level `app`
instance — same reasoning as `build_graph()`: production wiring
(`app/worker`/deployment entrypoint) passes real collaborators, tests
pass fakes, and there is exactly one code path either way rather than
test-only conditionals sprinkled through route handlers.

Status/approve read and write state via the compiled graph's own
`aget_state`/`aupdate_state` — no separate "runs" table. The graph's
checkpointer (Postgres in production, see app/db/checkpointer.py) is
already the durable, queryable record of every run; duplicating it
would just be a second source of truth that could drift from the first.

Known gap, stated plainly rather than glossed over: this project's test
suite exercises status/approve against LangGraph's in-memory
`MemorySaver` (see tests/test_api.py), which round-trips Python objects
directly with no serialization step. The real deployment uses the
Postgres checkpointer, which DOES serialize state between processes —
and this environment has no Postgres available to verify that
`BudgetState`/`MigrationTask`/etc. round-trip through it exactly as
constructed. `_field()` below defends against the round-trip producing
plain dicts instead of the original Pydantic objects (a real, if
unlikely, possibility depending on LangGraph's serializer), but this
should be smoke-tested against a real Postgres instance before
production use — see the README's noted gap for this step.
"""

from __future__ import annotations

import uuid
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request

from app.api.schemas import (
    ApproveResponse,
    MigrationRequest,
    MigrationStatusResponse,
    MigrationSubmitResponse,
    PRDraftSummary,
    TaskSummary,
)
from app.api.task_submitter import TaskSubmitter
from app.graph.nodes.pr_publish import publish_approved_pr
from app.graph.state import MigrationPhase
from app.vcs.interface import PRPublisher


def _field(obj, name: str, default=None):
    """Works whether `obj` is a Pydantic model (production, MemorySaver-backed
    tests) or a plain dict (a possible Postgres round-trip shape — see module
    docstring). Never raises AttributeError/KeyError on a missing field."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _enum_value(x, default=None):
    if x is None:
        return default
    return getattr(x, "value", x)  # already a plain string if this is a dict-shaped round-trip


def _state_to_status_response(run_id: str, values: dict) -> MigrationStatusResponse:
    tasks = values.get("tasks", []) or []
    task_summaries = [
        TaskSummary(
            id=_field(t, "id"),
            status=_enum_value(_field(t, "status")),
            target_files=[_field(tf, "path") for tf in (_field(t, "target_files") or [])],
            retry_count=_field(t, "retry_count", 0),
        )
        for t in tasks
    ]

    budget = values.get("budget")
    pr_draft_obj = values.get("pr_draft")
    pr_draft = (
        PRDraftSummary(title=_field(pr_draft_obj, "title"), branch_name=_field(pr_draft_obj, "branch_name"))
        if pr_draft_obj
        else None
    )

    full_suite = values.get("full_suite_result")

    return MigrationStatusResponse(
        run_id=run_id,
        phase=_enum_value(values.get("phase")),
        tasks=task_summaries,
        budget_spent_usd=_field(budget, "spent_usd"),
        budget_max_usd=_field(budget, "max_usd"),
        full_suite_outcome=_enum_value(_field(full_suite, "outcome")),
        pr_draft=pr_draft,
        pr_url=values.get("pr_url"),
        error_log_tail=(values.get("error_log") or [])[-20:],
    )


def create_app(
    *,
    graph: Any = None,
    task_submitter: TaskSubmitter,
    pr_publisher: PRPublisher,
    lifespan: Callable[[FastAPI], Any] | None = None,
) -> FastAPI:
    """
    Two ways to supply the compiled graph, and exactly one is required:

    - `graph=...`: used directly, set on `app.state` immediately. This is
      the synchronous, test-friendly path — every test in this project
      builds a graph up front (often MemorySaver-backed) and passes it
      straight in.
    - `lifespan=...`: a FastAPI lifespan context manager that sets
      `app.state.graph` itself during startup. This is the production
      path (see app/main.py) — it exists because the real checkpointer's
      connection pool must be opened inside the ASGI server's own event
      loop, not at module-import time on a throwaway loop (async DB
      drivers bind connections to the loop that created them; building
      the pool before uvicorn's loop exists would silently break the
      first real request). Routes below read `request.app.state.graph`
      at request time rather than closing over a fixed variable
      specifically so either path works identically.

    Passing neither means no route can ever find a graph; passing both
    means `graph` wins for the initial value and `lifespan` may still
    override it at startup — supported but not a case any caller here
    actually needs.
    """
    app = FastAPI(title="stackmigrate", lifespan=lifespan)
    if graph is not None:
        app.state.graph = graph

    @app.post("/migrations", response_model=MigrationSubmitResponse, status_code=202)
    async def submit_migration(payload: MigrationRequest) -> MigrationSubmitResponse:
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        task_submitter.submit(run_id=run_id, request=payload)
        return MigrationSubmitResponse(run_id=run_id, status="queued")

    @app.get("/migrations/{run_id}", response_model=MigrationStatusResponse)
    async def get_migration_status(run_id: str, request: Request) -> MigrationStatusResponse:
        snapshot = await request.app.state.graph.aget_state({"configurable": {"thread_id": run_id}})
        if not snapshot.values:
            raise HTTPException(status_code=404, detail=f"No migration run found with id '{run_id}'")
        return _state_to_status_response(run_id, snapshot.values)

    @app.post("/migrations/{run_id}/approve", response_model=ApproveResponse)
    async def approve_migration(run_id: str, request: Request) -> ApproveResponse:
        graph = request.app.state.graph
        thread_config = {"configurable": {"thread_id": run_id}}
        snapshot = await graph.aget_state(thread_config)
        if not snapshot.values:
            raise HTTPException(status_code=404, detail=f"No migration run found with id '{run_id}'")

        phase = _enum_value(snapshot.values.get("phase"))
        if phase != MigrationPhase.AWAITING_APPROVAL.value:
            raise HTTPException(
                status_code=409,
                detail=f"Run '{run_id}' is not awaiting approval (current phase: {phase!r}).",
            )
        if not snapshot.values.get("pr_draft"):
            raise HTTPException(status_code=409, detail=f"Run '{run_id}' has no PR draft to publish.")

        result = await publish_approved_pr(snapshot.values, pr_publisher)
        await graph.aupdate_state(thread_config, result)

        return ApproveResponse(
            run_id=run_id,
            phase=_enum_value(result.get("phase")),
            pr_url=result.get("pr_url"),
        )

    return app
