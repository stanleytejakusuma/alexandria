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

# How many drain intervals may pass with NO completed cycle before the drain is
# reported dead. 3 tolerates a skipped tick (lock contention) plus jitter while
# still catching a genuinely dead thread inside ~30 min at the default interval.
STALE_CYCLES = 3.0


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
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        return LivenessCheck(
            True, f"{state_path} exists but could not be parsed -- fail closed",
            age, True)

    # HEARTBEAT: the pending-age check above only fires once real user data is
    # already late, so a drain thread that died silently stayed invisible for as
    # long as the queue happened to be empty -- which is most of the time. The
    # asynchronous half writes last_success_at on EVERY completed cycle (a cycle
    # that found nothing is still a datapoint), so the age of that stamp is a
    # direct liveness signal for the drain itself, independent of the workload.
    #
    # STALE_CYCLES, not 1: promote_pending records no success when it skips on
    # lock contention (W5), so an ordinary index run legitimately costs a tick.
    # Alarming on a single miss would train operators to ignore this line -- the
    # same cry-wolf failure reconcile deliberately avoids.
    stamp = state.get("last_success_at")
    heartbeat_age = _heartbeat_age(stamp, state_path)
    if heartbeat_age is None:
        return LivenessCheck(
            True,
            f"{state_path} has no readable last_success_at -- cannot confirm the "
            f"drain has run; fail closed", age, True)
    if heartbeat_age > STALE_CYCLES * drain_interval:
        return LivenessCheck(
            True,
            f"no completed drain cycle for {heartbeat_age:.0f}s (> {STALE_CYCLES:.0f}x "
            f"the {drain_interval:.0f}s interval) -- the promotion drain looks dead, "
            f"so `remember` writes will stay unsearchable; restart `alexandria serve` "
            f"or run `alexandria promote`", age, True)

    return LivenessCheck(False, "", age, True)


def heartbeat_age(corpus: str | Path) -> float | None:
    """Seconds since the drain last completed a cycle, or None if unknown.

    Public counterpart to the internal check: /health exposes this so a monitor
    can watch the asynchronous half directly rather than inferring its health
    from how late user data happens to be.
    """
    path = _state_path(Path(corpus).expanduser())
    try:
        stamp = json.loads(path.read_text()).get("last_success_at")
    except (OSError, ValueError):
        stamp = None
        if not path.exists():
            return None
    return _heartbeat_age(stamp, path)


def _heartbeat_age(stamp: object, state_path: Path) -> float | None:
    """Seconds since the last completed cycle, or None if undeterminable.

    Prefers the recorded timestamp; falls back to the file mtime, because a
    heartbeat whose CONTENT is unreadable but whose file is being rewritten is
    still evidence the drain is running. Returns None only when neither source
    can be read -- the caller fails closed on that.
    """
    if isinstance(stamp, str):
        try:
            recorded = time.mktime(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S"))
            return max(0.0, time.time() - recorded)
        except (ValueError, OverflowError):
            pass
    try:
        return max(0.0, time.time() - state_path.stat().st_mtime)
    except OSError:
        return None
