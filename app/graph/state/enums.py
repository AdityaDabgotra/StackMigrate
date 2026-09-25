"""
Shared enums for the migration graph state.

Kept in one file so every node/adapter imports the same vocabulary —
avoids the classic bug where one module uses "PASSED" and another uses
"PASS" for the same concept.
"""

from enum import Enum


class SemanticUnitKind(str, Enum):
    """
    The unit of work the planner decomposes a codebase into.
    Deliberately framework-agnostic — a "unit" is a concept, not a file.
    """

    ENDPOINT = "endpoint"          # a single route/controller action
    SERVICE = "service"            # business-logic layer (Spring @Service, FastAPI service fn)
    DATA_MODEL = "data_model"      # entity/DTO/schema
    REPOSITORY = "repository"      # data access layer
    MIDDLEWARE = "middleware"      # auth filters, interceptors, guards
    CONFIG = "config"              # app wiring, DI config, env setup
    UTILITY = "utility"            # shared helpers with no framework coupling


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRYING = "retrying"
    SKIPPED = "skipped"            # e.g. dependency failed, so this was never attempted
    NEEDS_HUMAN = "needs_human"    # exhausted retries, escalated


class TestOutcome(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"                # test runner itself crashed (env/build issue, not assertion failure)
    TIMEOUT = "timeout"
    NOT_RUN = "not_run"


class MigrationPhase(str, Enum):
    """Top-level graph phase — used for routing and for human-readable progress reporting."""

    COMPREHENSION = "comprehension"
    SYNTHESIS_PLANNING = "synthesis_planning"
    EDITING = "editing"
    TESTING = "testing"
    ERROR_ANALYSIS = "error_analysis"
    AGGREGATING = "aggregating"
    AWAITING_APPROVAL = "awaiting_approval"
    PR_GENERATION = "pr_generation"
    DONE = "done"
    FAILED = "failed"
    ABORTED_BUDGET = "aborted_budget"
