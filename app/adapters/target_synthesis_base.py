"""
Target synthesis adapter interface.

Third member of the adapter family, alongside `SourceAdapter` (reads
source code -> IR) and `TargetTestAdapter` (installs/runs tests for a
target stack). This one answers: "given an IR unit, where does its
migrated code live, and what target-stack idioms should the editor
follow?" It never touches source code or test execution — kept
separate so each interface has one reason to change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.graph.state import SemanticUnit, SemanticUnitKind


class TargetSynthesisAdapter(ABC):
    stack_key: str
    display_name: str

    @abstractmethod
    def suggest_target_path(self, unit: SemanticUnit) -> str:
        """
        Conventional file path (relative to the target workspace root)
        for this unit's migrated code, e.g. an ENDPOINT unit named
        'createOrder' extracted from OrderController -> 'app/routers/orders.py'.
        """

    @abstractmethod
    def convention_notes(self, kind: SemanticUnitKind) -> str:
        """
        Target-stack idioms for one SemanticUnitKind, written for an LLM
        editor prompt — e.g. for FastAPI ENDPOINT units: 'use APIRouter,
        async def handlers, Pydantic request/response models'. Generic
        (not per-unit) since these conventions are the same for every
        unit of a given kind.
        """
