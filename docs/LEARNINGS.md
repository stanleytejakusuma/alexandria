# Learnings

## 2026-08-13 — arc: alexandria-agents-md

Single-document arc (write `AGENTS.md` from a cold read). Few genuine
learnings beyond what's now written into `AGENTS.md` itself; keeping only
what a future session would otherwise have to rediscover by trial.

- **Trap: bare `.venv/bin/pytest` fails full-suite collection** with
  `ModuleNotFoundError: No module named 'tests'`; `python -m pytest`
  collects all 654 tests cleanly. Reproduced live, not inferred — use
  `python -m pytest`, not the bare binary.
- **Unverified:** the arc only ran `--collect-only` on the 654-test suite,
  never a full run to completion, so current pass/fail count is unknown.
  Don't assume green.
- **`extensions/pi/` is inert by design, not oversight.** Its README says
  "Do not install yet" — gated behind `docs/SPEC-phase3-harness.md`
  requiring a blinded side-by-side eval gate plus explicit sign-off. The
  arc flagged this as a constraint it had initially missed on first pass,
  i.e. easy to skim past.
- **Doc-style convention confirmed as real, not one-off:** ✅ measured /
  ⏳ expected tagging (from `docs/CASE-STUDY-a-day-with-alexandria.md`) —
  a number is only "measured" if actually run; projections get "expected"
  plus their basis stated. Applies to any future report/case-study written
  in this repo.
- **Decision-doc naming is meaningful, not arbitrary:** singular
  `DECISION-<topic>-<date>.md` = one specific measured verdict; plural
  `DECISIONS-<topic>.md` = a bundle of related decisions under one
  question. Both require a `Date:`/`Status:` line and the rationale, not
  just the call.
- **Commit style is not Conventional Commits** — no `feat(scope):`
  parens. Dominant pattern from full `git log` histogram is
  `<component-or-type>: <description>`, prefix is whichever module/concern
  the commit touches (`docs:`, `fix:`, `feat:`, then narrow prefixes like
  `driver:`, `sync:`, `search:`).

No user corrections in this transcript — the "user" turns were the
brief's own checklist items being worked through sequentially, not a human
steering mid-task. No dead ends recorded either.
