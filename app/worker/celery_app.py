"""Celery app instance. Broker/backend read from REDIS_URL (see .env.example) —
same Redis instance serves both roles, which is fine at this project's scale."""

from __future__ import annotations

import os

from celery import Celery

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# `include` is what makes a worker started with `celery -A app.worker.celery_app worker`
# register `stackmigrate.run_migration`. The API only ever *sends* the task by name
# (app/api/task_submitter.py), so without this the worker would reject every job
# as an unregistered task.
celery_app = Celery("stackmigrate", broker=REDIS_URL, backend=REDIS_URL, include=["app.worker.tasks"])
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    # Migrations run for minutes, not seconds — a worker restart mid-run
    # should not silently drop the task.
    task_acks_late=True,
    worker_prefetch_multiplier=1,
)
