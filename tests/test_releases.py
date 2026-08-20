"""#30 P2a: staged releases -- build a complete new index beside the live
one, validate it, then atomically swap ONE pointer file. A crash or failure
at any point before the pointer swap leaves the OLD release serving
untouched; nothing is destroyed until a new release is confirmed good.

See docs/DECISION-staged-releases-p2a.md for the scoped-down rationale
(P2a only; the in-process lease-swap refactor and cross-host signed
transfer are explicitly deferred).
"""

import json
import time
from pathlib import Path

import pytest

from alexandria.index.releases import (
    ActiveReleaseMissing,
    ReleaseCorrupt,
    ReleaseNotFound,
    activate_release,
    active_release_id,
    checksum_release,
    list_releases,
    new_release_dir,
    resolve_active_index_dir,
    verify_checksums,
)


def _corpus(tmp_path: Path) -> Path:
    (tmp_path / "sources").mkdir(parents=True, exist_ok=True)
    return tmp_path


# ---------------------------------------------------------------------------
# resolve_active_index_dir: the one function every reader/writer must route
# through so "which index is live" has a single answer.
# ---------------------------------------------------------------------------

def test_resolve_falls_back_to_the_legacy_flat_layout_when_no_release_exists(tmp_path):
    """A corpus that predates P2a (or was never migrated) has no
    releases/active.json -- it must resolve to the CURRENT flat layout
    unchanged, so every existing corpus keeps working with zero migration
    step required to just keep serving."""
    corpus = _corpus(tmp_path)
    resolved = resolve_active_index_dir(corpus)
    assert resolved == corpus / ".alexandria" / "index"


def test_resolve_returns_the_active_release_dir_once_one_is_published(tmp_path):
    corpus = _corpus(tmp_path)
    release_dir = new_release_dir(corpus)
    release_dir.mkdir(parents=True)
    (release_dir / "manifest.json").write_text("{}")
    activate_release(corpus, release_dir.name)

    resolved = resolve_active_index_dir(corpus)
    assert resolved == release_dir


def test_resolve_raises_a_named_error_on_a_corrupt_active_json(tmp_path):
    corpus = _corpus(tmp_path)
    active_path = corpus / ".alexandria" / "index" / "active.json"
    active_path.parent.mkdir(parents=True)
    active_path.write_text("{not valid json")

    with pytest.raises(ReleaseCorrupt):
        resolve_active_index_dir(corpus)


def test_resolve_raises_when_active_json_names_a_release_directory_that_is_gone(tmp_path):
    """A corrupted or hand-edited active.json pointing at a missing release
    must fail LOUDLY, never silently fall back to the legacy layout (that
    would serve a WRONG, possibly stale index while claiming success)."""
    corpus = _corpus(tmp_path)
    active_path = corpus / ".alexandria" / "index" / "active.json"
    active_path.parent.mkdir(parents=True)
    active_path.write_text(json.dumps({"release_id": "does-not-exist"}))

    with pytest.raises(ActiveReleaseMissing, match="does-not-exist"):
        resolve_active_index_dir(corpus)


# ---------------------------------------------------------------------------
# new_release_dir: allocation, uniqueness
# ---------------------------------------------------------------------------

def test_new_release_dir_never_collides_with_an_existing_one(tmp_path):
    corpus = _corpus(tmp_path)
    first = new_release_dir(corpus)
    first.mkdir(parents=True)
    second = new_release_dir(corpus)
    assert first != second


def test_new_release_dir_lives_under_releases(tmp_path):
    corpus = _corpus(tmp_path)
    release_dir = new_release_dir(corpus)
    assert release_dir.parent == corpus / ".alexandria" / "index" / "releases"


# ---------------------------------------------------------------------------
# checksum_release / verify_checksums: the unsigned manifest Red pulled
# forward from P3 -- catches bit-rot/partial-copy today, and is exactly the
# artifact a future signing step would sign.
# ---------------------------------------------------------------------------

def test_checksum_release_records_every_file_and_verify_accepts_it(tmp_path):
    corpus = _corpus(tmp_path)
    release_dir = new_release_dir(corpus)
    release_dir.mkdir(parents=True)
    (release_dir / "manifest.json").write_bytes(b"some manifest bytes")
    (release_dir / "fts.sqlite").write_bytes(b"some sqlite bytes")

    checksum_release(release_dir)
    verify_checksums(release_dir)  # must not raise


def test_verify_checksums_catches_a_tampered_file(tmp_path):
    corpus = _corpus(tmp_path)
    release_dir = new_release_dir(corpus)
    release_dir.mkdir(parents=True)
    (release_dir / "manifest.json").write_bytes(b"original bytes")
    checksum_release(release_dir)

    (release_dir / "manifest.json").write_bytes(b"TAMPERED bytes, different length")

    with pytest.raises(ReleaseCorrupt, match="manifest.json"):
        verify_checksums(release_dir)


def test_verify_checksums_catches_a_missing_file(tmp_path):
    corpus = _corpus(tmp_path)
    release_dir = new_release_dir(corpus)
    release_dir.mkdir(parents=True)
    (release_dir / "manifest.json").write_bytes(b"original bytes")
    checksum_release(release_dir)
    (release_dir / "manifest.json").unlink()

    with pytest.raises(ReleaseCorrupt, match="manifest.json"):
        verify_checksums(release_dir)


def test_checksum_release_excludes_its_own_checksums_file(tmp_path):
    """checksums.json cannot include a checksum of itself -- that is
    self-referential and would need to be written before its own hash is
    known."""
    corpus = _corpus(tmp_path)
    release_dir = new_release_dir(corpus)
    release_dir.mkdir(parents=True)
    (release_dir / "manifest.json").write_bytes(b"x")

    checksum_release(release_dir)
    recorded = json.loads((release_dir / "checksums.json").read_text())
    assert "checksums.json" not in recorded


# ---------------------------------------------------------------------------
# activate_release: the ONE atomic, load-bearing operation
# ---------------------------------------------------------------------------

def test_activate_release_is_atomic_write_temp_then_replace(tmp_path):
    corpus = _corpus(tmp_path)
    release_dir = new_release_dir(corpus)
    release_dir.mkdir(parents=True)
    (release_dir / "manifest.json").write_text("{}")

    activate_release(corpus, release_dir.name)

    active_path = corpus / ".alexandria" / "index" / "active.json"
    assert active_path.exists()
    leftovers = list(active_path.parent.glob("active.json.tmp*"))
    assert leftovers == [], f"temp file(s) left behind: {leftovers}"
    assert active_release_id(corpus) == release_dir.name


def test_activate_release_refuses_a_release_that_does_not_exist_on_disk(tmp_path):
    corpus = _corpus(tmp_path)
    with pytest.raises(ReleaseNotFound):
        activate_release(corpus, "phantom-release-id")


def test_activate_release_never_deletes_the_previous_release(tmp_path):
    """The whole point: a rollback must be possible, and a failed NEXT
    rebuild must never destroy the currently-serving release."""
    corpus = _corpus(tmp_path)
    first = new_release_dir(corpus)
    first.mkdir(parents=True)
    (first / "manifest.json").write_text("{}")
    activate_release(corpus, first.name)

    second = new_release_dir(corpus)
    second.mkdir(parents=True)
    (second / "manifest.json").write_text("{}")
    activate_release(corpus, second.name)

    assert first.exists(), "activating a new release must not delete the old one"
    assert active_release_id(corpus) == second.name


def test_rollback_by_reactivating_a_previous_release_id(tmp_path):
    corpus = _corpus(tmp_path)
    first = new_release_dir(corpus)
    first.mkdir(parents=True)
    (first / "manifest.json").write_text("{}")
    activate_release(corpus, first.name)

    second = new_release_dir(corpus)
    second.mkdir(parents=True)
    (second / "manifest.json").write_text("{}")
    activate_release(corpus, second.name)

    # rollback: repoint to the first release, no file copy
    activate_release(corpus, first.name)
    assert active_release_id(corpus) == first.name


def test_active_release_id_is_none_before_any_activation(tmp_path):
    corpus = _corpus(tmp_path)
    assert active_release_id(corpus) is None


# ---------------------------------------------------------------------------
# list_releases: retention/inspection surface
# ---------------------------------------------------------------------------

def test_list_releases_reports_every_release_dir_and_which_is_active(tmp_path):
    corpus = _corpus(tmp_path)
    first = new_release_dir(corpus)
    first.mkdir(parents=True)
    (first / "manifest.json").write_text("{}")
    second = new_release_dir(corpus)
    second.mkdir(parents=True)
    (second / "manifest.json").write_text("{}")
    activate_release(corpus, second.name)

    releases = list_releases(corpus)
    ids = {r["release_id"] for r in releases}
    assert ids == {first.name, second.name}
    active = [r for r in releases if r["active"]]
    assert len(active) == 1 and active[0]["release_id"] == second.name
