"""#54 at-rest lints, deferred out of #52's Red review: asset-digest audit,
stray .partial sweep, orphan assets, duplicate-digest companions.

These are READ-ONLY checks over an already-ingested corpus -- they report,
never repair (repair is `alexandria ingest --refresh` for a bad companion, or
a human decision for an orphan/duplicate, since deleting either is a
judgment call this engine will not make silently).
"""

from pathlib import Path

from alexandria.ingest import lint_assets


def _mk_companion(corpus: Path, name: str, digest: str, asset_rel: str,
                  extra_fm: str = "") -> Path:
    d = corpus / "sources/assets" / name
    d.parent.mkdir(parents=True, exist_ok=True)
    d.write_text(
        "---\ntype: doc\nsource: ingest\ningest:\n"
        f"  sha256: {digest}\n  asset: {asset_rel}\n{extra_fm}"
        "---\n\nbody text\n")
    return d


def _mk_asset(corpus: Path, rel: str, content: bytes) -> Path:
    p = corpus / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def test_a_clean_corpus_reports_no_findings(tmp_path):
    corpus = tmp_path
    import hashlib
    digest = hashlib.sha256(b"hello world").hexdigest()
    _mk_asset(corpus, f"assets/{digest[:2]}/{digest}.pdf", b"hello world")
    _mk_companion(corpus, "doc-abcd1234.md", digest, f"assets/{digest[:2]}/{digest}.pdf")
    findings = lint_assets(corpus)
    assert findings == [], findings


def test_asset_digest_mismatch_is_reported(tmp_path):
    corpus = tmp_path
    import hashlib
    digest = hashlib.sha256(b"original bytes").hexdigest()
    asset = _mk_asset(corpus, f"assets/{digest[:2]}/{digest}.pdf", b"CORRUPTED bytes")
    _mk_companion(corpus, "doc-x.md", digest, f"assets/{digest[:2]}/{digest}.pdf")
    findings = lint_assets(corpus)
    assert any("digest" in f.lower() or "sha" in f.lower() or "mismatch" in f.lower()
              for f in findings), findings


def test_a_stray_partial_file_is_reported(tmp_path):
    corpus = tmp_path
    _mk_asset(corpus, "assets/ab/.abcd1234.pdf.partial", b"half-written")
    findings = lint_assets(corpus)
    assert any("partial" in f.lower() for f in findings), findings


def test_an_orphan_asset_with_no_companion_is_reported(tmp_path):
    corpus = tmp_path
    import hashlib
    digest = hashlib.sha256(b"nobody claims me").hexdigest()
    _mk_asset(corpus, f"assets/{digest[:2]}/{digest}.pdf", b"nobody claims me")
    findings = lint_assets(corpus)
    assert any("orphan" in f.lower() for f in findings), findings


def test_two_companions_claiming_the_same_digest_is_reported(tmp_path):
    corpus = tmp_path
    import hashlib
    digest = hashlib.sha256(b"shared bytes").hexdigest()
    asset_rel = f"assets/{digest[:2]}/{digest}.pdf"
    _mk_asset(corpus, asset_rel, b"shared bytes")
    _mk_companion(corpus, "doc-one.md", digest, asset_rel)
    _mk_companion(corpus, "doc-two.md", digest, asset_rel)
    findings = lint_assets(corpus)
    assert any("duplicate" in f.lower() for f in findings), findings


def test_a_companion_pointing_at_a_missing_asset_is_reported(tmp_path):
    corpus = tmp_path
    import hashlib
    digest = hashlib.sha256(b"gone").hexdigest()
    _mk_companion(corpus, "doc-missing.md", digest, f"assets/{digest[:2]}/{digest}.pdf")
    findings = lint_assets(corpus)
    assert any("missing" in f.lower() for f in findings), findings


def test_no_assets_directory_at_all_is_not_an_error(tmp_path):
    corpus = tmp_path
    (corpus / "sources").mkdir()
    assert lint_assets(corpus) == []


def test_cli_lint_surfaces_asset_findings(tmp_path, monkeypatch):
    from alexandria.cli import app
    corpus = tmp_path
    import hashlib
    digest = hashlib.sha256(b"nobody claims me either").hexdigest()
    _mk_asset(corpus, f"assets/{digest[:2]}/{digest}.pdf", b"nobody claims me either")
    rc = app(["--corpus", str(corpus), "lint"])
    assert rc != 0


def test_store_asset_temp_name_is_not_deterministic_across_writers():
    """The old temp name was a bare sibling of the final asset (".<name>.partial"),
    so two concurrent ingests of the SAME bytes could interleave writes to the
    SAME temp file. The name must be unique per writer (this process, this call)."""
    import inspect
    from alexandria import ingest as ing
    src = inspect.getsource(ing._store_asset)
    assert "os.getpid()" in src or "uuid" in src.lower(), (
        "the temp asset name must include something unique per writer/call")
