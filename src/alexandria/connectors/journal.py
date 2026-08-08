"""journal connector -- ingests the curated daily accountability journal.

The accountability journal is the owner's curated daily digest. Per the ratified
decision (2026-08-08), v1 ingests the CURATED journal only -- the
pillar-tagged daily digest (citadel: personal-finance/accountability.md) --
and explicitly NOT raw Telegram exports: no chat API, no continuous sync, no
attachment handling. Raw conversation ingestion waits until real queries
demonstrate missing detail.

The journal is a deliberate, human-curated write (like markdown-memory), so
no LLM is involved. Each `## YYYY-MM-DD` section becomes one note; pillar
tags are parsed from the `[pillar: ...]` bullets and applied as note tags.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from ..corpus import Doc, slugify, source_filename
from .base import NoStateMixin, RawItem

__all__ = ["JournalConnector", "parse_journal", "PILLAR_RE"]

PILLAR_RE = re.compile(r"\[(pillar|job|product|brand|finance|infra)(?::\s*([a-z-]+))?\]")
SECTION_RE = re.compile(r"^##\s+([0-9]{4}-[0-9]{2}-[0-9]{2})\s*$", re.M)


@dataclass
class JournalSection:
    date: str
    text: str
    pillars: list[str]


def parse_journal(path: Path) -> list[JournalSection]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    matches = list(SECTION_RE.finditer(raw))
    if not matches:
        return []
    sections = []
    for i, m in enumerate(matches):
        date = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        text = raw[start:end].strip()
        if not text:
            continue
        pillars = sorted({(tag or label) for label, tag in PILLAR_RE.findall(text)})
        sections.append(JournalSection(date, text, pillars))
    return sections


class JournalConnector(NoStateMixin):
    name = "journal"

    def __init__(self, journal_path: str | Path, actor: str = "connector/journal"):
        self.journal_path = Path(journal_path).expanduser()
        self.actor = actor

    def discover(self) -> list[RawItem]:
        if not self.journal_path.is_file():
            return []
        items = []
        for section in parse_journal(self.journal_path):
            sid = hashlib.sha256(
                f"{section.date}\n{section.text}".encode()).hexdigest()[:12]
            items.append(RawItem(
                source_id=sid,
                content=section.text,
                meta={"date": section.date, "pillars": section.pillars},
            ))
        return items

    def normalize(self, item: RawItem) -> list[Doc]:
        date = item.meta.get("date", "")
        title = f"Accountability journal {date}"
        fm = {
            "type": "memory",
            "title": title,
            "generated": {"by": self.actor, "at": date},
            "status": "stable",
            "source": self.name,
            "source_id": item.source_id,
            "tags": ["journal", "accountability"] + item.meta.get("pillars", []),
        }
        body = f"# {title}\n\n{item.content}\n"
        name = source_filename(self.name, item.source_id, title)
        return [Doc(path=f"sources/{slugify(self.name)}/{name}", frontmatter=fm, body=body)]
