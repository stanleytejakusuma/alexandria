"""kg-sync vault migration: field mapping, dedup, quarantine, count reconciliation.

The gate this must satisfy: in == written + quarantined + collapsed. Every input file
is accounted for exactly once, and the numbers are logged, not asserted by vibes.
"""

from alexandria.migrate import migrate_kg_sync
from alexandria.schema import validate

LEGACY = """---
source: notes-app
source_id: '10'
type: observation
project: ACME
kind: insight
status: active
date: '2026-07-04T15:54:00'
updated: '2026-07-04T15:54:00'
hash: abc
source_hash: def
supersedes: []
superseded_by: []
aliases: []
tags:
  - payments
---

# A thing

The retry guard failed open under load. See [[Payments Service]] and [[Retry Guard]].

## Related (semantic)

- [[Something Else]] (0.81)

## Annotations

Hand-written note that must survive.
"""


def write(vault, name, text):
    (vault / name).write_text(text, encoding="utf-8")


def test_field_mapping(tmp_path):
    vault, corpus = tmp_path / "v", tmp_path / "c"
    vault.mkdir()
    write(vault, "cip-10-a-thing.md", LEGACY)

    rep = migrate_kg_sync(vault, corpus, dry_run=False)
    assert rep.written == 1

    out = next((corpus / "sources").rglob("*.md"))
    from alexandria.corpus import Doc
    doc = Doc.read(out, root=corpus)
    fm = doc.frontmatter

    assert doc.path.startswith("sources/notes-app/")
    assert fm["type"] == "observation"
    assert fm["source"] == "notes-app"
    assert fm["source_id"] == "10"
    assert fm["status"] == "stable"                      # active -> stable
    assert fm["generated"]["by"] == "connector/notes-app"
    assert fm["generated"]["at"].startswith("2026-07-04")
    assert "insight" in fm["tags"]                       # legacy kind preserved as tag
    assert "payments" in fm["tags"]
    assert fm["project"] == "ACME"
    # entities backfilled from wikilinks + project, no private lexicon needed
    assert "Payments Service" in fm["entities"]
    assert "ACME" in fm["entities"]
    # legacy fields with no reader are dropped (the field law)
    for dead in ("kind", "date", "updated", "prev", "next"):
        assert dead not in fm
    # empty legacy lifecycle lists are dropped, not carried as noise
    assert "supersedes" not in fm

    assert validate(fm, doc.path) == []


def test_related_semantic_dropped_annotations_preserved(tmp_path):
    vault, corpus = tmp_path / "v", tmp_path / "c"
    vault.mkdir()
    write(vault, "cip-10-a.md", LEGACY)
    migrate_kg_sync(vault, corpus, dry_run=False)

    body = next((corpus / "sources").rglob("*.md")).read_text()
    assert "Related (semantic)" not in body
    assert "Something Else" not in body
    assert "## Annotations" in body
    assert "Hand-written note that must survive." in body
    assert "The retry guard failed open" in body


def test_status_and_type_maps(tmp_path):
    vault, corpus = tmp_path / "v", tmp_path / "c"
    vault.mkdir()
    write(vault, "a.md", LEGACY.replace("status: active", "status: closed")
          .replace("type: observation", "type: task").replace("source_id: '10'", "source_id: '11'"))
    write(vault, "b.md", LEGACY.replace("status: active", "status: orphaned")
          .replace("source_id: '10'", "source_id: '12'"))
    write(vault, "c.md", LEGACY.replace("type: observation", "type: memory-file")
          .replace("source_id: '10'", "source_id: '13'"))
    migrate_kg_sync(vault, corpus, dry_run=False)

    from alexandria.corpus import Doc
    got = {}
    for p in (corpus / "sources").rglob("*.md"):
        d = Doc.read(p, root=corpus)
        got[d.frontmatter["source_id"]] = d.frontmatter
    assert got["11"]["status"] == "deprecated"
    assert got["11"]["type"] == "task"
    assert got["12"]["status"] == "draft"
    assert got["13"]["type"] == "memory"        # memory-file folds to memory


def test_quarantines_files_without_frontmatter(tmp_path):
    vault, corpus = tmp_path / "v", tmp_path / "c"
    vault.mkdir()
    write(vault, "good.md", LEGACY)
    write(vault, "bad.md", "no frontmatter at all\n")

    rep = migrate_kg_sync(vault, corpus, dry_run=False)
    assert rep.quarantined == 1
    assert rep.written == 1
    assert (corpus / "sources" / "_unparsed" / "bad.md").exists()


def test_identical_duplicates_collapse_distinct_ones_kept(tmp_path):
    vault, corpus = tmp_path / "v", tmp_path / "c"
    vault.mkdir()
    write(vault, "dup1.md", LEGACY)
    write(vault, "dup2.md", LEGACY)                                  # identical body+id
    write(vault, "dup3.md", LEGACY.replace("failed open", "failed closed"))  # same id, different body

    rep = migrate_kg_sync(vault, corpus, dry_run=False)
    assert rep.collapsed == 1          # dup2 collapsed into dup1
    assert rep.written == 2            # dup1 + dup3 (kept, flagged)
    assert rep.dupes_kept == 1
    assert rep.total_in == rep.written + rep.quarantined + rep.collapsed


def test_counts_always_reconcile(tmp_path):
    vault, corpus = tmp_path / "v", tmp_path / "c"
    vault.mkdir()
    for i in range(5):
        write(vault, f"n{i}.md", LEGACY.replace("source_id: '10'", f"source_id: '{i}'"))
    write(vault, "bad.md", "nope\n")
    write(vault, "dupe.md", LEGACY.replace("source_id: '10'", "source_id: '0'"))

    rep = migrate_kg_sync(vault, corpus, dry_run=False)
    assert rep.total_in == 7
    assert rep.total_in == rep.written + rep.quarantined + rep.collapsed


def test_unquoted_yaml_dates_in_list_fields_are_coerced(tmp_path):
    """Real vault quirk: `aliases:\n  - 2021-06-17` parses as datetime.date, not str.
    261 daily notes hit this. The validator is right to reject it; migration normalizes."""
    vault, corpus = tmp_path / "v", tmp_path / "c"
    vault.mkdir()
    write(vault, "daily.md", LEGACY.replace(
        "aliases: []", "aliases:\n  - 2021-06-17").replace(
        "type: observation", "type: daily"))

    rep = migrate_kg_sync(vault, corpus, dry_run=False)
    assert rep.schema_errors == [], rep.schema_errors

    from alexandria.corpus import Doc
    fm = Doc.read(next((corpus / "sources").rglob("*.md")), root=corpus).frontmatter
    assert fm["aliases"] == ["2021-06-17"]
    assert all(isinstance(a, str) for a in fm["aliases"])


def test_dry_run_writes_nothing(tmp_path):
    vault, corpus = tmp_path / "v", tmp_path / "c"
    vault.mkdir()
    write(vault, "a.md", LEGACY)

    rep = migrate_kg_sync(vault, corpus, dry_run=True)
    assert rep.written == 1            # counted
    assert not corpus.exists()         # but nothing on disk


def test_every_output_validates(tmp_path):
    vault, corpus = tmp_path / "v", tmp_path / "c"
    vault.mkdir()
    for i, (typ, status) in enumerate(
        [("observation", "active"), ("memory", "closed"), ("task", "active"),
         ("daily", "orphaned"), ("project", "active")]
    ):
        write(vault, f"n{i}.md",
              LEGACY.replace("type: observation", f"type: {typ}")
                    .replace("status: active", f"status: {status}")
                    .replace("source_id: '10'", f"source_id: '{i}'"))
    rep = migrate_kg_sync(vault, corpus, dry_run=False)
    assert rep.schema_errors == [], rep.schema_errors
    assert rep.written == 5


# ---- regression: Codex adversarial review findings ----

FENCED = """---
source: notes-app
source_id: 'f1'
type: observation
title: Note about the schema
status: active
date: '2026-07-04T15:54:00'
---

Real content before.

```markdown
## Related (semantic)
- [[an example]] (0.81)
```

Real content after the fence.

## A Later Section

Must survive.
"""


def test_fenced_literal_heading_is_not_treated_as_a_section(tmp_path):
    """A note *about* the vault schema quotes the heading inside a code fence.
    A fence-unaware matcher deletes from there to EOF -- catastrophic silent loss."""
    vault, corpus = tmp_path / "v", tmp_path / "c"
    vault.mkdir()
    write(vault, "fenced.md", FENCED)
    migrate_kg_sync(vault, corpus, dry_run=False)

    body = next((corpus / "sources").rglob("*.md")).read_text()
    assert "Real content after the fence." in body
    assert "## A Later Section" in body
    assert "Must survive." in body
    assert "## Related (semantic)" in body      # inside the fence: preserved


def test_h3_related_heading_is_not_dropped(tmp_path):
    vault, corpus = tmp_path / "v", tmp_path / "c"
    vault.mkdir()
    write(vault, "h3.md", LEGACY.replace("## Related (semantic)", "### Related (semantic)"))
    migrate_kg_sync(vault, corpus, dry_run=False)
    body = next((corpus / "sources").rglob("*.md")).read_text()
    assert "### Related (semantic)" in body     # only H2 is machine-generated


def test_section_at_end_of_body_and_annotations_after(tmp_path):
    vault, corpus = tmp_path / "v", tmp_path / "c"
    vault.mkdir()
    write(vault, "a.md", LEGACY)
    migrate_kg_sync(vault, corpus, dry_run=False)
    body = next((corpus / "sources").rglob("*.md")).read_text()
    assert "Something Else" not in body         # the H2 section did get dropped
    assert "Hand-written note that must survive." in body


def test_invalid_utf8_is_quarantined_not_corrupted(tmp_path):
    vault, corpus = tmp_path / "v", tmp_path / "c"
    vault.mkdir()
    (vault / "bad.md").write_bytes(b"---\ntype: observation\n---\n\xff\xfe not utf8\n")
    write(vault, "good.md", LEGACY)

    rep = migrate_kg_sync(vault, corpus, dry_run=False)
    assert rep.quarantined == 1
    assert "invalid utf-8" in rep.quarantine_reasons
    # quarantined byte-for-byte, never replaced with U+FFFD or emptied
    assert (corpus / "sources" / "_unparsed" / "bad.md").read_bytes().endswith(b"\xff\xfe not utf8\n")
    assert rep.total_in == rep.written + rep.quarantined + rep.collapsed


def test_output_path_collisions_are_detected_and_disambiguated(tmp_path):
    """Distinct source_ids that normalize to one filename must not overwrite."""
    vault, corpus = tmp_path / "v", tmp_path / "c"
    vault.mkdir()
    write(vault, "a.md", LEGACY.replace("source_id: '10'", "source_id: 'a/b'"))
    write(vault, "b.md", LEGACY.replace("source_id: '10'", "source_id: 'a:b'")
                               .replace("failed open", "failed closed"))

    rep = migrate_kg_sync(vault, corpus, dry_run=False)
    assert rep.written == 2
    on_disk = list((corpus / "sources").rglob("*.md"))
    assert len(on_disk) == 2, "second document silently overwrote the first"
    assert len(rep.collisions) == 1


def test_unknown_legacy_keys_are_counted_not_silently_dropped(tmp_path):
    vault, corpus = tmp_path / "v", tmp_path / "c"
    vault.mkdir()
    write(vault, "a.md", LEGACY.replace("kind: insight", "kind: insight\nmystery_field: value"))
    rep = migrate_kg_sync(vault, corpus, dry_run=False)
    assert rep.dropped_keys["mystery_field"] == 1


def test_body_trailing_whitespace_is_preserved(tmp_path):
    vault, corpus = tmp_path / "v", tmp_path / "c"
    vault.mkdir()
    write(vault, "a.md", "---\nsource: notes-app\nsource_id: 'w1'\ntype: observation\n"
                         "title: T\nstatus: active\ndate: '2026-07-04'\n---\nbody line\n\n\n")
    migrate_kg_sync(vault, corpus, dry_run=False)
    from alexandria.corpus import Doc
    d = Doc.read(next((corpus / "sources").rglob("*.md")), root=corpus)
    assert d.body == "body line\n\n\n"


# ---- regression: Red round 2 -- derived rollups are not ground truth ----

ROLLUP = """---
source: derived
source_id: daily-2026-07-29
type: daily
project: none
kind: session
status: active
date: '2026-07-29T00:00:00'
tags: []
---

# 2026-07-29

Daily rollup. Lists all atomic nodes from this date. Auto-regenerated.

- [[some-note]] - insight - none
"""


def test_derived_rollups_are_excluded_not_migrated(tmp_path):
    """Rollups are regenerated wholesale upstream and are themselves N:1 syntheses.
    In an immutable layer with an exact body hash they would mint a superseding note
    on every regeneration -- unbounded churn, zero new information."""
    vault, corpus = tmp_path / "v", tmp_path / "c"
    vault.mkdir()
    write(vault, "daily-2026-07-29.md", ROLLUP)
    write(vault, "atomic.md", LEGACY)

    rep = migrate_kg_sync(vault, corpus, dry_run=False)
    assert rep.excluded_derived == 1
    assert rep.written == 1                      # only the atomic note
    assert rep.reconciles
    assert not (corpus / "sources" / "derived").exists()


def test_reconciliation_includes_exclusions(tmp_path):
    vault, corpus = tmp_path / "v", tmp_path / "c"
    vault.mkdir()
    for i in range(3):
        write(vault, f"r{i}.md", ROLLUP.replace("daily-2026-07-29", f"daily-r{i}"))
    write(vault, "a.md", LEGACY)
    write(vault, "bad.md", "no frontmatter\n")

    rep = migrate_kg_sync(vault, corpus, dry_run=False)
    assert rep.total_in == 5
    assert (rep.written, rep.quarantined, rep.excluded_derived) == (1, 1, 3)
    assert rep.total_in == rep.accounted


def test_unbalanced_fences_do_not_disable_the_drop(tmp_path):
    """Notebook-derived notes contain malformed openers (``output closed by ```),
    leaving an odd fence count. Naive fence tracking then sticks 'inside a fence'
    forever and silently skips every drop -- 26 real notes hit this."""
    vault, corpus = tmp_path / "v", tmp_path / "c"
    vault.mkdir()
    write(vault, "nb.md", LEGACY.replace(
        "The retry guard failed open under load.",
        "``output\nBoth conditions are True\n```\n\nprose after"))
    migrate_kg_sync(vault, corpus, dry_run=False)
    body = next((corpus / "sources").rglob("*.md")).read_text()
    assert "## Related (semantic)" not in body
    assert "Both conditions are True" in body     # fenced content still preserved
    assert "Hand-written note that must survive." in body
