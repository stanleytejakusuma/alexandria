"""inbox connector -- ingests user-confirmed writes (`alexandria remember`).

The inbox is the ONLY write surface for explicit memories: a human or an
agent decided, at write time, that a statement is worth keeping. Like
markdown-memory, this is the highest-trust tier -- no LLM touches the text,
and the entry carries provenance (harness, optional session ref) plus an
optional ``corrects`` pointer so a correction stays attached to the claim
it supersedes.

Store format: `inbox/YYYY-MM-DD.md` files, entries separated by a line
containing only `§`, each ending with
`<!-- created=YYYY-MM-DD, last=YYYY-MM-DD, from=pi, session=..., corrects=... -->`.

The extension must never mutate the searchable corpus directly: it calls
`alexandria remember`, which appends here; normal `sync inbox` promotes
entries into `sources/inbox/` and marks them consumed in state.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from ..corpus import Doc, slugify, source_filename
from .base import NoStateMixin, RawItem
from .md_memory import META_RE, SEPARATOR, Entry

__all__ = ["InboxConnector", "parse_inbox_file", "INBOX_META_RE"]

# extended meta: created, last, from (harness), session (optional), corrects (optional)
INBOX_META_RE = re.compile(
    r"<!--\s*created=([0-9-]+),\s*last=([0-9-]+)"
    r"(?:,\s*from=(\w+))?(?:,\s*session=([\w.-]+))?(?:,\s*corrects=([\w-]+))?\s*-->"
)


@dataclass
class InboxEntry:
    text: str
    created: str
    last: str
    harness: str = "pi"
    session: str = ""
    corrects: str = ""

    @property
    def entry_id(self) -> str:
        h = hashlib.sha256(
            f"{self.created}\n{self.text}".encode()).hexdigest()
        return h[:12]

    @property
    def title(self) -> str:
        head = re.split(r"(?<=[.:!?])\s", self.text.strip(), maxsplit=1)[0]
        return " ".join(head.split())[:120] or "Untitled memory"


def parse_inbox_file(path: Path) -> list[InboxEntry]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    out = []
    chunks = raw.split(f"\n{SEPARATOR}\n") if f"\n{SEPARATOR}\n" in raw else [raw]
    for chunk in chunks:
        text = chunk.strip()
        if not text:
            continue
        m = INBOX_META_RE.search(text)
        if m:
            created, last = m.group(1), m.group(2)
            harness = m.group(3) or "pi"
            session = m.group(4) or ""
            corrects = m.group(5) or ""
        else:
            m2 = META_RE.search(text)  # plain markdown-memory meta is accepted
            created, last = m2.group(1), m2.group(2) if m2 else ("", "")
            harness, session, corrects = "pi", "", ""
        body = (INBOX_META_RE.sub("", text)).strip()
        if body:
            out.append(InboxEntry(body, created, last, harness, session, corrects))
    return out


class InboxConnector(NoStateMixin):
    """Promotes append-only inbox entries into corpus sources (no LLM)."""

    name = "inbox"

    def __init__(self, inbox_dir: Path, actor: str = "agent-deliberate/inbox"):
        self.inbox_dir = Path(inbox_dir)
        self.actor = actor

    def discover(self) -> list[RawItem]:
        if not self.inbox_dir.is_dir():
            return []
        items = []
        for f in sorted(self.inbox_dir.glob("*.md")):
            for entry in parse_inbox_file(f):
                items.append(RawItem(
                    source_id=entry.entry_id,
                    content=entry.text,
                    meta={"created": entry.created, "last": entry.last,
                          "harness": entry.harness, "session": entry.session,
                          "corrects": entry.corrects, "file": f.name},
                ))
        return items

    def normalize(self, item: RawItem) -> list[Doc]:
        entry = InboxEntry(
            item.content,
            item.meta.get("created", ""),
            item.meta.get("last", ""),
            item.meta.get("harness", "pi"),
            item.meta.get("session", ""),
            item.meta.get("corrects", ""),
        )
        fm = {
            "type": "memory",
            "title": entry.title,
            "generated": {"by": self.actor,
                          "at": entry.created or entry.last or "1970-01-01"},
            "status": "stable",
            "source": self.name,
            "source_id": item.source_id,
            "tags": ["inbox", "user-confirmed"],
        }
        if entry.corrects:
            fm["corrects"] = entry.corrects
            fm["tags"] = ["inbox", "user-confirmed", "correction"]
        if entry.harness:
            fm["harness"] = entry.harness
        if entry.session:
            fm["session"] = entry.session
        body = f"# {entry.title}\n\n{item.content}\n"
        name = source_filename(self.name, item.source_id, entry.title)
        return [Doc(path=f"sources/{slugify(self.name)}/{name}", frontmatter=fm, body=body)]
