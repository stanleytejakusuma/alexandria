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
