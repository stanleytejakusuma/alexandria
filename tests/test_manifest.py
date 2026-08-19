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
    """A raw embedder with no L2 wrapper, used only to exercise diagnostics."""

    name = "fake-unnormalized"
    revision = ""
    dim = 2
    normalization_policy = "none"

    def embed(self, texts):
        return [[3.0, 4.0] for _ in texts]  # norm == 5.0


class _ThresholdProbeEmbedder:
    """Represents the same backend on two devices whose raw probe straddles
    the old 1e-3 test. CachedEmbedder's L2 wrapper is the compatibility
    contract; the raw measurement is retained only for diagnostics."""

    name = "threshold-model"
    revision = "r1"
    dim = 2

    def __init__(self, raw_probe_norm):
        self.raw_probe_norm = raw_probe_norm

    def embed(self, texts):
        return [[self.raw_probe_norm, 0.0] for _ in texts]


def test_compute_manifest_measures_dim_and_normalization_from_a_real_embed():
    manifest = compute_manifest(_FakeUnnormalizedEmbedder(), "fake")
    assert manifest["provider"] == "fake"
    assert manifest["model"] == "fake-unnormalized"
    assert manifest["dim"] == 2
    assert manifest["normalized"] is False  # raw-provider diagnostic
    assert manifest["normalization_policy"] == "none"
    assert manifest["dtype"] == "float32"


def test_compute_manifest_records_cached_embedder_l2_policy_and_raw_probe_diagnostic(tmp_path):
    manifest = compute_manifest(_hash_embedder(tmp_path), "hash")
    assert manifest["dim"] == 384
    assert manifest["normalized"] is True  # raw HashEmbedder probe is unit length
    assert manifest["normalization_policy"] == "l2"


def test_cached_embedder_enforces_declared_l2_normalization(tmp_path):
    """The policy is not an assertion about a backend. The wrapper actually
    performs it before values reach cache/index callers."""
    embedder = CachedEmbedder(_FakeUnnormalizedEmbedder(), tmp_path / "embeddings.sqlite")

    assert embedder.normalization_policy == "l2"
    assert embedder.embed(["anything"])[0] == pytest.approx([0.6, 0.8])


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


def test_verify_manifest_refuses_a_missing_manifest_with_a_rebuild_hint(tmp_path):
    with pytest.raises(ManifestMissing) as exc_info:
        verify_manifest(tmp_path, _hash_embedder(tmp_path), "hash")
    assert "--rebuild" in str(exc_info.value)


def test_verify_manifest_refuses_a_dimension_mismatch(tmp_path):
    """dim is the ONE field that needs the probe embed, so it is checked only
    after the cheap identity (provider/model/revision/policy/dtype) agrees.

    HashEmbedder's name embeds its dim, so a plain HashEmbedder dim swap would
    be caught by the cheap model-name check and never reach this path. This
    fake keeps the name identical and changes only the width -- the case the
    dim check exists for (same model string, wrong persisted width).
    """
    import json

    class SameNameWrongDim:
        name = "hash-384"
        revision = ""
        normalization_policy = "none"

        def __init__(self, dim):
            self._dim = dim

        @property
        def dim(self):
            return self._dim

        def embed(self, texts):
            return [[0.1] * self._dim for _ in texts]

    # Manifest built by the dim=384 backend.
    write_manifest(tmp_path, SameNameWrongDim(384), "hash")

    # Probe embedder with a different width, same identity otherwise.
    with pytest.raises(ManifestMismatch) as exc_info:
        verify_manifest(tmp_path, SameNameWrongDim(128), "hash")
    assert "dim" in str(exc_info.value)
    assert "model" not in str(exc_info.value), (
        "the cheap identity matched -- the refusal must name dim, not model")


def test_verify_manifest_refuses_a_provider_mismatch(tmp_path):
    """The exact bug the spec names: flipping --embed-provider between an
    incremental index run and a query must not be silently accepted."""
    write_manifest(tmp_path, _hash_embedder(tmp_path), "hash")

    with pytest.raises(ManifestMismatch) as exc_info:
        verify_manifest(tmp_path, _hash_embedder(tmp_path), "mlx")
    assert "provider" in str(exc_info.value)


def test_verify_manifest_accepts_same_l2_policy_when_raw_probe_crosses_old_threshold(tmp_path):
    """A device-specific raw probe must not block an index whose vectors both
    pass through CachedEmbedder's declared, enforced L2 wrapper."""
    first = CachedEmbedder(_ThresholdProbeEmbedder(1.0009), tmp_path / "first.sqlite")
    second = CachedEmbedder(_ThresholdProbeEmbedder(1.0011), tmp_path / "second.sqlite")
    assert compute_manifest(first, "fake")["normalized"] is True
    assert compute_manifest(second, "fake")["normalized"] is False

    write_manifest(tmp_path, first, "fake")
    verify_manifest(tmp_path, second, "fake")


def test_verify_manifest_refuses_a_declared_normalization_policy_mismatch(tmp_path):
    write_manifest(tmp_path, _hash_embedder(tmp_path), "hash")

    with pytest.raises(ManifestMismatch) as exc_info:
        verify_manifest(tmp_path, _FakeUnnormalizedEmbedder(), "hash")
    assert "normalization_policy" in str(exc_info.value)


def test_verify_manifest_reads_but_refuses_a_pre_policy_legacy_manifest(tmp_path):
    """A legacy probe boolean is diagnostic only; it cannot prove every
    existing index vector was wrapper-normalized. The JSON remains readable,
    but incremental mixing requires a rebuild under the declared policy."""
    import json

    embedder = _hash_embedder(tmp_path)
    write_manifest(tmp_path, embedder, "hash")
    path = tmp_path / ".alexandria" / "index" / "manifest.json"
    legacy = json.loads(path.read_text())
    legacy.pop("normalization_policy")
    path.write_text(json.dumps(legacy))

    assert read_manifest(tmp_path)["normalized"] is True
    with pytest.raises(ManifestMismatch, match="normalization_policy"):
        verify_manifest(tmp_path, embedder, "hash")


def test_verify_manifest_refuses_a_legacy_raw_policy(tmp_path):
    """A historical false diagnostic cannot be upgraded to the new enforced
    L2 policy merely to keep startup convenient; that would reintroduce the
    mixed-vector-space failure the guard exists to stop."""
    import json

    embedder = _hash_embedder(tmp_path)
    write_manifest(tmp_path, embedder, "hash")
    path = tmp_path / ".alexandria" / "index" / "manifest.json"
    legacy = json.loads(path.read_text())
    legacy.pop("normalization_policy")
    legacy["normalized"] = False
    path.write_text(json.dumps(legacy))

    with pytest.raises(ManifestMismatch, match="normalization_policy"):
        verify_manifest(tmp_path, embedder, "hash")


def test_verify_manifest_still_refuses_a_real_model_identity_mismatch(tmp_path):
    """Removing the probe diagnostic from equality does not weaken the other
    vector-space identities."""
    import json

    embedder = _hash_embedder(tmp_path)
    write_manifest(tmp_path, embedder, "hash")
    path = tmp_path / ".alexandria" / "index" / "manifest.json"
    changed = json.loads(path.read_text())
    changed["model"] = "another-real-model"
    path.write_text(json.dumps(changed))

    with pytest.raises(ManifestMismatch, match="model"):
        verify_manifest(tmp_path, embedder, "hash")


@pytest.mark.parametrize("invalid", [None, [], "not a manifest", 7, {"provider": "hash"}])
def test_read_manifest_rejects_structurally_invalid_json(tmp_path, invalid):
    """Syntactically valid JSON must not turn into an uncaught AttributeError."""
    import json

    path = tmp_path / ".alexandria" / "index" / "manifest.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(invalid))

    with pytest.raises(ManifestCorrupt):
        read_manifest(tmp_path)


@pytest.mark.parametrize("vector", [[1e308, 1e308], [1e-308, 1e-308]])
def test_manifest_normalized_diagnostic_handles_extreme_finite_probe_values(tmp_path, vector):
    """An extreme raw diagnostic is safe, without being mislabelled as unit."""
    from alexandria.index.manifest import write_manifest

    class ExtremeEmbedder:
        name = "extreme-test"
        dim = 2
        normalization_policy = "l2"

        def embed(self, texts):
            return [vector for _ in texts]

    manifest = write_manifest(tmp_path, ExtremeEmbedder(), "test")
    assert manifest["normalized"] is False


def test_verify_manifest_refuses_a_model_name_mismatch_WITHOUT_loading_the_provider(tmp_path):
    """#27: the cheap identity fields must refuse BEFORE any embed runs.

    On a Linux CI runner there is no torch/MLX and no sentence-transformers
    weights; loading the provider would crash the run. A model-name mismatch is
    knowable from metadata alone, so embedding must never be attempted. This is
    the mutation guard: revert the short-circuit and this test fails because
    embed() is called.
    """
    import json

    from alexandria.index.manifest import ManifestMismatch

    # Write the manifest directly, as a DIFFERENT provider would have left it:
    # write_manifest() probes, which is exactly what this guard exists to avoid.
    path = tmp_path / ".alexandria" / "index" / "manifest.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "provider": "local", "model": "Qwen/Qwen3-Embedding-0.6B",
        "revision": "", "dim": 1024, "normalized": False,
        "normalization_policy": "l2", "dtype": "float32",
    }))

    class NeverLoads:
        name = "a-different-model"
        revision = ""
        normalization_policy = "l2"
        embed_called = False

        @property
        def dim(self):
            raise AssertionError("dim was touched -- the provider would have loaded")

        def embed(self, texts):
            self.embed_called = True
            raise AssertionError("embed ran on a model-name mismatch -- provider loaded")

    with pytest.raises(ManifestMismatch, match="model"):
        verify_manifest(tmp_path, NeverLoads(), "local")
    assert NeverLoads.embed_called is False, (
        "embedding ran before the cheap model-name check could refuse")


def test_verify_manifest_refuses_a_provider_mismatch_WITHOUT_loading_the_provider(tmp_path):
    """Same short-circuit for the provider field, which is also metadata."""
    import json

    from alexandria.index.manifest import ManifestMismatch

    path = tmp_path / ".alexandria" / "index" / "manifest.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "provider": "local", "model": "Qwen/Qwen3-Embedding-0.6B",
        "revision": "", "dim": 1024, "normalized": False,
        "normalization_policy": "l2", "dtype": "float32",
    }))

    class NeverLoads:
        name = "Qwen/Qwen3-Embedding-0.6B"
        revision = ""
        normalization_policy = "l2"
        embed_called = False

        @property
        def dim(self):
            raise AssertionError("dim was touched -- the provider would have loaded")

        def embed(self, texts):
            self.embed_called = True
            raise AssertionError("embed ran on a provider mismatch")

    with pytest.raises(ManifestMismatch, match="provider"):
        verify_manifest(tmp_path, NeverLoads(), "mlx")
    assert NeverLoads.embed_called is False
