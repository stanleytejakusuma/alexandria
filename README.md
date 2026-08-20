# Alexandria

**A self-improving memory/RAG engine that synthesizes instead of transcribes — every claim carries a citation back to its source, and the engine learns from usage: your queries, corrections, and distilled sessions drive weekly memory generation and on-demand knowledge re-synthesis (docs/pi-self-learning-loop.md).**

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![Status: pre-alpha](https://img.shields.io/badge/status-pre--alpha-orange.svg)](#status--roadmap)

Alexandria turns the exhaust of your AI coding sessions, notes, chats and documents into a **maintained, cited knowledge base** — then serves it back to any LLM harness through one retrieval API.

It is built on a specific claim: **synthesis without provenance is not knowledge, it's a nicer-looking guess.**

Plenty of systems compress. The serious agent-memory projects (Mem0, Zep/Graphiti, Letta) already do write-time consolidation, and some do real supersession. What is rare is synthesis into an artifact a *human can audit*: a plain-text, diffable, revertable page where **every individual claim names the source it came from**, and a linter fails the build when one doesn't.

The hobby tier is worse — it logs everything and calls it memory, producing a log wearing a knowledge base's schema. No amount of better vector search fixes that, because the synthesis was never written at all. Alexandria targets both gaps: it synthesizes, and it makes the synthesis checkable.

```
raw sources  ──▶  connectors  ──▶  sources/          ──▶  nightly    ──▶  wiki/           ──▶  retrieval API
(sessions,        (1 adapter      (immutable,             synthesis       (maintained          (any LLM client,
 notes, chats,     per source,     append-only,           N:1             pages, every         Obsidian, humans)
 PDFs, tasks)      idempotent)     git-versioned)                         claim cited ────────▶ citations resolve
                                                                                                back into sources/
```

---

## Why is this made?

### The problem every AI-assisted developer hits

You use Claude Code, Cursor, Copilot, Codex, or a home-grown agent every day. Within a few months:

- **Every session starts from zero.** You re-explain the same architecture, the same conventions, the same three things that already went wrong — because the agent has no durable memory of the last four months of your own decisions.
- **Your "memory" tool is a landfill.** Session hooks dutifully log every conversation. Twenty thousand notes later, a search for one decision returns ten near-duplicate fragments of it, recorded weeks apart, half of them since reversed. Nothing says which one is current.
- **Nothing is ever superseded.** Systems ship `supersedes` fields that never get written, because nothing owns the job of deciding a note is obsolete. Contradictions accumulate in silence — and the retrieval layer surfaces the loudest match, not the true one.
- **Durability isn't knowable at write time.** Metadata written the moment a note is created cannot say whether it will matter in six months. Only later evidence can. So write-time importance scores are noise.
- **Bolting RAG on top doesn't help.** Better embeddings on an unsynthesized corpus return better-ranked fragments of the same mess.

The honest version: this project began because a personal knowledge vault reached **21,000 notes, 97% marked "active", `supersedes` written exactly zero times, and an Obsidian graph that crashed on index.** Every note was faithfully captured. Nothing was ever *understood*. That's not a retrieval problem — it's a missing stage.

### The same problem, at enterprise scale

The pattern scales up ugly:

- Knowledge is scattered across chat, wikis, tickets, code, transcripts, and email — each with its own vocabulary for the same entity.
- The answer to a question usually exists, in **four partial versions across three systems**, and the most recent isn't necessarily the correct one.
- Onboarding is "read six months of scrollback."
- Cross-domain questions ("why does billing retry differently from provisioning?") span teams that never share a document.
- Regulated environments must show **where an answer came from** — a plausible generated paragraph with no provenance is worse than no answer.

### The root cause both share

Conventional RAG *does* synthesize — it just does it **at query time**: under latency pressure, from whatever chunks the retriever happened to surface, with no review, and it throws the result away after every answer. The same synthesis work is redone forever, differently each time, and nobody can audit any of it.

**Alexandria moves synthesis to write time and keeps the receipt.**

A scheduled pass compresses many raw notes into few maintained pages, N:1. Every claim on those pages footnotes the source it came from. The result is a diff you can review, a file you can revert, and a citation you can follow back to the raw transcript. Hallucination stops being an invisible per-query event and becomes **a visible, correctable line in a file**.

---

## What's actually different

| | Conventional RAG / agent memory | Alexandria |
|---|---|---|
| When synthesis happens | Query time, every time, discarded | **Write time, once, kept** |
| Reviewable | No — the assembly is ephemeral | **Yes — it's a git diff** |
| Provenance | "Here are 5 chunks, trust me" | **Every claim footnotes a source id; lint fails otherwise** |
| Contradictions | Retrieved side-by-side, silently | **Adjudicated; loser marked superseded** |
| Corpus growth | Unbounded, quality decays | Sources unbounded; **map stays ~hundreds of pages** |
| Stale facts | Ranked by similarity, forever | **Absolute-date staleness + supersession** |
| Trust signals | Written at capture time (unknowable) | **Derived at read time from actual review events** |
| Failure mode | Confident wrong answer, no trace | Wrong *page*, visible in review, one `git revert` away |

---

## How it works

**Two layers, one hard rule.**

| | `sources/` — the territory | `wiki/` — the map |
|---|---|---|
| Mutability | **Immutable, append-only.** Never hand-edited. Supersession, not mutation | **Rewritable by design.** Git is the history |
| Written by | Connectors (machines) only | The synthesis pass (LLM), git-committed |
| Unit | One observation per file | One maintained page per entity / concept / decision |
| Scale | Unbounded — grows with activity | Hundreds — grows with *entities*, not sessions |
| Quality bar | **Faithful + structured. Nothing more.** No durability judgment here | **Every claim cited** — lint rejects uncited claims |

**The hard rule:** a claim in `wiki/` without a resolvable citation into `sources/` is a lint **error**. The map is never more than one hop from the territory.

Everything is markdown with YAML frontmatter — readable in Obsidian, greppable, diffable, and yours. The index is derived state and can be deleted and rebuilt at any time.

### Pipeline

1. **Connectors** pull from each upstream (agent session transcripts, notes, chat archives, tasks, PDFs), normalize to one schema, and write immutable source notes. Idempotent: re-running is a no-op; a changed upstream item creates a *new* note superseding the old.
2. **Synthesis (scheduled)** clusters uncited notes by entity, detects claims that contradict existing pages, rewrites affected wiki pages N:1, and writes `supersedes` on what lost. Output lands on a review branch until its faithfulness audit passes.
3. **Retrieval** serves both layers: hybrid BM25 + dense vector search, metadata filtering as the first gate, cross-encoder reranking, and two tiers — `map` (fast, wiki-first, cited) and `ground` (citations expanded to verbatim sources, for high-stakes questions).
4. **Lint** enforces the invariants: uncited claims, dangling citations, schema violations, and source-body mutation (an immutability tripwire on the content hash).

### Design rules that keep it honest

- **The field law:** every stored field must name its writer and its reader, or it doesn't ship. (A `supersedes` field with no writer sat unused for 14 months. Never again.)
- **Derived, never stored:** backlinks, similarity neighbors, trust tiers, orphan status — all computed at read time. Anything stored is something that can go stale.
- **No synthesis in session hooks.** Capture is cheap and synchronous; synthesis is expensive and scheduled. Conflating them is what produces 21,000 unread notes.
- **Retrieval-only API.** Consumers read. Only connectors write. No agent gets to "remember" something directly into the corpus.
- **Local by default.** Embeddings run on your machine; corpus text is never sent anywhere for indexing. No telemetry, no phone-home.

---

## Status & roadmap

**Pre-alpha — phase 2, building in public.** The design is complete and adversarially reviewed; the phase-0 corpus scaffold and phase-1 retrieval stack are built and gate-passing; phase-2's synthesis core is built and under measurement. Stars and issues welcome; production use is not yet advisable.

| Phase | Deliverable | Gate (evidence, not vibes) |
|---|---|---|
| **0** | Corpus scaffold · schema validator · migration · session connector | Counts reconcile exactly · schema lint clean · 50-note body-identical spot check |
| **1** | Chunker · embeddings · LanceDB · hybrid + rerank · search API | **Golden query set passes** · full rebuild < 30 min target — [re-measurement against the current corpus pending, last known figure ~80 min unsourced](SECURITY.md#recovery-time-objective-rto) · p50 < 500 ms |
| **2** | Synthesis sweep · adjudication · full lint · document ingest | Faithfulness ≥ 95% · **entailment audit ≥ 95%, zero contradicted** · **golden fact recall ≥ 90% (dual-grader)** · 2 weeks of clean reviewed diffs |
| **3** | Harness extensions (Pi, others) | **Blinded side-by-side** vs the incumbent memory tool — must win recall@5 before anything switches |
| **4** | Answer endpoint · static wiki site · demo corpus · docs | **Fresh-clone test:** end-to-end on demo data, zero private content in history |

Every gate is a measurement, not an opinion. Phases don't advance because they feel done.

**Phase-2 status (2026-08-07):** the single-page synthesis core, the
dual-grader fact-recall evaluator, and the full-sweep orchestrator are all
merged, with clustering (dedup + topic) and an immutable run manifest in
place. **Phase-2 gate (2026-08-08): a documented waiver stands; the v4
measurement converged and the loop is declared stopped.** v3 is the
certified evidence (frozen full set **34/40 = 85%**, stable-topic stratum
**34/35 = 97.1%**, Red ratified — `docs/pi-red-verdict-2026-08-07.md`).
v4 (round-4 fixes: clause-targeted repair + temporal-layering directive)
measured **28/40 = 70% FINAL_FAIL**: it closed the flagged classes where it
touched them (temporal-layering cluster 0.80 → 1.00; the 7-fact cluster a
clean 7/7 consensus) but could not emit two historically hard clusters
(single-claim entailment each) and the writer omitted two gathered facts.
Per the convergence stop-rule and Stanley's "no retry-until-success", no
v5: the compound/reversal class is declared a documented product-scope
limitation (`docs/pi-loop-termination.md` Backlog), the gate stays
as-waived, and the failure evidence is preserved in the private corpus.
**Phase-3 status (2026-08-08): one valid verdict, FAIL — Alexandria
stays read-only; contest is now quarterly telemetry.** Cycle-2 run 1 (the
first VALID verdict: disagreement 0.30 ≤ 0.40 cap, 12 queries adjudicated
by gpt-5.6-sol) measured recall@5 **0.521 vs 0.479** — statistical dead
heat (CIs overlap), floor unmet again (**0.52 < 0.60**; floor has never
cleared across all three runs: 0.51/0.53/0.52). Per stratum: stable 0.44
(n=27, the incumbent's surface advantage), reversal 0.54 (n=7, up from
0.38), operational 0.25 (n=4, up from 0.00) — the v4 wiki + ops docs
moved the bottom strata but the n's are noise-adjacent. Cycle-1 runs were
INVALID (disagreement 0.50 / 0.25 > 0.20 cap, discarded per SPEC §3). Per
the signed loop-termination contract no further runs this cycle; the
contest becomes standing quarterly telemetry under frozen mechanics
(docs/pi-contest-cycle2-amendment.md), and Alexandria's improvement loop
is now usage-driven (docs/pi-self-learning-loop.md). Alexandria stays
opt-in/read-only, the Pi extension remains read-only, and the contest becomes
standing quarterly telemetry under the same frozen mechanics (cycle-2 run 1
verdict FAIL is the telemetry baseline, private corpus
`cycle2-run1-DISPOSITION.md`). **Activation
decision 2026-08-08 (principal, recorded in
`docs/pi-activation-decision-2026-08-08.md`): the Pi extension is LIVE in
read-only mode** (`alexandria-search` + `alexandria-answer`, installed at
`~/.pi/agent/extensions/alexandria.ts` as a private copy with machine-local
config) while certification continues via the telemetry loop; any
write-capable surface (ingestion connectors, sync, distil) stays gated on a
valid PASS with no override without a new recorded decision. Full records
in the private corpus. Per-cluster phase-2 details:
[`docs/calibration/synthesis-fact-recall-v3-gate-summary.md`](docs/calibration/synthesis-fact-recall-v3-gate-summary.md)
and
[`docs/calibration/synthesis-fact-recall-v3-summary.md`](docs/calibration/synthesis-fact-recall-v3-summary.md)
(anonymized); full reports with evidence and adjudication reasons live in
the private corpus.
[`docs/calibration/synthesis-fact-recall-v3-gate-summary.md`](docs/calibration/synthesis-fact-recall-v3-gate-summary.md)
and
[`docs/calibration/synthesis-fact-recall-v3-summary.md`](docs/calibration/synthesis-fact-recall-v3-summary.md)
(anonymized); full reports with evidence and adjudication reasons live in
the private corpus.

The phase 3 → 4 boundary also carries a standing checkpoint: whether
Alexandria adopts graph-structured retrieval, formally re-examined against
named re-entry triggers rather than left ambient — see
[`docs/DECISIONS-graph-vs-vector.md`](docs/DECISIONS-graph-vs-vector.md).

---

## FAQ

**Is this just another RAG library?**
No. RAG is one component (phase 1). The differentiator is the *write-time synthesis layer* that produces a maintained, cited corpus for retrieval to run against. Point conventional RAG at an unsynthesized pile and you get well-ranked fragments of a mess.

**Is this a vector database?**
No. It uses one (LanceDB) as derived, rebuildable state. The durable artifact is markdown in git.

**How is this different from a "second brain" / Obsidian / Notion?**
Those are places for *you* to write. Alexandria is machinery that maintains the knowledge base *for* you from sources you already generate — and its output is a normal Obsidian-readable vault, so you can use both.

**How is this different from agent memory tools (Mem0, Zep/Graphiti, Letta, MCP memory servers)?**
Credit where due: those do consolidate at write time, and Zep does genuine temporal invalidation — the "nobody synthesizes" line you'll hear is simply false. Two differences remain. First, **auditability**: their consolidated memory lives in a database as opaque rows, whereas Alexandria's is markdown in git — you can read the whole thing, diff what last night's run changed, and `git revert` a bad synthesis. Second, **enforced per-claim provenance**: a lint ERROR fires on any wiki claim without a resolvable citation, so "why does it believe this?" always has an answer. It also treats an agent's own session transcripts as a first-class source to distill, not merely a place to write memories.

**Does it stop hallucination?**
No system does. It changes hallucination from invisible and recurring into visible and correctable: a wrong claim becomes a reviewable line in a diffable file with a citation you can check, instead of a fresh unreviewable paragraph on every query.

**Will it work with my agent / harness?**
Anything that can call an HTTP endpoint. The API is deliberately thin and read-only.

**Does my data leave my machine?**
Embeddings are local by default. Synthesis calls whatever OpenAI-compatible endpoint you configure — including a local model. The engine makes no other network calls.

**Why markdown and git instead of a database?**
Because the corpus should outlive the engine. Markdown in git is greppable, diffable, revertable, and readable by tools that don't exist yet.

---

## Prior art

Alexandria is a synthesis of ideas that deserve credit:

- **Andrej Karpathy's "LLM wiki" pattern** — the immutable-sources / LLM-maintained-wiki / lint-loop shape, and the observation that a wiki is a persistent compounding artifact rather than a chat log.
- **The Open Knowledge Format (OKF)** — field vocabulary, keyed footnote citations, derived (not stored) trust tiers, and absolute-date staleness. Alexandria adds the schema validator OKF doesn't ship, plus supersession, which OKF lacks.
- **Production RAG practice** — hybrid retrieval, metadata filtering as a first gate, and cross-encoder reranking as the highest-ROI component.

Where this diverges from all three: **the synthesis stage is the product**, and every claim it writes must cite.

---

## License

Apache-2.0. The engine is open source; your corpus is yours and stays on your machine.
