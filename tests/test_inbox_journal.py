"""inbox + journal connector tests (Red-approved v1: no-LLM,
curated-write connectors)."""

import hashlib

from alexandria.connectors.inbox import InboxConnector, parse_inbox_file
from alexandria.connectors.journal import JournalConnector, parse_journal
from alexandria.connectors.md_memory import SEPARATOR


def test_parse_inbox_file_roundtrip(tmp_path):
    p = tmp_path / "2026-08-08.md"
    p.write_text(
        "The gateway key lives in the keychain.\n\n"
        "<!-- created=2026-08-08, last=2026-08-08, from=pi, session=abc123 -->\n"
        f"{SEPARATOR}\n"
        "Correction: the endpoint is gateway.invalid, not localhost.\n\n"
        "<!-- created=2026-08-08, last=2026-08-08, from=codex, corrects=abc123def -->\n",
        encoding="utf-8",
    )
    entries = parse_inbox_file(p)
    assert len(entries) == 2
    assert entries[0].harness == "pi" and entries[0].session == "abc123"
    assert entries[1].corrects == "abc123def" and entries[1].harness == "codex"
    assert entries[0].entry_id == hashlib.sha256(
        "2026-08-08\nThe gateway key lives in the keychain.".encode()).hexdigest()[:12]


def test_inbox_connector_normalize_no_llm(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "2026-08-08.md").write_text(
        "A deliberate memory.\n\n<!-- created=2026-08-08, last=2026-08-08, from=other -->\n",
        encoding="utf-8",
    )
    c = InboxConnector(inbox_dir=inbox)
    items = c.discover()
    assert len(items) == 1
    docs = c.normalize(items[0])
    assert len(docs) == 1
    assert docs[0].frontmatter["source"] == "inbox"
    assert docs[0].frontmatter["harness"] == "other"
    assert "user-confirmed" in docs[0].frontmatter["tags"]
    assert docs[0].path.startswith("sources/inbox/")


def test_inbox_connector_missing_dir_is_empty(tmp_path):
    c = InboxConnector(inbox_dir=tmp_path / "nope")
    assert c.discover() == []


def test_parse_journal_sections_and_pillars(tmp_path):
    p = tmp_path / "accountability.md"
    p.write_text(
        "# Accountability journal\n"
        "## 2026-08-07\n"
        "- [finance] trail closed\n"
        "- [job] five applications committed\n"
        "## 2026-08-08\n"
        "- [brand] identity frame agreed\n",
        encoding="utf-8",
    )
    sections = parse_journal(p)
    assert len(sections) == 2
    assert sections[0].date == "2026-08-07"
    assert sections[0].pillars == ["finance", "job"]
    assert sections[1].pillars == ["brand"]


def test_parse_journal_missing_file(tmp_path):
    assert parse_journal(tmp_path / "nope.md") == []


def test_journal_connector_normalize(tmp_path):
    p = tmp_path / "accountability.md"
    p.write_text(
        "# Accountability journal\n"
        "## 2026-08-07\n"
        "- [finance] trail closed\n",
        encoding="utf-8",
    )
    c = JournalConnector(journal_path=p)
    items = c.discover()
    assert len(items) == 1
    docs = c.normalize(items[0])
    assert len(docs) == 1
    fm = docs[0].frontmatter
    assert fm["source"] == "journal"
    assert fm["generated"]["at"] == "2026-08-07"
    assert "finance" in fm["tags"] and "journal" in fm["tags"]
    assert docs[0].path.startswith("sources/journal/")


def test_journal_same_date_sections_do_not_collide(tmp_path):
    p = tmp_path / "accountability.md"
    p.write_text(
        "# Accountability journal\n"
        "## 2026-08-07\n"
        "- [finance] first\n"
        "## 2026-08-07\n"
        "- [job] second\n",
        encoding="utf-8",
    )
    c = JournalConnector(journal_path=p)
    items = c.discover()
    assert len(items) == 2
    docs = []
    for item in items:
        docs.extend(c.normalize(item))
    assert len({d.path for d in docs}) == 2
