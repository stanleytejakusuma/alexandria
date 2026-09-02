#!/bin/bash
# Alexandria weekly self-learning loop (runs via LaunchAgent, Sun 09:30).
# 1. distil the week's pi-sessions into corpus notes (memory generation)
# 2. index the newly synced docs so retrieval can actually see them
# 3. review the query log (gaps, cluster jumps, latency)
# 4. append a digest + commit the corpus snapshot
# 5. VERIFY the run actually changed something, and exit non-zero if it did not
# Individual step failures are recorded in the digest and are never fatal, so one
# bad sync cannot stop the rest. The final verification is different: it exits
# non-zero, because a loop that silently does nothing is the failure this script
# has actually suffered, and a digest nobody reads cannot surface it.
set -u
CORPUS="${ALEXANDRIA_CORPUS:-$HOME/alexandria-corpus}"
# Overridable so the broken-interpreter path can be exercised in a test
# without touching the real venv (see tests/test_weekly_loop_preflight.py).
REPO="${ALEXANDRIA_REPO:-$HOME/codebase/alexandria}"
# Call the console script, never `python -m alexandria.cli`. The venv holds an
# EDITABLE install whose .pth carries an absolute path; on 2026-08-30 that path
# (/private/tmp/alexandria-procurement-floor/src, a worktree macOS reaped from
# /tmp) no longer existed, so every `-m` invocation died with
# ModuleNotFoundError while this console script -- which hardcodes its own
# sys.path insert -- kept working. One entry point, and it is this one.
CLI="$REPO/.venv/bin/alexandria"
BASE_URL="${ALEXANDRIA_BASE_URL:?set in the supervisor}"
# Key source is deployment-dependent: the Mac supervisor supplies the
# keychain service name; a Linux/NAS supervisor supplies ALEXANDRIA_LLM_KEY
# directly (its fleet-gateway key, e.g. from an EnvironmentFile). Required
# only when ALEXANDRIA_LLM_KEY is absent.
KEYCHAIN_SERVICE="${ALEXANDRIA_KEYCHAIN_SERVICE:-}"
DIGEST="$CORPUS/.alexandria/loop/weekly-digest.md"
# Every line below appends to $DIGEST. Bash resolves redirects BEFORE running
# the command, so a missing parent dir does not just lose the log -- it stops
# each sync from executing at all, leaving only the --allow-empty commit as
# evidence of a "successful" run. (Observed: 1 empty commit, 0 syncs.)
mkdir -p "$(dirname "$DIGEST")"
# Snapshot BEFORE the run so the post-run check can compare against something
# real rather than trusting exit codes.
DOCS_BEFORE=$(find "$CORPUS/sources" "$CORPUS/wiki" -name '*.md' 2>/dev/null | wc -l | tr -d ' ')
GEN_BEFORE=$(python3 -c "import json;print(json.load(open('$CORPUS/.alexandria/index/generation.json')).get('generation',0))" 2>/dev/null || echo 0)

STAMP="$(date '+%Y-%m-%d %H:%M %Z')"
if [ -n "${ALEXANDRIA_LLM_KEY:-}" ]; then
  KEY="$ALEXANDRIA_LLM_KEY"
elif [ -n "$KEYCHAIN_SERVICE" ]; then
  KEY="$(security find-generic-password -s "$KEYCHAIN_SERVICE" -w 2>/dev/null)"
else
  KEY=""
fi

{
  echo ""
  echo "## $STAMP"
} >> "$DIGEST"

# PREFLIGHT (2026-09-02). Before this existed, a broken interpreter did not
# stop the run: five steps failed in sequence, each caught by its own
# `|| echo ... FAILED`, and the snapshot step still committed 3,053 files.
# The digest recorded the errors and nobody read it for three days.
#
# A dependency check belongs BEFORE the work, not distributed across it as
# per-step error handling. Run the cheapest real invocation of the CLI --
# `--help` touches argparse and every import the verbs need -- and abort the
# whole run if it cannot start. Fail loud and early beats fail quiet and late.
echo "### preflight (can the CLI actually start?)" >> "$DIGEST"
if [ ! -x "$CLI" ]; then
  echo "[FAIL] PREFLIGHT: no executable CLI at $CLI" >> "$DIGEST"
  PREFLIGHT_FAILED=1
elif ! PREFLIGHT_OUT=$("$CLI" --help 2>&1); then
  {
    echo "[FAIL] PREFLIGHT: '$CLI --help' exited non-zero — the loop cannot run."
    echo "$PREFLIGHT_OUT" | tail -5
    echo "Most likely: the venv's editable install points at a path that no"
    echo "longer exists (check .venv/lib/python3.12/site-packages/*.pth)."
    echo "Fix: cd $REPO && .venv/bin/python -m pip install -e . --no-deps"
  } >> "$DIGEST"
  PREFLIGHT_FAILED=1
else
  echo "[PASS] preflight: CLI starts" >> "$DIGEST"
  PREFLIGHT_FAILED=0
fi

if [ "$PREFLIGHT_FAILED" -ne 0 ]; then
  # Notify on the way out. A preflight failure is the one case where doing
  # nothing is correct -- but doing nothing SILENTLY is what cost a week.
  NOTIFIER="${ALEXANDRIA_NOTIFIER:-/opt/homebrew/bin/terminal-notifier}"
  if [ -x "$NOTIFIER" ]; then
    "$NOTIFIER" \
      -title "Alexandria weekly loop DID NOT RUN" \
      -subtitle "preflight failed: CLI cannot start" \
      -message "$(grep '\[FAIL\] PREFLIGHT' "$DIGEST" | tail -1 | cut -c1-180)" \
      -group alexandria-weekly-loop >/dev/null 2>&1 || true
  fi
  echo "aborted before any sync (nothing was written, nothing committed)" >> "$DIGEST"
  exit 1
fi

if [ -z "$KEY" ]; then
  echo "LLM key lookup failed — sync skipped" >> "$DIGEST"
  exit 0
fi

export ALEXANDRIA_LLM_KEY="$KEY"

echo "### sync pi-sessions" >> "$DIGEST"
"$CLI" --corpus "$CORPUS" sync pi-sessions \
  --base-url "$BASE_URL" --model deepseek-v4-flash --workers 6 \
  >> "$DIGEST" 2>&1 || echo "sync FAILED (see above)" >> "$DIGEST"

echo "### sync journal (accountability digest)" >> "$DIGEST"
"$CLI" --corpus "$CORPUS" sync journal \
  --journal-path "$HOME/citadel/personal-finance/accountability.md" \
  >> "$DIGEST" 2>&1 || echo "journal sync FAILED" >> "$DIGEST"

# The vault is the upstream for ~70% of the corpus and the ONLY durable home of
# ~1,296 harness memories the live store has already rotated out. No LLM, no
# gateway: pure normalize-and-copy, so it runs even when the gateway is down.
echo "### sync knowledge-graph (vault memories)" >> "$DIGEST"
"$CLI" --corpus "$CORPUS" sync knowledge-graph \
  >> "$DIGEST" 2>&1 || echo "knowledge-graph sync FAILED" >> "$DIGEST"

echo "### sync inbox (explicit memories)" >> "$DIGEST"
"$CLI" --corpus "$CORPUS" sync inbox \
  >> "$DIGEST" 2>&1 || echo "inbox sync FAILED" >> "$DIGEST"

# Syncing writes .md files; without this step they are on disk but invisible to
# every query, which is the freshness failure the loop exists to prevent. A
# Deliberately NOT --enrich. Measured 2026-08-11 with a no-enrichment control on
# the identical corpus: indexing the synthetic hypothetical-question chunks cost
# 6.1 pts of recall@k (0.673 -> 0.612) and cut the zero-overlap band from 38.9%
# to 22.2%. Enrichment stays available as a CLI flag; it is not the default and
# must not run unattended until the ranking interaction is fixed. See
# docs/DECISION-enrichment-2026-08-11.md.
echo "### index (make newly synced docs retrievable)" >> "$DIGEST"
"$CLI" --corpus "$CORPUS" index \
  >> "$DIGEST" 2>&1 || echo "index FAILED" >> "$DIGEST"

echo "### query-log review (7d)" >> "$DIGEST"
"$REPO/.venv/bin/python" "$REPO/scripts/query-log-review.py" --corpus "$CORPUS" --since 7 \
  >> "$DIGEST" 2>&1 || echo "review FAILED" >> "$DIGEST"

# Leg-ablation invariant (BACKLOG #47/#48): only a significant positive recall
# delta after removing a leg is red; MRR is context. Weekly, not pre-commit,
# because each amputated pass is a full golden-set scoring (~60-90s).
# The loop intentionally remains non-destructive: an ablation red must not hide
# the later snapshot/verification. Unlike ordinary best-effort steps, however,
# it has its own explicit notifier below so the red cannot be merely a digest line.
echo "### leg-ablation (is either retrieval leg dead weight?)" >> "$DIGEST"
LEG_ABLATION_STATUS=0
"$REPO/.venv/bin/python" "$REPO/scripts/leg-ablation.py" --corpus "$CORPUS" \
  >> "$DIGEST" 2>&1 || LEG_ABLATION_STATUS=$?
if [ "$LEG_ABLATION_STATUS" -ne 0 ]; then
  echo "[FAIL] leg-ablation exited $LEG_ABLATION_STATUS (distinct notifier queued)" >> "$DIGEST"
fi

# keep the corpus weekly-snapshot-able (the quarterly contest needs it).
# Only `sources` and `wiki`: `notes` does not exist, and `.alexandria/` is
# gitignored as derived state. git add is ATOMIC across pathspecs, so naming
# either one made the whole add fail ("fatal: pathspec 'notes' did not match")
# and stage nothing -- while --allow-empty still produced a commit. Observed
# 2026-08-11: 2,277 new notes untracked, commit reported 0 files changed.
# No --allow-empty: a commit must mean something was actually captured.
git -C "$CORPUS" add sources wiki >> "$DIGEST" 2>&1 || echo "git add FAILED" >> "$DIGEST"
if git -C "$CORPUS" diff --cached --quiet; then
  echo "corpus snapshot: nothing new to commit" >> "$DIGEST"
else
  staged=$(git -C "$CORPUS" diff --cached --numstat | wc -l | tr -d ' ')
  git -C "$CORPUS" commit -q -m "weekly loop digest $(date '+%Y-%m-%d')" \
    && echo "corpus snapshot: committed $staged file(s)" >> "$DIGEST" \
    || echo "corpus commit FAILED" >> "$DIGEST"
fi

# The load-bearing step. Everything above reports what it INTENDED to do; this
# checks what actually happened to the corpus, the index, and retrieval.
echo "### verify (did the loop actually change anything?)" >> "$DIGEST"
VERIFY_STATUS=0
"$REPO/.venv/bin/python" "$REPO/scripts/verify-loop-run.py" \
  --corpus "$CORPUS" --binary "$REPO/.venv/bin/alexandria" \
  --docs-before "$DOCS_BEFORE" --generation-before "$GEN_BEFORE" \
  >> "$DIGEST" 2>&1 || VERIFY_STATUS=1

# C5 freshness (2026-08-23): quality gates cannot detect liveness failures --
# recall stays green on a frozen corpus (the 2026-08-11 incident). A corpus
# that has gone quiet must fail the loop loudly, not just report.
echo "### staleness (did the corpus go quiet?)" >> "$DIGEST"
"$CLI" --corpus "$CORPUS" staleness \
  >> "$DIGEST" 2>&1 || { echo "staleness FAILED: corpus is stale" >> "$DIGEST"; VERIFY_STATUS=1; }

# A non-zero exit is recorded by launchd and read by nobody. The whole point of
# the self-check is that a silent failure becomes visible, so push it somewhere
# with a human on the other end. Best-effort: never let notification failure
# change the run's verdict.
# Absolute path deliberately: launchd runs with PATH=/usr/bin:/bin:/usr/sbin:/sbin,
# which cannot see /opt/homebrew/bin, so a bare `terminal-notifier` here would
# silently never fire -- a failure notifier that is itself an invisible no-op.
NOTIFIER="${ALEXANDRIA_NOTIFIER:-/opt/homebrew/bin/terminal-notifier}"
# Keep a red ablation nonfatal to the weekly maintenance run, but surface it via
# a distinct alert even when the final freshness verification passes.
if [ "$LEG_ABLATION_STATUS" -ne 0 ] && [ -x "$NOTIFIER" ]; then
  "$NOTIFIER" \
    -title "Alexandria weekly leg-ablation FAILED" \
    -subtitle "exit $LEG_ABLATION_STATUS; retrieval review required" \
    -message "$(grep '\[FAIL\] leg-ablation' "$DIGEST" | tail -1 | cut -c1-180)" \
    -group alexandria-weekly-leg-ablation >/dev/null 2>&1 || true
fi
if [ "$VERIFY_STATUS" -ne 0 ] && [ -x "$NOTIFIER" ]; then
  "$NOTIFIER" \
    -title "Alexandria weekly loop FAILED" \
    -subtitle "$(grep -c '\[FAIL\]' "$DIGEST" 2>/dev/null || echo '?') check(s) failed" \
    -message "$(grep '\[FAIL\]' "$DIGEST" | tail -3 | tr '\n' ' ' | cut -c1-180)" \
    -group alexandria-weekly-loop >/dev/null 2>&1 || true
fi

echo "done" >> "$DIGEST"
exit "$VERIFY_STATUS"
