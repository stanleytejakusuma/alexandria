# Alexandria overnight handoff — 2026-09-02

## Authority and safety boundary

Stanley authorized overnight autonomous work on Alexandria while the Mac is
charged. Continue reversible engine/docs/test work; queue any action needing a
fresh human decision. Do not touch capital systems, vault secrets, or deploy
services. Real-corpus writes are allowed only where already authorized by the
weekly-loop proof; do not start a new ingestion, reindex, promote, or release
cutover merely for experimentation.

## Current objective

Make the weekly loop reliable end-to-end, then work through independent
Alexandria backlog items while keeping this handoff current.

## Loop incident and work shipped locally

- Root cause: the engine venv had an editable `.pth` pointing into a reaped
  `/private/tmp` worktree. `python -m alexandria.cli` failed while the console
  entry point still worked; independent best-effort clauses let the script
  snapshot after sync/index failures.
- Engine `main` local commits: `5b61095`, `ee01e90`, `2026321`, `4eac50c`,
  `18fa929`. They are not pushed yet.
- Live bounded proof before the final Sol remediation: exit 0 in 13m35s;
  generation 250→251; 981 docs added; 5/5 newest indexed; newest Pi-session
  source retrieved rank 1; 3,782-file corpus snapshot committed.
- Tests: full suite passed 1159 before final shell-only remediation; focused
  loop suites pass 32 tests after it.
- Current shell defenses: console script preflight; timeout config validation;
  hard timeout + kill grace; bounded Pi-session batch; required sync/index
  failure skips snapshot; weekly ablation disabled by default.

## Open loop safety decision — chosen overnight approach

Use a disposable staging corpus for the weekly loop. A hard timeout can
interrupt active in-place index writes; staging keeps partial state outside the
canonical corpus. Design and test the staging/publish protocol before changing
the real LaunchAgent. No real-corpus staging run without a fresh verification
plan.

A first independent repair is complete locally: `39464b2 fix(corpus):
atomically replace synced documents`. `Doc.write` now writes+fsyncs a
same-directory temporary file before `os.replace`; its regression test proves
a replacement failure retains the prior Markdown byte-for-byte and leaves no
temporary residue. This removes torn source documents from the timeout
failure surface, but does not make a partly completed batch/index publish safe.

## Active work items

1. **Loop staging protocol**: inspect corpus source/state/index layout and
   design the smallest same-filesystem staging/publish strategy. It must prove
   a timed-out run cannot publish partial source/index state or a snapshot.
   **Current conclusion:** this is not a safe shell-only change. The canonical
   root combines Git-tracked `sources/`/`wiki/`, atomic per-file state, and an
   independently activated index directory. There is no one pointer that
   commits all four coherently. A real implementation needs either a
   root-level corpus-pointer/cutover design or a committed staged-publisher
   interface; do not use `rsync`, directory-by-directory moves, or best-effort
   Git commits as a substitute.
2. **Release sealing S1**: blocked separately by LanceDB lacking a verified
   immutable/read-only local connection. Do not merge the partial branch until
   a reader/snapshot decision is made.
3. **Backlog filed**: #57 bounded Pi-session ingestion liveness; #58 answer
   progress/budget; #59 release seal lifecycle; #60 durable worktree placement;
   #61 memory capacity; #62 serve disconnect noise/slow health.
4. **Other discovered work**: #83 Pi-session trigger, #84 loop, #85 memory,
   #86 worktree placement, #87 answer progress are session todos; reconcile
   them with `docs/BACKLOG.md` as they are investigated.

## Verification discipline

- Every worktree command starts with its explicit `cd`; Pi's default cwd is
  the main repo, not an arbitrary worktree.
- Never trust a test command until its path is verified; a prior false-green
  ran the base repo rather than the S1 worktree.
- Run `unset ALEXANDRIA_EMBED_PROVIDER && PYTHONPATH=src .venv/bin/python -m pytest tests/ -q`,
  `PYTHONPATH=src .venv/bin/python scripts/precommit-scan.py --all`, and
  `git diff --check` before claiming an engine change done.
- Main has unrelated peer-session modifications in `AGENTS.md`,
  `docs/RESUME-PROMPT.md`, `docs/STATE-OF-PLAY-2026-08-13.md`, and untracked
  `.playwright-cli/`; never stage/revert them.

## Morning report should include

- What staged-loop design was implemented and its crash/timeout proof.
- Every commit, test count, and whether it was pushed.
- Any blocked decisions, especially the Lance sealed-reader choice.
- The exact real-corpus action (if any) still needing confirmation.
