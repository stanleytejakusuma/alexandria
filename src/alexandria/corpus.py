"""Corpus document IO: frontmatter, doc ids, naming, and the immutability guard.

Doc id = corpus-relative path minus `.md` (OKF concept-id). Source notes are named
`<source>-<source_id>-<slug>.md` inside their connector partition.

`body_hash` is the immutability tripwire: source-note *bodies* are never rewritten,
only superseded. Frontmatter is excluded from the hash on purpose -- the two permitted
source mutations (lifecycle fields, `swept`) are frontmatter-only and must not trip it.
"""

from __future__ import annotations

import hashlib
import re
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
    """Return (frontmatter, body). frontmatter is None when absent or malformed."""
    if not text.startswith(FENCE):
        return None, text
    parts = text.split("\n" + FENCE, 1)
    if len(parts) != 2:
        return None, text
    raw = parts[0][len(FENCE):]
    body = parts[1]
    if body.startswith("\n"):
        body = body[1:]
    try:
        fm = yaml.safe_load(raw)
    except yaml.YAMLError:
        return None, text
    if not isinstance(fm, dict):
        return None, text
    return fm, body


def render(fm: dict, body: str) -> str:
    """Serialize frontmatter + body back to a markdown document."""
    raw = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, width=100).rstrip("\n")
    return f"{FENCE}\n{raw}\n{FENCE}\n{body}"


def doc_id(path: str | Path) -> str:
    p = str(path).lstrip("./")
    return p[:-3] if p.endswith(".md") else p


def body_hash(body: str) -> str:
    """Content hash of a note body. Trailing whitespace is not content."""
    return hashlib.sha256(body.rstrip().encode("utf-8")).hexdigest()


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
        out = Path(root) / self.path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render(self.frontmatter, self.body), encoding="utf-8")
        return out
