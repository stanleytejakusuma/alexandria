# Proposal — invocation habit

**Status:** proposal only, not a change. No file under `~/.pi/` was modified
to produce this document — that tree was read for context (the current
doctrine and tool-binding code) and nothing else.

## The problem, stated with data

`docs/DEMAND-REPORT-2026-08-14.md` §1 found 43 genuine (real, non-eval,
non-synthetic) queries against the corpus across 11 days, of which only
**7** came through the actual agent-tool bindings
(`AlexandriaSearch`/`AlexandriaContext`/`AlexandriaAnswer` in
`~/.pi/agent/extensions/alexandria.ts`). The rest (36) were a human or script
invoking `alexandria search` directly from a shell.

The relevant fact: **the "search first" doctrine already exists and is
already injected into every Pi session.** `~/.pi/agent/AGENTS.md` states,
verbatim, under "Alexandria (knowledge base)":

> Before answering anything about past work, decisions, or project
> history: `alexandria-search` first. If the user asks for synthesis or
> a sourced answer: `alexandria-answer`. At task start, offer/use
> `alexandria-context` when past context plausibly matters.

This is not a proposal to *add* doctrine — it's already there, in every
session's system prompt, and it is producing under one agent-initiated
query per day on average. Prompted doctrine that depends on the agent
choosing, in the moment, to recognize "this looks like a past-work
question," is not converting into invocation at any meaningful rate. That's
the actual gap this proposal is about — not "nobody told the agent to use
it," but "telling it isn't working."

For comparison (as given in the brief, not independently re-verified here —
I did not have access to claude-mem's source, only to `sources/claude-mem/`
observation dumps already ingested into the corpus, which are data *from*
claude-mem, not documentation *about* its mechanism): claude-mem binds 6
lifecycle hooks and auto-captures/injects deterministically, and has
accumulated roughly 12,444 observations against Alexandria's ~43 genuine
queries in the same rough window. The mechanical difference worth naming:
hooks fire unconditionally at a fixed point in the session lifecycle; a
prompt instruction fires only if the agent both recognizes the trigger
condition and chooses to comply, every single time, with no fallback if it
doesn't.

## Options, costed

### A. Do nothing — current state is correct usage

**The position:** the decision doc's own division of labor puts Alexandria's
useful band narrowly at "cross-session, older than working memory,
semantic-not-exact" — recent/local work is `rg`'s job. If that's true, low
volume isn't a bug, it's the tool being correctly scoped. 43 genuine queries
in 11 days, landing 91% of the time on a non-empty, non-weak result (§3 of
the demand report), could just be what "correctly narrow" looks like.

- **Cost:** zero engineering, zero added context/latency to any session.
- **Risk:** if this is wrong, the miss is invisible — every session that
  *should* have retrieved and didn't simply proceeds without anyone noticing
  the gap, because there's no error, just silence. The demand report can't
  distinguish "correctly scoped" from "quietly starved" from the query log
  alone; it can only measure what was asked, not what should have been asked
  and wasn't.
- **This is the defensible-null the brief invited** — and it's not
  refutable by this data alone. What tips me away from recommending it
  outright is finding B below: the actual friction isn't purely
  "not needed," it's partly "not free enough to bother with."

### B. Fix the daemon-routing gap first (orthogonal, cheap, do regardless)

Not really an "invocation habit" fix, but a prerequisite for any of C/D
being worth it: demand report §4 found genuine queries never reach the
always-on daemon's fast path (0/43 went through `client=serve`), so they
cost ~25.8s median even though a sub-5ms path already exists and is
running. `cli.py`'s search/answer commands should try the HTTP daemon first,
the way `alexandria.ts` already does, falling back to CLI-exec only if
unreachable.

- **Cost:** small, contained code change in `cli.py` (not built here — out
  of this arc's scope, flagged for whoever picks it up).
- **Risk:** low. It's applying an existing, already-proven pattern
  (`alexandria.ts`'s HTTP-first-then-fallback) to a second call site.
- **Why it matters for this proposal specifically:** every option below that
  adds automatic/frequent invocation becomes much more expensive to ship if
  each invocation still costs 25s. Do this first, independent of what's
  decided about C/D.

### C. Full session-start auto-injection (the claude-mem-shaped option)

Bind a session-start (or first-user-message) hook that runs
`alexandria-context` unconditionally with the task/first message as the
query, injecting results before the agent's first turn — mirroring
claude-mem's hook model.

- **Cost:** context tokens spent on *every* session, whether or not that
  session ever touches past-work territory. At current cold latency (25–33s
  per STATE-OF-PLAY, confirmed again here even post-daemon-fix because of
  the routing gap in B) this also taxes session-start wall-clock time for
  every session, most of which the demand report suggests won't use the
  result (genuine queries were 4 active days out of 11 even when someone
  *was* actively investigating something retrieval-shaped).
- **Risk:** this is the option the brief specifically asked to be
  skeptical of, and the data supports the skepticism. The decision doc's own
  analysis says the useful band is narrow (cross-session, >1wk old,
  semantic); a blanket inject pays that cost on the wide majority of
  sessions that don't fall in the band. Even with B fixed, this is a
  standing tax, not a one-time cost.
- **Recommendation: do not build this**, at least not without narrowing the
  trigger condition — see D.

### D. Deterministic trigger-phrase hook (narrow, cheap middle path)

A lightweight pre-turn or first-message hook that pattern-matches a short,
explicit trigger list (something like: "what did we", "last time",
"previously", "you mentioned", "did we already", "has this come up before")
against the incoming message, and calls `alexandria-context` *only* when it
matches — otherwise the session proceeds exactly as today.

- **Cost:** low. Fires rarely (by design, matching only the narrow band the
  decision doc already scoped Alexandria into), so the per-session context/
  latency tax is close to zero on the sessions that don't need it.
- **Risk:** false negatives are the same failure mode as today (a session
  that should retrieve, doesn't, because the phrasing didn't match the
  list) — but that's strictly better than today's status quo, not worse:
  today *every* phrasing depends on the agent's judgment; this converts the
  common phrasings into a deterministic hit while leaving the uncommon ones
  exactly where they are now (agent judgment, unchanged). False positives
  (firing on a phrase that isn't actually about past work) cost one
  wasted `alexandria-context` call — bounded, not compounding across every
  session the way C is.
- **This is the mechanical difference from claude-mem stated precisely:**
  claude-mem hooks fire unconditionally at a fixed lifecycle point; this
  hook fires conditionally on a fixed, auditable pattern match. It's a
  smaller, cheaper step toward "deterministic instead of prompted" than
  full parity with claude-mem's model, and it's sized to the narrow-band
  scoping this project has already committed to.
- **Recommendation: this is the one worth prototyping**, after B, and only
  if a few more weeks of the demand report (this is a re-runnable script,
  not a one-off) confirm the 7-in-11-days agent-tool-invocation rate holds
  or stays low — i.e. don't build it reactively to a single snapshot; let
  the trend confirm the doctrine-isn't-converting finding first.

## What I'm explicitly not recommending

Full parity with claude-mem's 6-hook, unconditional-capture model (option C
generalized to writes as well as reads). The corpus's own operating doctrine
(`~/.pi/agent/AGENTS.md`) already treats ambient/ automatic *writes* as
out of scope — memory writes go through an explicit inbox, never
auto-distilled — and the decision doc separately rejected ambient capture
for the same reason (permanent corpus, no deletion path, asymmetric cost of
a bad write vs a missed read). Nothing in this demand report changes that
calculus; if anything, the daemon-routing gap (B) and the small, bursty
genuine-query volume argue for spending the next unit of effort on making
existing reads cheap and occasionally automatic (D), not on expanding what
gets written automatically.

## Suggested order

1. **B** (daemon routing fix) — cheap, no behavioral risk, unblocks
   everything else's cost calculus.
2. **Re-run `scripts/demand-report.py` weekly for a few more cycles** before
   committing to D — one 11-day snapshot with n=43 genuine queries is not
   enough to justify shipping a new hook; a stable low agent-invocation rate
   across 3-4 weekly runs would be.
3. **D** (trigger-phrase hook), if the trend holds, sized to the narrow band
   the decision doc already scoped, not to full claude-mem parity.
4. **Not C**, and not blanket capture, on current evidence.
