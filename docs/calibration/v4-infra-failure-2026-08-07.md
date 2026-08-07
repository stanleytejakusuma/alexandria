# v4 leg: infra failure log (2026-08-07)

Status: leg ABORTED (enumerated infra failure — gateway read-stall class;
does not count against the cycle budget, does not extend the loop;
contract §5).

## Timeline

- 13:15:56 START cluster-1 (monorepo-git-migration; HEAD 2a5eb09)
- 13:16:08 attempt 1/3
- 13:36:30 attempt 2/3 (attempt 1 failed cleanly at ~20 min)
- 14:05:02 attempt 3/3 — LAST log line. Nothing after this.
- 14:05–20:15: Mac asleep (lid closed). caffeinate was attached to the
  runner but the runner's watchdog sleeps with the machine; nothing fires
  during sleep, and the python's socket read cannot complete while asleep.
- ~20:15: wake. Python resumes stuck: main thread 100% in
  `sock_recv_into -> internal_select -> poll` (sample 71696, 1735/1735
  samples) — a blocking socket read with no deadline.
- 21:10: leg inspected; 0 sidecars, 0 pages, 21.32s total CPU.
- 21:20: leg terminated (SIGTERM, then cleanup). Elapsed ~8h, no output.

## Root causes (two, both now fixed)

1. **llm.py deadline covers the body read only.** `_read_with_deadline`
   wraps `json.loads(...)` — the response BODY. The HEADER parse happens
   inside `urlopen()` (http.client reads status line + headers via
   `_buffered_readline` before returning the response object) and has no
   deadline. A server that accepts and never answers the request leaves
   the main thread in `poll()` forever. Fix: the deadline thread now wraps
   the entire `urlopen + body-read` sequence, and `urlopen` is always
   called with a non-None socket timeout (fallback 120s).
2. **Watchdog unobservable.** If the runner sleeps (machine sleep) or the
   silence accounting drifts, nothing logs it. Fix: the watchdog now
   appends a notice every 600s of continuing silence
   (`WATCHDOG: silence Ns (pid ...)`), so a non-firing watchdog is itself
   visible in the log; SIGTERM still fires at WATCHDOG_SILENCE (3600s).
   Also: caffeinate `-dimsu` (all sleep classes) attached to the runner.

## Disposition

- Re-run v4 (same HEAD, same clusters, same models) — the aborted leg was
  infra, not a measurement outcome.
- If the re-run hangs on the same class, the llm.py fix is wrong and the
  leg is killed and logged again — the contract's cycle budget is
  consumed only by a *measured* outcome.
