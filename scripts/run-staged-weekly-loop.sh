#!/bin/bash
# Opt-in staged weekly loop. Do not install in launchd until fixture + human
# cutover verification pass. A failed/timed-out child leaves only an unpublished
# candidate under the control root; it never mutates the selected generation.
set -eu
CONTROL_ROOT="${ALEXANDRIA_CORPUS:-$HOME/alexandria-corpus}"
REPO="${ALEXANDRIA_REPO:-$HOME/codebase/alexandria}"
PYTHON="$REPO/.venv/bin/python"
GENERATION_ID="${ALEXANDRIA_GENERATION_ID:-$(date -u '+%Y%m%dT%H%M%SZ')}"

STAGED=$(PYTHONPATH="$REPO/src" "$PYTHON" -c \
  "from alexandria.generation_publisher import stage_generation; print(stage_generation('$CONTROL_ROOT', '$GENERATION_ID'))")

echo "staged generation: $STAGED"
if ! ALEXANDRIA_STAGED_MODE=1 ALEXANDRIA_CORPUS="$STAGED" "$REPO/scripts/run-weekly-loop.sh"; then
  echo "staged loop failed; candidate retained and active generation unchanged: $STAGED" >&2
  exit 1
fi

PYTHONPATH="$REPO/src" "$PYTHON" "$REPO/scripts/publish-staged-generation.py" \
  --control-root "$CONTROL_ROOT" --staged "$STAGED"
