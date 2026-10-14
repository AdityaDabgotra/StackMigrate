"""Things that only break once the system runs as separate processes/containers."""

from __future__ import annotations

from app.services.repo_prep import GitRepoPreparer
from app.worker.celery_app import celery_app


def test_a_worker_started_with_celery_dash_A_celery_app_registers_the_migration_task():
    # The API only sends the task by NAME; the worker must import the module that defines it.
    celery_app.loader.import_default_modules()
    assert "stackmigrate.run_migration" in celery_app.tasks


def test_work_dir_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("STACKMIGRATE_WORK_DIR", "/var/lib/stackmigrate/work")
    assert GitRepoPreparer()._base_dir == "/var/lib/stackmigrate/work"
    assert GitRepoPreparer("/explicit")._base_dir == "/explicit"  # an explicit argument still wins


def test_importing_the_checkpointer_module_does_not_require_the_postgres_sdk(monkeypatch):
    import importlib
    import sys

    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres.aio", None)  # makes the import fail
    sys.modules.pop("app.db.checkpointer", None)
    importlib.import_module("app.db.checkpointer")  # must not raise
    sys.modules.pop("app.db.checkpointer", None)


async def test_every_state_model_survives_a_strict_checkpoint_round_trip(monkeypatch):
    """
    Runs a whole pause -> checkpoint -> resume cycle through a serializer in STRICT mode, which
    blocks any type not explicitly allowed. If a state model were missing from `state_serde()`,
    this would fail (and so would the first LangGraph upgrade that makes strict the default).
    """
    import tempfile

    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.types import Command

    from app.db.checkpointer import state_serde
    from app.graph.build import build_graph
    from app.graph.state import BudgetState, MigrationPhase, TestOutcome
    from tests.test_comprehension_node import FakeExtractor, _write_spring_repo
    from tests.test_graph_integration import FakeCodeGenerator, FakeSandboxRunner, _base_config
    from tests.test_pr_publish_node import FakePublisher

    monkeypatch.setenv("LANGGRAPH_STRICT_MSGPACK", "true")
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
        _write_spring_repo(src)
        publisher = FakePublisher()
        graph = build_graph(
            checkpointer=MemorySaver(serde=state_serde()),
            extractor=FakeExtractor(),
            code_generator=FakeCodeGenerator(),
            sandbox_runner=FakeSandboxRunner(outcome=TestOutcome.PASSED),
            pr_publisher=publisher,
        )
        config = _base_config(src, dst)
        config.update(github_target_repo="acme/orders-api", require_human_approval_before_pr=True)
        thread = {"configurable": {"thread_id": "strict"}}

        await graph.ainvoke({"config": config, "budget": BudgetState(max_usd=10.0)}, thread)
        snapshot = await graph.aget_state(thread)
        assert isinstance(snapshot.values["budget"], BudgetState)  # reconstructed as the real class, not a dict

        final = await graph.ainvoke(Command(resume={"approved": True}), thread)
        assert final["phase"] == MigrationPhase.DONE and len(publisher.calls) == 1
