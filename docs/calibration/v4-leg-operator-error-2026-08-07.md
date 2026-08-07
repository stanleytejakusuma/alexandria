# v4 leg: kill by operator error (2026-08-07) — CORRECTED RECORD

Status: the original v4 leg was **healthy and was killed by operator error
(timestamp timezone misread)**. No infra failure occurred. The correction
below supersedes the initial (wrong) incident report.

## What actually happened

The driver logs timestamps in **UTC** (`datetime.now(timezone.utc)` in
scripts/synthesize-golden-pages.py); the operator read them as local WIB
(UTC+7), producing a phantom 8-hour stall.

| Event | Log (UTC) | Real local time (WIB) |
|---|---|---|
| leg START (cluster 1) | 13:15:56 | 20:15:56 |
| attempt 1/3 | 13:16:08 | 20:16:08 |
| attempt 2/3 | 13:36:30 | 20:36:30 |
| attempt 3/3 | 14:05:02 | 21:05:02 |
| inspection ("stalled?") | — | 21:10 — attempt 3, 5 min in, in flight |
| leg killed by operator | — | ~21:18 |

Process evidence consistent with a healthy 56-minute leg: elapsed 56:16 at
inspection, 1735/1735 samples in `poll()` = a normal in-flight LLM call,
pages-dir mtime 20:15 = actual start, watchdog had not fired because its
silence timer (~20 min elapsed of a 60 min budget) had not expired.

## Corrections applied

- llm.py `_open_with_deadline` (commit 2eba4de): **kept** — a genuine
  hardening for the real stall class (silent gateway → unbounded 120s
  retry cycle), but its commit message's "v4 infra failure" justification
  is wrong; it was proactive hardening, not a fix for this leg.
- Runner (silence notices, `caffeinate -dimsu`, 1800s silence kill): kept.
- This document replaces the previous v4-infra-failure-2026-08-07.md
  narrative (timeline + root cause), which was wrong.

## Disposition

- The re-run (v4b, started 21:15:41 WIB, same HEAD) is **the v4 leg** —
  it is not a re-run of a botched leg, it is the leg itself, restarted
  once at 60 minutes for a non-measurement reason.
- The loop-termination contract is unaffected: no infra failure consumed
  the budget; v4b is the single v4 leg.

## Fix to prevent recurrence

Driver should print an explicit timezone marker (`%H:%M:%S UTC`) or local
time in its attempt heartbeats so log timestamps cannot be misread again.
(Pending — lands after v4b completes to keep the measured HEAD pinned.)

## Follow-up fix (same night, committed)

The v4b relaunch hit a SECOND, real bug: the watchdog SIGTERM'd cluster-1
attempt 2 at the 30-min silence mark, and the driver's crash guard recorded
the cluster as `driver_crash` and MOVED ON -- attempt 3 never ran (the guard
wraps the whole cluster; a signal unwinds all attempts). Fixed in
scripts/synthesize-golden-pages.py: the per-attempt loop now catches
`SystemExit` with a "terminated by" message (the signal handler's raise) and
burns only that attempt; genuine `sys.exit()` calls still propagate.
Regression test `test_driver_burns_attempt_on_signal_and_retries` (428
passed). The 30-min watchdog budget itself was also wrong for round-4
clause-graded attempts (longest observed attempt is now >30 min); the leg
relaunch uses the v3-proven 3600s budget.

## Follow-up fix (second root cause, same night, committed d7e4cfa)

The REAL reason clusters 1-2 failed on the relaunch (the 503/entailment
narrative was a symptom): the judge's coverage grading called
`llm.complete()` at the default temperature=0, and llm.py REFUSES
fast-tier models (gpt-5.6-terra) at temperature=0 — so coverage-b could
never produce a verdict, coverage_passed=False, and no cluster could ever
emit. Fix: judge.py's grade_skip_twice call and all three fact-recall
evaluator complete() calls now forward temperature=0.1 (the llm.py guard's
own documented escape hatch). Regression test
`test_judge_grades_coverage_at_nonzero_temperature_for_fast_tier_models`
asserts the coverage path records temperatures [0.1, 0.1]. 429 passed.
Leg relaunched (job alx-v4-leg-1786125, HEAD d7e4cfa, 00:24 WIB).
