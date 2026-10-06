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
`StateGraph` (verified against `langgraph==1.2.12`). Every node is wrapped
by `observed_node` (structured logging, see Step 8).

```
START -> comprehension --(budget gone)--------------------------> budget_abort -> END
              |
              v
          synthesis --route_to_editors--> editor (parallel Send fanout)
              |   \__(budget gone)__> budget_abort            |
              |                                                v
              |                                       budget_reconcile
              |                                         |            \
              |                       (more tasks now ready)        (nothing left ready)
              |                                         |                  |
              |                           (budget gone) |                  v
              |                          -> budget_abort|             sandbox_test
              |                                         |              |          \
              |                                         |           (passed)   (failed / error)
              |                                         |              |            |
              |                                         |              v            v
              |                                         |          aggregate    error_analysis
              |                                         |       (tail, below)     |        \
              |                                         |                  (something      (nothing retryable /
              |                                         |                   retryable)      round cap hit)
              |                                         |                       |               |
              |                                         \_______________________/               v
              |                                  (rejoins the SAME editor fanout)              END
              \__(nothing ready)__> sandbox_test
```

The tail after `aggregate` (Step 9):

```
aggregate --(aggregate failed: no draft)-----------------------------> END
    |
    +--(no repo, or approval not required)--------------------------> pr_publish -> END
    |
    +--(approval required)--> request_approval -> approval_gate
                                  (phase = AWAITING_APPROVAL)   (graph PAUSES here: interrupt())
                                                                      |
                                                       approve -------+------- reject
                                                          |                       |
                                                      pr_publish -> END         END  (phase = REJECTED)
```

`route_to_editors` is the conditional router on three edges (synthesis,
`budget_reconcile`, `error_analysis`), so multi-wave dependency scheduling
and the retry loop are one mechanism, not two. It is also the single
budget gate: see Step 8. The approval pause is described in Step 9.

## Status: Step 9 of 10 — Human-in-the-loop approval

140 tests pass. Every external collaborator (LLM, Docker, GitHub, Postgres)
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
  every diff in `editor_results` to the target workspace and runs the
  full suite; routes to `AGGREGATING` on pass, `ERROR_ANALYSIS` on anything
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
  keeps a last-line budget check (the real gate is in the router, Step 8).
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

One integrity fix to earlier steps, only partly landed:

- **Superseded attempts.** `editor_results` is append-only, so after a
  retry it holds multiple attempts for the same task. The PR side derives
  its file set from ONE shared module, `app/graph/diffs.py`
  (`effective_diffs` / `effective_results` / `stale_paths`) — latest
  attempt per `SUCCEEDED` task, nothing else — used by `aggregate` and
  `pr_publish`. **The sandbox does not use it yet**: see "Known gaps".

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
- **Approval gating.** `require_human_approval_before_pr` (in the schema
  since Step 1) is honored by `pr_publish` and, since Step 9, by a real
  pause/resume gate in front of it. `publish_approved_pr` is a standalone
  function so the publish logic is testable on its own.
- **A bug caught by reasoning through the wiring**: `aggregate` can fail
  (empty diffs, unpublishable path) without producing a `pr_draft`, which
  would have crashed `pr_publish` on a missing key. `pr_publish` keeps an
  explicit guard that leaves `aggregate`'s own `FAILED` state untouched,
  and (Step 9) the router sends a draft-less run straight to `END`.
- **Graph-level test for the publish path**
  (`tests/test_graph_integration.py`): proves a PR is published, via a fake
  publisher, with exactly the effective diffs and nothing else. The
  approval-gate tests live in `tests/test_approval_flow.py` (Step 9).

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
- **No separate "runs" database.** Status reads and approve/reject resumes
  go through the compiled graph's own `aget_state` / `ainvoke` against its
  existing checkpointer — LangGraph's checkpoint store already IS the
  durable, queryable record of every run; a second store would just be a
  second source of truth that could drift from the first.
- **`app/api/app.py`** — `create_app()` factory: `POST /migrations`
  (submit, hands off via a `TaskSubmitter` Protocol — Celery in production,
  a recording fake in tests), `GET /migrations/{id}` (404 if the thread
  never existed), and — as of Step 9 — `POST /migrations/{id}/approve` and
  `/reject`, which resume a paused run (see Step 9).
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

### Step 8: Budget guard + observability

**Budget guard.** Three independent ceilings, any one of which exhausts the
budget (`BudgetState.exhausted_reason()` says which): cost (`max_usd`), LLM
call count (`max_llm_calls`), and wall clock (`max_wall_clock_seconds`,
measured from `started_at`).

- **One central gate.** Every editor dispatch — first wave, dependency
  waves, retries — passes through `route_to_editors`. If work remains but
  the budget is exhausted, the run goes to the new `budget_abort` node. If
  nothing remains, the run proceeds to testing even when the last dollar
  was just spent.
- **Wave capping.** Parallel waves are sized by the largest single call
  cost seen so far (`BudgetState.affordable_calls`), so branches can't
  collectively overshoot the ceiling; leftovers are picked up by the next
  wave. The worst-case overshoot is one call. It is not an exact ceiling,
  since a call can't be capped before it runs — setting `max_tokens` on the
  Anthropic clients would tighten it further.
- **`budget_abort`** (`app/graph/nodes/budget_abort.py`) marks unfinished
  tasks `SKIPPED`, records why in `error_log`, sets `ABORTED_BUDGET` and
  ends the run. A partial migration is never tested or published.
  Comprehension running out of budget now ends the run through the same
  node instead of falling through to synthesis.
- **Per-run limits.** `max_usd`, `max_llm_calls` and `max_wall_clock_seconds`
  are API request fields, defaulting from `DEFAULT_MAX_USD_PER_RUN`,
  `DEFAULT_MAX_LLM_CALLS_PER_RUN` and `DEFAULT_MAX_WALL_CLOCK_SECONDS`. The
  status endpoint reports calls made and the limits alongside spend.

**Observability.** Two independent layers (`app/core/observability.py`):

- **Structured JSON logs, always on.** `observed_node` wraps every graph
  node and emits one `node_finished` (or `node_failed`) line with `run_id`,
  `node`, `task_id`, `duration_ms`, `phase`, `cost_delta_usd` and a budget
  snapshot (`budget_spent_usd`, `budget_remaining_usd`, `llm_calls_made`).
  Logging is configured once in the API lifespan and the Celery task.
  Filter one run with, e.g., `jq 'select(.run_id=="<id>")'`.
- **LangSmith tracing, opt-in, no node code.** Set `LANGSMITH_TRACING=true`,
  `LANGSMITH_API_KEY` and `LANGSMITH_PROJECT`. The worker passes the thread
  id, tags (`springboot->fastapi`) and metadata (`run_id`, stacks,
  `max_usd`) so a trace can be found by run.

Tests: `tests/test_budget_guard.py` covers each ceiling, wave capping, the
gate, the abort node, and two full-graph aborts (budget runs out
mid-migration; wall clock expired before comprehension) that assert the
sandbox and PR are never reached. `tests/test_observability.py` covers the
log lines, failure re-raise and trace metadata.

### Step 9: Human-in-the-loop approval via `interrupt()`

When a run has a `github_target_repo` and `require_human_approval_before_pr`
(default true), the graph now genuinely PAUSES before opening a PR and
resumes only on a human decision. It's a checkpointed LangGraph
`interrupt()`, so the run survives process restarts (with the Postgres
checkpointer) and the Celery worker is free the moment it pauses.

- **Two nodes around one interrupt** (`app/graph/nodes/approval.py`).
  `request_approval` only sets `phase=AWAITING_APPROVAL` so the paused
  checkpoint reads correctly; `approval_gate` calls `interrupt()` with a
  reviewable payload (title, body, branch, file list). A node's own state
  update is not committed when it interrupts, and on resume the node re-runs
  from its first line — so nothing before `interrupt()` may have side
  effects, which is why the bookkeeping lives in its own node.
- **Fails closed.** Only a resume of exactly `{"approved": true}` approves.
  A missing key, `"true"`, `1`, or `null` is a rejection.
- **One definition of "needs approval"** (`needs_human_approval` in
  `app/graph/routing.py`) is shared by the router and by `pr_publish`.
  `pr_publish` now refuses to publish (phase `FAILED`) if approval is
  required but `approval_status != "approved"`, so a future wiring mistake
  can't open an unapproved PR.
- **API.** `POST /migrations/{id}/approve` and `/reject` (optional body
  `{"feedback": "..."}`) resume the graph with `Command(resume=...)`.
  404 for an unknown run, 409 if the run isn't paused at the gate.
  `GET /migrations/{id}` reports `pending_approval` (title, body, branch,
  files) while paused, then `approval_status` and `human_feedback` after.
  A per-run lock plus a re-read inside it means a double-clicked approve
  publishes once (tested concurrently). The API no longer takes a publisher:
  the publisher lives in the graph, so there is one publish path.
- **New phase `REJECTED`:** terminal, nothing published, feedback recorded in
  `human_feedback` and `error_log`.
- **A bug caught while wiring this:** `observed_node` (Step 8) caught every
  `Exception`, and LangGraph's `interrupt()` travels as an exception, so each
  approval pause would have been logged as a failed node. Interrupts are now
  logged as `node_interrupted` and re-raised.
- **Tests** (`tests/test_approval_flow.py`): pauses with nothing published;
  approve publishes exactly once with the effective files; reject publishes
  nothing; five malformed resume values all fail closed; no pause when
  approval isn't required or no repo is set; reject-then-409 over HTTP; a
  concurrent double-approve returns 200 and 409 and publishes once.

## Roadmap

1. ~~Scaffolding + state schema~~ ✅
2. ~~Comprehension node (repo analysis → IR)~~ ✅
3. ~~Sandbox execution layer (Docker isolation)~~ ✅
4. ~~Synthesis + editor nodes with parallel `Send()` fanout~~ ✅
5. ~~Test runner + error-analyzer retry loop~~ ✅
6. ~~Diff aggregation + GitHub PR generation~~ ✅
7. ~~FastAPI + Celery async API layer~~ ✅
8. ~~Budget guard + observability wiring~~ ✅
9. ~~Human-in-the-loop approval via `interrupt()`~~ ✅
10. Deployment (Dockerfile, docker-compose, CI)

## Known gaps

Stated plainly rather than glossed over. Items 1 to 3 were described in
earlier versions of this README as done; they are not in the code.

1. **Sandbox applies superseded attempts.** `sandbox_test` applies every
   diff in `editor_results` instead of `effective_diffs`, and there is no
   `remove_paths` cleanup. After a retry, a file the retry stopped emitting
   can linger in the workspace and be seen by the tests, so the sandbox and
   the PR can disagree about "the files".
2. **No editor file contract.** Nothing enforces that generated output is
   exactly the task's declared `target_files`, so an editor could add or
   rewrite undeclared files (including the target repo's own tests).
3. **Error-analysis feedback is not used.** `ErrorAnalysis.suggested_fix_instructions`
   is computed but never fed into the retry prompt (`MigrationTask` has no
   `retry_feedback` field), so a retry is a blind second attempt.
4. **Comprehension undercounts LLM calls.** It records N extraction calls as
   one, so `max_llm_calls` is looser than it looks.
5. **Nodes mutate `BudgetState` in place.** Safe today, but risky for
   checkpoint history and for Step 9's `interrupt()` resume.
6. **Wall clock keeps running while a run waits for approval.** It only
   matters if a decision can lead back to editors; today a rejection
   terminates the run, so it is dormant.
7. **`AsyncPostgresSaver` round-trip is unverified** (see Step 7): tests use
   the in-memory `MemorySaver`. Step 9 adds a pause/resume through the
   checkpointer, which makes this check more important.
8. **No authentication on `/approve` and `/reject`.** Anyone who can reach the
   API and knows a run id can approve a PR. Must be fixed before deployment
   (Step 10).
9. **The approval lock is in-process.** With several API replicas, two
   instances could both resume a run; serialize on the run in the database.
10. **Reviewers see the PR body and file paths, not file contents or a diff.**
11. **Reject is terminal.** There is no "request changes" loop that feeds
    `human_feedback` back to the editors, and no expiry for runs left waiting.

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

## Configuration

Copy `.env.example` to `.env`. Per-run budget defaults
(`DEFAULT_MAX_USD_PER_RUN`, `DEFAULT_MAX_LLM_CALLS_PER_RUN`,
`DEFAULT_MAX_WALL_CLOCK_SECONDS`) can be overridden per request. Tracing
needs `LANGSMITH_TRACING=true` plus the key and project; the JSON logs need
no configuration.

## Why a TypedDict for `GraphState` and not pure Pydantic?

LangGraph's state-merging machinery expects `Annotated[T, reducer]`
field annotations on a `TypedDict`. We get Pydantic's validation where
it matters (every individual field is a Pydantic model) without
fighting LangGraph's merge semantics. Full rationale is in the
docstring at the top of `app/graph/state/graph_state.py`.