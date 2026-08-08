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

_CSS = """
:root{
  color-scheme:light dark;
  --bg:#fafafa;--fg:#1a1a1a;--muted:#666;--line:#e5e5e5;
  --accent:#4f46e5;--card:#fff;--code-bg:#f0f0f2;--quote:#8a8a8a;
}
@media (prefers-color-scheme:dark){
  :root{--bg:#111216;--fg:#e8e8ea;--muted:#9a9aa2;--line:#2a2b31;
    --accent:#8b87ff;--card:#1a1b21;--code-bg:#23242b;--quote:#b0b0b8;}
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:16px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
  "Helvetica Neue",Arial,sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:52rem;margin:0 auto;padding:2.5rem 1.5rem 4rem}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
h1{font-size:1.9rem;line-height:1.25;margin:0 0 .4rem;letter-spacing:-.01em}
h2{font-size:1.35rem;margin:2rem 0 .6rem}
h3{font-size:1.1rem;margin:1.6rem 0 .5rem}
h4{font-size:1rem;margin:1.4rem 0 .4rem;text-transform:none}
p{margin:.55rem 0}
ul{padding-left:1.4rem;margin:.55rem 0}
li{margin:.22rem 0}
blockquote{border-left:3px solid var(--line);margin:.9rem 0;padding:.1rem 0 .1rem 1rem;
  color:var(--quote)}
code{background:var(--code-bg);padding:.12em .35em;border-radius:4px;
  font:87.5%/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
strong{font-weight:600}
hr{border:none;border-top:1px solid var(--line);margin:2rem 0}
.src{color:var(--muted);font-size:.85rem}
.back{color:var(--muted);font-size:.9rem}
.back:hover{color:var(--accent);text-decoration:none}
.card{display:block;background:var(--card);border:1px solid var(--line);
  border-radius:10px;padding:1rem 1.15rem;margin:.6rem 0;color:var(--fg)}
.card:hover{border-color:var(--accent);text-decoration:none}
.card h2{margin:0 0 .25rem;font-size:1.05rem}
.card .excerpt{color:var(--muted);font-size:.9rem;margin:0;display:-webkit-box;
  -webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.meta{color:var(--muted);font-size:.85rem;margin-bottom:1.5rem}
.tags{display:flex;gap:.4rem;flex-wrap:wrap;margin-top:.8rem}
.tag{background:var(--code-bg);border:1px solid var(--line);color:var(--muted);
  border-radius:999px;padding:.1rem .6rem;font-size:.78rem}
"""


def _inline(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    return text


def _excerpt(body_html: str) -> str:
    m = re.search(r"<p>(.*?)</p>", body_html, re.S)
    if not m:
        return ""
    return re.sub(r"<[^>]+>", "", m.group(1))[:180]


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


def _tags(fm: dict[str, object]) -> str:
    raw = fm.get("tags")
    if not raw:
        return ""
    tags = [t.strip() for t in str(raw).split(",") if t.strip()]
    return f'<div class="tags">' + "".join(
        f'<span class="tag">{html.escape(t)}</span>' for t in tags
    ) + "</div>"


def _page(title: str, body: str, slug: str, extra: str = "") -> str:
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{html.escape(title)}</title>"
        f"<style>{_CSS}</style></head><body><div class=\"wrap\">"
        f"<p class=\"back\"><a href=\"../index.html\">&larr; wiki index</a></p>"
        f"<h1>{html.escape(title)}</h1>{extra}{body}"
        f"<hr><p class=\"src\">source: <code>{html.escape(slug)}</code></p>"
        "</div></body></html>"
    )


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
        meta = f"<p class=\"meta\">{html.escape(slug)}</p>"
        page_html = _page(title, body_html, slug, meta)
        (out / "pages" / f"{slug}.html").write_text(page_html, encoding="utf-8")
        entries.append((slug, title, body_html))

    cards = "".join(
        f"<a class=\"card\" href=\"pages/{slug}.html\"><h2>{html.escape(title)}</h2>"
        f"<p class=\"excerpt\">{html.escape(_excerpt(body_html))}</p></a>"
        for slug, title, body_html in entries
    )
    index_html = (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>Wiki index</title>"
        f"<style>{_CSS}</style></head><body><div class=\"wrap\">"
        f"<h1>Wiki index</h1>"
        f"<p class=\"meta\">{len(entries)} page(s) &middot; generated by "
        f"<code>alexandria wiki-site</code></p>"
        f"{cards}</div></body></html>"
    )
    (out / "index.html").write_text(index_html, encoding="utf-8")
    return [slug for slug, _, _ in entries]
