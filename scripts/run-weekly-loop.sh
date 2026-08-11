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
REPO="$HOME/codebase/alexandria"
BASE_URL="${ALEXANDRIA_BASE_URL:?set in LaunchAgent}"
KEYCHAIN_SERVICE="${ALEXANDRIA_KEYCHAIN_SERVICE:?set in LaunchAgent}"
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
KEY="$(security find-generic-password -s "$KEYCHAIN_SERVICE" -w 2>/dev/null)"

{
  echo ""
  echo "## $STAMP"
} >> "$DIGEST"

if [ -z "$KEY" ]; then
  echo "keychain lookup failed — sync skipped" >> "$DIGEST"
  exit 0
fi

export ALEXANDRIA_LLM_KEY="$KEY"

echo "### sync pi-sessions" >> "$DIGEST"
"$REPO/.venv/bin/python" -m alexandria.cli --corpus "$CORPUS" sync pi-sessions \
  --base-url "$BASE_URL" --model deepseek-v4-flash --workers 6 \
  >> "$DIGEST" 2>&1 || echo "sync FAILED (see above)" >> "$DIGEST"

echo "### sync journal (accountability digest)" >> "$DIGEST"
"$REPO/.venv/bin/python" -m alexandria.cli --corpus "$CORPUS" sync journal \
  --journal-path "$HOME/citadel/personal-finance/accountability.md" \
  >> "$DIGEST" 2>&1 || echo "journal sync FAILED" >> "$DIGEST"

# The vault is the upstream for ~70% of the corpus and the ONLY durable home of
# ~1,296 harness memories the live store has already rotated out. No LLM, no
# gateway: pure normalize-and-copy, so it runs even when the gateway is down.
echo "### sync knowledge-graph (vault memories)" >> "$DIGEST"
"$REPO/.venv/bin/python" -m alexandria.cli --corpus "$CORPUS" sync knowledge-graph \
  >> "$DIGEST" 2>&1 || echo "knowledge-graph sync FAILED" >> "$DIGEST"

echo "### sync inbox (explicit memories)" >> "$DIGEST"
"$REPO/.venv/bin/python" -m alexandria.cli --corpus "$CORPUS" sync inbox \
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
"$REPO/.venv/bin/python" -m alexandria.cli --corpus "$CORPUS" index \
  >> "$DIGEST" 2>&1 || echo "index FAILED" >> "$DIGEST"

echo "### query-log review (7d)" >> "$DIGEST"
"$REPO/.venv/bin/python" "$REPO/scripts/query-log-review.py" --corpus "$CORPUS" --since 7 \
  >> "$DIGEST" 2>&1 || echo "review FAILED" >> "$DIGEST"

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

# A non-zero exit is recorded by launchd and read by nobody. The whole point of
# the self-check is that a silent failure becomes visible, so push it somewhere
# with a human on the other end. Best-effort: never let notification failure
# change the run's verdict.
if [ "$VERIFY_STATUS" -ne 0 ] && command -v terminal-notifier >/dev/null 2>&1; then
  terminal-notifier \
    -title "Alexandria weekly loop FAILED" \
    -subtitle "$(grep -c '\[FAIL\]' "$DIGEST" 2>/dev/null || echo '?') check(s) failed" \
    -message "$(grep '\[FAIL\]' "$DIGEST" | tail -3 | tr '\n' ' ' | cut -c1-180)" \
    -group alexandria-weekly-loop >/dev/null 2>&1 || true
fi

echo "done" >> "$DIGEST"
exit "$VERIFY_STATUS"
