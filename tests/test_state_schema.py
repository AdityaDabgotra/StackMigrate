import operator

import pytest
from pydantic import ValidationError

from app.graph.state import (
    BudgetState,
    ComprehensionResult,
    EndpointUnit,
    MigrationTask,
    SemanticUnit,
    SemanticUnitKind,
    TargetFileSpec,
    TaskStatus,
)
from app.graph.state.graph_state import _merge_task_lists


def _make_endpoint_unit() -> SemanticUnit:
    return SemanticUnit(
        id="endpoint::create_order",
        kind=SemanticUnitKind.ENDPOINT,
        source_files=["src/main/java/com/example/OrderController.java"],
        depends_on_unit_ids=["service::order_service"],
        payload=EndpointUnit(
            name="createOrder",
            http_method="POST",
            path="/api/orders",
            description="Creates a new order for the authenticated user",
            request_body_model="CreateOrderRequest",
            response_model="OrderResponse",
            auth_requirement="requires JWT",
            calls_services=["OrderService"],
            business_logic_summary="Validates cart items, checks inventory, creates Order, publishes OrderCreated event",
        ),
    )


def test_comprehension_result_validates_real_shaped_data():
    result = ComprehensionResult(
        source_stack="spring-boot-3.2",
        repo_root="/tmp/repo",
        units=[_make_endpoint_unit()],
        global_notes=["Uses a global @ControllerAdvice that wraps all errors in {error, code}"],
        unresolved_ambiguities=[],
    )
    assert result.units[0].kind == SemanticUnitKind.ENDPOINT
    assert isinstance(result.units[0].payload, EndpointUnit)
    assert result.units[0].payload.http_method == "POST"


def test_comprehension_result_rejects_bad_kind():
    with pytest.raises(ValidationError):
        SemanticUnit(
            id="bad::unit",
            kind="not_a_real_kind",  # invalid enum value
            source_files=[],
            payload={},
        )


def test_task_list_reducer_updates_in_place_by_id():
    """
    Simulates what happens when the error-analysis node updates a task's
    status after a retry: it must overwrite the existing task, not
    append a second copy with the same id.
    """
    t1 = MigrationTask(
        id="task::endpoint::create_order",
        source_unit_ids=["endpoint::create_order"],
        target_files=[TargetFileSpec(path="app/routers/orders.py", purpose="order endpoints")],
        synthesis_instructions="Map @RestController -> APIRouter, @PostMapping -> @router.post",
        status=TaskStatus.PENDING,
    )
    existing = [t1]

    t1_updated = t1.model_copy(update={"status": TaskStatus.RETRYING, "retry_count": 1})
    merged = _merge_task_lists(existing, [t1_updated])

    assert len(merged) == 1  # no duplication
    assert merged[0].status == TaskStatus.RETRYING
    assert merged[0].retry_count == 1


def test_task_list_reducer_appends_genuinely_new_tasks():
    t1 = MigrationTask(
        id="task::a",
        source_unit_ids=["a"],
        target_files=[TargetFileSpec(path="a.py", purpose="x")],
        synthesis_instructions="...",
    )
    t2 = MigrationTask(
        id="task::b",
        source_unit_ids=["b"],
        target_files=[TargetFileSpec(path="b.py", purpose="y")],
        synthesis_instructions="...",
    )
    merged = _merge_task_lists([t1], [t2])
    assert {t.id for t in merged} == {"task::a", "task::b"}


def test_additive_reducer_matches_operator_add_semantics():
    """
    editor_results / test_results / error_analyses use plain
    `operator.add` as their reducer. This documents *why* that's safe:
    each parallel editor branch returns a list with exactly ONE
    EditorResult, so concatenation (not id-merge) is correct — unlike
    tasks, results are never "updated," only appended once per attempt.
    """
    branch_a_output = ["editor_result_for_task_a"]
    branch_b_output = ["editor_result_for_task_b"]
    merged = operator.add(branch_a_output, branch_b_output)
    assert merged == ["editor_result_for_task_a", "editor_result_for_task_b"]


def test_budget_state_exhaustion_logic():
    budget = BudgetState(max_usd=1.00, max_llm_calls=10)
    assert not budget.is_exhausted()

    for _ in range(10):
        budget.record_call(cost_usd=0.05)

    assert budget.is_exhausted()  # hit llm_calls ceiling before usd ceiling
    assert budget.spent_usd == pytest.approx(0.50)
    assert budget.remaining_usd() == pytest.approx(0.50)  # usd itself isn't exhausted, calls are
