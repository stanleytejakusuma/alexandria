"""Strict parsing and target validation for phase-2 seeded contradiction pairs.

Ground truth for Judge 3 (gather-completeness for CONTRA-SCAN,
`docs/SPEC-phase2-eval.md`): each pair is two real corpus documents that
genuinely contradict, correct, or supersede one another. The test this
enables lives in phase-2 code, not here -- given a synthesis target that
cites claim_a, does the gather stage's candidate pool surface claim_b? This
module only owns strict loading and on-disk verification of the pairs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .jsonl_records import load_jsonl_records

__all__ = [
    "ContradictionPairEntry",
    "load_contradiction_golden",
    "verify_contradiction_targets",
]

RELATIONSHIP_VALUES = frozenset({"contradicts", "corrects", "supersedes"})
PROVENANCE_VALUES = frozenset({"hand", "assisted"})


@dataclass(frozen=True)
class ContradictionPairEntry:
    """A query that should surface both members of a genuinely-contradicting pair."""

    id: str
    query: str
    claim_a: str
    claim_b: str
    relationship: str
    note: str | None
    provenance: str


_FIELDS = {"id", "query", "claim_a", "claim_b", "relationship", "note", "provenance"}
_REQUIRED_FIELDS = {"id", "query", "claim_a", "claim_b", "relationship", "provenance"}


def load_contradiction_golden(path: str | Path) -> list[ContradictionPairEntry]:
    """Load a contradiction-pairs JSONL file, rejecting every malformed row."""
    return load_jsonl_records(path, _parse_entry, lambda e: e.id)


def verify_contradiction_targets(entries: list[ContradictionPairEntry], corpus_path: str | Path) -> list[str]:
    """Return pair ids where claim_a or claim_b is missing from the corpus."""
    corpus = Path(corpus_path)
    existing = {
        path.relative_to(corpus).with_suffix("").as_posix()
        for path in corpus.rglob("*.md")
        if path.is_file()
    } if corpus.exists() else set()
    return [entry.id for entry in entries if entry.claim_a not in existing or entry.claim_b not in existing]


def _parse_entry(raw: object, line_number: int) -> ContradictionPairEntry:
    if not isinstance(raw, dict):
        raise ValueError(f"line {line_number}: entry must be a JSON object")
    unknown = set(raw) - _FIELDS
    if unknown:
        raise ValueError(f"line {line_number}: unknown field(s): {', '.join(sorted(unknown))}")
    missing = _REQUIRED_FIELDS - set(raw)
    if missing:
        raise ValueError(f"line {line_number}: missing field(s): {', '.join(sorted(missing))}")

    entry_id = raw["id"]
    query = raw["query"]
    claim_a = raw["claim_a"]
    claim_b = raw["claim_b"]
    relationship = raw["relationship"]
    note = raw.get("note")
    provenance = raw["provenance"]

    if not isinstance(entry_id, str) or not entry_id:
        raise ValueError(f"line {line_number}: id must be a non-empty string")
    if not isinstance(query, str) or not query:
        raise ValueError(f"line {line_number}: query must be a non-empty string")
    if not isinstance(claim_a, str) or not claim_a:
        raise ValueError(f"line {line_number}: claim_a must be a non-empty string")
    if not isinstance(claim_b, str) or not claim_b:
        raise ValueError(f"line {line_number}: claim_b must be a non-empty string")
    if claim_a == claim_b:
        raise ValueError(f"line {line_number}: claim_a and claim_b must be distinct documents")
    if relationship not in RELATIONSHIP_VALUES:
        raise ValueError(f"line {line_number}: relationship must be one of "
                         f"{sorted(RELATIONSHIP_VALUES)}, got {relationship!r}")
    if note is not None and not isinstance(note, str):
        raise ValueError(f"line {line_number}: note must be a string when present")
    if provenance not in PROVENANCE_VALUES:
        raise ValueError(f"line {line_number}: provenance must be one of "
                         f"{sorted(PROVENANCE_VALUES)}, got {provenance!r}")

    return ContradictionPairEntry(entry_id, query, claim_a, claim_b, relationship, note, provenance)
