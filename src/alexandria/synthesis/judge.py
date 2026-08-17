"""Orchestrate existing entailment and coverage judges for one synthesized page."""

from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace

from ..audit import AuditResult, grade_note
from ..coverage import AgreementResult, grade_skip_twice
from ..llm import LLMError
from .gather import GatherResult, SourceChunk
from .write import Claim, SynthesisPage

__all__ = ["ChunkAccountingError", "JudgeVerdict", "complete_skip_log", "judge_page"]


class ChunkAccountingError(ValueError):
    """A gathered chunk was neither cited nor given a deterministic skip predicate."""


@dataclass(frozen=True)
class JudgeVerdict:
    page: SynthesisPage
    chunk_accounted: bool
    entailment_passed: bool
    coverage_passed: bool
    audit: AuditResult
    coverage: tuple[AgreementResult, ...]
    failed_claim_ids: tuple[str, ...]
    failing_skip_ids: tuple[str, ...]
    borderline_skip_ids: tuple[str, ...]
    errors: tuple[str, ...]
    failed_clause_ids: tuple[tuple[str, str], ...] = ()

    @property
    def passes(self) -> bool:
        return (
            self.chunk_accounted
            and self.entailment_passed
            and self.coverage_passed
            and not self.errors
        )


def judge_page(gathered: GatherResult, page: SynthesisPage, *, audit_llm, coverage_llm_a,
               coverage_llm_b, coverage_sample_per_stratum: int = 1,
               audit_concurrency: int = 4) -> JudgeVerdict:
    """Run no new grading logic: deterministic accounting plus the three existing judges."""
    normalized_page = _validate_chunk_accounting(gathered, page)
    errors: list[str] = []
    audit = AuditResult()
    failed_claim_ids: list[str] = []
    failed_clause_ids: list[tuple[str, str]] = []

    chunks_by_id = {chunk.chunk_id: chunk for chunk in gathered.chunks}
    chunks_by_doc = {chunk.doc_id: chunk for chunk in gathered.chunks}
    # Deterministic pre-pass: compute each claim's evidence locally, then run
    # the (independent) per-claim entailment calls concurrently -- they share no
    # state, so wall-clock drops from N*call to ~ceil(N/workers)*call while
    # results stay in claim order. Model-agnostic: the same audit_llm client is
    # used either way (LLMClient is stateless apart from diagnostic counters;
    # the gateway owns concurrency). audit_concurrency=1 preserves the old
    # strictly-sequential behavior for tests/determinism.
    grade_jobs: list[tuple] = []  # (claim, evidence) pairs needing an LLM call
    for claim in normalized_page.claims:
        evidence, claim_errors = _claim_evidence(claim, chunks_by_id, chunks_by_doc)
        if claim_errors:
            errors.extend(claim_errors)
            failed_claim_ids.append(claim.id)
            continue
        grade_jobs.append((claim, evidence))

    def _grade_one(claim, evidence):
        try:
            return grade_note(
                audit_llm,
                _transcript(evidence),
                claim.id,
                claim.text,
                claim.id,
                clauses=True,
            )
        except LLMError as exc:
            return exc

    workers = max(1, int(audit_concurrency))
    if len(grade_jobs) > 1 and workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(lambda job: _grade_one(*job), grade_jobs))
    else:
        results = [_grade_one(claim, evidence) for claim, evidence in grade_jobs]

    for (claim, _evidence), verdict in zip(grade_jobs, results):
        if isinstance(verdict, LLMError):
            errors.append(str(verdict))
            failed_claim_ids.append(claim.id)
            continue
        audit.verdicts.append(verdict)
        if verdict.verdict != "supported":
            failed_claim_ids.append(claim.id)
            if verdict.clauses:
                # compound-claim splitting (round-4): repair targets only the
                # failing clauses, not the whole claim
                for clause in verdict.clauses:
                    if clause.verdict != "supported":
                        failed_clause_ids.append((claim.id, clause.clause))
            else:
                failed_clause_ids.append((claim.id, claim.text))

    agreements: list[AgreementResult] = []
    failing_skip_ids: list[str] = []
    borderline_skip_ids: list[str] = []
    page_claims = _page_claims(normalized_page.claims)
    for skip in _stratified_sample(normalized_page.skip_log, coverage_sample_per_stratum):
        chunk_id = skip["chunk_id"]
        try:
            agreement = grade_skip_twice(
                coverage_llm_a,
                coverage_llm_b,
                page_claims,
                chunks_by_id[chunk_id].text,
                f"skip:{chunk_id}",
                # fast-tier models (sol/terra) are refused at temperature=0 by
                # llm.py -- measured 2026-08-07: coverage-b terra at default
                # temp made EVERY cluster fail coverage (can never emit);
                # nonzero temp is verified clean by the llm.py guard.
                temperature_a=0.1,
                temperature_b=0.1,
            )
        except LLMError as exc:
            errors.append(str(exc))
            failing_skip_ids.append(chunk_id)
            continue
        agreements.append(agreement)
        if agreement.consensus_label == "LB":
            failing_skip_ids.append(chunk_id)
        elif agreement.consensus_label == "borderline":
            borderline_skip_ids.append(chunk_id)

    entailment_passed = audit.passes and not any(
        claim.id in failed_claim_ids for claim in normalized_page.claims
    )
    coverage_passed = not failing_skip_ids and not borderline_skip_ids and not any(
        error.startswith("grader failed on skip:") for error in errors
    )
    return JudgeVerdict(
        page=normalized_page,
        chunk_accounted=True,
        entailment_passed=entailment_passed,
        coverage_passed=coverage_passed,
        audit=audit,
        coverage=tuple(agreements),
        failed_claim_ids=tuple(failed_claim_ids),
        failing_skip_ids=tuple(failing_skip_ids),
        borderline_skip_ids=tuple(borderline_skip_ids),
        errors=tuple(errors),
        failed_clause_ids=tuple(failed_clause_ids),
    )


def complete_skip_log(gathered: GatherResult, page: SynthesisPage) -> SynthesisPage:
    """Create deterministic records for uncited chunks before a strict judge pass.

    This is deliberately separate from ``judge_page``. The former is the pipeline's
    mechanical bookkeeping; the latter is the lint gate that must prove it rejects an
    incomplete supplied log instead of silently repairing one.
    """
    chunks_by_id = {chunk.chunk_id: chunk for chunk in gathered.chunks}
    chunks_by_doc = {chunk.doc_id: chunk for chunk in gathered.chunks}
    cited = _cited_chunk_ids(page.claims, chunks_by_id, chunks_by_doc)
    supplied: dict[str, dict[str, str]] = {}
    for entry in page.skip_log:
        if not isinstance(entry, dict):
            raise ChunkAccountingError("skip log entry must be a mapping")
        chunk_id = entry.get("chunk_id")
        if not isinstance(chunk_id, str) or chunk_id not in chunks_by_id:
            raise ChunkAccountingError(f"skip log references unknown chunk {chunk_id!r}")
        if chunk_id in supplied:
            raise ChunkAccountingError(f"skip log contains duplicate entry for {chunk_id}")
        reason = entry.get("reason")
        if not isinstance(reason, str) or not _valid_skip_reason(reason, chunk_id, chunks_by_id):
            raise ChunkAccountingError(
                f"skip log has invalid deterministic predicate for {chunk_id}: {reason!r}"
            )
        supplied[chunk_id] = {
            "chunk_id": chunk_id,
            "doc_id": chunks_by_id[chunk_id].doc_id,
            "reason": reason,
        }

    normalized: list[dict[str, str]] = []
    for chunk in gathered.chunks:
        if chunk.chunk_id in cited:
            continue
        normalized.append(supplied.get(chunk.chunk_id, {
            "chunk_id": chunk.chunk_id,
            "doc_id": chunk.doc_id,
            "reason": "out_of_scope:not_cited",
        }))
    return replace(page, skip_log=tuple(normalized))


def _validate_chunk_accounting(gathered: GatherResult, page: SynthesisPage) -> SynthesisPage:
    chunks_by_id = {chunk.chunk_id: chunk for chunk in gathered.chunks}
    chunks_by_doc = {chunk.doc_id: chunk for chunk in gathered.chunks}
    cited = _cited_chunk_ids(page.claims, chunks_by_id, chunks_by_doc)
    supplied: dict[str, dict[str, str]] = {}
    for entry in page.skip_log:
        if not isinstance(entry, dict):
            raise ChunkAccountingError("skip log entry must be a mapping")
        chunk_id = entry.get("chunk_id")
        if not isinstance(chunk_id, str) or chunk_id not in chunks_by_id:
            raise ChunkAccountingError(f"skip log references unknown chunk {chunk_id!r}")
        if chunk_id in supplied:
            raise ChunkAccountingError(f"skip log contains duplicate entry for {chunk_id}")
        reason = entry.get("reason")
        if not isinstance(reason, str) or not _valid_skip_reason(reason, chunk_id, chunks_by_id):
            raise ChunkAccountingError(
                f"skip log has invalid deterministic predicate for {chunk_id}: {reason!r}"
            )
        supplied[chunk_id] = {
            "chunk_id": chunk_id,
            "doc_id": chunks_by_id[chunk_id].doc_id,
            "reason": reason,
        }
    overlap = cited & supplied.keys()
    if overlap:
        raise ChunkAccountingError(f"chunk is both cited and skipped: {sorted(overlap)[0]}")
    expected_skips = set(chunks_by_id) - cited
    if supplied.keys() != expected_skips:
        missing = sorted(expected_skips - supplied.keys())
        extra = sorted(supplied.keys() - expected_skips)
        detail = missing[0] if missing else extra[0]
        raise ChunkAccountingError(f"unaccounted or incorrectly skipped chunk: {detail}")
    return replace(
        page,
        skip_log=tuple(
            supplied[chunk.chunk_id] for chunk in gathered.chunks if chunk.chunk_id in supplied
        ),
    )


def _valid_skip_reason(reason: str, chunk_id: str, chunks_by_id: dict[str, SourceChunk]) -> bool:
    if reason.startswith("duplicate_of:"):
        duplicate_id = reason.removeprefix("duplicate_of:")
        return bool(duplicate_id) and duplicate_id in chunks_by_id and duplicate_id != chunk_id
    if reason.startswith("below_salience:"):
        try:
            float(reason.removeprefix("below_salience:"))
        except ValueError:
            return False
        return True
    return reason.startswith("out_of_scope:") and bool(reason.removeprefix("out_of_scope:"))


def _cited_chunk_ids(claims: tuple[Claim, ...], chunks_by_id: dict[str, SourceChunk],
                      chunks_by_doc: dict[str, SourceChunk]) -> set[str]:
    cited: set[str] = set()
    for claim in claims:
        for citation in claim.citations:
            if citation.chunk_id in chunks_by_id:
                cited.add(citation.chunk_id)
            elif citation.chunk_id is None and citation.doc_id in chunks_by_doc:
                cited.add(chunks_by_doc[citation.doc_id].chunk_id)
    return cited


def _claim_evidence(claim: Claim, chunks_by_id: dict[str, SourceChunk],
                    chunks_by_doc: dict[str, SourceChunk]) -> tuple[list[SourceChunk], list[str]]:
    evidence: list[SourceChunk] = []
    errors: list[str] = []
    seen: set[str] = set()
    if not claim.citations:
        return [], [f"claim {claim.id} has no citations"]
    for citation in claim.citations:
        if citation.chunk_id is not None:
            chunk = chunks_by_id.get(citation.chunk_id)
            if chunk is None:
                errors.append(f"claim {claim.id} cites unknown chunk {citation.chunk_id}")
                continue
            if chunk.doc_id != citation.doc_id:
                errors.append(
                    f"claim {claim.id} citation doc_id does not match {citation.chunk_id}"
                )
                continue
        else:
            chunk = chunks_by_doc.get(citation.doc_id)
            if chunk is None:
                errors.append(f"claim {claim.id} cites unknown doc_id {citation.doc_id}")
                continue
        if chunk.chunk_id not in seen:
            seen.add(chunk.chunk_id)
            evidence.append(chunk)
    return evidence, errors


def _transcript(chunks: list[SourceChunk]) -> str:
    return "\n\n".join(f"[{chunk.doc_id} {chunk.chunk_id}]\n{chunk.text}" for chunk in chunks)


def _page_claims(claims: tuple[Claim, ...]) -> str:
    lines: list[str] = []
    for claim in claims:
        citations = ", ".join(
            citation.chunk_id or citation.doc_id for citation in claim.citations
        )
        lines.append(f"- {claim.text} [{citations}]")
    return "\n".join(lines)


def _stratified_sample(skip_log: tuple[dict[str, str], ...],
                       per_stratum: int) -> list[dict[str, str]]:
    if per_stratum < 1:
        raise ValueError("coverage_sample_per_stratum must be positive")
    strata: dict[str, list[dict[str, str]]] = defaultdict(list)
    for entry in skip_log:
        strata[entry["reason"].split(":", 1)[0]].append(entry)
    selected: list[dict[str, str]] = []
    for name in sorted(strata):
        selected.extend(
            sorted(strata[name], key=lambda entry: entry["chunk_id"])[:per_stratum]
        )
    return selected
