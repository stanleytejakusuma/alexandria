"""knowledge-graph connector.

The mappings asserted here were derived from all 20,861 vault/corpus pairs that
share a path, not chosen -- see the connector docstring. The two stripping tests
cover real defects found while building it, both of which would have silently
corrupted ~20,000 existing documents.
"""

from alexandria.connectors.knowledge_graph import KnowledgeGraphConnector

NOTE = """---
source: alpha-notes
source_id: '6417'
type: observation
project: TOOL
kind: insight
status: active
date: 2026-07-01
hash: 4833936d
tags:
  - source/alpha-notes
  - type/observation
---

# All 14 Skills Verified

Body text.

- [[proj-tool]]
- [[daily-2026-07-01]]

## Related (semantic)
<!-- Populated by semantic projection sweep. -->
## Annotations
<!-- Hand-edit freely. -->
"""

POPULATED = NOTE.replace(
    "## Related (semantic)\n<!-- Populated by semantic projection sweep. -->\n",
    "## Related (semantic)\n- [[mep-aaaa-session-1]] (0.97)\n- [[mep-bbbb-session-2]] (0.96)\n",
)


def _doc(tmp_path, text, name="an-6417-all-14-skills-verified.md"):
    # Each note gets its own vault dir: discover() returns every note in the
    # tree sorted, so a second note written beside the first would leave
    # items[0] pointing at the wrong one.
    vault = tmp_path / name.removesuffix(".md")
    vault.mkdir()
    (vault / name).write_text(text, encoding="utf-8")
    conn = KnowledgeGraphConnector(vault)
    items = conn.discover()
    assert items, f"discover() found nothing; errors={conn.errors}"
    return conn.normalize(items[0])[0]


def test_note_lands_under_its_original_source_not_the_connector(tmp_path):
    """A alpha-notes note must land in sources/alpha-notes/ under the filename the
    original importer produced, or this connector duplicates 20,862 documents
    instead of regenerating them."""
    doc = _doc(tmp_path, NOTE)
    assert doc.path == "sources/alpha-notes/alpha-notes-6417-all-14-skills-verified.md"
    assert doc.frontmatter["source"] == "alpha-notes"
    assert doc.frontmatter["source_id"] == "6417"
    assert doc.frontmatter["title"] == "All 14 Skills Verified"
    # Provenance names the connector that actually produced it.
    assert doc.frontmatter["generated"]["by"] == f"connector/{KnowledgeGraphConnector.name}"
    assert doc.frontmatter["generated"]["at"] == "2026-07-01"


def test_type_and_status_use_the_measured_mappings(tmp_path):
    doc = _doc(tmp_path, NOTE)
    assert doc.frontmatter["status"] == "stable"          # active -> stable
    assert doc.frontmatter["type"] == "observation"       # identity
    doc2 = _doc(tmp_path, NOTE.replace("status: active", "status: closed")
                              .replace("type: observation", "type: memory-file"),
                name="an-6418-x.md")
    assert doc2.frontmatter["status"] == "deprecated"     # closed -> deprecated
    assert doc2.frontmatter["type"] == "memory"           # memory-file -> memory


def test_kind_becomes_a_tag_and_entities_are_project_plus_wikilinks(tmp_path):
    doc = _doc(tmp_path, NOTE)
    assert "insight" in doc.frontmatter["tags"]           # `kind` appended
    assert doc.frontmatter["entities"] == ["TOOL", "proj-tool", "daily-2026-07-01"]


def test_a_populated_related_section_leaks_into_neither_body_nor_entities(tmp_path):
    """`## Related (semantic)` is usually an empty placeholder, but for some
    cipher notes it holds machine-inferred similarity links. Stripping only the
    heading left them in the body AND in entities -- 102 of 600 sampled documents
    were wrong until entities were read off the CLEANED body rather than the raw
    note."""
    doc = _doc(tmp_path, POPULATED)
    assert "mep-aaaa" not in doc.body, "similarity links survived in the body"
    assert not any("mep-" in e for e in doc.frontmatter["entities"]), \
        f"similarity links leaked into entities: {doc.frontmatter['entities']}"
    # The section itself is gone, but hand-edited Annotations must survive.
    assert "## Related (semantic)" not in doc.body
    assert "## Annotations" in doc.body


def test_vault_scaffolding_and_sync_conflicts_are_skipped(tmp_path):
    for name in ("index.md", "log.md", "README.md",
                 "an-1-x.sync-conflict-20260727-061926-RMLHTGP.md"):
        (tmp_path / name).write_text(NOTE, encoding="utf-8")
    (tmp_path / "an-6417-real.md").write_text(NOTE, encoding="utf-8")
    assert len(KnowledgeGraphConnector(tmp_path).discover()) == 1


def test_a_note_without_source_or_h1_is_reported_not_silently_dropped(tmp_path):
    (tmp_path / "no-source.md").write_text(NOTE.replace("source: alpha-notes\n", ""),
                                           encoding="utf-8")
    (tmp_path / "no-h1.md").write_text(NOTE.replace("# All 14 Skills Verified", "text"),
                                       encoding="utf-8")
    conn = KnowledgeGraphConnector(tmp_path)
    assert conn.discover() == []
    assert len(conn.errors) == 2, "unusable notes must surface, not vanish"


def test_errors_do_not_leak_between_instances(tmp_path):
    """base.NoStateMixin declares `errors` as a shared class attribute."""
    (tmp_path / "bad.md").write_text("no frontmatter", encoding="utf-8")
    first = KnowledgeGraphConnector(tmp_path)
    first.discover()
    assert first.errors
    assert KnowledgeGraphConnector(tmp_path).errors == []
