# Extensions

Optional, per-harness add-ons. Nothing here is imported by the core engine.

- `pi/` -- the pi harness integration (`alexandria.ts`). Other harnesses may
  add their own extension directory following the same shape.
- The Prime Agent bridge skill lives OUTSIDE this repo, at
  `~/.prime/agent/skills/alexandria/` (a thin client over the HTTP API in
  `docs/HTTP-API.md`).

Rule: an extension may only consume the public CLI/HTTP surface. It must never
import `src/` internals. Harness names must never appear in the engine core.
