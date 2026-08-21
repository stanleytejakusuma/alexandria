"""§6 / gate B1: a restore from backup reproduces query history, audit log,
and liveness state -- exercised, not asserted."""

from __future__ import annotations

import sqlite3

from alexandria.backup import backup_state, restore_state
from alexandria.cache import read_index_generation
from alexandria.cli import app
from alexandria.liveness import check as liveness_check


def _build_a_corpus_with_real_state(tmp_path, monkeypatch):
    """Produce genuine state through the real CLI, not hand-crafted fixtures:
    an indexed doc, a search (populates queries.sqlite + audit log via
    AuditLogger), a remembered-and-promoted fact (touches pending/ + liveness),
    and an eval run (populates eval_runs.jsonl)."""
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    doc = corpus / "sources" / "note.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("---\nsource: test\n---\n\nBackup gate fixture document about ledgers.\n")
    assert app(["--corpus", str(corpus), "index"]) == 0
    assert app(["--corpus", str(corpus), "search", "ledgers"]) == 0
    assert app(["--corpus", str(corpus), "remember", "A fact for the backup gate."]) == 0
    assert app(["--corpus", str(corpus), "promote"]) == 0
    return corpus


def test_b1_backup_then_restore_reproduces_query_history_audit_log_and_liveness(tmp_path, monkeypatch):
    corpus = _build_a_corpus_with_real_state(tmp_path, monkeypatch)

    queries_before = (corpus / ".alexandria" / "queries.sqlite").read_bytes()
    audit_dir = corpus / ".alexandria" / "audit"
    audit_files_before = {p.name: p.read_bytes() for p in audit_dir.glob("*.jsonl")}
    liveness_before = (corpus / ".alexandria" / "liveness.json").read_text()

    archive = tmp_path / "state-backup.tar.gz"
    backup_result = backup_state(corpus, archive)
    assert ".alexandria/queries.sqlite" in backup_result.included
    assert ".alexandria/liveness.json" in backup_result.included
    assert archive.exists()

    # Simulate total loss of state (but NOT the index -- that's the whole point).
    (corpus / ".alexandria" / "queries.sqlite").unlink()
    for p in audit_dir.glob("*.jsonl"):
        p.unlink()
    (corpus / ".alexandria" / "liveness.json").unlink()
    assert not (corpus / ".alexandria" / "queries.sqlite").exists()

    restore_result = restore_state(corpus, archive)
    assert ".alexandria/queries.sqlite" in restore_result.restored
    assert not restore_result.dry_run

    queries_after = (corpus / ".alexandria" / "queries.sqlite").read_bytes()
    assert queries_after == queries_before
    audit_files_after = {p.name: p.read_bytes() for p in audit_dir.glob("*.jsonl")}
    assert audit_files_after == audit_files_before
    liveness_after = (corpus / ".alexandria" / "liveness.json").read_text()
    assert liveness_after == liveness_before

    # Not just byte-identical -- actually USABLE. A fresh sqlite connection
    # can query the restored queries.sqlite for real rows, and the restored
    # liveness state makes a real liveness.check() call pass.
    conn = sqlite3.connect(str(corpus / ".alexandria" / "queries.sqlite"))
    n = conn.execute("SELECT COUNT(*) FROM queries").fetchone()[0]
    conn.close()
    assert n >= 1
    result = liveness_check(corpus)
    assert result.state_file_present is True

    # The index itself was never touched, and never should be -- it's excluded
    # from the archive entirely, and search still works using the pre-existing
    # (never-deleted) chunks.lance.
    rc = app(["--corpus", str(corpus), "search", "ledgers"])
    assert rc == 0


def test_b1_the_index_is_never_included_in_the_archive(tmp_path, monkeypatch):
    corpus = _build_a_corpus_with_real_state(tmp_path, monkeypatch)
    archive = tmp_path / "state-backup.tar.gz"
    result = backup_state(corpus, archive)

    import tarfile
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
    assert not any("chunks.lance" in n for n in names)
    assert not any("fts.sqlite" in n for n in names)
    assert result.archive_path == archive


def test_b1_a_missing_optional_state_path_is_reported_not_treated_as_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    doc = corpus / "sources" / "note.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("---\nsource: test\n---\n\nMinimal fixture, no search/remember ever run.\n")
    assert app(["--corpus", str(corpus), "index"]) == 0

    archive = tmp_path / "backup.tar.gz"
    result = backup_state(corpus, archive)
    # queries.sqlite/audit/pending never got created (no search/remember ever
    # ran) -- missing, and that's not a failure. liveness.json DOES exist:
    # `index` itself is a successful liveness cycle and records one.
    assert set(result.missing) >= {".alexandria/queries.sqlite", ".alexandria/pending"}
    assert ".alexandria/liveness.json" in result.included
    assert archive.exists()


def test_b1_restore_refuses_to_write_outside_the_state_allowlist(tmp_path, monkeypatch):
    """A hand-crafted or corrupted archive that smuggles an extra member
    (e.g. targeting chunks.lance, or an arbitrary path) must not be trusted."""
    import tarfile

    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    doc = corpus / "sources" / "note.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("---\nsource: test\n---\n\nAllowlist fixture.\n")
    assert app(["--corpus", str(corpus), "index"]) == 0

    malicious_dir = tmp_path / "malicious"
    malicious_dir.mkdir()
    smuggled = malicious_dir / "chunks.lance"
    smuggled.write_text("this should never land in the corpus")

    archive = tmp_path / "malicious.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(smuggled, arcname=".alexandria/index/chunks.lance/injected.txt")
        tar.add(smuggled, arcname="../../etc/passwd")

    result = restore_state(corpus, archive)
    assert result.restored == []
    assert not (corpus / ".alexandria" / "index" / "chunks.lance" / "injected.txt").exists()


def test_b1_dry_run_lists_without_writing(tmp_path, monkeypatch):
    corpus = _build_a_corpus_with_real_state(tmp_path, monkeypatch)
    archive = tmp_path / "backup.tar.gz"
    backup_state(corpus, archive)

    queries_path = corpus / ".alexandria" / "queries.sqlite"
    before = queries_path.stat().st_mtime
    result = restore_state(corpus, archive, dry_run=True)
    after = queries_path.stat().st_mtime

    assert result.dry_run is True
    assert len(result.restored) > 0
    assert before == after  # nothing was actually written


def test_b1_via_the_real_cli_subcommands(tmp_path, monkeypatch, capsys):
    corpus = _build_a_corpus_with_real_state(tmp_path, monkeypatch)
    archive = tmp_path / "cli-backup.tar.gz"

    rc = app(["--corpus", str(corpus), "backup", str(archive)])
    assert rc == 0
    assert archive.exists()
    out = capsys.readouterr().out
    assert "queries.sqlite" in out

    (corpus / ".alexandria" / "queries.sqlite").unlink()
    rc = app(["--corpus", str(corpus), "restore", str(archive)])
    assert rc == 0
    assert (corpus / ".alexandria" / "queries.sqlite").exists()
    out = capsys.readouterr().out
    assert "restored" in out.lower()


def test_b1_traversal_through_an_allowlisted_prefix_is_rejected_by_the_allowlist_itself(tmp_path):
    """The case the original allowlist test missed.

    `../../etc/passwd` and `.alexandria/index/chunks.lance/x` are both rejected
    trivially by a prefix check. The interesting input is traversal THROUGH a
    permitted prefix -- `.alexandria/pending/../../../../tmp/pwned` starts with
    an allowlisted prefix and resolves outside the corpus entirely. That passed
    the raw-name check, leaving tarfile's `filter="data"` as the only thing
    standing between a hostile archive and an arbitrary write. Defence in depth
    means the allowlist has to hold on its own.
    """
    import tarfile
    from pathlib import Path

    corpus = tmp_path / "corpus"
    (corpus / ".alexandria").mkdir(parents=True)
    payload = tmp_path / "payload.txt"
    payload.write_text("pwned")

    archive = tmp_path / "hostile.tar.gz"
    hostile_names = [
        ".alexandria/pending/../../../../tmp/pwned-traversal",
        ".alexandria/audit/../../../escaped.txt",
        ".alexandria/./queries.sqlite/../../../../also-escaped",
        "/etc/absolute-path",
        ".alexandria/../../relative-escape",
    ]
    with tarfile.open(archive, "w:gz") as tar:
        for name in hostile_names:
            tar.add(payload, arcname=name)

    result = restore_state(corpus, archive)

    assert result.restored == [], (
        f"the allowlist admitted a traversal member on its own: {result.restored}")
    for probe in (Path("/tmp/pwned-traversal"), tmp_path / "escaped.txt",
                  tmp_path / "also-escaped", tmp_path / "relative-escape"):
        assert not probe.exists(), f"a hostile member escaped the corpus: {probe}"


def test_b1_a_legitimate_nested_state_path_still_restores(tmp_path):
    """The rejection above must not be so broad it refuses real nested members
    -- `.alexandria/audit/answers.jsonl` and `.alexandria/index/generation.json`
    are both legitimately nested under allowlisted prefixes."""
    import tarfile

    corpus = tmp_path / "corpus"
    (corpus / ".alexandria").mkdir(parents=True)
    payload = tmp_path / "p.txt"
    payload.write_text("{}")

    archive = tmp_path / "ok.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(payload, arcname=".alexandria/audit/answers.jsonl")
        tar.add(payload, arcname=".alexandria/index/generation.json")
        tar.add(payload, arcname=".alexandria/liveness.json")

    result = restore_state(corpus, archive)

    assert sorted(result.restored) == [
        ".alexandria/audit/answers.jsonl",
        ".alexandria/index/generation.json",
        ".alexandria/liveness.json",
    ]
    assert (corpus / ".alexandria" / "audit" / "answers.jsonl").exists()


def test_b1_restore_never_regresses_the_generation_counter(tmp_path, monkeypatch):
    """#6 erasure-core item 4, Red review 2026-08-21 (finding #1): the
    generation counter is cache-key-freshness ONLY (verified: used nowhere
    but cache.py/search.py's cache-key path) -- a real restore must not be
    ABLE to regress it, not merely be warned that it did. This is the
    load-bearing assertion: the generation on disk after a real (non-dry-
    run) restore must be the corpus's own, unregressed value."""
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    doc = corpus / "sources" / "note.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("---\nsource: test\n---\n\nRegression fixture.\n")
    assert app(["--corpus", str(corpus), "index"]) == 0  # generation 1

    old_backup = tmp_path / "old.tar.gz"
    result = backup_state(corpus, old_backup)
    assert ".alexandria/index/generation.json" in result.included

    # advance the corpus's generation further (simulates real activity --
    # a delete, a reindex -- happening after the backup was taken)
    assert app(["--corpus", str(corpus), "delete", "sources/note"]) == 0  # bumps generation
    assert app(["--corpus", str(corpus), "index", "--rebuild"]) == 0       # bumps again
    live_gen_before_restore = read_index_generation(corpus)

    restore_result = restore_state(corpus, old_backup, dry_run=False)
    assert restore_result.generation_regression is not None
    archive_gen, corpus_gen = restore_result.generation_regression
    assert archive_gen < corpus_gen
    assert restore_result.generation_preserved is True
    # generation.json is NOT in `restored` -- it was deliberately skipped
    assert ".alexandria/index/generation.json" not in restore_result.restored

    # THE load-bearing check: the actual generation on disk is unchanged,
    # never regressed to the archive's older value.
    assert read_index_generation(corpus) == live_gen_before_restore


def test_b1_restore_reports_no_regression_for_a_forward_or_equal_restore(tmp_path, monkeypatch):
    """The check must not false-positive on the normal case: restoring a
    backup that is current or newer than the corpus's live generation --
    and in that normal case, generation.json restores exactly as before."""
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    doc = corpus / "sources" / "note.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("---\nsource: test\n---\n\nForward fixture.\n")
    assert app(["--corpus", str(corpus), "index"]) == 0

    backup_path = tmp_path / "current.tar.gz"
    backup_state(corpus, backup_path)

    restore_result = restore_state(corpus, backup_path, dry_run=False)
    assert restore_result.generation_regression is None
    assert restore_result.generation_preserved is False
    assert ".alexandria/index/generation.json" in restore_result.restored


def test_b1_restore_generation_check_handles_a_missing_generation_gracefully(tmp_path, monkeypatch):
    """No generation.json in the archive, or none yet on the corpus -- both
    are normal (a fresh corpus, or a backup taken before any index run),
    not a regression."""
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    corpus.mkdir(parents=True)
    (corpus / ".alexandria").mkdir()
    (corpus / ".alexandria" / "queries.sqlite").touch()

    backup_path = tmp_path / "empty-gen.tar.gz"
    backup_state(corpus, backup_path)  # no generation.json exists yet

    restore_result = restore_state(corpus, backup_path, dry_run=True)
    assert restore_result.generation_regression is None


def test_b1_cli_restore_reports_the_preserved_generation_and_keeps_it_unregressed(
        tmp_path, monkeypatch, capsys):
    """End-to-end through the real CLI: the message must be visible AND the
    on-disk generation after restore must genuinely be unregressed -- not
    just a warning printed while the bad value is written anyway."""
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    doc = corpus / "sources" / "note.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("---\nsource: test\n---\n\nCLI regression fixture.\n")
    assert app(["--corpus", str(corpus), "index"]) == 0

    old_backup = tmp_path / "old.tar.gz"
    assert app(["--corpus", str(corpus), "backup", str(old_backup)]) == 0
    capsys.readouterr()

    assert app(["--corpus", str(corpus), "delete", "sources/note"]) == 0
    assert app(["--corpus", str(corpus), "index", "--rebuild"]) == 0
    capsys.readouterr()
    live_gen = read_index_generation(corpus)

    assert app(["--corpus", str(corpus), "restore", str(old_backup)]) == 0
    err = capsys.readouterr().err
    assert "kept the current" in err
    assert "generation" in err.lower()
    assert read_index_generation(corpus) == live_gen  # genuinely unregressed
