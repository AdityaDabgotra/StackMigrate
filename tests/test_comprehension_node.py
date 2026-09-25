"""
Step 2 validation tests.

Uses a FakeExtractor (satisfies the `Extractor` Protocol) instead of the
real Anthropic-backed client — these tests prove the orchestration logic
(discovery -> extraction -> id assignment -> dependency resolution ->
budget accounting) is correct, independent of any real model call.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from app.adapters.springboot.source_adapter import SpringBootSourceAdapter
from app.core.llm import ExtractionResponse, FileExtractionResult, RawExtractedUnit
from app.graph.nodes.comprehension import build_comprehension_node, run_comprehension
from app.graph.state import (
    BudgetState,
    EndpointUnit,
    MigrationPhase,
    RepositoryUnit,
    SemanticUnitKind,
    ServiceUnit,
)

ORDER_CONTROLLER_JAVA = """
package com.example.orders;

@RestController
@RequestMapping("/api/orders")
public class OrderController {

    private final OrderService orderService;

    @PostMapping
    public OrderResponse createOrder(@RequestBody CreateOrderRequest request) {
        return orderService.createOrder(request);
    }
}
"""

ORDER_SERVICE_JAVA = """
package com.example.orders;

@Service
public class OrderService {

    private final OrderRepository orderRepository;

    public OrderResponse createOrder(CreateOrderRequest request) {
        // ...
    }
}
"""

ORDER_REPOSITORY_JAVA = """
package com.example.orders;

@Repository
public interface OrderRepository extends JpaRepository<Order, Long> {
    Optional<Order> findByIdAndAccountId(Long id, Long accountId);
}
"""


def _write_spring_repo(root: str) -> None:
    base = os.path.join(root, "src", "main", "java", "com", "example", "orders")
    os.makedirs(base, exist_ok=True)
    with open(os.path.join(base, "OrderController.java"), "w") as f:
        f.write(ORDER_CONTROLLER_JAVA)
    with open(os.path.join(base, "OrderService.java"), "w") as f:
        f.write(ORDER_SERVICE_JAVA)
    with open(os.path.join(base, "OrderRepository.java"), "w") as f:
        f.write(ORDER_REPOSITORY_JAVA)
    # a file outside scope terms, to prove scope filtering works
    other = os.path.join(root, "src", "main", "java", "com", "example", "billing")
    os.makedirs(other, exist_ok=True)
    with open(os.path.join(other, "InvoiceController.java"), "w") as f:
        f.write("@RestController\npublic class InvoiceController {}\n")


class FakeExtractor:
    """
    Maps file content -> canned FileExtractionResult by sniffing a
    marker string in the prompt. Good enough for orchestration tests;
    NOT meant to simulate real extraction quality.
    """

    def __init__(self, fail_on: str | None = None):
        self.fail_on = fail_on
        self.calls: list[str] = []

    async def extract(self, prompt: str) -> ExtractionResponse:
        self.calls.append(prompt)

        if self.fail_on and self.fail_on in prompt:
            raise RuntimeError("simulated extraction failure")

        if "OrderController.java" in prompt:
            result = FileExtractionResult(
                units=[
                    RawExtractedUnit(
                        kind=SemanticUnitKind.ENDPOINT,
                        name="createOrder",
                        payload=EndpointUnit(
                            name="createOrder",
                            http_method="POST",
                            path="/api/orders",
                            description="Creates an order",
                            request_body_model="CreateOrderRequest",
                            response_model="OrderResponse",
                            calls_services=["OrderService"],
                            business_logic_summary="Delegates to OrderService.createOrder",
                        ),
                    )
                ]
            )
        elif "OrderService.java" in prompt:
            result = FileExtractionResult(
                units=[
                    RawExtractedUnit(
                        kind=SemanticUnitKind.SERVICE,
                        name="OrderService",
                        payload=ServiceUnit(
                            name="OrderService",
                            description="Order business logic",
                            depends_on=["OrderRepository"],
                            business_logic_summary="Creates and persists orders",
                        ),
                    )
                ]
            )
        elif "OrderRepository.java" in prompt:
            result = FileExtractionResult(
                units=[
                    RawExtractedUnit(
                        kind=SemanticUnitKind.REPOSITORY,
                        name="OrderRepository",
                        payload=RepositoryUnit(
                            name="OrderRepository",
                            description="Order data access",
                            target_data_model="Order",
                            operations=["find by id and account id"],
                        ),
                    )
                ]
            )
        else:
            result = FileExtractionResult(units=[], ambiguities=["unrecognized file in test fixture"])

        return ExtractionResponse(result=result, cost_usd=0.01, input_tokens=500, output_tokens=200)


@pytest.mark.asyncio
async def test_discovery_respects_scope_and_skips_out_of_scope_files():
    with tempfile.TemporaryDirectory() as tmp:
        _write_spring_repo(tmp)
        adapter = SpringBootSourceAdapter()
        files = adapter.discover_files(tmp, "migrate the OrderController module only")
        relative_paths = {f.relative_path for f in files}

        assert any("OrderController.java" in p for p in relative_paths)
        assert any("OrderService.java" in p for p in relative_paths)
        assert any("OrderRepository.java" in p for p in relative_paths)
        assert not any("InvoiceController.java" in p for p in relative_paths)  # out of scope


@pytest.mark.asyncio
async def test_run_comprehension_resolves_cross_file_dependencies():
    with tempfile.TemporaryDirectory() as tmp:
        _write_spring_repo(tmp)
        extractor = FakeExtractor()

        result, cost, errors = await run_comprehension(
            repo_root=tmp,
            scope_description="migrate the OrderController module only",
            source_stack="springboot",
            remaining_budget_usd=10.0,
            extractor=extractor,
        )

        assert errors == []
        assert cost == pytest.approx(0.03)  # 3 files * $0.01
        assert len(result.units) == 3

        by_kind = {u.kind: u for u in result.units}
        endpoint = by_kind[SemanticUnitKind.ENDPOINT]
        service = by_kind[SemanticUnitKind.SERVICE]
        repository = by_kind[SemanticUnitKind.REPOSITORY]

        # endpoint -> service dependency resolved by name
        assert service.id in endpoint.depends_on_unit_ids
        # service -> repository dependency resolved by name
        assert repository.id in service.depends_on_unit_ids
        # repository references "Order" (the entity) which was never extracted
        # (out of scope in this fixture) -> should surface as an ambiguity, not crash
        assert any("Order" in a for a in result.unresolved_ambiguities)


@pytest.mark.asyncio
async def test_run_comprehension_continues_past_single_file_extraction_failure():
    with tempfile.TemporaryDirectory() as tmp:
        _write_spring_repo(tmp)
        extractor = FakeExtractor(fail_on="OrderService.java")

        result, cost, errors = await run_comprehension(
            repo_root=tmp,
            scope_description="migrate the OrderController module only",
            source_stack="springboot",
            remaining_budget_usd=10.0,
            extractor=extractor,
        )

        assert len(errors) == 1
        assert "OrderService.java" in errors[0]
        # the other two files still got extracted despite one failure
        assert len(result.units) == 2
        assert any("could not be analyzed" in a for a in result.unresolved_ambiguities)


@pytest.mark.asyncio
async def test_run_comprehension_stops_when_budget_exhausted():
    with tempfile.TemporaryDirectory() as tmp:
        _write_spring_repo(tmp)
        extractor = FakeExtractor()

        result, cost, errors = await run_comprehension(
            repo_root=tmp,
            scope_description="migrate the OrderController module only",
            source_stack="springboot",
            remaining_budget_usd=0.0,  # no budget at all
            extractor=extractor,
        )

        assert cost == 0.0
        assert len(extractor.calls) == 0  # never even called the LLM
        assert len(result.units) == 0
        assert any("Budget exhausted" in e for e in errors) or any(
            "budget exhausted" in a.lower() for a in result.unresolved_ambiguities
        )


@pytest.mark.asyncio
async def test_comprehension_node_updates_graph_state_correctly():
    with tempfile.TemporaryDirectory() as tmp:
        _write_spring_repo(tmp)
        extractor = FakeExtractor()
        node = build_comprehension_node(extractor)

        state = {
            "config": {
                "run_id": "test-run",
                "source_repo_url": "https://example.com/repo.git",
                "source_ref": "main",
                "source_stack": "springboot",
                "target_stack": "fastapi",
                "scope_description": "migrate the OrderController module only",
                "github_target_repo": None,
                "require_human_approval_before_pr": True,
                "local_checkout_path": tmp,
            },
            "budget": BudgetState(max_usd=10.0),
        }

        update = await node(state)

        assert update["phase"] == MigrationPhase.SYNTHESIS_PLANNING
        assert update["comprehension"] is not None
        assert len(update["comprehension"].units) == 3
        assert update["budget"].spent_usd == pytest.approx(0.03)


@pytest.mark.asyncio
async def test_comprehension_node_short_circuits_on_pre_exhausted_budget():
    node = build_comprehension_node(FakeExtractor())
    state = {
        "config": {
            "run_id": "test-run",
            "source_repo_url": "x",
            "source_ref": "main",
            "source_stack": "springboot",
            "target_stack": "fastapi",
            "scope_description": "x",
            "github_target_repo": None,
            "require_human_approval_before_pr": True,
            "local_checkout_path": "/nonexistent",
        },
        "budget": BudgetState(max_usd=1.0, spent_usd=1.0),  # already exhausted
    }

    update = await node(state)
    assert update["phase"] == MigrationPhase.ABORTED_BUDGET
    assert "comprehension" not in update
