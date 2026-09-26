"""
Comprehension node.

Responsibilities (deliberately does NOT include any target-stack
knowledge — see app/graph/state/ir.py for why that separation matters):

  1. Ask the registered SourceAdapter which files are in scope.
  2. Extract a FileExtractionResult per file via the LLM client.
  3. Assign stable SemanticUnit ids and resolve cross-file name
     references (e.g. an EndpointUnit's `calls_services: ["OrderService"]`)
     into `depends_on_unit_ids`.
  4. Emit a ComprehensionResult and advance the graph phase.

Concurrency: extraction runs sequentially, one file at a time. This is
a deliberate simplicity-over-throughput choice for this step — it makes
budget accounting exact (we can check `budget.is_exhausted()` before
every single call and stop precisely mid-run) which matters more than
speed for a scoped, module-sized migration. Bounded concurrency (e.g. a
semaphore of 4-5) is a reasonable future optimization once the budget
guard is proven correct — tracked as a TODO, not silently done now.
"""

from __future__ import annotations

from typing import Protocol

from app.adapters.base import DiscoveredFile, SourceAdapter
from app.adapters.registry import get_source_adapter
from app.core.llm import ExtractionResponse, FileExtractionResult, RawExtractedUnit
from app.core.text import find_capitalized_tokens, slugify
from app.graph.state import (
    ComprehensionResult,
    DataModelUnit,
    EndpointUnit,
    MigrationPhase,
    RepositoryUnit,
    SemanticUnit,
    ServiceUnit,
)


class Extractor(Protocol):
    """
    Structural type both `ExtractionClient` (real) and any test fake
    must satisfy. Using a Protocol rather than requiring a subclass
    keeps test fakes decoupled from the real client's constructor/deps.
    """

    async def extract(self, prompt: str) -> ExtractionResponse: ...


class _PendingUnit:
    """Internal bookkeeping between the per-file extraction pass and the reference-resolution pass."""

    __slots__ = ("raw", "source_relative_path", "assigned_id")

    def __init__(self, raw: RawExtractedUnit, source_relative_path: str, assigned_id: str) -> None:
        self.raw = raw
        self.source_relative_path = source_relative_path
        self.assigned_id = assigned_id


def _assign_id(kind_value: str, name: str, used_ids: set[str]) -> str:
    base = f"{kind_value}::{slugify(name)}"
    candidate = base
    suffix = 2
    while candidate in used_ids:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used_ids.add(candidate)
    return candidate


def _referenced_names(payload) -> list[str]:
    """
    Pulls the plain-text names a unit's payload references, so the
    resolution pass can look them up in the name->id table. Each
    payload type references other units differently, hence the
    isinstance dispatch rather than one shared field on the base schema
    (a shared field would force irrelevant emptiness onto every kind).
    """
    if isinstance(payload, EndpointUnit):
        names = list(payload.calls_services)
        if payload.request_body_model:
            names.append(payload.request_body_model)
        if payload.response_model:
            names.append(payload.response_model)
        return names
    if isinstance(payload, ServiceUnit):
        return list(payload.depends_on)
    if isinstance(payload, RepositoryUnit):
        return [payload.target_data_model]
    if isinstance(payload, DataModelUnit):
        tokens: list[str] = []
        for rel in payload.relationships:
            tokens.extend(find_capitalized_tokens(rel))
        return tokens
    return []  # MiddlewareUnit and anything else: no structured references to resolve


async def run_comprehension(
    *,
    repo_root: str,
    scope_description: str,
    source_stack: str,
    remaining_budget_usd: float,
    extractor: Extractor,
    adapter: SourceAdapter | None = None,
) -> tuple[ComprehensionResult, float, list[str]]:
    """
    Pure-ish orchestration function, separated from the LangGraph node
    wrapper below so it can be unit-tested without constructing a full
    GraphState. Returns (result, total_cost_usd, error_log_entries).
    """
    adapter = adapter or get_source_adapter(source_stack)
    files: list[DiscoveredFile] = adapter.discover_files(repo_root, scope_description)

    error_log: list[str] = []
    ambiguities: list[str] = []
    pending_units: list[_PendingUnit] = []
    used_ids: set[str] = set()
    total_cost = 0.0

    if not files:
        ambiguities.append(
            f"No source files discovered under scope '{scope_description}' for stack '{source_stack}'. "
            "Check that local_checkout_path is correct and the scope description matches real file/class names."
        )

    for file in files:
        if total_cost >= remaining_budget_usd:
            error_log.append(
                f"Budget exhausted before analyzing '{file.relative_path}' "
                f"({len(files) - files.index(file)} file(s) skipped)."
            )
            ambiguities.append(f"'{file.relative_path}' was not analyzed: budget exhausted.")
            break

        prompt = adapter.build_extraction_prompt(file)
        try:
            response = await extractor.extract(prompt)
        except Exception as exc:  # noqa: BLE001 — deliberately broad: any single-file extraction
            # failure must not abort the whole run; log and continue with the rest.
            error_log.append(f"Extraction failed for '{file.relative_path}': {exc!r}")
            ambiguities.append(f"'{file.relative_path}' could not be analyzed due to an extraction error.")
            continue

        total_cost += response.cost_usd
        result: FileExtractionResult = response.result
        ambiguities.extend(f"{file.relative_path}: {a}" for a in result.ambiguities)

        for raw_unit in result.units:
            unit_id = _assign_id(raw_unit.kind.value, raw_unit.name, used_ids)
            pending_units.append(_PendingUnit(raw_unit, file.relative_path, unit_id))

    # Build the name -> id lookup for cross-file reference resolution.
    # A name can legitimately map to multiple ids (e.g. overloaded/ambiguous
    # naming) — we keep the first match and note ambiguity rather than guess.
    name_to_id: dict[str, str] = {}
    for pu in pending_units:
        key = pu.raw.name.lower()
        if key not in name_to_id:
            name_to_id[key] = pu.assigned_id

    units: list[SemanticUnit] = []
    for pu in pending_units:
        referenced_names = _referenced_names(pu.raw.payload)
        depends_on_ids: list[str] = []
        for ref_name in referenced_names:
            resolved = name_to_id.get(ref_name.lower())
            if resolved and resolved != pu.assigned_id:
                depends_on_ids.append(resolved)
            elif ref_name:
                ambiguities.append(
                    f"'{pu.assigned_id}' references '{ref_name}', which was not found among "
                    "analyzed units (likely outside the migration scope, or a built-in/library type)."
                )

        units.append(
            SemanticUnit(
                id=pu.assigned_id,
                kind=pu.raw.kind,
                source_files=[pu.source_relative_path],
                depends_on_unit_ids=depends_on_ids,
                payload=pu.raw.payload,
                stack_specific_hints=pu.raw.stack_specific_hints,
            )
        )

    result = ComprehensionResult(
        source_stack=source_stack,
        repo_root=repo_root,
        units=units,
        global_notes=[],
        unresolved_ambiguities=ambiguities,
    )
    return result, total_cost, error_log


def build_comprehension_node(extractor: Extractor):
    """
    Factory returning the actual LangGraph node callable, closing over
    the extractor implementation. Production wiring (Step 4, when the
    full graph is assembled) calls this with a real `ExtractionClient`;
    tests call it with a fake.
    """

    async def comprehension_node(state: dict) -> dict:
        config = state["config"]
        budget = state["budget"]

        if budget.is_exhausted():
            return {
                "phase": MigrationPhase.ABORTED_BUDGET,
                "error_log": ["Comprehension skipped: budget already exhausted before this node ran."],
            }

        result, cost, node_errors = await run_comprehension(
            repo_root=config["local_checkout_path"],
            scope_description=config["scope_description"],
            source_stack=config["source_stack"],
            remaining_budget_usd=budget.remaining_usd(),
            extractor=extractor,
        )

        budget.record_call(cost_usd=cost)
        next_phase = MigrationPhase.SYNTHESIS_PLANNING if result.units else MigrationPhase.FAILED

        return {
            "comprehension": result,
            "phase": next_phase,
            "budget": budget,
            "error_log": node_errors,
        }

    return comprehension_node
