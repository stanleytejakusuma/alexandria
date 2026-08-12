"""Index manifest: which embedding model produced an index's vectors.

SPEC-write-path-and-serve.md §3.3 / gate F4. The embedding *cache* key already
includes model name + revision + mode, so switching ``ALEXANDRIA_EMBED_PROVIDER``
correctly invalidates the cache -- but the *index* (LanceDB table / SQLite
fallback) is a different store with no such identity check. A subsequent
incremental ``index`` run (upsert, not ``--rebuild``) would silently write
vectors from a different model into a column that already holds another
model's vectors: not a crash, not a visibly wrong answer, just quietly
degraded similarity ranking.

The manifest records the six fields that determine whether two batches of
vectors are comparable: provider, model, revision, dimension, normalization,
and dtype. Normalization and dtype are measured, not assumed -- a raw vs.
L2-normalized index silently diverges on cosine ranking, and a float32 vs.
int8-quantized index computes distance in a different numeric space; both
look like plausible results while being quietly wrong. Hardware
non-determinism (CPU vs. GPU low-bit differences) is deliberately not
recorded: it sits far below the ranking noise floor.

A MISSING manifest is not "compatible" -- it is the state of every index
built before this module existed, and treating "absent" as "trust it" would
leave the guard inert exactly where the failure mode is real. Missing and
mismatched both fail loudly; a one-time backfill command
(``alexandria index --backfill-manifest``) lets an operator assert "this
existing index was in fact built with the current --embed-provider config"
without paying to re-embed the whole corpus.
"""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any

MANIFEST_FILE = "manifest.json"

# The fields compared between the on-disk manifest and a freshly computed one.
# created_at is provenance, not identity -- deliberately excluded.
IDENTITY_FIELDS = ("provider", "model", "revision", "dim", "normalized", "dtype")

# All vectors in this codebase end up as plain Python float lists (`float()`
# cast in CachedEmbedder.embed and every provider's embed()) -- there is no
# evidence anywhere of an int8-stored-vector path, so this is recorded as a
# constant rather than introspected per call. If a quantized-storage provider
# is ever added, this becomes the one place that needs to start varying.
DTYPE = "float32"

_PROBE_TEXT = "__alexandria_manifest_probe__"


class ManifestCorrupt(Exception):
    """manifest.json exists but could not be parsed."""


class ManifestMissing(Exception):
    """No manifest exists for this index yet."""


class ManifestMismatch(Exception):
    """The current embedding config does not match the index manifest."""


def _manifest_path(corpus: str | Path) -> Path:
    return Path(corpus).expanduser() / ".alexandria" / "index" / MANIFEST_FILE


def compute_manifest(embedder, provider: str) -> dict[str, Any]:
    """Measure the current embedder's identity by embedding one probe string.

    Dimension and normalization are *measured* (embed a probe, check its
    length and L2 norm) rather than read from provider metadata, because
    ``.dim`` itself forces a model load for the local/MLX providers -- there
    is no cheaper way to know either fact honestly, and the load is about to
    happen anyway for any real query on this engine.
    """
    vector = embedder.embed([_PROBE_TEXT])[0]
    norm = math.sqrt(sum(value * value for value in vector))
    return {
        "provider": provider,
        "model": embedder.name,
        "revision": getattr(embedder, "revision", ""),
        "dim": len(vector),
        "normalized": abs(norm - 1.0) < 1e-3,
        "dtype": DTYPE,
    }


def write_manifest(corpus: str | Path, embedder, provider: str) -> dict[str, Any]:
    """Compute and atomically persist the manifest for the current index."""
    manifest = compute_manifest(embedder, provider)
    manifest["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    path = _manifest_path(corpus)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    tmp.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return manifest


def read_manifest(corpus: str | Path) -> dict[str, Any] | None:
    """Return the on-disk manifest, or None if none has ever been written.

    Raises ManifestCorrupt if the file exists but cannot be parsed -- treated
    as distinct from "missing" for the same reason as generation.json (see
    cache.GenerationFileCorrupt): silently falling back to "no manifest" on a
    corrupt-but-present file would make a real backfill invisible.
    """
    path = _manifest_path(corpus)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError, TypeError) as exc:
        raise ManifestCorrupt(f"{path} exists but could not be parsed: {exc}") from exc


def verify_manifest(corpus: str | Path, embedder, provider: str) -> None:
    """Refuse loudly on a missing or mismatched manifest (gate F4).

    Raises ManifestMissing / ManifestMismatch / ManifestCorrupt; callers on
    the CLI/serve boundary should catch these and exit with the message
    (see cli.py's _build_search_engine, the single chokepoint search,
    answer, and eval all go through).
    """
    on_disk = read_manifest(corpus)
    if on_disk is None:
        raise ManifestMissing(
            f"index at {corpus} has no manifest -- cannot verify which "
            f"embedding model produced its vectors, so mixing providers in "
            f"one index would silently corrupt similarity ranking. If this "
            f"index predates manifests and was in fact built with the "
            f"current --embed-provider config, run once: "
            f"alexandria --corpus {corpus} index --backfill-manifest"
        )
    fresh = compute_manifest(embedder, provider)
    diffs = [f"{field}: index={on_disk.get(field)!r} current={fresh[field]!r}"
              for field in IDENTITY_FIELDS if on_disk.get(field) != fresh[field]]
    if diffs:
        raise ManifestMismatch(
            f"embedding config does not match the index manifest at "
            f"{_manifest_path(corpus)} ({'; '.join(diffs)}). Mixing vector "
            f"spaces in one index silently corrupts similarity ranking. "
            f"Either restore the original embedding config, or rebuild: "
            f"alexandria --corpus {corpus} index --rebuild"
        )
