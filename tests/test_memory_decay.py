"""Decay pass: pinned entries survive, eviction is whole-entry, apply is fenced."""

import subprocess, sys
from pathlib import Path

from alexandria import decay


def test_standing_rules_classify_as_pinned():
    e = decay.Entry("Never push capital repos. Always sanitize before publishing.",
                    "2026-07-01", "2026-08-03", "raw")
    assert e.classify()[0] == "PINNED"


def test_incident_notes_classify_as_episodic():
    e = decay.Entry("This morning the guard shipped a live bug; it was fixed by noon.",
                    "2026-08-03", "2026-08-03", "raw")
    assert e.classify()[0] == "EPISODIC"


def test_ambiguous_entries_are_never_auto_evicted():
    e = decay.Entry("Some neutral note about a table schema.", "", "", "raw")
    assert e.classify()[0] == "UNCLASSIFIED"


def test_apply_refuses_without_ingestion_proof(tmp_path):
    store = tmp_path / "MEMORY.md"
    store.write_text("An entry. <!-- created=2026-01-01, last=2026-01-01 -->\n")
    r = subprocess.run([sys.executable, "-m", "alexandria.decay",
                        str(store), "--apply"], capture_output=True, text=True)
    assert r.returncode == 2
    assert "REFUSING" in r.stderr
    assert "An entry" in store.read_text()          # untouched


def test_round_trip_preserves_surviving_entries_exactly(tmp_path):
    """The observed defect in the existing mechanism is mangling survivors."""
    store = tmp_path / "MEMORY.md"
    original = ("Never do X. <!-- created=2026-01-01, last=2026-01-01 -->\n\n§\n\n"
                "Always do Y. <!-- created=2026-01-02, last=2026-01-02 -->\n")
    store.write_text(original)
    entries = decay.parse(store)
    assert len(entries) == 2
    assert decay.render(entries) == original


def test_multi_topic_blob_splits_on_numbered_clauses():
    """The cap made atomic entries impossible, so entries became multi-topic blobs --
    which is exactly what makes eviction collateral-lossy."""
    blob = ("Date/time bugs. (1) ps lstart is fixed-width, pad with two spaces. "
            "(2) Time-bucketed UI has time-shaped states, probe thresholds. "
            "(3) When a fix does not take, re-derive the root cause. "
            "(4) npm 11.8 removes bin entries written as ./bin/x.")
    parts = decay.split_clauses(blob)
    assert len(parts) == 4
    assert all(p.startswith("Date/time bugs.") for p in parts)
    assert "ps lstart" in parts[0] and "npm 11.8" in parts[3]


def test_prose_without_clause_structure_is_left_alone():
    """A bad split mangles a survivor -- worse than no split."""
    text = "A single coherent rule about publishing that has no numbered clauses."
    assert decay.split_clauses(text) == [text]
    assert decay.split_clauses("Only (1) one clause here.") == ["Only (1) one clause here."]
