# Spec: Compute/storage split — the compute host builds, the NAS serves

**Status:** draft for review (not implemented, nothing committed)
**Date:** 2026-08-24
**Supersedes:** the implicit "every host runs the full weekly loop" deployment model
**Traces to:** BACKLOG #47/#48 (leg-ablation), #30 (write-path/releases), the 2026-08-24
loop-timeout incident, and the operator directive: *no sustained heavy computation on the
NAS — be gentle with it.*

**The claim this spec makes:** *all heavy computation happens on a machine that is
allowed to work hard (the compute host); the NAS stores bytes and serves light reads; results move
between them as immutable, checksummed artifacts with an atomic pointer flip.*

Everything below serves that sentence.

---

## 0. Why now — the pain points (all observed, all dated)

| # | Pain point | Evidence |
|---|---|---|
| P1 | **Weekly loop killed by systemd timeout.** `TimeoutStartSec=4h` expired mid-leg-ablation; run died at exactly 17:16:26 on 2026-08-24; verify never executed; service left failed. | the NAS `weekly-digest.md`, `systemctl` Result=timeout |
| P2 | **fp16 cross-encoder emulated on NAS CPU.** Reranker defaults to `model.half()` (a measured 3.12x win on Apple Silicon, `rerank.py:63`). the NAS's Pentium Gold 8505 has **no AVX-512-FP16** (`avx512_fp16: False` verified via /proc/cpuinfo), so torch emulates fp16 — the "speedup" is plausibly a slowdown. This alone plausibly explains a documented "~60–90s/pass" budget becoming ~4h+. | bench flags + `run-weekly-loop.sh` comment |
| P3 | **Compute co-located with storage degrades the NAS.** Operator directive 2026-08-24. Ablation sustains ~370% CPU for hours on the 24/7 Pentium + spinning-storage box. Even when it "works," it is the wrong place. | this week's watch |
| P4 | **Two writers already exist.** Mac and the NAS EACH run a weekly loop over their OWN corpus copy (dual-master by accident, reconciled by ad-hoc pushes). Nothing enforces single-writer; the Aug-23 charon entries proved copies diverge silently. | digests on both hosts |
| P5 | **Stale-serve promotes into frozen indexes.** serve resolves index paths once at startup; after any release cutover, a long-lived serve writes `/remember` traffic into the orphaned index for days (observed Aug 21–24, Mac; memory `alexandria_stale_serve_forks_promote_into_legacy_index`). | chunk-level probes |
| P6 | **Verify blind window.** Because P1 killed the run pre-verify, the ccba007 verify fixes remain unexercised on NAS; `[FAIL] indexed 0/5 newest documents` from the 12:03 run is still formally unanswered. | watcher final report |
| P7 | **Ablation cadence is misplaced.** It is a QA gate that belongs to *engine changes* (embedder/reranker/chunker/fusion changes), not to a weekly calendar. Running it weekly on the weakest host maximizes cost exactly when it yields no new information. | script header + verdict history |

## 1. The enterprise pattern this follows (and where it doesn't transfer)

Yes — the AWS shape you describe is real and is the right mental model:

| Cloud | Ours |
|---|---|
| S3: cheap, durable bytes; no compute | the NAS: corpus git repo, snapshots, backups, served indexes |
| EC2/EKS/EMR: stateless compute fleets | the compute host: sync distillation, embedding, index builds, eval |
| Build farm → immutable index artifact → atomic deploy (OpenSearch snapshot/restore, Vespa content deploy) | staged `releases/<id>/` built on the compute host, shipped to the NAS, activated by flipping `active.json` |
| Serving tier reads local replicas only | the NAS serve (or the compute host serve — see §3 R4) reads a fully-local release |

**Where naive disaggregation does NOT transfer** (pushback): hybrid search has two legs
with opposite storage affinity.

- Vector leg: LanceDB is object-storage-friendly (lazy range reads). Disaggregates fine.
- Lexical leg: SQLite FTS5. SQLite's own docs warn against network filesystems; this repo
  already refuses write locks on NFS/SMB (`writelock.py`) rather than pretend they're safe.

So the design is NOT "both legs read over the network." It is **build-and-publish**:
compute hosts build complete, self-contained, checksummed release directories on local
disk; the serving host receives them as files; activation is an atomic pointer flip.
Reads stay 100% local on whichever host serves. Writes to an index only ever happen on
one machine, on local disk.

## 2. Verified host facts (2026-08-24)

| Host | Hardware | Role in this spec | Notes |
|---|---|---|---|
| the NAS (NAS) | Pentium Gold 8505, 6 cores, spinning storage, 24/7 | **Storage + (optionally) serving.** No builds, no eval, no distillation. | avx512_fp16 absent; load already 5–6 from postgres/ingest baseline |
| the compute host | Ryzen 7 7840HS, **16 threads**, 27 GiB RAM, NVMe, always-on, nearly idle | **The compute host.** | Zen 4 has AVX-512 incl. FP16 → native fp16 cross-encoder. Repo now at ccba007 (repaired 2026-08-24: broken pack + shallow fetch, fixed with `fetch --depth`). torch 2.13 installed. **Its corpus clone is STALE**: gen 55, Aug 16, pre-releases layout, no active.json — must be rebuilt/re-synced before it can build anything. |
| Mac | M-series, MLX | Dev host. Exits the loop-running business. | MLX vectors are non-portable (different space than torch) |

## 3. Requirements

### R1 — Role assignment (hard rule)
the compute host: all sync, index, eval, ablation, embedding. the NAS: storage, snapshots,
backup, and read-serving. Mac: development only. No weekly-loop unit remains on the NAS
or Mac. The the NAS loop timer is disabled, not retuned.

### R2 — Single-writer contract (fixes P4)
Exactly one host ever mutates corpus content or indexes: **the compute host**. This includes the
`/remember` promote path — serve on the NAS must be strictly read-only for queries, and any
remember traffic routes to the compute host (direct POST or queued delta shipped by the compute host).
the NAS receives content exclusively as (a) git-fetched `sources` commits and (b) published
release directories.

Durability: **the NAS's git remote is canonical; the compute host's working copy is disposable.**
the compute host pushes after every mutation batch (not weekly); loss recovery = re-clone from
the NAS, rebuild locally, republish. The existing `CONTRACT-source-ownership.md` is amended
to name the compute host as sole writer. Mac's loop LaunchAgent and the NAS's loop timer/service are
removed in the migration (§5).

### R3 — Publishing protocol (fixes P1/P5 structurally)
1. the compute host completes sync → index (staged release build) → verify on ITS local corpus.
2. `publish` step: rsync the release directory to `the NAS:.alexandria/index/releases/<id>/`,
   then atomically flip `active.json` on the NAS (write-temp + rename — the mechanism that
   exists since the Aug-21 cutover).
3. Releases are immutable once published; a failed build never reaches the flip.
4. **Every publish restarts (or hot-revalidates, see R7) the NAS's serve.** Until R7 ships,
   restart-after-publish is mandatory — it is the known workaround for the stale-context
   promote fork.
5. NAS-side post-publish verify (read-only, seconds): checksum match + `/health`
   source_documents_agree=true. Trust outcomes, not exit codes.

### R4 — Serve placement (decision needed, recommendation included)
- **Option A (recommended initially): serve stays on the NAS.** Serving is light
  (fusion + 8-pair rerank per query). With R6's dtype fix, per-query rerank on the NAS is
  tolerable (~1–2 s worst case). Keeps power draw lowest; NAS earns its keep as the
  always-available reader.
- **Option B (if A's query latency disappoints): serve moves to the compute host too,** and the NAS
  becomes pure storage/backup. Simplest possible story ("the NAS touches nothing"), costs
  ~10 W always-on on the compute host and couples consumer availability to the compute host uptime.
Decide A/B after one week of Option-A latency measurements from the real query log;
do not guess.

### R5 — Weekly loop split (fixes P1/P7)
- **the compute host `alexandria-build.service/timer`** (weekly): sync → index → staleness →
  publish → post-publish verify. Expected runtime on 16 threads: minutes, not hours.
  TimeoutStart generous (2h) but now irrelevant to correctness.
- **Ablation becomes opt-in** (`--with-ablation`, or a separate
  `alexandria-ablation.timer` at monthly cadence + manual trigger on engine changes).
  Default OFF for any deployment — enterprise users never inherit a multi-hour eval by
  installing the product. The loop's notifier wiring moves with it. The on-engine-change
  trigger must be AUTOMATED (CI hook keyed to embedder/reranker/chunker/fusion file
  paths), not operator memory.
- **the NAS retains only a read-only verify hook** post-publish (R3.5).

### R6 — Device-aware reranker precision (fixes P2)
Tri-state config `ALEXANDRIA_RERANK_HALF=auto|on|off`. `auto` selects fp16 only for
known-good accelerators (CUDA/MPS); CPU defaults to fp32 — an `avx512_fp16` cpuinfo flag
alone is NOT sufficient evidence that torch's CPU backend uses native fp16 for this model.
Correctness gate when enabling half anywhere: compare final top-k ORDERING between
fp32/fp16 over a sample of real queries (tie-tolerant), not byte-identical scores.

### R7 — Promote re-resolves active.json (fixes P5 properly)
promote_pending re-resolves `resolve_active_index_dir()` at each drain cycle (or verifies
its store path still equals it and rebuilds context on mismatch). Until merged, the ops
rule stands: restart serve after every cutover/publish.

### R8 — Corpus replication topology
sources replication: the compute host commits weekly (as today's loops do) → the NAS pulls via git
(ssh remote). Index replication: R3 release shipping only. No shared mounts anywhere;
no SQLite file is ever opened across hosts.

## 4. What the compute host needs before day one (migration blockers)
1. Corpus re-sync: current clone is gen55/Aug-16/pre-releases. Cheapest correct path:
   fresh clone/pull of sources history from the NAS (or Mac, whichever is ahead), then one
   full local index build on the compute host (fast: 16-thread Zen4, est. minutes-to-tens-of-minutes).
2. Embed-provider standardization on **torch/local** (never MLX) so built releases are
   portable to the NAS's CPU. Manifest checks will refuse mismatches — good.
3. HF model cache warm-up for embedder + reranker (Qwen3-Embedding-0.6B already cached;
   bge reranker/embedder need pulling).
4. Repo repair note: the compute host `.git` had invalid refs (fixed via depth-fetch 2026-08-24);
   consider a clean re-clone during migration anyway.

## 5. Migration plan (each phase independently verifiable)
- **M1 (stopgap, this week, before Sunday's timer fires):** disable the NAS loop timer;
  run ONE full loop on the Mac in a SCRATCH corpus copy with publish/snapshot disabled and
  all scheduled LaunchAgents/timers paused on both Mac and the NAS — its only job is to
  exercise the ccba007 verify end-to-end; it must not touch the canonical corpus.
- **M2:** the compute host corpus rebuild + provider standardization (§4).
- **M3:** publish protocol (R3) implemented AND R7 (promote re-resolves active.json)
  merged — a publish is not done without it; automated serve-reload remains as
  belt-and-braces. First the compute host→the NAS release shipped; post-publish verify green on the NAS.
- **M4:** retire Mac + the NAS loop units; single-writer contract enforced in docs + hooks.
- **M5:** ablation opt-in + dtype fix (R5/R6) merged; one ablation run ON THE COMPUTE HOST to
  finally produce a healthy NAS-independent quality baseline.
- **M6 (optional):** serve-placement decision per R4 after a latency week.

## 6. Verification gates
- Sunday run completes on the compute host with verify PASS and publish checksum match.
- the NAS health shows source_documents_agree=true after every publish.
- One ablation run on the compute host completes in <30 min wall (vs >4h on the NAS), fp16-vs-fp32
  ordering identity recorded.
- No process on the NAS exceeds a light-duty CPU envelope (define: no >50% sustained
  multicore for >10 min outside serve request handling).

## 7. Out of scope
Replacing SQLite FTS with a network-native lexical engine; LanceDB object-storage mode
over the network; moving serve behind a proxy/auth layer (separate work); changing what
the golden set measures; multi-tenant anything.

## 8. Open questions for the operator
1. R4 Option A vs B (recommend A first, measure, then decide).
2. Confirm the compute host may hold the ONLY writable corpus copy (single-writer contract).
3. Where should the NAS pull sources commits FROM once Mac exits the loop business —
   the compute host directly over ssh (recommended), or keep Mac as git intermediary?
4. Monthly ablation cadence acceptable, or strictly on-demand per engine change?


## 9. Adversarial review outcome (Red, 2026-08-24) — APPROVE-WITH-CHANGES

Verdict: **APPROVE-WITH-CHANGES**. All accepted changes are folded into §R2/R5/R6/M1/M3
above. Summary of what Red found:

1. **Load-bearing hole (fixed in R2):** under Option A, serve-on-the NAS still promotes
   `/remember` traffic into the local index — a second writer that violates the
   single-writer contract and recreates the dual-master problem under another name.
   Resolution: serve is strictly read-only for queries; remember traffic routes to
   the compute host (direct or queued delta).
2. **Durability (fixed in R2):** the NAS git remote is canonical; the compute host pushes after every
   batch; the compute host working copy is disposable/rebuildable.
3. **R7 ordering (fixed in M3):** promote re-resolution must land before/with the first
   real publish; restart-after-publish is belt-and-braces, never the sole guarantee.
4. **R6 over-optimism (fixed in R6):** avx512_fp16 flag ≠ torch native-fp16 CPU path;
   CPU defaults fp32; tri-state config; top-k ordering gate instead of byte identity.
5. **Option A/B reframed:** if `/remember` cannot cleanly move off serve-on-the NAS,
   Option B (serve on the compute host) is the defensible default. Decision deferred until the
   write path is implemented.
6. **Enforcement:** the NAS light-duty envelope enforced via systemd `CPUQuota`/
   `MemoryMax` on any remaining units, not just measured informally.
