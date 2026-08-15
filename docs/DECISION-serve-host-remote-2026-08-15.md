# Decision: serve and storage move to the always-on NAS host

**Date:** 2026-08-15
**Status:** decided (operator-ratified)
**Supersedes:** SPEC-write-path-and-serve.md §11 "Serve host" bullet (default local).

## What

alexandria's server, storage, and index move to the operator's always-on NAS host.
The operator's own machine is demoted to the build machine. This executes the
"remote supported" topology that §5.8 reserved and supersedes the "default local"
for this deployment.

## Drivers

- The operator's machine is at ~95% disk capacity; the corpus + index (5.8 GB and
  growing) should not live there.
- The operator's machine sleeps; a server reachable for the second-harness read
  path (BACKLOG #13) needs an always-on host.
- The operator stated this as a requirement ("storage on the NAS, not local").

## What moves

Everything that follows the corpus: storage, index, serve, the weekly loop, capture
sweeps, and the extension fallback host. The operator's machine keeps building
(embedding) and querying as a client.

## Constraints carried from §5.8

1. **Embedding provider.** MLX is Apple-Silicon-only. Serving on the NAS requires the
   index to have been built with `ALEXANDRIA_EMBED_PROVIDER=local` (torch) — a full
   re-embed, not a file copy; the two providers are different vector spaces and the
   manifest gate (F4) refuses a mismatch loudly.
2. **Build where it's capable, serve anywhere.** The re-embed runs on the operator's
   machine; do not CPU-embed the full corpus on the NAS (the 2026-08-11 hard-reboot
   pattern).
3. **Recall re-certification.** The 63.3% / MRR 0.531 baseline was measured on MLX
   vectors; after the `local` re-embed, re-run `alexandria eval` and record the new
   baseline. The old number does not transfer.
4. **Gateway key.** Use the fleet LLM gateway (loopback from the NAS); mint a dedicated
   virtual key there. Do not copy the local-machine keychain keys — they are
   local-instance keys and are rejected by the fleet gateway.
5. **Bind.** The server binds loopback on the NAS (fail-closed); clients reach it over
   the tunnel. Private host/key detail lives outside the repo (see the migration
   companion notes), because the leak scanner forbids private hostnames in-repo.

## Verification

- `alexandria eval` on the NAS against the `local`-built index records a new recall
  baseline, with no silent provider mismatch.
