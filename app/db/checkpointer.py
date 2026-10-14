"""
Postgres-backed checkpointer.

Why Postgres and not the SQLite/in-memory checkpointer LangGraph ships
with for demos: migration runs are long (minutes to tens of minutes),
run inside Celery workers that can be killed/rescheduled, and need to
survive process restarts without losing agent state. SQLite's
single-writer lock also becomes a real bottleneck once the Celery
worker pool is >1 process, which it will be in production.

Usage:
    async with get_checkpointer() as checkpointer:
        graph = build_graph().compile(checkpointer=checkpointer)
        result = await graph.ainvoke(initial_state, config={"configurable": {"thread_id": run_id}})

`thread_id` == our `run_id` — one migration run is one LangGraph thread.
This is what lets us resume a run after an `interrupt()` (human
approval) or after a worker crash: reconnect with the same thread_id
and LangGraph replays from the last checkpoint.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator

if TYPE_CHECKING:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver


def _get_conn_string() -> str:
    conn_string = os.environ.get("MIGRATION_DB_URL")
    if not conn_string:
        raise RuntimeError(
            "MIGRATION_DB_URL is not set. Expected a Postgres connection string, e.g. "
            "postgresql://user:pass@host:5432/stackmigrate"
        )
    return conn_string


def state_serde():
    """
    Serializer that explicitly allows every model/enum in `app.graph.state` to be
    reconstructed from a checkpoint. Without this, LangGraph logs "Deserializing
    unregistered type ... will be blocked in a future version" for each of them: a
    routine dependency upgrade would then stop in-flight runs (including ones paused
    for human approval) from loading. Derived from the package's `__all__`, so a new
    state model is covered automatically.
    """
    from enum import Enum

    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from pydantic import BaseModel

    import app.graph.state as state_pkg

    allowed = [
        (obj.__module__, obj.__qualname__)
        for obj in (getattr(state_pkg, name) for name in state_pkg.__all__)
        if isinstance(obj, type) and issubclass(obj, (BaseModel, Enum))
    ]
    return JsonPlusSerializer(allowed_msgpack_modules=allowed)


@asynccontextmanager
async def get_checkpointer() -> AsyncIterator[AsyncPostgresSaver]:
    """
    Yields a ready-to-use AsyncPostgresSaver. Call `await checkpointer.setup()`
    once per fresh database (idempotent — creates the checkpoint tables if
    they don't exist) via the `ensure_schema` function below, not on every
    request.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # lazy: only needed when a real DB is used

    conn_string = _get_conn_string()
    async with AsyncPostgresSaver.from_conn_string(conn_string, serde=state_serde()) as checkpointer:
        yield checkpointer


async def ensure_schema() -> None:
    """
    Run once at deploy time (e.g. in a release/migration step, not on
    every app boot) to create LangGraph's checkpoint tables. Safe to
    call repeatedly — it's idempotent.
    """
    async with get_checkpointer() as checkpointer:
        await checkpointer.setup()
