"""Small, dependency-free text utilities shared across nodes."""

from __future__ import annotations

import re


def slugify(name: str) -> str:
    """'CreateOrderRequest' -> 'create_order_request'. Used to build stable SemanticUnit ids."""
    s = re.sub(r"(?<!^)(?=[A-Z])", "_", name)  # camelCase -> camel_Case
    s = re.sub(r"[^A-Za-z0-9]+", "_", s)
    return s.strip("_").lower()


def find_capitalized_tokens(text: str) -> list[str]:
    """
    Best-effort extraction of likely type/class-name references from free
    text, e.g. "has many OrderItem" -> ["OrderItem"]. Used for the weak
    dependency resolution on DataModelUnit.relationships — deliberately
    conservative (see comprehension node docstring for why this is a
    best-effort pass, not a hard requirement).
    """
    return re.findall(r"\b[A-Z][A-Za-z0-9]{2,}\b", text)
