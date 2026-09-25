"""
Budget guard state. Tracked as a plain field on the graph state (not a
separate node's private state) so every node can cheaply check remaining
budget before doing expensive work, and the Budget Guard node can halt
the run centrally.
"""

from pydantic import BaseModel, Field


class BudgetState(BaseModel):
    max_usd: float = Field(description="Hard ceiling for this migration run")
    spent_usd: float = 0.0
    max_llm_calls: int = Field(default=500, description="Secondary guard independent of cost, catches runaway loops")
    llm_calls_made: int = 0
    max_wall_clock_seconds: int = Field(default=3600, description="1 hour default; overridable per run")

    def remaining_usd(self) -> float:
        return max(0.0, self.max_usd - self.spent_usd)

    def is_exhausted(self) -> bool:
        return self.spent_usd >= self.max_usd or self.llm_calls_made >= self.max_llm_calls

    def record_call(self, cost_usd: float) -> None:
        self.spent_usd += cost_usd
        self.llm_calls_made += 1
