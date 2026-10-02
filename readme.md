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

## Graph flow

`build_graph()` in `app/graph/build.py` assembles the compiled LangGraph
`StateGraph` (verified against `langgraph==1.2.12`):

```
START -> comprehension -> synthesis --route_to_editors--> editor (parallel Send fanout)
                                    \__(nothing ready)__>    |
                                                             v
                                                    budget_reconcile
                                                      |           \
                                      (more tasks now ready)     (nothing left ready)
                                                      |                 |
                                                      |                 v
                                                      |            sandbox_test
                                                      |             |          \
                                                      |          (passed)   (failed / error)
                                                      |             |            |
                                                      |             v            v
                                                      |         aggregate    error_analysis
                                                      |             |          |        \
                                                      |             v     (something    (nothing retryable /
                                                      |         pr_publish  retryable)    round cap hit)
                                                      |             |          |              |
                                                      |             v          |              v
                                                      |            END         |             END
                                                      \_______________________/
                                                  (rejoins the SAME editor fanout)
```

`route_to_editors` is the conditional router on three edges (synthesis,
`budget_reconcile`, `error_analysis`), so multi-wave dependency scheduling
and the retry loop are one mechanism, not two.

## Status: Step 7 of 10 — FastAPI + Celery async API layer

114 tests pass. Every external collaborator (LLM, Docker, GitHub, Postgres)
is exercised through a fake, so the suite needs no API key, daemon, or token.

### Steps 1–2: Scaffolding, state schema, comprehension

- Project structure (`app/core`, `app/graph`, `app/adapters`, `app/db`)
- Full Pydantic state schema (`app/graph/state/`) — IR, tasks, budget,
  enums, and the top-level LangGraph state with correct reducers for
  the parallel editor fanout
- Postgres checkpointer setup (`app/db/checkpointer.py`)
- **Adapter interface** (`app/adapters/base.py`) — the plug point that
  keeps the pipeline stack-agnostic. A `SourceAdapter` knows how to
  discover and prompt-ify files for ONE stack; the Comprehension node
  has zero stack-specific knowledge
- **Spring Boot source adapter** (`app/adapters/springboot/`) — file
  discovery scoped to a package/module (so a run stays bounded to what
  the caller actually wants migrated, not the whole monorepo), plus
  extraction prompts covering the common Spring annotations
  (`@RestController`, `@Service`, `@Repository`, `@Entity`, etc.)
- **LLM extraction client** (`app/core/llm.py`) — structured-output
  calls with token-based cost estimation feeding the budget guard
- **Comprehension node** (`app/graph/nodes/comprehension.py`) —
  discovers files → extracts per file → resolves cross-file references
  (e.g. an endpoint's `calls_services: ["OrderService"]`) into real
  `depends_on_unit_ids` → emits a `ComprehensionResult`. Handles
  per-file extraction failures without aborting the whole run, and
  stops precisely at the budget ceiling rather than overspending.

**Bug found and fixed:** the initial scope filter matched file names
against the scope description literally, so
`"migrate the OrderController module only"` matched `OrderController.java`
but silently excluded `OrderService.java` and `OrderRepository.java` —
the very files a Spring "module" migration needs. Fixed by matching
anchor files first, then pulling in every file in the same package
directory (Spring modules are conventionally one-package-per-feature).
The test suite caught this immediately rather than it surfacing later
as a mysteriously incomplete migration.

### Step 3: Sandbox execution layer

- **Target test adapter interface** (`app/adapters/target_test_base.py`)
  — mirrors `SourceAdapter` but for the *target* side: how to install
  deps and run tests for one target stack. Separate interface from
  `SourceAdapter` on purpose (a stack can be a valid migration target
  before anyone's written a reader for it as a source, or vice versa).
- **FastAPI target adapter** (`app/adapters/fastapi_target/`) — pytest
  install/run commands as argv lists (never a shell string — closes
  off shell-injection entirely) and exit-code-aware output parsing
  (pytest's exit code 5 "no tests collected" is treated as
  `TestOutcome.ERROR`, not `FAILED` — it almost always means the
  migrated code has an import error, a different failure mode than a
  normal assertion failure).
- **Workspace diff application** (`app/sandbox/workspace.py`) — writes
  `FileDiff` objects (LLM output) to disk with path-traversal
  protection. Every diff path is treated as untrusted input; an
  absolute path or a `"../../etc/cron.d/evil"` style path is refused,
  and a batch containing even one unsafe path writes nothing at all
  (no partially-applied migrations).
- **Docker sandbox runner** (`app/sandbox/docker_runner.py`) — runs
  install (network enabled) and test (network **disabled**) as two
  separate, non-root, capability-dropped, read-only-rootfs, resource-
  limited containers. The container-config building
  (`_build_container_kwargs`) is a pure function with zero Docker SDK
  dependency, specifically so the actual security posture is unit-
  testable without a Docker daemon.
- **Sandbox test node** (`app/graph/nodes/sandbox_test.py`) — applies
  all current effective diffs to the target workspace and runs the full
  suite; routes to `AGGREGATING` on pass, `ERROR_ANALYSIS` on anything
  else.

### Step 4: Synthesis, editors, and the first real compiled graph

- **`app/graph/build.py`** — the first step with a REAL compiled graph,
  not just isolated node functions.
- **Target synthesis adapter** (`app/adapters/target_synthesis_base.py`,
  `app/adapters/fastapi_target/synthesis_adapter.py`) — third member of
  the adapter family (alongside `SourceAdapter` and `TargetTestAdapter`):
  decides where each IR unit's migrated code lives and what FastAPI
  idioms an editor should follow, per `SemanticUnitKind`.
- **Synthesis node** (`app/graph/nodes/synthesis.py`) — fully
  deterministic, zero LLM cost. Groups IR units that share a target
  file into one `MigrationTask` (so two parallel editors can never race
  to write the same file — e.g. all of `OrderController`'s endpoints
  collapse into one `app/routers/order.py` task) and translates the
  IR's unit-level dependency graph into a task-level one.
- **Editor node** (`app/graph/nodes/editor.py`) — the actual code-
  generation LLM call, invoked once per parallel `Send()` branch.
  Handles retries (`RETRYING` → `NEEDS_HUMAN` after `max_retries`) and
  a soft budget pre-check.
- **Dependency-aware fanout routing** (`app/graph/routing.py`) —
  dispatches every task whose dependencies have already `SUCCEEDED` as
  one parallel `Send()` batch. (Originally single-wave; Step 5 made it
  multi-wave.)
- **A real concurrency-safety design decision**: editor branches do
  NOT mutate `BudgetState` in place. Under `Send()`-based fanout,
  LangGraph may serialize/copy state per branch, so an in-place
  mutation on one branch's copy can be silently lost when another
  branch's return value overwrites it. Instead each branch emits its
  own cost as an append-only delta (`budget_deltas`), and a dedicated
  single-writer node (`app/graph/nodes/budget_reconcile.py`) folds only
  the not-yet-consumed entries into the authoritative budget —
  provably correct across multiple waves via
  `tests/test_budget_reconcile_node.py`'s double-counting test.
- **`tests/test_graph_integration.py`** — runs the **actual compiled
  graph** end-to-end (real `Send()`, real reducers, real conditional
  edges) with fake LLM/sandbox collaborators. No amount of isolated
  node-function testing can catch a wiring bug at the graph level, only
  running the graph can.

### Step 5: Error analysis and the retry loop

The system becomes self-correcting, not just a linear pipeline.

- **Multi-wave scheduling.** `budget_reconcile`'s edge to `sandbox_test`
  is a *conditional* edge that reuses `route_to_editors` — after each
  editor wave, the graph checks whether finishing it made more tasks
  ready (a dependency just succeeded) and dispatches again. A 3-level
  chain (repository → service → router) fully completes in one graph
  run, proven by `test_full_pipeline_comprehension_through_sandbox`.
- **Error-analysis node** (`app/graph/nodes/error_analysis.py`) — on a
  failed sandbox run, an LLM call sees the failing tests, the raw
  output, and every task whose code is actually present in that run
  (nothing else is a valid suspect), and returns — reusing the
  `ErrorAnalysis` schema from Step 1's state design directly as its
  structured-output target — a root cause and a retry/escalate
  decision per implicated task. A task it can't confidently attribute
  is left untouched rather than guessed at.
- **The retry loop reuses the multi-wave dispatch mechanism, not a
  second copy of it.** A task error-analysis marks `RETRYING` becomes
  eligible for the exact same dispatch path a fresh dependency-driven
  task uses — proven by
  `test_retry_loop_recovers_after_error_analysis_marks_tasks_retryable`,
  where marking all 3 tasks retryable correctly forces a full
  from-scratch re-migration in dependency order before the sandbox
  re-runs and passes.
- **Two independent, both-tested bounds prevent an infinite loop:**
  per-task `retry_count` vs `max_retries`, and a global `retry_round`
  vs `max_retry_rounds`. `test_round_cap_terminates_a_perpetually_failing_run`
  runs a sandbox that fails forever with an analyzer that always claims
  "retry me," and asserts the graph still terminates at exactly the
  configured round cap.
- **A real bug caught by reasoning through the wiring before writing
  any test**: if error-analysis escalates everything to `NEEDS_HUMAN`,
  the graph goes straight to `END` and `budget_reconcile` never runs
  again — so the error-analysis LLM call's own cost would have silently
  vanished from the final budget. Fixed by having `error_analysis`
  (never part of a parallel fanout) reconcile its own cost directly.
- A "no diffs to test" result now flows through to `error_analysis`
  (which recognizes it has nothing to analyze and escalates to
  `NEEDS_HUMAN`) instead of dead-ending at `sandbox_test`'s own phase.

### Step 6: Diff aggregation and GitHub PR generation

Includes three integrity fixes to earlier steps that only surfaced once
"what exactly ships in the PR?" was asked in earnest:

- **Fix 1 — superseded attempts.** `editor_results` is append-only, so
  after a retry it holds multiple attempts for the same task. Both the
  sandbox and the PR now derive their file set from ONE shared module,
  `app/graph/diffs.py` (`effective_diffs` / `effective_results` /
  `stale_paths`) — latest attempt per `SUCCEEDED` task, nothing else.
  `stale_paths` also feeds `remove_paths` (`app/sandbox/workspace.py`)
  so a retry that stops emitting a helper file doesn't leave a stale
  copy the tests still see.
- **Fix 2 — undeclared output paths.** The editor node enforces a file
  contract: generated output must be exactly the task's declared
  `target_files`, no more, no fewer (`_contract_violation` in
  `app/graph/nodes/editor.py`). Without this, nothing stopped an editor
  from quietly rewriting the target repo's own test files to make a
  suite "pass." A violation is treated like a failed generation —
  retried with the violation itself as feedback. This caught a real bug
  in the test fixtures: the fake code generator was parsing target file
  paths out of the *entire* prompt instead of just the file-list
  section, and picked up a spurious path from `::`-delimited IR unit ids.
- **Fix 3 — the retry loop now uses its own analysis.**
  `ErrorAnalysis.suggested_fix_instructions` was computed but never fed
  anywhere. Added `MigrationTask.retry_feedback`, set by `error_analysis`
  and read by the editor's retry prompt — a retry is now a directed fix
  attempt, not a second blind guess.

New in this step:

- **`app/graph/nodes/aggregate.py`** — builds the PR title/body/branch
  name deterministically (no LLM call — a structured, factual summary
  doesn't need one, and a template avoids a flaky source of variance in
  a step whose output is about to enter a real repository) from the
  effective diffs, and re-validates every path with
  `app/vcs/paths.py`'s `publishable_path_problem` as defense-in-depth.
  `.github/` is explicitly refused — a generated CI workflow in a PR is
  a privilege-escalation vector (workflows can run with repository
  secrets). A violation here is a hard stop, never a silent drop.
- **`app/vcs/github_publisher.py`** — real PyGithub-backed publisher:
  builds a git tree/commit/branch/PR the standard way (there's no
  "upload several files" REST endpoint). Lazy-imports `PyGithub`, same
  pattern as every other external SDK in this project; constructing
  `GitHubPRPublisher()` never touches the network or requires a token —
  that only happens inside `publish()`.
- **Approval gating** (`app/graph/nodes/pr_publish.py`) — honors
  `require_human_approval_before_pr`: stops at `AWAITING_APPROVAL` with
  the draft ready to inspect, without calling the publisher. The real
  pause/resume (`interrupt()`) is Step 9's job; for now this is a clean
  terminal state, and `publish_approved_pr` is a standalone function so
  the API layer reuses the exact same publish path rather than a second
  implementation that could drift from it.
- **A bug caught by reasoning through the wiring**: the fixed
  `aggregate -> pr_publish` edge always runs `pr_publish`, but
  `aggregate` can fail (empty diffs, unpublishable path) without
  producing a `pr_draft`. Fixed with an explicit guard that leaves
  `aggregate`'s own `FAILED` state untouched.
- **Graph-level tests for the publish path** in
  `tests/test_graph_integration.py`: one proves a PR is published (via
  a fake publisher) with exactly the effective diffs and nothing else,
  the other proves the approval gate stops the run — publisher never
  called — while still leaving a draft behind.

**Bug found and fixed while wiring Step 6 into the compiled graph:**
`aggregate` and `pr_publish` were not registered in `build_graph()`, a
passing sandbox run still routed to `END`, and `GraphState` had no
`pr_draft` field. The last one is the subtle failure: LangGraph silently
discards any key a node returns that isn't declared in the state schema,
so `aggregate`'s draft vanished with no error and `pr_publish` saw
nothing. Node-level unit tests can't see this — only running the
compiled graph can. **Rule of thumb: every key a node returns must be
declared in `GraphState`.**

### Step 7: FastAPI + Celery async API layer

- **`app/services/repo_prep.py`** — turns request URLs into the two
  local paths every node since Step 2 assumed already existed
  (`local_checkout_path`/`target_workspace_path`). Real git clones via
  GitPython (lazy-imported), or a scaffolded fresh FastAPI skeleton
  when no target repo is given. **Tested with a real network clone**
  against a tiny public repo (`tests/test_repo_prep.py`) — deliberately
  not faked, since the whole point of this module is "did we get the
  real clone semantics right," which a mock can't verify.
- **No separate "runs" database.** Status and approval read/write
  through the compiled graph's own `aget_state`/`aupdate_state` against
  its existing checkpointer — LangGraph's checkpoint store already IS
  the durable, queryable record of every run; a second store would just
  be a second source of truth that could drift from the first.
- **`app/api/app.py`** — `create_app()` factory with three routes:
  `POST /migrations` (submit, hands off via a `TaskSubmitter` Protocol —
  Celery in production, a recording fake in tests), `GET /migrations/{id}`
  (404 if the thread never existed), `POST /migrations/{id}/approve`
  (404 if unknown, 409 if not `AWAITING_APPROVAL` or no draft, otherwise
  calls `publish_approved_pr` — reused, not reimplemented — then
  `aupdate_state` to persist the result).
- **`app/worker/tasks.py`** — the real Celery task: repo prep, real
  graph, real Postgres checkpointer, one run per LangGraph thread
  (`thread_id == run_id`).
- **A genuine architectural bug caught before it could break in
  production**: the first draft of `app/main.py` built the Postgres
  checkpointer's connection pool at module-import time, on a throwaway
  event loop — before uvicorn's real loop exists. Async DB drivers bind
  connections to the loop that created them, so the pool would have
  been silently unusable under uvicorn. Fixed by moving construction
  into a FastAPI `lifespan` handler and having routes read
  `request.app.state.graph`.
- **A second real bug**: `app/db/checkpointer.py` imported
  `AsyncPostgresSaver` at module level, so anything importing it
  required the Postgres extra installed. Fixed with a lazy import, like
  every other external SDK in this project.
- **Stated gap, not glossed over**: this environment has no Postgres to
  test against, so `AsyncPostgresSaver`'s round-trip of our custom
  Pydantic model fields (`BudgetState`, `MigrationTask`, etc.) through
  real serialization is unverified — tests use LangGraph's in-memory
  `MemorySaver`. `app/api/app.py`'s status mapper defends against the
  round-trip producing plain dicts instead of the original objects, but
  this should be smoke-tested against a real Postgres instance before
  relying on it in production.

## Roadmap

1. ~~Scaffolding + state schema~~ ✅
2. ~~Comprehension node (repo analysis → IR)~~ ✅
3. ~~Sandbox execution layer (Docker isolation)~~ ✅
4. ~~Synthesis + editor nodes with parallel `Send()` fanout~~ ✅
5. ~~Test runner + error-analyzer retry loop~~ ✅
6. ~~Diff aggregation + GitHub PR generation~~ ✅
7. ~~FastAPI + Celery async API layer~~ ✅
8. Budget guard + observability wiring
9. Human-in-the-loop approval via `interrupt()`
10. Deployment (Dockerfile, docker-compose, CI)

## Running the tests

```bash
pip install -r requirements.txt
PYTHONPATH=. pytest tests/ -v
```

On Windows PowerShell:

```powershell
$env:PYTHONPATH = "."
pytest tests/ -v
```

`app/core/llm.py`'s real `ExtractionClient`,
`app/sandbox/docker_runner.py`'s real `DockerSandboxRunner`,
`app/vcs/github_publisher.py`'s real `GitHubPRPublisher`, and
`app/db/checkpointer.py`'s `get_checkpointer()` all import their
respective SDKs (`langchain_anthropic`, `docker`, `github`,
`langgraph.checkpoint.postgres`) lazily, inside method/function bodies
— so the full test suite runs with zero API key, zero Docker daemon,
zero GitHub token, and zero Postgres instance. The one exception is
`tests/test_repo_prep.py`, which does real (tiny, shallow) `git clone`
calls against a public GitHub repo — everything else is either a pure
function or exercised through a fake.

## Why a TypedDict for `GraphState` and not pure Pydantic?

LangGraph's state-merging machinery expects `Annotated[T, reducer]`
field annotations on a `TypedDict`. We get Pydantic's validation where
it matters (every individual field is a Pydantic model) without
fighting LangGraph's merge semantics. Full rationale is in the
docstring at the top of `app/graph/state/graph_state.py`.