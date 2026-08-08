"""wiki-site audit page + answer route-map tests."""

import json

from alexandria.auditlog import AuditLogger
from alexandria.wiki_site import render_audit, render_site, _route_map

TRACE = {
    "rounds": [
        [["sources/a#1", 0.99], ["sources/b#2", 0.5]],
        [["wiki/x#1", 0.8]],
    ],
    "pool": ["sources/a", "sources/b", "wiki/x"],
    "cited": ["sources/a", "wiki/x"],
    "claims": 4,
    "iterations": 1,
}


def test_route_map_renders_stages():
    html = _route_map(TRACE)
    assert "round 1" in html and "round 2" in html
    assert "pool (3)" in html and "cited (2)" in html
    assert "claims=4 iterations=1" in html
    assert "width:99%" in html  # score bar
    assert "sources/a" in html


def test_route_map_empty_trace():
    assert _route_map({}) == ""
    assert _route_map(None) == ""


def test_render_audit_includes_route(tmp_path):
    logger = AuditLogger(tmp_path)
    logger.answer(query="route demo", total_ms=500, emitted=True, model="m",
                  n_claims=4, trace=TRACE)
    render_audit(tmp_path / ".alexandria" / "audit", tmp_path)
    html = (tmp_path / "audit.html").read_text(encoding="utf-8")
    assert "route demo" in html
    assert "route" in html and "claims=4" in html
    assert "round 1" in html


def test_render_site_links_audit_page(tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "hello.md").write_text("---\ntitle: Hello\n---\n# Hello\n", encoding="utf-8")
    (tmp_path / ".alexandria" / "audit").mkdir(parents=True)
    (tmp_path / ".alexandria" / "audit" / "answers.jsonl").write_text(
        json.dumps({"ts": "2026-08-08T10:00:00+0700", "kind": "answer",
                    "query": "q", "total_ms": 1, "emitted": True, "model": "m",
                    "n_claims": 0, "failed_claims": [], "error": "",
                    "stages": {}, "caller": "cli", "user": "local",
                    "trace": {}}) + "\n",
        encoding="utf-8",
    )
    slugs = render_site(wiki, tmp_path / "site",
                        audit_dir=tmp_path / ".alexandria" / "audit")
    assert slugs == ["hello"]
    index = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    assert "Pipeline audit" in index
    assert (tmp_path / "site" / "audit.html").exists()
