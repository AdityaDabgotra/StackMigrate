"""
PR publisher interface. Same pattern as every other external collaborator
in this project (Extractor, CodeGenerator, ErrorAnalyzer, SandboxRunner):
a Protocol production code depends on, a real implementation behind a
lazy import, and a fake for tests — so the orchestration logic (who
gets called, when, with what) is fully testable without live GitHub
credentials or network access.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.graph.state import FileDiff


@dataclass
class PublishedPR:
    url: str
    branch_name: str
    commit_sha: str


class PRPublisher(Protocol):
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
    ) -> PublishedPR: ...
