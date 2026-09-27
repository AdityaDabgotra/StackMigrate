from __future__ import annotations

import pytest

from app.adapters.fastapi_target.synthesis_adapter import FastAPITargetSynthesisAdapter
from app.graph.nodes.synthesis import build_synthesis_node, run_synthesis
from app.graph.state import (
    ComprehensionResult,
    EndpointUnit,
    MigrationPhase,
    RepositoryUnit,
    SemanticUnit,
    SemanticUnitKind,
    ServiceUnit,
)

ADAPTER = FastAPITargetSynthesisAdapter()


def _sample_comprehension() -> ComprehensionResult:
    endpoint1 = SemanticUnit(
        id="endpoint::create_order",
        kind=SemanticUnitKind.ENDPOINT,
        source_files=["src/main/java/com/example/orders/OrderController.java"],
        depends_on_unit_ids=["service::order_service"],
        payload=EndpointUnit(
            name="createOrder",
            http_method="POST",
            path="/api/orders",
            description="Creates an order",
            calls_services=["OrderService"],
            business_logic_summary="Creates an order",
        ),
    )
    endpoint2 = SemanticUnit(
        id="endpoint::get_order",
        kind=SemanticUnitKind.ENDPOINT,
        source_files=["src/main/java/com/example/orders/OrderController.java"],
        depends_on_unit_ids=["service::order_service"],
        payload=EndpointUnit(
            name="getOrder",
            http_method="GET",
            path="/api/orders/{id}",
            description="Fetches an order",
            calls_services=["OrderService"],
            business_logic_summary="Fetches an order by id",
        ),
    )
    service = SemanticUnit(
        id="service::order_service",
        kind=SemanticUnitKind.SERVICE,
        source_files=["src/main/java/com/example/orders/OrderService.java"],
        depends_on_unit_ids=["repository::order_repository"],
        payload=ServiceUnit(
            name="OrderService", description="Order logic", depends_on=["OrderRepository"],
            business_logic_summary="Creates and fetches orders",
        ),
    )
    repository = SemanticUnit(
        id="repository::order_repository",
        kind=SemanticUnitKind.REPOSITORY,
        source_files=["src/main/java/com/example/orders/OrderRepository.java"],
        payload=RepositoryUnit(
            name="OrderRepository", description="Order data access", target_data_model="Order",
            operations=["find by id"],
        ),
    )
    return ComprehensionResult(
        source_stack="springboot", repo_root="/tmp/repo", units=[endpoint1, endpoint2, service, repository]
    )


def test_endpoints_from_same_controller_collapse_into_one_task():
    tasks = run_synthesis(_sample_comprehension(), "fastapi", adapter=ADAPTER)
    endpoint_tasks = [t for t in tasks if t.target_files[0].path == "app/routers/order.py"]
    assert len(endpoint_tasks) == 1
    assert set(endpoint_tasks[0].source_unit_ids) == {"endpoint::create_order", "endpoint::get_order"}


def test_task_level_dependencies_are_resolved_from_unit_dependencies():
    tasks = run_synthesis(_sample_comprehension(), "fastapi", adapter=ADAPTER)
    by_path = {t.target_files[0].path: t for t in tasks}

    router_task = by_path["app/routers/order.py"]
    service_task = by_path["app/services/order_service.py"]
    repo_task = by_path["app/repositories/order_repository.py"]

    assert service_task.id in router_task.depends_on_task_ids
    assert repo_task.id in service_task.depends_on_task_ids
    assert repo_task.depends_on_task_ids == []


def test_synthesis_instructions_include_unit_business_logic():
    tasks = run_synthesis(_sample_comprehension(), "fastapi", adapter=ADAPTER)
    router_task = next(t for t in tasks if t.target_files[0].path == "app/routers/order.py")
    assert "Creates an order" in router_task.synthesis_instructions
    assert "Fetches an order by id" in router_task.synthesis_instructions
    assert "APIRouter" in router_task.synthesis_instructions  # FastAPI convention notes made it in


@pytest.mark.asyncio
async def test_synthesis_node_updates_graph_state():
    node = build_synthesis_node()
    state = {"comprehension": _sample_comprehension(), "config": {"target_stack": "fastapi"}}
    update = await node(state)

    assert update["phase"] == MigrationPhase.EDITING
    assert len(update["tasks"]) == 3  # router (2 endpoints merged), service, repository


@pytest.mark.asyncio
async def test_synthesis_node_fails_gracefully_on_empty_comprehension():
    node = build_synthesis_node()
    empty = ComprehensionResult(source_stack="springboot", repo_root="/tmp/repo", units=[])
    update = await node({"comprehension": empty, "config": {"target_stack": "fastapi"}})

    assert update["phase"] == MigrationPhase.FAILED
    assert "tasks" not in update
