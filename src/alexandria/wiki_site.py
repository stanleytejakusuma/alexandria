"""Static wiki site renderer (phase-4 deliverable).

Renders a wiki directory (markdown pages with frontmatter, the exact shape
`run_pipeline` emits) into a self-contained static site: one index.html plus
one .html per page. Stdlib only, no JavaScript, no external assets -- the
fresh-clone test's requirement is that the site renders anywhere, forever.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

__all__ = ["render_markdown", "render_site"]


def _inline(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    return text


def render_markdown(md: str) -> str:
    """A deliberately small markdown subset: headings, paragraphs, bullet
    lists, blockquotes, inline styles, and footnote-style citations rendered
    as a numbered references section. Unknown constructs stay escaped text."""
    out: list[str] = []
    list_open = False
    refs: list[str] = []

    def close_list() -> None:
        nonlocal list_open
        if list_open:
            out.append("</ul>")
            list_open = False

    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            close_list()
            continue
        if m := re.match(r"^(#{1,4})\s+(.*)$", line):
            close_list()
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
        elif line.startswith("- ") or line.startswith("* "):
            if not list_open:
                out.append("<ul>")
                list_open = True
            out.append(f"<li>{_inline(line[2:])}</li>")
        elif line.startswith("> "):
            close_list()
            out.append(f"<blockquote>{_inline(line[2:])}</blockquote>")
        elif m := re.match(r"^\[\^(\d+)\]:\s*(.*)$", line):
            close_list()
            refs.append(m.group(2))
        elif m := re.match(r"^\[(\d+)\]:\s*(.*)$", line):
            close_list()
            refs.append(m.group(2))
        else:
            close_list()
            out.append(f"<p>{_inline(line)}</p>")
    close_list()
    if refs:
        out.append("<h3>References</h3>")
        out.append("<ol>")
        out.extend(f"<li>{_inline(r)}</li>" for r in refs)
        out.append("</ol>")
    return "\n".join(out)


def _frontmatter(md: str) -> tuple[dict[str, object], str]:
    if not md.startswith("---\n"):
        return {}, md
    end = md.find("\n---", 4)
    if end == -1:
        return {}, md
    fm: dict[str, object] = {}
    for line in md[4:end].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fm[key.strip()] = value.strip().strip('"')
    return fm, md[end + 4 :]


def render_site(wiki_dir: Path, out_dir: Path) -> list[str]:
    """Render every *.md under wiki_dir into out_dir. Returns the rendered
    page slugs. Deterministic: pages sorted by slug."""
    wiki = Path(wiki_dir)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "pages").mkdir(exist_ok=True)

    pages = sorted(wiki.rglob("*.md"))
    entries: list[tuple[str, str, str]] = []  # (slug, title, html)
    for page in pages:
        slug = str(page.relative_to(wiki))[:-3].replace("/", "-")
        md = page.read_text(encoding="utf-8", errors="replace")
        fm, body = _frontmatter(md)
        title = str(fm.get("title") or page.stem)
        body_html = render_markdown(body)
        page_html = (
            "<!doctype html><html><head><meta charset=\"utf-8\">"
            f"<title>{html.escape(title)}</title>"
            "<style>body{max-width:46rem;margin:2rem auto;padding:0 1rem;"
            "font-family:system-ui,sans-serif;line-height:1.55}"
            "h1{font-size:1.6rem}blockquote{border-left:3px solid #ccc;"
            "margin:0;padding-left:1rem;color:#444}"
            "code{background:#f2f2f2;padding:.1em .3em;border-radius:3px}"
            "</style></head><body>"
            f"<p><a href=\"../index.html\">&larr; index</a></p>"
            f"<h1>{html.escape(title)}</h1>{body_html}"
            f"<hr><p class=\"src\">source: <code>{html.escape(slug)}</code></p>"
            "</body></html>"
        )
        (out / "pages" / f"{slug}.html").write_text(page_html, encoding="utf-8")
        entries.append((slug, title, body_html))

    rows = "".join(
        f"<li><a href=\"pages/{slug}.html\">{html.escape(title)}</a></li>"
        for slug, title, _ in entries
    )
    index_html = (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<title>Wiki index</title><style>body{max-width:46rem;margin:2rem auto;"
        "padding:0 1rem;font-family:system-ui,sans-serif;line-height:1.55}</style>"
        "</head><body><h1>Wiki index</h1>"
        f"<p>{len(entries)} page(s)</p><ul>{rows}</ul></body></html>"
    )
    (out / "index.html").write_text(index_html, encoding="utf-8")
    return [slug for slug, _, _ in entries]
