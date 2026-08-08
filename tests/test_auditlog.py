"""auditlog tests: JSONL append + summary parse."""

from pathlib import Path

from alexandria.auditlog import AuditLogger, audit_summary


def test_logger_appends_and_summarizes(tmp_path):
    corpus = tmp_path / "corpus"
    logger = AuditLogger(corpus)
    logger.answer(query="what happened", total_ms=1200, emitted=True,
                  model="m", n_claims=3)
    logger.answer(query="bad one", total_ms=99, emitted=False, model="m",
                  failed_claims=["c1"], error="native checks")
    logger.sync(connector="journal", duration_ms=42, discovered=2,
                normalized=2, committed=2, skipped=0)
    summary = audit_summary(corpus)
    assert "answers: 2 recent" in summary
    assert "sync: 1 recent" in summary
    assert "emitted=True claims=3" in summary
    assert "emitted=False" in summary and "err=native checks" in summary
    assert "commit=2" in summary
    # rows are JSONL, one per line
    raw = (corpus / ".alexandria" / "audit" / "answers.jsonl").read_text()
    assert raw.count("\n") == 2
    assert Path(corpus / ".alexandria" / "audit" / "answers.jsonl").exists()


def test_logger_survives_unwritable_dir(tmp_path):
    logger = AuditLogger(tmp_path / "nope" / "corpus")
    logger.answer(query="q", total_ms=1, emitted=True, model="m")  # no raise
    assert logger.errors
