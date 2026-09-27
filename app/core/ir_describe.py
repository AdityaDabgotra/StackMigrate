"""
Renders a SemanticUnit's payload as plain text for an LLM prompt.
Kept as one shared function (rather than duplicated inline in the
synthesis and editor node prompts) so the two prompts never drift out
of sync in how they describe the same unit.
"""

from __future__ import annotations

from app.graph.state import (
    DataModelUnit,
    EndpointUnit,
    MiddlewareUnit,
    RepositoryUnit,
    SemanticUnit,
    ServiceUnit,
)


def describe_unit(unit: SemanticUnit) -> str:
    payload = unit.payload

    if isinstance(payload, EndpointUnit):
        params = (
            ", ".join(f"{p.name} ({p.type_description}, {p.source_location})" for p in payload.parameters)
            or "none"
        )
        return (
            f"[{unit.id}] {payload.http_method} {payload.path} — {payload.description}\n"
            f"    Parameters: {params}\n"
            f"    Request body: {payload.request_body_model or 'none'}; "
            f"Response: {payload.response_model or 'none'}\n"
            f"    Auth: {payload.auth_requirement or 'none'}\n"
            f"    Logic: {payload.business_logic_summary}"
        )

    if isinstance(payload, ServiceUnit):
        return (
            f"[{unit.id}] Service '{payload.name}' — {payload.description}\n"
            f"    Depends on: {', '.join(payload.depends_on) or 'none'}\n"
            f"    Logic: {payload.business_logic_summary}"
        )

    if isinstance(payload, RepositoryUnit):
        ops = "; ".join(payload.operations) or "none"
        return (
            f"[{unit.id}] Repository '{payload.name}' for '{payload.target_data_model}' — {payload.description}\n"
            f"    Operations: {ops}"
        )

    if isinstance(payload, DataModelUnit):
        fields = (
            "; ".join(
                f"{f.name}: {f.type_description}" + (f" [{', '.join(f.constraints)}]" if f.constraints else "")
                for f in payload.fields
            )
            or "none"
        )
        rels = "; ".join(payload.relationships) or "none"
        return (
            f"[{unit.id}] Data model '{payload.name}' — {payload.description}\n"
            f"    Fields: {fields}\n"
            f"    Relationships: {rels}"
        )

    if isinstance(payload, MiddlewareUnit):
        return (
            f"[{unit.id}] Middleware '{payload.name}' (scope: {payload.trigger_scope}) — "
            f"{payload.behavior_summary}"
        )

    return f"[{unit.id}] {payload}"
