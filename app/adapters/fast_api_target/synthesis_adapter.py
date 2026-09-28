"""
FastAPI target synthesis adapter.

Path convention: standard FastAPI project layout —
  app/routers/<module>.py    — endpoints (APIRouter per feature module)
  app/services/<name>.py     — business logic
  app/repositories/<name>.py — data access
  app/schemas/<name>.py      — Pydantic request/response/data models
  app/middleware/<name>.py   — ASGI middleware / dependencies used across routes
  app/core/<name>.py         — app-wide config/wiring
  app/utils/<name>.py        — framework-agnostic helpers
"""

from __future__ import annotations

from app.adapters.target_synthesis_base import TargetSynthesisAdapter
from app.core.text import slugify
from app.graph.state import SemanticUnit, SemanticUnitKind

_KIND_TO_DIR = {
    SemanticUnitKind.ENDPOINT: "app/routers",
    SemanticUnitKind.SERVICE: "app/services",
    SemanticUnitKind.REPOSITORY: "app/repositories",
    SemanticUnitKind.DATA_MODEL: "app/schemas",
    SemanticUnitKind.MIDDLEWARE: "app/middleware",
    SemanticUnitKind.CONFIG: "app/core",
    SemanticUnitKind.UTILITY: "app/utils",
}

_CONVENTION_NOTES = {
    SemanticUnitKind.ENDPOINT: """\
- Define an `APIRouter` (or add to the module's existing one) with a sensible `prefix`.
- Handlers are `async def`, take request data via Pydantic models (body) or typed
  function parameters (path/query params), and return a Pydantic response model or
  a plain serializable type.
- Auth requirements become a `Depends(...)` dependency (e.g. `Depends(require_role("ADMIN"))`),
  never inline logic in the handler body.
- Raise `HTTPException` for error responses; don't invent a custom error envelope
  unless the IR's `global_notes` says the source app has one worth preserving.""",
    SemanticUnitKind.SERVICE: """\
- A plain async (or sync, if no I/O) function or a small class with injected
  dependencies passed as constructor/function arguments — not a framework-managed
  singleton the way Spring `@Service` beans are. FastAPI has no DI container of its
  own; dependencies are threaded through via `Depends(...)` at the router layer.""",
    SemanticUnitKind.REPOSITORY: """\
- A function-per-operation module (or small class) using SQLAlchemy (async session)
  for data access. Each `operations` entry in the IR becomes one function, named
  descriptively (e.g. "find by email and active true" -> `find_by_email_and_active`).""",
    SemanticUnitKind.DATA_MODEL: """\
- A Pydantic `BaseModel` subclass. Use `Field(...)` for constraints (max length,
  required/optional) rather than custom validators where a Field constraint suffices.
  Relationships described in plain language become typed references to other schema
  classes (e.g. "has many OrderItem" -> `items: list[OrderItemSchema]`).""",
    SemanticUnitKind.MIDDLEWARE: """\
- Prefer a FastAPI dependency (`Depends(...)`) over raw ASGI middleware when the
  behavior is scoped to specific routes (this covers most Spring interceptor/filter
  cases). Only use actual ASGI middleware for behavior that must apply globally
  before routing even happens.""",
    SemanticUnitKind.CONFIG: """\
- A `pydantic-settings` `BaseSettings` subclass for environment-driven config,
  or a small module of constructor functions for wiring (FastAPI has no DI
  container/config-class equivalent to Spring `@Configuration`).""",
    SemanticUnitKind.UTILITY: """\
- A plain module-level function. No framework coupling expected or desired here.""",
}


class FastAPITargetSynthesisAdapter(TargetSynthesisAdapter):
    stack_key = "fastapi"
    display_name = "FastAPI"

    def suggest_target_path(self, unit: SemanticUnit) -> str:
        directory = _KIND_TO_DIR.get(unit.kind, "app/misc")
        name = getattr(unit.payload, "name", None) or unit.id
        module_name = self._module_name_for(unit, name)
        return f"{directory}/{module_name}.py"

    def convention_notes(self, kind: SemanticUnitKind) -> str:
        return _CONVENTION_NOTES.get(kind, "- Follow standard FastAPI project conventions.")

    @staticmethod
    def _module_name_for(unit: SemanticUnit, name: str) -> str:
        """
        Endpoints get grouped by their source controller (so 5 methods on
        OrderController all target app/routers/orders.py, not 5 separate
        files) — everything else gets one file per unit, which is the
        right granularity for services/repos/schemas.
        """
        if unit.kind == SemanticUnitKind.ENDPOINT and unit.source_files:
            # e.g. ".../OrderController.java" -> "order" (strip Controller suffix, pluralize-free slug)
            stem = unit.source_files[0].rsplit("/", 1)[-1].rsplit(".", 1)[0]
            stem = stem[: -len("Controller")] if stem.endswith("Controller") else stem
            return slugify(stem) or "router"
        return slugify(name)
