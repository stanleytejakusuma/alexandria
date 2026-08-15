# Decision: this deployment runs on the always-on NAS host (remote topology)

**Date:** 2026-08-15
**Status:** decided (operator-ratified)
**Confirms:** SPEC-write-path-and-serve.md §5.8 / §11 — default local, remote supported.
**Selects, for this deployment:** the remote (always-on NAS) topology.

## Product principle (unchanged, restated for the record)

**Default: the operator's own machine.** A single-user install is the whole story and
needs no configuration. **Optional: a remote always-on host.** For an enterprise /
fleet / production-grade environment, compute and storage move off the machine so the
database is reachable fleet-wide, and access control becomes a real requirement.

This decision does **not** change the default. It exercises the *optional* remote
topology for the operator's own environment, which is being run as an
enterprise-grade, fleet-accessible deployment.

## What

For this deployment, alexandria's server, storage, and index move to the always-on NAS
host. The operator's own machine is demoted to the build machine (it still embeds and
queries as a client). A single-user, all-local install remains the out-of-the-box
default.

## Drivers

- The operator's machine is at ~95% disk capacity; the corpus + index (5.8 GB and
  growing) should not live there.
- The operator's machine sleeps; a server reachable for the second-harness read
  path (BACKLOG #13) needs an always-on host.
- Fleet-wide accessibility: storage on the NAS is reachable by the home-lab fleet,
  and it exercises the production/enterprise hosting path end to end.

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
