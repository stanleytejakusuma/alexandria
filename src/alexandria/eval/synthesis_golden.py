"""Strict parsing and target validation for phase-2 synthesis golden clusters.

Ground truth for Judge 2 (coverage, `docs/SPEC-phase2-eval.md`): each cluster
is a topic a synthesized wiki page would cover, hand-enumerated with the
load-bearing facts a good synthesis of it MUST include. Coverage is measured
as load-bearing-fact recall per page -- a fact missing from every draft is a
real miss, not a stylistic choice, which is exactly why the set has to be
exhaustive per cluster rather than "a few examples."
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .jsonl_records import load_jsonl_records

__all__ = [
    "LoadBearingFact",
    "SynthesisClusterEntry",
    "load_synthesis_golden",
    "verify_synthesis_targets",
]

PROVENANCE_VALUES = frozenset({"hand", "assisted"})


@dataclass(frozen=True)
class LoadBearingFact:
    """One fact a good synthesis of the cluster's topic must not omit."""

    id: str
    text: str
    supported_by: tuple[str, ...]


@dataclass(frozen=True)
class SynthesisClusterEntry:
    """One topic cluster: its source pool and the facts synthesis must cover."""

    id: str
    topic: str
    source_docs: tuple[str, ...]
    load_bearing_facts: tuple[LoadBearingFact, ...]
    provenance: str


_CLUSTER_FIELDS = {"id", "topic", "source_docs", "load_bearing_facts", "provenance"}
_FACT_FIELDS = {"id", "text", "supported_by"}


def load_synthesis_golden(path: str | Path) -> list[SynthesisClusterEntry]:
    """Load a synthesis golden JSONL file, rejecting every malformed row."""
    return load_jsonl_records(path, _parse_entry, lambda e: e.id)


def verify_synthesis_targets(entries: list[SynthesisClusterEntry], corpus_path: str | Path) -> list[str]:
    """Return cluster ids where a source doc or a fact's supported_by doc is missing."""
    corpus = Path(corpus_path)
    existing = {
        path.relative_to(corpus).with_suffix("").as_posix()
        for path in corpus.rglob("*.md")
        if path.is_file()
    } if corpus.exists() else set()

    missing_ids = []
    for entry in entries:
        docs_to_check = set(entry.source_docs)
        for fact in entry.load_bearing_facts:
            docs_to_check.update(fact.supported_by)
        if any(doc not in existing for doc in docs_to_check):
            missing_ids.append(entry.id)
    return missing_ids


def _parse_entry(raw: object, line_number: int) -> SynthesisClusterEntry:
    if not isinstance(raw, dict):
        raise ValueError(f"line {line_number}: entry must be a JSON object")
    unknown = set(raw) - _CLUSTER_FIELDS
    if unknown:
        raise ValueError(f"line {line_number}: unknown field(s): {', '.join(sorted(unknown))}")
    missing = _CLUSTER_FIELDS - set(raw)
    if missing:
        raise ValueError(f"line {line_number}: missing field(s): {', '.join(sorted(missing))}")

    entry_id = raw["id"]
    topic = raw["topic"]
    source_docs = raw["source_docs"]
    raw_facts = raw["load_bearing_facts"]
    provenance = raw["provenance"]

    if not isinstance(entry_id, str) or not entry_id:
        raise ValueError(f"line {line_number}: id must be a non-empty string")
    if not isinstance(topic, str) or not topic:
        raise ValueError(f"line {line_number}: topic must be a non-empty string")
    if (not isinstance(source_docs, list) or not source_docs
            or not all(isinstance(d, str) and d for d in source_docs)):
        raise ValueError(f"line {line_number}: source_docs must be a non-empty string list")
    if not isinstance(raw_facts, list) or not raw_facts:
        raise ValueError(f"line {line_number}: load_bearing_facts must be a non-empty list")
    if provenance not in PROVENANCE_VALUES:
        raise ValueError(f"line {line_number}: provenance must be one of "
                         f"{sorted(PROVENANCE_VALUES)}, got {provenance!r}")

    source_set = set(source_docs)
    facts: list[LoadBearingFact] = []
    fact_ids: set[str] = set()
    for fact_raw in raw_facts:
        fact = _parse_fact(fact_raw, line_number, source_set)
        if fact.id in fact_ids:
            raise ValueError(f"line {line_number}: duplicate fact id {fact.id!r} in cluster {entry_id!r}")
        fact_ids.add(fact.id)
        facts.append(fact)

    return SynthesisClusterEntry(entry_id, topic, tuple(source_docs), tuple(facts), provenance)


def _parse_fact(raw: object, line_number: int, source_set: set[str]) -> LoadBearingFact:
    if not isinstance(raw, dict):
        raise ValueError(f"line {line_number}: each fact must be a JSON object")
    unknown = set(raw) - _FACT_FIELDS
    if unknown:
        raise ValueError(f"line {line_number}: unknown fact field(s): {', '.join(sorted(unknown))}")
    missing = _FACT_FIELDS - set(raw)
    if missing:
        raise ValueError(f"line {line_number}: fact missing field(s): {', '.join(sorted(missing))}")

    fact_id, text, supported_by = raw["id"], raw["text"], raw["supported_by"]
    if not isinstance(fact_id, str) or not fact_id:
        raise ValueError(f"line {line_number}: fact id must be a non-empty string")
    if not isinstance(text, str) or not text:
        raise ValueError(f"line {line_number}: fact text must be a non-empty string")
    if (not isinstance(supported_by, list) or not supported_by
            or not all(isinstance(d, str) and d for d in supported_by)):
        raise ValueError(f"line {line_number}: fact supported_by must be a non-empty string list")
    unlisted = [d for d in supported_by if d not in source_set]
    if unlisted:
        raise ValueError(f"line {line_number}: fact {fact_id!r} supported_by references doc(s) "
                         f"not in source_docs: {unlisted}")
    return LoadBearingFact(fact_id, text, tuple(supported_by))
