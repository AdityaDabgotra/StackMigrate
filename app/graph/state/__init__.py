"""
Public surface of the state package. Import from here (`app.graph.state`)
in node implementations, not from the individual submodules — this file
is the contract; submodule layout can change freely underneath it.
"""

from app.graph.state.budget import BudgetState
from app.graph.state.enums import (
    MigrationPhase,
    SemanticUnitKind,
    TaskStatus,
    TestOutcome,
)
from app.graph.state.graph_state import GraphState, MigrationRunConfig
from app.graph.state.ir import (
    ComprehensionResult,
    DataModelUnit,
    EndpointUnit,
    FieldSpec,
    MiddlewareUnit,
    ParameterSpec,
    RepositoryUnit,
    SemanticUnit,
    ServiceUnit,
)
from app.graph.state.tasks import (
    ErrorAnalysis,
    EditorResult,
    FileDiff,
    MigrationTask,
    TargetFileSpec,
    TestResult,
)

__all__ = [
    "BudgetState",
    "MigrationPhase",
    "SemanticUnitKind",
    "TaskStatus",
    "TestOutcome",
    "GraphState",
    "MigrationRunConfig",
    "ComprehensionResult",
    "DataModelUnit",
    "EndpointUnit",
    "FieldSpec",
    "MiddlewareUnit",
    "ParameterSpec",
    "RepositoryUnit",
    "SemanticUnit",
    "ServiceUnit",
    "ErrorAnalysis",
    "EditorResult",
    "FileDiff",
    "MigrationTask",
    "TargetFileSpec",
    "TestResult",
]
