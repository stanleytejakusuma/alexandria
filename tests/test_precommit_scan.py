"""The scanner is the only thing standing between a private corpus and a public repo.
It gets a real test."""

import importlib.util
from pathlib import Path

# script filename has a hyphen -- load it by path
_spec = importlib.util.spec_from_file_location(
    "precommit_scan", Path(__file__).resolve().parent.parent / "scripts" / "precommit-scan.py"
)
precommit_scan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(precommit_scan)

P = precommit_scan.PATTERNS


def hits(text):
    return precommit_scan.scan_text(text, P)


def test_catches_absolute_home_path():
    assert hits("path = /Users/someone/knowledge-graph/note.md")


def test_catches_secret_shapes():
    assert hits('api_key = "abcdefghijklmnopqrstuvwxyz123"')
    assert hits("AKIAIOSFODNN7EXAMPLE is a key")
    assert hits("-----BEGIN PRIVATE KEY-----")
    assert hits("sk-abcdefghijklmnopqrstuvwxyz")


def test_catches_wallet_shapes():
    assert hits("0x" + "a" * 40)


def test_local_patterns_extend_the_list(tmp_path, monkeypatch):
    local = tmp_path / ".leakpatterns.local"
    local.write_text("# comment\nacmecorp\n\n")
    monkeypatch.setattr(precommit_scan, "REPO", tmp_path)
    extra = precommit_scan.load_local_patterns()
    assert len(extra) == 1
    assert hits("nothing here") == []
    assert precommit_scan.scan_text("we deployed acmecorp today", P + extra)


def test_clean_text_passes():
    assert hits("Alexandria synthesizes sources into a cited wiki.") == []
    assert hits("api binds 127.0.0.1 by default") == []


def test_readme_is_clean():
    """The actual artifact we are about to publish."""
    readme = Path(__file__).resolve().parent.parent / "README.md"
    found = precommit_scan.scan_text(readme.read_text(encoding="utf-8"), P)
    assert found == [], f"README would leak: {found}"


def test_documentation_ip_ranges_are_not_flagged():
    """RFC 5737 addresses are reserved for examples and cannot route anywhere.
    Flagging them only trains people to ignore the scanner."""
    assert hits("connect to 192.0.2.99") == []
    assert hits("see 198.51.100.7 and 203.0.113.4") == []
    assert hits("api binds 127.0.0.1") == []


def test_real_private_addresses_are_still_flagged():
    assert hits("the box is at 192.168.44.99")
    assert hits("tailnet host 100.104.107.107")


# --- zero-width evasion -----------------------------------------------------
# Three files reached the public repo carrying `H\u200dermes` -- a private agent
# name split by a zero-width joiner. It reads as the name to any human on
# GitHub, and defeated every regex here by not being the string they match.
# AGENTS.md warned about exactly this; the scanner did not implement it.

ZWJ = "\u200d"


def test_a_zero_width_character_is_reported_in_its_own_right():
    """There is no legitimate use for one in this codebase, so its mere
    presence is the finding -- independent of whether it hides a known
    pattern. A novel name nobody has written a regex for is still caught."""
    found = hits(f"the H{ZWJ}ostname of the box")
    assert any("zero-width" in name for _, name, _ in found)


def test_a_pattern_hidden_by_a_zero_width_character_is_still_matched():
    """The evasion has to actually fail. Reporting the character while letting
    the name through would be theatre: the commit still leaks."""
    patterns = [("test name", "Hermes")]
    assert not precommit_scan.scan_text(f"deploy to H{ZWJ}ermes", [("test name", "Hermes")]) == []
    names = [n for _, n, _ in precommit_scan.scan_text(f"deploy to H{ZWJ}ermes", patterns)]
    assert "test name" in names, "the hidden name must still be caught, not just the character"


def test_clean_text_is_still_clean():
    """The stripped-form matching must not invent hits on ordinary lines."""
    assert precommit_scan.scan_text("a perfectly ordinary line", [("test name", "Hermes")]) == []
