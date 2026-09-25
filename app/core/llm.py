"""
LLM extraction client for the Comprehension phase.

Kept as a thin, swappable class (not free functions) specifically so
tests can inject a fake implementation and assert on node orchestration
logic without ever calling the real Anthropic API. See
`tests/test_comprehension_node.py` for the fake used in Step 2's tests.

Cost estimation note: the per-token rates below are approximate and
env-overridable. They exist to keep the BudgetState guard meaningful
during development, NOT as a source of truth for billing — verify
current rates at https://anthropic.com/pricing before relying on this
for real spend enforcement, and prefer the exact cost reported by your
billing/usage API in production if/when available.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from pydantic import BaseModel, Field

from app.graph.state import (
    DataModelUnit,
    EndpointUnit,
    MiddlewareUnit,
    RepositoryUnit,
    SemanticUnitKind,
    ServiceUnit,
)

DEFAULT_INPUT_COST_PER_MTOK = float(os.environ.get("ANTHROPIC_INPUT_COST_PER_MTOK", "3.00"))
DEFAULT_OUTPUT_COST_PER_MTOK = float(os.environ.get("ANTHROPIC_OUTPUT_COST_PER_MTOK", "15.00"))
DEFAULT_MODEL = os.environ.get("COMPREHENSION_MODEL", "claude-sonnet-4-6")


class RawExtractedUnit(BaseModel):
    """
    What we ask the LLM to produce per file, before the node assigns a
    stable `id` and resolves cross-file references into
    `depends_on_unit_ids`. Structurally this is `SemanticUnit` minus the
    fields only the node can fill in.
    """

    kind: SemanticUnitKind
    name: str = Field(description="The primary identifier for this unit, e.g. the class or method name")
    payload: EndpointUnit | ServiceUnit | DataModelUnit | RepositoryUnit | MiddlewareUnit
    stack_specific_hints: dict = Field(default_factory=dict)


class FileExtractionResult(BaseModel):
    units: list[RawExtractedUnit] = Field(
        description="Every semantic unit found in this file. A single controller file with 3 "
        "HTTP-mapped methods produces 3 EndpointUnit entries here, not 1."
    )
    ambiguities: list[str] = Field(
        default_factory=list,
        description="Anything in this file you could not confidently map — surfaced to human review",
    )


@dataclass
class ExtractionResponse:
    result: FileExtractionResult
    cost_usd: float
    input_tokens: int
    output_tokens: int


class ExtractionClient:
    """
    Real implementation, wired to Anthropic via langchain. Not exercised
    in Step 2's tests (no network access / API key in this environment)
    — this class is what production wiring uses; tests use a fake that
    satisfies the same interface (see `extract`'s signature).
    """

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        self._model_name = model
        self._structured_model = None  # lazily constructed, see _get_model

    def _get_model(self):
        if self._structured_model is None:
            # Imported lazily so this module can be imported (e.g. by tests
            # that only use the fake client) without langchain_anthropic
            # installed / an API key present.
            from langchain_anthropic import ChatAnthropic

            base = ChatAnthropic(model=self._model_name, timeout=120, max_retries=2)
            self._structured_model = base.with_structured_output(FileExtractionResult, include_raw=True)
        return self._structured_model

    async def extract(self, prompt: str) -> ExtractionResponse:
        model = self._get_model()
        raw = await model.ainvoke(prompt)
        parsed: FileExtractionResult = raw["parsed"]
        usage = getattr(raw["raw"], "usage_metadata", None) or {}
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        cost = (
            input_tokens / 1_000_000 * DEFAULT_INPUT_COST_PER_MTOK
            + output_tokens / 1_000_000 * DEFAULT_OUTPUT_COST_PER_MTOK
        )
        return ExtractionResponse(
            result=parsed, cost_usd=cost, input_tokens=input_tokens, output_tokens=output_tokens
        )
