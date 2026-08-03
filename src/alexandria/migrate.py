"""One-shot transform-copy of a kg-sync-style flat markdown vault into the corpus.

Ships as the generic worked example of a migration: read a flat vault of markdown +
YAML frontmatter, remap fields onto the corpus schema, partition by producing system,
quarantine what will not parse, and reconcile the counts exactly.

Two rules govern the mapping:

- **Bodies are copied verbatim** (minus machine-generated sections that have been
  replaced by a real feature). The territory is not editorialized.
- **The field law:** legacy frontmatter keys with no reader in the new schema are
  dropped, not carried forward as decoration. Every dropped key is counted and
  reported, so the loss is auditable rather than silent.

The source vault is opened read-only. Nothing here writes to it.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .corpus import Doc, body_hash, slugify, source_filename, split_frontmatter
from .schema import validate

__all__ = ["MigrationReport", "migrate_kg_sync"]

# legacy type -> corpus type. Source profile allows: observation|memory|task|daily|doc
TYPE_MAP = {
    "observation": "observation",
    "memory": "memory",
    "memory-file": "memory",
    "task": "task",
    "daily": "daily",
    "project": "doc",       # proj-* hubs land in the territory; wiki seeding is phase 2
    "doc": "doc",
}
# legacy kind -> type, used only when legacy `type` is missing or unmappable
KIND_MAP = {
    "insight": "observation", "session": "observation", "feature": "observation",
    "change": "observation", "bugfix": "observation", "refactor": "observation",
    "decision": "memory", "task-closed": "task", "task-open": "task",
}
STATUS_MAP = {"active": "stable", "closed": "deprecated", "orphaned": "draft"}

# Keys consumed by the mapping (read, then intentionally not carried verbatim).
CONSUMED = {"kind", "date", "updated", "status", "type"}
# Keys with no reader in the new schema. Dropped per the field law; counted for audit.
NO_READER = {"prev", "next", "target", "category"}

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)")
# machine-generated section replaced by /v1/similar -- dropped, not migrated
DROP_SECTION_RE = re.compile(r"\n##+\s*Related \(semantic\).*?(?=\n##\s|\Z)", re.DOTALL)
MAX_ENTITIES = 24


@dataclass
class MigrationReport:
    total_in: int = 0
    written: int = 0
    quarantined: int = 0
    collapsed: int = 0
    dupes_kept: int = 0
    by_source: Counter = field(default_factory=Counter)
    by_type: Counter = field(default_factory=Counter)
    by_status: Counter = field(default_factory=Counter)
    dropped_keys: Counter = field(default_factory=Counter)
    schema_errors: list[str] = field(default_factory=list)
    quarantine_names: list[str] = field(default_factory=list)

    @property
    def reconciles(self) -> bool:
        return self.total_in == self.written + self.quarantined + self.collapsed

    def render(self) -> str:
        lines = [
            "migration report",
            "=" * 60,
            f"  files in vault      {self.total_in:>7}",
            f"  written             {self.written:>7}",
            f"  quarantined         {self.quarantined:>7}  (no/!parse frontmatter)",
            f"  collapsed dupes     {self.collapsed:>7}  (same source_id, identical body)",
            f"  kept dupes (INFO)   {self.dupes_kept:>7}  (same source_id, different body)",
            "-" * 60,
            f"  reconciles          {'YES' if self.reconciles else 'NO -- INVESTIGATE'}"
            f"   ({self.written} + {self.quarantined} + {self.collapsed}"
            f" = {self.written + self.quarantined + self.collapsed} vs {self.total_in} in)",
            f"  schema errors       {len(self.schema_errors):>7}",
        ]
        for label, counter in (("by source", self.by_source), ("by type", self.by_type),
                               ("by status", self.by_status)):
            lines.append(f"\n  {label}:")
            lines += [f"    {k:<20} {v:>7}" for k, v in counter.most_common()]
        if self.dropped_keys:
            lines.append("\n  legacy keys dropped (no reader in new schema):")
            lines += [f"    {k:<20} {v:>7}" for k, v in self.dropped_keys.most_common()]
        if self.schema_errors:
            lines.append("\n  first schema errors:")
            lines += [f"    {e}" for e in self.schema_errors[:10]]
        return "\n".join(lines)


def _entities(fm: dict, body: str) -> list[str]:
    """Cheap pattern pass: wikilink targets + project. No private lexicon required --
    the vault's own 100k wikilinks already name its entities."""
    found: list[str] = []
    seen: set[str] = set()
    for cand in ([fm["project"]] if fm.get("project") else []) + WIKILINK_RE.findall(body):
        name = " ".join(str(cand).split()).strip()
        key = name.lower()
        if name and key not in seen and len(name) <= 80:
            seen.add(key)
            found.append(name)
    return found[:MAX_ENTITIES]


def _clean_body(body: str) -> str:
    return DROP_SECTION_RE.sub("\n", body).rstrip() + "\n"


def _str_list(value: object) -> list[str]:
    """Coerce a legacy list field to list[str].

    YAML parses an unquoted `2021-06-17` as a `datetime.date`, so date-shaped aliases
    and tags arrive as objects rather than strings (261 daily notes in the reference
    vault). `str()` reproduces the value exactly as it was written.
    """
    if value in (None, "", []):
        return []
    items = value if isinstance(value, list) else [value]
    return [s for s in (str(v).strip() for v in items) if s]


def _transform(fm: dict, body: str, report: MigrationReport) -> tuple[dict, str]:
    source = str(fm.get("source") or "unknown")
    legacy_type = str(fm.get("type") or "")
    kind = str(fm.get("kind") or "")

    new_type = TYPE_MAP.get(legacy_type) or KIND_MAP.get(kind) or "observation"
    status = STATUS_MAP.get(str(fm.get("status") or "active"), "stable")
    if kind == "task-closed":
        status = "deprecated"

    tags = _str_list(fm.get("tags"))
    if kind and kind not in tags:
        tags.append(kind)           # legacy kind survives as sub-taxonomy

    when = fm.get("date") or fm.get("updated") or ""
    clean = _clean_body(body)

    out: dict = {
        "type": new_type,
        "title": str(fm.get("title") or "").strip() or _title_from_body(clean),
        "generated": {"by": f"connector/{slugify(source)}", "at": str(when)},
        "status": status,
        "source": source,
        "source_id": str(fm.get("source_id") or ""),
    }
    for key in ("description", "project", "hash", "source_hash"):
        if fm.get(key):
            out[key] = fm[key]
    if tags:
        out["tags"] = tags
    for key in ("supersedes", "superseded_by", "aliases"):
        if values := _str_list(fm.get(key)):   # empty legacy lists carry no information
            out[key] = values
    ents = _entities(fm, clean)
    if ents:
        out["entities"] = ents

    for key in fm:
        if key in NO_READER:
            report.dropped_keys[key] += 1
    return out, clean


def _title_from_body(body: str) -> str:
    for line in body.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip() or "Untitled"
        if line.strip():
            return line.strip()[:80]
    return "Untitled"


def migrate_kg_sync(vault: str | Path, corpus: str | Path, dry_run: bool = True,
                    limit: int | None = None) -> MigrationReport:
    """Transform-copy `vault` into `corpus`. The vault is never written to."""
    vault, corpus = Path(vault), Path(corpus)
    report = MigrationReport()

    files = sorted(p for p in vault.glob("*.md") if p.is_file())
    if limit:
        files = files[:limit]

    # (source, source_id) -> list of body hashes already emitted
    seen: dict[tuple[str, str], list[str]] = defaultdict(list)

    for path in files:
        report.total_in += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        fm, body = split_frontmatter(text)

        if fm is None:
            report.quarantined += 1
            report.quarantine_names.append(path.name)
            if not dry_run:
                dest = corpus / "sources" / "_unparsed" / path.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(text, encoding="utf-8")
            continue

        new_fm, new_body = _transform(fm, body, report)
        key = (new_fm["source"], new_fm["source_id"])
        bh = body_hash(new_body)

        if bh in seen[key]:
            report.collapsed += 1               # exact duplicate -- one copy is enough
            continue
        suffix = ""
        if seen[key]:
            report.dupes_kept += 1              # same id, different content: keep both
            suffix = f"-{len(seen[key]) + 1}"
        seen[key].append(bh)

        name = source_filename(new_fm["source"], new_fm["source_id"] or path.stem,
                               new_fm["title"])
        rel = f"sources/{slugify(new_fm['source'])}/{name[:-3]}{suffix}.md"
        doc = Doc(path=rel, frontmatter=new_fm, body=new_body)

        issues = validate(new_fm, rel)
        if issues:
            report.schema_errors.append(f"{path.name}: {issues[0]}")

        if not dry_run:
            doc.write(corpus)

        report.written += 1
        report.by_source[new_fm["source"]] += 1
        report.by_type[new_fm["type"]] += 1
        report.by_status[new_fm["status"]] += 1

    return report
