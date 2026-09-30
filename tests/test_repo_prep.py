"""
Unlike every other external-service test in this project, this one is
NOT behind a fake — it does a real, tiny, shallow `git clone` against a
small public repo. Worth doing for real here specifically because the
whole point of GitRepoPreparer is "did we get the clone invocation
right" (branch/ref handling, destination path, error surfacing) —
faking `git.Repo.clone_from` would just test that we called a mock
correctly, not that the real clone semantics work. Kept fast with
`depth=1` and a near-empty repo.
"""

from __future__ import annotations

import os
import shutil
import tempfile

import pytest

from app.services.repo_prep import GitRepoPreparer

REAL_TEST_REPO = "https://github.com/octocat/Hello-World.git"


@pytest.mark.asyncio
async def test_prepare_source_clones_a_real_repo():
    base = tempfile.mkdtemp()
    try:
        preparer = GitRepoPreparer(base_dir=base)
        path = await preparer.prepare_source(repo_url=REAL_TEST_REPO, ref="master", run_id="run-1")

        assert os.path.isdir(path)
        assert os.path.isfile(os.path.join(path, "README"))
        assert path == os.path.join(base, "run-1", "source")
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.mark.asyncio
async def test_prepare_target_with_repo_url_clones_it():
    base = tempfile.mkdtemp()
    try:
        preparer = GitRepoPreparer(base_dir=base)
        path = await preparer.prepare_target(repo_url=REAL_TEST_REPO, target_stack="fastapi", run_id="run-2")

        assert os.path.isfile(os.path.join(path, "README"))
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.mark.asyncio
async def test_prepare_target_without_repo_url_scaffolds_fastapi_skeleton():
    base = tempfile.mkdtemp()
    try:
        preparer = GitRepoPreparer(base_dir=base)
        path = await preparer.prepare_target(repo_url=None, target_stack="fastapi", run_id="run-3")

        assert os.path.isfile(os.path.join(path, "requirements.txt"))
        assert os.path.isfile(os.path.join(path, "app", "main.py"))
        assert os.path.isfile(os.path.join(path, "tests", "test_health.py"))
        with open(os.path.join(path, "requirements.txt")) as f:
            assert "fastapi" in f.read()
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.mark.asyncio
async def test_prepare_target_without_repo_url_and_unknown_stack_raises():
    base = tempfile.mkdtemp()
    try:
        preparer = GitRepoPreparer(base_dir=base)
        with pytest.raises(ValueError, match="No scaffold available"):
            await preparer.prepare_target(repo_url=None, target_stack="cobol-cics", run_id="run-4")
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.mark.asyncio
async def test_concurrent_runs_get_isolated_directories():
    base = tempfile.mkdtemp()
    try:
        preparer = GitRepoPreparer(base_dir=base)
        path_a = await preparer.prepare_target(repo_url=None, target_stack="fastapi", run_id="run-a")
        path_b = await preparer.prepare_target(repo_url=None, target_stack="fastapi", run_id="run-b")
        assert path_a != path_b
        assert os.path.isdir(path_a) and os.path.isdir(path_b)
    finally:
        shutil.rmtree(base, ignore_errors=True)
