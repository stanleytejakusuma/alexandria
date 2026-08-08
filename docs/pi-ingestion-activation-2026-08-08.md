# pi-ingestion-activation-2026-08-08.md

# Alexandria write-surface activation (2026-08-08)

**Decided by:** Stanley (principal), agreeing to option (b) with ingestion
first: "I also agree on b. but focus on the ingestion first."

## The decision

Activate the Alexandria **write surface**: the `pi-sessions` connector
(`alexandria sync pi-sessions`) is live, distilling Pi agent session bursts
into source notes in the private corpus. This extends the 2026-08-08
read-only activation (`docs/pi-activation-decision-2026-08-08.md`) with the
ingestion half of the product.

## Quality gate (passed 2026-08-08)

n=5 quality gate initially returned zero notes — investigation showed the
first discovery-order bursts are tool-output dumps (TypeScript schemas,
library source), which the distiller *correctly* declines
(`{"observations":[]}`). Real conversation bursts distill properly:
- a burst about Ghostty keybindings → 7 notes
- a local-AI-gateway debugging session → 8 notes, including a root-cause note
  (request-dedup hash bug, not semantic cache) and a hotfix-deploy note

The substance filter + skip-log + fail-safe (unconsumed bursts on failure)
all behaved as designed. Failure modes seen and understood: `ddgw/claude-
haiku-4-5` is a broken DuckDuckGo alias on the private gateway (ERR_BAD_REQUEST)
— model of choice for distil: `deepseek-v4-flash` via the private gateway
(configured via the standard env vars) with `ALEXANDRIA_LLM_KEY` set to the gateway key.

## Conditions

1. Ingested notes land in the private corpus (`~/alexandria-corpus`) only;
   the public repo never carries corpus content (leak-scan enforced).
2. Distil model: `deepseek-v4-flash` (cheap, measured good enough for the
   note shape); revisit if note quality regresses.
3. Ingestion cadence: manual `sync` runs on demand; a cron is not yet wired
   (Stanley's telemetry-cadence call pending).
4. Everything from the prior activation decision still applies (no
   write-capable surface beyond this connector; reversibility by deletion).
