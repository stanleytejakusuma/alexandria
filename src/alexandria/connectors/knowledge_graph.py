"""knowledge-graph connector -- ingests the Obsidian knowledge-graph vault.

The vault is the UPSTREAM for most of the corpus. As of 2026-08-11, 20,922 of
28,610 corpus documents (73%) carried `generated.by: connector/<source>` naming
connectors that have never existed in this repository -- they were bulk-imported
by tooling that is simply gone. The corpus could not be regenerated from source,
and 1,516 vault memories had no path into it at all. The false provenance is
precisely why the gap stayed invisible: every document named a producer, so
nothing looked orphaned.

This connector is that missing path. It deliberately preserves each note's
ORIGINAL `source` rather than restamping everything as `knowledge-graph`, so a
note from upstream store X still lands in `sources/X/` under the same filename
the vanished importer produced. Verified on a 600-note random sample:
frontmatter and body are 600/600 identical to what is already on disk, the only
intended difference being `generated.by`.

No LLM. The vault is already structured, human- or agent-curated Markdown;
distilling it would add a fabrication surface and lose the original wording.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from ..corpus import Doc, slugify, source_filename
from .base import NoStateMixin, RawItem

__all__ = ["KnowledgeGraphConnector"]

# index/log/README are vault scaffolding, not knowledge. Syncthing conflict
# copies are duplicates of a note we already ingest under its canonical name.
SKIP_NAMES = {"index.md", "log.md", "README.md"}
SKIP_MARKER = "sync-conflict"

H1_RE = re.compile(r"^#\s+(.+)$", re.M)
WIKILINK_RE = re.compile(r"\[\[([^\]|]+)")
# The projection sweep's own output, dropped on import. Usually just a
# placeholder comment, but for some upstream stores it is POPULATED with scored
# wikilinks (`- [[<id>]] (0.97)`). Those are machine-inferred similarity
# edges, not authored content: keeping them would pollute both the body and
# `entities` with near-duplicate neighbours. So the whole section is removed, up
# to the next `## ` heading or end of note -- `## Annotations` survives, which
# is where hand-edits live.
RELATED_RE = re.compile(r"## Related \(semantic\)\n.*?(?=^## |\Z)", re.S | re.M)

# Derived from all 20,861 vault/corpus pairs that share a path -- not guessed.
# Both mappings are total and unambiguous over that set.
TYPE_MAP = {"memory-file": "memory"}          # 461 cases; all other types identity
STATUS_MAP = {"active": "stable", "closed": "deprecated"}   # 20,327 / 534


def _split(text: str) -> tuple[dict, str]:
    """Return (frontmatter, body). A note without parseable frontmatter yields ({}, '')."""
    if not text.startswith("---"):
        return {}, ""
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, ""
    try:
        fm = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}, ""
    return (fm, parts[2]) if isinstance(fm, dict) else ({}, "")


class KnowledgeGraphConnector(NoStateMixin):
    name = "knowledge-graph"

    def __init__(self, vault_dir):
        self.vault_dir = Path(vault_dir).expanduser()
        # base.NoStateMixin declares `errors` as a CLASS attribute, which is
        # shared by every instance. Shadow it per-instance so one connector's
        # errors cannot leak into another's report.
        self.errors: list[str] = []

    def discover(self) -> list[RawItem]:
        items: list[RawItem] = []
        if not self.vault_dir.is_dir():
            return items
        for path in sorted(self.vault_dir.rglob("*.md")):
            if path.name in SKIP_NAMES or SKIP_MARKER in path.name:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                self.errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
                continue
            fm, body = _split(text)
            source, source_id = fm.get("source"), fm.get("source_id")
            # No source/source_id means we cannot place the note in the corpus
            # layout or dedupe it against an existing document. No H1 means no
            # title, and the title is load-bearing: it is half the filename and
            # the most information-dense line for retrieval.
            if not source or source_id is None or not H1_RE.search(body):
                self.errors.append(f"{path.name}: unusable (source/source_id/H1 missing)")
                continue
            items.append(RawItem(source_id=f"{source}:{source_id}", content=body, meta=dict(fm)))
        return items

    def normalize(self, item: RawItem) -> list[Doc]:
        fm = item.meta
        source, source_id = str(fm["source"]), str(fm["source_id"])
        title = H1_RE.search(item.content).group(1).strip()

        # Clean FIRST, then read entities off the cleaned text. Scanning the raw
        # note instead pulls the projection sweep's similarity links back in via
        # `entities` even though they were just stripped from the body.
        body = RELATED_RE.sub("", item.content)

        tags = list(fm.get("tags") or [])
        if (kind := fm.get("kind")) and kind not in tags:
            tags.append(str(kind))

        # Entities are the note's graph edges: its project plus every wikilink
        # target. They are what makes a vault note findable by association
        # rather than by wording, so they are carried across rather than dropped.
        entities: list[str] = []
        if project := fm.get("project"):
            entities.append(str(project))
        for target in WIKILINK_RE.findall(body):
            if (t := target.strip()) and t not in entities:
                entities.append(t)

        kg_type = str(fm.get("type") or "observation")
        kg_status = str(fm.get("status") or "active")
        out = {
            "type": TYPE_MAP.get(kg_type, kg_type),
            "title": title,
            # The producing connector, honestly stated. The documents this
            # replaces named per-source connectors that do not exist, which is
            # precisely why the reproducibility gap stayed invisible for months.
            "generated": {"by": f"connector/{self.name}",
                          "at": str(fm.get("date") or fm.get("updated") or "1970-01-01")},
            "status": STATUS_MAP.get(kg_status, kg_status),
            "source": source,
            "source_id": source_id,
        }
        for key in ("project", "hash", "source_hash"):
            if value := fm.get(key):
                out[key] = str(value)
        out["tags"] = tags
        out["entities"] = entities

        name = source_filename(source, source_id, title)
        return [Doc(path=f"sources/{slugify(source)}/{name}", frontmatter=out, body=body)]
