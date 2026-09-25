"""
Adapter interface.

This is the plug point that keeps the pipeline stack-agnostic: the
Comprehension node knows how to run an extraction loop, but it has ZERO
knowledge of Java, Spring annotations, or Maven file layout. All of
that lives in a `SourceAdapter` implementation. Adding a new source
stack (PHP, Angular, ...) means writing one adapter class, not touching
`comprehension.py`.

A `TargetAdapter` (used in Step 4 — Synthesis/editors) is the mirror
image: it knows how to write idiomatic target-stack code, not how to
read source code. Keeping them as separate interfaces (rather than one
"StackAdapter" doing both directions) is deliberate — a stack can be a
valid source without a target adapter existing yet, and vice versa.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.graph.state import SemanticUnitKind


@dataclass(frozen=True)
class DiscoveredFile:
    """One source file the adapter has identified as relevant to migrate."""

    path: str                          # absolute path on disk
    relative_path: str                 # path relative to repo_root, used in IR.source_files
    likely_kind: SemanticUnitKind | None  # adapter's best guess before LLM extraction; None if unsure
    content: str


class SourceAdapter(ABC):
    """
    Implemented once per source stack. Registered in
    `app.adapters.registry.SOURCE_ADAPTERS` under a string key
    (e.g. "springboot") that matches `MigrationRunConfig.source_stack`.
    """

    #: adapter registry key — must match MigrationRunConfig.source_stack
    stack_key: str

    #: human-readable, used in prompts and logs
    display_name: str

    @abstractmethod
    def discover_files(self, repo_root: str, scope_description: str) -> list[DiscoveredFile]:
        """
        Walk repo_root and return the files relevant to the migration
        scope. MUST respect `scope_description` (e.g. "migrate the
        OrderController module only") to keep runs bounded — this is
        the strangler-fig scope guardrail from the architecture, not
        an optional nicety.
        """

    @abstractmethod
    def build_extraction_prompt(self, file: DiscoveredFile) -> str:
        """
        Return the source-stack-specific instructions (few-shot
        exemplars, annotation-mapping hints) to prepend to the generic
        extraction prompt for this one file. E.g. for Spring Boot this
        explains what @RestController / @Service / @Entity map to in
        IR terms. The LLM call itself and the generic part of the
        prompt live in the node, not here.
        """

    @abstractmethod
    def guess_unit_name(self, file: DiscoveredFile) -> str:
        """
        Cheap, non-LLM best-effort name for a file's primary unit
        (e.g. the public class name in a Java file). Used to build the
        name->id lookup table for the dependency-resolution pass —
        does not need to be perfect, just a good prior.
        """
