"""C5 freshness: the age of the newest corpus content and newest index finish.

The weekly loop once failed silently for three days (a missing ``mkdir -p``
aborted every output redirect before its command ran) and no gate fired,
because recall@k on a fixed golden set stays green forever on a frozen
corpus.  The general lesson, per SPEC-multi-tenant-and-learning-loop.md
§C5: **quality metrics do not detect liveness failures** -- a system can be
perfectly accurate about stale data.  This module is the C5 freshness check:
it measures TWO liveness signals and fails loudly past a threshold.

1. **Content freshness** -- the age of the newest indexable source document
   on disk (``sources/`` + ``wiki/``, using the same ``is_indexable_source``
   predicate the indexer uses, so quarantined/AppleDouble files never count).
   A frozen corpus stops producing new content; this is the exact failure
   class the 2026-08-11 incident belonged to.
2. **Index freshness** -- the age of the last successful index finish
   (``generation.json``'s ``finished_at``).  Content can keep arriving while
   the index silently stops running; new documents then exist but are
   unsearchable, which recall gates also cannot see.

A corpus with no indexable content is not stale (nothing to measure) and is
reported healthy with a reason.  A corpus whose content exists but whose
index has never finished is a liveness failure ("never indexed").
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .index.chunker import is_indexable_source

# Default threshold: twice the weekly loop cadence, so a healthy once-a-week
# sync never false-alarms while a truly frozen corpus trips within two weeks.
FRESHNESS_DEFAULT_MAX_AGE_DAYS = 14
_FRESHNESS_DEFAULT_MAX_AGE_SECONDS = FRESHNESS_DEFAULT_MAX_AGE_DAYS * 24 * 3600

_GENERATION_FILE = ".alexandria/index/generation.json"


@dataclass(frozen=True)
class StalenessReport:
    """Result of a C5 freshness check.

    ``ok`` is True only when every measurable signal is within ``max_age``
    (and at least one signal was measurable).  ``reasons`` names every signal
    that is missing or past the threshold, so a loud failure is also an
    actionable one.
    """

    content_age_seconds: float | None  # age of the newest source, in seconds
    index_age_seconds: float | None  # age of the newest index finish, in seconds
    max_age_seconds: float
    ok: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)


def _now_utc() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def newest_source_mtime(corpus: Path) -> float | None:
    """Epoch seconds of the newest indexable source file, or None if none."""
    newest: float | None = None
    for root in ("sources", "wiki"):
        base = corpus / root
        if not base.is_dir():
            continue
        for path in base.rglob("*.md"):
            if not is_indexable_source(path.relative_to(corpus)):
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if newest is None or mtime > newest:
                newest = mtime
    return newest


def newest_index_finished_at(corpus: Path) -> float | None:
    """Epoch seconds of the last successful index finish (generation.json)."""
    gen_path = corpus / _GENERATION_FILE
    if not gen_path.is_file():
        return None
    try:
        payload = json.loads(gen_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    raw = payload.get("finished_at") if isinstance(payload, dict) else None
    if not isinstance(raw, str):
        return None
    try:
        finished = _dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=_dt.timezone.utc)
    return finished.timestamp()


def check(corpus: str | Path, *, max_age_seconds: float | None = None) -> StalenessReport:
    """C5 freshness check: fail loudly when the corpus has gone quiet.

    ``max_age_seconds`` defaults to two weeks (``FRESHNESS_DEFAULT_MAX_AGE_DAYS``).
    """
    corpus = Path(corpus)
    threshold = (
        max_age_seconds if max_age_seconds is not None
        else _FRESHNESS_DEFAULT_MAX_AGE_SECONDS
    )
    now = _now_utc().timestamp()

    content_age = newest_source_mtime(corpus)
    index_age = newest_index_finished_at(corpus)
    reasons: list[str] = []

    if content_age is None:
        reasons.append("no indexable documents under sources/ or wiki/")
        return StalenessReport(None, index_age, threshold, True, tuple(reasons))

    content_age_now = max(now - content_age, 0.0)
    if content_age_now > threshold:
        reasons.append(
            f"newest document is {_human_age(content_age_now)} old "
            f"(> {_human_age(threshold)}) -- the corpus has gone quiet; "
            "re-run the weekly loop / sync")

    index_age_now: float | None = None
    if index_age is None:
        reasons.append(
            "documents exist but the index has never finished "
            "(no generation.json finished_at) -- new content may be unsearchable")
    else:
        index_age_now = max(now - index_age, 0.0)
        if index_age_now > threshold:
            reasons.append(
                f"the index last finished {_human_age(index_age_now)} ago "
                f"(> {_human_age(threshold)}) -- content is arriving but is not "
                "being indexed; run `alexandria index`")

    # Both age fields are AGES in seconds (0 == just now), so a caller can
    # compare them directly against ``max_age_seconds``.
    return StalenessReport(content_age_now, index_age_now, threshold, not reasons, tuple(reasons))


def _human_age(seconds: float) -> str:
    if seconds >= 24 * 3600:
        return f"{seconds / (24 * 3600):.1f} days"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} hours"
    return f"{seconds / 60:.1f} minutes"
