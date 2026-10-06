"""
Production ASGI entrypoint: `uvicorn app.main:app`.

Every other module in `app/api` and `app/graph` is a factory that takes
its collaborators as arguments — this is the one place that actually
constructs the real ones and wires them together. Kept deliberately
thin: if this file starts accumulating logic beyond "construct the real
things and call create_app", that logic belongs in one of the modules
it's importing from instead.

Uses FastAPI's `lifespan` (see create_app's docstring for the full
reasoning) rather than building the checkpointer at import time: the
checkpointer's connection pool must be opened inside uvicorn's own
event loop, not a throwaway one that exists only during module import,
or async DB connections created there would be unusable once uvicorn's
real loop takes over.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.app import create_app
from app.api.task_submitter import CeleryTaskSubmitter
from app.core.observability import configure_logging
from app.db.checkpointer import get_checkpointer
from app.graph.build import build_graph


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    configure_logging()
    async with get_checkpointer() as checkpointer:
        await checkpointer.setup()  # idempotent; cheap enough to call on every boot
        fastapi_app.state.graph = build_graph(checkpointer=checkpointer)
        yield
    # checkpointer's own __aexit__ closes its connection pool cleanly here


app = create_app(
    task_submitter=CeleryTaskSubmitter(),
    lifespan=lifespan,
)
