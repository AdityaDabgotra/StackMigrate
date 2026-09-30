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

## Status: Step 7 of 10 — FastAPI + Celery async API layer

**Done:**
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
- **12 passing tests** across both files, using a fake LLM extractor
  (no real API calls) to prove the orchestration logic is correct —
  this caught and fixed a real bug during development (see below)

**Bug found and fixed during this step:** the initial scope filter
matched file names against the scope description literally, so
`"migrate the OrderController module only"` matched `OrderController.java`
but silently excluded `OrderService.java` and `OrderRepository.java` —
the very files a Spring "module" migration needs. Fixed by matching
anchor files first, then pulling in every file in the same package
directory (Spring modules are conventionally one-package-per-feature).
The test suite caught this immediately rather than it surfacing later
as a mysteriously incomplete migration.

**Also done in this step:**
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
  normal assertion failure, and Step 5's retry logic will need to tell
  them apart).
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
  all current `editor_results` diffs to the target workspace and runs
  the full suite; routes to `AGGREGATING` on pass, `ERROR_ANALYSIS` on
  anything else.
- **23 new tests** (35 total) covering diff safety, pytest output
  parsing, container security config, and node wiring — all via fakes,
  no Docker daemon or network needed to run the suite.

**Also done in this step — and this is the first step with a REAL
compiled graph, not just isolated node functions:**
- **`app/graph/build.py`** — `build_graph()` assembles an actual
  LangGraph `StateGraph` (verified against `langgraph==1.2.12`, the
  version installed and exercised while building this):
  `comprehension → synthesis → (Send fanout) → editor → budget_reconcile → sandbox_test`.
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
  one parallel `Send()` batch. **Documented limitation**: this is
  single-wave — a task blocked on a same-wave sibling isn't
  automatically re-dispatched once that sibling finishes; multi-wave
  scheduling is explicitly folded into Step 5's retry loop rather than
  built twice.
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
- **23 new tests (58 total)**, including
  `tests/test_graph_integration.py` — runs the **actual compiled
  graph** end-to-end (real `Send()`, real reducers, real conditional
  edges) with fake LLM/sandbox collaborators. This is the test that
  would catch a `Send()` payload-shape bug or a reducer
  misconfiguration; no amount of isolated node-function testing can
  catch a wiring bug at the graph level, only running the graph can —
  and it passed on the first full run after one import-path fix
  (`CodeGenerator` was defined in `editor.py`, not `llm.py` — a typo
  `build.py` initially imported from the wrong module).

**Also done in this step — this is where the system becomes actually
self-correcting, not just a linear pipeline:**
- **Resolved Step 4's documented limitation.** `budget_reconcile`'s
  fixed edge to `sandbox_test` became a *conditional* edge that reuses
  `route_to_editors` — so after each editor wave, the graph checks
  whether finishing it made more tasks ready (a dependency just
  succeeded) and dispatches again. A 3-level chain
  (repository → service → router) now fully completes in one graph
  run, proven by `test_full_pipeline_comprehension_through_sandbox`,
  which now asserts all 3 tasks reach `SUCCEEDED` — Step 4's version of
  this same test could only guarantee the one task with no dependencies.
- **Error-analysis node** (`app/graph/nodes/error_analysis.py`) — on a
  failed sandbox run, an LLM call sees the failing tests, the raw
  output, and every task whose code is actually present in that run
  (nothing else is a valid suspect), and returns — reusing the
  `ErrorAnalysis` schema from Step 1's state design directly as its
  structured-output target — a root cause and a retry/escalate
  decision per implicated task. A task it can't confidently attribute
  is left untouched rather than guessed at.
- **The retry loop reuses the multi-wave dispatch mechanism, not a
  second copy of it.** `route_to_editors` is now the conditional router
  on *three* edges (synthesis, budget_reconcile, error_analysis), each
  with its own "nothing ready" mapping. A task error-analysis marks
  `RETRYING` becomes eligible for the exact same dispatch path a fresh
  dependency-driven task uses — proven by
  `test_retry_loop_recovers_after_error_analysis_marks_tasks_retryable`,
  where marking all 3 tasks retryable correctly forces a full
  from-scratch re-migration in dependency order (each task's dependency
  is no longer `SUCCEEDED` either, so none are immediately ready) before
  the sandbox re-runs and passes.
- **Two independent, both-tested bounds prevent an infinite loop:**
  per-task `retry_count` vs `max_retries`, and a global `retry_round`
  vs `max_retry_rounds`. `test_round_cap_terminates_a_perpetually_failing_run`
  runs a sandbox that fails forever with an analyzer that always claims
  "retry me," and asserts the graph still terminates at exactly the
  configured round cap — not "should terminate on paper," actually
  observed to stop.
- **A real bug caught by reasoning through the wiring before writing
  any test**: if error-analysis escalates everything to `NEEDS_HUMAN`
  (nothing retryable), the graph goes straight to `END` and
  `budget_reconcile` never runs again — so under the Step 4-style
  delta mechanism, the error-analysis LLM call's own cost would have
  silently vanished from the final budget. Fixed by having
  `error_analysis` (which, unlike editor branches, is never part of a
  parallel fanout) reconcile its own cost directly instead of using the
  delta indirection that parallel branches need.
- **15 new tests (73 total)**, including two new full-graph
  integration tests that exercise the real retry loop and the real
  round-cap termination — not simulated, actually run through
  `graph.ainvoke()`. One pre-existing test's expectation changed
  correctly: a "no diffs to test" result now flows through to
  `error_analysis` (which recognizes it has nothing to analyze and
  escalates to `NEEDS_HUMAN`) instead of dead-ending at `sandbox_test`'s
  own phase the way Step 4's shorter graph did — an improvement in
  behavior, not a regression papered over.

**Also done in this step — plus three integrity fixes to earlier steps
that only surfaced once "what exactly ships in the PR?" was asked in
earnest:**
- **Fix 1 — superseded attempts.** `editor_results` is append-only, so
  after a retry it holds multiple attempts for the same task. Both the
  sandbox and the PR now derive their file set from ONE shared module,
  `app/graph/diffs.py` (`effective_diffs` / `effective_results` /
  `stale_paths`) — latest attempt per `SUCCEEDED` task, nothing else.
  Before this fix, the sandbox and a future PR step could each compute
  "the files" independently and silently disagree. `stale_paths` also
  feeds a new `remove_paths` (`app/sandbox/workspace.py`) so a retry
  that stops emitting a helper file doesn't leave a stale copy the
  tests still see.
- **Fix 2 — undeclared output paths.** The editor node now enforces a
  file contract: generated output must be exactly the task's declared
  `target_files`, no more, no fewer (`app/graph/nodes/editor.py`
  `_contract_violation`). Without this, nothing stopped an editor from
  quietly rewriting the target repo's own test files to make a suite
  "pass." A violation is treated like a failed generation — retried
  with the violation itself as feedback, not silently accepted.
  **This caught a real bug in the test fixtures while building it**:
  the integration tests' fake code generator was parsing target file
  paths out of the *entire* prompt instead of just the file-list
  section, and picked up a spurious path from `::`-delimited IR unit
  ids embedded in the instructions text. The new contract check
  rejected that bogus output exactly as designed — proof the check
  does real work, not just a rubber stamp.
- **Fix 3 — the retry loop now actually uses its own analysis.**
  `ErrorAnalysis.suggested_fix_instructions` was computed since Step 5
  but never fed anywhere. Added `MigrationTask.retry_feedback`, set by
  `error_analysis` and read by the editor's retry prompt — a retry is
  now a directed fix attempt, not a second blind guess.
- **`app/graph/nodes/aggregate.py`** — builds the PR title/body/branch
  name deterministically (no LLM call — a structured, factual summary
  doesn't need one, and a template avoids a flaky source of variance in
  a step whose output is about to enter a real repository) from the
  effective diffs, and re-validates every path with
  `app/vcs/paths.py`'s `publishable_path_problem` as defense-in-depth
  even though the editor contract should already prevent a bad path
  from arriving here. `.github/` is explicitly refused — a generated
  CI workflow in a PR is a privilege-escalation vector (workflows can
  run with repository secrets), and no application-code migration has
  a legitimate reason to touch it. A violation here is a hard stop,
  never a silent drop.
- **`app/vcs/github_publisher.py`** — real PyGithub-backed publisher:
  builds a git tree/commit/branch/PR the standard way (there's no
  "upload several files" REST endpoint). Lazy-imports `PyGithub`, same
  pattern as every other external SDK in this project.
- **Approval gating** (`app/graph/nodes/pr_publish.py`) — honors
  `require_human_approval_before_pr` (present in the schema since
  Step 1, unused until now): stops at `AWAITING_APPROVAL` with the
  draft ready to inspect, without calling the publisher. The real
  pause/resume (`interrupt()`) is explicitly Step 9's job; for now this
  is a clean terminal state, and `publish_approved_pr` is a standalone
  function (not inlined in the node) specifically so a future caller —
  the Step 7 API layer, once a human approves — reuses the exact same
  publish path this node itself takes when approval isn't required,
  rather than a second implementation that could drift from it.
- **A bug caught by reasoning through the wiring before running
  anything**: the fixed `aggregate -> pr_publish` edge always runs
  `pr_publish`, but `aggregate` can fail (empty diffs, unpublishable
  path) without producing a `pr_draft` — which would have crashed
  `pr_publish` on a missing key. Fixed with an explicit guard that
  leaves `aggregate`'s own `FAILED` state untouched instead.
- **32 new tests (105 total)**, including two new full-graph
  integration tests: one proves a PR is actually published (via a fake
  publisher) with exactly the effective diffs and nothing else, the
  other proves the approval gate actually stops the run — publisher
  never called — while still leaving a ready draft behind.

**Also done in this step:**
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
  (404 if the thread never existed — verified via LangGraph's actual
  behavior for an unknown `thread_id`, not assumed), `POST
  /migrations/{id}/approve` (404 if unknown, 409 if not
  `AWAITING_APPROVAL` or no draft, otherwise calls Step 6's
  `publish_approved_pr` — reused, not reimplemented — then
  `aupdate_state` to persist the result).
- **`app/worker/tasks.py`** — the real Celery task: repo prep, real
  graph, real Postgres checkpointer, one run per LangGraph thread
  (`thread_id == run_id`).
- **A genuine architectural bug caught before it could break in
  production**: the first draft of `app/main.py` built the Postgres
  checkpointer's connection pool at module-import time, on a throwaway
  event loop — before uvicorn's real loop exists. Async DB drivers bind
  connections to the loop that created them, so the pool would have
  been silently unusable the moment a real request hit it under
  uvicorn. Fixed by moving construction into a FastAPI `lifespan`
  handler (runs inside the server's actual loop) and having routes read
  `request.app.state.graph` instead of closing over a fixed variable —
  the right idiomatic pattern, not a workaround.
- **A second real bug, caught immediately by just trying to run the
  new tests**: `app/db/checkpointer.py` (written in Step 1) imported
  `AsyncPostgresSaver` at module level — meaning anything importing
  `app.db.checkpointer` at all required the Postgres extra installed,
  even code that never calls `get_checkpointer()`. Every other external
  SDK in this project was already lazy-imported for exactly this
  reason; this one had been missed. Fixed the same way.
- **Stated gap, not glossed over**: this environment has no Postgres to
  test against, so `AsyncPostgresSaver`'s round-trip of our custom
  Pydantic model fields (`BudgetState`, `MigrationTask`, etc.) through
  real serialization is unverified here — tests use LangGraph's
  in-memory `MemorySaver`, which round-trips Python objects directly
  with no serialization step at all. `app/api/app.py`'s status mapper
  defends against the round-trip producing plain dicts instead of the
  original objects, but this should be smoke-tested against a real
  Postgres instance before relying on it in production.
- **15 new tests (120 total)**: 5 for repo prep (including the real
  clone), 3 for the worker's orchestration (fakes throughout, in-memory
  checkpointer), 7 for the full API surface — including proving that
  `POST /approve` actually persists (a follow-up `GET` sees the
  published PR URL, not just the response body of the approve call
  itself).

**Not yet built (upcoming steps):**
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

## Running the current tests

```bash
pip install -r requirements.txt
PYTHONPATH=. pytest tests/ -v
```

Note: `app/core/llm.py`'s real `ExtractionClient`,
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
