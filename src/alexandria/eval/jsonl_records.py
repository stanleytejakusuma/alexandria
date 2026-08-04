"""Shared strict-JSONL parsing for private eval ground-truth files.

One malformed-row story, one duplicate-id story, everywhere eval ground truth
is loaded from disk -- the retrieval golden set, phase-2's synthesis clusters,
and its seeded contradiction pairs all share this shape (id, per-line JSON,
no silent skips) and had no reason to each reinvent it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

__all__ = ["load_jsonl_records"]

T = TypeVar("T")


def load_jsonl_records(
    path: str | Path,
    parse_row: Callable[[object, int], T],
    id_of: Callable[[T], str],
) -> list[T]:
    """Parse a JSONL file, one record type per file, line-numbered on failure."""
    source = Path(path)
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"unable to read {source}: {exc}") from exc

    records: list[T] = []
    seen_ids: set[str] = set()
    for line_number, line in enumerate(lines, start=1):
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"line {line_number}: malformed JSON ({exc.msg})") from exc
        record = parse_row(raw, line_number)
        record_id = id_of(record)
        if record_id in seen_ids:
            raise ValueError(f"line {line_number}: duplicate id {record_id!r}")
        seen_ids.add(record_id)
        records.append(record)
    return records
