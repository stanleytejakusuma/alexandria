"""Offline tests for the static wiki site renderer (phase-4)."""

from __future__ import annotations

from pathlib import Path

from alexandria.wiki_site import render_markdown, render_site


def test_render_markdown_subset(tmp_path):
    md = (
        "# The Deal\n\n"
        "Proxima closed. **Key terms** agreed. [^1]\n\n"
        "- first bullet\n"
        "- second bullet\n\n"
        "> a quoted caveat\n\n"
        "[^1]: sources/sales/proxima-notes"
    )
    out = render_markdown(md)
    assert "<h1>The Deal</h1>" in out
    assert "<strong>Key terms</strong>" in out
    assert "<ul>" in out and "<li>first bullet</li>" in out and "<li>second bullet</li>" in out
    assert "<blockquote>a quoted caveat</blockquote>" in out
    assert "<h3>References</h3>" in out
    assert "sources/sales/proxima-notes" in out
    assert "&#x27;" not in out or "<code>" in out  # no raw escaping surprises


def test_render_markdown_escapes_html():
    out = render_markdown("<script>alert(1)</script> plain")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_render_site_builds_index_and_pages(tmp_path):
    wiki = tmp_path / "wiki"
    (wiki / "sub").mkdir(parents=True)
    (wiki / "alpha.md").write_text(
        "---\ntitle: Alpha Page\n---\n\n# Alpha\n\nBody text. [^1]\n\n[^1]: doc-a\n",
        encoding="utf-8",
    )
    (wiki / "sub" / "beta.md").write_text("# Beta\n\nOther body.\n", encoding="utf-8")
    out = tmp_path / "site"
    slugs = render_site(wiki, out)
    assert slugs == ["alpha", "sub-beta"]
    index = (out / "index.html").read_text(encoding="utf-8")
    assert "Alpha Page" in index
    assert "2 page(s)" in index
    assert 'href="pages/alpha.html"' in index
    alpha = (out / "pages" / "alpha.html").read_text(encoding="utf-8")
    assert "<h1>Alpha</h1>" in alpha
    assert "doc-a" in alpha
    beta = (out / "pages" / "sub-beta.html").read_text(encoding="utf-8")
    assert "<h1>Beta</h1>" in beta


def test_render_site_handles_empty_wiki(tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    out = tmp_path / "site"
    assert render_site(wiki, out) == []
    assert "0 page(s)" in (out / "index.html").read_text(encoding="utf-8")
