# pi-activation-decision-2026-08-08.md

# Alexandria phase-3 activation decision (2026-08-08)

**Decided by:** Stanley (principal).
**Context:** the phase-2 synthesis gate closed via the Red-ratified waiver
(`docs/pi-red-verdict-2026-08-07.md`), and the phase-2↔phase-3 loop was
terminated per the signed loop-termination contract. The phase-3 contest
cycle (run 1 + run 2) closed with NO verdict — both INVALID on grader
disagreement (0.50, then 0.25 vs the 0.20 cap). Per SPEC §3 an INVALID run
is discarded and the FAIL-slot default applies: the product stays read-only.

## The decision

Proceed with phase 3 **operationally**: activate the Alexandria Pi extension
in **read-only mode** (alexandria-search + alexandria-answer tools) while
certification continues via the standing contest telemetry (frozen queries,
frozen mechanics, monthly cadence). This overrides the SPEC's
"inert until a PASS verdict" activation rule — the override is recorded
here and in README phase-3 status, per the recording discipline.

## Rationale (principal's framing)

- The whole loop between phase 2 and phase 3 is closed; nothing technical
  blocks operating phase 3 while its certification cycle runs.
- The extension is read-only by construction: search is retrieval-only;
  answer synthesizes pages to disk under the corpus; nothing writes to any
  live system, executes trades, or reads secrets beyond the gateway key
  already scoped to Alexandria.
- The telemetry loop remains the ONLY path to a PASS verdict. Activation
  does not shortcut certification; it starts collecting real usage signal
  (queries logged to the corpus query log) that the contest does not have.

## Conditions

1. Read-only scope only: the two shipped tools, nothing else.
2. The contest telemetry (monthly, frozen mechanics) stays the certification
   path; a PASS there upgrades nothing — activation is already done — but a
   FAIL or INVALID keeps Alexandria out of any write-capable surface.
3. This decision is reversible by Stanley at any time (remove the
   `~/.pi/agent/extensions/alexandria.ts` file).
4. Any future write-capable surface (ingestion connectors, sync, distil)
   remains gated on a valid contest PASS — no override without a new
   recorded decision.
