"""Corpus document IO: frontmatter, doc ids, naming, and the immutability guard.

Doc id = corpus-relative path minus `.md` (OKF concept-id). Source notes are named
`<source>-<source_id>-<slug>.md` inside their connector partition.

`body_hash` is the immutability tripwire: source-note *bodies* are never rewritten,
only superseded. Frontmatter is excluded from the hash on purpose -- the two permitted
source mutations (lifecycle fields, `swept`) are frontmatter-only and must not trip it.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml

__all__ = [
    "Doc", "body_hash", "doc_id", "render", "slugify", "source_filename",
    "split_frontmatter",
]

FENCE = "---"
MAX_SLUG = 60


def split_frontmatter(text: str) -> tuple[dict | None, str]:
    """Return (frontmatter, body). frontmatter is None when absent or malformed.

    Both delimiters must be lines consisting of exactly `---`. A document whose YAML
    ends with `--- trailing prose` is malformed and is reported as such rather than
    being silently accepted with three characters shaved off.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != FENCE:
        return None, text
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == FENCE:
            raw = "".join(lines[1:i])
            body = "".join(lines[i + 1:])
            try:
                fm = yaml.safe_load(raw)
            except yaml.YAMLError:
                return None, text
            return (fm, body) if isinstance(fm, dict) else (None, text)
    return None, text          # never closed


def render(fm: dict, body: str) -> str:
    """Serialize frontmatter + body back to a markdown document."""
    raw = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, width=100).rstrip("\n")
    return f"{FENCE}\n{raw}\n{FENCE}\n{body}"


def doc_id(path: str | Path) -> str:
    p = str(path).lstrip("./")
    return p[:-3] if p.endswith(".md") else p


def body_hash(body: str) -> str:
    """Exact content hash of a note body.

    Deliberately byte-exact, including trailing whitespace: this is the immutability
    tripwire, and a guard that forgives whitespace edits is a guard with a hole in it.
    """
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def slugify(text: str, max_len: int = MAX_SLUG) -> str:
    """ASCII, lowercase, hyphenated. Deterministic -- it is part of file identity."""
    norm = unicodedata.normalize("NFKD", str(text))
    ascii_only = norm.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_only).strip("-").lower()
    slug = re.sub(r"-{2,}", "-", slug)[:max_len].strip("-")
    return slug or "untitled"


def source_filename(source: str, source_id: str, title: str) -> str:
    return f"{slugify(source)}-{slugify(source_id)}-{slugify(title)}.md"


@dataclass
class Doc:
    path: str                      # corpus-relative, e.g. sources/pi/pi-1-x.md
    frontmatter: dict = field(default_factory=dict)
    body: str = ""

    @property
    def doc_id(self) -> str:
        return doc_id(self.path)

    @property
    def body_hash(self) -> str:
        return body_hash(self.body)

    @classmethod
    def read(cls, path: str | Path, root: str | Path) -> Doc:
        path, root = Path(path), Path(root)
        text = path.read_text(encoding="utf-8")
        fm, body = split_frontmatter(text)
        if fm is None:
            raise ValueError(f"{path}: missing or malformed frontmatter")
        return cls(path=str(path.relative_to(root)), frontmatter=fm, body=body)

    def write(self, root: str | Path) -> Path:
        """Atomically replace this document without exposing a torn Markdown file.

        Connectors may be interrupted mid-sync. State checkpoints are already
        temp-and-replace; corpus sources need the same guarantee so the next
        run sees either the preceding complete document or this complete one.
        """
        out = Path(root) / self.path
        out.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            dir=out.parent, prefix=f".{out.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(render(self.frontmatter, self.body))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, out)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return out
