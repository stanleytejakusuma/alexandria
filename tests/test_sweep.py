"""Offline tests for the full-sweep orchestrator (WORK-ORDER-phase2-full-sweep.md).

All pipeline/clustering calls are fakes -- this order tests ORCHESTRATION:
fold correctness, exhaustive accounting (the load-bearing test), determinism,
resumability, cross-page linking. No LLM, no corpus, no embedder.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alexandria.index.chunker import Chunk
from alexandria.synthesis.sweep import SweepResult, run_sweep


class FakePipeline:
    """Records calls; emits one page per topic with all gathered chunk ids
    as cited claims (so covered-map accounting is inspectable)."""

    def __init__(self, corpus_root: Path) -> None:
        self.corpus_root = corpus_root
        self.calls: list[str] = []
        self.fail_on: set[str] = set()
        self.crash_on: set[str] = set()

    def __call__(self, engine, topic_query, **kwargs):
        self.calls.append(topic_query)
        topic = topic_query.split("|")[0]
        if topic in self.crash_on:
            raise RuntimeError(f"injected crash on {topic}")
        if topic in self.fail_on:
            return type("R", (), {"passed": False, "emitted": False, "page_path": None,
                                  "page": type("P", (), {"claims": []})(),
                                  "verdict": type("V", (), {"failed_claim_ids": set()})(),
                                  "gathered": type("G", (), {"chunks": []})()})()
        page_path = self.corpus_root / "wiki" / f"{topic}.md"
        page_path.parent.mkdir(parents=True, exist_ok=True)
        page_path.write_text(f"# {topic}\n", encoding="utf-8")
        return type("R", (), {
            "emitted": True, "page_path": page_path,
            "skip_log_path": None, "passed": True,
            "page": type("P", (), {"claims": []})(),
            "verdict": type("V", (), {"failed_claim_ids": set()})(),
            "gathered": type("G", (), {"chunks": []})(),
        })()


def _chunks(docs: dict[str, str]) -> list[Chunk]:
    out = []
    for doc_id, text in docs.items():
        out.append(Chunk(doc_id=doc_id, chunk_id=f"{doc_id}#0", text=text))
    return out


def _clusters(chunks, *, threshold, embedder):
    """Scripted clustering: pairs by doc_id prefix (a+b, x+y), singletons alone."""
    from alexandria.synthesis.clustering import TopicCluster
    groups: dict[str, list[Chunk]] = {}
    for c in chunks:
        groups.setdefault(c.doc_id[0], []).append(c)
    out = []
    for key, members in sorted(groups.items()):
        out.append(TopicCluster(
            cluster_id=f"topic-{key}",
            member_ids=tuple(sorted(m.chunk_id for m in members)),
            representative_id=members[0].chunk_id,
            representative_text=members[0].text,
        ))
    return out


def _run(tmp_path, docs, *, crash_on=None, checkpoint=None, resume=False):
    chunks = _chunks(docs)
    fake = FakePipeline(tmp_path)
    fake.crash_on = crash_on or set()
    result = run_sweep(
        chunks,
        engine=None,
        pipeline_impl=fake,
        clustering_impl=_clusters,
        topic_threshold=0.75,
        embedder=None,
        corpus_root=tmp_path,
        checkpoint_path=checkpoint or tmp_path / "sweep.json",
        resume=resume,
    )
    return result, fake


def test_exhaustive_accounting_every_doc_lands_somewhere(tmp_path):
    docs = {"a1": "alpha topic", "a2": "alpha topic again",
            "b1": "beta topic", "b2": "beta topic again",
            "c1": "loner doc with no topic peers"}
    result, fake = _run(tmp_path, docs)
    chunk_by_id = {c.chunk_id: c for c in _chunks(docs)}
    processed = {chunk_by_id[mid].doc_id for cluster in result.topics for mid in cluster.member_ids}
    excluded = set(result.excluded_docs)
    assert processed | excluded == set(docs)
    assert processed & excluded == set()
    assert "c1" in excluded and result.excluded_docs["c1"] == "no_cluster_match"
    assert len(fake.calls) == 2  # only the two multi-member topics ran


def test_unaccounted_document_is_a_hard_failure(tmp_path):
    """The order's load-bearing test: one doc silently missing from both
    processed clusters and the exclusion log must RAISE, not warn."""
    chunks = _chunks({"a1": "alpha", "a2": "alpha again", "ghost": "unseen"})

    def sneaky_clustering(chunks, *, threshold, embedder):
        # returns a cluster for a1/a2 only -- 'ghost' is in no cluster
        from alexandria.synthesis.clustering import TopicCluster
        members = [c for c in chunks if c.doc_id != "ghost"]
        return [TopicCluster("topic-a", tuple(sorted(m.chunk_id for m in members)),
                             members[0].chunk_id, members[0].text)]

    fake = FakePipeline(tmp_path)
    with pytest.raises(RuntimeError, match="accounting FAILED"):
        run_sweep(chunks, engine=None, pipeline_impl=fake, clustering_impl=sneaky_clustering,
                  topic_threshold=0.75, embedder=None, corpus_root=tmp_path,
                  checkpoint_path=tmp_path / "sweep.json", resume=False)


def test_deterministic_order_and_result(tmp_path):
    docs = {"x1": "x topic", "x2": "x topic again", "y1": "y topic", "y2": "y again"}
    r1, f1 = _run(tmp_path, docs)
    r2, f2 = _run(tmp_path, docs)
    assert [c.cluster_id for c in r1.topics] == [c.cluster_id for c in r2.topics]
    assert f1.calls == f2.calls
    assert r1.excluded_docs == r2.excluded_docs
    assert r1.pages == r2.pages


def test_resume_skips_completed_topics_and_continues(tmp_path):
    docs = {"a1": "alpha", "a2": "alpha again", "b1": "beta", "b2": "beta again"}
    checkpoint = tmp_path / "sweep.json"
    # first run: crash after the first topic (the crash propagates -- the
    # checkpoint is the durability contract, not the return value)
    with pytest.raises(RuntimeError, match="injected crash"):
        _run(tmp_path, docs, crash_on={"beta"}, checkpoint=checkpoint)
    assert checkpoint.exists()
    # resume: first topic is skipped, second completes
    result2, fake2 = _run(tmp_path, docs, checkpoint=checkpoint, resume=True)
    assert fake2.calls == ["beta"]
    assert len(result2.pages) == 2
    state = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert len(state["completed"]) == 2


def test_failed_topic_recorded_not_emitted(tmp_path):
    docs = {"a1": "alpha", "a2": "alpha again"}
    fake = FakePipeline(tmp_path)
    fake.fail_on = {"alpha"}
    result = run_sweep(_chunks(docs), engine=None, pipeline_impl=fake,
                       clustering_impl=_clusters, topic_threshold=0.75, embedder=None,
                       corpus_root=tmp_path, checkpoint_path=tmp_path / "s.json", resume=False)
    assert result.pages == ()
    assert result.failed_topics == ("topic-a",)


def test_cross_page_redundancy_links_to_prior_page(tmp_path):
    """If a topic's gathered chunks are all already covered by an earlier
    page, the node links instead of re-synthesizing (cross-page dup guard)."""
    chunks = _chunks({"a1": "alpha", "a2": "alpha again", "b1": "alpha", "b2": "alpha again"})
    # scripted clustering puts a1/a2 and b1/b2 in SEPARATE topics with the
    # same text, so the second topic's chunks are all covered by the first.
    from alexandria.synthesis.clustering import TopicCluster

    def pairing_clustering(chunks, *, threshold, embedder):
        return [
            TopicCluster("topic-a", ("a1#0", "a2#0"), "a1#0", "alpha|A"),
            TopicCluster("topic-b", ("b1#0", "b2#0"), "b1#0", "alpha|B"),
        ]

    fake = FakePipeline(tmp_path)
    result = run_sweep(chunks, engine=None, pipeline_impl=fake,
                       clustering_impl=pairing_clustering, topic_threshold=0.75,
                       embedder=None, corpus_root=tmp_path,
                       checkpoint_path=tmp_path / "s.json", resume=False)
    assert len(fake.calls) == 2  # both nodes ran (the fake gathers nothing, so no link)
    assert len(result.pages) == 2
    assert result.linked_topics == ()
