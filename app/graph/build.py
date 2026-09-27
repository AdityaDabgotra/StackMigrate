"""
Assembles the compiled graph.

Current wiring (Step 4 scope):

    START -> comprehension -> synthesis --route_to_editors--> editor (parallel Send fanout)
                                        \\_(no ready tasks)_>              |
                                                                            v
                                                              sandbox_test <- budget_reconcile
                                                                    |
                                                                   END

Single-wave limitation: see app/graph/routing.py's module docstring.
This graph will grow a retry/replan loop back from sandbox_test through
an error-analysis node to another editor wave in Step 5 — the shape
above is deliberately the smallest correct slice of that eventual graph,
not a different design that gets thrown away.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.core.llm import ExtractionClient
from app.graph.nodes.budget_reconcile import build_budget_reconcile_node
from app.graph.nodes.comprehension import Extractor, build_comprehension_node
from app.graph.nodes.editor import CodeGenerator, build_editor_node
from app.graph.nodes.sandbox_test import build_sandbox_test_node
from app.graph.nodes.synthesis import build_synthesis_node
from app.graph.routing import NO_READY_TASKS, route_to_editors
from app.graph.state import GraphState
from app.sandbox.docker_runner import DockerSandboxRunner
from app.sandbox.interface import SandboxRunner


def build_graph(
    *,
    extractor: Extractor | None = None,
    code_generator: CodeGenerator | None = None,
    sandbox_runner: SandboxRunner | None = None,
    checkpointer=None,
):
    """
    All three collaborators default to real, production-backed
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
    sandbox_runner = sandbox_runner or DockerSandboxRunner()

    graph = StateGraph(GraphState)
    graph.add_node("comprehension", build_comprehension_node(extractor))
    graph.add_node("synthesis", build_synthesis_node())
    graph.add_node("editor", build_editor_node(code_generator))
    graph.add_node("budget_reconcile", build_budget_reconcile_node())
    graph.add_node("sandbox_test", build_sandbox_test_node(sandbox_runner))

    graph.add_edge(START, "comprehension")
    graph.add_edge("comprehension", "synthesis")
    graph.add_conditional_edges("synthesis", route_to_editors, {NO_READY_TASKS: "sandbox_test"})
    graph.add_edge("editor", "budget_reconcile")
    graph.add_edge("budget_reconcile", "sandbox_test")
    graph.add_edge("sandbox_test", END)

    return graph.compile(checkpointer=checkpointer)


def _default_code_generator():
    from app.core.llm import EditorClient

    return EditorClient()
