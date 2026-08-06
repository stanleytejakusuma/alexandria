#!/usr/bin/env bash
# Phase-4 gate: fresh-clone test (README phase table).
#
# Clones the public repo into a temp dir, installs, and runs the demo corpus
# end-to-end: lint -> index -> search -> wiki-site. Optional: `answer` when a
# gateway is configured (ALEX_BASE_URL / ALEX_API_KEY_ENV, see
# docs/QUICKSTART.md). Finally leaks-scans the clone: zero private content in
# a fresh checkout is the gate's second half.
#
# Usage: scripts/fresh-clone-test.sh [REPO_URL]
set -euo pipefail
REPO_URL="${1:-https://github.com/stanleytejakusuma/alexandria}"
WORK="$(mktemp -d /tmp/alexandria-freshclone.XXXXXX)"
echo "==> fresh clone into $WORK"
git clone -q --depth 1 "$REPO_URL" "$WORK/alexandria"

cd "$WORK/alexandria"
PY="$(command -v python3.12 || command -v python3)"
"$PY" -m venv .venv
.venv/bin/pip install -q -e ".[dev]"

echo "==> [1/5] lint"
.venv/bin/alexandria --corpus demo-corpus lint
echo "==> [2/5] index (offline)"
.venv/bin/alexandria --corpus demo-corpus index --limit 0
echo "==> [3/5] search"
.venv/bin/alexandria --corpus demo-corpus search "Proxima deal status and next steps" | head -4
echo "==> [4/5] wiki-site"
.venv/bin/alexandria --corpus demo-corpus wiki-site --wiki demo-corpus/wiki --out "$WORK/site" || true
if [ -n "${ALEX_BASE_URL:-}" ] && [ -n "${ALEX_API_KEY_ENV:-}" ]; then
  echo "==> [5/5] answer (live; gateway configured)"
  .venv/bin/alexandria --corpus demo-corpus answer \
    "What is the Proxima deal state and what happens next?" \
    --base-url "$ALEX_BASE_URL" --api-key-env "$ALEX_API_KEY_ENV" \
    --save-dir "$WORK/answer-wiki" | tail -12
else
  echo "==> [5/5] answer skipped (set ALEX_BASE_URL + ALEX_API_KEY_ENV to include it)"
fi

echo "==> leak scan of the fresh clone (zero private content gate)"
.venv/bin/python scripts/precommit-scan.py --all

echo "FRESH-CLONE TEST PASSED ($WORK)"
