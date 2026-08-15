#!/usr/bin/env bash
# One-command measurement loop for the synthesis fact-recall gate.
#
# Chains: synthesize-golden-pages (live, needs gateway) -> fact-recall evaluator
# (live) -> manifest verify -> v1-vs-current delta -> sanitized public summary.
# Every fix round becomes: edit prompts, run this, read the delta.
#
# Config via environment:
#   ALEXANDRIA_CORPUS  private corpus dir (default ~/alexandria-corpus)
#   ALEX_GOLDEN        golden file (default $CORPUS/.alexandria/golden/synthesis-clusters.jsonl)
#   ALEX_OUT           work dir (default /tmp/alx/synth-run)
#   ALEX_BASELINE      prior report for the delta (optional)
#   ALEX_BASE_URL      gateway base URL (required; no default -- the host is
#                       a private network address that must not appear in this
#                       repo, the leak scanner enforces that)
#   ALEX_API_KEY_ENV   env var holding the gateway key (default ALEXANDRIA_AXIOM_KEY)
#   ALEX_MODEL_A       grader/writer A (default deepseek-v4-flash)
#   ALEX_MODEL_B       grader B (default deepseek-v4-pro)
#   ALEX_RETRIES       pipeline retries per cluster (default 2)
set -euo pipefail
cd "$(dirname "$0")/.."

CORPUS="${ALEXANDRIA_CORPUS:-$HOME/alexandria-corpus}"
GOLDEN="${ALEX_GOLDEN:-$CORPUS/.alexandria/golden/synthesis-clusters.jsonl}"
OUT="${ALEX_OUT:-/tmp/alx/synth-run}"
BASE_URL="${ALEX_BASE_URL:-}"
API_KEY_ENV="${ALEX_API_KEY_ENV:-ALEXANDRIA_AXIOM_KEY}"
MODEL_A="${ALEX_MODEL_A:-deepseek-v4-flash}"
MODEL_B="${ALEX_MODEL_B:-deepseek-v4-pro}"
RETRIES="${ALEX_RETRIES:-2}"
REPORT_DIR="$CORPUS/.alexandria/golden"

[ -n "$BASE_URL" ] || { echo "ALEX_BASE_URL must be set (gateway base URL)"; exit 1; }

[ -f "$GOLDEN" ] || { echo "golden file not found: $GOLDEN"; exit 1; }
[ -n "${!API_KEY_ENV:-}" ] || { echo "env $API_KEY_ENV is unset (gateway key)"; exit 1; }

mkdir -p "$OUT"
echo "==> [1/5] synthesis (pages + sidecars) -> $OUT/pages"
.venv/bin/python scripts/synthesize-golden-pages.py \
    --golden "$GOLDEN" --out "$OUT/pages" --gather "$OUT/gather" \
    --retries "$RETRIES" \
    --base-url "$BASE_URL" --api-key-env "$API_KEY_ENV"

echo "==> [2/5] fact-recall evaluation"
REPORT="$REPORT_DIR/synthesis-fact-recall-$(date +%Y%m%d-%H%M%S).json"
.venv/bin/python scripts/eval-synthesis-fact-recall.py \
    --pages "$OUT/pages" --gather "$OUT/gather" \
    --base-url "$BASE_URL" --api-key-env "$API_KEY_ENV" \
    --model-a "$MODEL_A" --model-b "$MODEL_B" \
    --output "$REPORT" --report-dir "$REPORT_DIR"

echo "==> [3/5] manifest verification of $REPORT"
.venv/bin/python scripts/eval-synthesis-fact-recall.py --verify "$REPORT"

if [ -n "${ALEX_BASELINE:-}" ]; then
  echo "==> [4/5] delta vs baseline $ALEX_BASELINE"
  .venv/bin/python scripts/compare-fact-recall.py \
      --baseline "$ALEX_BASELINE" --current "$REPORT" || true
else
  echo "==> [4/5] no ALEX_BASELINE set -- skipping delta (set it next round)"
fi

echo "==> [5/5] sanitized public summary"
.venv/bin/python scripts/emit-fact-recall-summary.py \
    --report "$REPORT" --golden "$GOLDEN"

echo "done. report: $REPORT  (commit with: git add -f $REPORT)"
