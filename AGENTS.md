# AGENTS.md — alexandria (the engine repo)

This is the **engine** repo (`~/codebase/alexandria`, package `src/alexandria/`,
654 tests as of HEAD `85b8c21`). It is not the corpus. The corpus this engine
indexes/serves lives at `~/alexandria-corpus`, a separate repo, and is
**out of scope for edits from here** — see "The corpus is not this repo" below.

## Read first

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
spec's prose alone.

## The corpus is not this repo

`~/alexandria-corpus` is a separate git repo this engine operates on. Corpus
content (`.alexandria/`, `/sources/`, `/wiki/`, `*.lancedb/`, `*.sqlite`) is
git-ignored here and never committed to this repo either way — see
`.gitignore`. Do not run any command from this repo that mutates that corpus
(`sync`, `index`, `--enrich`, `promote`, `restore`) unless the task explicitly
calls for it against a corpus you're meant to change. Reading/inspecting the
engine's own code and tests is always fine.

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
python -m pytest            # not bare .venv/bin/pytest
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
  on `PATH`.

## What I could not fully verify

- `docs/SPEC-phase2-eval.md` has no `Status:` header — grep it before citing
  as built or paper.
- Full-suite runtime and current pass/fail count were not re-run in full here
  (only `--collect-only`, which returned 654 tests, matching STATE-OF-PLAY's
  claim). Don't assume all 654 currently pass without running them.
- Contents of `.leakpatterns.local` and the pre-commit hook body were
  deliberately not dumped here (private / would itself be a leak-scanner
  target) — read them locally if you need the actual patterns.
