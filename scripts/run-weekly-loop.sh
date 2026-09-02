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
# Every unattended command has a deadline. A healthy connector can otherwise
# replay an unbounded backlog for hours (838 pi-session bursts estimated 141m
# on 2026-09-02), monopolising the Mac and preventing the next scheduled run.
# A bounded batch makes backlog catch-up incremental; the next weekly run picks
# up the rest. Override only for an explicitly supervised catch-up.
TIMEOUT="${ALEXANDRIA_TIMEOUT:-/opt/homebrew/bin/timeout}"
STEP_TIMEOUT_SECONDS="${ALEXANDRIA_STEP_TIMEOUT_SECONDS:-1800}"
TIMEOUT_KILL_AFTER_SECONDS="${ALEXANDRIA_TIMEOUT_KILL_AFTER_SECONDS:-30}"
PI_SESSIONS_LIMIT="${ALEXANDRIA_PI_SESSIONS_LIMIT:-100}"
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
KEY=""
KEY_STATUS=0

is_positive_integer() { [[ "$1" =~ ^[1-9][0-9]*$ ]]; }
run_bounded() { "$TIMEOUT" --kill-after="${TIMEOUT_KILL_AFTER_SECONDS}s" "$STEP_TIMEOUT_SECONDS" "$@"; }

# Validate all execution controls before any external call. A bad launchd value
# must be a failed preflight, never a supposedly healthy but unbounded run.
if [ ! -x "$TIMEOUT" ] || ! is_positive_integer "$STEP_TIMEOUT_SECONDS" || ! is_positive_integer "$TIMEOUT_KILL_AFTER_SECONDS" || ! is_positive_integer "$PI_SESSIONS_LIMIT"; then
  PREFLIGHT_CONFIG_FAILED=1
else
  PREFLIGHT_CONFIG_FAILED=0
fi

if [ -n "${ALEXANDRIA_LLM_KEY:-}" ]; then
  KEY="$ALEXANDRIA_LLM_KEY"
elif [ -n "$KEYCHAIN_SERVICE" ] && [ "$PREFLIGHT_CONFIG_FAILED" -eq 0 ]; then
  KEY=$("$TIMEOUT" --kill-after="${TIMEOUT_KILL_AFTER_SECONDS}s" 30 security find-generic-password -s "$KEYCHAIN_SERVICE" -w 2>/dev/null)
  KEY_STATUS=$?
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
if [ "$PREFLIGHT_CONFIG_FAILED" -ne 0 ]; then
  echo "[FAIL] PREFLIGHT: timeout/config invalid (runner=$TIMEOUT, step=$STEP_TIMEOUT_SECONDS, kill-after=$TIMEOUT_KILL_AFTER_SECONDS, batch=$PI_SESSIONS_LIMIT)" >> "$DIGEST"
  PREFLIGHT_FAILED=1
elif [ ! -x "$CLI" ]; then
  echo "[FAIL] PREFLIGHT: no executable CLI at $CLI" >> "$DIGEST"
  PREFLIGHT_FAILED=1
elif ! PREFLIGHT_OUT=$(run_bounded "$CLI" --help 2>&1); then
  {
    echo "[FAIL] PREFLIGHT: '$CLI --help' exited non-zero or timed out — the loop cannot run."
    echo "$PREFLIGHT_OUT" | tail -5
    echo "Most likely: the venv's editable install points at a path that no"
    echo "longer exists (check .venv/lib/python3.12/site-packages/*.pth)."
    echo "Fix: cd $REPO && VIRTUAL_ENV=$REPO/.venv /opt/homebrew/bin/uv pip install -e . --no-deps"
  } >> "$DIGEST"
  PREFLIGHT_FAILED=1
elif ! run_bounded "$CLI" sync pi-sessions --help >> "$DIGEST" 2>&1; then
  echo "[FAIL] PREFLIGHT: sync pi-sessions --help cannot start" >> "$DIGEST"
  PREFLIGHT_FAILED=1
else
  echo "[PASS] preflight: CLI and required sync parser start; timeout=${STEP_TIMEOUT_SECONDS}s; pi-session batch=${PI_SESSIONS_LIMIT}" >> "$DIGEST"
  PREFLIGHT_FAILED=0
fi

if [ "$PREFLIGHT_FAILED" -ne 0 ]; then
  # Notify on the way out. A preflight failure is the one case where doing
  # nothing is correct -- but doing nothing SILENTLY is what cost a week.
  NOTIFIER="${ALEXANDRIA_NOTIFIER:-/opt/homebrew/bin/terminal-notifier}"
  if [ -x "$NOTIFIER" ]; then
    "$NOTIFIER" \
      -title "Alexandria weekly loop DID NOT RUN" \
      -subtitle "preflight failed" \
      -message "$(grep '\[FAIL\] PREFLIGHT' "$DIGEST" | tail -1 | cut -c1-180)" \
      -group alexandria-weekly-loop >/dev/null 2>&1 || true
  fi
  echo "aborted before any sync; no snapshot was committed" >> "$DIGEST"
  exit 1
fi

if [ -z "$KEY" ]; then
  echo "[FAIL] required credential lookup exited $KEY_STATUS; no sync was attempted; no snapshot was committed" >> "$DIGEST"
  exit 1
fi

export ALEXANDRIA_LLM_KEY="$KEY"

# Required work must be bounded and fail the whole run. The old independent
# `|| echo ... FAILED` clauses let five broken steps fall through to a corpus
# snapshot, creating a commit that looked healthy while retrieval was frozen.
run_required() {
  local label="$1"
  shift
  echo "### $label" >> "$DIGEST"
  run_bounded "$@" >> "$DIGEST" 2>&1
  local status=$?
  if [ "$status" -ne 0 ]; then
    if [ "$status" -eq 124 ]; then
      echo "[FAIL] $label timed out after ${STEP_TIMEOUT_SECONDS}s" >> "$DIGEST"
    else
      echo "[FAIL] $label exited $status" >> "$DIGEST"
    fi
  fi
  return "$status"
}

abort_required_work() {
  local label="$1"
  echo "required work failed at '$label'; snapshot skipped (partial corpus is not committed)" >> "$DIGEST"
  NOTIFIER="${ALEXANDRIA_NOTIFIER:-/opt/homebrew/bin/terminal-notifier}"
  if [ -x "$NOTIFIER" ]; then
    "$NOTIFIER" \
      -title "Alexandria weekly loop DID NOT COMPLETE" \
      -subtitle "required step failed: $label" \
      -message "$(grep "\[FAIL\] $label" "$DIGEST" | tail -1 | cut -c1-180)" \
      -group alexandria-weekly-loop >/dev/null 2>&1 || true
  fi
  exit 1
}

run_required "sync pi-sessions" "$CLI" --corpus "$CORPUS" sync pi-sessions \
  --base-url "$BASE_URL" --model deepseek-v4-flash --workers 6 --limit "$PI_SESSIONS_LIMIT" \
  || abort_required_work "sync pi-sessions"

run_required "sync journal (accountability digest)" "$CLI" --corpus "$CORPUS" sync journal \
  --journal-path "$HOME/citadel/personal-finance/accountability.md" \
  || abort_required_work "sync journal (accountability digest)"

# The vault is the upstream for ~70% of the corpus and the ONLY durable home of
# ~1,296 harness memories the live store has already rotated out. No LLM, no
# gateway: pure normalize-and-copy, so it runs even when the gateway is down.
run_required "sync knowledge-graph (vault memories)" "$CLI" --corpus "$CORPUS" sync knowledge-graph \
  || abort_required_work "sync knowledge-graph (vault memories)"

run_required "sync inbox (explicit memories)" "$CLI" --corpus "$CORPUS" sync inbox \
  || abort_required_work "sync inbox (explicit memories)"

# Syncing writes .md files; without this step they are on disk but invisible to
# every query, which is the freshness failure the loop exists to prevent. A
# Deliberately NOT --enrich. Measured 2026-08-11 with a no-enrichment control on
# the identical corpus: indexing the synthetic hypothetical-question chunks cost
# 6.1 pts of recall@k (0.673 -> 0.612) and cut the zero-overlap band from 38.9%
# to 22.2%. Enrichment stays available as a CLI flag; it is not the default and
# must not run unattended until the ranking interaction is fixed. See
# docs/DECISION-enrichment-2026-08-11.md.
run_required "index (make newly synced docs retrievable)" "$CLI" --corpus "$CORPUS" index \
  || abort_required_work "index (make newly synced docs retrievable)"

echo "### query-log review (7d)" >> "$DIGEST"
"$REPO/.venv/bin/python" "$REPO/scripts/query-log-review.py" --corpus "$CORPUS" --since 7 \
  >> "$DIGEST" 2>&1 || echo "review FAILED" >> "$DIGEST"

# Ablation is deliberately NOT a weekly-loop responsibility. It performs full
# model loads/scoring and has no bearing on whether the corpus can ingest and
# index. The compute/storage split ratified on 2026-08-25 keeps it off all
# weekly loops; run it explicitly in CI/on an engine change instead. The opt-in
# remains only for supervised diagnosis and is separately bounded.
LEG_ABLATION_STATUS=0
if [ "${ALEXANDRIA_RUN_LEG_ABLATION:-0}" = "1" ]; then
  echo "### leg-ablation (supervised opt-in)" >> "$DIGEST"
  run_bounded "$REPO/.venv/bin/python" "$REPO/scripts/leg-ablation.py" --corpus "$CORPUS" \
    >> "$DIGEST" 2>&1 || LEG_ABLATION_STATUS=$?
  if [ "$LEG_ABLATION_STATUS" -ne 0 ]; then
    echo "[FAIL] leg-ablation exited $LEG_ABLATION_STATUS (distinct notifier queued)" >> "$DIGEST"
  fi
else
  echo "### leg-ablation (skipped; CI/on-engine-change only)" >> "$DIGEST"
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
