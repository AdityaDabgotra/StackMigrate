from __future__ import annotations

import importlib
import sys

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from app.api.app import create_app
from app.api.auth import load_api_key
from app.graph.build import build_graph
from tests.test_api import FakeTaskSubmitter, _request_payload

KEY = "s3cret-key"


def _client(api_key: str | None) -> tuple[TestClient, FakeTaskSubmitter]:
    submitter = FakeTaskSubmitter()
    app = create_app(graph=build_graph(checkpointer=MemorySaver()), task_submitter=submitter, api_key=api_key)
    return TestClient(app), submitter


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/migrations"),
        ("get", "/migrations/run-x"),
        ("post", "/migrations/run-x/approve"),
        ("post", "/migrations/run-x/reject"),
    ],
)
def test_every_migration_route_rejects_missing_and_wrong_keys(method, path):
    client, submitter = _client(KEY)
    kwargs = {"json": _request_payload()} if path == "/migrations" else {}

    assert getattr(client, method)(path, **kwargs).status_code == 401
    assert getattr(client, method)(path, headers={"X-API-Key": "wrong"}, **kwargs).status_code == 401
    assert submitter.calls == []  # an unauthenticated submit never reaches the queue


def test_correct_key_is_accepted():
    client, submitter = _client(KEY)

    response = client.post("/migrations", json=_request_payload(), headers={"X-API-Key": KEY})

    assert response.status_code == 202 and len(submitter.calls) == 1
    # an authenticated approve on an unknown run reaches the handler (404), not the auth layer
    assert client.post("/migrations/nope/approve", headers={"X-API-Key": KEY}).status_code == 404


def test_healthz_is_open_so_orchestrators_can_probe_it():
    client, _ = _client(KEY)
    assert client.get("/healthz").json() == {"status": "ok"}


def test_no_key_configured_means_auth_off_for_tests():
    client, _ = _client(None)
    assert client.post("/migrations", json=_request_payload()).status_code == 202


def test_load_api_key_fails_closed_unless_explicitly_opted_out():
    assert load_api_key({"STACKMIGRATE_API_KEY": "k"}) == "k"
    assert load_api_key({"STACKMIGRATE_ALLOW_NO_AUTH": "1"}) is None
    with pytest.raises(RuntimeError, match="STACKMIGRATE_API_KEY"):
        load_api_key({})
    with pytest.raises(RuntimeError):
        load_api_key({"STACKMIGRATE_API_KEY": "", "STACKMIGRATE_ALLOW_NO_AUTH": "0"})


def test_production_entrypoint_refuses_to_boot_without_a_key(monkeypatch):
    monkeypatch.delenv("STACKMIGRATE_API_KEY", raising=False)
    monkeypatch.delenv("STACKMIGRATE_ALLOW_NO_AUTH", raising=False)
    sys.modules.pop("app.main", None)
    with pytest.raises(RuntimeError, match="STACKMIGRATE_API_KEY"):
        importlib.import_module("app.main")

    monkeypatch.setenv("STACKMIGRATE_API_KEY", KEY)
    sys.modules.pop("app.main", None)
    assert importlib.import_module("app.main").app is not None
    sys.modules.pop("app.main", None)
