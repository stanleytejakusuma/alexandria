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


def deleted_flag(value: Any) -> str:
    """Normalise any write-time representation of the soft-delete flag to the
    canonical stored string ("true" / "false"), defaulting missing or
    unrecognised input to NOT deleted.

    This is the WRITE-time default, and it is deliberately more permissive
    than the READ-time predicate in `not_deleted_clause` below: almost every
    chunk record that ever passes through here never touched deletion at all
    (`chunk_doc_records`/`doc_frontmatter_metadata` in index/chunker.py is the
    normal source and always supplies a real bool), so defaulting an absent
    value to "false" cannot hide content nobody asked to delete. A read-time
    default of "true" for an unrecognised value would do the opposite --
    that asymmetry is intentional, not an oversight (see ARC-BRIEF's "fail
    closed" requirement and `not_deleted_clause`'s docstring).

    String input is compared case-insensitively against the literal "true"
    so a record round-tripped back out of the store (`get`/`get_many` return
    the stored string, not a bool) and passed back through `upsert` without
    modification stores the same value it already had, instead of Python
    truthiness flipping the stored string "false" (truthy!) into "true".
    """
    if isinstance(value, str):
        return "true" if value.strip().lower() == "true" else "false"
    return "true" if value else "false"


def not_deleted_clause(alias: str = "") -> str:
    """The one predicate fragment every retrieval path ANDs onto its query,
    so a tombstoned chunk can never surface through one leg (dense or
    lexical) while staying hidden on the other -- both `index/store.py`'s
    `search_vector` and `index/bm25.py`'s `search` call this, never a
    hand-written copy of the same string.

    Positive allow-list (`= 'false'`), not a negated deny-list (`!= 'true'`):
    a row whose `deleted` column is NULL, corrupted, or written by code that
    predates this column is EXCLUDED, not shown. That is "fail closed" per
    ARC-BRIEF: a document someone believed was deleted staying invisible when
    its flag can't be positively confirmed is a far smaller failure than a
    tombstone silently reappearing. (In SQL specifically, `NULL = 'false'`
    and `NULL != 'true'` are equally NULL/excluded -- the two forms only
    diverge for a non-null value that is neither "true" nor "false", which is
    exactly the unreadable/malformed case this guards against.)
    """
    prefix = f"{alias}." if alias else ""
    return f"{prefix}deleted = 'false'"


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
