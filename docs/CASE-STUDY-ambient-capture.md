# Case study — a normal working session, with Alexandria baked in

The companion document (`CASE-STUDY-the-write-path.md`) walks the path you drive
by hand. This one asks the harder question: **what happens during an ordinary
session where nobody invokes Alexandria at all?**

**Ambient capture is specified and not built** (`SPEC-data-model-and-ambient-capture.md`,
Phase 3). So this is not a demo of a working feature. It is something more useful:
the segmentation, substance gating, and retrieval components **do** exist, so
every number below was produced by running the real code against a real session
transcript. Only the *trigger* is simulated.

The session under the microscope is the one that built the write-path package:
`019fd02b`, 2026-08-11 through 2026-08-13, 55.8 MB, 21,375 events.

---

## Part 0 — what actually happened, with no ambient anything

The honest baseline. Query log for 2026-08-12, the day the package was built:

| Source | Count |
|---|---|
| Golden-set eval (the pre-commit gate) | ~430 |
| `serve` acceptance smoke tests | 5 |
| **Genuine retrieval by the agent working** | **~8, all from the previous session** |

**Zero retrieval queries during a 13-hour build session**, by the person who
wrote the system, on the day he wrote it. Four causes, all verifiable:

1. **Latency.** Measured that day: 50.2s, 36.6s, 33.5s, 28.6s, and one recorded
   `ETIMEDOUT`. Against `rg` at ~200ms. Every individual decision to skip it was
   locally correct.
2. **The corpus cannot answer questions about the session in progress.** Nearly
   everything needed was from that same day.
3. **The trigger is prose.** `memory_search` gets called because it arrives as a
   structured policy block with explicit trigger conditions. Alexandria's
   doctrine arrives as prose in a project file. Structure beats prose under time
   pressure.
4. **The substrate was faster.** Reconstructing history meant reading
   `~/.pi/agent/sessions/*.jsonl` and `git log` — the *same underlying data*
   Alexandria indexes, read directly.

This is the entire case for ambient capture. Memory that depends on being
invoked does not get invoked.

---

## Part 1 — what ambient capture would have done to this session

All of this is real output from `connectors/pi_sessions.py`.

```python
events = [json.loads(l) for l in session_jsonl.open() if l.strip()]   # 21,383
kept   = strip_telemetry(events)                                      # 12,379
bursts = segment_bursts(kept, gap_hours=4.0)                          # 16
```

Telemetry stripping drops 9,004 events — `custom` (8,794), `session_info`,
`model_change`, `thinking_level_change`. None of it is conversation and none of
it should ever reach a distiller.

The 16 bursts, with the substance gate applied:

```
burst  0: turns= 35 user_chars= 17,306 transcript=  551,611 pieces= 5 keep=True
burst  1: turns= 20 user_chars=  9,951 transcript=  362,390 pieces= 4 keep=True
burst  2: turns= 54 user_chars=  9,860 transcript=  687,408 pieces= 6 keep=True
burst  3: turns=  1 user_chars=    284 transcript=   14,939 pieces= 1 keep=True
burst  4: turns= 33 user_chars=  8,331 transcript=  629,667 pieces= 6 keep=True
burst  5: turns= 61 user_chars=  9,791 transcript=1,145,043 pieces=10 keep=True
burst  6: turns=  9 user_chars=    568 transcript=  139,822 pieces= 2 keep=True
burst  7: turns= 30 user_chars=  4,280 transcript=  421,682 pieces= 4 keep=True
burst  8: turns=106 user_chars= 22,553 transcript=1,355,113 pieces=12 keep=True
burst  9: turns=  1 user_chars=      8 transcript=   22,129 pieces= 1 keep=False
          <- user_chars=8 < 180 (turns=1, tool output not counted)
burst 10: turns= 12 user_chars=  2,248 transcript=  401,493 pieces= 4 keep=True
burst 11: turns= 10 user_chars=  1,088 transcript=   86,003 pieces= 1 keep=True
burst 12: turns= 84 user_chars= 47,839 transcript=1,071,515 pieces= 9 keep=True
burst 13: turns= 43 user_chars= 29,331 transcript=1,488,405 pieces=13 keep=True
burst 14: turns= 13 user_chars=  4,664 transcript=  384,095 pieces= 4 keep=True
burst 15: turns=  9 user_chars=  1,218 transcript=   73,484 pieces= 1 keep=True
```

**Burst 9 is the substance gate earning its place.** One turn, 8 characters of
human input, beside 22,129 characters of tool output. Skipped, with a reason
recorded. This gate exists because an earlier version counted *turns* rather than
*human content*: seven such bursts produced **45 fabricated notes**, because a
model handed a file listing and asked for observations will invent some.

Bursts are split at 120k chars, giving **82 distillation calls**:

```
distillation calls        : 82
transcript chars sent     : 8,814,498
~input tokens             : 2,448,472
```

**2.4M input tokens to capture one session.** That is the number the spec's
"measure for a week, then bound" clause exists to discover, and it is larger than
I would have guessed before running it.

---

## Part 2 — the defects this session would have hit

### The burst id is not stable, demonstrated live

`Burst.burst_id` hashes every message's role and text. Its docstring claims it is
"content-derived and stable" — and it is stable against iteration order and
wall-clock, which is what the author was thinking about. It is **not** stable
against the session gaining another turn:

```
burst_id now                       : 3a862d788848
burst_id after ONE more user turn  : 4f7cf01aaf04
same burst, different id           : True
```

An **open** session therefore gets a new id every time anyone says anything. It
misses the `seen` check and is distilled again. And because `SPEC`'s §5.1.1 adds
a *periodic sweep* — deliberately, to catch sessions killed without a clean
shutdown — the sweep runs against live sessions and makes this worse, not better.

The compounding, computed on this session's own open burst:

```
open burst = 75,446 chars = 20,957 tokens per pass
after  1h open:  1 pass,  1 duplicate doc set,   20,957 tokens
after  6h open:  6 passes, 6 duplicate doc sets, 125,743 tokens
after 13h open: 13 passes, 13 duplicate doc sets, 272,444 tokens
```

Thirteen permanent, near-identical document sets describing one conversation —
**in a corpus with no deletion path.** The fix is in the spec (derive the id from
session path, first-message timestamp, and window ordinal) but it is not built,
and shipping the sweep before the fix would be actively destructive.

### Secrets

This session's transcript contains API endpoints, a keychain service name, host
addresses, and file paths from a machine running live trading services. A real
one would routinely contain pasted credentials. Ambient capture would send
8.8 MB of it to a model gateway and write the results permanently.

That gap is why `SPEC` §5.5 exists — redact **before** distillation, reusing the
existing `scripts/precommit-scan.py` rather than a second scanner that drifts —
and it was missing from the spec's first two drafts entirely.

### Duplicates against what is already there

The corpus already holds 10,644 documents under `sources/pi-sessions/`. This
session would add ~82 more document sets covering work that is *also* described
in the four inbox entries flushed by hand yesterday. Cross-connector semantic
duplication is explicitly deferred in the spec — deliberately, because a wrong
similarity threshold silently discards real knowledge — but it is a known,
growing cost, not a solved problem.

---

## Part 3 — the read side, measured

If a session started right now and Alexandria injected context automatically,
this is literally what would happen:

```bash
alexandria --corpus ~/alexandria-corpus search "alexandria write path pending marker promote" --k 3
```

```
1. ...alexandria-demoted-off-critical-path-deny-scan-promoted-to-r5   score=0.666
2. ...weekly-loop-requires-env-vars-from-launchagent                   score=0.607
3. ...alexandria-session-passed-non-deterministic-pi-jobs-probe        score=0.567

real  0m36.260s
```

Two things to notice, and both are the point:

**36.26 seconds.** Worse than the 25–33s documented elsewhere. Injecting this at
session start, synchronously, would make every session begin with a
half-minute stall. This is why `SPEC` §6 requires `serve` running as a supervised
daemon *and* makes injection optional and degradable — with the server warm the
same query is sub-second (measured: 0.427s in the companion case study).

**Everything returned is from old sessions.** The write-path package, the two
audits, the negative eval set, the spec revisions — none of it is here, because
none of it has been distilled. The retrieval is working correctly; the corpus
simply does not contain the last three days.

All three results clear the 0.12 relevance floor comfortably. They are *related*
to the query and none of them is what a session starting now actually needs.
This is the honest state of the read side: **it retrieves what it has, and what
it has is what somebody remembered to sync.**

---

## Part 4 — what is real here, and what is not

| Element | Status |
|---|---|
| Burst segmentation, telemetry stripping, substance gate | **real code, run on the real transcript** |
| 16 bursts / 82 calls / 2.4M tokens | **measured** |
| `burst_id` instability | **demonstrated live** |
| Redistillation compounding | **computed** from the measured open burst |
| Retrieval output and 36.26s | **real query against the real corpus** |
| Zero-query baseline | **real**, from `queries.sqlite` |
| The automatic *trigger* | **does not exist** — every step above was invoked by hand |
| Distillation output (the observations themselves) | **not run here** — an LLM call per burst, deliberately not fired |

---

## Part 5 — what this changes

Three things this exercise established that the spec had wrong or missing:

1. **The cost is bigger than assumed.** 2.4M input tokens for one session. The
   "measure for a week, then bound" discipline is not bureaucratic caution — at
   this rate a bound is the difference between a feature and a bill.
2. **Ordering is load-bearing, and the spec's build order is right for a reason
   it did not state.** Shipping the §5.1.1 sweep before the `burst_id` fix would
   produce duplicate permanent documents *faster than a human could notice*, in a
   system with no delete. The sweep is not safe on its own.
3. **Ambient read is worth less than ambient write until capture exists.**
   Injection today would surface three-day-stale context at a 36-second cost.
   Write first, read second — which is the order the spec already specifies,
   now with a measurement behind it.

The strongest argument for this whole package remains Part 0: the system was not
used by the person most motivated to use it, on the day he built it — not through
carelessness, but because at every single decision point the cheap certain path
won. **The aggregate of locally correct decisions was a system nobody used.**

---

## Reproducing this

```bash
cd ~/codebase/alexandria
unset ALEXANDRIA_EMBED_PROVIDER
.venv/bin/python3 - <<'EOF'
import json, copy
from pathlib import Path
from alexandria.connectors.pi_sessions import (
    strip_telemetry, segment_bursts, substance, split_oversized)

p = sorted(Path.home().glob(".pi/agent/sessions/*/*.jsonl"))[-1]   # any session
events = [json.loads(l) for l in p.open() if l.strip()]
bursts = segment_bursts(strip_telemetry(events), gap_hours=4.0, session_id="x")

calls = chars = 0
for b in bursts:
    v = substance(b, min_user_chars=180)
    print(f"turns={b.user_turns:>3} user_chars={b.user_chars:>7,} keep={v.keep} {v.reason}")
    if v.keep:
        for piece in split_oversized(b, 120_000):
            calls += 1; chars += len(piece.transcript())
print(f"\n{calls} distillation calls, ~{chars/3.6:,.0f} input tokens")

b = bursts[-1]; b2 = copy.deepcopy(b)
b2.messages.append({"role": "user", "text": "one more question"})
print(f"burst_id stable across an appended turn: {b.burst_id == b2.burst_id}")
EOF
```

Nothing above writes to the corpus. `substance`, `segment_bursts`, and
`split_oversized` are pure functions over parsed events.
