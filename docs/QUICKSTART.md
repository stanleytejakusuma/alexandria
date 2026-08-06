# Alexandria quickstart

Alexandria is a personal knowledge engine that **synthesizes instead of
transcribes**: a write-time layer turns raw notes and session history into a
maintained corpus of cited pages, and retrieval runs against *that* — every
claim carries a keyed citation back to its source.

Python ≥ 3.12. No GPU required for day-to-day use (embeddings run locally;
the MLX provider needs an Apple Silicon Mac, the PyTorch provider works
anywhere).

## Install and build an index

```bash
git clone https://github.com/stanleytejakusuma/alexandria
cd alexandria
python -m venv .venv && .venv/bin/pip install -e .
# point at a corpus: any directory of markdown files (frontmatter optional
# for indexing; required for lint-clean corpora -- see docs/DEMO-CORPUS.md)
.venv/bin/alexandria --corpus demo-corpus index
```

## Core verbs

| Command | What it does |
|---|---|
| `alexandria lint` | validate every document against the schema (source vs wiki profiles, required fields, actor convention) |
| `alexandria index` | chunk → embed → hybrid index (BM25 + vectors + rerank) |
| `alexandria search "query"` | hybrid retrieval with trace (`--trace`) |
| `alexandria answer "question"` | synthesize a **cited answer page** (gather → write → judge → repair); needs an LLM gateway (see below) |
| `alexandria wiki-site --wiki DIR --out DIR` | render a wiki dir into a self-contained static site |
| `alexandria eval` | score retrieval against your golden set; `--fail-on-regression` gates tuning changes |
| `alexandria migrate kg-sync VAULT` | one-shot import from an existing markdown vault |
| `alexandria sync pi-sessions` | pull sessions from Pi and distil into corpus documents |

## The synthesis pipeline (the differentiator)

One synthesized page is produced by `run_pipeline` in
`src/alexandria/synthesis/pipeline.py`:

1. **gather** — two bounded retrieval rounds find the candidate source pool
   (and explicitly hunts for *earlier superseded assertions*, the measured
   weak spot of plain retrieval).
2. **write** — a page whose every claim is a load-bearing statement carrying
   citations; chunk accounting is a build error, not a warning: every
   gathered chunk is either cited or logged with a deterministic skip
   predicate (`duplicate_of`, `below_salience`, `out_of_scope`).
3. **judge** — entailment audit (claims must be *in* the evidence) and
   coverage grading (two independent graders), the anti-Goodhart pair.
4. **repair** — failed claims are fixed by finding a real citation or by
   removing the claim; removal is logged and re-triggers both judges (no
   quietly passing by gutting the page), bounded to a fixed iteration count.

## LLM configuration for `answer`

`answer` talks to any OpenAI-compatible gateway:

```bash
export ALEXANDRIA_LLM_KEY="your-gateway-key"
.venv/bin/alexandria --corpus demo-corpus answer \
  "What is the Proxima deal state and what happens next?" \
  --base-url http://127.0.0.1:20128/v1 \
  --llm-model deepseek-v4-pro \
  --grader-a-model openrouter/anthropic/claude-sonnet-5 \
  --grader-b-model deepseek-v4-pro \
  --save-dir /tmp/my-wiki
```

The pipeline's native checks require three independent LLM clients; the
defaults above are the cost-cautious configuration. `answer` never writes
into your private corpus unless you pass `--save-dir`.

## Calibration and honest gates

Every phase gate is a measurement with a recorded number and a manifest,
not a vibe: retrieval is gated on a hand-verified golden set with
recall@k and zero-overlap bands; synthesis is gated on load-bearing-fact
recall (dual-grader consensus) and entailment audit. `scripts/eval-gate.py`
fires the regression net on relevant changes, and CI runs the offline suite
plus the leak scanner on every push. Calibration numbers live in
`docs/calibration/` with the small-n caveats attached.

## Layout

- `src/alexandria/` — the engine: `index/` (chunker, embedders, stores),
  `retrieval/`, `synthesis/` (gather, write, judge, repair, pipeline,
  clustering), `eval/` (golden sets, metrics, the fact-recall evaluator),
  `connectors/`, `cli.py`.
- `scripts/` — measurement drivers (golden-page synthesis, fact-recall
  evaluation, clustering calibration, the one-command measure loop).
- `demo-corpus/` — fictional multi-team sample corpus (see
  `docs/DEMO-CORPUS.md`).
- `docs/` — decision records, work orders, and the calibration story.

## Status

Pre-alpha, phase 2 in progress, building in public. The retrieval stack and
corpus scaffold pass their gates; the synthesis core is under measurement
against the ≥ 90% fact-recall gate. See `README.md` for the phase table and
current evidence.
