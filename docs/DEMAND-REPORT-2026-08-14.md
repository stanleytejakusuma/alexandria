# Demand Report — 2026-08-14

**Status:** first run, complete. Answers step 0 of
`docs/DECISION-capture-cadence-2026-08-14.md`: is low query volume a demand
problem (nobody needs this) or a supply problem (nothing prompts the agent to
use it)? Re-run weekly with `scripts/demand-report.py` to watch the trend —
this doc is a snapshot, not a standing truth.

> **CORRECTION (applied 2026-08-14, after review — read before the numbers below).**
> This report's original classifier treated the audit-log caller `local-anonymous`
> as proof of a synthetic probe. It is not: `serve.py:45` assigns that identity to
> *any* TCP caller, so a canary and a real question are stamped identically. That
> made the claim "no genuine query ever reached the daemon" true **by construction
> rather than by measurement**, and discarded 10 real queries.
>
> `scripts/demand-report.py` now classifies daemon rows on their text content, and
> two regression tests in `tests/test_demand_report.py` pin both directions. The
> corrected headline is **genuine = 58, not 43/48**, of which **10 (17%) arrived
> through the daemon**, not zero.
>
> The single clearest piece of evidence: the query *"Prime Agent system prompt"*
> appears twice, 2.5 minutes apart on 08-14 — once via the extension's CLI fallback
> (`caller=pi-extension`, counted genuine) and once via its HTTP primary path
> (`client=serve`, discarded as synthetic). Same person, same intent, opposite
> buckets. Since `alexandria.ts` is HTTP-first with CLI as fallback, `client=serve`
> is the extension's *primary* surface — exactly where genuine usage lands.
>
> **Sections 1–4 below retain the original n=43 figures.** Their *direction* is
> unchanged (eval traffic still dominates ~93%; the freshness falsifier is still
> not observed), but treat the absolute genuine counts as a floor and re-run
> `scripts/demand-report.py` for current numbers. The daemon-specific claims in
> §1 and §4 are **superseded by this note**.

> **RELABEL (2026-08-14, Sol audit SOL-05) — "genuine" now means something narrower.**
> The correction above still over-claimed: it labeled every non-probe
> `search`/`serve`/`answer` row "genuine", but `client` is a *code-path* label,
> not evidence of who called. `scripts/demand-report.py` now reserves `genuine`
> for the one positive caller identity — the pi extension self-labels
> `ALEXANDRIA_CALLER=pi-extension` — and reports code-path-only rows as
> `likely_genuine`. The audit log's default `caller="cli"` is also no longer
> treated as genuine: it is the log's fallback, i.e. unattributed.
>
> Current run (2026-08-14, 3609 rows): `genuine` 8, `likely_genuine` 41,
> `uncertain` 162. The honest real-usage estimate is **49 (8 confirmed + 41
> likely)**, bounded below by 8 and above by ~211 (49 + 162 uncertain). Read
> "58 genuine" anywhere below as "49 real-usage, 8 confirmed" under this taxonomy.
> The direction of the report's verdict (invocation habit is the binding
> constraint, not freshness) is unchanged.

**Source data:** `~/alexandria-corpus/.alexandria/queries.sqlite`, 3154 rows,
2026-08-03 through 2026-08-14 (11 days), opened read-only throughout. Cross-
referenced against `~/alexandria-corpus/.alexandria/audit/search.jsonl` (97
rows, a separate log that records a `caller` identity — `cli`, `pi-extension`,
`local-anonymous`, `consumer-audit` — that `queries.sqlite`'s own `client`
column does not reliably carry, see Methodology).

## Headline numbers

| Classification | Count | % of 3154 |
|---|---:|---:|
| `eval_infra` (confirmed automated eval/gate/benchmark traffic) | 2939 | 93.2% |
| `synthetic_probe` (confirmed canary/health-check/self-test traffic) | 37 | 1.2% |
| `genuine` (confirmed real usage) | **43** | **1.4%** |
| `uncertain` (could not confidently classify) | 135 | 4.3% |

The brief's working assumption going in was "~5 real queries in the tool's
lifetime." That undercounts by roughly 8×. The corrected number is **43
genuine queries across 11 days**, but they land on only **4 of those 11
days** (Aug 9, 11, 12, 13) — real usage is bursty, not ambient, and the
correction changes the magnitude, not the qualitative picture: usage is
still very low relative to the corpus's 93%-eval-traffic query log.

Of the 43 genuine queries, only **7 came through the actual agent-tool
bindings** (`AlexandriaSearch`/`AlexandriaContext`/`AlexandriaAnswer`, via
`~/.pi/agent/extensions/alexandria.ts`, identified by
`ALEXANDRIA_CALLER=pi-extension`). The other 36 came through a human or
script directly invoking `alexandria search` from a shell (`caller=cli` in
the audit log). This split matters for the invocation-habit question — see
`docs/PROPOSAL-invocation-habit-2026-08-14.md`.

## 1. Volume by client, separated from eval/test infrastructure

`client` alone is **not reliable** for this split — see Methodology below,
this is the report's main non-obvious finding. After cross-referencing with
the audit log's `caller` field, exact golden-set text matching, and a
tight-burst replay detector:

```
cli            eval_infra        2843      negative-probe eval_infra          25
cli            genuine             33      search         genuine             10
cli            synthetic_probe     17      search         synthetic_probe      1
cli            uncertain          135      separation     eval_infra          71
                                            serve          synthetic_probe     19
```

`client=serve` (the always-on daemon) has **zero genuine rows** in this
dataset — all 19 `serve` rows are anonymous canary probes (`caller:
local-anonymous`, confirmed by fingerprinted probe text like "obscure
phrase... zebra quantum ledger"). Real usage never went through the daemon.
This is load-bearing for the latency finding below.

`uncertain` (135) is concentrated on 2026-08-06/07 (43 + 67 = 110 of 135,
~81%), the two heaviest known eval-gate days. Read this as **probably also
automated** (multi-second gaps consistent with an LLM-graded eval loop
between retrieval calls, not human pauses) rather than a hidden pool of real
usage — but this is not confirmed the way `eval_infra`/`genuine` are, and is
reported as uncertain rather than folded into either bucket.

## 2. Age of retrieved documents at query time — the freshness falsifier

Restricted to the 43 genuine queries: 213 retrieved-document references, 211
resolved to a frontmatter `generated.at` date (2 dropped for negative deltas
— clock skew / date-only granularity, not counted as failures).

```
age (hours since doc generated, at query time):
  n=211  min=6.5  p50=214.1 (~8.9 days)  p90=1957.3 (~81.6 days)  max=45157 (~5.2 years)

  <48h        20  ( 9.5%)
  48h-1wk     62  (29.4%)
  1wk-1mo     75  (35.5%)
  >1mo        54  (25.6%)
```

**61% of what genuine queries retrieve is more than a week old.** Only 9.5%
is younger than 48h. This is a distribution, not a mean, per the brief's
instruction — the median (~9 days) already tells the story, and the tail
(some docs 5+ years old, from `derived/*` daily knowledge-graph rollups whose
`generated.at` reflects the rolled-up calendar date, not sync date) confirms
the corpus genuinely serves old content when queried, not just old content
existing unused.

This directly corroborates the decision doc's provisional read: the corpus's
real value is the "last week and older, cross-session, semantic-not-exact"
band, not freshness. With actual query data behind it now instead of
inference.

## 3. Failed / empty retrievals

```
empty retrieval (0 results):                0 / 43
weak top-score (<= bottom decile, 0.085):    4 / 43
```

**Zero genuine queries returned nothing.** The decision doc's stated trigger
for building the ephemeral "today" tier was observing a **same-day failed
query against fresh content** — that specific event did not happen in this
window. I inspected all 4 weak-score cases by hand: their top-3 retrieved
documents range from 146 hours to 3281 hours old (6 to 137 days) — none
involved young content. There is no evidence in this dataset of the
freshness gap the ephemeral tier would fix.

This is the single most decision-relevant number in the report: it directly
answers the decision doc's step-0 falsifier question, and the answer is "not
observed" — with the caveat that 43 queries over 11 days is a small sample
and a same-day-failure event could simply not have occurred yet by chance.

## 4. Latency — cold vs warm, and a wiring gap the daemon fix didn't reach

```
genuine-only:
  cold (cache_hit=0):  n=37  p50=25.8s  p90=32.8s  max=72.4s
  warm (cache_hit>0):  n=6   p50=9.9s   p90=12.2s  max=28.6s

serve daemon (client=serve, all synthetic_probe traffic):
  cache_hit=1 (full result reuse):  0.7–4.8ms
  cache_hit=0 (novel query, daemon still warm): 0.5–26s
```

The always-on daemon's fast path is real (sub-5ms on exact-repeat hits) —
but **none of the 43 genuine queries went through it.** Real traffic in this
data entered exclusively via the `cli`/`search` code paths, which spin up a
fresh cold Python process (plus cold model load) every time, regardless of
whether the daemon is running. The result: genuine-query latency (p50 25.8s
cold) still matches the *original* pre-fix complaint from the decision doc,
not the "0.03–0.8s warm" figure that description was based on — that figure
turns out to describe only the daemon's exact-repeat-query cache-hit case.

This is a **plumbing gap, not a modeled-latency problem**: `cli.py`'s search
path doesn't try the HTTP daemon first the way
`~/.pi/agent/extensions/alexandria.ts` already does (HTTP-to-127.0.0.1:8420,
falling back to CLI-exec only if the daemon is unreachable). Making the CLI
path daemon-first would let genuine queries benefit from the fix that's
already deployed. This is a separate, smaller fix than either the freshness
question or the invocation-habit proposal, but it's cheap and directly
undercuts the daemon investment's payoff as currently wired — worth doing
regardless of the invocation-habit decision.

## Verdict

**Neither pure demand nor pure supply — but the dominant, actionable
constraint is invocation habit, not freshness, and not the daemon's own
latency.**

- **Freshness (the original hypothesis behind the cadence question):**
  not supported by data. 0/43 empty retrievals, all 4 weak results hit
  content 6+ days old, only 9.5% of genuine retrievals touch <48h content.
  This corroborates the decision doc's call to defer the ephemeral tier —
  now with a real (if small) sample instead of assumption.
- **Volume is genuinely low**, even after correcting the "~5" estimate to
  43: 4 active days out of 11, and only 7 of those 43 came through the
  agent-tool surface the global doctrine
  (`~/.pi/agent/AGENTS.md`, "Alexandria-first for cross-session knowledge")
  is supposed to produce. That doctrine already exists, is already injected
  into every session, and is producing under one agent-initiated query per
  day on average. This is the strongest evidence that the binding
  constraint is invocation habit, not lack of a need — see
  `docs/PROPOSAL-invocation-habit-2026-08-14.md` for what to do about it,
  argued skeptically per the brief's instruction.
- **The daemon-routing gap** (§4) is a separate, concrete, cheap fix that
  should happen regardless of the invocation decision — it's currently
  undercutting the very latency fix the decision doc credited with removing
  the "30s cold" objection.

## Explicit uncertainty (what this data cannot tell you)

- **n=43 is small.** Day-level and cluster-level patterns (e.g., all of Aug
  11's activity landing in two research-session-shaped bursts) are
  suggestive, not statistically robust. A few more weeks of this report will
  matter more than this single snapshot.
- **The `uncertain` bucket (135, 4.3%) is a genuine blind spot.** It is
  probably mostly automated (concentrated on the two heaviest eval days,
  absent from the caller-confirmed genuine days) but that is circumstantial,
  not confirmed the way `eval_infra`/`genuine`/`synthetic_probe` are. Do not
  read "genuine=43" as a hard floor; read it as a confirmed floor with an
  unresolved 135-row gray zone above it.
- **Why real usage clusters on Aug 11–13 specifically is not established.**
  I did not attempt to reconstruct what those sessions were investigating —
  doing so would mean reading and possibly quoting real query text, which
  the brief's leak-scan constraint asks me to avoid doing loosely, and it
  risks fitting a narrative I can't fully verify. Flagged as open.
- **The `client` column is confirmed unreliable** for genuine/eval
  separation on its own (see Methodology) — a code-level fix so `client`
  reflects the true caller/tool path consistently would make future runs of
  this report cheaper and more precise than the audit-log-cross-reference
  workaround this script currently relies on.
- **`alexandria answer` has zero rows in `queries.sqlite`** despite
  `cli.py`'s `cmd_answer` explicitly constructing its search engine with
  `client="answer"` (confirmed: one real `answer` invocation exists in
  `.alexandria/audit/answers.jsonl` with a 328-second total latency, but no
  matching row in `queries.sqlite`). This report cannot see `answer`-path
  query volume at all — an instrumentation gap outside this arc's fix scope,
  worth flagging for whoever owns `synthesis/pipeline.py` next.

## Methodology

`scripts/demand-report.py` (stdlib-only: `sqlite3`, `json`, `re`,
`statistics`, `collections`, `datetime`, `pathlib`, `argparse`; no new
dependency). Opens `queries.sqlite` via `file:...?mode=ro` — never writes.
Re-run any time with:

```
.venv/bin/python scripts/demand-report.py
```

Classification precedence per row (first match wins), implemented in
`classify()`:

1. **Batch-replay detection** (`find_batch_replay_ids`) — a maximal run of
   same-client rows with consecutive timestamp gaps < 5.0s and length >= 5 is
   an automated replay burst (rationale: a genuine query, cold or even a
   warm-but-novel daemon hit, costs single-digit-to-tens of seconds; 5+
   full-latency real queries closer than 5s apart is structurally
   impossible). This check runs **first** and overrides every other signal,
   because it caught a ~89-question replayed benchmark set (one burst: 71
   queries in ~1 wall-clock second) that matched none of the four committed
   golden/eval `.jsonl` files by text — i.e. a benchmark this script could
   not otherwise identify by content. → `eval_infra`.
2. Exact text match against the combined golden/negative/contradiction/
   contest-blind query sets (84 unique texts), or `client` in
   `{separation, negative-probe}` (confirmed single-purpose calibration
   clients by construction). → `eval_infra`.
3. Audit-log `caller` in `{local-anonymous, consumer-audit}` (confirmed
   anonymous-daemon-hit canary traffic and a self-test sweep of the audit
   pathway itself), or query text matches a canary/probe fingerprint regex
   (`canary`, `obscure phrase`, `novel probe`, `zebra quantum ledger`,
   `Session: <uuid>`, etc.). → `synthetic_probe`.
4. Audit-log `caller` in `{pi-extension, cli}` (confirmed real caller
   identities), or `client` in `{search, serve, answer}`. → `genuine`.
5. Remaining `client=cli` rows: burst-gap < 2.0s to the previous same-client
   row (below `BATCH_MIN_SIZE` but still matching the measured burst
   signature of confirmed eval traffic, median 0.54s) → `eval_infra`;
   otherwise → `uncertain`.

**Why `client` alone fails:** cross-referencing all 97 audit-log rows
(which independently record a `caller`) against `queries.sqlite` by exact
query text + timestamp within 120s found 97/97 matched, but landed under
inconsistent `client` tags — e.g. `caller=cli` rows split roughly 53/10
between `client=cli` and `client=search`; `caller=pi-extension` (7 total)
split 6/1 the same way. The root cause was not fully diagnosed (noted as an
open instrumentation question, not chased further — see Explicit
uncertainty), but the audit-log cross-reference reliably works around it.

Doc-age resolution (`resolve_doc_date`) parses the `generated:\n  at:`
frontmatter field on the corpus markdown file each `retrieved_ids` entry
points to (`sources/<connector>/<slug>#<chunkhash>` → strip the `#chunkhash`
suffix, resolve `.md`).

**Tests:** `tests/test_demand_report.py`, 6 cases covering the batch-replay
detector (tight burst flagged, human-paced 10-minutes-apart cluster NOT
flagged, below-minimum-size NOT flagged, per-client isolation, priority
override of a confirmed-genuine caller when inside a burst window, and a
plain genuine classification outside any burst). Mutation-verified: deleting
the batch-override branch in `classify()` makes
`test_classify_batch_replay_overrides_pi_extension_caller` fail by name (not
just an aggregate count drop); restoring it passes again.
