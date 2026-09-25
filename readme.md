# stackmigrate

An agentic system that migrates a scoped module of a codebase from one
tech stack to another (e.g. a Spring Boot `@RestController` module to
FastAPI) — built on LangGraph, designed to be stack-agnostic by
architecture rather than hardcoded per stack-pair.

## Core design decision

The pipeline is split into two phases that never share code paths:

- **Comprehension**: reads source code (any stack) and produces an
  `IntermediateRepresentation` — a plain-language, framework-agnostic
  description of what the code does (endpoints, data models, services,
  auth requirements, business logic).
- **Synthesis**: reads *only* the IR, never the original source, and
  produces target-stack code. This is what makes the system N-to-N
  instead of needing one bespoke pipeline per stack pair — see
  `app/graph/state/ir.py` for the full schema and rationale.

## Status: Step 1 of 10 — scaffolding + state schema

**Done:**
- Project structure (`app/core`, `app/graph`, `app/adapters`, `app/db`)
- Full Pydantic state schema:
  - `app/graph/state/ir.py` — the intermediate representation
  - `app/graph/state/tasks.py` — migration tasks, editor/test/error results
  - `app/graph/state/budget.py` — cost/call/time budget guard
  - `app/graph/state/enums.py` — shared vocabulary across all nodes
  - `app/graph/state/graph_state.py` — the top-level LangGraph state,
    with correct reducers for the parallel editor fanout (this is the
    part most LangGraph tutorials get wrong — see the docstring there)
- Postgres checkpointer setup (`app/db/checkpointer.py`) — production
  persistence, not the SQLite dev default, so runs survive Celery
  worker restarts
- 6 passing tests proving the schema and reducers behave correctly
  (`tests/test_state_schema.py`)

**Not yet built (upcoming steps):**
1. ~~Scaffolding + state schema~~ ✅
2. Comprehension node (repo analysis → IR)
3. Sandbox execution layer (Docker isolation)
4. Synthesis + editor nodes with parallel `Send()` fanout
5. Test runner + error analyzer retry loop
6. Diff aggregation + GitHub PR generation
7. FastAPI + Celery async API layer
8. Budget guard + observability wiring
9. Human-in-the-loop approval via `interrupt()`
10. Deployment (Dockerfile, docker-compose, CI)

## Running the current tests

```bash
pip install -r requirements.txt
PYTHONPATH=. pytest tests/ -v
```

## Why a TypedDict for `GraphState` and not pure Pydantic?

LangGraph's state-merging machinery expects `Annotated[T, reducer]`
field annotations on a `TypedDict`. We get Pydantic's validation where
it matters (every individual field is a Pydantic model) without
fighting LangGraph's merge semantics. Full rationale is in the
docstring at the top of `app/graph/state/graph_state.py`.
