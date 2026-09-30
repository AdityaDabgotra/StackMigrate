"""
Repo preparation: turns a migration request's URLs into the two local
paths the graph actually operates on
(`MigrationRunConfig.local_checkout_path` / `.target_workspace_path`).
Every node built in Steps 2-6 assumed these paths already existed —
this is where they actually get created.

Behind a Protocol, same as every other external collaborator in this
project (Extractor, CodeGenerator, SandboxRunner, PRPublisher): a real
implementation using GitPython behind a lazy import, and a fake for
tests that never touches the network. `GitRepoPreparer` itself is also
exercised directly in tests/test_repo_prep.py (a real clone against a
tiny public repo) — behind-a-Protocol doesn't mean untested, just
tested separately from the orchestration logic that depends on it.
"""

from __future__ import annotations

import asyncio
import os
from typing import Protocol

_TARGET_SCAFFOLDS = {
    "fastapi": {
        "requirements.txt": "fastapi>=0.115.0\nuvicorn[standard]>=0.32.0\npydantic>=2.9.0\npytest>=8.3.0\nhttpx>=0.27.0\n",
        "app/__init__.py": "",
        "app/main.py": (
            "from fastapi import FastAPI\n\n"
            "app = FastAPI()\n\n\n"
            '@app.get("/health")\n'
            "async def health():\n"
            '    return {"status": "ok"}\n'
        ),
        "tests/__init__.py": "",
        "tests/test_health.py": (
            "from fastapi.testclient import TestClient\n\n"
            "from app.main import app\n\n"
            "client = TestClient(app)\n\n\n"
            "def test_health():\n"
            '    response = client.get("/health")\n'
            "    assert response.status_code == 200\n"
        ),
    }
}


class RepoPreparer(Protocol):
    async def prepare_source(self, *, repo_url: str, ref: str, run_id: str) -> str: ...
    async def prepare_target(self, *, repo_url: str | None, target_stack: str, run_id: str) -> str: ...


class GitRepoPreparer:
    """
    Real implementation. Clones happen in a thread executor (GitPython's
    clone is a blocking subprocess call) under `base_dir`, one
    subdirectory per run so concurrent runs never collide.
    """

    def __init__(self, base_dir: str = "/tmp/stackmigrate-runs") -> None:
        self._base_dir = base_dir

    async def prepare_source(self, *, repo_url: str, ref: str, run_id: str) -> str:
        dest = os.path.join(self._base_dir, run_id, "source")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: self._clone(repo_url, ref, dest))
        return dest

    async def prepare_target(self, *, repo_url: str | None, target_stack: str, run_id: str) -> str:
        dest = os.path.join(self._base_dir, run_id, "target")
        if repo_url:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: self._clone(repo_url, None, dest))
            return dest

        os.makedirs(dest, exist_ok=True)
        scaffold = _TARGET_SCAFFOLDS.get(target_stack)
        if scaffold is None:
            raise ValueError(f"No scaffold available for target stack '{target_stack}' and no target_repo_url given.")
        for relative_path, content in scaffold.items():
            full_path = os.path.join(dest, relative_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content)
        return dest

    @staticmethod
    def _clone(repo_url: str, ref: str | None, dest: str) -> None:
        import git  # lazy import: only needed when actually cloning

        os.makedirs(os.path.dirname(dest), exist_ok=True)
        kwargs = {"depth": 1}
        if ref:
            kwargs["branch"] = ref
        git.Repo.clone_from(repo_url, dest, **kwargs)
