# WORK ORDER — Phase 2 fact-recall evaluator (golden page-to-fact harness)

**Repo:** `~/codebase/alexandria` · **Branch:** `phase2-fact-recall-eval`, not `main`
**Venv:** `.venv` (`.venv/bin/python`, never system python) · Python 3.12
**Baseline:** `main` at **334 passing tests** (328 phase-1 baseline + 6 phase-2 core). Do not regress it.

---

## 0. Why this exists, and why it is evaluation-only (read first)

Phase-2's golden-synthesis gate (`docs/SPEC-phase2-eval.md`) is
**load-bearing-fact recall ≥ 90%**. The first live measurement against a real
golden cluster produced an honest mismatch: the single-page pipeline emitted a
page whose native checks passed (deterministic chunk accounting, entailment
audit, sampled skip-log coverage, anti-gutting repair), yet two independent
page-to-golden graders scored only **3/5** and **4/5** facts covered — below
the 90% gate.

The root distinction this work order exists to implement:

- `src/alexandria/coverage.py` (Judge 2) is scoped by design to one question:
  *does an uncited, skipped chunk contradict or materially qualify a claim the
  page makes?* It is **not** a completeness grader — it never asks whether the
  page expressed every load-bearing proposition inside evidence it **did**
  cite. Its own rubric says completeness is a separate recall check.
- Therefore a native coverage pass cannot certify the recall gate. The gate
  needs its own measurement instrument.

**This work order builds that instrument and nothing else.** It must NOT touch
the runtime pipeline, its prompts, retrieval, or `coverage.py` (which is frozen
without explicit sign-off). We measure first; if the full 8-cluster run shows a
consistent omission pattern, *that* evidence decides what to change next —
not a guess made before measurement.

Independent review (Red, 2026-08-05) approved this lane with four
load-bearing requirements, all incorporated below:

1. Grade the **rendered reader-visible page text**, not the internal structured
   claims object — otherwise we silently measure an easier, different quantity.
2. Every `covered: true` verdict must carry a **quoted page evidence span**,
   or a grader can hallucinate coverage un-auditably.
3. Do **not** gate on blind strict-AND consensus alone: report each grader's
   recall, per-fact agreement/disagreement, and a conservative consensus
   recall, and list every disagreement for manual adjudication.
4. Join every consensus-miss against the captured gather output
   deterministically, so "evidence never gathered" (retrieval failure) is
   separated from "evidence gathered but page omitted it" (writer/repair
   failure).

The pipeline's page generation is NOT part of this work order's implementation
scope for live runs: the measurement driver (`scripts/synthesize-golden-
pages.py`, §4) exists so the maintainer can produce the frozen page set, but
**no live LLM call is made during implementation** — verification here is the
offline suite only (§8). Same posture as the phase-2 core work order.

---

## 1. Scope

**In scope (new files only, plus tests):**

- `src/alexandria/eval/synthesis_fact_recall.py` — the evaluator module (all
  logic; fully offline-testable).
- `scripts/synthesize-golden-pages.py` — measurement driver: produces frozen
  per-cluster pages + gather sidecars for the maintainer's live run.
- `scripts/eval-synthesis-fact-recall.py` — thin CLI over the module.
- `tests/test_synthesis_fact_recall.py`, `tests/test_synthesize_golden_pages.py`.

**Out of scope (hard, do not expand):**

- Any change to `src/alexandria/coverage.py` (frozen), `src/alexandria/synthesis/*`
  (runtime pipeline), writer/repair/gather prompts, retrieval, rerankers, or
  `src/alexandria/eval/{gather_completeness,history,runner,metrics}.py`.
- The full-corpus sweep orchestrator, topic/dedup clustering, persistent gather
  state, dedup, or any graph storage (all separately scoped work orders).
- Live LLM execution during implementation (see §0/§8).
- New dependencies, changes to `pyproject.toml`, or new connectors.

---

## 2. Interfaces you consume (already built, do not change)

All from this repo, verified against `main`:

- `alexandria.eval.synthesis_golden`:
  - `LoadBearingFact(id: str, text: str, supported_by: tuple[str, ...])` —
    frozen dataclass; `supported_by` holds corpus doc ids.
  - `SynthesisClusterEntry(id, topic, source_docs, load_bearing_facts,
    provenance)` — frozen dataclass; `load_bearing_facts` is a tuple of
    `LoadBearingFact`.
  - `load_synthesis_golden(path: str | Path) -> list[SynthesisClusterEntry]`
    — raises `ValueError` on malformed rows (strict).
- `alexandria.llm`:
  - `LLMClient(base_url="http://127.0.0.1:20128/v1", model="omni-claude-sonnet",
    api_key_env="ALEXANDRIA_LLM_KEY", timeout=120, max_retries=4,
    base_delay=2.0, min_interval=0.0)` with `complete(self, system: str,
    user: str, temperature: float = 0.0) -> str`. It raises `LLMError` on
    transport/HTTP errors and on refused model+temp combos (do not fight the
    temperature=0 guard — the graders below are `claude-fable-5` and
    `deepseek-v4-pro`, both safe at 0).
  - `ScriptedClient(responses: list[str], calls: list[tuple[str, str]])` with
    the same `complete` signature — replays canned responses in order, raises
    `LLMError("ScriptedClient exhausted")` when empty.
  - `LLMError(RuntimeError)`.
- `alexandria.corpus.split_frontmatter(text: str) -> tuple[dict | None, str]`
  — returns `(frontmatter, body)`; used to strip frontmatter when grading the
  rendered body (frontmatter is metadata, not reader-visible prose).
- `alexandria.synthesis.pipeline.run_pipeline(engine, topic_query, *, ...)`
  — the page-generation driver's backend (see §4). Signature and six LLM
  client params are already tested; do not re-test its internals beyond the
  two smoke tests in §6.
- The real search engine builder used by the driver:
  `alexandria.cli._build_search_engine(config, corpus)` and
  `alexandria.config.load_config(corpus_override=...)` (see how the phase-2
  core's live verification wired them).

---

## 3. Module: `src/alexandria/eval/synthesis_fact_recall.py`

One module, all logic, no I/O beyond what callers pass in. Mirror the shape
of `src/alexandria/eval/gather_completeness.py` (frozen dataclasses, explicit
`passes_gate`, errors recorded never dropped).

### 3.1 Prompt contract

`GRADER_SYSTEM` (module constant) must state, in prose:

- Grade only what a reader sees in the rendered page body; a citation alone is
  not coverage; do not infer unstated facts.
- A general statement counts as covering a fact only if it preserves every
  load-bearing qualifier of that fact (actor, event, cause, chronology,
  numeric threshold, outcome).
- Return ONLY valid JSON: `{"facts":[{"id":"<id>","covered":true,"evidence":"<verbatim page span>"} | {"id":"<id>","covered":false,"evidence":""}]}`.
- Every fact id must appear **exactly once**.

`build_fact_recall_prompt(page_text: str, facts: Sequence[LoadBearingFact])
-> tuple[str, str]` returns `(GRADER_SYSTEM, user_prompt)`; the user prompt
embeds `<golden_facts>` (JSON with id/text) and `<page>` (the page body).

### 3.2 Strict parsing

`parse_fact_recall_response(raw: str, expected_ids: tuple[str, ...]) ->
tuple[FactVerdict, ...]` — raises `LLMError` (message names the violation) on
ANY of: invalid JSON; `facts` missing or not a list; a fact missing `id` or
`covered`; `id` not in `expected_ids`; duplicate `id`; missing ids (set
mismatch); `covered` not a `bool`; `evidence` not a `str`; `covered is True`
with empty `evidence`; `covered is False` with non-empty `evidence`. No
partial acceptance, ever — this is the "eval that cannot fail correctly is
worse than no eval" guard.

### 3.3 Verdicts, results, agreement

```python
GATE_THRESHOLD = 0.90

@dataclass(frozen=True)
class FactVerdict:
    fact_id: str
    covered: bool
    evidence: str          # verbatim quoted page span when covered, else ""
    error: str | None = None

@dataclass(frozen=True)
class FactRecallResult:
    model: str
    verdicts: tuple[FactVerdict, ...]
    recall: float          # covered_count / len(verdicts); 0.0 if empty
    errors: tuple[str, ...]
    @property
    def passed(self) -> bool: ...   # recall >= GATE_THRESHOLD and not errors

@dataclass(frozen=True)
class FactRecallAgreement:
    result_a: FactRecallResult
    result_b: FactRecallResult
    consensus_covered: tuple[str, ...]   # ids BOTH graders marked covered
    contested_ids: tuple[str, ...]       # ids on which they disagree
    consensus_recall: float              # len(consensus_covered) / n
    union_recall: float                  # len(a_covered | b_covered) / n
    @property
    def passed(self) -> bool: ...        # consensus_recall >= GATE_THRESHOLD and no errors on either side
```

### 3.4 Grading functions

- `grade_fact_recall(llm, page_text: str, facts: Sequence[LoadBearingFact],
  model: str | None = None) -> FactRecallResult` — default `model` is
  `str(getattr(llm, "model", "scripted"))`; one call, strict parse, recall
  from the parsed verdicts; `LLMError` propagates (the caller records it —
  never a silent pass).
- `grade_fact_recall_twice(llm_a, llm_b, page_text, facts, model_a=None,
  model_b=None) -> FactRecallAgreement` — two independent graders; per-fact
  consensus = both say covered; disagreement → `contested_ids`; either
  grader's error propagates (never fall back to one side).
- `passes_gate(recall: float) -> bool` — `recall >= GATE_THRESHOLD`, named so
  the threshold lives in exactly one place (mirror `gather_completeness`).
- `classify_miss(fact: LoadBearingFact, gathered_doc_ids: set[str]) -> str` —
  returns `"evidence_not_gathered"` when no `supported_by` doc is in
  `gathered_doc_ids`, else `"evidence_gathered_but_omitted"`. Pure
  deterministic doc-id membership; no LLM.

### 3.5 Whole-eval orchestration (offline-testable)

```python
@dataclass(frozen=True)
class ClusterFactRecall:
    cluster_id: str
    topic: str
    agreement: FactRecallAgreement
    contested_ids: tuple[str, ...]
    consensus_recall: float
    union_recall: float
    recall_a: float
    recall_b: float
    errors: tuple[str, ...]
    miss_taxonomy: tuple[dict[str, str], ...]  # per consensus-missed fact:
        # {"fact_id", "classification"} plus original fact text

@dataclass(frozen=True)
class FactRecallReport:
    clusters: tuple[ClusterFactRecall, ...]
    total_facts: int
    pooled_consensus_recall: float    # sum(len(consensus_covered)) / total_facts
    pooled_union_recall: float
    pooled_recall_a: float
    pooled_recall_b: float
    contested_count: int
    gate: bool                         # pooled_consensus_recall >= GATE_THRESHOLD
    timestamp: str                     # UTC ISO
    git_sha: str
    config: dict[str, object]          # model names, k, page/gather dirs
```

`run_fact_recall_eval(entries: Sequence[SynthesisClusterEntry], page_dir:
str | Path, gather_dir: str | Path, llm_a, llm_b, *, model_a: str | None =
None, model_b: str | None = None) -> FactRecallReport`:

- For each entry in order: read `page_dir/<entry.id>.md` (missing file →
  an error row with `errors=("page missing", ...)`, never skipped silently);
  strip frontmatter via `split_frontmatter` and grade the body; read
  `gather_dir/<entry.id>.gather.json` for `gathered_doc_ids` (missing sidecar
  → classification falls back to `evidence_not_gathered` for consensus-missed
  facts, recorded in `errors` as `"gather sidecar missing"`).
- Per-fact rows in `miss_taxonomy` come ONLY from consensus-missed facts
  (both graders say not covered); contested facts are listed in
  `contested_ids` and must be manually adjudicated — the report does not
  resolve them.
- `git_sha` via `subprocess.run(["git", "rev-parse", "HEAD"], ...)` in the
  repo root (same pattern as `eval/runner._git_sha`); failures yield
  `"unknown"`.
- `config` carries the two grader model names, `k` (unused by this module,
  but the driver records it for provenance), and the page/gather dir names.

---

## 4. Measurement driver: `scripts/synthesize-golden-pages.py`

Purpose: produce the frozen page set the evaluator consumes. Serial,
side-effect-free, isolated outputs — explicitly NOT the full-sweep
orchestrator (that is a separate work order; do not build scheduling,
clustering, dedup, or persistent gather state here).

Args (argparse): `--golden` (default `~/alexandria-corpus/.alexandria/golden/
synthesis-clusters-v1.jsonl`), `--out` (required; created if missing),
`--limit N` (optional smoke cap), and model overrides with these defaults:
`--gather-model claude-sonnet-5 --writer-model claude-sonnet-5
--repair-model claude-sonnet-5 --audit-model claude-fable-5
--coverage-a claude-fable-5 --coverage-b deepseek-v4-pro --seed-k 8`.

Behavior per cluster, in file order, serial:

1. Build the engine once up front (via `_build_search_engine` +
   `load_config(corpus_override=...)` against `~/alexandria-corpus`), warm up
   with one `search("warmup")`.
2. Call `run_pipeline(engine, cluster.topic, ..., corpus_root=out)` with
   `min_interval=0.5` set on each `LLMClient` (be a good citizen), and
   `writer_model` = the writer model name.
3. Write:
   - `out/pages/<cluster.id>.md` — copy of the emitted page file
     (`out/wiki/<slug>.md`, `slug = slugify(cluster.topic)`).
   - `out/pages/<cluster.id>.skip-log.json` — copy of the emitted skip log.
   - `out/gather/<cluster.id>.gather.json` — `{"cluster_id", "topic",
     "gathered_doc_ids": [unique doc ids in result.gathered.chunks],
     "gathered_chunk_count", "round_one_count", "round_two_count",
     "follow_up_queries", "repair_iterations", "native_passed",
     "emitted", "timestamp"}`.
4. A pipeline that fails (`emitted is False`) is DATA, not an exception: still
   write the gather sidecar with `emitted: false`, `native_passed: false`,
   and the failing verdict fields (`failed_claim_ids`, `failing_skip_ids`),
   then continue to the next cluster. Never abort the batch on one bad page.
5. Print a per-cluster one-line summary and the total wall time.

The driver is NOT run during implementation (offline-only rule, §8). It exists
for the maintainer's live run after merge.

---

## 5. Evaluator CLI: `scripts/eval-synthesis-fact-recall.py`

Thin wrapper over the module — no logic beyond arg parsing and printing.

Args: `--pages` (required dir), `--gather` (required dir), `--golden`
(default as §4), `--model-a claude-fable-5 --model-b deepseek-v4-pro`,
`--output` (default `docs/calibration/synthesis-fact-recall-v1-<UTC ts>.json`).

Prints: per-cluster table (recall_a, recall_b, consensus_recall, union_recall,
contested count, errors), the pooled numbers, the gate verdict
(`PASS`/`FAIL` with the ≥ 90% threshold), and the contested-id list for manual
adjudication. Writes the full `FactRecallReport` (plus per-fact rows with
evidence spans and miss classifications) to `--output` as JSON — a number that
lives only in terminal output is not a measured number.

---

## 6. Tests (offline, ScriptedClient only — mandatory, TDD)

Write tests FIRST, confirm they fail, then implement. All under
`tests/test_synthesis_fact_recall.py` and `tests/test_synthesize_golden_pages.py`,
reusing the `FakeEngine`/`Result`/`ScriptedClient` patterns already in
`tests/test_eval_runner.py` and `tests/test_synthesis_pipeline.py`. No network,
no real models, no golden-file reads from the private corpus — use synthetic
ids like `"cluster-a"`, `"fact-1"` and pages as plain strings.

Module tests:

1. `test_parse_valid_response_with_exact_ids_and_evidence`
2. `test_parse_rejects_missing_fact_id`
3. `test_parse_rejects_duplicate_fact_id`
4. `test_parse_rejects_unknown_fact_id`
5. `test_parse_rejects_non_bool_covered`
6. `test_parse_rejects_covered_without_evidence`  ← the hallucination guard
7. `test_parse_rejects_not_covered_with_evidence`
8. `test_parse_rejects_invalid_json`
9. `test_parse_rejects_facts_not_a_list`
10. `test_grade_fact_recall_computes_recall_and_records_model`
11. `test_grade_fact_recall_defaults_model_from_client`
12. `test_grade_fact_recall_propagates_malformed_response`  ← never a silent pass
13. `test_twice_agrees_covered`
14. `test_twice_agrees_not_covered`
15. `test_twice_disagreement_marks_contested_and_lowers_consensus_recall`
16. `test_twice_preserves_both_individual_verdicts_and_evidence`
17. `test_twice_propagates_either_grader_error`
18. `test_passes_gate_at_boundary`  (0.90 passes, 0.899 fails)
19. `test_classify_miss_when_evidence_gathered_but_omitted`
20. `test_classify_miss_when_evidence_not_gathered`
21. `test_build_prompt_embeds_every_fact_id_and_the_page` (sanity, mirrors the
    coverage prompt tests)
22. `test_run_fact_recall_eval_reports_consensus_and_contested_per_cluster`
    (two tmp clusters, scripted graders, assert pooled math + gate)
23. `test_run_fact_recall_eval_records_missing_page_as_error` (never skipped)
24. `test_run_fact_recall_eval_missing_gather_sidecar_falls_back_and_records`

Driver tests (thin, offline):

25. `test_driver_writes_page_and_gather_sidecar` (FakeEngine + ScriptedClients,
    tmp out dir; assert `pages/<id>.md`, `gather/<id>.gather.json` with the
    expected `gathered_doc_ids`)
26. `test_driver_records_failed_pipeline_without_aborting_batch` (cluster 2
    fails → its sidecar has `emitted: false`, cluster 1's files still exist)

---

## 7. Constraints

- TDD only; every test runs offline via `ScriptedClient`. No real API calls
  during implementation.
- Do NOT modify `src/alexandria/coverage.py` (frozen without explicit sign-off)
  or anything under `src/alexandria/synthesis/`. This work order only ADDS an
  evaluation surface.
- No new dependencies; no `pyproject.toml` changes.
- `.venv/bin/python`, never system python.
- Leak scanner: `.venv/bin/python scripts/precommit-scan.py --all` must be
  clean before every commit. Do not write private corpus cluster ids or
  project codenames into repo files or fixtures — synthetic ids only.
- Commit on `phase2-fact-recall-eval` only, feature-sized commits.

## 8. Verification before reporting done (offline only)

```bash
.venv/bin/python -m pytest tests/ -q        # all green, no skips masking failures
.venv/bin/python scripts/precommit-scan.py --all
```

Expected: 334 + 26 = **360 passing**. No live LLM calls, and scripts
`synthesize-golden-pages.py` / `eval-synthesis-fact-recall.py` are NOT executed
against real models during implementation — the maintainer runs the live
8-cluster pass after this branch is reviewed and merged.

## 9. Report back

- Modules built + test counts (name the 26 new tests; confirm 360 total).
- Which test proves a malformed/missing/duplicate fact-id grader response
  fails loudly (name it, say what it asserts).
- Which test proves grader disagreement becomes `contested` and lowers
  consensus recall (name it).
- Which test proves `classify_miss` separates gathered-but-omitted from
  never-gathered evidence.
- Any spec deviation, and why.
- Anything in §2–§6 that bit you anyway.
- Explicit confirmation that no live LLM call was made and neither new script
  was executed against real models.
