"""Validated metadata filtering shared by lexical and dense retrieval.

The public retrieval API deliberately accepts mappings rather than database-specific
predicate strings. It keeps query text and filter values out of SQL syntax.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

FILTER_FIELDS = frozenset({
    "type", "project", "status", "source", "tags", "entities", "layer", "generated_at",
})
LIST_FIELDS = frozenset({"tags", "entities"})


def normalize_filters(where: Mapping[str, Any] | None) -> dict[str, str]:
    """Validate a metadata filter and return stable string values.

    The current CLI exposes scalar equality filters. Keeping this narrow avoids a
    misleading pseudo-query language and makes both backends apply the same gate.
    """
    if where is None:
        return {}
    if not isinstance(where, Mapping):
        raise TypeError("metadata filters must be a mapping")
    normalized: dict[str, str] = {}
    for field, value in sorted(where.items()):
        if field not in FILTER_FIELDS:
            raise ValueError(f"unsupported metadata filter: {field}")
        if value is None:
            continue
        if not isinstance(value, (str, int, float, bool)):
            raise TypeError(f"metadata filter {field} must be a scalar")
        normalized[field] = str(value)
    return normalized


def sqlite_where(where: Mapping[str, Any] | None, *, alias: str = "") -> tuple[str, list[str]]:
    """Build a parameterized SQLite predicate for validated metadata filters."""
    filters = normalize_filters(where)
    prefix = f"{alias}." if alias else ""
    clauses: list[str] = []
    params: list[str] = []
    for field, value in filters.items():
        if field in LIST_FIELDS:
            clauses.append(f"EXISTS (SELECT 1 FROM json_each({prefix}{field}) WHERE value = ?)")
        else:
            clauses.append(f"{prefix}{field} = ?")
        params.append(value)
    return (" AND ".join(clauses) or "1 = 1"), params


def lancedb_where(where: Mapping[str, Any] | None) -> str | None:
    """Create a safe Lance predicate from a fixed field allow-list.

    Lance currently has no parameter binding API for predicates; field names are
    whitelisted and literal values are JSON quoted, which prevents user text from
    becoming predicate syntax.
    """
    filters = normalize_filters(where)
    clauses = []
    for field, value in filters.items():
        literal = json.dumps(value)
        if field in LIST_FIELDS:
            clauses.append(f"array_has({field}, {literal})")
        else:
            clauses.append(f"{field} = {literal}")
    return " AND ".join(clauses) or None
