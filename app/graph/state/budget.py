"""
Budget guard state. Tracked as a plain field on the graph state (not a
separate node's private state) so every node can cheaply check remaining
budget before doing expensive work, and the central guard (the routing
gate in app/graph/routing.py plus the `budget_abort` node) can halt the
run in one place.

Three independent ceilings, any one of which exhausts the budget:
  * cost            (`max_usd`)
  * LLM call count  (`max_llm_calls`) — catches runaway loops that are cheap per call
  * wall clock      (`max_wall_clock_seconds`) — measured from `started_at`
"""

from __future__ import annotations

import time

from pydantic import BaseModel, Field


class BudgetState(BaseModel):
    max_usd: float = Field(description="Hard ceiling for this migration run")
    spent_usd: float = 0.0
    max_llm_calls: int = Field(default=500, description="Secondary guard independent of cost, catches runaway loops")
    llm_calls_made: int = 0
    max_wall_clock_seconds: int = Field(default=3600, description="1 hour default; overridable per run")
    started_at: float = Field(
        default_factory=time.time,
        description="Epoch seconds when the run's budget was created; the wall-clock ceiling counts from here",
    )
    max_call_cost_usd: float = Field(
        default=0.0, description="Largest single LLM call cost observed so far; used to size parallel waves"
    )
    default_call_reserve_usd: float = Field(
        default=0.25, description="Assumed cost of one call until a real one has been observed"
    )

    def remaining_usd(self) -> float:
        return max(0.0, self.max_usd - self.spent_usd)

    def elapsed_seconds(self, now: float | None = None) -> float:
        return (time.time() if now is None else now) - self.started_at

    def wall_clock_exceeded(self, now: float | None = None) -> bool:
        return self.elapsed_seconds(now) >= self.max_wall_clock_seconds

    def exhausted_reason(self, now: float | None = None) -> str | None:
        """Human-readable reason the budget is exhausted, or None if it isn't."""
        if self.spent_usd >= self.max_usd:
            return f"cost ceiling reached (${self.spent_usd:.2f} of ${self.max_usd:.2f})"
        if self.llm_calls_made >= self.max_llm_calls:
            return f"LLM call cap reached ({self.llm_calls_made} of {self.max_llm_calls})"
        if self.wall_clock_exceeded(now):
            return f"wall-clock limit reached ({self.elapsed_seconds(now):.0f}s of {self.max_wall_clock_seconds}s)"
        return None

    def is_exhausted(self, now: float | None = None) -> bool:
        return self.exhausted_reason(now) is not None

    def record_call(self, cost_usd: float, *, calls: int = 1, track_max: bool = True) -> None:
        self.spent_usd += cost_usd
        self.llm_calls_made += calls
        if track_max and calls > 0:
            self.max_call_cost_usd = max(self.max_call_cost_usd, cost_usd / calls)

    def affordable_calls(self, wanted: int) -> int:
        """
        How many parallel LLM calls it is safe to launch right now. Reserves
        the largest observed single-call cost per in-flight branch, so the
        wave's combined spend can't blow far past the ceiling. Always allows
        at least one call while the budget isn't exhausted, so a run can
        always make progress; the worst-case overshoot is therefore one call.
        """
        if wanted <= 0 or self.is_exhausted():
            return 0
        reserve = self.max_call_cost_usd or self.default_call_reserve_usd  # real observation beats the guess
        # tiny epsilon: float division like 0.4 // 0.1 yields 3.0, one call short
        return max(1, min(wanted, int((self.remaining_usd() + 1e-9) // reserve)))
