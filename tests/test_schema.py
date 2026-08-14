"""Schema validation: shared core + source/wiki profiles.

The field law (spec 4.5): every stored field names its writer and its reader, or it
does not ship. These tests encode the consequence -- a field that belongs to the other
profile is an ERROR, not a shrug.
"""

import pytest

from alexandria.schema import (
    Profile,
    Severity,
    profile_for_path,
    validate,
)


def core(**over):
    """Minimal valid shared-core frontmatter."""
    fm = {
        "type": "observation",
        "title": "A thing that happened",
        "generated": {"by": "connector/pi-sessions", "at": "2026-07-31T10:00:00Z"},
        "source": "pi-sessions",
        "source_id": "abc123",
    }
    fm.update(over)
    return fm


def wiki(**over):
    fm = {
        "type": "entity",
        "title": "Payments service",
        "generated": {"by": "sweep/some-model", "at": "2026-07-31T10:00:00Z"},
        "sources": [{"id": "s1", "resource": "sources/pi/pi-1-a-note"}],
    }
    fm.update(over)
    return fm


def codes(issues, sev=Severity.ERROR):
    return {i.code for i in issues if i.severity is sev}


# ---------------------------------------------------------------- profile routing


def test_profile_from_path():
    assert profile_for_path("sources/pi/note.md") is Profile.SOURCE
    assert profile_for_path("wiki/systems/thing.md") is Profile.WIKI
    assert profile_for_path("sources/_unparsed/x.md") is Profile.SOURCE


def test_unknown_path_is_an_error():
    issues = validate({"type": "observation", "title": "x"}, "elsewhere/note.md")
    assert "unknown_profile" in codes(issues)


# ---------------------------------------------------------------- shared core


def test_minimal_source_note_is_valid():
    assert validate(core(), "sources/pi/n.md") == []


def test_minimal_wiki_page_is_valid():
    assert validate(wiki(), "wiki/systems/n.md") == []


@pytest.mark.parametrize("field", ["type", "title", "generated"])
def test_required_core_fields(field):
    fm = core()
    del fm[field]
    assert "missing_required" in codes(validate(fm, "sources/pi/n.md"))


def test_status_defaults_to_stable_and_rejects_junk():
    assert validate(core(status="stable"), "sources/pi/n.md") == []
    assert "bad_enum" in codes(validate(core(status="active"), "sources/pi/n.md"))


def test_type_enum_is_profile_specific():
    # 'entity' is a wiki type; illegal on a source note
    assert "bad_enum" in codes(validate(core(type="entity"), "sources/pi/n.md"))
    # 'observation' is a source type; illegal on a wiki page
    assert "bad_enum" in codes(validate(wiki(type="observation"), "wiki/x/n.md"))


def test_generated_actor_convention():
    ok = ["connector/pi-sessions", "sweep/gpt-x", "ingest/gpt-x", "human:someone",
          "agent-deliberate/pi"]
    for actor in ok:
        fm = core(generated={"by": actor, "at": "2026-07-31T10:00:00Z"})
        assert validate(fm, "sources/pi/n.md") == [], actor
    bad = core(generated={"by": "whoever", "at": "2026-07-31T10:00:00Z"})
    assert "bad_actor" in codes(validate(bad, "sources/pi/n.md"))


def test_generated_requires_both_keys():
    fm = core(generated={"by": "connector/pi-sessions"})
    assert "missing_required" in codes(validate(fm, "sources/pi/n.md"))


def test_list_fields_reject_scalars():
    assert "bad_type" in codes(validate(core(tags="one"), "sources/pi/n.md"))
    assert "bad_type" in codes(validate(core(aliases="one"), "sources/pi/n.md"))


def test_deleted_must_be_a_boolean():
    """Soft-delete tombstone (ARC-BRIEF). Strict bool, not merely truthy --
    index/chunker.py's doc_frontmatter_metadata reads this with `is True`, so
    a quoted `deleted: \"false\"` is silently treated as NOT deleted rather
    than raising. This lint check is the only thing that surfaces that kind
    of typo to the operator."""
    assert validate(core(deleted=True), "sources/pi/n.md") == []
    assert validate(core(deleted=False), "sources/pi/n.md") == []
    assert "bad_type" in codes(validate(core(deleted="true"), "sources/pi/n.md"))
    assert "bad_type" in codes(validate(wiki(deleted="false"), "wiki/x/n.md"))


def test_unknown_keys_are_tolerated():
    """OKF 4.1: consumers MUST tolerate unknown keys."""
    assert validate(core(some_future_field="x"), "sources/pi/n.md") == []


# ---------------------------------------------------------------- source profile


def test_source_requires_source_and_source_id():
    for field in ("source", "source_id"):
        fm = core()
        del fm[field]
        assert "missing_required" in codes(validate(fm, "sources/pi/n.md"))


def test_swept_shape():
    fm = core(swept={"at": "2026-07-31T10:00:00Z", "disposition": "cited"})
    assert validate(fm, "sources/pi/n.md") == []
    bad = core(swept={"at": "2026-07-31T10:00:00Z", "disposition": "maybe"})
    assert "bad_enum" in codes(validate(bad, "sources/pi/n.md"))


def test_entities_and_session_are_source_fields():
    fm = core(entities=["payments", "host-a"], session="path/to/transcript.jsonl")
    assert validate(fm, "sources/pi/n.md") == []


def test_wiki_only_fields_rejected_on_source_notes():
    """Spec 4.3: no `verified`, no `stale_after` on source notes -- a log entry is
    never 'reviewed' or 'stale', only superseded."""
    assert "profile_mismatch" in codes(
        validate(core(verified=[{"by": "human:x", "at": "2026-07-31"}]), "sources/pi/n.md")
    )
    assert "profile_mismatch" in codes(
        validate(core(stale_after="2027-01-01"), "sources/pi/n.md")
    )
    assert "profile_mismatch" in codes(
        validate(core(sources=[{"id": "a", "resource": "b"}]), "sources/pi/n.md")
    )


# ---------------------------------------------------------------- wiki profile


def test_source_only_fields_rejected_on_wiki_pages():
    assert "profile_mismatch" in codes(validate(wiki(source_id="x"), "wiki/x/n.md"))
    assert "profile_mismatch" in codes(validate(wiki(swept={"at": "x"}), "wiki/x/n.md"))


def test_sources_registry_shape():
    assert "bad_type" in codes(validate(wiki(sources="s1"), "wiki/x/n.md"))
    assert "bad_shape" in codes(validate(wiki(sources=[{"id": "s1"}]), "wiki/x/n.md"))
    assert "bad_shape" in codes(validate(wiki(sources=[{"resource": "r"}]), "wiki/x/n.md"))


def test_verified_bare_mapping_is_promoted_to_list():
    """OKF: a bare mapping is treated as a one-element list."""
    assert validate(wiki(verified={"by": "human:x", "at": "2026-07-31"}), "wiki/x/n.md") == []
    assert validate(
        wiki(verified=[{"by": "human:x", "at": "2026-07-31"}]), "wiki/x/n.md"
    ) == []


def test_stale_after_must_be_a_date():
    assert validate(wiki(stale_after="2027-01-01"), "wiki/x/n.md") == []
    assert "bad_date" in codes(validate(wiki(stale_after="soon"), "wiki/x/n.md"))


def test_wiki_page_with_no_sources_is_an_error():
    """A wiki page with zero citations cannot satisfy the hard rule."""
    fm = wiki()
    del fm["sources"]
    assert "missing_required" in codes(validate(fm, "wiki/x/n.md"))


def test_structural_wiki_files_need_no_citations():
    """index.md and log.md are the wiki's furniture -- a contents list and a run
    chronology. They assert nothing, so there is nothing to cite."""
    fm = {"type": "concept", "title": "Index",
          "generated": {"by": "human:owner", "at": "2026-07-31T00:00:00Z"},
          "sources": []}
    assert validate(fm, "wiki/index.md") == []
    assert validate(fm, "wiki/log.md") == []


def test_ordinary_wiki_pages_still_require_citations():
    """The exemption is by exact path, so the invariant stays hard everywhere else."""
    fm = {"type": "entity", "title": "Payments",
          "generated": {"by": "sweep/m", "at": "2026-07-31T00:00:00Z"},
          "sources": []}
    assert "missing_required" in codes(validate(fm, "wiki/systems/payments.md"))
    assert "missing_required" in codes(validate(fm, "wiki/index-of-things.md"))
