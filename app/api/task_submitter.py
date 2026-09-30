"""
Abstracts "kick off a migration run in the background" away from Celery
specifically, same reasoning as every other Protocol in this project:
the route handler's job is to validate the request and hand it off —
whether that's Celery, a different queue, or (in tests) nothing at all
that actually runs, is not the route's concern.
"""

from __future__ import annotations

from typing import Protocol

from app.api.schemas import MigrationRequest


class TaskSubmitter(Protocol):
    def submit(self, *, run_id: str, request: MigrationRequest) -> None: ...


class CeleryTaskSubmitter:
    def submit(self, *, run_id: str, request: MigrationRequest) -> None:
        from app.worker.celery_app import celery_app

        celery_app.send_task("stackmigrate.run_migration", args=[run_id, request.model_dump()])
