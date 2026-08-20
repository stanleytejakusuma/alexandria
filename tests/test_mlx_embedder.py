"""MLXEmbedder: Apple-native embedding provider.

Exists because PyTorch's MPS backend has a confirmed, currently-unfixed graph-cache
leak (pytorch/pytorch#154329) that made a full index run climb system swap by ~10GB.
MLX is Apple's own framework with a different memory model, so this sidesteps the bug
rather than working around it.

Every test here runs offline against a fake -- no model download, no network.
"""

import pytest

from alexandria.index.embedder import MLXEmbedder


class FakeArray:
    def __init__(self, rows):
        self._rows = rows
        self.shape = (len(rows), len(rows[0]) if rows else 0)

    def tolist(self):
        return self._rows


class FakeOutput:
    def __init__(self, rows):
        self.text_embeds = FakeArray(rows)


class FakeModel:
    pass


def make_embedder(rows_for):
    """Build an MLXEmbedder wired to a fake generate()/model, no MLX required."""
    embedder = MLXEmbedder(max_length=640)
    embedder._model = FakeModel()
    embedder._processor = object()
    calls = []

    def fake_generate(model, processor, texts, **kwargs):
        calls.append({"texts": list(texts), **kwargs})
        return FakeOutput(rows_for(list(texts)))

    embedder._generate = fake_generate
    return embedder, calls


def test_returns_one_vector_per_text():
    embedder, _ = make_embedder(lambda ts: [[float(i), 1.0] for i, _ in enumerate(ts)])
    vectors = embedder.embed(["a", "b", "c"])
    assert len(vectors) == 3
    assert all(isinstance(v, list) for v in vectors)
    assert all(isinstance(x, float) for x in vectors[0])


def test_empty_input_is_a_noop_and_never_calls_the_model():
    embedder, calls = make_embedder(lambda ts: [])
    assert embedder.embed([]) == []
    assert calls == []


def test_max_length_is_passed_through_and_exceeds_the_corpus_maximum():
    """mlx_embeddings.generate() defaults to max_length=512, but this corpus produces
    chunks up to 603 tokens -- accepting the default would silently truncate content."""
    embedder, calls = make_embedder(lambda ts: [[1.0, 0.0] for _ in ts])
    embedder.embed(["x"])
    assert calls[0]["max_length"] == 640
    assert MLXEmbedder().max_length >= 603


def test_name_identifies_the_quantized_model():
    """The cache key is sha256(name + text). The name must distinguish this provider
    from the PyTorch one, or 8-bit vectors would silently reuse fp32 cache entries."""
    assert "mlx" in MLXEmbedder().name.lower()
    assert MLXEmbedder().name != "Qwen/Qwen3-Embedding-0.6B"


def test_dim_reports_the_vector_width():
    embedder, _ = make_embedder(lambda ts: [[0.0] * 1024 for _ in ts])
    assert embedder.dim == 1024


def test_missing_mlx_dependency_raises_a_clear_error():
    embedder = MLXEmbedder()
    embedder._import_error = ImportError("No module named 'mlx'")
    with pytest.raises(RuntimeError, match="mlx"):
        embedder.embed(["x"])


# ---------------------------------------------------------------------------
# #44: offline-degradation -- same requirement as LocalEmbedder: fail fast
# and loud on a hung/unreachable load, never silently degrade.
# ---------------------------------------------------------------------------

def test_load_timeout_is_configurable_and_bounded_by_default():
    default = MLXEmbedder()
    assert 0 < default.load_timeout <= 120.0


def test_raises_within_its_bound_on_a_hung_load(monkeypatch):
    import sys
    import time
    import types

    from alexandria.model_load import ModelLoadTimeout

    def hanging_load(model_name):
        time.sleep(30)
        return object(), object()

    fake = types.ModuleType("mlx_embeddings")
    fake.load = hanging_load
    fake.generate = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "mlx_embeddings", fake)

    embedder = MLXEmbedder(load_timeout=0.2)
    started = time.monotonic()
    with pytest.raises(ModelLoadTimeout, match="mlx"):
        embedder.embed(["probe"])
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"embed() blocked the caller for {elapsed:.1f}s past its bound"


def test_timeout_error_names_the_model(monkeypatch):
    import sys
    import time
    import types

    def hanging_load(model_name):
        time.sleep(30)

    fake = types.ModuleType("mlx_embeddings")
    fake.load = hanging_load
    fake.generate = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "mlx_embeddings", fake)

    embedder = MLXEmbedder(model="test/mlx-probe-model", load_timeout=0.1)
    with pytest.raises(Exception, match="test/mlx-probe-model"):
        embedder.embed(["probe"])


def test_a_real_exception_is_not_masked_as_a_timeout(monkeypatch):
    import sys
    import types

    def boom_load(model_name):
        raise OSError("couldn't connect to huggingface.co and nothing is cached")

    fake = types.ModuleType("mlx_embeddings")
    fake.load = boom_load
    fake.generate = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "mlx_embeddings", fake)

    embedder = MLXEmbedder(load_timeout=5.0)
    with pytest.raises(OSError, match="cached"):
        embedder.embed(["probe"])


def test_does_not_retry_the_full_timeout_on_a_second_call(monkeypatch):
    """Same bug class as LocalEmbedder's and the reranker's."""
    import sys
    import time
    import types

    load_calls = []

    def hanging_load(model_name):
        load_calls.append(time.monotonic())
        time.sleep(30)

    fake = types.ModuleType("mlx_embeddings")
    fake.load = hanging_load
    fake.generate = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "mlx_embeddings", fake)

    embedder = MLXEmbedder(load_timeout=0.1)
    started = time.monotonic()
    with pytest.raises(Exception):
        embedder.embed(["probe one"])
    with pytest.raises(Exception):
        embedder.embed(["probe two"])
    elapsed = time.monotonic() - started

    assert elapsed < 0.5, f"second call took {elapsed:.2f}s -- re-attempted the load"
    assert len(load_calls) == 1
