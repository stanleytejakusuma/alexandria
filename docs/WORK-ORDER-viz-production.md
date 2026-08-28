# WORK ORDER — viz-production: the derived semantic-graph visualization as a hardened engine module

**Repo:** `~/codebase/alexandria` · **Branch:** `viz-production`, not `main`
**Venv:** `.venv` (`.venv/bin/python`, never system python) · Python 3.12
**Baseline:** `main` at `e33ea53`, 1153 tests passing. Do not regress it.

## 0. Why this exists / why scoped this way

A 2-day demo build produced a semantic-graph visualization of the corpus
(36,928 docs / 58,723 chunks: kNN edges → Leiden clusters → UMAP layout →
LLM-labeled cluster-map wiki + canvas viewer). Stanley wants it
"production-grade," explicitly as **personal-tool quality**: tests, versioned
payload, staleness detection, a sanitization boundary from day one — not
multi-user, not a retrieval feature.

The repo has a standing decision, `docs/DECISIONS-graph-vs-vector.md:99`:
**REJECT** a persistent, standing knowledge-graph index. Per the decision's
own re-examination protocol, this was re-opened and put to Red (verdict in
`/tmp/red-verdict-graph.md`, session-local; the ADR addendum appended to that
decision doc on 2026-08-28 records the ruling and seven bound conditions).
Red ruled the artifact a **rendering, not the graph index** — no extraction,
no relation semantics, no retrieval path, fully regenerable — but the verdict
is **PROCEED-WITH-CONDITIONS**, and those conditions are hard requirements,
not suggestions. This work order implements the module **under them**.

Two real incidents shaped the requirements:
- **Data-path contamination** (caught by Red, not by us): the demo wrote 149
  LLM-labeled cluster pages into `wiki/` — which `chunker.py:564` walks —
  making LLM labels retrievable corpus content, exactly the failure the
  decision names. Reverted (`86fe959f3` on the corpus repo), generator
  repointed outside the corpus. The data-boundary test (§5) exists so this
  can never re-enter.
- **Unvalidated labels**: 159 LLM-generated claims shipped unaudited. A
  wrong map misleads the operator *as a retrieval component* even with zero
  software retrieval path. The label audit (deliverable 4.5) is the
  proportionate defense.

## 1. Where things live

- New module: `src/alexandria/viz/` — **a strict dependency sink**: it
  imports the engine, nothing imports it.
- Build output (payload + wiki pages) lives **outside the corpus root**,
  default `~/.alexandria/viz-out/` (override via `--out`). Never write into
  `~/alexandria-corpus/` — that root is indexed; generated markdown there is
  a contamination event.
- Reference implementation (not in this repo, do not copy blindly): the
  demo at `~/alexandria-graph/` — `build_graph.py` (pipeline),
  `viewer.js`/`index.html` (UI), `gen_wiki.py` (pages), `relink.py`
  (rendered-link rewriting), `serve_demo.py` (serving). It is the proof of
  concept; this work order productionizes its logic. The viewer UI is NOT
  re-implemented here (§8) — but the demo's measured lessons are §7 traps.

## 2. What already exists — call these, do not rebuild them

- `src/alexandria/index/releases.py`: `active_release_id()`,
  `resolve_active_index_dir()`, `list_releases()` — the release machinery
  the payload must bind to.
- `src/alexandria/index/manifest.py`: `read_manifest()` — provider/model/dim
  provenance precedent for the payload schema.
- `src/alexandria/staleness.py`: `StalenessReport`, `newest_source_mtime()`,
  `newest_index_finished_at()` — staleness detection EXISTS in the engine;
  the viz must consume it, not reimplement it.
- `src/alexandria/synthesis/clustering.py`: `find_topic_clusters()` (Leiden
  via sknetwork), `_pairs_above()` streaming cosine pairs, `DEDUP_THRESHOLD`
  — cluster primitives. The demo used its own pipeline; the production
  module should reuse these where the shape fits (documented deviation
  allowed where the demo's mutual-kNN + backbone design is measurably
  better — say so in the module README).
- `src/alexandria/wiki_site.py`: the stdlib static renderer (305 lines) —
  reuse for wiki page rendering.
- `src/alexandria/llm.py` + the gateway client pattern (red-chain route:
  POST `http://127.0.0.1:20128/v1/chat/completions`, key from keychain
  `pi-gateway`, model `gpt-5.6-sol` primary / `auto/cheap` fallback) for
  labels — but ALL tests must be offline via `ScriptedClient`
  (`tests/test_eval_runner.py` pattern).

## 3. The shape this builds

```
alexandria viz build [--out DIR] [--release ID] [--public]
  → reads active release (or --release), pulls vectors + chunk/doc metadata
  → mutual-kNN + similarity floor + spanning backbone → Leiden
    (fine ~149, super ~10) → doc aggregation → c-TF-IDF + optional LLM labels
  → versioned payload (binary positions/edges, meta.json) + cluster-map
    markdown wiki (pages + index) → written to --out (NOT the corpus)
alexandria viz audit [--out DIR]        # cluster-quality report, no thresholds
alexandria viz serve [--out DIR] [--port] [--host]   # static serve of the build
alexandria viz gc [--out DIR]           # list/remove old builds (never auto)
```

- **Payload schema** (frozen, versioned): `schema_version: 1`; meta.json
  carries `release_id`, `generation`, `built_at`, `embedder_provider`,
  `embedder_model`, `seed`, counts. Edge keys are EXACTLY the geometric set:
  `source`, `target`, `similarity` (int32/int32/f32). Positions: f32 binary.
  No label/type/relation fields in the schema — labels live in the wiki side
  of the build, not the payload. (`--public` mode additionally scrubs:
  titles → cluster labels; `id` dropped; only positions+colors+edges remain.
  The demo found 919 of 36,928 titles name private hosts — this is a leak
  scanner fixture.)
- **Reproducibility**: fixed seeds everywhere (Leiden `random_state`,
  UMAP `random_state`); deterministic ordering of inputs. Cross-rebuild
  *identity* persistence (stable IDs, label caches) is FORBIDDEN — the
  no-persistence ratchet. Reproducible = same inputs, same output; a changed
  corpus legitimately changes the map.
- **Staleness**: build refuses (exit 2, loud) when the active release's
  generation is stale per `staleness.check()` or when `--release` points at
  a release whose checksums fail `verify_checksums()`.

## 4. Deliverables

### 4.1 `src/alexandria/viz/__init__.py` — entrypoints
`build()`, `audit()`, `serve()`, `gc()` — thin, CLI-wired, no logic.

### 4.2 `src/alexandria/viz/build.py` — the pipeline
Port of the demo's `build_graph.py` pipeline (mutual-kNN, floor, backbone,
degree cap, Leiden, super-clusters, doc aggregation, c-TF-IDF, UMAP,
payload writer). Vector pull via the LanceDB release table (chunks + vectors
+ doc metadata; `to_arrow()`, never `to_lance()` — pylance absent). Verify
block at the end (cluster id ranges, edge sim band, connectivity) — the
demo's verify block caught 4 real bugs; it is not optional.

### 4.3 `src/alexandria/viz/payload.py` — schema + writer + reader
`PAYLOAD_SCHEMA_VERSION = 1`; `write_payload(out, ...)`; `read_payload(out)`
with strict key validation. The frozen schema lives here and only here.

### 4.4 `src/alexandria/viz/labels.py` — c-TF-IDF + LLM labels
Offline-first: c-TF-IDF terms always; LLM titles/summaries via the gateway
pattern (primary `gpt-5.6-sol`, fallback `auto/cheap`, then title-only,
then offline fallback) with a **written-once cache in the build dir**
(regenerable, so allowed: it is output state, not cross-rebuild persistence
— cache keyed by cluster centroid hash + model id; document this line).

### 4.5 `src/alexandria/viz/wiki.py` + label audit record
Cluster-map markdown writer (pages + index) into `--out/wiki/` — never the
corpus. Plus, ONCE, before the module is called production: the **label
audit** — sample ~20 of the cluster labels, inspect ~5 member docs each,
record the match rate in `src/alexandria/viz/README.md`. <80% sane → labels
carry a visible "auto-generated, unaudited" banner.

### 4.6 `src/alexandria/viz/serve.py` — static serve
`http.server`-based (stdlib, matching `serve.py` philosophy), host/port
arguments (NO hardcoded `127.0.0.1`), `Cache-Control: no-store`, request
logging to stderr. Foreground (supervised by launchd/systemd, per the demo's
lesson that `nohup &` children die silently).

### 4.7 `src/alexandria/viz/sanitize.py` — the day-one public boundary
`--public` transform: titles → cluster labels, drop `id`, strip wiki links
to private paths. Leak-scanned as a fixture (§6).

### 4.8 Tests (TDD, offline, vacuity-verified)
- `tests/test_viz_data_boundary.py` — §5 THE TEST THAT MATTERS MOST.
- `tests/test_viz_payload_schema.py` — schema freeze: exactly the geometric
  key set; a `relation_type`-style key fails; failure message cites
  `DECISIONS-graph-vs-vector.md`.
- `tests/test_viz_sink.py` — no engine module imports `viz`; payload path
  constant appears only within `viz` (grep-level check).
- `tests/test_viz_staleness.py` — stale generation → exit 2; checksum
  failure on `--release` → exit 2.
- `tests/test_viz_reproducible.py` — two builds, same fixture corpus, same
  inputs → byte-identical payloads.
- `tests/test_viz_sanitize.py` — fixture with known private-host titles;
  `--public` output scrubs them (vacuity: fails pre-fix).
- `tests/test_viz_offline.py` — full build on a tiny fixture corpus with
  `ScriptedClient` labels and zero network; asserts the builder never calls
  the gateway when labels are offline.

## 5. THE TEST THAT MATTERS MOST

`test_build_output_never_enters_the_index` (in `test_viz_data_boundary.py`):

Build against a fixture corpus (its own tmp corpus — fixtures only, never
the real one). After `viz build`, assert **zero cluster-map content in
either index**: search the fixture's `fts.sqlite` and LanceDB table for a
distinctive cluster-page string and for every generated filename; all
absent. Also assert the build wrote nothing under `<corpus>/.alexandria/`
and nothing under `<corpus>/wiki/`.

This is the exact regression we shipped in the demo (149 LLM-labeled pages
landed in the indexed root; Red caught it; reverted `86fe959f3`). If this
test ever regresses, the module is contaminated by construction. Vacuity
check: the test must fail against a variant that writes to the corpus root.

## 6. Constraints

- **TDD**: tests before implementation, suite green at every commit
  (`unset ALEXANDRIA_EMBED_PROVIDER && PYTHONPATH=src .venv/bin/python -m
  pytest tests/ -q`).
- **All LLM calls in tests are offline** via `ScriptedClient`
  (`tests/test_eval_runner.py` pattern). No third mocking pattern.
- **Red conditions are hard requirements** (ADR addendum, 2026-08-28): data
  boundary; sink rule; schema freeze; label audit ≥80%; no-persistence
  ratchet (no stable IDs, no label caching across builds, no incremental
  updates, no state not regenerable by one command); metric-6 ledger
  (`src/alexandria/viz/MAINTENANCE.md` — log time; 2 months >60 min/week →
  module exits); module existence inadmissible at the phase 3→4 graph
  re-examination.
- **Do-not-modify list**: `src/alexandria/llm.py` (temperature=0 refuse-guard
  is load-bearing), `src/alexandria/index/chunker.py` (the `sources/`+`wiki/`
  walk is the boundary this module must route around, not edit),
  `src/alexandria/retrieval/rerank.py`, `src/alexandria/serve.py`. If you
  believe any must change, stop and report why.
- **Never** run an index build on the compute host (live capital services;
  the fence in `AGENTS.md`). Viz builds run on Mac — they are read-only over
  the release's vectors and regenerate in ~2 minutes; they are not
  "sustained heavy compute," but keep them out of the NAS's serve host too.
- **Leak scanner**: `scripts/precommit-scan.py --all` runs on every commit.
  `sanitize.py`'s private-host fixture must itself be a fixture (synthetic
  hostnames), never real ones. Repo docs say "the NAS"/"the compute host".

## 7. Known traps (hard-won in the demo — do not re-earn them)

1. `argpartition` is NOT sorted — argsort within rows (`order =
   np.argsort(-gathered, axis=1); np.take_along_axis`) or floor logic breaks.
2. Row indexing: gather needs LOCAL rows (`np.arange(e-s)`), placement needs
   GLOBAL (`idx[s:e]`). Mixing them → IndexError.
3. sknetwork: `from sknetwork.clustering import leiden` imports a MODULE; the
   callable is the `Leiden` class (`.fit_predict(adj)`).
4. **int64 truncation**: `np.array(sorted(best.items()), dtype=np.int64)`
   truncates float sims to 0 — edges became all-zeros. Use `np.fromiter`
   for keys and values separately.
5. Degree caps must exempt backbone (max-spanning-tree) edges or
   connectivity breaks into dozens of components.
6. `for (i < pos.length)` with `pos[2*i]` overruns a flat Float32Array
   (length = 2×node count) → NaN → blank canvas.
7. Centroid-placed cluster labels LIE when UMAP splits a cluster across
   regions. The demo's answer: density-ownership grid + neighbourhood
   purity gate (cell purity alone reads 1.0 while the visible radius is 40%
   foreign). If labels are drawn on the map, this applies.
8. LLM labels are nondeterministic per build → versioned-diff noise → the
   pressure to cache labels IS the persistence ratchet in disguise. The
   write-once cache (§4.4) is the deliberate, documented line.
9. Template/boilerplate chunks dominate clustering (the demo measured 8,077
   of 36,928 docs as template-y; top clusters were envelope texts). The
   demo's post-hoc template detection + demotion is required, or the map is
   a map of boilerplate.
10. Near-duplicate chunks (97.4% of chunks have a ≥0.99 twin) saturate the
    similarity floor — compute the floor from the 10th-neighbour knee, not
    the top-1 curve.
11. `git revert` of a commit chain must go NEWEST-FIRST; oldest-first
    produces modify/delete conflicts (learned the hard way today).
12. Never write generated markdown into any directory the chunker walks.

## 8. Out of scope — do not build

- **No viewer UI port.** The canvas viewer stays a reference at
  `~/alexandria-graph/`. This work order ships the data product + serve;
  a future work order may port the UI (and may reference
  `global:large-graph-viewer` skill).
- No relation types, no entity extraction, no typed edges — ever (schema
  freeze).
- No retrieval integration: nothing in `search`/`answer`/synthesis may read
  the payload. The operator is a retrieval component (Red) — that is why the
  label audit exists, not why search should.
- No stable cluster IDs, no incremental rebuilds, no label persistence
  beyond the write-once cache.
- No multi-user/auth/tenancy.
- No cluster-quality *thresholds* or gates — `audit` reports numbers only
  (Red: baselines you don't have become a tuning hobby or dead code).
- No changes to the corpus, the indexer, or the serve stack.

## 9. Verification before reporting done

```bash
unset ALEXANDRIA_EMBED_PROVIDER && PYTHONPATH=src .venv/bin/python -m pytest tests/ -q
HF_HUB_OFFLINE=1 HF_HOME=$(mktemp -d) PATH=/usr/bin:/bin PYTHONPATH=src .venv/bin/python -m pytest tests/ -q
PYTHONPATH=src .venv/bin/python scripts/precommit-scan.py --all
git diff --check
```

Plus, on the real Mac corpus (read-only, ~2 min):
- `alexandria viz build` succeeds; `viz audit` reports; payload schema
  validates via `read_payload`; `viz serve --port 8510` serves 200 with
  `no-store`; `--public` output contains none of the 919 private-host
  titles (compare against the demo's known list);
- label audit recorded in the module README with the sampled match rate;
- MAINTENANCE.md ledger started.

## 10. Report back

- Modules built + test counts (each file, each test's vacuity check result).
- Proof points for §5: the data-boundary test's failure mode demonstrated
  pre-fix, green post-fix.
- Real-corpus numbers: build time, cluster counts, edge counts, payload
  sizes (binary vs JSON), staleness behavior on the actual active release.
- Label audit outcome (rate, sampled clusters, banner decision).
- Any spec deviation and why (e.g. clustering reuse vs demo pipeline).
- Anything in constraints/traps that bit you anyway.
