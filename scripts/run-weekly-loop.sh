#!/bin/bash
# Alexandria weekly self-learning loop (runs via LaunchAgent, Sun 09:30).
# 1. distil the week's pi-sessions into corpus notes (memory generation)
# 2. review the query log (gaps, cluster jumps, latency)
# 3. append a digest + commit the corpus snapshot
# Exit 0 always; failures are recorded in the digest, never fatal.
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

echo "### sync inbox (explicit memories)" >> "$DIGEST"
"$REPO/.venv/bin/python" -m alexandria.cli --corpus "$CORPUS" sync inbox \
  >> "$DIGEST" 2>&1 || echo "inbox sync FAILED" >> "$DIGEST"

echo "### query-log review (7d)" >> "$DIGEST"
"$REPO/.venv/bin/python" "$REPO/scripts/query-log-review.py" --corpus "$CORPUS" --since 7 \
  >> "$DIGEST" 2>&1 || echo "review FAILED" >> "$DIGEST"

# keep the corpus weekly-snapshot-able (the quarterly contest needs it)
git -C "$CORPUS" add notes sources wiki .alexandria/loop 2>/dev/null
git -C "$CORPUS" commit -q -m "weekly loop digest $(date '+%Y-%m-%d')" --allow-empty 2>/dev/null

echo "done" >> "$DIGEST"
