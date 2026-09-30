"""API-facing request/response models — deliberately separate from the internal
MigrationRunConfig TypedDict, since the API surface and the graph's internal
state schema have different concerns (the caller never supplies
local_checkout_path/target_workspace_path, for instance — those are computed
server-side by repo_prep)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class MigrationRequest(BaseModel):
    source_repo_url: str
    source_ref: str = "main"
    source_stack: str = Field(description="Source adapter registry key, e.g. 'springboot'")
    target_stack: str = Field(description="Target adapter registry key, e.g. 'fastapi'")
    scope_description: str = Field(description="e.g. 'migrate the OrderController module only'")
    github_target_repo: str | None = Field(default=None, description="'owner/repo' to open the PR against")
    target_repo_url: str | None = Field(
        default=None, description="Base checkout for the target workspace; omit to scaffold a fresh skeleton"
    )
    require_human_approval_before_pr: bool = True
    max_usd: float = 5.0
    max_retry_rounds: int = 5


class MigrationSubmitResponse(BaseModel):
    run_id: str
    status: str = "queued"


class TaskSummary(BaseModel):
    id: str
    status: str
    target_files: list[str]
    retry_count: int


class PRDraftSummary(BaseModel):
    title: str
    branch_name: str


class MigrationStatusResponse(BaseModel):
    run_id: str
    phase: str | None
    tasks: list[TaskSummary]
    budget_spent_usd: float | None
    budget_max_usd: float | None
    full_suite_outcome: str | None
    pr_draft: PRDraftSummary | None
    pr_url: str | None
    error_log_tail: list[str]


class ApproveResponse(BaseModel):
    run_id: str
    phase: str
    pr_url: str | None
