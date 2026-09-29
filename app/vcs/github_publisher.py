"""
Real GitHub-backed PR publisher, via PyGithub.

Flow (the standard way to commit multiple files in one commit through
the GitHub API — there is no "upload several files" endpoint, only
git's own tree/commit/ref primitives):
  1. Read the base branch's current commit + tree.
  2. Build a new tree containing the migrated files on top of the base tree.
  3. Create a commit pointing at the new tree, with the base commit as parent.
  4. Create a new branch ref pointing at that commit.
  5. Open a PR from the new branch into the base branch.

Every file path is re-validated with `publishable_path_problem` right
before being sent to the API — this is intentionally a second check
(the aggregation node already validates), because this function is the
last line of defense before anything reaches a real repository, and a
future caller might construct a `GitHubPRPublisher` call directly
without going through the aggregation node's checks.
"""

from __future__ import annotations

import os

from app.graph.state import FileDiff
from app.vcs.interface import PublishedPR
from app.vcs.paths import publishable_path_problem


class UnpublishablePathError(ValueError):
    pass


class GitHubPRPublisher:
    def __init__(self, token: str | None = None) -> None:
        self._token = token or os.environ.get("GITHUB_TOKEN")
        self._client = None

    def _get_client(self):
        if self._client is None:
            from github import Github  # lazy import: only needed when actually publishing

            if not self._token:
                raise RuntimeError("GITHUB_TOKEN is not set and no token was passed to GitHubPRPublisher.")
            self._client = Github(self._token)
        return self._client

    def publish(
        self,
        *,
        repo_full_name: str,
        base_branch: str,
        branch_name: str,
        commit_message: str,
        pr_title: str,
        pr_body: str,
        files: list[FileDiff],
    ) -> PublishedPR:
        for f in files:
            problem = publishable_path_problem(f.path)
            if problem:
                raise UnpublishablePathError(f"Refusing to publish '{f.path}': {problem}")

        from github import InputGitTreeElement

        client = self._get_client()
        repo = client.get_repo(repo_full_name)

        base_ref = repo.get_git_ref(f"heads/{base_branch}")
        base_commit = repo.get_git_commit(base_ref.object.sha)

        tree_elements = [
            InputGitTreeElement(path=f.path, mode="100644", type="blob", content=f.content) for f in files
        ]
        new_tree = repo.create_git_tree(tree_elements, base_tree=base_commit.tree)
        new_commit = repo.create_git_commit(message=commit_message, tree=new_tree, parents=[base_commit])

        repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=new_commit.sha)

        pr = repo.create_pull(title=pr_title, body=pr_body, head=branch_name, base=base_branch)

        return PublishedPR(url=pr.html_url, branch_name=branch_name, commit_sha=new_commit.sha)
