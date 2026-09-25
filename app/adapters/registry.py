"""
Adapter registry. This is the ONE file that has to change when adding a
new source stack — everything else (comprehension node, graph wiring)
looks the adapter up by key and stays untouched.
"""

from __future__ import annotations

from app.adapters.base import SourceAdapter
from app.adapters.springboot.source_adapter import SpringBootSourceAdapter

SOURCE_ADAPTERS: dict[str, SourceAdapter] = {
    "springboot": SpringBootSourceAdapter(),
}


def get_source_adapter(stack_key: str) -> SourceAdapter:
    try:
        return SOURCE_ADAPTERS[stack_key]
    except KeyError:
        available = ", ".join(sorted(SOURCE_ADAPTERS)) or "(none registered)"
        raise ValueError(
            f"No source adapter registered for stack '{stack_key}'. Available: {available}"
        )
