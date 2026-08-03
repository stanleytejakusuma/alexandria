"""Strict parsing and target validation for private golden retrieval sets."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

__all__ = ["GoldenEntry", "load_golden", "verify_targets"]


@dataclass(frozen=True)
class GoldenEntry:
    """One query and its valid (ANY-OF) target document ids."""

    id: str
    query: str
    must_retrieve: tuple[str, ...]
    k: int
    note: str | None = None


_FIELDS = {"id", "query", "must_retrieve", "k", "note"}
_REQUIRED_FIELDS = _FIELDS - {"note"}


def load_golden(path: str | Path) -> list[GoldenEntry]:
    """Load a golden JSONL file, rejecting every malformed row with its line number."""
    source = Path(path)
    entries: list[GoldenEntry] = []
    ids: set[str] = set()
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"unable to read golden set {source}: {exc}") from exc

    for line_number, line in enumerate(lines, start=1):
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"golden line {line_number}: malformed JSON ({exc.msg})") from exc
        entry = _parse_entry(raw, line_number)
        if entry.id in ids:
            raise ValueError(f"golden line {line_number}: duplicate id {entry.id!r}")
        ids.add(entry.id)
        entries.append(entry)
    return entries


def verify_targets(entries: list[GoldenEntry], corpus_path: str | Path) -> list[str]:
    """Return golden entry ids with a target document missing from the corpus.

    Target ids deliberately stay extensionless. Existing corpus markdown paths are
    converted to their document ids before comparison, so this does not alter a
    golden target by appending an extension.
    """
    corpus = Path(corpus_path)
    existing = {
        path.relative_to(corpus).with_suffix("").as_posix()
        for path in corpus.rglob("*.md")
        if path.is_file()
    } if corpus.exists() else set()
    return [entry.id for entry in entries if any(target not in existing for target in entry.must_retrieve)]


def _parse_entry(raw: object, line_number: int) -> GoldenEntry:
    if not isinstance(raw, dict):
        raise ValueError(f"golden line {line_number}: entry must be a JSON object")
    unknown = set(raw) - _FIELDS
    if unknown:
        raise ValueError(f"golden line {line_number}: unknown field(s): {', '.join(sorted(unknown))}")
    missing = _REQUIRED_FIELDS - set(raw)
    if missing:
        raise ValueError(f"golden line {line_number}: missing field(s): {', '.join(sorted(missing))}")

    entry_id = raw["id"]
    query = raw["query"]
    targets = raw["must_retrieve"]
    k = raw["k"]
    note = raw.get("note")
    if not isinstance(entry_id, str) or not entry_id:
        raise ValueError(f"golden line {line_number}: id must be a non-empty string")
    if not isinstance(query, str) or not query:
        raise ValueError(f"golden line {line_number}: query must be a non-empty string")
    if (not isinstance(targets, list) or not targets
            or not all(isinstance(target, str) and target for target in targets)):
        raise ValueError(f"golden line {line_number}: must_retrieve must be a non-empty string list")
    if not isinstance(k, int) or isinstance(k, bool) or k < 0:
        raise ValueError(f"golden line {line_number}: k must be a non-negative integer")
    if note is not None and not isinstance(note, str):
        raise ValueError(f"golden line {line_number}: note must be a string when present")
    return GoldenEntry(entry_id, query, tuple(targets), k, note)
