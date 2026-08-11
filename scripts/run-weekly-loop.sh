#!/bin/bash
# Alexandria weekly self-learning loop (runs via LaunchAgent, Sun 09:30).
# 1. distil the week's pi-sessions into corpus notes (memory generation)
# 2. index the newly synced docs so retrieval can actually see them
# 3. review the query log (gaps, cluster jumps, latency)
# 4. append a digest + commit the corpus snapshot
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

echo "done" >> "$DIGEST"
