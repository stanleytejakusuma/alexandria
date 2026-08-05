# Rubric: skip-log audit labeling (load-bearing omission vs safe skip)

Date: 2026-08-04, merged 2026-08-05. Status: DRAFT — written before any
phase-2 code exists, by design (same doctrine as `SPEC-phase2-eval.md`:
judge before player, rubric before cases). This document is the decision
procedure a human labeler or a second grader follows to label a
`(skipped_chunk, page_claims)` pair. It is the ground-truth-construction
rule for calibrating the Judge-2 coverage grader, playing the role RAGTruth
played for `audit.py`.

**Provenance.** Two independent blind drafts were commissioned from
different model families with the identical brief, specifically so
convergence would be real evidence and disagreement would localize genuine
uncertainty rather than either author's blind spot: `claude-fable-5`
(xhigh reasoning) and `gpt-5.6-sol` (high reasoning, via Codex). This
document merges both. See **Appendix C** for the full cross-validation
record — what converged, what diverged, and what this merge adopted from
which draft. Neither source draft was independently committed before this
merge, so Appendix C (not git history) is the durable record of what each
draft actually said and where they agreed or diverged.

Adversarial-review note: sections marked **UNRESOLVED** are known soft
spots. They are flagged here so review effort goes to breaking them, not
finding them.

---

## 0. Framing and scope

The audit question, verbatim from the spec:

> Does this skipped chunk contain a fact that CONTRADICTS or MATERIALLY
> QUALIFIES a claim actually stated on the page?

- **Yes → `LB` (load-bearing omission).** A real miss.
- **No → `SS` (safe skip).** Every SS label carries a reason code (§2).

Two scoping decisions that shape everything below:

1. **The unit of contradiction/qualification is a (claim, fact) pair, not a
   (page, chunk) pair.** A label of LB is only valid if the labeler can write
   down the specific page claim C, the specific fact F from the skipped chunk,
   and the relation between them. If no such pair can be exhibited, the label
   is SS. This is the rubric's central falsifiability mechanism: an LB label
   without an exhibited (C, F, relation) triple is invalid on its face, and a
   reviewer can attack any LB label by attacking its triple.
2. **This audit measures faithfulness of the page to its stated claims, not
   completeness of the page.** "The page should also have said X" is Judge 2's
   *recall gate* (golden-synthesis load-bearing-fact recall ≥ 90%), not the
   skip audit. Keeping these separate is what stops this rubric's "material"
   from creeping into "interesting" (the main false-positive engine, §3).

**The reader.** "Material" needs an anchor. Alexandria is a personal
knowledge engine; the reader is the vault owner consulting the page to make
decisions. Materiality is judged against *that* reader acting on *this*
page — not a hypothetical maximally-sensitive reader to whom everything
matters.

**page_claims** are what the page *actually states*, citations included —
not what it could have stated. **skipped_chunk** is a single retrieved
chunk the sweep gathered but did not cite. Contradiction/qualification is
evaluated only against stated claims, never against a claim the page never
made (§2c).

---

## 1. Core decision procedure

A skipped chunk is **load-bearing (LB)** iff **both** hold:

| Condition | Operational test |
|---|---|
| **C1 — target existence** | The page makes at least one explicit, citable claim *C* the skipped chunk addresses. No such *C* exists → `SS: no_target_claim` (§2c). |
| **C2 — contradiction or material qualification** | The chunk either **(a) contradicts** *C* (asserts ¬C, or a fact making C false under standard reading), or **(b) materially qualifies** *C* (adds a necessary condition, scope bound, exception, severity modifier, temporal bound, or confidence caveat that changes what a reasonable reader would infer from C standing alone). |

### Step 0 — Atomize

Break the page's claims into atomic, citable propositions C₁…Cₙ, and the
skipped chunk into atomic facts F₁…Fₘ. Do not atomize hedges or discourse
markers into pseudo-facts ("over-atomization" is a named false-positive
mechanism, §3).

### Step 1 — Duplicate screen (fast exit)

For each (Fᵢ, Cⱼ) pair on the same subject: if Fᵢ is entailed by Cⱼ (or by
Cⱼ plus another already-cited chunk) with no added condition, exception, or
severity change, it's `SS: near_duplicate` (§2a) — exit before contradiction
testing.

### Step 2 — Contradiction test (per remaining Fᵢ × Cⱼ)

A fact **contradicts** a claim iff one of:

1. **Direct negation** — asserts the logical negation (`C`: "fix verified
   working"; `F`: "fix failed verification").
2. **Mutual exclusivity** — C and F cannot both be true under the same
   conditions (`C`: "migration completed 6/1"; `F`: "migration blocked by
   unresolved data corruption as of 6/15").
3. **Explicit correction/supersession** — the chunk itself labels C as
   outdated, retracted, or superseded, subject to the documented-supersession
   rule in §2d.

**Not contradiction:** a *prior* failure that C explicitly acknowledges and
resolves ("fix verified working after initial failure on 5/28" is not
contradicted by "initial failure on 5/28"); a fact about a different
component/version/context with no overlapping scope; unresolved time/scope
misread as contradiction (a named false-positive mechanism, §3) — resolve
temporal/scope overlap *before* judging contradiction, don't infer it from
surface negation words alone.

**Mirror case, added after real disagreement in double-labeling (see
Appendix D): C states an earlier problem/blocked/active-defect state, F
reports a LATER resolution.** This is the temporal mirror of the
not-contradiction bullet above, and it does not get the same pass. Rule:

- If C is phrased as an **unanchored, ongoing claim** ("is blocked,"
  "undermines," no date given) and F reports that this was later resolved,
  the omission is **LB `qualification:temporal`** — a reader has no signal
  that C is stale, and F was already gathered.
- If C is **explicitly anchored to a named historical event** ("the 6/24
  audit found X," "as of the bring-up attempt") rather than phrased as the
  current state, whether a later resolution must still surface is
  **UNRESOLVED** — double-labeling produced genuine, defensible disagreement
  on this exact distinction with no rubric text to arbitrate it. Treat as
  `borderline` until adjudicated case-by-case; do not default to either
  label mechanically.

The distinguishing test: read C in isolation, with no other context. Would
a reader reasonably take it as describing the *current* state, or as a
report of what was true *at a stated past moment*? The former is
unanchored; the latter is anchored. This wording-level distinction is doing
real labeling work and was not previously named in this rubric.

### Step 3 — Material-qualification test (per remaining Fᵢ × Cⱼ)

A fact **materially qualifies** a claim iff it is:

- **Necessary** — omitting it leaves C misleading in a way affecting
  decisions or beliefs;
- **Non-redundant** — not already implied by C or another cited chunk;
- **Scope-bound** — applies to the same entity, timeframe, and conditions
  as C.

**Qualification categories (exhaustive — every LB:qualification label must
fit one of these):**

| Category | Example | Why material |
|---|---|---|
| Scope/condition bound | *C*: "Retry logic handles network failures." *F*: "Only for idempotent GET; POST requires manual opt-in." | Reader would assume universal coverage. |
| Exception/carve-out | *C*: "All user data encrypted at rest." *F*: "Except audit logs, stored plaintext per compliance." | Security posture materially different. |
| Severity/risk modifier | *C*: "Regression fixed in v2.1." *F*: "Fix introduced a new memory leak under load." | "Fixed" implies resolved; a new leak changes the risk profile. |
| Temporal bound | *C*: "Team uses monorepo." *F*: "Migrated to polyrepo 2024-Q3; monorepo archived." | Present-tense claim false for current state. |
| Dependency/precondition | *C*: "Feature flag enables dark mode." *F*: "Requires `experiments.dark_mode=true`; default false." | Reader assumes the flag alone suffices. |
| Confidence/evidence grade | *C*: "Root cause identified." *F*: "Hypothesis only; not reproduced in staging." | "Identified" implies certainty; a hypothesis is not confirmation. |

**Not material qualification:** restating C with more detail (elaboration,
not qualification); a caveat C already includes; a lower-salience detail
that doesn't change the actionable takeaway (an exact timestamp when C only
claims "an error occurred"); metadata about the chunk itself (source,
retrieval score) rather than about the claim's truth.

### Step 4 — Default

If neither Step 2 nor Step 3 fires for any (Fᵢ, Cⱼ) pair → `SS`, coded per
§2 (tangential, no_target_claim, or trivial).

### What distinguishes load-bearing from merely related

A chunk can share every keyword with a stated claim and still be SS
(tangential, §2b); a chunk can share none and still be LB (a contradiction
phrased in entirely different vocabulary). The test is never lexical
overlap — it is whether an exhibited (C, F, relation) triple survives Steps
2–3.

---

## 2. Edge cases

Each must be resolved by rule before labeling any real case — the labeler
applies the rule, they do not improvise per-case.

### (a) Near-duplicate of an already-cited fact → `SS: near_duplicate`

**Test:** swap the cited chunk for the skipped one. Does any claim on the
page lose support, or gain a qualification it didn't have? If no — near
duplicate. If yes (the "duplicate" actually carries an exception the cited
version lacked) — it is not a duplicate, re-run Steps 2–3.

Deliberately defined against **page claims**, not chunk-to-chunk textual
similarity: a chunk can be textually near-identical to a cited chunk while
carrying the one qualifying clause the page dropped, and that is still a
miss, not a duplicate.

**Worked example.** *C* (cited): "Retry logic handles transient network
failures for GET requests." Skipped *F1*: "Retry logic covers transient
network failures on GET endpoints" → `SS: near_duplicate` (same claim,
different words). Skipped *F2*: "...with exponential backoff starting at
100ms" → `SS: near_duplicate` (elaboration, not qualification — backoff
params don't change "handles failures"). Skipped *F3*: "...but only if
`idempotency_key` is provided" → `LB: qualification:dependency` (changes
"handles" to "handles, conditionally").

### (b) Topically related but non-conflicting → `SS: tangential`

**Test:** does this chunk answer whether any stated claim is true, false,
or misleading? If it only shares a subject/entity without bearing on any
claim's truth value, it's tangential regardless of topical closeness.

### (c) Contradicts a claim the page never made → `SS: no_target_claim`

**Rule:** if no page claim addresses the same subject as the chunk, the
audit does not fire — this is a *recall* gap (the page should have covered
the topic at all), owned by Judge 2's recall gate and by Judge 3's
gather-completeness for CONTRA-SCAN, not by this audit.

**This is a named, disclosed dependency, not a silent gap** (§6.2): a page
that states fewer claims has fewer chances to be caught contradicting
something, so this audit is sound only *jointly* with the recall gate and
the anti-gutting guard in the repair loop — `no_target_claim` rate is
tracked as telemetry precisely so a page that drops claims to dodge this
audit becomes visible elsewhere, even though it doesn't fail this gate
directly.

**Exception:** if the chunk explicitly self-labels as "draft," "hypothesis,"
"superseded," or "retracted," that self-label is content and is evaluated
under (d)/Step 2 rather than dismissed as no-target-claim.

### (d) Lower-credibility / stale / since-corrected skipped chunk

**Rule: content first, credibility only via documented supersession.**

1. Run Steps 1–3 on content alone. If F doesn't contradict/qualify any C,
   the credibility question never arises.
2. If F *does* conflict with a stated claim, the conflict is discharged —
   `SS: superseded` — iff **both**: (i) the corpus contains an explicit,
   dated supersession of F (a correction, retraction, or "this turned out
   to be wrong" note — not merely a later chunk saying otherwise), and
   (ii) the page states the current, superseding fact.
3. If supersession is merely *inferable* — later timestamp, a sense that
   the cited source is "the better take," a duplicate note from an earlier
   draft with no on-record correction — label **LB**.

**Reasoning.** Allowing graders to weigh credibility freely reintroduces
post-hoc rationalization: any inconvenient skipped fact can be discounted as
"probably the stale one" — precisely the silent source-preference this
judge exists to catch. When two live sources genuinely conflict and nothing
on record resolves it, the correct page behavior is to surface the conflict
(CONTRA-SCAN's contract); omitting one side is load-bearing even if the
grader privately suspects which side is right. The documented-supersession
carve-out keeps the rule from absurdity (a formally retracted claim is not
a live conflict) while keeping the judgment mechanical: check for an
artifact, not for plausibility.

**Cost, stated plainly:** this rule produces some false LBs where a fact was
quietly abandoned without anyone writing down the correction. Accepted; the
fix belongs upstream (write the correction into the corpus), not in a laxer
grader.

**Worked example.** Page claims: "The cron scheduler was the root cause of
the duplicate-run bug." Skipped chunk (three weeks older): "Root cause looks
like the lock file, not cron." A later note "lock-file theory ruled out
5/12, cron confirmed" exists → `SS: superseded`. No such note exists, only
that the cron chunk is newer → `LB`: the page silently chose a side in an
unresolved conflict.

### (e) Materiality threshold

Operationalized as tiers:

- **Tier 1 — contradiction:** always material, no decision test needed.
- **Tier 2 — scope/condition/temporal/severity/dependency/confidence
  qualification (the six categories in §1):** material iff the *decision
  test* passes — a written reader-sentence derived from C that F breaks,
  concerning a decision or the claim's practical reliability. "The
  regression came two days later," "only verified on staging," "works
  except for inputs over 512 tokens" all pass for any page whose reader
  might build on the claim.
- **Tier 3 — trivial caveat:** true, attached to a stated claim, but the
  decision test fails — typo fixes, cosmetic details, restating an
  uncertainty the page already states, precision refinements below the
  page's own granularity ("40%" vs "41.3%") → `SS: trivial`.

**UNRESOLVED — flagged for adversarial review.** The Tier-2/Tier-3 boundary
is the genuinely soft joint of this rubric. The decision test constrains it
(both halves must be written down, so a reviewer can attack a contrived
reader-sentence), but "would this plausibly change a decision" remains
judgment, and inter-labeler disagreement will concentrate exactly here. No
bright line is claimed. Mitigation is procedural, not definitional: every
calibration case in the qualification strata gets **two independent
labels**; disagreements are adjudicated with written rationale; the
disagreement rate is reported alongside grader accuracy; cases where
adjudication itself was contested are kept in the set marked `borderline`
rather than silently dropped or resolved. **If blind double-labeling shows
agreement below ~80% on Tier-2/3 boundary cases, the rubric's materiality
section — not the labelers — is the defect**, and it gets revised before
the grader is calibrated against those labels.

---

## 3. False-positive risk and the tradeoff this rubric makes

**Known over-flagging mechanisms, in predicted order of impact:**

1. **Materiality inflation.** Dropping the "reasonable reader of this page"
   anchor turns the decision test into "could this conceivably matter to
   anyone," under which nearly every true fact is material. The anchor and
   the written reader-sentence are the brakes.
2. **Negative-valence attachment.** Graders over-flag anything bad-sounding
   near a positive claim — "a regression was found [in a different
   subsystem]" reads like it qualifies "the fix works." The triple
   requirement with resolved referents is the brake: the regression must
   attach to *this* claim's subject.
3. **Unresolved time/scope read as contradiction.** Pre-fix failure reports
   "contradicting" post-fix success claims. Step 2's resolve-before-judging
   rule is the brake.
4. **Over-atomization.** Splitting hedges and discourse into pseudo-facts
   that then look like qualifications. Step 0's atomization rule is the
   brake.
5. **Stale chunk misread as live contradiction** because a temporal mismatch
   was missed (chunk says "X broken" in an old note, page says "X works,"
   no date reconciliation) — the same brake as (3), applied to §2d.
6. **§2d's own conservatism** — deliberately strict, will convert some
   quietly-abandoned claims into LBs. Accepted (§2d).

**The tradeoff, named.** Like `audit.py` v2 (which bought Subtle-Conflict
recall 73.3% at the cost of clean-FP rising 20% → 32.5%), this rubric leans
recall: a false LB costs one human review of one skip; a missed LB is the
Goodhart gap itself — the failure mode with no other detector. **Calibration
target: expect a clean-material false-positive rate in the same ~30%
neighborhood as `audit.py` v2's measured 32.5%**, provided LB recall on the
hard (qualification/subtle-contradiction) strata clears roughly 70%+ — this
number is a prediction to check the eventual calibration run against, not a
target to engineer toward.

The recall-leaning choices are specifically: the page-claims-based (not
chunk-similarity) duplicate test in §2a, and the documented-supersession-
only rule in §2d. Unlike v2 (which bought recall via raw prompt
*strictness*, which raised vibes-based FPs across the board), this rubric
buys recall via *scope* rules while spending its FP budget through a
mechanical brake — the exhibited (C, F, relation) triple — that specifically
filters the vibes-based flags (mechanisms 1–2 above) that dominate FP risk
in a stricter-prompt approach. **Prediction to verify at calibration time:**
FPs will concentrate in the Tier-2/3 boundary stratum and the §2d
no-documented-supersession cases; both get dedicated FP measurement (§5).

---

## 4. The bootstrap problem: verdict on the simulation proposal

**Proposal under evaluation:** with no phase-2 code and hence no real
skip-logs, build calibration cases from `synthesis-clusters-v1.jsonl` by
taking a subset of a cluster's verified load-bearing facts as "page claims"
and treating the remainder as "skipped chunks," labeling from verified
knowledge of the fact list.

**Verdict: sound at its core, with three real flaws — two closable now, one
only closable after phase-2 exists. Use it, with the amendments below, and
disclose the residual.**

**Why the core is sound.** The label function depends only on the
`(skipped_chunk, page_claims)` pair — nothing in §1–2 references how the
skip happened. A grader that answers the pair-question correctly on
simulated pairs answers it correctly *for those pairs*, regardless of
origin. Simulation is a legitimate way to manufacture ground truth for this
question. The problem is not the validity of individual cases; it is the
**distribution** of cases the raw proposal would generate.

**Flaw 1 — difficulty/negative-class bias.** The clusters file was authored
to enumerate load-bearing facts exhaustively. Held-out facts from it are
therefore *certified-important* facts — the LB cases this generates are
disproportionately clean and obvious. Worse, the raw method generates almost
**no realistic SS cases at all**: no near-duplicates with drift, no
tangential-but-topical chunks, no trivial caveats — because the file
contains only load-bearing facts by construction. A grader calibrated on
this raw distribution gets an inflated accuracy number and, critically, an
**unmeasured FP rate** — exactly the metric the v2 experience says matters
most, and exactly the "eval that cannot fail correctly" failure mode the
project doctrine warns against.

*Closure:* deliberately construct the negative strata (§5): paraphrase-
duplicates written from cited facts; tangential chunks lifted from
*adjacent* clusters in the same corpus; stale variants with and without
documented corrections; trivial-caveat edits of real facts. Also construct
**adversarial** near-miss SS cases per cluster — chunks that share
entities/keywords with stated claims, use qualification-shaped language
("however," "except," "only," "but"), and are plausible as real-retriever
output, but are *actually* safe by the rubric. And construct **borderline**
LB cases by weakening real qualifiers (move the regression to a
neighboring subsystem; soften "regression found" to "possible regression
suspected") so the set contains cases hard enough to be genuinely
diagnostic — tagged `borderline` and double-labeled per §2e.

**Flaw 2 — the page-claims side is unrealistically clean.** Real phase-2
pages will be LLM prose: merged claims, hedges, paraphrase drift. Hand-picked
atomic facts as "stated claims" test the grader against inputs cleaner than
production. *Closure, cheap and available now:* generate the simulated
pages by actually prompting an LLM to write a short cited page from the
chosen fact subset (this is calibration infrastructure, real-LLM,
`scripts/`-tier per existing doctrine — the offline `ScriptedClient`
pattern doesn't apply here), then verify the generated page states exactly
the intended claim subset before using it as a case. This keeps the label
ground-truthed while making the claims side realistic.

**Flaw 3 — label circularity.** The authors of the fact list also assign
the labels, and "load-bearing" was decided at authoring time — for the
*topic*, not for any particular constructed page. A fact globally load-
bearing for its cluster is **not automatically LB relative to a specific
simulated page**: if the chosen claim subset never states the claim that
fact would qualify, the correct label is `SS: no_target_claim` (§2c), not
an automatic LB inherited from the fact's place on the original list.
*Closure:* every case is labeled by applying §1–2 to the pair **as
constructed**, blind to the fact's original provenance where feasible, with
double-labeling on the strata named in §5; ideally the second labeler is
from a different model family than the one that authored the case, or is
the human.

**Residual gap, not closable now (disclosed):** even with constructed
negatives, the simulated skip distribution is a *guess* at what a real
sweep skips, and constructed negatives may carry stylistic tells. The
calibration number this set produces is therefore **provisional**.

**Plan: two-stage calibration.** Stage 1, on this simulated set, gates
initial grader development. Stage 2 re-labels a sample of the first real
skip-logs from phase-2 shadow runs (sweep running, gates not yet enforcing)
against this same rubric and re-measures before the audit gate becomes
blocking. The rubric itself is stage-invariant; only the case source
changes. **Every stage-1 report carries an explicit "PROVISIONAL —
SIMULATED CALIBRATION" header; stage-1 numbers must never be quoted as "the
grader's accuracy" without that qualifier.**

---

## 5. Stratification plan

Design constraints: the set is entirely hand-built (realistic ceiling
~80–120 cases at this project's verification standard), and RAGTruth's
lesson applies directly — its rarest real category (Subtle Conflict) had
n=15 in a 2,675-item population, and per-category numbers at that n are
directional, not precise. This set will not overclaim what n≈10–15 can
support.

**Strata (assigned by construction — we control the generator, so strata
are assigned, not sampled from a natural distribution):**

| # | Stratum | True label | Target n | Why this n |
|---|---|---|---|---|
| 1 | Direct contradiction | LB | 10 | Expected easy (v2 analog: Evident Conflict 98.3%); small n acceptable |
| 2 | Qualification — temporal / subsequent development | LB | 15 | The headline Goodhart case; hard joint |
| 3 | Qualification — scope/condition/exception | LB | 15 | Hard joint, second flavor |
| 4 | Borderline qualification (deliberately weakened, tagged `borderline`) | adjudicated | 10 | Measures behavior at the Tier-2/3 boundary; double-labeled |
| 5 | Near-duplicate (incl. drifted paraphrase) | SS | 12 | FP measurement — §2a's swap test under stress |
| 6 | Tangential, same corpus, adjacent cluster | SS | 12 | FP measurement — mechanism 2 of §3 |
| 7 | Stale/superseded — WITH documented correction | SS | 8 | §2d discharge path |
| 8 | Stale/conflicting — WITHOUT documented correction | LB | 8 | §2d conservative path; FPs predicted here, measure it |
| 9 | Trivial caveat | SS | 8 | Tier-3 FP measurement |
| 10 | No-target-claim (contradicts an unstated claim) | SS | 6 | Verifies scope discipline + `no_target_claim` telemetry |

Total ≈ 104. Positive/negative ≈ 55/49 — deliberately near-balanced so the
FP rate is measured on a real denominator, not an afterthought.

**Reporting rules (the no-overclaiming part):**

1. **Gate on pooled metrics only:** overall LB-recall and overall SS-FP-rate
   across all strata. Per-stratum numbers are reported as **raw counts with
   Wilson 95% intervals**, never bare percentages — at n=10, 9/10 correct is
   a Wilson interval of roughly [60%, 98%], and the report says so.
2. Per-stratum results are **red-flag detectors, not estimates**: any
   stratum at or below ~6/10 triggers investigation regardless of pooled
   numbers, but no stratum-level precision claim is made either way.
3. FP rate is additionally broken out across the SS strata (5, 6, 7, 9, 10)
   individually — the v2 lesson is that FPs concentrate in specific clean
   subtypes, and a pooled FP number would hide exactly that.
4. Strata 2–4 and 8 (predicted-hard) get 100% double-labeling with
   disagreement rate reported; easy strata get 30% double-label spot-checks.
5. The calibration report states in its header that the set is simulated
   (§4), that stage-2 recalibration on real skip-logs is pending, and which
   strata are too small for their numbers to travel.

**Why not mirror expected production frequencies?** Because they aren't
known yet (no phase-2 exists), and calibration wants power where errors are
costly, not where cases happen to be common. Production frequency data
arrives with stage-2 shadow-run recalibration; if real skip-logs show a
skip class this table lacks, that class gets added then.

---

## 6. Summary of honest limitations

1. The Tier-2/3 materiality boundary is judgment-laden; managed procedurally
   (double-label, adjudicate, report agreement, revise the rubric if <~80%),
   not solved definitionally (§2e).
2. The audit cannot punish strategic silence (fewer claims → fewer attack
   surfaces); it is sound only jointly with the recall gate and the
   anti-gutting guard; `no_target_claim` rate is the telemetry (§2c).
3. §2d will mislabel a quietly-abandoned claim as a live conflict when
   nobody wrote the correction down; accepted recall-leaning cost (§2d, §3).
4. Stage-1 calibration is on simulated cases whose negatives are synthetic
   and may carry stylistic tells; numbers are provisional until stage-2
   shadow-run recalibration (§4).
5. Small-n strata support red-flag detection, not precision claims;
   reported with Wilson intervals and said so out loud (§5).

---

## Appendix A: Label taxonomy (canonical)

| Code | Meaning | Parent | Rubric ref |
|---|---|---|---|
| `LB:contradiction:direct` | Direct negation of a stated claim | LB | §1 Step 2.1 |
| `LB:contradiction:mutual_exclusive` | Mutually exclusive fact | LB | §1 Step 2.2 |
| `LB:contradiction:superseded` | Chunk explicitly supersedes an undischarged claim | LB | §2d |
| `LB:qualification:scope` | Scope/condition bound | LB | §1 Step 3 |
| `LB:qualification:exception` | Exception/carve-out | LB | §1 Step 3 |
| `LB:qualification:severity` | Severity/risk modifier | LB | §1 Step 3 |
| `LB:qualification:temporal` | Temporal bound | LB | §1 Step 3 |
| `LB:qualification:dependency` | Dependency/precondition | LB | §1 Step 3 |
| `LB:qualification:confidence` | Confidence/evidence-grade caveat | LB | §1 Step 3 |
| `SS:near_duplicate` | Near-duplicate of a cited fact (swap test) | SS | §2a |
| `SS:tangential` | Topically related, no bearing on any claim's truth | SS | §2b |
| `SS:no_target_claim` | Addresses no stated claim | SS | §2c |
| `SS:superseded` | Conflict discharged by a documented correction | SS | §2d |
| `SS:trivial` | True, attached, but fails the decision test | SS | §2e Tier 3 |

---

## Appendix B: Worked examples index

Full worked examples appear inline at: §2a (near-duplicate, retry-logic
example), §2d (superseded, cron-vs-lock-file example), §1 Step 3
(qualification categories table, six examples). Additional illustrative
(not corpus-real) examples for calibration-case authors:

| Page claim (cited) | Skipped chunk | Label |
|---|---|---|
| "Fix verified working." | "Fix verified, but memory leak under load." | `LB:qualification:severity` — may delay deploy, add monitoring |
| "All tests pass." | "All tests pass, but only on Linux." | `LB:qualification:scope` — blocks Windows release |
| "API stable." | "API stable, but v2 deprecated next quarter." | `LB:qualification:temporal` — affects integration planning |
| "Migration done." | "Migration done, but 3 edge cases need manual cleanup." | `LB:qualification:dependency` — changes "done" to "mostly done + follow-up" |
| "Export completes." | "Export fails silently if `tmp` disk <5GB free." | `LB:qualification:scope` — changes "completes" to "completes iff disk space" |
| "Fix verified working [on 6/1]." | "Fix verified working [on 6/1, after 3 retries]." | `SS:near_duplicate` — extra detail is elaboration, not qualification |

---

## Appendix C: Cross-validation record

Two independent, blind drafts were produced from the identical brief before
any merge — `claude-fable-5` (xhigh) and `gpt-5.6-sol` (high, via Codex),
each with no visibility into the other. This table is the record of where
they agreed (treated as strong signal the design is right, not an artifact
of one model's idiosyncrasy) and where they diverged (treated as genuine
uncertainty or a gap one model closed that the other missed), and what this
merged document adopted as a result.

| Dimension | Fable (`claude-fable-5`) | Sol (`gpt-5.6-sol`) | Verdict | Adopted into this document |
|---|---|---|---|---|
| Core two-part test (target existence, then contradiction/qualification) | Steps 0–4 procedure | §1.1 falsifiable C1/C2 criteria, decision tree | **Converged independently** | Both — merged as §1, Sol's C1/C2 framing + Fable's step structure |
| No-target-claim scoping (contradicts an unstated claim = out of scope) | §2c, tied to recall gate + anti-gutting guard | §2c, same reasoning, same code name | **Converged independently, same code name** | Fable's fuller telemetry/dependency framing, §2c |
| Near-duplicate test | Swap-test against page claims | Same swap-test framing | **Converged independently** | Merged, §2a |
| Tangential test | "Does it bear on any claim's truth" | Same framing | **Converged independently** | Merged, §2b |
| Materiality operationalization | "Decision test" (reader-sentence) | "Decision Flip Criterion" | **Converged independently — different names, identical mechanism** | Fable's naming + Sol's exhaustive 6-category qualification table, §1 Step 3 / §2e |
| Materiality boundary is unresolved, mitigated by double-label + adjudicate + ~80% agreement threshold | Explicit, same threshold | Explicit, same threshold | **Converged independently on the exact number** | Verbatim, §2e |
| Source-credibility handling (content-only, documented-supersession carve-out, reject inferred staleness) | §2d, full reasoning + named cost | §1.2 exception clause, same rule | **Converged independently on a non-obvious call** | Fable's fuller reasoning + worked example, §2d |
| False-positive tradeoff vs `audit.py` v2 | Named tradeoff, mechanism-level FP prediction (where FPs concentrate) | Named tradeoff + explicit numeric target (~30%) | **Converged on direction; Sol added a number** | Both — mechanism prediction + numeric target, §3 |
| Bootstrap verdict: sound core, but raw simulation has no realistic negatives | Flaw 1, same diagnosis | §4.2 "Obvious Load-Bearing Bias," same diagnosis | **Converged independently, same root cause identified** | Merged, §4 Flaw 1 |
| Bootstrap fix: construct adversarial/negative strata deliberately | Closure for Flaw 1 | §4.3 "Adversarial SS Injection" | **Converged independently** | Merged, §4 Flaw 1 closure |
| Page-claims side is unrealistically clean vs real LLM prose (Flaw 2) | Named explicitly, LLM-generation closure proposed | Not identified as a distinct flaw in reviewed sections | **Fable-only addition** | Adopted as-is, §4 Flaw 2 |
| Label circularity — cluster-level "load-bearing" ≠ automatically LB for a specific constructed page (Flaw 3) | Named explicitly | Not identified as a distinct flaw in reviewed sections | **Fable-only addition** | Adopted as-is, §4 Flaw 3 |
| Two-stage calibration plan (stage-1 simulated, provisional; stage-2 shadow-run recalibration before gate enforces) | Explicit, with a "PROVISIONAL" labeling discipline | Not present as a formal staged plan in reviewed sections | **Fable-only addition** | Adopted as-is, §4 close + §6.4 |
| Stratification structure (assign, don't sample; weight toward hard/adversarial cases; Wilson intervals; no per-stratum precision claims) | 10 strata, ~104 cases, detailed rationale per row | Present with a similar table (partial visibility during review) | **Converged on structure** | Fable's fuller table, §5 |
| Label taxonomy | Codes used inline, no consolidated table | Explicit "Appendix B: Label Taxonomy (Canonical)" | **Sol structured it more explicitly** | Sol's table format, rebuilt as Appendix A here with rubric cross-references added |

**Net read:** the core design — decision procedure, all five edge cases,
the materiality test, the credibility rule, and the bootstrap-problem
diagnosis — converged independently between two different model families
with zero shared context. That is meaningfully stronger evidence the design
is sound than either draft alone, especially on the non-obvious calls (the
content-only credibility rule; the exact 80% agreement threshold). The two
gaps Fable alone caught (Flaw 2, Flaw 3) are additive fixes, not
disagreements — nothing in this record required adjudicating a genuine
conflict between the two drafts.

---

## Appendix D: double-labeling adjudication record (v1 calibration set)

Per §5's requirement, the hard strata (2, 3, 4, 8 — 22 cases) were
double-labeled: the original construction (`claude-fable-5`, applying the
rubric to its own constructed pairs) versus a fully blind independent pass
by `gpt-5.6-sol` (given only `page_claims` + `skipped_chunk`, no labels, no
visibility into the original reasoning). Raw agreement: **10/22 (45%)** —
well below the ~80% threshold §2e sets as the signal the rubric itself, not
the labelers, may be the defect. Every disagreement was individually
re-checked against the real source corpus before any resolution — the same
discipline applied throughout this project's ground-truth work. The low raw
agreement number is not a failure signal on its own; what it produced on
inspection is the real result.

Case descriptions below are anonymized (this document is public; the real
case ids with full content live only in the private calibration-set commit).

| Case | Original | Sol (blind) | Adjudication | Resolution |
|---|---|---|---|---|
| An infra-component status claim, phrased as ongoing ("is operational"), later reported permanently retired | LB | SS | Unanchored present tense later contradicted by a permanent-status change — Sol's "no forward permanence claim" reasoning doesn't apply to unanchored claims | **Kept LB** |
| A blocked-service-bringup claim, later reported successfully started | LB | SS | Same pattern: unanchored "is blocked" later resolved | **Kept LB** |
| An active-defect claim ("undermines..."), later reported fixed and verified | LB | SS | Same pattern: unanchored active-problem claim later resolved | **Kept LB** |
| A secret-key-exposure audit finding, phrased as a dated historical event ("the audit found X on [date]") | LB | SS | Claim is explicitly anchored to a named historical event, not phrased as current state — genuinely unresolved by the rubric as it stood | **Moved to borderline (stratum 4)**; motivated the new Step 2 mirror-case rule |
| An infra-isolation rationale vs. a later, broader migration decision | LB | SS | Sol correct: the two facts are complementary, not qualifying — a real construction flaw (sequential cluster facts assumed to qualify without checking) | **Relabeled SS:tangential**, moved to stratum 6 |
| A distribution-mechanism description vs. the legal rationale for one of its properties | LB | SS | Sol correct: mechanism description and its legal rationale don't stand in a qualifying relationship | **Relabeled SS:tangential**, moved to stratum 6 |
| An accepted-risk verdict vs. its stated future re-escalation triggers | LB | SS | Sol correct, and confirmed by re-checking the real source cluster: the original relation claimed a specific trigger was implicated, but the real fact directly contradicts that premise. A genuine factual error in construction, not an interpretive disagreement | **Relabeled SS:tangential**, moved to stratum 6 |
| A "sole distribution path" claim vs. preserved-but-inactive peer copies | LB | SS | Redundant with an already-existing borderline twin built for the same real fact pair; Sol's SS lean adds weight against a confident label | **Dropped** (kept only the borderline twin) |
| A same-day reversed harness-migration decision, two conflicting "final posture" records | LB | SS | "Chose not to migrate" is genuinely ambiguous between full rejection and rejecting only exclusive migration — both readings defensible | **Moved to borderline (stratum 4)** |
| A drawdown-breaker restart-reset limitation vs. a narrower bug fix | borderline | SS | Already reclassified to borderline pre-double-labeling (fix-scope ambiguity found by re-reading the source code excerpt); Sol's independent SS lean is consistent with genuine uncertainty, not a new problem | No change — borderline is working as intended |
| "Sole distribution path" twin (hand-built borderline case) | borderline | SS | Sol's SS lean is exactly the kind of independent second opinion this stratum exists to collect | No change — this *is* the adjudication data point |
| Accepted-risk / re-escalation-triggers twin (hand-built borderline case) | borderline | SS | Same as above | No change |

**Net read.** Of 12 disagreements: 3 were the original construction correctly
resisting an over-broad reading Sol applied uniformly across temporal cases
(unanchored present tense really does need updating); 4 were real
construction errors Sol correctly caught (including one confirmed factual
error, not just an interpretive call); 5 were genuine, defensible ambiguity
correctly routed to the borderline stratum rather than forced either way.
**No disagreement was resolved by authority — every one was checked against
either the real source corpus or a re-derived reading of the rubric's own
logic.** The double-labeling process did exactly what §5 designed it to do:
it surfaced errors in both directions and one real rubric gap (the
anchored-vs-unanchored temporal distinction, now Step 2's mirror-case rule),
rather than rubber-stamping either labeler.

Post-adjudication set: 71/104 cases (see `coverage-calibration-v1.jsonl`
commit history for the full count-by-stratum). Strata 2, 3, and 8 shrank as
weak constructions were removed; stratum 4 grew as genuine ambiguity was
correctly identified rather than suppressed. This is the intended dynamic —
a stratification plan's job is to find true ambiguity, not to hit a target
count.
