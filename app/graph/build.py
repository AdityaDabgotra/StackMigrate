"""
Assembles the compiled graph.

Current wiring (Step 5 scope):

    START -> comprehension -> synthesis --route_to_editors--> editor (parallel Send fanout)
                                        \\__(nothing ready)___>   |
                                                                   v
                                                          budget_reconcile
                                                            |            \\
                                            (more tasks now ready,       (nothing left ready)
                                             e.g. a dependency               |
                                             just succeeded)                 v
                                                 |                     sandbox_test
                                                 |                       |        \\
                                                 |                    (passed)  (failed/error)
                                                 |                       |         |
                                                 |                      END   error_analysis
                                                 |                                 |       \\
                                                 |                        (something      (nothing
                                                 |                         retryable)      retryable /
                                                 |                              |          round cap hit)
                                                  \\_____________________________/              |
                                                    (rejoins the SAME editor fanout,             v
                                                     reusing route_to_editors — this is          END
                                                     what makes multi-wave dependency
                                                     scheduling AND the retry loop the
                                                     same mechanism, not two)

`route_to_editors` is reused as the conditional router on THREE edges
(synthesis, budget_reconcile, error_analysis), each with its own
`NO_READY_TASKS` mapping — that's what makes a single editor/
budget_reconcile pair correctly handle both "dispatch the next
dependency wave" and "dispatch a retry" without duplicated logic.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.core.llm import ExtractionClient
from app.core.observability import observed_node
from app.graph.nodes.aggregate import build_aggregate_node
from app.graph.nodes.pr_publish import build_pr_publish_node
from app.graph.nodes.approval import build_approval_gate_node, build_request_approval_node
from app.graph.nodes.budget_abort import build_budget_abort_node
from app.graph.nodes.budget_reconcile import build_budget_reconcile_node
from app.graph.nodes.comprehension import Extractor, build_comprehension_node
from app.graph.nodes.editor import CodeGenerator, build_editor_node
from app.graph.nodes.error_analysis import ErrorAnalyzer, build_error_analysis_node
from app.graph.nodes.sandbox_test import build_sandbox_test_node
from app.graph.nodes.synthesis import build_synthesis_node
from app.graph.routing import (
    APPROVAL_REQUIRED,
    APPROVED,
    FINISH,
    PUBLISH,
    REJECTED,
    BUDGET_EXHAUSTED,
    CONTINUE,
    NO_READY_TASKS,
    SANDBOX_NEEDS_ANALYSIS,
    SANDBOX_PASSED,
    route_after_aggregate,
    route_after_approval,
    route_after_comprehension,
    route_after_sandbox,
    route_to_editors,
)
from app.graph.state import GraphState
from app.sandbox.docker_runner import DockerSandboxRunner
from app.sandbox.interface import SandboxRunner
from app.vcs.github_publisher import GitHubPRPublisher


def build_graph(
    *,
    extractor: Extractor | None = None,
    code_generator: CodeGenerator | None = None,
    error_analyzer: ErrorAnalyzer | None = None,
    sandbox_runner: SandboxRunner | None = None,
    pr_publisher: GitHubPRPublisher | None = None,
    checkpointer=None,
):
    """
    All collaborators default to real, production-backed
    implementations — pass fakes for testing (see
    tests/test_graph_integration.py). `checkpointer=None` runs the
    graph without persistence, which is fine for tests and for the
    real production run too, once the caller wraps this in
    `app.db.checkpointer.get_checkpointer()` and passes it through —
    that wiring happens where the graph is actually invoked (the
    FastAPI/Celery layer, Step 7), not here.
    """
    extractor = extractor or ExtractionClient()
    code_generator = code_generator or _default_code_generator()
    error_analyzer = error_analyzer or _default_error_analyzer()
    sandbox_runner = sandbox_runner or DockerSandboxRunner()
    pr_publisher = pr_publisher or _default_pr_publisher()

    graph = StateGraph(GraphState)
    graph.add_node("comprehension", observed_node("comprehension", build_comprehension_node(extractor)))
    graph.add_node("synthesis", observed_node("synthesis", build_synthesis_node()))
    graph.add_node("editor", observed_node("editor", build_editor_node(code_generator)))
    graph.add_node("budget_reconcile", observed_node("budget_reconcile", build_budget_reconcile_node()))
    graph.add_node("sandbox_test", observed_node("sandbox_test", build_sandbox_test_node(sandbox_runner)))
    graph.add_node("error_analysis", observed_node("error_analysis", build_error_analysis_node(error_analyzer)))
    graph.add_node("aggregate", observed_node("aggregate", build_aggregate_node()))
    graph.add_node("pr_publish", observed_node("pr_publish", build_pr_publish_node(pr_publisher)))
    graph.add_node("request_approval", observed_node("request_approval", build_request_approval_node()))
    graph.add_node("approval_gate", observed_node("approval_gate", build_approval_gate_node()))
    graph.add_node("budget_abort", observed_node("budget_abort", build_budget_abort_node()))

    graph.add_edge(START, "comprehension")
    graph.add_conditional_edges(
        "comprehension", route_after_comprehension, {CONTINUE: "synthesis", BUDGET_EXHAUSTED: "budget_abort"}
    )

    # Initial dispatch: whatever's ready with no dependencies at all.
    graph.add_conditional_edges(
        "synthesis", route_to_editors, {NO_READY_TASKS: "sandbox_test", BUDGET_EXHAUSTED: "budget_abort"}
    )

    graph.add_edge("editor", "budget_reconcile")

    # The multi-wave loop: after each wave, check whether finishing it made
    # more tasks ready (a dependency just succeeded) and dispatch again;
    # once nothing's left ready, move on to actually running the tests.
    graph.add_conditional_edges(
        "budget_reconcile", route_to_editors, {NO_READY_TASKS: "sandbox_test", BUDGET_EXHAUSTED: "budget_abort"}
    )

    graph.add_conditional_edges(
        "sandbox_test",
        route_after_sandbox,
        {SANDBOX_PASSED: "aggregate", SANDBOX_NEEDS_ANALYSIS: "error_analysis"},
    )

    # The retry loop: error_analysis marks retryable tasks RETRYING, then
    # rejoins the exact same dispatch mechanism as the initial multi-wave
    # scheduling above. If nothing came out retryable (everything escalated
    # to NEEDS_HUMAN, or nothing was attributable), the run ends here.
    graph.add_conditional_edges(
        "error_analysis", route_to_editors, {NO_READY_TASKS: END, BUDGET_EXHAUSTED: "budget_abort"}
    )

    graph.add_conditional_edges(
        "aggregate",
        route_after_aggregate,
        {APPROVAL_REQUIRED: "request_approval", PUBLISH: "pr_publish", FINISH: END},
    )
    graph.add_edge("request_approval", "approval_gate")
    graph.add_conditional_edges("approval_gate", route_after_approval, {APPROVED: "pr_publish", REJECTED: END})
    graph.add_edge("pr_publish", END)
    graph.add_edge("budget_abort", END)

    return graph.compile(checkpointer=checkpointer)


def _default_pr_publisher():
    from app.vcs.github_publisher import GitHubPRPublisher

    return GitHubPRPublisher()

def _default_code_generator():
    from app.core.llm import EditorClient

    return EditorClient()


def _default_error_analyzer():
    from app.core.llm import ErrorAnalysisClient

    return ErrorAnalysisClient()
