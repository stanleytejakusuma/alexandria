# Case study — a day with Alexandria, from the operator's chair

The other two case studies are written from the system's side. This one is
written from **yours**: what you type, what fires without you asking, what comes
back, and — the part that matters — **what it costs you when it is wrong.**

**Read this as a specification of expected behaviour, not a demo.** Phases 1–4
are not built. Where a number is real it is marked ✅ **measured**; where it is a
projection it is marked ⏳ **expected**, and the basis for the expectation is
given. Nothing here is presented as working that is not.

---

## 08:40 — you open a session and type nothing about memory

```
$ pi
> the allocator missed its 08:00 run again
```

**What fires before the model answers anything:**

```
session_start hook
  └─ probe http://127.0.0.1:8420/health        ⏳ expected  ~3ms
     ├─ unreachable → inject nothing, continue silently   ← the common case
     └─ reachable   → POST /search {query: <first user message>, k: 5}
```

⏳ **Expected: sub-second.** Basis: ✅ measured 0.427s warm through `serve` against
the real corpus, versus ✅ 29.17s cold. The entire argument for running `serve` as
a daemon is this one difference — the ~16s embedding-model load amortised across
every query instead of paid on each.

**What lands in your context, unasked:**

```
[alexandria] 3 prior notes, retrieved in 0.4s

  • the-strategy-writer fails when the Arbitrum RPC times out:
    the writer records data_quality='stale' and Postgres rejects it —
    check constraint ventura_strategy_pnl_daily_data_quality_check.
    Unit is an hourly timer oneshot with no Restart=.        (2026-08-12)

  • Diagnosis note: "boot race / After=network-online.target" was WRONG.
    That directive was already present.                       (2026-08-12)
```

**Why this is the whole product in one screen.** You said nine words. You did not
say "search," did not name the service's failure mode, did not remember that you
had already misdiagnosed this once. The second note is worth more than the
first — *it stops you re-running a diagnosis you already falsified.*

That note exists because it was written down after that session. Which is exactly
the step that does not happen today.

> ⚠️ **Expected failure, stated honestly.** With the corpus as it stands, the
> retrieval you would actually get is ✅ **measured**: `search "alexandria write
> path pending marker promote"` → **36.26s**, three results, *all from old
> sessions*, none about the last three days of work. Injection is only as good as
> capture. **This is why ambient write ships before ambient read.**

---

## 09:15 — you correct the agent

```
> no, it's not a boot race — that directive is already in the unit
```

⏳ **Expected tool call:**

```
alexandria-remember {
  text: "the-strategy-writer: the 'boot race /
         After=network-online.target' diagnosis is wrong — the directive is
         already present. Real cause is the hourly oneshot rejecting a
         data_quality='stale' row.",
  corrects: "inbox-<id-of-the-earlier-wrong-note>"
}
→ {status: "promoted", entry_id: "...", chunks_written: 1}      ✅ measured path
```

The write path here is **real and shipped** — ✅ measured: `/remember` over HTTP
promotes inline and the fact is retrievable in the same request cycle
(`CASE-STUDY-the-write-path.md`).

**What is *not* built is what `corrects` does.** Today it is recorded at write
time and **read by nothing** — it appears in no index field and no retrieval
path. So both notes persist with equal standing, and a future search returns the
wrong diagnosis alongside the correction with no signal which supersedes which.

⏳ **Expected after Phase 1:** the corrected note carries `supersedes`, resolution
collapses the pair at read time, and only the current revision surfaces — while
`--as-of` still recovers what the system believed on 2026-08-12. That last part
is the audit property, and it is why the design is append-only rather than
edit-in-place.

---

## 11:30 — you paste a log containing a token

```
> here's the failing request: Authorization: Bearer sk-live-...
```

⏳ **Expected, and non-negotiable before Phase 3 ships:**

```
burst scan (scripts/precommit-scan.py patterns, pre-distillation)
  └─ credential shape detected → burst redacted BEFORE the model is called
```

**Order is the entire control.** Redact-then-distil means the secret never leaves
the machine in a prompt. Distil-then-redact means it already reached the gateway
and you are cleaning up a copy.

This is the single highest-risk item in the package, and the spec's first two
drafts **did not mention it at all**. Automatic capture plus permanent storage
plus no deletion path is, without a filter, a machine for producing unremovable
secrets at machine speed — and the first time it matters, it will have been
running for months.

The honest limit: entropy scanning has false negatives. "the password is hunter2"
in prose defeats it. The claim is that it removes the *mechanical* class of leak,
not that capture is safe. Which is why `ALEXANDRIA_AMBIENT=0` exists, and why
capture lands in `inbox/` where you can drop it before it becomes permanent.

---

## 14:00 — you ask a question you have answered before

```
> why did we pick token bucket over sliding window again?
```

⏳ **Expected:** `alexandria-search` fires without you naming the tool, returns in
under a second warm, and the answer arrives with a citation you can open.

✅ **Measured, today:** the same query cold is **25–50s**. On 2026-08-12 the
recorded times were 50.2s, 36.6s, 33.5s, 28.6s — and one `ETIMEDOUT`.

**That latency is not a UX nit, it is the causal mechanism.** ✅ Measured: during
the 13-hour session that built this system, its author issued **zero** retrieval
queries against it. Of 442 queries logged that day, ~430 were the eval gate and 5
were smoke tests. Not carelessness — at every decision point a 200ms `rg` beat a
30s tool that might time out. **The aggregate of locally correct decisions was a
system nobody used.**

---

## 18:00 — you close the laptop

⏳ **Expected, and nothing is typed:**

```
session_shutdown hook  →  enqueue session for distillation
                          (fast path)
launchd sweep (periodic) →  catch sessions killed without a clean exit
                          (the mechanism; the hook is only its fast path)
  └─ idle gate: skip bursts still accumulating
  └─ substance gate: skip bursts with no human content   ✅ measured, real code
  └─ redaction: skip/clean bursts with credential shapes
  └─ distil → inbox/ → promote → searchable next session
```

✅ **Measured on this session's own transcript** (55.8 MB, 21,383 events):

```
21,383 events → 12,379 after telemetry stripping (9,004 dropped)
16 bursts → 15 substantive → 82 distillation calls
~2,448,472 input tokens for ONE session
```

The substance gate working, ✅ measured — burst 9: **one turn, 8 characters of
human input, beside 22,129 characters of tool output. Skipped, with a reason
recorded.** That gate exists because an earlier version counted *turns* instead
of human content, and seven such bursts produced **45 fabricated notes**: a model
handed a file listing and asked for observations will invent some.

> ⚠️ **This step is not safe to ship yet, and the reason is measured, not
> theoretical.** ✅ `Burst.burst_id` hashes every message, so an open session
> gaining one turn changes its id, misses the `seen` check, and is re-distilled:
>
> ```
> burst_id now                      : 3a862d788848
> burst_id after ONE more user turn : 4f7cf01aaf04
> ```
>
> At an hourly sweep, a 13-hour open session yields **13 permanent
> near-identical document sets** — in a corpus with **no deletion path**.
> Shipping the sweep before the id fix is actively destructive.

---

## The expectations, stated plainly

If this works, here is what you should be able to assert in six months:

1. **You never type a memory command.** Capture is a property of working, not a
   task. If you find yourself invoking it, it has failed in the specific way §3
   of the state-of-play document describes.
2. **A correction sticks.** Telling the agent it is wrong makes the wrong version
   stop surfacing — without destroying the record that it was once believed.
3. **Session start is free.** Sub-second, or it silently does nothing. A memory
   system that makes every session begin with a 30-second stall will be disabled
   within a week, correctly.
4. **The bill is bounded and visible.** 2.4M input tokens per session is a real
   number; the ledger already records model and tokens per call. A bound you can
   see is the difference between a feature and a surprise.
5. **Nothing lands that you cannot inspect first.** Capture lands in `inbox/` and
   becomes permanent only on promote. Given no deletion path, that window is the
   only review opportunity that exists.
6. **It degrades quietly.** Server down, model unavailable, corpus stale — the
   session proceeds normally. Alexandria is never a dependency of getting work
   done.

## What would make me say it failed

- Capture works but retrieval surfaces stale or duplicate notes, and you start
  ignoring the injected block. **Injected context that is routinely wrong is
  worse than none** — it costs attention on every session.
- The token bill lands outside the bound and the bound gets raised instead of the
  capture getting narrower.
- A secret makes it into the corpus. There is no deletion path, so that is not a
  bug to be fixed afterwards — it is permanent.
- Six months in, `queries.sqlite` shows the same thing it shows today: agent-
  initiated queries near zero, everything else machinery. **That is the metric
  that matters, and gate R4 exists to check exactly it** — which is why it now
  requires an allowlist and a rate over ≥20 sessions rather than "non-zero,"
  a bar that passes on a single query.

---

## Status of every claim in this document

| Claim | Basis |
|---|---|
| 0.427s warm / 29.17s cold via `serve` | ✅ measured, scratch corpus |
| `/remember` promotes inline, retrievable same cycle | ✅ measured, shipped code |
| 36.26s real cold query, all results from old sessions | ✅ measured, real corpus |
| 25–50s cold latencies, one ETIMEDOUT | ✅ measured, `queries.sqlite` |
| Zero agent-initiated queries in the build session | ✅ measured, `queries.sqlite` |
| 16 bursts / 82 calls / 2.4M tokens | ✅ measured, real transcript |
| Substance gate skipping an 8-char burst | ✅ measured, real code |
| `burst_id` instability | ✅ demonstrated live |
| `corrects` recorded but read by nothing | ✅ verified in source |
| session_start injection, redaction, `supersedes` resolution, `--as-of` | ⏳ **not built** — Phases 1–4 |
