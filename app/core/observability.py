"""
Observability for a migration run: structured per-node logs + tracing metadata.

Two layers, deliberately independent:

1. Structured JSON logs (always on). `observed_node` wraps every graph node
   and emits one `node_finished` (or `node_failed`) line carrying run_id,
   node, task_id, duration, phase transition and a budget snapshot. This is
   what you grep when a run misbehaves, with no external service needed.

2. LangSmith tracing (opt-in, zero code in the nodes). LangGraph and
   langchain-anthropic trace automatically once LANGSMITH_TRACING=true,
   LANGSMITH_API_KEY and LANGSMITH_PROJECT are set in the environment.
   `run_config` just attaches the thread id, tags and metadata so a trace
   can be found by run_id, stack pair, or budget.
"""

from __future__ import annotations

import functools
import json
import logging
import time
from typing import Any, Awaitable, Callable

from langgraph.errors import GraphBubbleUp

logger = logging.getLogger("stackmigrate")

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """One JSON object per line; anything passed via `extra=` becomes a top-level field."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        payload.update({k: v for k, v in record.__dict__.items() if k not in _RESERVED})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Idempotent. Call once at process start (API lifespan, Celery worker)."""
    if any(isinstance(h.formatter, JsonFormatter) for h in logger.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False


def _enum_value(x: Any) -> Any:
    return getattr(x, "value", x)


def _budget_snapshot(state: dict) -> dict[str, Any]:
    budget = state.get("budget")
    if budget is None:
        return {}
    return {
        "budget_spent_usd": round(budget.spent_usd, 4),
        "budget_remaining_usd": round(budget.remaining_usd(), 4),
        "llm_calls_made": budget.llm_calls_made,
    }


def observed_node(name: str, fn: Callable[[dict], Awaitable[dict]]) -> Callable[[dict], Awaitable[dict]]:
    """Wrap an async graph node with timing + structured logging. Never alters its result."""

    @functools.wraps(fn)
    async def wrapper(state: dict) -> dict:
        run_id = (state.get("config") or {}).get("run_id")
        task = state.get("current_task")  # only present inside a Send() editor branch
        base = {"run_id": run_id, "node": name, "task_id": getattr(task, "id", None)}
        started = time.monotonic()
        try:
            update = await fn(state)
        except GraphBubbleUp:
            # interrupt() (and other control-flow signals) travel as exceptions;
            # that's a pause, not a failure. Log it as such and let it propagate.
            logger.info(
                "node_interrupted", extra={**base, "duration_ms": round((time.monotonic() - started) * 1000)}
            )
            raise
        except Exception:
            logger.exception(
                "node_failed", extra={**base, "duration_ms": round((time.monotonic() - started) * 1000)}
            )
            raise
        update = update or {}
        logger.info(
            "node_finished",
            extra={
                **base,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "phase": _enum_value(update.get("phase", state.get("phase"))),
                "cost_delta_usd": round(sum(update.get("budget_deltas", [])), 4),
                **_budget_snapshot({**state, **update}),
            },
        )
        return update

    return wrapper


def run_config(run_id: str, *, source_stack: str, target_stack: str, max_usd: float) -> dict[str, Any]:
    """The `config=` for graph.ainvoke: checkpoint thread id + searchable trace metadata."""
    return {
        "configurable": {"thread_id": run_id},
        "run_name": f"stackmigrate:{run_id}",
        "tags": ["stackmigrate", f"{source_stack}->{target_stack}"],
        "metadata": {
            "run_id": run_id,
            "source_stack": source_stack,
            "target_stack": target_stack,
            "max_usd": max_usd,
        },
    }
