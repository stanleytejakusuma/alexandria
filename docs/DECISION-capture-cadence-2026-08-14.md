# Decision: what the capture system should be

**Date:** 2026-08-14
**Status:** deliberated, not yet ratified by the operator
**Method:** adversarial deliberation (Red chain), round 1 of a 2-round cap

## The question

Work done today is not retrievable until the Sunday 09:30 weekly loop. Latency
is solved (30s cold → 0.57s warm, via the always-on daemon). Freshness is not.
Two candidate directions were on the table:

- **A.** Raise the cadence of the existing sync+index (~92s per pass). Nearly
  zero new code.
- **B.** Build ambient capture per `docs/SPEC-data-model-and-ambient-capture.md`
  (1,019 lines, none built, two review rounds spent).

## Verdict

**Direction A, modified. Reject B for now.** A ships only after two
prerequisites, and after one zero-cost demand-side experiment that precedes
everything.

## The reasoning that changed the shape of the answer

**Freshness is partly a proxy, and the demand side was never re-verified.** The
zero-query finding predates the latency fix. Nobody has shown the *fast* tool
gets used even for last-week content, which is already fresh enough. If usage
stays near zero for well-covered old content, no capture cadence fixes anything
— the missing piece is invocation habit, not data.

**Retrieval structurally loses to `rg` on fresh content, so stop competing
there.** `rg` wins on today's work because the scope is small, the location is
known, and recall is verifiable-complete. Retrieval wins in a narrow band:
cross-session, older than working memory, semantic rather than exact-string.
That band is real and valuable — and it is *last week and older*. The freshest
content is the least valuable to retrieval, because recency correlates with
memorability. Right division of labour: **`rg` owns today, Alexandria owns
history.** The honest freshness target is "indexed within minutes of session
close", not "indexed while the session is open".

**Raising cadence on an unstable `burst_id` with no deletion path is not a
trade, it is a ratchet.** Thirteen permanent near-duplicate document sets per
long session dilute every future query forever. Duplicate tolerance is
acceptable only where duplicates can die.

**The asymmetry dictates admission policy.** A missing document costs one failed
retrieval. A bad document is a permanent tax on every future query. The
permanent corpus should admit only *stable* content — closed sessions, operator
`remember`, weekly distillation — and never mid-flight snapshots of open
sessions.

## The unlock

**A closed session is one whose id has stopped churning.** Session-close capture
therefore sidesteps the `burst_id` instability *without implementing the id
fix at all* — the blocker that gates ambient capture simply does not apply.
This is why session-close is not merely a cheaper ambient capture; it is a
different risk profile.

## Sequence

0. **Demand experiment — this week, zero code.** Nudge the agent to query
   Alexandria at task start; instrument the query log with the age of retrieved
   documents. Decide from data whether freshness work is warranted at all. This
   gates everything below.
1. **Soft-delete flag, before any cadence change.** A retrieval-respected
   `deleted` flag is functional deletion. Small code; converts the dominant
   constraint (permanence) from irreversible to merely annoying, and de-risks
   every later decision. It does NOT satisfy the crypto-shred rider — but that
   rider binds ambient capture, which is deferred, so they defer together.
2. **Session-close capture, gated to closed sessions only.** Run the existing
   ~92s sync on session close, indexing only closed sessions. A plist plus one
   guard. Freshness becomes "minutes after session end", which covers every use
   case except intra-session concurrency.
3. **Only if step 3's case bites in practice:** an ephemeral "today" tier — a
   separate index over open-session transcripts, rebuilt from scratch each pass
   and discarded on promotion. Rebuilding from scratch makes id instability
   harmless and gives deletion for free. Build after observing real failed
   same-day queries, not before.

## What we refuse to build

- **Ambient capture.** Blocked by the ratified erasure rider, and it maximises
  write volume into a corpus with no deletion path — the worst possible pairing.
  Session-close capture removes most of its motivation.
- **`entity_rev` / `supersedes` chains.** Versioning machinery for a corpus that
  should be append-rarely. Soft-delete plus re-add covers the single-operator
  case.
- **Crypto-shred erasure.** Load-bearing only if ambient capture ships. Deferred
  with it.
- **The typed observation ontology.** An up-front taxonomy layered over semantic
  retrieval. Add tags later if a real query pattern demands them.

**Keep from the spec:** stable id derivation from (session path, first-message
timestamp, window ordinal) — small and genuinely load-bearing — and tombstones,
simplified to the soft-delete flag.

## The one genuine freshness case

Session B asking what session A concluded two hours ago: cross-context, recent,
absent from B's working memory. This is the only case ambient or hourly capture
serves that session-close capture does not. Concurrent writers are *observed* in
this system, so this may be the killer app rather than the edge case — that is
the strongest counter-argument to the ranking above. Log failed same-day queries
before building for it.

## Falsifier

After steps 0 and 2 ship, if query volume stays near zero for four weeks —
*including* against well-covered week-old content — then freshness was never the
constraint, and this tool serves only the narrow band. The honest move then is
to freeze capture work, keep the weekly sync as archival, and accept Alexandria
as a cold-history tool. Conversely, sustained queries hitting documents younger
than 48 hours would validate the cadence investment and reopen the ephemeral
tier with data behind it.
