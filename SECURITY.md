# Security

Alexandria is **pre-alpha** (`Development Status :: 2 - Pre-Alpha` in `pyproject.toml`) and
this document reflects that: it states what is actually true today, verified against live
code, not an aspirational target. Where a real gap exists, it says so plainly rather than
describing a mitigation that has not shipped.

For a systematic attack-surface walk-through (STRIDE), see `docs/THREAT-MODEL.md`. This
document is the operator-facing summary; the threat model is the reviewer-facing detail.

## Reporting a vulnerability

This is a personal, single-maintainer project. For anything that should stay private until
fixed (which is most real security findings), use **GitHub's Private Vulnerability
Reporting** on this repository (the "Security" tab -> "Report a vulnerability") rather than
opening a public issue -- it gives a genuine private channel with no extra maintenance
overhead for a single-maintainer project. For anything you are comfortable being public
immediately (e.g. a hardening suggestion, not an active exploit), an ordinary GitHub issue is
fine. There is no bug bounty and no formal disclosure SLA at this stage -- pre-alpha software
with a single deployer does not carry the same obligations a production service does, but a
real report will be read and acted on.

## What this engine is, threat-model-wise

Alexandria is a **local-first personal/single-operator knowledge engine**. The design
assumption stated throughout the codebase (see `docs/SPEC-multi-tenant-and-learning-loop.md`
section 0) is one operator per corpus. Multi-tenant isolation, RBAC, and cross-organization
access control are **explicitly not built** (see "What is out of scope" below) -- do not
deploy this today as a shared service for mutually-untrusted users.

### Trust boundaries, as they actually exist in the code

1. **Filesystem access equals identity.** `alexandria serve` (`src/alexandria/serve.py`)
   derives caller identity from the connecting socket, not from any request-body claim: a
   Unix socket connection is identified by its own path (one socket per identity), a TCP
   connection is stamped with the fixed `local-anonymous` identity regardless of who
   connects. The CLI similarly derives identity from the OS user (`getpass.getuser()`),
   never from a user-suppliable flag -- the former `--user` flag was removed rather than
   validated, because there was no way to verify it and a forged-but-plausible value is
   worse than an absent one (backlog #8, "structurally verified"). The one remaining
   passthrough (`--caller`) is explicitly labeled unverified in its own `--help` text and
   in the audit trail (values outside a small known set are prefixed `unverified:`) -- see
   backlog #8's full note for the reasoning.
2. **`serve` binds loopback-only by default.** `bind()` in `serve.py` refuses a non-loopback
   host unless `ALEXANDRIA_SERVE_ALLOW_REMOTE=1` is explicitly set (`NonLoopbackRefused`).
   There is no TLS and no auth beyond filesystem/socket identity -- this is documented in
   `serve.py`'s own module docstring as **deliberately not built**. Binding this server to a
   non-loopback interface without a reverse proxy providing TLS+auth is not supported.
3. **No secrets are read from argv, printed, or committed.** LLM/vision gateway
   credentials are read from environment variables only (`ALEXANDRIA_LLM_KEY`,
   `ALEXANDRIA_VISION_KEY`), with an optional macOS Keychain fallback the operator
   configures via `ALEXANDRIA_VISION_KEYCHAIN_SERVICE` -- the engine ships **no default
   keychain service name**, so a fresh clone has no path to any operator's real keychain
   entry. Two distinct checks, each covering a different surface: (a)
   `scripts/precommit-scan.py`, a pre-commit hook blocking a NEW commit from introducing a
   secret shape, private absolute path, or project-specific codename (forward-looking,
   bypassable with `--no-verify`, so it is enforcement, not a guarantee); (b) a full-history
   scan (`gitleaks git --log-opts="--all"`, run 2026-08-21 against all 265 commits across
   all branches at the time of that run) found zero leaks. History cleanliness is
   point-in-time verified, not continuously re-checked -- a future scan may find a different
   result if the enforcement in (a) is ever bypassed.
4. **The corpus write surface is narrow and validated at the sink.** The only unauthenticated
   write path (`serve`'s `/remember`, and the CLI's `remember` verb) appends to an inbox file,
   never edits an existing document. Every field that could forge the inbox's own parsing
   structure (a line that looks like the entry separator, an embedded metadata comment, a
   `from`/`session`/`corrects` value containing characters outside `[\w.-]`) is rejected using
   the SAME regex OBJECTS the real parser uses (`_reject_inbox_injection` imports
   `INBOX_META_RE`/`META_RE`/`SEPARATOR` directly from `connectors/inbox.py` and
   `connectors/md_memory.py`, verified live -- it does not duplicate the pattern literals) --
   so the guard structurally cannot drift from what would actually be misparsed. This
   validates structurally at the sink; it does not (and cannot) validate the semantic truth of
   what is being remembered -- that is a human/agent judgment call, not this engine's job.
5. **Corpus mutations are lock-protected and filesystem-fenced.** Every writer (`index`,
   `promote`, `delete`, `ingest`) takes an exclusive `flock`-based `WriteLock`
   (`writelock.py`); readers take a non-blocking shared `IndexReadLock` that retries briefly
   then refuses loudly (`IndexReadUnavailable`) rather than serving a torn read during an
   active rebuild. `assert_local_filesystem()` explicitly refuses to operate on a network
   filesystem mount (NFS/SMB), because `flock` is documented as unreliable-to-a-no-op there --
   this is checked, not assumed.
6. **Direct runtime dependency surface is intentionally small; total is not.** Three DIRECT
   runtime dependencies: `lancedb`, `pyyaml`, `sentence-transformers` (`pyproject.toml`). No
   web framework, no `requests` (LLM/vision gateway calls use stdlib `urllib`). But
   `sentence-transformers` alone pulls in `torch`/`transformers`/etc., so the TOTAL pinned
   transitive dependency graph is 77 packages (`uv.lock`, counted at the commit that
   introduced this document) -- a procurement reviewer evaluating attack surface should weigh
   the total, not just the direct count. `uv.lock` pins exact resolved versions of every one
   of them; CI installs from the lock file (`uv sync --locked`), which refuses to proceed if
   the lock is stale relative to `pyproject.toml` -- a dependency version bump (direct or
   transitive) requires an explicit, reviewable `uv lock` commit rather than silently picking
   up whatever PyPI has published since the lock was last generated. No SBOM or
   package-signature verification exists beyond this version pinning.

### Known, documented weaknesses (not fixed, tracked)

These are real gaps, not theoretical ones -- each is tracked with an issue number in
`docs/BACKLOG.md` (grep for the number to see current status, which may have advanced since
this document was last updated):

- **No erasure path exists yet** (backlog #6). Soft-delete (tombstoning) exists and removes a
  document from retrieval; nothing removes a document from the corpus's git history, the
  append-only audit log, or backup archives. If you ingest something you need to be able to
  permanently and completely destroy, this engine cannot yet do that. **This decision is
  currently pending explicit operator sign-off** -- do not treat any stated resolution in
  other docs as final without checking `docs/BACKLOG.md` #6's current status.
- **Enrichment (the `--enrich` indexing step) has a retrieval-poisoning defense, not an
  elimination.** (Backlog #5, `src/alexandria/untrusted.py` + `enrich.py`.) A hostile document
  cannot forge prompt-structure delimiters (escaped), the enrichment prompt carries inert-data
  framing, an instruction-shaped or off-topic-faithfulness-failing synthetic "hypothetical
  question" is filtered before it can boost retrieval ranking, and a bad payload can be
  force-invalidated. **None of this eliminates prompt injection risk** -- no prompt-level
  defense is complete. The durable mitigation is architectural: the synthesis LLM holds no
  tool access, so an injected instruction cannot directly cause the model to take an action --
  it can, at most, distort a synthesized answer's content. Do not give the synthesis path tool
  access; this is the actual floor the mitigations above sit on top of.
- **No RBAC, no multi-tenant isolation.** (See "What is out of scope" below.) A single corpus
  is fully readable/writable by anyone who can reach the CLI or an unauthenticated loopback
  `serve` instance on the host it runs on. Access control at this stage is entirely "who can
  execute code as this OS user / reach this loopback port," the same boundary as any other
  local developer tool.
- **`--caller` provides no forgery protection.** It labels which TOOL is invoking the CLI
  (e.g. distinguishing a documented external caller like the pi extension from an ad-hoc
  script), never a verified identity. See "Trust boundaries" item 1 above.

### What is deliberately out of scope right now

Per `serve.py`'s own module docstring and `docs/SPEC-multi-tenant-and-learning-loop.md`'s
explicit non-goals: TLS termination, authentication beyond filesystem/socket identity, a
process manager/supervisor, full RBAC/role hierarchies, cross-organization federation, and
online learning from unvetted input. These are not oversights -- building them before the
single-operator fundamentals (retrieval correctness, write-path safety, injection resistance)
were solid would have been the wrong sequencing. See `docs/BACKLOG.md`'s "P2/P3 -- Deferred,
with named triggers" section for what would need to be true before each of these gets built.

## Recovery time objective (RTO)

**Scenario this covers**: rebuild-from-intact-corpus (`alexandria index --rebuild` against an
already-present, uncorrupted `sources/` tree) -- the number an operator needs for "how long is
retrieval unavailable while the index is regenerated." This is a DIFFERENT, larger number than
restore-from-backup-then-rebuild (`alexandria restore` + `--rebuild`), which additionally
includes backup-archive retrieval/extraction time; that combined number is not yet measured
either and would need its own entry once it is.

**Target**: < 30 minutes (the phase-1 design gate, `README.md`).

**Last known measurement**: ~80 minutes, per `docs/SPEC-multi-tenant-and-learning-loop.md`
section G1 -- an unsourced historical note (no stated corpus size, embed provider, or hardware
at measurement time), not a documented methodology. Backlog item **#11** (see
`docs/BACKLOG.md`) tracks running a real, methodology-documented remeasurement against the
current live corpus (33.5k documents, ~13GB as of 2026-08-20) -- **this requires an explicit
operator go-ahead before running, since a full `--rebuild` on the real corpus is a genuine
live operation**, even though it is now atomic/staged and safe to run (backlog #30 P2a: the
prior active index keeps serving until the new one validates and atomically replaces it).
This document will be updated with the real number, methodology, and date once that
measurement runs.

## Reporting practice for this document

If anything above is stale relative to the live code, that is itself a bug -- file it. This
document is verified-against-code as of the commit that introduced it; it is not maintained
automatically, and code changes do not require a `SECURITY.md` update as part of their own
review unless they change one of the boundaries described here.
