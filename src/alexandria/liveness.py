"""§7 liveness.

The failure this system actually suffered was a step reporting success
while doing nothing (the weekly loop writing into a directory that did not
exist, aborting every sync on its own redirect while an `--allow-empty`
commit manufactured evidence of success -- it ran zero times for three days
and nothing noticed).

**The primary signal is the age of the oldest unconsumed pending entry**
(`pending.oldest_pending_age`) -- not a heartbeat. `remember` writes the
marker and only a successful promotion consumes it, so this measures the
actual promise ("searchable within one interval"), not whether some process
reported success. A heartbeat fails for exactly the reason the original bug
survived: a run that aborts early never writes one.

This module additionally maintains `liveness.json` -- `last_success_at`,
`promoted_count`, `generation` -- but these are **retained as telemetry
only**, never consulted to compute the age above. What IS consulted is the
file's mere presence: `cmd_index` and `cmd_promote` (and later `serve`'s
inline promote) write it on every completed cycle, so any corpus that
passed `_require_index` (an index exists, meaning `cmd_index` ran at least
once) is expected to have one. A missing-or-unparseable state file on such
a corpus is therefore a real anomaly, not a fresh-install false positive --
"absence of evidence is not evidence of health" (§7), applied to a
precondition this module can actually guarantee rather than one it has to
guess at.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .pending import oldest_pending_age

__all__ = ["LivenessCheck", "STATE_FILE", "check", "record_success"]

STATE_FILE = "liveness.json"

# §11: "the drain survives only as the offline fallback, defaulting to 10
# minutes". Warn past 2x that -- §7's rule -- so a single missed cycle
# (a transient lock skip, W5) never triggers a false alarm.
DEFAULT_DRAIN_INTERVAL_SECONDS = 600.0
WARN_MULTIPLE = 2.0


def _state_path(corpus: str | Path) -> Path:
    return Path(corpus).expanduser() / ".alexandria" / STATE_FILE


def record_success(corpus: str | Path, *, promoted_count: int, generation: int) -> None:
    """Telemetry only. Called after any completed (non-lock-skipped) index or
    promote cycle, whether or not it had anything to do -- a cycle that ran
    and found nothing pending is still a successful liveness datapoint."""
    path = _state_path(corpus)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    tmp.write_text(json.dumps({
        "last_success_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "promoted_count": promoted_count,
        "generation": generation,
    }, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class LivenessCheck:
    stale: bool
    reason: str
    oldest_pending_age_seconds: float | None
    state_file_present: bool


def check(corpus: str | Path, *, drain_interval: float = DEFAULT_DRAIN_INTERVAL_SECONDS) -> LivenessCheck:
    """Fail-closed: any inability to positively confirm health is reported as
    stale, never silently treated as healthy."""
    corpus = Path(corpus).expanduser()

    try:
        age = oldest_pending_age(corpus)
    except OSError as exc:
        return LivenessCheck(
            True, f"could not read pending state ({exc}) -- fail closed", None,
            _state_path(corpus).exists())

    if age is not None and age > WARN_MULTIPLE * drain_interval:
        return LivenessCheck(
            True,
            f"oldest pending entry is {age:.0f}s old (> {WARN_MULTIPLE:.0f}x the "
            f"{drain_interval:.0f}s drain interval) -- promotion may be stuck",
            age, _state_path(corpus).exists())

    state_path = _state_path(corpus)
    if not state_path.exists():
        # §7: "a missing or unparseable state file counts as stale and warns
        # -- fail closed." Safe to apply unconditionally because cmd_index
        # and cmd_promote always write this file on every completed cycle,
        # so by the time a corpus has a real index (the precondition every
        # caller of check() already passed via _require_index) this file is
        # expected to exist; its absence is a genuine anomaly, not a
        # fresh-install false positive.
        return LivenessCheck(
            True, f"{state_path} does not exist -- fail closed", age, False)
    try:
        json.loads(state_path.read_text())
    except (OSError, ValueError):
        return LivenessCheck(
            True, f"{state_path} exists but could not be parsed -- fail closed",
            age, True)

    return LivenessCheck(False, "", age, True)
