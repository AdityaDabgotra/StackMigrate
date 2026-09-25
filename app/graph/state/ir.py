"""
Intermediate Representation (IR) schema.

This is the load-bearing design decision of the whole project: the
Comprehension phase reads source code (any stack) and produces THIS,
not target-stack code. The Synthesis phase reads ONLY this, never the
original source files. That separation is what lets one pipeline
support N-to-N stack pairs instead of hardcoding pairwise rules.

Every field here must be expressible in plain language, independent of
any programming language's syntax. If you find yourself wanting to add
a field like `spring_annotation` or `decorator_name`, it belongs in
`stack_specific_hints` (a free-form dict), not as a first-class field.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.graph.state.enums import SemanticUnitKind


class ParameterSpec(BaseModel):
    name: str
    type_description: str = Field(
        description="Plain-language type, e.g. 'string, required', 'integer, optional, defaults to 10'"
    )
    source_location: str = Field(
        description="Where this comes from conceptually: 'path param', 'query param', 'request body field', 'header'"
    )


class FieldSpec(BaseModel):
    """One field of a data model (entity, DTO, schema)."""

    name: str
    type_description: str
    constraints: list[str] = Field(
        default_factory=list,
        description="e.g. ['not null', 'unique', 'max length 255', 'foreign key -> User.id']",
    )


class DataModelUnit(BaseModel):
    name: str
    description: str
    fields: list[FieldSpec]
    relationships: list[str] = Field(
        default_factory=list,
        description="Plain-language relationships, e.g. 'has many Order', 'belongs to Account'",
    )


class EndpointUnit(BaseModel):
    name: str
    http_method: str
    path: str
    description: str
    parameters: list[ParameterSpec] = Field(default_factory=list)
    request_body_model: str | None = Field(
        default=None, description="Name of the DataModelUnit this endpoint consumes, if any"
    )
    response_model: str | None = None
    auth_requirement: str | None = Field(
        default=None, description="Plain-language, e.g. 'requires JWT with role ADMIN'"
    )
    calls_services: list[str] = Field(
        default_factory=list, description="Names of ServiceUnits this endpoint invokes"
    )
    business_logic_summary: str = Field(
        description="What this endpoint actually does, in plain language — this is what Synthesis relies on most"
    )


class ServiceUnit(BaseModel):
    name: str
    description: str
    depends_on: list[str] = Field(
        default_factory=list, description="Names of RepositoryUnits / other ServiceUnits it depends on"
    )
    business_logic_summary: str


class RepositoryUnit(BaseModel):
    name: str
    description: str
    target_data_model: str
    operations: list[str] = Field(
        description="Plain-language data operations, e.g. 'find by email', 'save', 'delete by id and account_id'"
    )


class MiddlewareUnit(BaseModel):
    name: str
    description: str
    trigger_scope: str = Field(description="e.g. 'all /api/** routes', 'only endpoints marked @AdminOnly'")
    behavior_summary: str


class SemanticUnit(BaseModel):
    """
    A polymorphic wrapper so the IR can hold a heterogeneous, ordered list
    of units while keeping each unit's schema strongly typed.
    """

    id: str = Field(description="Stable unique id, e.g. 'endpoint::create_order'")
    kind: SemanticUnitKind
    source_files: list[str] = Field(description="Original file path(s) this unit was extracted from")
    depends_on_unit_ids: list[str] = Field(
        default_factory=list,
        description="Other SemanticUnit ids this must be migrated after (dependency ordering for the fanout)",
    )
    payload: EndpointUnit | ServiceUnit | DataModelUnit | RepositoryUnit | MiddlewareUnit | dict
    stack_specific_hints: dict = Field(
        default_factory=dict,
        description="Escape hatch for source-stack idioms that don't generalize, e.g. "
        "{'spring_scope': 'singleton', 'uses_aspectj': True}. Synthesis may ignore these.",
    )


class ComprehensionResult(BaseModel):
    """Full output of the Comprehension phase — this is what gets persisted and handed to Synthesis."""

    source_stack: str = Field(description="e.g. 'spring-boot-3.2'")
    repo_root: str
    units: list[SemanticUnit]
    global_notes: list[str] = Field(
        default_factory=list,
        description="Cross-cutting concerns that don't fit one unit, e.g. 'uses global exception handler "
        "that wraps all errors in {error, code} envelope'",
    )
    unresolved_ambiguities: list[str] = Field(
        default_factory=list,
        description="Things Comprehension couldn't confidently determine — surfaced to human review, "
        "not silently guessed at by Synthesis",
    )
