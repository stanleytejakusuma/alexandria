**Repo:** `~/codebase/alexandria` · **Branch:** `migrate-serve-to-nas`, not `main`
**Venv:** `.venv` (`.venv/bin/python`, never system python) · Python 3.12
**Baseline:** `main` at 749 passing tests. Do not regress it.

## 0. Why this exists / why scoped this way

Operator-ratified decision (see `docs/DECISION-serve-host-remote-2026-08-15.md`):
this deployment opts into the *optional* remote topology (serve + storage + index
on the always-on NAS host); the operator's machine is demoted to build machine.
The default remains local for a single user (§5.8). This work order is **mostly an ops runbook**, not a code
feature — the engine already supports remote hosting (SPEC §5.8) and already has the
`local` (torch) embedding provider. Deliberately excluded: the split topology
(storage on NAS / compute on the laptop — rejected: query-time embedding runs on the
serve host and the laptop sleeps), installer packaging, and any retrieval-quality
change. If the runbook finds a real code gap (e.g. the serve path does not fail loudly
enough on a provider mismatch), that change lands on this branch; otherwise the branch
carries doc + test additions only.

## 1. Where things live

- Engine code: this repo.
- Corpus + index + state: `~/alexandria-corpus`, a **separate local-only git repo**.
  The migration is a corpus-repo operation as much as an engine one; the engine repo
  treats the corpus as out of scope for edits.
- **Private detail lives outside the repo.** The leak scanner forbids private
  hostnames/codenames in-repo (`.leakpatterns.local`). The actual host, addresses,
  keychain service names, and exact commands go in the companion notes next to the
  corpus (not in this repo). This work order refers to "the NAS host" / "the fleet
  LLM gateway" / "the tunnel" throughout.

## 2. What already exists — call these, do not rebuild them

- `ALEXANDRIA_EMBED_PROVIDER=local` — torch (sentence-transformers) provider,
  `LocalEmbedder` in `src/alexandria/index/embedder.py`.
- `alexandria index --rebuild` — full re-index, retains the embedding cache
  (do **not** pass `--enrich`; see DECISION-enrichment-2026-08-11.md).
- `alexandria serve --host/--port/--unix-socket` — loopback-by-default, remote via
  tunnel (§5.2/§5.3).
- Manifest gate F4 — refuses a provider mismatch loudly on read and write.
- `backup_state`/`restore_state` (`src/alexandria/backup.py`) — state, never the
  rebuildable indexes.
- `scripts/serve-launchd.sh` — keychain-sourcing bridge (adapt for the NAS supervisor).
- `scripts/run-weekly-loop.sh` — the weekly loop; it moves with the corpus.
- `alexandria eval --fail-on-regression` — the recall gate; re-baseline after re-embed.

## 3. The shape this work order builds

1. Re-embed on the operator's machine with `ALEXANDRIA_EMBED_PROVIDER=local`
   (`index --rebuild`, no `--enrich`).
2. Re-run `alexandria eval`; record the new recall baseline (the MLX 63.3% does not
   transfer).
3. Transfer corpus + index + embedding cache to the NAS host (rsync; see companion
   notes for exact paths/excludes).
4. Deploy `serve` on the NAS: loopback bind, `ALEXANDRIA_EMBED_PROVIDER=local` set
   explicitly (default `mlx` must fail loudly on Linux, not silently fall back), and
   the LLM key supplied by the NAS-side keychain bridge or env.
5. Mint a dedicated virtual key on the fleet LLM gateway (do not copy local-machine
   keys); point serve at the fleet gateway (loopback from the NAS).
6. Re-point clients: extension fallback host, weekly loop, and any capture sweep to
   the NAS over the tunnel.
7. Retire the local-machine launchd job only after the NAS path is verified.

## 4. Deliverables

- `docs/DECISION-serve-host-remote-2026-08-15.md` — the ratified decision (done on
  `main`, referenced here).
- Companion runbook (outside the repo) — exact hosts, addresses, keychain services,
  rsync/flags, supervisor config, and the minted-key steps.
- Engine changes, **only if a gap is found**: the serve path must fail loudly when the
  running provider cannot embed on that host (F4 covers the index/manifest case; this
  is the "provider not installed on this platform" case). If added, it ships with a
  mutation-tested regression test.

## 5. THE TEST THAT MATTERS MOST

A provider/host mismatch must be **loud, not silent**. Serving a `local`-built index
with `mlx` (or running the `mlx` provider on a non-Apple-Silicon host) must refuse at
startup/query, never return wrong-similarity results. The existing manifest-gate tests
cover the index half; if deliverable §4 adds a platform check, its test must fail on
the mutated code by name.

## 6. Constraints

- TDD: tests before implementation; suite green at every commit.
- Any offline LLM/retrieval mocking uses the existing `ScriptedClient` / `FakeEngine`
  patterns — do not invent a third.
- Do-not-modify: `llm.py`'s temperature=0 refuse-guard (live gateway bug workaround),
  the synthetic gate's lexical-only config (BACKLOG #47), and the soft-delete
  hydration guard.
- No private hostnames/codenames in-repo (`.leakpatterns.local`).

## 7. Known traps

- Do not CPU-embed the full corpus on the NAS — the 2026-08-11 hard-reboot pattern.
  Build on the operator's machine, serve anywhere.
- Do not copy the MLX-built index to the NAS — different vector space; the manifest
  gate will refuse it, which is the intended behavior, not a bug to route around.
- The 63.3% recall baseline is MLX-only; re-baseline after the `local` re-embed and
  record it honestly.
- Local-machine keychain keys 401 on the fleet gateway (per-instance key databases);
  mint a new key there.

## 8. Out of scope

- Installer packaging / a guided provider-choice step (§5.8 names it future work).
- The split topology (storage on NAS, compute on the laptop).
- Any retrieval-quality change beyond what the re-embed itself produces.
- Deleting the local-machine corpus copy until the NAS path is verified end-to-end.

## 9. Verification before reporting done

```bash
.venv/bin/python -m pytest tests/ -q       # all green, no skips masking failures
.venv/bin/python scripts/precommit-scan.py --all
```
plus, against the real corpus: `alexandria eval` on the NAS records the new baseline,
and one real `answer` emits with the new gateway key.

## 10. Report back

Modules built + test counts; proof for the §5 test; the new recall baseline (old vs
new, honestly); any spec deviation and why; anything in §7 that bit anyway.


---

## Red productionalization review addendum (2026-08-23)

Independent review of the migration plan + serve production posture returned
**APPROVE-WITH-CHANGES** (round 1). The auth/hardening on main is sound for
the three deployment shapes; two blockers and four musts must land before
cutover. This addendum amends the plan accordingly; the engine-side fixes
that were code gaps (unix-socket 0600 mode on identity sockets) landed on
`feat/prod-readiness-nas`.

### Blockers (must fix before any cutover)

**B1 — embedding-cache transfer: plain rsync of a live SQLite WAL dataset is
unsafe.** A cold rsync can capture `db`/`-wal`/`-shm` in inconsistent states
and silently poison embeddings on the NAS even though startup and manifest
checks pass. Required procedure:

- Quiesce ALL Mac writers (stop the launchd job and any batch/cron writers).
- `PRAGMA wal_checkpoint(TRUNCATE)` on the cache DB, then copy ONLY the main
  DB file (no `-wal`/`-shm` sidecars).
- On the NAS before starting serve: `PRAGMA integrity_check` on the copied
  DB, and confirm the manifest identity (provider `local`, model, revision,
  dim, normalization, dtype) exactly matches the Mac build that generated
  the cache.
- NEVER rebuild the cache on the NAS with a CPU embed as the default path:
  a ~45k-chunk CPU embed previously took this NAS host down (2026-08-11);
  NAS rebuild RTO must be benchmarked (§M3) before it is even considered.

**B2 — cutover must prevent split-brain.** "Mac launchd retired only after
verification" is too late if the Mac keeps serving/writing after the final
rsync, or if any client still points at the Mac:

- Stop the Mac launchd job and every batch/cron writer BEFORE the final
  rsync; the final rsync must come from a quiescent source.
- Re-point clients as ONE cutover action, then verify NAS `/health` and the
  staleness check, then retire the Mac configuration.
- Take a rollback snapshot (backup_state + index baseline archive) before
  the NAS begins writing. Cross-writer locking protects writers inside ONE
  install; it does not coordinate two hosts.

### Musts (before migration)

- **M1 — eval gate for the re-embed shift**: run the eval suite with the Mac
  `local` provider, record the new recall/MRR baseline, set a rejection
  threshold BEFORE looking at results, archive the old index + MLX baseline,
  and do not cut over on a material regression.
- **M2 — NAS provider readiness**: validate that the `local` torch provider
  imports, loads the model, and passes the manifest gate ON THE NAS before
  the migration; make systemd fail the unit immediately if the
  provider/model cannot load (serve already refuses startup loudly via
  `_warm_embedder`; the supervisor must treat that as a failed start).
- **M3 — NAS rebuild RTO benchmark**: measure a full CPU re-embed/rebuild on
  the NAS before cutover. The 33.1min Mac RTO does not transfer; if NAS RTO
  is unacceptable, the safely-checkpointed cache copy (B1) is the fallback.
- **M4 — off-NAS backup + restore test**: an off-NAS copy of the corpus +
  `.alexandria` state (not only `backup_state` archives on the same disk),
  and a restore test run on the NAS itself.

### Auth/TLS deployment conditions (all three shapes)

- Loopback-only local: sound on a trusted host; enable `--require-token` if
  the NAS has untrusted local users (any local process can otherwise act as
  `local-anonymous` with full access, including `/remember`).
- Remote via tunnel: sound only if the tunnel terminates at NAS loopback and
  is authenticated. Direct remote TCP without TLS/SSH/VPN is a BLOCKER
  (tokens would pass in cleartext).
- Proxy-fronted VPS: `--require-token` is mandatory; the proxy must forward
  `Authorization` and must not log it.
- Identity sockets are now chmod 0600 (engine fix on `feat/prod-readiness-nas`);
  verify ownership/mode on the NAS supervisor config.
- Token rotation requires a serve restart (load-once token file) — document
  it; online rotation is a can-land-after item.

### Ops musts before migration

- Disk monitoring and log rotation (audit/cost logs, sqlite DBs).
- systemd hardening: `EnvironmentFile` secrets mode 0600 or tighter, restart
  policy, and a readiness check (health poll) after start.
- Basic alerting on heartbeat/liveness failure and staleness — never rely on
  a person checking `/health`.

### Can land after migration

- Centralized log aggregation/metrics.
- Online token rotation/revocation without restart; token expiry/rate limits.
- Automated upgrade/rollback pipeline (pinned commit + eval baseline
  comparison).
- Multi-tenancy/RBAC remains deliberately deferred (single-tenant trigger).
