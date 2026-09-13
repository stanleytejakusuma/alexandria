#!/bin/bash
# Bridge for the launchd serve job. The code reads the LLM key from the
# ALEXANDRIA_LLM_KEY env var, but a launchd plist is static XML and cannot run
# `security`, so a bare `alexandria serve` under launchd has no key. This sources
# the key from the keychain (same pattern as run-weekly-loop.sh) and then execs
# the real serve command, so launchd tracks the correct PID and the key never
# appears in the plist. Base URL is read by serve.py directly from
# ALEXANDRIA_LLM_BASE_URL (default 127.0.0.1:20128).
set -euo pipefail
SVC="${ALEXANDRIA_KEYCHAIN_SERVICE:?serve-launchd requires ALEXANDRIA_KEYCHAIN_SERVICE (set in LaunchAgent)}"
KEY="$(security find-generic-password -s "$SVC" -w 2>/dev/null)" || true
if [ -z "$KEY" ]; then
  echo "serve-launchd: keychain lookup failed for service '$SVC'" >&2
  exit 1
fi
export ALEXANDRIA_LLM_KEY="$KEY"
unset KEY
# The embedder model (Qwen3-Embedding-0.6B) is fully cached locally; without
# this, every cold start re-checks HF Hub for updates and a slow/unreachable
# network kills the first query with ModelLoadTimeout (seen live 2026-09-12:
# /answer timed out while the model re-checked the hub). Offline mode loads
# the cached copy instantly. Only revisit if a different model is adopted.
export HF_HUB_OFFLINE=1
exec "$@"
