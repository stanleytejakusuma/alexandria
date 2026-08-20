"""#30 P2a: staged releases -- build a complete new index beside the live
one, validate it, then atomically swap ONE pointer file.

The problem this closes: `alexandria index --rebuild` currently drops the
live LanceDB and FTS tables IN PLACE before refilling them. P1 (IndexReadLock
+ rebuild_marker, already shipped) makes that window fail closed for readers
-- no torn read -- but a crash mid-rebuild still leaves the corpus with no
working index until a NEW rebuild succeeds, which is unbounded if the
failure is persistent. That is the actual remaining gap: recoverability, not
downtime.

Layout, additive to the existing flat one (see
docs/DECISION-staged-releases-p2a.md for the full rationale):

    .alexandria/index/
        active.json              <- {"release_id": "...", "activated_at": "..."}
        releases/
            <release-id>/
                chunks.lance/    <- VectorStore(releases/<id>)
                fts.sqlite       <- BM25Index(releases/<id>/fts.sqlite)
                manifest.json    <- write_manifest(..., index_dir=releases/<id>)
                checksums.json   <- this module's checksum_release()
        chunks.lance/            <- LEGACY, unmanaged; read as a release iff
        fts.sqlite               <-   no active.json exists yet (one-time
        manifest.json            <-   migration path, never written to again
                                       once a real release is activated)

A corpus with no active.json (every pre-P2a corpus, and any corpus that has
never rebuilt since) resolves to the legacy flat layout unchanged -- P2a
requires zero migration step to keep an existing corpus serving.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

__all__ = [
    "ActiveReleaseMissing",
    "ReleaseCorrupt",
    "ReleaseNotFound",
    "activate_release",
    "active_release_id",
    "checksum_release",
    "list_releases",
    "new_release_dir",
    "resolve_active_index_dir",
    "verify_checksums",
]

ACTIVE_FILE = "active.json"
CHECKSUMS_FILE = "checksums.json"
RELEASES_SUBDIR = "releases"


class ReleaseCorrupt(Exception):
    """active.json or checksums.json exists but could not be parsed/verified."""


class ReleaseNotFound(Exception):
    """Activation named a release_id with no corresponding directory on disk."""


class ActiveReleaseMissing(Exception):
    """active.json names a release_id whose directory is gone.

    Deliberately distinct from falling back to the legacy layout: a
    corrupted or hand-edited pointer must fail LOUDLY, never silently serve
    a possibly-stale or wrong index while claiming success -- the same
    "trust outcomes, not exit codes" discipline the rest of this project
    already applies to generation.json and manifest.json.
    """


def _index_root(corpus: str | Path) -> Path:
    return Path(corpus).expanduser() / ".alexandria" / "index"


def _active_path(corpus: str | Path) -> Path:
    return _index_root(corpus) / ACTIVE_FILE


def _releases_root(corpus: str | Path) -> Path:
    return _index_root(corpus) / RELEASES_SUBDIR


def active_release_id(corpus: str | Path) -> str | None:
    """The currently active release id, or None if no release has ever been
    activated (a legacy-layout or brand-new corpus)."""
    path = _active_path(corpus)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ReleaseCorrupt(f"{path} exists but could not be parsed: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("release_id"), str):
        raise ReleaseCorrupt(f"{path} must contain a JSON object with a 'release_id' string")
    return data["release_id"]


def resolve_active_index_dir(corpus: str | Path) -> Path:
    """THE single function every reader and writer must route through to
    find the live index directory.

    No active.json -> the legacy flat layout, unchanged (zero-migration
    default). An active.json naming a release whose directory is gone is a
    loud ActiveReleaseMissing, never a silent fallback.
    """
    release_id = active_release_id(corpus)
    if release_id is None:
        return _index_root(corpus)
    release_dir = _releases_root(corpus) / release_id
    if not release_dir.is_dir():
        raise ActiveReleaseMissing(
            f"active.json names release {release_id!r}, but "
            f"{release_dir} does not exist -- refusing to silently fall "
            f"back to a different index while claiming success")
    return release_dir


def new_release_dir(corpus: str | Path) -> Path:
    """Allocate a NEVER-REUSED release directory path. Does not create it --
    the caller creates it (and everything inside) before any write is
    considered part of the release."""
    releases_root = _releases_root(corpus)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    release_id = f"{stamp}-{uuid.uuid4().hex[:8]}"
    candidate = releases_root / release_id
    # Timestamp collisions are astronomically unlikely (the uuid suffix
    # already guards it), but "never reused" is a stated invariant, not a
    # probabilistic one -- so it is checked, not merely hoped for.
    while candidate.exists():
        release_id = f"{stamp}-{uuid.uuid4().hex[:8]}"
        candidate = releases_root / release_id
    return candidate


def _iter_release_files(release_dir: Path):
    """Every file in a release EXCEPT checksums.json itself (self-referential
    -- it cannot include a checksum of the file recording checksums)."""
    for path in sorted(release_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(release_dir).as_posix()
        if rel == CHECKSUMS_FILE:
            continue
        yield rel, path


def checksum_release(release_dir: str | Path) -> dict[str, str]:
    """Record sha256 of every file in a release, written atomically as
    checksums.json.

    Pulled forward from P3 (Red review, 2026-08-19): unsigned today, but
    exactly the artifact a future signed-transfer protocol would sign. Cheap
    now (single-host bit-rot/partial-write detection); free to extend later.
    """
    release_dir = Path(release_dir)
    digests: dict[str, str] = {}
    for rel, path in _iter_release_files(release_dir):
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        digests[rel] = digest.hexdigest()
    out_path = release_dir / CHECKSUMS_FILE
    tmp = out_path.with_name(f"{CHECKSUMS_FILE}.tmp{os.getpid()}")
    tmp.write_text(json.dumps(digests, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, out_path)
    return digests


def verify_checksums(release_dir: str | Path) -> None:
    """Recompute every file's sha256 and compare against checksums.json.
    Raises ReleaseCorrupt naming the FIRST mismatched or missing file."""
    release_dir = Path(release_dir)
    checksums_path = release_dir / CHECKSUMS_FILE
    if not checksums_path.exists():
        raise ReleaseCorrupt(f"{release_dir} has no {CHECKSUMS_FILE}; cannot verify integrity")
    try:
        recorded = json.loads(checksums_path.read_text())
    except (OSError, ValueError) as exc:
        raise ReleaseCorrupt(f"{checksums_path} exists but could not be parsed: {exc}") from exc

    on_disk = dict(_iter_release_files(release_dir))
    for rel, expected in sorted(recorded.items()):
        path = on_disk.get(rel)
        if path is None:
            raise ReleaseCorrupt(f"{release_dir}: {rel} is recorded in checksums.json but missing on disk")
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        actual = digest.hexdigest()
        if actual != expected:
            raise ReleaseCorrupt(
                f"{release_dir}: {rel} checksum mismatch (expected {expected[:12]}, "
                f"got {actual[:12]}) -- the file changed or was corrupted after sealing")
    extra = set(on_disk) - set(recorded)
    if extra:
        raise ReleaseCorrupt(
            f"{release_dir}: file(s) present but not recorded in checksums.json: "
            f"{sorted(extra)} -- the release was modified after sealing")


def activate_release(corpus: str | Path, release_id: str) -> None:
    """Atomically publish active.json naming release_id.

    Write-temp-then-os.replace(): a crash mid-write leaves either the OLD
    active.json (old release keeps serving) or the fully-written new one,
    never a torn/partial file. This is the ONE load-bearing operation in
    this module -- everything else exists to make this call safe to make.

    Never deletes anything: the previous release stays on disk (retention is
    a separate, explicit operation), so this call also IS the rollback
    mechanism -- reactivating a previous release_id is a normal call, not a
    special case.
    """
    release_dir = _releases_root(corpus) / release_id
    if not release_dir.is_dir():
        raise ReleaseNotFound(
            f"cannot activate {release_id!r}: {release_dir} does not exist on disk")
    active_path = _active_path(corpus)
    active_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"release_id": release_id,
              "activated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    tmp = active_path.with_name(f"{ACTIVE_FILE}.tmp{os.getpid()}")
    tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, active_path)


def list_releases(corpus: str | Path) -> list[dict[str, Any]]:
    """Every release directory on disk, each tagged with whether it is the
    currently active one -- the retention/inspection surface (`--gc`,
    `--list-releases`, or equivalent, build on this)."""
    releases_root = _releases_root(corpus)
    current = active_release_id(corpus)
    if not releases_root.is_dir():
        return []
    out = []
    for path in sorted(releases_root.iterdir()):
        if not path.is_dir():
            continue
        out.append({"release_id": path.name, "active": path.name == current})
    return out
