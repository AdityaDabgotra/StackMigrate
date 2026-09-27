"""
Synthesis node.

Deterministic by design — no LLM call, no cost, no flakiness. Its job
is purely structural: decide which target file each IR unit's migrated
code belongs in, group units that share a target file into one
MigrationTask (so two parallel editor branches never race to write the
same file), and translate the IR's unit-level dependency graph
(`SemanticUnit.depends_on_unit_ids`) into a task-level dependency graph
(`MigrationTask.depends_on_task_ids`) that the editor fanout's
scheduling (`app/graph/routing.py`) respects.

The actual code-generation LLM call happens in the Editor node, not
here — see app/graph/nodes/editor.py.
"""

from __future__ import annotations

from collections import defaultdict

from app.adapters.target_registry import get_target_synthesis_adapter
from app.adapters.target_synthesis_base import TargetSynthesisAdapter
from app.core.ir_describe import describe_unit
from app.core.text import slugify
from app.graph.state import ComprehensionResult, MigrationPhase, MigrationTask, TargetFileSpec


def _build_instructions(units: list, adapter: TargetSynthesisAdapter) -> str:
    kind = units[0].kind
    lines = [adapter.convention_notes(kind), "", "Unit(s) to implement in this file:"]
    for unit in units:
        lines.append(f"- {describe_unit(unit)}")
    return "\n".join(lines)


def run_synthesis(
    comprehension: ComprehensionResult, target_stack: str, adapter: TargetSynthesisAdapter | None = None
) -> list[MigrationTask]:
    adapter = adapter or get_target_synthesis_adapter(target_stack)

    path_to_units: dict[str, list] = defaultdict(list)
    unit_to_path: dict[str, str] = {}
    for unit in comprehension.units:
        path = adapter.suggest_target_path(unit)
        path_to_units[path].append(unit)
        unit_to_path[unit.id] = path

    path_to_task_id = {path: f"task::{slugify(path)}" for path in path_to_units}
    unit_to_task_id = {unit.id: path_to_task_id[unit_to_path[unit.id]] for unit in comprehension.units}

    tasks: list[MigrationTask] = []
    for path, units in path_to_units.items():
        task_id = path_to_task_id[path]
        depends_on_task_ids = sorted(
            {
                unit_to_task_id[dep_id]
                for unit in units
                for dep_id in unit.depends_on_unit_ids
                if dep_id in unit_to_task_id and unit_to_task_id[dep_id] != task_id
            }
        )
        names = [getattr(u.payload, "name", u.id) for u in units]
        tasks.append(
            MigrationTask(
                id=task_id,
                source_unit_ids=[u.id for u in units],
                target_files=[TargetFileSpec(path=path, purpose=f"{units[0].kind.value}: {', '.join(names)}")],
                depends_on_task_ids=depends_on_task_ids,
                synthesis_instructions=_build_instructions(units, adapter),
            )
        )

    return tasks


def build_synthesis_node():
    async def synthesis_node(state: dict) -> dict:
        comprehension = state.get("comprehension")
        if comprehension is None or not comprehension.units:
            return {
                "phase": MigrationPhase.FAILED,
                "error_log": ["Synthesis skipped: no comprehension result (or zero units) to plan from."],
            }

        tasks = run_synthesis(comprehension, state["config"]["target_stack"])
        return {"tasks": tasks, "phase": MigrationPhase.EDITING}

    return synthesis_node
