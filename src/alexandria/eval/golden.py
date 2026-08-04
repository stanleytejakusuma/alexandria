"""Strict parsing and target validation for private golden retrieval sets."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .jsonl_records import load_jsonl_records

__all__ = ["GoldenEntry", "load_golden", "verify_targets"]


# NoLiMa-style diagnostic stratification: tagging each query by how much it lexically
# overlaps its target turns one aggregate recall number into a slice that can actually
# localize a regression ("zero-overlap recall dropped" is far more actionable than
# "recall dropped"). provenance separates hand-written entries from LLM-assisted ones,
# so a future audit of the golden set itself can tell which entries carry a human's
# direct judgment versus a retriever-assisted acceptance.
OVERLAP_BANDS = frozenset({"literal", "partial", "zero"})
PROVENANCE_VALUES = frozenset({"hand", "assisted"})


@dataclass(frozen=True)
class GoldenEntry:
    """One query and its valid (ANY-OF) target document ids."""

    id: str
    query: str
    must_retrieve: tuple[str, ...]
    k: int
    note: str | None = None
    overlap_band: str | None = None
    provenance: str | None = None


_FIELDS = {"id", "query", "must_retrieve", "k", "note", "overlap_band", "provenance"}
_REQUIRED_FIELDS = {"id", "query", "must_retrieve", "k"}


def load_golden(path: str | Path) -> list[GoldenEntry]:
    """Load a golden JSONL file, rejecting every malformed row with its line number."""
    return load_jsonl_records(path, _parse_entry, lambda e: e.id)


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
    overlap_band = raw.get("overlap_band")
    provenance = raw.get("provenance")
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
    if overlap_band is not None and overlap_band not in OVERLAP_BANDS:
        raise ValueError(f"golden line {line_number}: overlap_band must be one of "
                         f"{sorted(OVERLAP_BANDS)}, got {overlap_band!r}")
    if provenance is not None and provenance not in PROVENANCE_VALUES:
        raise ValueError(f"golden line {line_number}: provenance must be one of "
                         f"{sorted(PROVENANCE_VALUES)}, got {provenance!r}")
    return GoldenEntry(entry_id, query, tuple(targets), k, note, overlap_band, provenance)
