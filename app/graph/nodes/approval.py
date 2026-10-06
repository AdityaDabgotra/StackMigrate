"""
Human approval: two small nodes around one LangGraph `interrupt()`.

    aggregate -> request_approval -> approval_gate -> pr_publish   (approved)
                                          |
                                          +-------> END           (rejected)

Why two nodes. `interrupt()` pauses the graph and checkpoints it, but the
interrupting node's own state update is NOT committed — and on resume the
node re-runs from its first line. So:

  * `request_approval` is pure bookkeeping: it sets phase AWAITING_APPROVAL
    and finishes, so the paused run's checkpoint reads correctly for the
    status endpoint (and anything else reading state).
  * `approval_gate` does nothing before `interrupt()` that must not happen
    twice, so its re-execution on resume is harmless.

Resume value is `{"approved": bool, "feedback": str | None}`. Anything that
is not exactly `approved is True` — a malformed payload, a missing key, a
string "true" — is treated as a REJECTION: this gate fails closed.

The interrupt payload is what a reviewer needs to decide and must be
JSON-serializable (the Postgres checkpointer stores it).
"""

from __future__ import annotations

from langgraph.types import interrupt

from app.graph.state import MigrationPhase


def _get(obj, name, default=None):
    """Pydantic model or plain dict (a possible checkpointer round-trip shape)."""
    if obj is None:
        return default
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def build_request_approval_node():
    async def request_approval_node(state: dict) -> dict:
        return {"phase": MigrationPhase.AWAITING_APPROVAL}

    return request_approval_node


def build_approval_gate_node():
    async def approval_gate_node(state: dict) -> dict:
        draft = state["pr_draft"]
        decision = interrupt(
            {
                "kind": "pr_approval",
                "run_id": (state.get("config") or {}).get("run_id"),
                "title": _get(draft, "title"),
                "body": _get(draft, "body"),
                "branch_name": _get(draft, "branch_name"),
                "files": list(state.get("aggregated_diff_paths") or []),
            }
        )

        approved = isinstance(decision, dict) and decision.get("approved") is True
        feedback = decision.get("feedback") if isinstance(decision, dict) else None

        if approved:
            return {"approval_status": "approved", "human_feedback": feedback, "phase": MigrationPhase.PR_GENERATION}

        note = f": {feedback}" if feedback else "."
        return {
            "approval_status": "rejected",
            "human_feedback": feedback,
            "phase": MigrationPhase.REJECTED,
            "error_log": [f"PR rejected by reviewer{note}"],
        }

    return approval_gate_node
