from __future__ import annotations

from app.vcs.paths import publishable_path_problem


def test_normal_paths_are_fine():
    for p in ["app/routers/orders.py", "requirements.txt", "README.md", "app/models/order.py"]:
        assert publishable_path_problem(p) is None


def test_rejects_absolute_path():
    assert publishable_path_problem("/etc/passwd") is not None


def test_rejects_parent_traversal():
    assert publishable_path_problem("../../etc/cron.d/evil") is not None


def test_rejects_github_workflows_directory():
    assert publishable_path_problem(".github/workflows/deploy.yml") is not None


def test_rejects_dotgit_directory():
    assert publishable_path_problem(".git/hooks/pre-commit") is not None


def test_rejects_gitattributes_and_gitmodules():
    assert publishable_path_problem(".gitattributes") is not None
    assert publishable_path_problem(".gitmodules") is not None


def test_rejects_backslash():
    assert publishable_path_problem("app\\routers\\orders.py") is not None


def test_rejects_unsafe_characters():
    assert publishable_path_problem("app/routers/$(rm -rf).py") is not None


def test_case_insensitive_github_check():
    assert publishable_path_problem(".GitHub/workflows/x.yml") is not None
