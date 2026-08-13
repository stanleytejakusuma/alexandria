"""The reproducible half of the eval gate: everything here runs from a clean clone.

BACKLOG #20. The certification gate scores retrieval against a golden set that
lives in a private corpus repo, so `scripts/eval-gate.py` SKIPS on every machine
that does not have that corpus -- which is every machine except one, including
CI. These tests close that hole with a synthetic corpus committed to this repo.

READ THIS BEFORE QUOTING A GREEN RUN AS EVIDENCE. This gate measures the
INSTRUMENT, not the knowledge. It drives the real chunker, the real vector store,
the real BM25 index, real RRF fusion, the real manifest check, and the real
scoring in `metrics.py` / `negative.py` / `history.py`. It says nothing about
whether retrieval over the real corpus is any good, because the embedder here is
`HashEmbedder` -- deterministic, dependency-free, and semantically empty. Recall
below is earned by BM25 and by fusion tolerating a noise dense-leg.

Two gates, two purposes: the private-corpus gate answers "did retrieval quality
move?", this one answers "does the measuring instrument still work?". A green run
here is never evidence that retrieval is healthy.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from alexandria.config import AppConfig
from alexandria.eval.golden import load_golden, verify_targets
from alexandria.eval.history import compare
from alexandria.eval.negative import load_negative, run_negative, separation
from alexandria.eval.runner import run_eval
from alexandria.eval.synthetic import (
    GOLDEN_PATH,
    NEGATIVE_PATH,
    SYNTHETIC_CORPUS,
    build_synthetic_engine,
)
from alexandria.index.bm25 import BM25Index
from alexandria.index.chunker import chunk_doc_records, is_indexable_source

# Floors, not targets. Measured on this fixture at the commit that introduced it:
# recall@k 0.950, MRR 0.514. The floors sit a little below so ordinary churn does
# not fail the build, while a real scoring or fusion break -- which moves these by
# tens of points, not by one -- still does. Raise them if the measured numbers
# rise; never lower one to make a red build green without saying why in the
# commit message.
RECALL_FLOOR = 0.90
MRR_FLOOR = 0.45

GATE_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "synthetic-eval-gate.py"

# Only `sources/` is walked by the indexer, so the fixture README beside it is
# not a corpus document and must not be counted as one.
SOURCE_DOCUMENTS = sorted((SYNTHETIC_CORPUS / "sources").rglob("*.md"))

# A fixture where everything lands at rank 1 cannot detect a ranking regression:
# there is no room below the answer for a broken scorer to push it into. Measured
# rank-1 share of hits on this fixture: 0.29.
MAX_RANK_ONE_SHARE = 0.60


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    return build_synthetic_engine(tmp_path_factory.mktemp("synthetic-corpus"))


@pytest.fixture(scope="module")
def golden():
    return load_golden(GOLDEN_PATH)


@pytest.fixture(scope="module")
def report(engine, golden):
    return run_eval(engine, golden)


def test_every_fixture_document_is_indexable(engine):
    """A document with broken frontmatter drops out of the index silently.

    The golden set would then be scoring against a corpus quietly smaller than the
    one on disk, which is the exact failure shape this project keeps hitting: a
    step reports success while doing nothing.
    """
    config = AppConfig(corpus_path=SYNTHETIC_CORPUS)
    assert SOURCE_DOCUMENTS, "fixture corpus is empty"

    total = 0
    for path in SOURCE_DOCUMENTS:
        relative = path.relative_to(SYNTHETIC_CORPUS)
        assert is_indexable_source(relative), f"{relative} would be skipped by the indexer"
        records, error = chunk_doc_records(path, SYNTHETIC_CORPUS, config)
        assert error is None, f"{relative}: {error}"
        assert records, f"{relative} produced no chunks"
        total += len(records)

    assert engine.store.count() == total, (
        f"{total} chunks were produced but {engine.store.count()} reached the index"
    )


def test_golden_targets_all_resolve(engine, golden):
    """A typo in must_retrieve makes an entry unscoreable, not merely wrong."""
    assert verify_targets(golden, engine._corpus_root) == []


def test_negative_set_is_mostly_in_domain():
    """BACKLOG #22: the private negative set is 21/22 out-of-domain brand queries,
    which makes its negative score median an artefact of easy negatives rather
    than a measurement. This set must not repeat that.

    A query about aircraft carriers is trivially unanswerable by a library corpus.
    A query about the acquisitions budget is unanswerable only because nobody
    wrote that document -- that is the case that actually probes precision.
    """
    entries = load_negative(NEGATIVE_PATH)
    in_domain = [entry for entry in entries if entry.note.startswith("In-domain")]
    assert len(entries) >= 10

    # `verified_against` claims how many documents were read to confirm the query
    # is unanswerable. Add a document without re-checking and that claim silently
    # becomes false -- an unverified negative that still reports as verified.
    document_count = len(SOURCE_DOCUMENTS)
    stale = [entry.id for entry in entries if entry.verified_against != document_count]
    assert not stale, (
        f"{stale} claim verification against a corpus of a different size than the "
        f"{document_count} documents now on disk; re-check them or the claim is false"
    )

    assert len(in_domain) / len(entries) >= 0.75, (
        f"only {len(in_domain)}/{len(entries)} negatives are in-domain; an "
        "out-of-domain-heavy negative set measures nothing but topic distance"
    )


def test_synthetic_recall_clears_the_floor(report):
    assert report.summary.errors == 0, report.summary.error_ids
    assert report.summary.target_errors == []
    assert report.summary.recall_at_k >= RECALL_FLOOR, report.summary.misses
    assert report.summary.mrr >= MRR_FLOOR


def test_near_duplicate_documents_are_discriminated(report):
    """THE TEST THAT MATTERS MOST.

    `renewals-main` and `renewals-annex` state the same five rules with different
    numbers and share nearly all their vocabulary. Only a branch word separates
    them. Any scorer that degrades toward document length, term frequency alone,
    or first-document-wins will still satisfy the aggregate recall floor while
    returning the wrong branch's rules -- which, for a question about a renewal
    limit, is a worse answer than none.

    So this asserts PLACEMENT, not presence, in two ways -- both needed, because
    each alone is escapable:

    - A rank ceiling per query. Presence-only assertions are why the first draft
      of this test survived a mutation that swapped the top two results: for two
      of the three queries the near-duplicate is not retrieved at all, so a
      "right before wrong" check had nothing to compare and passed vacuously.
      Measured ranks when this fixture was written: 1, 2, 1.
    - Relative order wherever both documents do surface, plus a guard that at
      least one query still exercises that comparison. If a future change stops
      retrieving the distractor everywhere, the ordering check would quietly
      become a no-op again, and the guard fails instead of hiding it.

    KNOWN WEAKNESS, deliberately recorded and not asserted: `syn-renew-telephone`
    ("renewal by telephone line until 21:00") ranks `renewals-annex` FIRST, above
    the `renewals-main` document that actually states the 21:00 line -- because
    the annex document scores on the same words while saying it has no telephone
    line. That is a real discrimination failure of the retrieval path over this
    fixture. It is documented rather than asserted: pinning current behaviour into
    a gate would cement the bug as the contract.
    """
    MAIN = "sources/policy/renewals-main"
    ANNEX = "sources/policy/renewals-annex"
    expectations = {
        "syn-renew-main-count": (MAIN, ANNEX, 1),
        "syn-renew-annex-extension": (ANNEX, MAIN, 2),
        "syn-renew-override": (MAIN, ANNEX, 1),
    }
    results = {result.id: result for result in report.results}
    compared = 0

    for entry_id, (right, wrong, max_rank) in expectations.items():
        retrieved = results[entry_id].retrieved_ids
        assert right in retrieved, f"{entry_id}: {right} not retrieved at all"
        position = retrieved.index(right) + 1
        assert position <= max_rank, (
            f"{entry_id}: correct document {right} fell to rank {position} "
            f"(ceiling {max_rank}) -- retrieved order {retrieved}"
        )
        if wrong in retrieved:
            compared += 1
            assert retrieved.index(right) < retrieved.index(wrong), (
                f"{entry_id}: near-duplicate {wrong} outranked the correct {right} "
                f"-- retrieved order {retrieved}"
            )

    assert compared >= 1, (
        "no query retrieved both halves of the near-duplicate pair, so the "
        "relative-order assertion never ran; the trap is no longer being sprung"
    )


def test_fixture_retains_discriminating_power(report):
    """Guards the fixture itself against being softened into uselessness.

    The cheapest way to make a failing retrieval gate green is to rewrite the
    queries until each one quotes its target document verbatim. That produces a
    fixture where every hit is rank 1 and no ranking regression is observable.
    """
    ranks = [result.rank for result in report.results if result.hit]
    assert ranks, "no hits at all"
    rank_one_share = sum(1 for rank in ranks if rank == 1) / len(ranks)
    assert rank_one_share <= MAX_RANK_ONE_SHARE, (
        f"{rank_one_share:.0%} of hits are at rank 1; the fixture has been made "
        "too easy to detect a ranking regression"
    )


def test_recall_interval_brackets_the_estimate(report):
    """The Wilson interval added on the parent branch, exercised end to end."""
    low, high = report.summary.recall_ci
    assert 0.0 <= low <= report.summary.recall_at_k <= high <= 1.0
    assert high > low, "a 40-case interval cannot have zero width"


def test_significance_bar_flags_a_degraded_engine(engine, golden, report, tmp_path):
    """The other test that matters: the regression detector, proven to detect.

    `compare()` is what stands between a real quality loss and a green commit. A
    bar that never fires is indistinguishable from no bar, and nothing in the
    unit tests for `mcnemar_exact` proves it fires on an actual damaged index.

    Degradation here is amputating the lexical leg -- an empty BM25 index -- which
    is a genuine failure mode (a rebuild that wrote vectors and skipped FTS) and
    leaves only the semantically-empty hash dense leg behind.
    """
    degraded = replace(report, results=report.results)  # keep the baseline intact
    healthy_bm25 = engine.bm25
    engine.bm25 = BM25Index(tmp_path / "empty-fts.sqlite")
    try:
        degraded = run_eval(engine, golden)
    finally:
        engine.bm25 = healthy_bm25

    assert degraded.summary.recall_at_k < report.summary.recall_at_k, (
        "amputating the lexical leg did not lower recall; the fixture is being "
        "answered by something other than the retrieval path under test"
    )
    delta = compare(report, degraded)
    assert delta.hit_to_miss, "no query changed verdict, so nothing was detected"
    assert delta.significant, (
        f"p={delta.p_value} did not clear the significance bar for "
        f"{len(delta.hit_to_miss)} lost queries"
    )


def test_separation_report_is_internally_consistent(engine, report):
    """Negative machinery, exercised without asserting a conclusion it cannot support.

    With `IdentityReranker` the score a result carries is the RRF fusion score --
    a rank-derived constant family, not a relevance magnitude -- so positive and
    negative distributions are identical here by construction and `separable` is
    correctly False. Asserting separability would be asserting an artefact; this
    asserts only that the instrument computes and reports coherently.
    """
    negatives = run_negative(engine, load_negative(NEGATIVE_PATH), k=5)
    assert len(negatives) == len(load_negative(NEGATIVE_PATH))
    assert all(row.error is None for row in negatives), [row.error for row in negatives]

    payload = separation(report.results, negatives).to_dict()
    # separation() counts scored *hits* on the positive side by design: a missed
    # golden entry says nothing about how confidently a correct answer scores.
    assert payload["n_positive"] == report.summary.hits
    assert payload["n_negative"] == len(negatives)
    for side in ("positive", "negative"):
        low = payload[f"{side}_top1_min"]
        median = payload[f"{side}_top1_median"]
        high = payload[f"{side}_top1_max"]
        assert low <= median <= high, f"{side} median {median} outside [{low}, {high}]"
    assert 0.0 <= payload["clean_floor_recall"] <= 1.0


def test_harness_changes_gate_themselves_without_paying_for_the_quality_gate():
    """Routing in `eval-gate.py`: which staged paths earn which gate.

    Two distinct holes are guarded here, both found by reading the routing rather
    than by a failure:

    - Editing the harness or its fixtures used to run NO gate. The one thing such
      a change can break -- the instrument -- was the only thing unchecked.
    - Adding those paths to WATCHED would have been the wrong fix: it drags in the
      60-90s private quality gate, which also appends a row to the corpus's eval
      history, for an edit that provably cannot move retrieval quality. That is
      the friction the WATCHED comment warns turns a gate into something people
      route around.

    Third: a commit touching synthesis AND retrieval used to return after the
    synthesis branch, exempting retrieval from its gate entirely.
    """
    gates_to_run = _load_gate_router()

    assert gates_to_run(["README.md"]) == set()
    assert gates_to_run(["src/alexandria/retrieval/search.py"]) == {"synthetic", "quality"}
    assert gates_to_run(["src/alexandria/synthesis/write.py"]) == {"synthesis"}

    for path in ("src/alexandria/eval/synthetic.py",
                 "scripts/synthetic-eval-gate.py",
                 "tests/test_synthetic_gate.py",
                 "tests/fixtures/synthetic-golden-v1.jsonl",
                 "tests/fixtures/synthetic-corpus/sources/policy/renewals-main.md"):
        assert gates_to_run([path]) == {"synthetic"}, (
            f"{path} must gate the harness and must NOT trigger the private "
            f"quality gate"
        )

    assert gates_to_run([
        "src/alexandria/synthesis/write.py", "src/alexandria/retrieval/search.py",
    ]) == {"synthesis", "synthetic", "quality"}


def _load_gate_router():
    """Import `gates_to_run` from a hyphenated script name imports cannot reach."""
    spec = importlib.util.spec_from_file_location(
        "eval_gate", Path(__file__).resolve().parents[1] / "scripts" / "eval-gate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.gates_to_run


def test_gate_runs_with_no_private_corpus_and_no_network(tmp_path):
    """DEFINITION OF DONE #2, proven by execution rather than asserted.

    Runs `scripts/synthetic-eval-gate.py` in a SUBPROCESS -- not in-process, so no
    environment change can leak back and no already-imported module can quietly
    supply something the clean-clone case would lack -- with HOME pointed at an
    empty directory (making `~/alexandria-corpus` impossible) and HuggingFace
    forced offline (making a model download impossible).

    Then it checks the OUTCOME, not the exit code: 40 cases actually scored. A
    gate that returns 0 because it skipped is a failure wearing a pass, and that
    is exactly the behaviour of `eval-gate.py` on every machine but one.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    assert not (fake_home / "alexandria-corpus").exists()

    environment = {
        "HOME": str(fake_home),
        "PATH": os.environ.get("PATH", ""),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "NO_PROXY": "*",
        "TMPDIR": str(tmp_path),
    }
    completed = subprocess.run(
        [sys.executable, str(GATE_SCRIPT), "--json"],
        capture_output=True, text=True, env=environment, timeout=300,
    )
    assert completed.returncode == 0, completed.stderr

    payload = json.loads(completed.stdout)
    assert payload["failures"] == []
    assert payload["summary"]["n"] == 40, "the gate did not score the full golden set"
    assert payload["summary"]["errors"] == 0
    assert payload["summary"]["recall_at_k"] >= RECALL_FLOOR
    assert payload["n_negatives"] >= 10, "negative set was not run"
