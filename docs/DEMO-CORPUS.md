# Demo corpus — Northwind Dynamics

A fictional, public sample corpus for Alexandria's phase-4 fresh-clone test and
the multi-team handoff story: **four teams, one deal, zero private content.**

The scenario: sales (Maya) closes Project Proxima with a logistics software
vendor; BD (Liam) owns the contract and a revenue-share partnership framing;
HR (Priya) gates two new roles on the close; GM (Sofia) tracks it as an anchor
deal and flags the indemnity risk. The pipeline's job is to synthesize a
maintained "Proxima deal status and next steps" page any team can retrieve —
picking up the thread with context instead of starting fresh.

All names, companies, numbers, and dates are fictional. Nothing here is real
infrastructure, real people, or real private data — the corpus is designed to
stay public and leak-scanner-clean.

## Fresh-clone test (phase-4 gate)

```bash
git clone <repo> alexandria && cd alexandria
python -m venv .venv && .venv/bin/pip install -e .
.venv/bin/alexandria --corpus demo-corpus lint
.venv/bin/alexandria --corpus demo-corpus index --limit 0
.venv/bin/alexandria --corpus demo-corpus search "Proxima deal status"
.venv/bin/alexandria --corpus demo-corpus answer "What is the Proxima deal state and what happens next?" --save-dir /tmp/alx-demo-wiki
.venv/bin/alexandria --corpus demo-corpus wiki-site --wiki /tmp/alx-demo-wiki --out /tmp/alx-demo-site
```

(Pass `--base-url`/`--api-key-env` to `answer` for your LLM gateway; the other
commands are fully offline.)
