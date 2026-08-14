"""Frontmatter schema: shared core + source/wiki profiles.

This is the validator OKF specifies the need for but does not ship.

Two design rules from the spec are enforced mechanically here:

- **The field law (4.5):** every stored field names its writer and its reader.
  A field belonging to the other layer's profile is an ERROR, not a shrug --
  that is how `supersedes` sat unwritten for 14 months elsewhere.
- **Unknown keys are tolerated (OKF 4.1):** forward compatibility beats strictness.
  Unknown *keys* pass; known keys with wrong *shapes* do not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum

__all__ = ["Profile", "Severity", "Issue", "profile_for_path", "validate"]


class Profile(Enum):
    SOURCE = "source"
    WIKI = "wiki"


class Severity(Enum):
    ERROR = "error"
    INFO = "info"


@dataclass(frozen=True)
class Issue:
    severity: Severity
    code: str
    field: str
    message: str

    def __str__(self) -> str:
        return f"[{self.severity.value}] {self.code}: {self.field} -- {self.message}"


# ---------------------------------------------------------------- vocabulary

SOURCE_TYPES = frozenset({"observation", "memory", "task", "daily", "doc"})
WIKI_TYPES = frozenset({"entity", "concept", "decision", "comparison", "person", "project"})
STATUSES = frozenset({"draft", "stable", "deprecated"})
DISPOSITIONS = frozenset({"cited", "skipped"})

# Actor convention (spec 4.2, OKF 7)
ACTOR_RE = re.compile(
    r"^(?:connector/[\w.-]+|sweep/[\w.:-]+|ingest/[\w.:-]+|human:[\w.-]+|agent-deliberate/[\w.-]+)$"
)

CORE_REQUIRED = ("type", "title", "generated")
SOURCE_REQUIRED = ("source", "source_id")
WIKI_REQUIRED = ("sources",)

# `index.md` and `log.md` are the wiki's structural furniture -- a table of contents
# and a run chronology. They assert nothing, so there is nothing for them to cite.
# Exempted BY EXACT PATH rather than by loosening the rule, so "every claim cites"
# stays hard for every actual page.
WIKI_STRUCTURAL = frozenset({"wiki/index", "wiki/log"})

# Fields exclusive to one profile -- presence on the other side is a mismatch.
SOURCE_ONLY = frozenset(
    {"source", "source_id", "source_hash", "hash", "entities", "session", "swept"}
)
WIKI_ONLY = frozenset({"sources", "verified", "stale_after"})

LIST_OF_STR = ("tags", "aliases", "supersedes", "superseded_by", "entities")


def profile_for_path(path: str) -> Profile | None:
    """Doc id / corpus-relative path decides the profile. Layer is structural."""
    p = str(path).lstrip("./")
    if p.startswith("sources/"):
        return Profile.SOURCE
    if p.startswith("wiki/"):
        return Profile.WIKI
    return None


def _doc_id(path: str) -> str:
    p = str(path).lstrip("./")
    return p[:-3] if p.endswith(".md") else p


def _is_date(v: object) -> bool:
    if isinstance(v, (date, datetime)):
        return True
    if not isinstance(v, str):
        return False
    try:
        datetime.fromisoformat(v.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def validate(fm: dict, path: str) -> list[Issue]:
    """Validate one document's frontmatter. Returns [] when clean."""
    issues: list[Issue] = []
    err = lambda code, field, msg: issues.append(Issue(Severity.ERROR, code, field, msg))  # noqa: E731

    if not isinstance(fm, dict):
        return [Issue(Severity.ERROR, "bad_type", "<document>", "frontmatter must be a mapping")]

    profile = profile_for_path(path)
    if profile is None:
        err("unknown_profile", "<path>", f"{path!r} is under neither sources/ nor wiki/")
        return issues

    # --- required fields -------------------------------------------------
    required = CORE_REQUIRED + (SOURCE_REQUIRED if profile is Profile.SOURCE else WIKI_REQUIRED)
    if profile is Profile.WIKI and _doc_id(path) in WIKI_STRUCTURAL:
        required = CORE_REQUIRED
    for field in required:
        if fm.get(field) in (None, "", []):
            err("missing_required", field, f"required for the {profile.value} profile")

    # --- profile exclusivity (the field law) -----------------------------
    foreign = (WIKI_ONLY if profile is Profile.SOURCE else SOURCE_ONLY) & fm.keys()
    for field in sorted(foreign):
        other = "wiki" if profile is Profile.SOURCE else "source"
        err("profile_mismatch", field, f"{other}-profile field on a {profile.value} document")

    # --- core shapes ------------------------------------------------------
    if "type" in fm:
        allowed = SOURCE_TYPES if profile is Profile.SOURCE else WIKI_TYPES
        if fm["type"] not in allowed:
            err("bad_enum", "type", f"{fm['type']!r} not in {sorted(allowed)}")

    if "title" in fm and not isinstance(fm["title"], str):
        err("bad_type", "title", "must be a string")

    # Soft-delete tombstone (ARC-BRIEF soft-delete). Core, not profile-exclusive
    # -- both sources and wiki pages can be marked deleted. Strict bool, not
    # merely truthy: index/chunker.py's doc_frontmatter_metadata reads this
    # with `is True`, so a quoted `deleted: "false"` is treated as NOT
    # deleted there (the safer misread), but it is still a mistake worth
    # surfacing at lint time rather than only relying on that asymmetry.
    if "deleted" in fm and not isinstance(fm["deleted"], bool):
        err("bad_type", "deleted", "must be a boolean")

    if "status" in fm and fm["status"] not in STATUSES:
        err("bad_enum", "status", f"{fm['status']!r} not in {sorted(STATUSES)}")

    gen = fm.get("generated")
    if gen is not None:
        if not isinstance(gen, dict):
            err("bad_type", "generated", "must be a mapping {by, at}")
        else:
            for key in ("by", "at"):
                if not gen.get(key):
                    err("missing_required", f"generated.{key}", "both `by` and `at` are required")
            actor = gen.get("by")
            if isinstance(actor, str) and not ACTOR_RE.match(actor):
                err("bad_actor", "generated.by",
                    f"{actor!r} breaks the actor convention "
                    "(connector/<n>, sweep/<m>, ingest/<m>, human:<n>, agent-deliberate/<h>)")
            if gen.get("at") is not None and not _is_date(gen["at"]):
                err("bad_date", "generated.at", f"{gen['at']!r} is not an ISO date/datetime")

    for field in LIST_OF_STR:
        v = fm.get(field)
        if v is None:
            continue
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            err("bad_type", field, "must be a list of strings")

    # --- source profile ---------------------------------------------------
    if profile is Profile.SOURCE:
        swept = fm.get("swept")
        if swept is not None:
            if not isinstance(swept, dict):
                err("bad_type", "swept", "must be a mapping {at, disposition}")
            else:
                if not swept.get("at"):
                    err("missing_required", "swept.at", "required when `swept` is present")
                disp = swept.get("disposition")
                if disp is not None and disp not in DISPOSITIONS:
                    err("bad_enum", "swept.disposition", f"{disp!r} not in {sorted(DISPOSITIONS)}")
        if "session" in fm and not isinstance(fm["session"], str):
            err("bad_type", "session", "must be a string")

    # --- wiki profile -----------------------------------------------------
    else:
        srcs = fm.get("sources")
        if srcs is not None:
            if not isinstance(srcs, list):
                err("bad_type", "sources", "must be a list of {id, resource, title?}")
            else:
                for n, entry in enumerate(srcs):
                    if not isinstance(entry, dict):
                        err("bad_shape", f"sources[{n}]", "must be a mapping")
                        continue
                    for key in ("id", "resource"):
                        if not entry.get(key):
                            err("bad_shape", f"sources[{n}].{key}",
                                "citation entries need both `id` and `resource`")

        ver = fm.get("verified")
        if ver is not None:
            entries = [ver] if isinstance(ver, dict) else ver  # bare mapping == 1-element list
            if not isinstance(entries, list):
                err("bad_type", "verified", "must be a list of {by, at} (or one bare mapping)")
            else:
                for n, entry in enumerate(entries):
                    if not isinstance(entry, dict):
                        err("bad_shape", f"verified[{n}]", "must be a mapping {by, at}")
                        continue
                    for key in ("by", "at"):
                        if not entry.get(key):
                            err("bad_shape", f"verified[{n}].{key}", "required")

        if "stale_after" in fm and not _is_date(fm["stale_after"]):
            err("bad_date", "stale_after", f"{fm['stale_after']!r} is not an absolute date")

    return issues
