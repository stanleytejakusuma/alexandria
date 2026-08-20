"""Index manifest: which embedding model produced an index's vectors.

SPEC-write-path-and-serve.md §3.3 / gate F4. The embedding *cache* key already
includes model name + revision + mode, so switching ``ALEXANDRIA_EMBED_PROVIDER``
correctly invalidates the cache -- but the *index* (LanceDB table / SQLite
fallback) is a different store with no such identity check. A subsequent
incremental ``index`` run (upsert, not ``--rebuild``) would silently write
vectors from a different model into a column that already holds another
model's vectors: not a crash, not a visibly wrong answer, just quietly
degraded similarity ranking.

The manifest records the compatibility identity for every vector batch:
provider, model, revision, dimension, declared normalization policy, and
dtype. The policy is not an optimistic provider claim: CachedEmbedder is the
common production boundary and L2-normalizes every nonzero finite vector before
it reaches indexing or retrieval. The probe's observed ``normalized`` value is
still recorded for diagnostics, but device-level floating-point variation must
not change compatibility when that wrapper contract is the same. A raw vs.
L2-normalized index silently diverges on distance ranking, and a float32 vs.
int8-quantized index computes distance in a different numeric space; both look
like plausible results while being quietly wrong. Hardware non-determinism
(CPU vs. GPU low-bit differences) is deliberately not an identity field.

A MISSING manifest is not "compatible" -- it is the state of every index
built before this module existed, and treating "absent" as "trust it" would
leave the guard inert exactly where the failure mode is real. Missing and
mismatched both fail loudly. A non-empty pre-policy index must be rebuilt: no
operator assertion or one-vector probe can establish that every stored vector
crossed the declared L2 boundary.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any

MANIFEST_FILE = "manifest.json"

# The fields compared between the on-disk manifest and a freshly computed one.
# ``normalized`` is a raw provider-probe diagnostic, not identity: device-level
# arithmetic can move it across a tolerance even while CachedEmbedder guarantees
# the same L2 vectors at the index/retrieval boundary. ``normalization_policy``
# is identity because it declares that enforced boundary. ``created_at`` is
# provenance, not identity.
IDENTITY_FIELDS = ("provider", "model", "revision", "dim", "normalization_policy", "dtype")

# Old manifests predate this explicit field. Their single raw probe cannot
# prove that every persisted vector passed through a wrapper, so they remain
# readable but are deliberately unverified and cannot be incrementally mixed
# with a new declared policy. Rebuild to establish that representation.
UNVERIFIED_LEGACY_NORMALIZATION_POLICY = "unverified_legacy"

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


def _manifest_path(corpus: str | Path, index_dir: str | Path | None = None) -> Path:
    """#30 P2a: index_dir is an ADDITIVE override, defaulting to None so every
    existing caller (7 sites in cli.py/promote.py) is byte-identical to
    before. When given, the manifest lives directly under it (a staged
    release directory) instead of under the legacy `.alexandria/index/`
    derived from corpus -- this is what lets a release carry its own
    manifest independent of whatever is currently active."""
    if index_dir is not None:
        return Path(index_dir).expanduser() / MANIFEST_FILE
    return Path(corpus).expanduser() / ".alexandria" / "index" / MANIFEST_FILE


def compute_manifest(embedder, provider: str) -> dict[str, Any]:
    """Measure the backend probe and record the enforced vector contract.

    The raw provider probe supplies the model's observed dimension and a useful
    ``normalized`` diagnostic. It cannot define compatibility: CPU/GPU/device
    arithmetic can move a norm across the former 1e-3 threshold. Production
    callers use CachedEmbedder, whose explicit ``normalization_policy`` is
    enforced in code before any vector reaches the index or query path. That
    declared policy, not the diagnostic probe boolean, is the identity field.
    """
    probe_embedder = getattr(embedder, "provider", embedder)
    vector = probe_embedder.embed([_PROBE_TEXT])[0]
    # This field remains only a diagnostic (compatibility uses the declared
    # wrapper policy), but it must not turn finite extreme probe values into an
    # overflowed false diagnostic.
    norm = math.hypot(*vector)
    return {
        "provider": provider,
        "model": embedder.name,
        "revision": getattr(embedder, "revision", ""),
        "dim": len(vector),
        "normalized": abs(norm - 1.0) < 1e-3,
        "normalization_policy": getattr(
            embedder, "normalization_policy",
            getattr(probe_embedder, "normalization_policy", "none"),
        ),
        "dtype": DTYPE,
    }


def write_manifest(corpus: str | Path, embedder, provider: str, *,
                   index_dir: str | Path | None = None) -> dict[str, Any]:
    """Compute and atomically persist the manifest for the current index.

    ``compute_manifest`` records the raw backend probe diagnostic. Also put the
    wrapper-normalized probe in its cache, so later read-side compatibility
    checks can inspect the declared boundary without an avoidable provider
    load during server startup.

    ``index_dir`` (#30 P2a): see `_manifest_path`.
    """
    manifest = compute_manifest(embedder, provider)
    if hasattr(embedder, "normalization_policy"):
        embedder.embed([_PROBE_TEXT])
    manifest["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    path = _manifest_path(corpus, index_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    tmp.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return manifest


def read_manifest(corpus: str | Path, *, index_dir: str | Path | None = None) -> dict[str, Any] | None:
    """Return the on-disk manifest, or None if none has ever been written.

    Raises ManifestCorrupt if the file exists but cannot be parsed -- treated
    as distinct from "missing" for the same reason as generation.json (see
    cache.GenerationFileCorrupt): silently falling back to "no manifest" on a
    corrupt-but-present file would make a real backfill invisible.

    ``index_dir`` (#30 P2a): see `_manifest_path`.
    """
    path = _manifest_path(corpus, index_dir)
    if not path.exists():
        return None
    try:
        manifest = json.loads(path.read_text())
    except (OSError, ValueError, TypeError) as exc:
        raise ManifestCorrupt(f"{path} exists but could not be parsed: {exc}") from exc
    # A syntactically valid JSON scalar/list/partial object is still corrupt
    # manifest data. Validate at this boundary so CLI/serve catch ManifestCorrupt
    # rather than leaking AttributeError while calling ``.get`` below. Omitted
    # normalization_policy remains the one intentional legacy representation.
    required = {"provider": str, "model": str, "revision": str, "dim": int,
                "normalized": bool, "dtype": str}
    if not isinstance(manifest, dict):
        raise ManifestCorrupt(f"{path} must contain a JSON object")
    for field, expected_type in required.items():
        value = manifest.get(field)
        if (not isinstance(value, expected_type)
                or (expected_type is int and isinstance(value, bool))):
            raise ManifestCorrupt(
                f"{path} has invalid or missing {field!r} field")
    if manifest["dim"] <= 0:
        raise ManifestCorrupt(f"{path} has invalid non-positive 'dim' field")
    policy = manifest.get("normalization_policy")
    if policy is not None and policy not in {"l2", "none"}:
        raise ManifestCorrupt(f"{path} has invalid 'normalization_policy' field")
    if manifest["dtype"] != DTYPE:
        raise ManifestCorrupt(f"{path} has unsupported 'dtype' field")
    return manifest


def verify_manifest_for_write(corpus: str | Path, embedder, provider: str, store) -> None:
    """Guard the WRITE path: refuse to add vectors to a non-empty index that a
    different model built.

    verify_manifest() guards readers, but a reader can only observe corruption a
    writer already committed -- and an index run then overwrites the manifest to
    match whatever it just wrote, so the read-path guard passes forever after.
    The guard has to sit where the damage is done.

    An empty index has no vectors to disagree with, so both a missing manifest
    and a provider change are legitimate there; the manifest is written at the
    end of the run. That also covers --rebuild, which drops the table first.

    #30 P2a: checks the manifest at `store.path` -- wherever THIS store
    actually writes (the active release once one exists, the legacy path
    otherwise) -- never a separately-resolved directory that could drift out
    of sync with what `store` itself points at.
    """
    if store.count() == 0:
        return
    index_dir = getattr(store, "path", None)
    verify_manifest(corpus, embedder, provider, index_dir=index_dir)


def _current_compatibility_cheap(embedder, provider: str) -> dict[str, Any]:
    """The identity fields knowable WITHOUT loading the provider (#27).

    provider, model, revision, normalization_policy and dtype are metadata:
    the embedder exposes them without materialising weights. dim is the
    exception -- every real provider derives it from the loaded model -- so it
    is measured separately once the cheap identity agrees. That ordering is
    what lets a provider/model mismatch refuse instantly (Linux CI runner,
    wrong --embed-provider) instead of after a multi-minute model load.
    """
    return {
        "provider": provider,
        "model": embedder.name,
        "revision": getattr(embedder, "revision", ""),
        "normalization_policy": getattr(embedder, "normalization_policy", "none"),
        "dtype": DTYPE,
    }


def _current_compatibility(embedder, provider: str) -> dict[str, Any]:
    """Return only current vector-space identity, not raw probe diagnostics.

    Calling through CachedEmbedder both verifies the vector width at the actual
    L2 boundary and reuses the probe write_manifest cached. It deliberately
    does not compare/re-measure the raw norm: that value is operational
    diagnostics, and can drift across devices without changing wrapper output.
    """
    cheap = _current_compatibility_cheap(embedder, provider)
    vector = embedder.embed([_PROBE_TEXT])[0]
    return {**cheap, "dim": len(vector)}


def verify_manifest(corpus: str | Path, embedder, provider: str, *,
                    index_dir: str | Path | None = None,
                    allow_unverified_legacy: bool = False) -> None:
    """Refuse loudly on a missing or mismatched manifest (gate F4).

    Provider/model/revision/dimension/declared-policy/dtype are strict
    identities. ``normalized`` is deliberately absent from that comparison:
    it is a raw probe diagnostic, while CachedEmbedder's L2 policy is enforced
    on every vector that reaches index or retrieval.

    Raises ManifestMissing / ManifestMismatch / ManifestCorrupt; callers on
    the CLI/serve boundary should catch these and exit with the message
    (see cli.py's _build_search_engine, the single chokepoint search,
    answer, and eval all go through).

    ``index_dir`` (#30 P2a): see `_manifest_path`.

    ``allow_unverified_legacy`` (#45, READ PATH ONLY, default False -- every
    existing caller is unaffected): a pre-policy manifest's single raw probe
    cannot prove every vector was L2-normalized, so by default it stays a
    hard refusal (see the tests above this one, which pin that unchanged
    behavior). Cosine similarity is scale-invariant by construction --
    dot(a,b)/(|a||b|) -- so an index whose vectors happen to be UNNORMALIZED
    ranks identically under cosine distance to one that is; the risk this
    guard actually protects against is LanceDB's DEFAULT search metric,
    which is raw L2 distance, not cosine, and IS scale-sensitive (measured:
    a same-direction vector at 100x magnitude ranked as if it were nearly
    orthogonal). The caller opting in here MUST also force cosine distance
    at the store (VectorStore's ``force_cosine_metric``) -- this function
    only relaxes the manifest comparison; it does not make a read safe by
    itself. Writes must NEVER opt in: verify_manifest_for_write has no such
    parameter (pinned by test_verify_manifest_for_write_never_accepts_the_
    opt_in), because incrementally writing a KNOWN-normalized vector into a
    column of UNKNOWN normalization is the actual mixing this guard exists
    to prevent -- that risk is unrelated to which distance metric a query
    later uses.
    """
    on_disk = read_manifest(corpus, index_dir=index_dir)
    if on_disk is None:
        raise ManifestMissing(
            f"index at {corpus} has no manifest -- cannot verify which "
            f"embedding model produced its vectors, so mixing providers in "
            f"one index would silently corrupt similarity ranking. If this "
            f"index predates declared normalization policy, rebuild it under "
            f"the desired configuration: alexandria --corpus {corpus} index --rebuild"
        )
    on_disk_policy = on_disk.get(
        "normalization_policy", UNVERIFIED_LEGACY_NORMALIZATION_POLICY)
    on_disk_identity = on_disk | {"normalization_policy": on_disk_policy}
    is_unverified_legacy = on_disk_policy == UNVERIFIED_LEGACY_NORMALIZATION_POLICY
    if allow_unverified_legacy and is_unverified_legacy:
        # #45's "ratchet" (Red review, 2026-08-20): without this, the
        # relaxation removes ALL rebuild pressure forever -- a legacy index
        # becomes permanently comfortable with no nudge toward the verified
        # state. Never blocks the read, matching the existing liveness-stale
        # warning pattern at the CLI boundary.
        print(f"alexandria: serving an unverified_legacy index at "
              f"{_manifest_path(corpus, index_dir)} -- normalization cannot be "
              f"proven for its existing vectors. Rebuild when convenient: "
              f"alexandria --corpus {corpus} index --rebuild", file=sys.stderr)
    # The ONE field the opt-in relaxes. Every other field stays exactly as
    # strict, opted in or not -- provider/model/revision/dim/dtype determine
    # whether vectors are even comparable at all, which no metric choice can
    # fix.
    policy_fields = (("provider", "model", "revision", "dtype") if
                     (allow_unverified_legacy and is_unverified_legacy) else
                     ("provider", "model", "revision", "normalization_policy", "dtype"))

    # CHEAP FIELDS FIRST (#27): provider/model/revision/policy/dtype are
    # metadata, so a mismatch refuses here without loading the provider. dim
    # is the only field requiring the probe embed, checked only after the
    # cheap identity agrees.
    cheap = _current_compatibility_cheap(embedder, provider)
    cheap_diffs = [f"{field}: index={on_disk_identity.get(field)!r} current={cheap[field]!r}"
                   for field in policy_fields
                   if on_disk_identity.get(field) != cheap[field]]
    if cheap_diffs:
        raise ManifestMismatch(
            f"embedding config does not match the index manifest at "
            f"{_manifest_path(corpus)} ({'; '.join(cheap_diffs)}). Mixing vector "
            f"spaces in one index silently corrupts similarity ranking. "
            f"Either restore the original embedding config, or rebuild: "
            f"alexandria --corpus {corpus} index --rebuild"
        )

    fresh = {**cheap, "dim": len(embedder.embed([_PROBE_TEXT])[0])}
    identity_fields = (tuple(f for f in IDENTITY_FIELDS if f != "normalization_policy")
                       if (allow_unverified_legacy and is_unverified_legacy) else IDENTITY_FIELDS)
    diffs = [f"{field}: index={on_disk_identity.get(field)!r} current={fresh[field]!r}"
              for field in identity_fields if on_disk_identity.get(field) != fresh[field]]
    if diffs:
        raise ManifestMismatch(
            f"embedding config does not match the index manifest at "
            f"{_manifest_path(corpus)} ({'; '.join(diffs)}). Mixing vector "
            f"spaces in one index silently corrupts similarity ranking. "
            f"Either restore the original embedding config, or rebuild: "
            f"alexandria --corpus {corpus} index --rebuild"
        )
