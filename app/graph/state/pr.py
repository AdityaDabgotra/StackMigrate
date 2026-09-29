"""Pull-request draft content, produced by the aggregation node and consumed by pr_publish."""

from __future__ import annotations

from pydantic import BaseModel


class PRDraft(BaseModel):
    title: str
    body: str
    branch_name: str
    commit_message: str
