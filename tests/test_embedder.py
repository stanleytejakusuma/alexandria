"""Embedders are deterministic offline in tests and cache model work by content."""

from pathlib import Path

from alexandria.index.embedder import CachedEmbedder, HashEmbedder


class CountingEmbedder:
    name = "counting-v1"
    dim = 3

    def __init__(self):
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[float(len(text)), 0.0, 1.0] for text in texts]


def test_hash_embedder_is_deterministic_and_dependency_free():
    embedder = HashEmbedder(dim=16)
    assert embedder.embed(["alpha", "beta", "alpha"])[0] == embedder.embed(["alpha"])[0]
    assert len(embedder.embed(["alpha"])[0]) == 16
    assert embedder.embed(["alpha"])[0] != embedder.embed(["beta"])[0]


def test_cached_embedder_only_calls_provider_for_new_content(tmp_path: Path):
    provider = CountingEmbedder()
    embedder = CachedEmbedder(provider, tmp_path / "cache" / "embeddings.sqlite")

    first = embedder.embed(["same", "other", "same"])
    second = embedder.embed(["other", "same"])

    assert provider.calls == [["same", "other"]]
    assert first[0] == second[1]
    assert embedder.last_cache_stats == {"hits": 2, "misses": 0}


def test_embedding_cache_key_includes_model_name(tmp_path: Path):
    first_provider = CountingEmbedder()
    CachedEmbedder(first_provider, tmp_path / "embeddings.sqlite").embed(["same"])

    second_provider = CountingEmbedder()
    second_provider.name = "counting-v2"
    CachedEmbedder(second_provider, tmp_path / "embeddings.sqlite").embed(["same"])

    assert len(first_provider.calls) == 1
    assert len(second_provider.calls) == 1


def test_corrupt_cache_value_is_recomputed(tmp_path: Path):
    provider = CountingEmbedder()
    cache_path = tmp_path / "embeddings.sqlite"
    embedder = CachedEmbedder(provider, cache_path)
    embedder.embed(["same"])
    embedder._connection.execute("UPDATE embeddings SET vector = 'not json'")
    embedder._connection.commit()

    embedder.embed(["same"])

    assert provider.calls == [["same"], ["same"]]
