# WORK-ORDER: per-user bearer-token auth for `alexandria serve`

**Repo:** `~/codebase/alexandria` · **Branch:** `feat/serve-auth`, not `main`
**Venv:** `.venv` (`.venv/bin/python`, never system python) · Python 3.12
**Baseline:** `main` at 6075e7e, 1118 passing tests. Do not regress it.

> **Status: implemented 2026-08-23** on this branch. `src/alexandria/serve_auth.py`
> (token file load/verify/mint), `serve.py` per-request identity resolution +
> 401, `cli.py` `--token-file`/`--require-token`/`--add-token`, 10 tests in
> `tests/test_serve_auth.py` (vacuity-verified: 7/10 fail against pre-fix
> serve.py), docs updated (HTTP-API.md identity+auth section, SECURITY.md
> items 2/4, THREAT-MODEL.md remote-attacker + spoofing rows).
(Unmerged at baseline: feat/cross-writer-integrity 61ca644, feat/staleness-metric 0804617 — independent, not required here.)

## 0. Why this exists / why scoped this way

`serve` today has exactly one identity story (docs/HTTP-API.md): TCP callers are
always `local-anonymous` (a `caller`/`user` field in the body is deliberately
ignored — identity is never forgeable over HTTP); a unix socket binds identity
at bind time. Attribution is audit-only: "it is not an authorization boundary."
`ALEXANDRIA_SERVE_ALLOW_REMOTE=1` opens `/search`, `/answer`, and `/remember`
to ANY network caller with zero authentication — the whole "a customer can run
it" story (backlog #3) is blocked on exactly this. `/remember` writes to the
corpus (append-only inbox, then promote), so a remote unauthenticated caller
can inject content today.

Scope discipline, deliberately excluded:
- **No TLS.** §10 names TLS as not-built; bearer tokens over plain HTTP are
  sniffable on the wire. v1 documents that token use is only meaningful over
  loopback/LAN/VPN (or behind a TLS-terminating proxy). TLS stays §10-deferred.
- **No RBAC/roles/ACLs.** That is #26 step 4-5, after this lands. This work
  is only: a token identifies A user, and the identity is verified at the
  serve boundary (completing #8's structural-verification requirement for
  the serve path).
- **No per-token rate limits** (that is #31/#38 territory).
- **No config reload / process manager** (§10, unchanged).

## 1. Where things live

Code changes: `src/alexandria/serve.py` (handler + identity resolution),
`src/alexandria/cli.py` (`cmd_serve` + parser + a token-mint helper), a new
`src/alexandria/serve_auth.py` (token file load/verify/mint). Tests:
`tests/test_serve_auth.py` (new) + `tests/test_serve.py` (identity/401
regressions). Docs: `docs/HTTP-API.md` (auth section), `docs/SECURITY.md`
(threat-model row), `docs/WORK-ORDER-*` companion, AGENTS.md verb list if a
CLI subcommand is added. The real corpus and its `.alexandria` state are
never touched by tests or by the token store.

## 2. What already exists — call these, do not rebuild

- `serve.py`'s `dispatch(ctx, identity, method, path, body)` — identity is a
  plain `str` threaded through handlers; attribution goes to the audit
  logger as `caller=identity, user=identity`. Auth plugs in BEFORE dispatch
  (in the socket handler / `_make_handler_class`), producing the identity.
- `NonLoopbackRefused` + `REMOTE_ENV` (`ALEXANDRIA_SERVE_ALLOW_REMOTE`) —
  the existing remote-bind opt-in gate (S2). Keep it as the TRANSPORT gate;
  the token is the AUTH gate on top.
- `LOCAL_ANONYMOUS` — the current fallback identity for TCP.
- `--unix-socket IDENTITY=PATH` — per-socket identity binding (§5.3).
- `writelock`/`index_read_lock` — unchanged; auth is orthogonal.
- `docs/HTTP-API.md` "Identity (read this first)" — must be updated in lock
  step with the code, never left stale.

## 3. Shape

A token file (`serve --token-file PATH`, default
`ALEXANDRIA_SERVE_TOKENS` env, else off) holds lines `user:sha256(token)` —
hashed at rest so the file is not the credential. At startup serve loads it;
per request: `Authorization: Bearer <token>` → constant-time match against
each stored hash → identity = the matched user. No match + loopback +
no per-socket identity → `local-anonymous` (unchanged); no match on a
REMOTE bind → 401. A socket-bound identity always wins for its socket
(stronger, admin-configured), even if a token is also present.

Identity precedence per request: socket binding > valid token > local-anonymous.
Attribution: `caller=identity, user=identity` as today; body fields stay
ignored. `/remember` gets `from_=<identity>` as today.

**`--require-token` (env `ALEXANDRIA_SERVE_REQUIRE_TOKEN=1`):** for a
VPS/cloud-hosted knowledge base fronted by a TLS-terminating reverse proxy
(AWS ALB/nginx/etc.), the proxy forwards to loopback, so every proxied
request would look local under the default rule. This opt-in requires a
valid token for ALL TCP requests -- loopback included -- while
explicitly-bound unix sockets remain tokenless (admin-configured local
channels). The default stays tokenless-loopback for the single-user local
case. TLS remains the proxy's job (this server is stdlib HTTP only).

Token minting: `alexandria serve --add-token NAME` prints one token and
appends its sha256 to the token file (0600), so operators never hand-edit
hashes.

## 4. Deliverables

- `src/alexandria/serve_auth.py`:
  - `load_token_file(path) -> dict[str, str]` (user → sha256 hex; malformed
    lines refused loudly, file parsed strictly)
  - `verify_bearer(header: str | None, tokens) -> str | None` (constant-time;
    returns the matched user or None)
  - `mint_token() -> str` (secrets.token_urlsafe(32)) + `hash_token(t)`
- `serve.py`: read `Authorization` header in the handler; resolve identity
  with the precedence above; remote-without-valid-token → 401 JSON
  (`{"error": "unauthorized"}`); wire the token file into `ServeContext`/
  `build_serve_context`.
- `cli.py`: `serve --token-file` arg + `serve --add-token NAME` helper
  (prints token once, stores hash, 0600, refuses to overwrite an existing
  user).
- Tests (`tests/test_serve_auth.py`, offline, no model): valid token →
  identity + audit row; wrong token → 401; missing token on loopback →
  local-anonymous (unchanged); missing token with REMOTE bind → 401; socket
  identity beats a token; body `caller` field ignored even with a valid
  token; hashed-at-rest file (no plaintext token in the file); constant-time
  path (compare_digest used); `--add-token` mints + persists hash; malformed
  token file refuses loudly.
- Docs: HTTP-API.md auth section (header, 401 shape, precedence, hashed
  token file, TLS caveat); SECURITY.md threat-model row for remote serve
  (token required, TLS deferred, sniffing caveat).

## 5. THE TEST THAT MATTERS MOST

`test_remote_without_a_valid_token_is_401` — with `ALEXANDRIA_SERVE_ALLOW_REMOTE`
set and NO token (or a wrong token), a remote request to `/search` returns
401 and nothing is attributed; with a valid token it returns 200 and the
audit row carries the token's user. Plus `test_require_token_forces_401_on_loopback`:
with `--require-token` and no/wrong token, even a LOOPBACK request is 401
(the VPS/proxy-fronted deployment); with a valid token it is 200. This is the "a customer can run it"
unlock: today that request is served as `local-anonymous` with full access.

## 6. Constraints

- TDD: tests before implementation, suite green at every commit; baseline
  1118 must never regress.
- All tests offline (no model, no network): the auth path never constructs
  an embedder/engine — `dispatch`-level tests with a stubbed context.
- Do-not-modify: `dispatch`'s routing semantics (identity stays a plain str
  threaded to handlers; handlers keep their audit calls); the unix-socket
  binding mechanism; `writelock.py`; `config.py` defaults (token file is a
  serve option, not a global config change).
- No plaintext token in the token file, in tests, or in docs.

## 7. Known traps

- `BaseHTTPRequestHandler` closes the connection on unhandled exceptions —
  the 401 path must return a proper response, never raise (the existing
  `_make_handler_class` wrapper pattern handles this; keep 401 inside it).
- Header case: `Authorization` must be matched case-insensitively
  (RFC 7230); the `Bearer ` prefix is case-insensitive.
- A token file with an empty line / `#` comment must not break strict
  parsing — decide and test (recommend: skip blank/comment lines, refuse
  malformed `user:hash` lines).
- `hmac.compare_digest` on a user-provided string vs stored hex — always
  compare equal-length hex after validating the stored side is 64 hex chars;
  never compare against the plaintext token.
- The token file must be re-read per request? No — load once at startup
  (no config reload built); a reload hook is §10-deferred. `--add-token`
  writes the same file the running daemon loaded, so it takes effect on
  the NEXT serve start (document this).
- `--add-token` must not echo the token to the audit log or shell history
  guidance: print once, note "re-run to mint again".

## 8. Out of scope

TLS; RBAC/roles/per-route ACLs; token expiry/rotation UI (operator rotation =
re-mint + restart); per-token rate limits; multi-tenant scoping (#38); the
RBAC WORK-ORDER (#26) itself.

## 9. Verification before reporting done

```bash
unset ALEXANDRIA_EMBED_PROVIDER && PYTHONPATH=src .venv/bin/python -m pytest tests/ -q
HF_HUB_OFFLINE=1 HF_HOME=$(mktemp -d) PATH=/usr/bin:/bin PYTHONPATH=src .venv/bin/python -m pytest tests/ -q
PYTHONPATH=src .venv/bin/python scripts/precommit-scan.py --all
git diff --check
```

## 10. Report back

Modules + test counts; the §5 test's proof; end-to-end curl smoke against a
local serve instance (loopback + a fake remote header) with honest numbers;
any spec deviation and why; anything in traps that bit.
