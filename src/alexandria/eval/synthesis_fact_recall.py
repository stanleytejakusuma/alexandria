"""Golden page-to-fact recall evaluator (WORK-ORDER-phase2-fact-recall-eval.md).

Phase-2's golden-synthesis gate (docs/SPEC-phase2-eval.md) is load-bearing-fact
recall >= 90%. The runtime coverage judge (`coverage.py`) cannot certify that
gate: it is scoped by design to one question -- does an uncited, skipped chunk
contradict or materially qualify a claim the page makes? It never asks whether
the page expressed every load-bearing proposition inside evidence it DID cite.

This module is the missing instrument, and it is evaluation-only: it grades an
already-produced page against hand-curated golden facts with two independent
model-family graders, and it does not touch the runtime pipeline, its prompts,
retrieval, or coverage.py. Design decisions are load-bearing (independent
review, 2026-08-05):

1. Grade the rendered reader-visible page body, not the internal structured
   claims object -- otherwise we silently measure an easier, different quantity.
2. Every `covered: true` verdict must carry a quoted page evidence span, or a
   grader can hallucinate coverage un-auditably.
3. Do not gate on blind strict-AND consensus alone: each grader's recall,
   per-fact agreement/disagreement, and a conservative consensus recall are
   all reported, and every disagreement is listed for manual adjudication.
4. Every consensus-miss is joined against the captured gather output
   deterministically, separating "evidence never gathered" (retrieval failure)
   from "evidence gathered but the page omitted it" (writer/repair failure).
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from ..corpus import split_frontmatter
from ..llm import LLMError
from .synthesis_golden import LoadBearingFact, SynthesisClusterEntry

__all__ = [
    "GATE_THRESHOLD",
    "GRADER_SYSTEM",
    "ClusterFactRecall",
    "FactRecallAgreement",
    "FactRecallReport",
    "FactRecallResult",
    "FactVerdict",
    "build_fact_recall_prompt",
    "classify_miss",
    "grade_fact_recall",
    "grade_fact_recall_twice",
    "parse_fact_recall_response",
    "passes_gate",
    "run_fact_recall_eval",
]

GATE_THRESHOLD = 0.90

GRADER_SYSTEM = """You are grading whether a generated knowledge page covers a fixed set of
hand-curated facts. Grade only what a reader sees in the page text: the prose
and claims exactly as rendered. A citation alone is not coverage. Do not infer
unstated facts. A general statement counts only if it preserves every
load-bearing qualifier of the fact (actor, event, cause, chronology, numeric
threshold, outcome).

Return ONLY valid JSON with exactly this shape:
{"facts":[
  {"id":"<fact id>","covered":true,"evidence":"<verbatim page span>"} |
  {"id":"<fact id>","covered":false,"evidence":""}
]}
Every fact id must appear exactly once. For covered facts the evidence field
must quote the exact page span that states the fact; for uncovered facts it
must be an empty string."""


@dataclass(frozen=True)
class FactVerdict:
    fact_id: str
    covered: bool
    evidence: str          # verbatim quoted page span when covered, else ""
    error: str | None = None


@dataclass(frozen=True)
class FactRecallResult:
    model: str
    verdicts: tuple[FactVerdict, ...]
    recall: float
    errors: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return self.recall >= GATE_THRESHOLD and not self.errors


@dataclass(frozen=True)
class FactRecallAgreement:
    result_a: FactRecallResult
    result_b: FactRecallResult
    consensus_covered: tuple[str, ...]
    contested_ids: tuple[str, ...]
    consensus_recall: float
    union_recall: float

    @property
    def passed(self) -> bool:
        return (self.consensus_recall >= GATE_THRESHOLD
                and not self.result_a.errors and not self.result_b.errors)


def build_fact_recall_prompt(page_text: str,
                             facts: Sequence[LoadBearingFact]) -> tuple[str, str]:
    """Return (system, user) for one fact-recall grading call."""
    golden = json.dumps(
        [{"id": f.id, "text": f.text} for f in facts],
        ensure_ascii=False, indent=2,
    )
    user = (f"<golden_facts>\n{golden}\n</golden_facts>\n\n"
            f"<page>\n{page_text}\n</page>")
    return GRADER_SYSTEM, user


def parse_fact_recall_response(raw: str, expected_ids: tuple[str, ...]) -> tuple[FactVerdict, ...]:
    """Strictly parse one grader response. ANY violation raises LLMError -- an
    eval that cannot fail correctly is worse than no eval (never partial accept)."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMError(f"fact-recall grader returned invalid JSON: {exc}") from exc
    facts = payload.get("facts") if isinstance(payload, dict) else None
    if not isinstance(facts, list):
        raise LLMError("fact-recall grader response missing 'facts' list")

    expected = set(expected_ids)
    seen: set[str] = set()
    verdicts: list[FactVerdict] = []
    for row in facts:
        if not isinstance(row, dict):
            raise LLMError("fact-recall grader returned a non-object fact row")
        missing = {"id", "covered"} - set(row)
        if missing:
            raise LLMError(f"fact-recall grader fact row missing field(s): {sorted(missing)}")
        fid, covered, evidence = row["id"], row["covered"], row.get("evidence", "")
        if not isinstance(fid, str):
            raise LLMError("fact-recall grader fact id is not a string")
        if fid not in expected:
            raise LLMError(f"fact-recall grader returned unknown fact id {fid!r}")
        if fid in seen:
            raise LLMError(f"fact-recall grader returned duplicate fact id {fid!r}")
        seen.add(fid)
        if not isinstance(covered, bool):
            raise LLMError(f"fact-recall grader covered for {fid!r} is not a boolean")
        if not isinstance(evidence, str):
            raise LLMError(f"fact-recall grader evidence for {fid!r} is not a string")
        if covered and not evidence.strip():
            raise LLMError(f"fact-recall grader marked {fid!r} covered without evidence")
        if not covered and evidence.strip():
            raise LLMError(f"fact-recall grader marked {fid!r} not covered but supplied evidence")
        verdicts.append(FactVerdict(fid, covered, evidence))

    missing_ids = sorted(expected - seen)
    if missing_ids:
        raise LLMError(f"fact-recall grader omitted fact id(s): {missing_ids}")

    by_id = {v.fact_id: v for v in verdicts}
    return tuple(by_id[fid] for fid in expected_ids)


def grade_fact_recall(llm, page_text: str, facts: Sequence[LoadBearingFact],
                      model: str | None = None) -> FactRecallResult:
    """One grader pass. Malformed responses propagate as LLMError -- never a
    silent pass."""
    grader_model = model or str(getattr(llm, "model", "scripted"))
    system, user = build_fact_recall_prompt(page_text, facts)
    raw = llm.complete(system, user)
    verdicts = parse_fact_recall_response(raw, tuple(f.id for f in facts))
    n = len(verdicts)
    recall = sum(v.covered for v in verdicts) / n if n else 0.0
    return FactRecallResult(grader_model, verdicts, recall, ())


def grade_fact_recall_twice(llm_a, llm_b, page_text: str,
                            facts: Sequence[LoadBearingFact],
                            model_a: str | None = None,
                            model_b: str | None = None) -> FactRecallAgreement:
    """Two independent graders. Per-fact consensus = both say covered;
    disagreement is contested (never resolved here -- listed for manual
    adjudication). Either grader's error propagates."""
    result_a = grade_fact_recall(llm_a, page_text, facts, model=model_a)
    result_b = grade_fact_recall(llm_b, page_text, facts, model=model_b)
    a_by_id = {v.fact_id: v for v in result_a.verdicts}
    b_by_id = {v.fact_id: v for v in result_b.verdicts}
    consensus = tuple(fid for fid in (f.id for f in facts)
                      if a_by_id[fid].covered and b_by_id[fid].covered)
    contested = tuple(fid for fid in (f.id for f in facts)
                      if a_by_id[fid].covered != b_by_id[fid].covered)
    n = len(facts) or 1
    return FactRecallAgreement(
        result_a=result_a,
        result_b=result_b,
        consensus_covered=consensus,
        contested_ids=contested,
        consensus_recall=len(consensus) / n,
        union_recall=sum(1 for fid in (f.id for f in facts)
                         if a_by_id[fid].covered or b_by_id[fid].covered) / n,
    )


def passes_gate(recall: float) -> bool:
    """Spec's gate: >= 90%. A bare comparison, named so the threshold lives in
    exactly one place."""
    return recall >= GATE_THRESHOLD


def classify_miss(fact: LoadBearingFact, gathered_doc_ids: set[str]) -> str:
    """Deterministic miss taxonomy (no LLM): was the fact's evidence ever
    gathered? Separates retrieval failure from writer/repair failure."""
    if any(doc in gathered_doc_ids for doc in fact.supported_by):
        return "evidence_gathered_but_omitted"
    return "evidence_not_gathered"


@dataclass(frozen=True)
class ClusterFactRecall:
    cluster_id: str
    topic: str
    agreement: FactRecallAgreement | None
    contested_ids: tuple[str, ...]
    consensus_recall: float
    union_recall: float
    recall_a: float
    recall_b: float
    errors: tuple[str, ...]
    miss_taxonomy: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class FactRecallReport:
    clusters: tuple[ClusterFactRecall, ...]
    total_facts: int
    pooled_consensus_recall: float
    pooled_union_recall: float
    pooled_recall_a: float
    pooled_recall_b: float
    contested_count: int
    gate: bool
    timestamp: str
    git_sha: str
    config: dict[str, object]


def run_fact_recall_eval(entries: Sequence[SynthesisClusterEntry],
                         page_dir: str | Path, gather_dir: str | Path,
                         llm_a, llm_b, *, model_a: str | None = None,
                         model_b: str | None = None) -> FactRecallReport:
    """Grade every entry's frozen page against its golden facts. A missing page
    or unreadable gather sidecar is an error row, never silently skipped -- and
    its facts stay in the pooled denominator as misses (an eval that cannot
    fail correctly is worse than no eval)."""
    pages, gathers = Path(page_dir), Path(gather_dir)
    clusters: list[ClusterFactRecall] = []
    for entry in entries:
        page_path = pages / f"{entry.id}.md"
        if not page_path.exists():
            clusters.append(_error_cluster(entry, ("page missing",)))
            continue
        _, body = split_frontmatter(page_path.read_text(encoding="utf-8"))

        gathered: set[str] = set()
        errors: list[str] = []
        gather_path = gathers / f"{entry.id}.gather.json"
        if gather_path.exists():
            try:
                raw = json.loads(gather_path.read_text(encoding="utf-8"))
                gathered = set(raw.get("gathered_doc_ids", []))
            except (json.JSONDecodeError, OSError, TypeError) as exc:
                errors.append(f"gather sidecar unreadable: {exc}")
        else:
            errors.append("gather sidecar missing")

        try:
            agreement = grade_fact_recall_twice(
                llm_a, llm_b, body, entry.load_bearing_facts,
                model_a=model_a, model_b=model_b,
            )
        except LLMError as exc:
            clusters.append(_error_cluster(entry, tuple(errors) + (f"grader failed: {exc}",)))
            continue

        a_by_id = {v.fact_id: v for v in agreement.result_a.verdicts}
        b_by_id = {v.fact_id: v for v in agreement.result_b.verdicts}
        miss_taxonomy = []
        for fact in entry.load_bearing_facts:
            if not a_by_id[fact.id].covered and not b_by_id[fact.id].covered:
                miss_taxonomy.append({
                    "fact_id": fact.id,
                    "fact_text": fact.text,
                    "classification": classify_miss(fact, gathered),
                })

        clusters.append(ClusterFactRecall(
            cluster_id=entry.id,
            topic=entry.topic,
            agreement=agreement,
            contested_ids=agreement.contested_ids,
            consensus_recall=agreement.consensus_recall,
            union_recall=agreement.union_recall,
            recall_a=agreement.result_a.recall,
            recall_b=agreement.result_b.recall,
            errors=tuple(errors),
            miss_taxonomy=tuple(miss_taxonomy),
        ))

    total_facts = sum(len(e.load_bearing_facts) for e in entries)
    denom = total_facts or 1
    consensus_count = sum(len(c.agreement.consensus_covered) for c in clusters if c.agreement)
    union_count = sum(len(c.agreement.consensus_covered) + len(c.agreement.contested_ids)
                      for c in clusters if c.agreement)
    # pooled recalls: weight per-cluster recall by its fact count (error clusters
    # contribute zero covered).
    per_cluster_facts = {e.id: len(e.load_bearing_facts) for e in entries}
    recall_a = sum(c.recall_a * per_cluster_facts.get(c.cluster_id, 0) for c in clusters) / denom
    recall_b = sum(c.recall_b * per_cluster_facts.get(c.cluster_id, 0) for c in clusters) / denom
    contested_count = sum(len(c.contested_ids) for c in clusters if c.agreement)

    return FactRecallReport(
        clusters=tuple(clusters),
        total_facts=total_facts,
        pooled_consensus_recall=consensus_count / denom,
        pooled_union_recall=union_count / denom,
        pooled_recall_a=recall_a,
        pooled_recall_b=recall_b,
        contested_count=contested_count,
        gate=passes_gate(consensus_count / denom),
        timestamp=datetime.now(timezone.utc).isoformat(),
        git_sha=_git_sha(),
        config={
            "model_a": model_a or "default",
            "model_b": model_b or "default",
            "gate_threshold": GATE_THRESHOLD,
            "page_dir": str(pages),
            "gather_dir": str(gathers),
        },
    )


def _error_cluster(entry: SynthesisClusterEntry, errors: tuple[str, ...]) -> ClusterFactRecall:
    return ClusterFactRecall(
        cluster_id=entry.id,
        topic=entry.topic,
        agreement=None,
        contested_ids=(),
        consensus_recall=0.0,
        union_recall=0.0,
        recall_a=0.0,
        recall_b=0.0,
        errors=errors,
        miss_taxonomy=(),
    )


def _git_sha() -> str:
    root = Path(__file__).resolve().parents[3]
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False,
    )
    return completed.stdout.strip() or "unknown"
