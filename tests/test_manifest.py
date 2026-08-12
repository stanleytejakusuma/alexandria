"""Gate F4: an index carries a manifest naming its embedding provider, model,
revision, dimension, normalization, and dtype; a missing or mismatched
manifest must refuse loudly rather than silently mix vector spaces in one
index (see src/alexandria/index/manifest.py for the full rationale)."""

import pytest

from alexandria.index.embedder import CachedEmbedder, HashEmbedder
from alexandria.index.manifest import (
    ManifestCorrupt,
    ManifestMismatch,
    ManifestMissing,
    compute_manifest,
    read_manifest,
    verify_manifest,
    write_manifest,
)


def _hash_embedder(tmp_path, dim=384):
    return CachedEmbedder(HashEmbedder(dim=dim), tmp_path / "embeddings.sqlite")


class _FakeUnnormalizedEmbedder:
    """embed() returns a vector whose L2 norm is deliberately not 1.0, so
    compute_manifest's normalization detection is exercised on a real
    non-normalized case rather than only ever seeing HashEmbedder's
    already-normalized output."""

    name = "fake-unnormalized"
    revision = ""

    def embed(self, texts):
        return [[3.0, 4.0] for _ in texts]  # norm == 5.0


def test_compute_manifest_measures_dim_and_normalization_from_a_real_embed():
    manifest = compute_manifest(_FakeUnnormalizedEmbedder(), "fake")
    assert manifest["provider"] == "fake"
    assert manifest["model"] == "fake-unnormalized"
    assert manifest["dim"] == 2
    assert manifest["normalized"] is False
    assert manifest["dtype"] == "float32"


def test_compute_manifest_detects_a_normalized_embedder(tmp_path):
    manifest = compute_manifest(_hash_embedder(tmp_path), "hash")
    assert manifest["dim"] == 384
    assert manifest["normalized"] is True  # HashEmbedder divides by its own L2 norm


def test_write_then_read_roundtrips(tmp_path):
    written = write_manifest(tmp_path, _hash_embedder(tmp_path), "hash")
    assert "created_at" in written
    on_disk = read_manifest(tmp_path)
    assert on_disk == written


def test_read_manifest_returns_none_when_never_written(tmp_path):
    assert read_manifest(tmp_path) is None


def test_write_manifest_is_atomic_leaves_no_temp_file(tmp_path):
    write_manifest(tmp_path, _hash_embedder(tmp_path), "hash")
    index_dir = tmp_path / ".alexandria" / "index"
    leftovers = list(index_dir.glob("manifest.json.tmp*"))
    assert leftovers == [], f"temp file(s) left behind: {leftovers}"


def test_read_manifest_fails_loud_on_a_corrupt_but_present_file(tmp_path):
    """Mutation check: a bare `except: return None` in read_manifest would
    make a corrupt manifest indistinguishable from a missing one, hiding a
    real backfill from ever happening. This must raise, not return None."""
    index_dir = tmp_path / ".alexandria" / "index"
    index_dir.mkdir(parents=True)
    (index_dir / "manifest.json").write_text("{not valid json")

    with pytest.raises(ManifestCorrupt):
        read_manifest(tmp_path)


def test_verify_manifest_passes_silently_when_config_matches(tmp_path):
    embedder = _hash_embedder(tmp_path)
    write_manifest(tmp_path, embedder, "hash")
    verify_manifest(tmp_path, embedder, "hash")  # must not raise


def test_verify_manifest_refuses_a_missing_manifest_with_a_backfill_hint(tmp_path):
    with pytest.raises(ManifestMissing) as exc_info:
        verify_manifest(tmp_path, _hash_embedder(tmp_path), "hash")
    assert "--backfill-manifest" in str(exc_info.value)


def test_verify_manifest_refuses_a_dimension_mismatch(tmp_path):
    write_manifest(tmp_path, _hash_embedder(tmp_path, dim=384), "hash")

    with pytest.raises(ManifestMismatch) as exc_info:
        verify_manifest(tmp_path, _hash_embedder(tmp_path, dim=128), "hash")
    assert "dim" in str(exc_info.value)


def test_verify_manifest_refuses_a_provider_mismatch(tmp_path):
    """The exact bug the spec names: flipping --embed-provider between an
    incremental index run and a query must not be silently accepted."""
    write_manifest(tmp_path, _hash_embedder(tmp_path), "hash")

    with pytest.raises(ManifestMismatch) as exc_info:
        verify_manifest(tmp_path, _hash_embedder(tmp_path), "mlx")
    assert "provider" in str(exc_info.value)


def test_verify_manifest_refuses_a_normalization_mismatch(tmp_path):
    write_manifest(tmp_path, _hash_embedder(tmp_path), "hash")

    with pytest.raises(ManifestMismatch) as exc_info:
        verify_manifest(tmp_path, _FakeUnnormalizedEmbedder(), "hash")
    assert "normalized" in str(exc_info.value) or "dim" in str(exc_info.value)
