from types import SimpleNamespace

from alexandria.eval.golden import GoldenEntry
from alexandria.eval.runner import run_eval


class Result:
    def __init__(self, doc_id: str):
        self.doc_id = doc_id


class Store:
    def count(self) -> int:
        return 42


class FakeEngine:
    def __init__(self, results_by_query):
        self.results_by_query = results_by_query
        self.embedder = SimpleNamespace(name="hash-24")
        self.reranker = SimpleNamespace(model_name="fake-reranker", half_precision=True)
        self.config = SimpleNamespace(prefetch=20, top_k=5, rrf_k=60, wiki_boost=1.25)
        self.store = Store()

    def search(self, query, *, k=None):
        value = self.results_by_query[query]
        if isinstance(value, Exception):
            raise value
        return [Result(doc_id) for doc_id in value]


def test_run_eval_scores_document_ids_any_of_and_records_fingerprint():
    engine = FakeEngine({"find it": ["sources/noise", "sources/alternate", "sources/late"]})
    entry = GoldenEntry("any-of", "find it", ("sources/primary", "sources/alternate"), 2)

    report = run_eval(engine, [entry])

    result = report.results[0]
    assert result.hit is True
    assert result.rank == 2
    assert result.retrieved_ids == ["sources/noise", "sources/alternate"]
    assert report.summary.recall_at_k == 1.0
    assert report.summary.mrr == 0.5
    assert report.config == {
        "embedder": "hash-24",
        "reranker": {"name": "fake-reranker", "precision": "fp16"},
        "prefetch": 20,
        "top_k": 5,
        "rrf_k": 60,
        "wiki_boost": 1.25,
    }
    assert report.corpus_chunks == 42
    assert report.git_sha


def test_run_eval_can_go_red_when_retrieval_returns_no_golden_targets():
    engine = FakeEngine({"missing": []})

    report = run_eval(engine, [GoldenEntry("must-fail", "missing", ("sources/wanted",), 5)])

    assert report.summary.recall_at_k == 0.0
    assert report.summary.hits == 0
    assert report.summary.misses == ["must-fail"]
    assert report.results[0].hit is False


def test_run_eval_records_query_errors_without_aborting_or_counting_a_hit():
    engine = FakeEngine({"broken": RuntimeError("offline"), "works": ["sources/good"]})
    entries = [
        GoldenEntry("broken", "broken", ("sources/wanted",), 5),
        GoldenEntry("works", "works", ("sources/good",), 5),
    ]

    report = run_eval(engine, entries)

    assert [result.id for result in report.results] == ["broken", "works"]
    assert report.results[0].error == "RuntimeError: offline"
    assert report.summary.errors == 1
    assert report.summary.error_ids == ["broken"]
    assert report.summary.recall_at_k == 0.5
