"""markdown-memory connector: deliberate memory stores -> source notes, no LLM."""

from pathlib import Path

from alexandria.connectors.md_memory import MarkdownMemoryConnector, Entry, parse_store
from alexandria.schema import validate

STORE = """First rule about publishing. Never push capital repos. <!-- created=2026-07-29, last=2026-08-03 -->

§

Second rule. Grade the render against the claim. <!-- created=2026-07-31, last=2026-08-03 -->
"""


def test_parse_store_splits_on_separator(tmp_path):
    p = tmp_path / "MEMORY.md"
    p.write_text(STORE)
    entries = parse_store(p)
    assert len(entries) == 2
    assert entries[0].created == "2026-07-29"
    assert "<!--" not in entries[0].text          # metadata stripped from the body
    assert entries[1].title.startswith("Second rule")


def test_store_without_separator_is_one_entry(tmp_path):
    p = tmp_path / "USER.md"
    p.write_text("Only one entry here. <!-- created=2026-07-29, last=2026-08-03 -->\n")
    assert len(parse_store(p)) == 1


def test_entry_id_is_content_derived():
    a = Entry("same text", "2026-01-01", "2026-01-02", "MEMORY.md")
    b = Entry("same text", "2026-09-09", "2026-09-09", "MEMORY.md")
    c = Entry("different", "2026-01-01", "2026-01-02", "MEMORY.md")
    assert a.entry_id == b.entry_id      # dates change, identity does not
    assert a.entry_id != c.entry_id      # edited text is a NEW note, not a mutation


def test_notes_carry_the_deliberate_trust_tier(tmp_path):
    (tmp_path / "MEMORY.md").write_text(STORE)
    c = MarkdownMemoryConnector(memory_dir=tmp_path)
    docs = [d for i in c.discover() for d in c.normalize(i)]
    assert len(docs) == 2
    for d in docs:
        assert d.frontmatter["generated"]["by"] == "agent-deliberate/harness"
        assert d.frontmatter["source"] == "markdown-memory"
        assert validate(d.frontmatter, d.path) == []


def test_no_llm_is_used(tmp_path):
    """The material is already a deliberate structured statement. Distilling it would
    add a fabrication surface and lose the human's exact wording."""
    (tmp_path / "MEMORY.md").write_text(STORE)
    c = MarkdownMemoryConnector(memory_dir=tmp_path)      # no llm argument exists
    docs = [d for i in c.discover() for d in c.normalize(i)]
    assert "Never push capital repos" in docs[0].body


def test_project_stores_are_tagged_and_scoped(tmp_path):
    mem = tmp_path / "mem"; mem.mkdir()
    proj = tmp_path / "projects" / "codebase"; proj.mkdir(parents=True)
    (proj / "MEMORY.md").write_text("Project rule here. <!-- created=2026-08-01, last=2026-08-03 -->\n")
    c = MarkdownMemoryConnector(memory_dir=mem, projects_dir=tmp_path / "projects")
    docs = [d for i in c.discover() for d in c.normalize(i)]
    assert len(docs) == 1
    assert docs[0].frontmatter["project"] == "codebase"
    assert "project-memory" in docs[0].frontmatter["tags"]


def test_missing_created_still_validates(tmp_path):
    (tmp_path / "MEMORY.md").write_text("An entry with no metadata comment at all.\n")
    c = MarkdownMemoryConnector(memory_dir=tmp_path)
    docs = [d for i in c.discover() for d in c.normalize(i)]
    assert validate(docs[0].frontmatter, docs[0].path) == []
