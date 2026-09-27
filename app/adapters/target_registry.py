"""Registry for TargetTestAdapters — mirrors app/adapters/registry.py for SourceAdapters."""

from __future__ import annotations

from app.adapters.fast_api_target.test_adapter import FastAPITargetAdapter
from app.adapters.target_test_base import TargetTestAdapter

TARGET_TEST_ADAPTERS: dict[str, TargetTestAdapter] = {
    "fastapi": FastAPITargetAdapter(),
}


def get_target_test_adapter(stack_key: str) -> TargetTestAdapter:
    try:
        return TARGET_TEST_ADAPTERS[stack_key]
    except KeyError:
        available = ", ".join(sorted(TARGET_TEST_ADAPTERS)) or "(none registered)"
        raise ValueError(
            f"No target test adapter registered for stack '{stack_key}'. Available: {available}"
        )
