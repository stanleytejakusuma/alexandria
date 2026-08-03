"""markdown-memory connector -- ingests flat-markdown memory stores.

These stores are *deliberate* writes: a human or an agent decided at the time that
something was worth keeping. That judgment is the highest-trust signal in the corpus,
so these notes carry the `agent-deliberate/pi` actor rather than a machine-extraction
actor, and no LLM is involved here -- the material is already structured.

Store format: entries separated by a line containing only `§`, each ending with an
HTML comment `<!-- created=YYYY-MM-DD, last=YYYY-MM-DD -->`.

Ingesting these is the precondition for evicting them: a capped store cannot be
pruned safely until its contents exist somewhere durable.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from ..corpus import Doc, slugify, source_filename
from .base import RawItem

__all__ = ["MarkdownMemoryConnector", "Entry", "parse_store", "STORE_KINDS"]

SEPARATOR = "§"
META_RE = re.compile(r"<!--\s*created=([0-9-]+),\s*last=([0-9-]+)\s*-->")

# store filename -> (corpus type, tags). `failures` are lessons, not incidents.
STORE_KINDS = {
    "USER.md": ("memory", ["user-preference"]),
    "MEMORY.md": ("memory", ["global"]),
    "failures.md": ("memory", ["lesson", "failure"]),
}


@dataclass
class Entry:
    text: str
    created: str
    last: str
    store: str
    project: str = ""

    @property
    def entry_id(self) -> str:
        """Content-derived: re-ingesting an unchanged entry must be a no-op, and an
        edited entry must land as a new note rather than mutating the old one."""
        h = hashlib.sha256(f"{self.store}\n{self.text}".encode()).hexdigest()
        return h[:12]

    @property
    def title(self) -> str:
        head = re.split(r"(?<=[.:!?])\s", self.text.strip(), maxsplit=1)[0]
        return " ".join(head.split())[:120] or "Untitled memory"


def parse_store(path: Path, project: str = "") -> list[Entry]:
    """Split one store into entries. A store with no separator is a single entry."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    out = []
    for chunk in raw.split(f"\n{SEPARATOR}\n") if f"\n{SEPARATOR}\n" in raw else [raw]:
        text = chunk.strip()
        if not text:
            continue
        m = META_RE.search(text)
        created, last = (m.group(1), m.group(2)) if m else ("", "")
        body = META_RE.sub("", text).strip()
        if body:
            out.append(Entry(body, created, last, path.name, project))
    return out


class MarkdownMemoryConnector:
    name = "markdown-memory"

    def __init__(self, memory_dir, projects_dir=None,
                 actor: str = "agent-deliberate/harness"):
        self.memory_dir = Path(memory_dir).expanduser()
        self.projects_dir = Path(projects_dir).expanduser() if projects_dir else None
        self.actor = actor

    def discover(self) -> list[RawItem]:
        items: list[RawItem] = []
        for filename in STORE_KINDS:
            for entry in parse_store(self.memory_dir / filename):
                items.append(self._item(entry))
        if self.projects_dir and self.projects_dir.is_dir():
            for store in sorted(self.projects_dir.glob("*/MEMORY.md")):
                for entry in parse_store(store, project=store.parent.name):
                    items.append(self._item(entry))
        return items

    @staticmethod
    def _item(entry: Entry) -> RawItem:
        return RawItem(source_id=entry.entry_id, content=entry.text,
                       meta={"created": entry.created, "last": entry.last,
                             "store": entry.store, "project": entry.project})

    def normalize(self, item: RawItem) -> list[Doc]:
        """No LLM: the material is already a deliberate, structured statement.
        Distilling it would only add a fabrication surface and lose the human wording."""
        store = item.meta.get("store", "MEMORY.md")
        kind, tags = STORE_KINDS.get(store, ("memory", ["project"]))
        entry = Entry(item.content, item.meta.get("created", ""),
                      item.meta.get("last", ""), store, item.meta.get("project", ""))

        fm = {
            "type": kind,
            "title": entry.title,
            # Deliberate write-time judgment -- the highest trust tier in the corpus.
            "generated": {"by": self.actor,
                          "at": item.meta.get("created") or item.meta.get("last") or ""},
            "status": "stable",
            "source": self.name,
            "source_id": item.source_id,
            "tags": list(tags),
        }
        if project := item.meta.get("project"):
            fm["project"] = project
            fm["tags"] = tags + ["project-memory"]
        if not fm["generated"]["at"]:
            fm["generated"]["at"] = "1970-01-01"      # unknown, but the field is required

        body = f"# {entry.title}\n\n{item.content}\n"
        name = source_filename(self.name, item.source_id, entry.title)
        return [Doc(path=f"sources/{slugify(self.name)}/{name}", frontmatter=fm, body=body)]
