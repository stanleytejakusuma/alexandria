"""Golden page-to-fact recall evaluator (WORK-ORDER-phase2-fact-recall-eval.md).

Phase-2's golden-synthesis gate (docs/SPEC-phase2-eval.md) is load-bearing-fact
recall >= 90%. The runtime coverage judge (`coverage.py`) cannot certify that
gate: it is scoped by design to one question -- does an uncited, skipped chunk
contradict or materially qualify a claim the page makes? It never asks whether
the page expressed every load-bearing proposition inside evidence it DID cite.

This module is the missing instrument, and it is evaluation-only: it grades an
already-produced page against hand-curated golden facts with two independent
model-family graders, and it does not touch the runtime pipeline, its prompts,
retrieval, or coverage.py.

Design decisions are load-bearing (Red review rounds 1-2, 2026-08-05):

1. Grade the rendered reader-visible page body, not the internal structured
   claims object -- otherwise we silently measure an easier, different quantity.
2. Every `covered: true` verdict must carry a quoted page evidence span that is
   a whitespace-normalized substring of the rendered body, or the response
   fails loudly (a grader can hallucinate coverage un-auditably).
3. Do not gate on blind strict-AND consensus alone: each grader's recall,
   per-fact agreement/disagreement, and a conservative consensus recall are all
   reported, and every disagreement is listed for manual adjudication.
4. Every consensus-miss is joined against the captured gather output
   deterministically, separating "evidence never gathered" (retrieval failure)
   from "evidence gathered but the page omitted it" (writer/repair failure);
   the taxonomy is explicitly PROVISIONAL because golden facts reference whole
   documents and a gathered doc does not prove its passage was gathered.
5. Run-status separation: only attributable pipeline failures count as recall
   misses; measurement-invalid clusters are excluded from the denominator and
   force an INVALID verdict (never encoded as FAIL).
6. Verdict states are distinct: PASS / PROVISIONAL_FAIL (near-threshold band or
   unresolved grader disagreement -- adjudication required) / FINAL_FAIL /
   INVALID. Adjudications can be supplied to resolve contested and
   near-threshold facts; they are recorded, never silent.

Known deferral (Red round 2, not a merge blocker for the experimental
evaluator): a full immutable run manifest binding golden-set hashes, prompts,
model config, pages/sidecars, and code revision, plus atomic write/no-silent-
overwrite enforcement. Required before this becomes the authoritative gate;
scoped as a separate work order.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from ..corpus import split_frontmatter
from ..llm import LLMError
from ..synthesis.repair import REPAIR_SYSTEM
from ..synthesis.write import WRITER_SYSTEM
from .synthesis_golden import LoadBearingFact, SynthesisClusterEntry

__all__ = [
    "BAND_LOW",
    "GATE_THRESHOLD",
    "MANIFEST_VERSION",
    "build_manifest",
    "verify_manifest",
    "GRADER_SYSTEM",
    "STATUS_GRADED",
    "STATUS_MEASUREMENT_INVALID",
    "STATUS_PIPELINE_FAILURE",
    "VERDICT_FINAL_FAIL",
    "VERDICT_INVALID",
    "VERDICT_PASS",
    "VERDICT_PROVISIONAL_FAIL",
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
# Near-threshold band: a consensus recall in [BAND_LOW, GATE_THRESHOLD) cannot
# become FINAL_FAIL without adjudication (a fixable near-miss must be reviewed,
# not silently declared dead). Predeclared, not tuned per run.
BAND_LOW = 0.85

STATUS_GRADED = "graded"
STATUS_PIPELINE_FAILURE = "pipeline_failure"
STATUS_MEASUREMENT_INVALID = "measurement_invalid"

VERDICT_PASS = "PASS"
VERDICT_PROVISIONAL_FAIL = "PROVISIONAL_FAIL"
VERDICT_FINAL_FAIL = "FINAL_FAIL"
VERDICT_INVALID = "INVALID"

# Aggregation semantics version: bump when scoring/verdict rules change so
# reports from different aggregation versions are never silently compared.
# v2 (2026-08-07): evidence-not-verbatim facts are flagged per-fact and join
# the contested/adjudication list instead of invalidating the whole cluster
# (measured: diffuse page statements make verbatim quoting impossible for some
# facts, and one such fact was nuking 5 facts out of the denominator).
MANIFEST_VERSION = "fact-recall-v2"

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
    raw: str = ""          # the grader's raw response, retained for audit

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


def _no_duplicate_keys(pairs):
    """json.loads hook: a grader response with duplicate JSON keys is malformed
    and must fail loudly -- the decoder otherwise silently keeps the last one."""
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise LLMError(f"duplicate JSON key {key!r} in grader response")
        result[key] = value
    return result


def _normalized(text: str) -> str:
    """Collapse all whitespace runs to single spaces for evidence-span matching."""
    return " ".join(text.split())


def parse_fact_recall_response(raw: str, expected_ids: tuple[str, ...],
                               page_body: str = "") -> tuple[FactVerdict, ...]:
    """Strictly parse one grader response. ANY violation raises LLMError -- an
    eval that cannot fail correctly is worse than no eval (never partial accept).

    When page_body is given, every covered verdict's evidence span must be a
    whitespace-normalized substring of the rendered page body -- a grader
    quoting evidence that is not in the page is lying and must fail."""
    try:
        payload = json.loads(raw, object_pairs_hook=_no_duplicate_keys)
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

    if page_body:
        page_norm = _normalized(page_body)
        for verdict in verdicts:
            if (verdict.covered and verdict.error is None
                    and _normalized(verdict.evidence) not in page_norm):
                # Per-fact flag, NOT a cluster killer: a diffuse page statement
                # can make verbatim quoting impossible for a genuinely-covered
                # fact (measured 2026-08-07). The fact joins the adjudication
                # list with its evidence retained; strictness holds for every
                # fact whose evidence DOES verify.
                verdicts = [
                    v if v is not verdict else FactVerdict(
                        v.fact_id, v.covered, v.evidence,
                        error="evidence_not_verbatim")
                    for v in verdicts
                ]

    by_id = {v.fact_id: v for v in verdicts}
    return tuple(by_id[fid] for fid in expected_ids)


def grade_fact_recall(llm, page_text: str, facts: Sequence[LoadBearingFact],
                      model: str | None = None,
                      evidence_retries: int = 2) -> FactRecallResult:
    """One grader pass. Malformed responses propagate as LLMError -- never a
    silent pass. The raw response is retained on the result for audit.

    evidence_retries: the evidence-substring check fails STOCHASTICALLY --
    graders occasionally quote a paraphrase instead of a verbatim span
    (measured 2026-08-07: the same page/model/prompt passed on re-run while
    three clusters were invalidated in one batch). Strictness is preserved
    (a response only counts when its evidence is verbatim in the page); a
    bounded retry with a verbatim hint recovers the measurement from a
    stochastic quote, mirroring the driver's emission-retry doctrine."""
    grader_model = model or str(getattr(llm, "model", "scripted"))
    system, user = build_fact_recall_prompt(page_text, facts)
    raw = llm.complete(system, user)
    retries = 0
    while True:
        try:
            verdicts = parse_fact_recall_response(
                raw, tuple(f.id for f in facts), page_body=page_text)
        except LLMError as exc:
            if retries >= evidence_retries:
                raise
            retries += 1
            hint = (f"Your previous response was rejected: {exc}. "
                    f"Quote evidence spans VERBATIM from the page text -- "
                    f"no paraphrasing, no added punctuation.")
            raw = llm.complete(system, f"{user}\n\n{hint}")
            continue
        flagged = [v.fact_id for v in verdicts if v.error == "evidence_not_verbatim"]
        if flagged and retries < evidence_retries:
            # A diffuse page statement can make verbatim quoting impossible;
            # one retry with a hint before accepting the flag (the flagged
            # fact joins the adjudication list either way).
            retries += 1
            hint = (f"Your evidence for {flagged} is not a verbatim page span. "
                    f"Quote the EXACT page text that states the fact, or mark "
                    f"it not covered.")
            raw = llm.complete(system, f"{user}\n\n{hint}")
            continue
        break
    n = len(verdicts)
    recall = sum(v.covered for v in verdicts) / n if n else 0.0
    errors = tuple(f"evidence retried {retries}x" for _ in range(1)) if retries else ()
    return FactRecallResult(grader_model, verdicts, recall, errors, raw=raw)


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
    gathered? Separates retrieval failure from writer/repair failure. PROVISIONAL
    by design -- golden facts reference whole docs, not passages."""
    if any(doc in gathered_doc_ids for doc in fact.supported_by):
        return "evidence_gathered_but_omitted"
    return "evidence_not_gathered"


def _cluster_outcome(entry: SynthesisClusterEntry, agreement: FactRecallAgreement,
                     adjudications: dict[str, bool] | None, gathered: set[str]):
    """Apply adjudications to the raw agreement. Adjudication keys are
    \"<cluster_id>::<fact_id>\" and override BOTH graders' verdicts for that fact
    (the adjudicated label is ground truth for it). Returns
    (consensus_ids, contested_ids, miss_taxonomy)."""
    a_by_id = {v.fact_id: v for v in agreement.result_a.verdicts}
    b_by_id = {v.fact_id: v for v in agreement.result_b.verdicts}
    consensus: list[str] = []
    contested: list[str] = []
    misses: list[dict[str, str]] = []
    for fact in entry.load_bearing_facts:
        adj = adjudications.get(f"{entry.id}::{fact.id}") if adjudications else None
        if adj is True:
            consensus.append(fact.id)
            continue
        if adj is False:
            misses.append({
                "fact_id": fact.id,
                "fact_text": fact.text,
                "classification": "adjudicated_not_covered",
                "provisional": False,
            })
            continue
        a_v, b_v = a_by_id[fact.id], b_by_id[fact.id]
        a_cov, b_cov = a_v.covered, b_v.covered
        a_flag = a_v.error == "evidence_not_verbatim"
        b_flag = b_v.error == "evidence_not_verbatim"
        if (a_cov and b_cov) and not (a_flag or b_flag):
            consensus.append(fact.id)
        elif a_cov != b_cov or a_flag or b_flag:
            # disagreement OR unverifiable evidence: adjudication required
            contested.append(fact.id)
        else:
            misses.append({
                "fact_id": fact.id,
                "fact_text": fact.text,
                "classification": classify_miss(fact, gathered),
                "provisional": True,
            })
    return tuple(consensus), tuple(contested), tuple(misses)


@dataclass(frozen=True)
class ClusterFactRecall:
    cluster_id: str
    topic: str
    agreement: FactRecallAgreement | None
    status: str                      # graded | pipeline_failure | measurement_invalid
    consensus_fact_count: int
    contested_ids: tuple[str, ...]
    consensus_recall: float
    union_recall: float
    recall_a: float
    recall_b: float
    errors: tuple[str, ...]
    miss_taxonomy: tuple[dict[str, str], ...]
    adjudicated_fact_count: int = 0


@dataclass(frozen=True)
class FactRecallReport:
    clusters: tuple[ClusterFactRecall, ...]
    total_facts: int                 # every fact across all clusters
    scored_fact_count: int           # facts in graded + pipeline_failure clusters
    consensus_count: int             # adjudicated-consensus-covered facts, graded clusters
    pooled_consensus_recall: float   # consensus_count / scored_fact_count
    pooled_union_recall: float
    pooled_recall_a: float
    pooled_recall_b: float
    macro_consensus_recall: float    # unweighted mean of per-cluster consensus recall
    contested_count: int
    adjudicated_count: int
    pipeline_failure_cluster_ids: tuple[str, ...]
    invalid_cluster_ids: tuple[str, ...]
    verdict: str                     # PASS | PROVISIONAL_FAIL | FINAL_FAIL | INVALID
    timestamp: str
    git_sha: str
    config: dict[str, object]
    manifest: dict[str, object] = field(default_factory=dict)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str | None:
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError:
        return None


def build_manifest(*, golden_path: str | Path, page_dir: str | Path,
                   gather_dir: str | Path, model_a: str, model_b: str) -> dict[str, object]:
    """Bind every artifact that produced a report: golden set bytes, the writer/
    repair/grader prompts, model config, each graded page and gather sidecar,
    git sha, and the aggregation version. Re-verifying against the same manifest
    proves the artifacts were not edited after the fact (Red round-2 requirement
    for authoritative gate use)."""
    golden = Path(golden_path)
    pages, gathers = Path(page_dir), Path(gather_dir)
    return {
        "aggregation_version": MANIFEST_VERSION,
        "git_sha": _git_sha(),
        "golden_sha256": _sha256_file(golden),
        "prompt_sha256": {
            "writer": _sha256_bytes(WRITER_SYSTEM.encode("utf-8")),
            "repair": _sha256_bytes(REPAIR_SYSTEM.encode("utf-8")),
            "grader": _sha256_bytes(GRADER_SYSTEM.encode("utf-8")),
        },
        "models": {"model_a": model_a, "model_b": model_b},
        "pages": {p.stem: _sha256_file(p) for p in sorted(pages.glob("*.md"))},
        "gather_sidecars": {
            (p.stem[:-len(".gather")] if p.stem.endswith(".gather") else p.stem): _sha256_file(p)
            for p in sorted(gathers.glob("*.gather.json"))
        },
    }


def verify_manifest(manifest: dict[str, object], *, golden_path: str | Path,
                    page_dir: str | Path, gather_dir: str | Path) -> list[str]:
    """Recompute the manifest from current disk state and list every mismatch.
    Empty list = artifacts unchanged since the report was produced."""
    if not manifest:
        return ["report carries no manifest (pre-manifest artifact)"]
    problems: list[str] = []
    current = build_manifest(golden_path=golden_path, page_dir=page_dir,
                             gather_dir=gather_dir, model_a="", model_b="")
    if current["golden_sha256"] != manifest.get("golden_sha256"):
        problems.append("golden set hash changed")
    for name, sha in (current.get("prompt_sha256") or {}).items():
        if sha != (manifest.get("prompt_sha256") or {}).get(name):
            problems.append(f"prompt {name} changed")
    if current["git_sha"] != manifest.get("git_sha"):
        problems.append("git sha changed")
    for fid, sha in (current.get("pages") or {}).items():
        if sha != (manifest.get("pages") or {}).get(fid):
            problems.append(f"page {fid}.md changed")
    for cid, sha in (current.get("gather_sidecars") or {}).items():
        if sha != (manifest.get("gather_sidecars") or {}).get(cid):
            problems.append(f"gather sidecar {cid} changed")
    return problems


def _verdict(consensus_recall: float, contested_count: int, invalid: bool) -> str:
    if invalid:
        return VERDICT_INVALID
    if consensus_recall >= GATE_THRESHOLD:
        return VERDICT_PASS
    if contested_count or consensus_recall >= BAND_LOW:
        # unresolved disagreement or a near-threshold miss: adjudication required
        # before this can be declared dead.
        return VERDICT_PROVISIONAL_FAIL
    return VERDICT_FINAL_FAIL


def run_fact_recall_eval(entries: Sequence[SynthesisClusterEntry],
                         page_dir: str | Path, gather_dir: str | Path,
                         llm_a, llm_b, *, model_a: str | None = None,
                         model_b: str | None = None,
                         adjudications: dict[str, bool] | None = None,
                         golden_path: str | Path | None = None) -> FactRecallReport:
    """Grade every entry's frozen page against its golden facts, with explicit
    run-status separation (Red review rounds 1-2, 2026-08-05):

    - graded: page + readable gather sidecar + both graders returned verdicts.
    - pipeline_failure: the gather sidecar records emitted=false (a real
      synthesis failure). Its facts stay in the denominator as misses -- fail
      closed, attributable.
    - measurement_invalid: missing page, missing/unreadable sidecar, or a
      grader error. The run cannot produce a trustworthy score; the facts are
      excluded from the denominator and the verdict is INVALID (never FAIL).

    Adjudications (optional) override both graders per fact, keyed
    \"<cluster_id>::<fact_id>\" -> bool. They are recorded in the report and
    recompute consensus/contested/misses."""
    pages, gathers = Path(page_dir), Path(gather_dir)
    clusters: list[ClusterFactRecall] = []
    for entry in entries:
        gather_path = gathers / f"{entry.id}.gather.json"
        gathered: set[str] = set()
        sidecar_emitted: bool | None = None
        sidecar_error: str | None = None
        if gather_path.exists():
            try:
                raw_sidecar = json.loads(gather_path.read_text(encoding="utf-8"))
                gathered = set(raw_sidecar.get("gathered_doc_ids", []))
                sidecar_emitted = bool(raw_sidecar.get("emitted", False))
            except (json.JSONDecodeError, OSError, TypeError) as exc:
                sidecar_error = f"gather sidecar unreadable: {exc}"
        else:
            sidecar_error = "gather sidecar missing"

        page_path = pages / f"{entry.id}.md"
        if not page_path.exists():
            if sidecar_emitted is False:
                # attributable pipeline failure: the page was never produced.
                clusters.append(_failure_cluster(entry, ("page not emitted by pipeline",)))
            else:
                clusters.append(_invalid_cluster(
                    entry, (sidecar_error or "page missing",)))
            continue
        if sidecar_error is not None:
            # An unreadable/missing sidecar makes the run invalid even with a
            # valid page: without it, attribution and integrity cannot be
            # established, and folding the cluster's facts in as misses would
            # produce a conservative but false quality estimate.
            clusters.append(_invalid_cluster(entry, (sidecar_error,)))
            continue
        if sidecar_emitted is False:
            # page exists but the pipeline recorded no emission: stale/mismatched
            # artifacts -- a measurement-integrity problem, not a graded page.
            clusters.append(_invalid_cluster(
                entry, ("page present but sidecar records emitted=false",)))
            continue

        _, body = split_frontmatter(page_path.read_text(encoding="utf-8"))
        try:
            agreement = grade_fact_recall_twice(
                llm_a, llm_b, body, entry.load_bearing_facts,
                model_a=model_a, model_b=model_b,
            )
        except LLMError as exc:
            clusters.append(_invalid_cluster(entry, (f"grader failed: {exc}",)))
            continue

        consensus_ids, contested_ids, misses = _cluster_outcome(
            entry, agreement, adjudications, gathered)
        n = len(entry.load_bearing_facts) or 1
        adjudicated_here = sum(
            1 for fact in entry.load_bearing_facts
            if adjudications and f"{entry.id}::{fact.id}" in adjudications)
        clusters.append(ClusterFactRecall(
            cluster_id=entry.id,
            topic=entry.topic,
            agreement=agreement,
            status=STATUS_GRADED,
            consensus_fact_count=len(consensus_ids),
            contested_ids=contested_ids,
            consensus_recall=len(consensus_ids) / n,
            union_recall=(len(consensus_ids) + len(contested_ids)) / n,
            recall_a=agreement.result_a.recall,
            recall_b=agreement.result_b.recall,
            errors=(),
            miss_taxonomy=misses,
            adjudicated_fact_count=adjudicated_here,
        ))

    per_cluster_facts = {e.id: len(e.load_bearing_facts) for e in entries}
    graded = [c for c in clusters if c.status == STATUS_GRADED]
    failures = [c for c in clusters if c.status == STATUS_PIPELINE_FAILURE]
    invalid = [c for c in clusters if c.status == STATUS_MEASUREMENT_INVALID]
    scored = graded + failures

    scored_facts = sum(per_cluster_facts[c.cluster_id] for c in scored)
    denom = scored_facts or 1
    consensus_count = sum(c.consensus_fact_count for c in graded)
    union_count = sum(c.consensus_fact_count + len(c.contested_ids) for c in graded)
    recall_a = sum(c.recall_a * per_cluster_facts[c.cluster_id] for c in graded) / denom
    recall_b = sum(c.recall_b * per_cluster_facts[c.cluster_id] for c in graded) / denom
    macro = (sum(c.consensus_recall for c in scored) / len(scored)
             if scored else 0.0)
    contested_count = sum(len(c.contested_ids) for c in graded)
    adjudicated_count = sum(c.adjudicated_fact_count for c in graded)
    consensus_recall = consensus_count / denom

    return FactRecallReport(
        clusters=tuple(clusters),
        total_facts=sum(per_cluster_facts.values()),
        scored_fact_count=scored_facts,
        consensus_count=consensus_count,
        pooled_consensus_recall=consensus_recall,
        pooled_union_recall=union_count / denom,
        pooled_recall_a=recall_a,
        pooled_recall_b=recall_b,
        macro_consensus_recall=macro,
        contested_count=contested_count,
        adjudicated_count=adjudicated_count,
        pipeline_failure_cluster_ids=tuple(c.cluster_id for c in failures),
        invalid_cluster_ids=tuple(c.cluster_id for c in invalid),
        verdict=_verdict(consensus_recall, contested_count, bool(invalid)),
        timestamp=datetime.now(timezone.utc).isoformat(),
        git_sha=_git_sha(),
        config={
            "model_a": model_a or "default",
            "model_b": model_b or "default",
            "gate_threshold": GATE_THRESHOLD,
            "band_low": BAND_LOW,
            "page_dir": str(pages),
            "gather_dir": str(gathers),
        },
        manifest=(build_manifest(golden_path=golden_path, page_dir=pages, gather_dir=gathers,
                                 model_a=model_a or "default", model_b=model_b or "default")
                  if golden_path is not None else {}),
    )


def _invalid_cluster(entry: SynthesisClusterEntry,
                     errors: tuple[str, ...]) -> ClusterFactRecall:
    return ClusterFactRecall(
        cluster_id=entry.id, topic=entry.topic, agreement=None,
        status=STATUS_MEASUREMENT_INVALID, consensus_fact_count=0, contested_ids=(),
        consensus_recall=0.0, union_recall=0.0, recall_a=0.0, recall_b=0.0,
        errors=errors, miss_taxonomy=(),
    )


def _failure_cluster(entry: SynthesisClusterEntry,
                     errors: tuple[str, ...]) -> ClusterFactRecall:
    return ClusterFactRecall(
        cluster_id=entry.id, topic=entry.topic, agreement=None,
        status=STATUS_PIPELINE_FAILURE, consensus_fact_count=0, contested_ids=(),
        consensus_recall=0.0, union_recall=0.0, recall_a=0.0, recall_b=0.0,
        errors=errors, miss_taxonomy=(),
    )


def _git_sha() -> str:
    root = Path(__file__).resolve().parents[3]
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False,
    )
    return completed.stdout.strip() or "unknown"
