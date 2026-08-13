# AGENTS.md — alexandria (the engine repo)

This is the **engine** repo (`~/codebase/alexandria`, package `src/alexandria/`,
654 tests as of HEAD `85b8c21`). It is not the corpus. The corpus this engine
indexes/serves lives at `~/alexandria-corpus`, a separate repo, and is
**out of scope for edits from here** — see "The corpus is not this repo" below.

You are probably here as a fresh session with a `docs/WORK-ORDER-*.md` file to
implement and nothing else. This doc is written for exactly that: no inherited
conversation, no assumed familiarity with the project's history. Read this
file in full before opening the work order.

## How work happens here: the WORK ORDER protocol

Feature work in this repo ships as a **WORK ORDER**: a self-contained spec at
`docs/WORK-ORDER-<name>.md`, each implemented on its own branch, by a session
that starts cold (no memory of any other work order). If you were handed one,
it is the primary source of truth for *what* to build — this file covers the
*how*, the parts every work order assumes you already know so it doesn't have
to repeat them.

**Every WORK-ORDER-*.md observed in this repo opens with the same header
contract — read it literally, don't infer:**
```
**Repo:** `~/codebase/alexandria` · **Branch:** `<branch-name>`, not `main`
**Venv:** `.venv` (`.venv/bin/python`, never system python) · Python 3.12
**Baseline:** `main` at N passing tests. Do not regress it.
```
- Work on the **named branch**, never directly on `main`. The branch name is
  stated in the header verbatim — don't derive it from the filename (it
  usually matches the `WORK-ORDER-` suffix, e.g. `phase2-synthesis-core`, but
  `WORK-ORDER-phase1-eval-harness.md` is branch `eval-harness`, not
  `phase1-eval-harness` — the header is authoritative, not the filename).
  Confirmed existing branches: `eval-harness`, `phase1-retrieval`,
  `phase2-clustering`, `phase2-fact-recall-eval`, `phase2-full-sweep`,
  `phase2-synthesis-core` (`git branch -a`). All are fully merged into `main`
  already (0 commits ahead, many behind) — `main` history is linear, no merge
  commits observed; work order branches land on `main` as ordinary commits,
  and the branch ref is left in place afterward rather than deleted.
- **Always `.venv/bin/python`, never bare `python`/`python3`.** Stated in
  every work order's header, not optional.
- **The baseline test count is a regression gate, not trivia.** Run the suite
  before you start, confirm it matches the stated baseline, and don't drop
  below it at any commit.

**The body of a work order follows a consistent numbered shape** (confirmed
across `WORK-ORDER-phase2-synthesis-core.md`; expect variation but not a
different skeleton):
0. Why this exists / why scoped this way — read this before deliverables, it
   tells you what's deliberately excluded and why, so you don't "helpfully"
   build it anyway.
1. Where things live — code goes in this repo; corpus/wiki/ground-truth data
   is private and lives in `~/alexandria-corpus`, **never** in this repo.
2. What already exists — call these, do not rebuild them. Read this list
   before writing anything; duplicating an existing module is a real failure
   mode here, not a hypothetical one.
3. The pipeline/shape this work order builds.
4. Deliverables — concrete modules/files, one subsection each.
5. THE TEST THAT MATTERS MOST (yes, shouted like that in the source) — the
   single test that would catch this work order's most dangerous failure
   mode if it regressed. Treat it as non-negotiable.
6. Constraints — always includes: TDD (tests before implementation, suite
   green at every commit); all LLM/retrieval calls in tests must be **offline**
   via the existing `ScriptedClient` (for LLM calls) / `FakeEngine` (for
   retrieval) patterns already used in `tests/test_eval_runner.py` and
   `tests/test_gather_completeness.py` — do not invent a third mocking
   pattern; and an explicit **do-not-modify list** of files whose behavior is
   load-bearing for reasons outside this work order's scope (e.g. `llm.py`'s
   temperature=0 refuse-guard on certain models, a confirmed live gateway bug
   workaround — not incidental code). If a work order tells you not to touch
   something and you believe you must, **stop and report why before touching
   it** — do not route around the instruction.
7. Known traps — prior, hard-won gotchas specific to that work order's area.
8. Out of scope — do not build. Read this as seriously as the deliverables
   list; scope creep here has been explicitly called out as a failure mode
   ("do not guess at this; a later work order will cover it").
9. Verification before reporting done — always includes, verbatim or close to it:
   ```bash
   .venv/bin/python -m pytest tests/ -q       # all green, no skips masking failures
   .venv/bin/python scripts/precommit-scan.py --all
   ```
   plus, where relevant, an end-to-end run against the real corpus with real
   (not scripted) dependencies, with honestly-reported numbers — including if
   they're worse than an isolated calibration predicted.
10. Report back — a fixed checklist: modules built + test counts, proof
    points for the §5 test, real end-to-end numbers where applicable, any
    spec deviation and why, and anything in the constraints/traps sections
    that bit you anyway. Write this even if nobody is watching; it's the
    handoff for the next cold session.

**If there is no work order yet and you need to know what to build next:**
`docs/BACKLOG.md` is the maintained, prioritized queue (a "Top 10 — the
critical path" table plus lower-priority items), each row citing the spec
section and file:line it traces to. Don't invent work; pull from there or
from a `SPEC-*.md`, and if you write a new WORK-ORDER-*.md yourself, match the
header contract and numbered shape above so the next cold session can trust it
the same way.

## Read first

- The work order you were handed, if any (see above).
- `docs/STATE-OF-PLAY-2026-08-13.md` — most recent handoff: what's built, what's
  paper, known bugs fixed, operational traps. Written specifically to survive
  context loss; read it before touching anything non-trivial.
- `docs/DECISION-enrichment-2026-08-11.md` — why `--enrich` is dropped, with the
  measured numbers. Don't re-litigate without new measurement.
- `docs/ARCHITECTURE.md`, `README.md` — design intent and pitch.
- SPEC-*.md status headers can be **stale relative to reality** — e.g.
  `SPEC-write-path-and-serve.md` still says "Status: proposed" in its own
  header, but STATE-OF-PLAY confirms it's fully implemented with tests behind
  every gate. Trust STATE-OF-PLAY / actual code over a spec's self-reported
  status line.

## BUILT vs PAPER — verify before describing anything as working

Specs in `docs/SPEC-*.md` may be fully written and **not implemented**. Confirmed
current statuses (grep `^Status:` / `^**Status:**` in each file before relying
on this table — it will drift):

| Spec | Status |
|---|---|
| `SPEC-write-path-and-serve.md` | header says "proposed", but **built** — confirm via STATE-OF-PLAY, not the header |
| `SPEC-data-model-and-ambient-capture.md` | revised after 2 adversarial review rounds, review budget spent |
| `SPEC-phase3-harness.md` | revised post-Red-review |
| `SPEC-multi-tenant-and-learning-loop.md` | **draft, unimplemented** |
| `SPEC-versioning-and-supersession.md` | **accepted, unimplemented** |
| `SPEC-phase2-eval.md` | no status header found — check before citing |

Before describing any capability as "working," check the module actually
exists under `src/alexandria/` and has a passing test — don't infer from a
spec's prose alone. `src/alexandria/` top-level layout for orientation:
`cli.py` (argparse entry), `index/` (chunk/embed/store/manifest/bm25),
`retrieval/` (search/fusion/rerank), `synthesis/` (gather/write/judge/repair/
pipeline/clustering/sweep), `eval/` (golden sets, metrics, runner), `connectors/`
(pi_sessions, journal, md_memory, inbox, knowledge_graph), plus top-level
`pending.py`, `writelock.py`, `promote.py`, `reconcile.py`, `liveness.py`,
`serve.py`, `backup.py`, `enrich.py`, `decay.py`, `migrate.py`, `llm.py`,
`grounding.py`, `coverage.py`, `monitor.py`, `wiki_site.py`.

## The corpus is not this repo

`~/alexandria-corpus` is a separate git repo this engine operates on. Corpus
content (`.alexandria/`, `/sources/`, `/wiki/`, `*.lancedb/`, `*.sqlite`) is
git-ignored here and never committed to this repo either way — see
`.gitignore`. Do not run any command from this repo that mutates that corpus
(`sync`, `index`, `--enrich`, `promote`, `restore`) unless the task explicitly
calls for it against a corpus you're meant to change. Reading/inspecting the
engine's own code and tests is always fine. Tests use synthetic fixtures only
— never real corpus/wiki/ground-truth content — and
`scripts/precommit-scan.py` is a live pre-commit hook that will block a commit
containing it. If it blocks you, **fix the content, do not weaken the
scanner.**

## Hard constraints (verified against code, not just docs)

- **Never pass `--enrich`** to `index`. The flag is real (`alexandria index
  --help` confirms it exists). Measured: −6.1 pts recall@k (0.673 → 0.612),
  −0.027 MRR, across a controlled A/B/C corpus rebuild. See
  `docs/DECISION-enrichment-2026-08-11.md`. The weekly loop
  (`scripts/run-weekly-loop.sh`) was fixed to no longer pass it — don't
  reintroduce it without a new measurement that beats the pre-registered
  bar in that decision doc.
- **`sync` alone does not make anything retrievable.** `sync` (verb list:
  `migrate, sync, remember, promote, reconcile, backup, restore, lint, serve,
  index, search, eval, answer, wiki-site, audit, cache, decay` — confirmed via
  `alexandria --help`) only pulls/distils from a connector into the corpus.
  Nothing is queryable until `index` runs afterward.
- **Never run a corpus index build on the second host** (per STATE-OF-PLAY:
  it carries live capital services and a 45k-chunk CPU embed took that host
  down on 2026-08-11).
- **There is no deletion path.** Anything written to the corpus via this
  engine is permanent — treat any `remember`/`promote`/`sync` against a real
  corpus as one-way.
- **Trust outcomes, not exit codes.** Confirmed pattern from this session's
  own bug list: the weekly loop's own log directory was never `mkdir -p`'d, so
  every `>>` redirect aborted the sync silently while `git commit --allow-empty`
  still succeeded — three days of "success" with a frozen corpus. A step
  reporting success is not evidence it did anything; check the observable
  result (row counts, generation numbers, file mtimes) changed.

## Running tests

```
.venv/bin/python -m pytest tests/ -q        # not bare .venv/bin/pytest
```
Verified: bare `.venv/bin/pytest` (no args, full collection) fails with
`ModuleNotFoundError: No module named 'tests'` on files that do `import
tests.X`; `python -m pytest` (adds cwd to `sys.path`) collects all 654 tests
cleanly. Single-file runs work fine either way — this only bites full-suite
collection.

Also verified: an `ALEXANDRIA_EMBED_PROVIDER=hash` left set in the shell fails
`test_mlx_is_the_default_embed_provider` — not a real regression, just don't
leave that env var exported. Default provider is `mlx` (Apple Silicon only);
values are `local`, `mlx`, `hash`. The embedding cache key includes the model
name, so switching providers correctly invalidates cached vectors — but an
MLX-built index cannot be copied to a Linux host (different vector space,
needs full re-embed).

The production test suite runs against the **SQLite fallback** store, not
LanceDB, by design (network-free). LanceDB is installed; tests exercising real
LanceDB behavior construct it explicitly.

CI (`.github/workflows/ci.yml`) runs on `macos-latest` and mirrors the same
two commands as a work order's §9 verification: `pytest tests/ -q` and
`scripts/precommit-scan.py --all`. `ruff` is listed as a dev dependency
(`pyproject.toml`) but is **not** wired into CI or a pre-commit hook — treat
it as available, not enforced.

## Committing

- Pre-commit runs a leak scan (`.leakpatterns.local`, private — never commit
  this file's contents) then an eval gate (60–90s). Give Bash calls around
  commits `timeout: 120`+ or they die mid-run.
- **Use `git commit -F <tmpfile>`** for multi-paragraph commit messages.
  Heredoc inside `-m "$(cat <<'EOF'...)"` breaks on embedded backticks/parens.
- The leak scanner only sees **staged** files — `git add -N` scans zero files.
- A zero-width joiner (U+200D) defeats the scanner while still leaking a name
  to a human reader. Strip with `perl -i -CSD -pe 's/\x{200D}//g'` before
  staging if you suspect one crept in.
- Private names (hosts, agent identities) must not enter this repo. Keep them
  in the private companion doc outside it, not here.
- Existing commit style is plain conventional-ish prefixes observed in `git
  log`: `feat:`, `fix:`, `docs:`, `eval:`, `search:`, `sync:`, `loop:`,
  sometimes with a `(BACKLOG #N)` or spec-section reference in the subject.
  No enforced format, but match the pattern rather than inventing a new one.

## CLI surface (confirmed via `alexandria --help` / `<verb> --help`)

`alexandria` is on `PATH` (`~/.local/bin/alexandria`, symlinked to this repo's
`.venv/bin/alexandria`; entry point `alexandria.cli:app` per `pyproject.toml`).
Verbs: `migrate, sync, remember, promote, reconcile, backup, restore, lint,
serve, index, search, eval, answer, wiki-site, audit, cache, decay`.

- `index` flags confirmed: `--rebuild` (recreate index tables, retain
  embedding cache), `--backfill-manifest` (one-time fix for pre-manifest
  indexes), `--limit`, `--enrich` (documents to enrich; **do not use**), plus
  provider/model config.
- `backup` never backs up the rebuildable indexes, only `.alexandria` state.
- `serve` is a stdlib `http.server` exposing `/health /search /answer
  /remember` — no external web framework dependency.

## Dependencies / environment

- `pyproject.toml`: runtime deps are `lancedb>=0.25`, `pyyaml>=6.0`,
  `sentence-transformers>=5.0`. Dev: `pytest>=8.0`, `ruff>=0.6`.
- argparse, not a CLI framework — deliberate, per the CLI's own `--help`
  epilog ("the surface is small, and stdlib means one fewer dependency for a
  tool whose whole pitch is that your data outlives the engine").
- `.venv/` exists locally; activate with `source .venv/bin/activate` or just
  prefix commands with `.venv/bin/python3 -m ...` / use the `alexandria` shim
  on `PATH`. Work orders standardize on the explicit `.venv/bin/python` prefix
  rather than assuming an activated shell — match that in scripts/CI-facing
  commands.

## What I could not fully verify

- `docs/SPEC-phase2-eval.md` has no `Status:` header — grep it before citing
  as built or paper.
- Full-suite runtime and current pass/fail count were not re-run in full here
  (only `--collect-only`, which returned 654 tests, matching STATE-OF-PLAY's
  claim). Don't assume all 654 currently pass without running them.
- Contents of `.leakpatterns.local` and the pre-commit hook body were
  deliberately not dumped here (private / would itself be a leak-scanner
  target) — read them locally if you need the actual patterns.
- The exact git mechanism (rebase vs. squash vs. cherry-pick) by which
  work-order branches' commits end up on `main` without a merge commit was
  inferred from `git log --graph`/ahead-behind counts, not confirmed from a
  written policy doc. Whatever you do, don't force-push over another
  branch's history without checking `git log <branch>..main` first.
