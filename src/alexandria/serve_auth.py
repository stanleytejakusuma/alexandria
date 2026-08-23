"""Per-user bearer-token auth for `alexandria serve`.

§10 previously excluded auth beyond filesystem/socket identity; a
`ALEXANDRIA_SERVE_ALLOW_REMOTE=1` bind served every network caller as
``local-anonymous`` with full access, and `/remember` even writes to the
corpus. This module adds the minimal verified-identity boundary: a token
file of ``user:sha256(token)`` lines (hashed at rest -- the file is not the
credential), constant-time verification of ``Authorization: Bearer``
headers, and an operator-facing mint helper. TLS stays out of scope
(terminate at a proxy/ALB); the token is the auth boundary, and the token
file lives on the same host as the daemon.

Design notes (see docs/WORK-ORDER-serve-auth.md):

- Identity precedence per request: unix-socket binding > valid token >
  ``local-anonymous`` (loopback, default) or 401 (remote, or
  ``--require-token``).
- ``--require-token`` (env ``ALEXANDRIA_SERVE_REQUIRE_TOKEN=1``) forces a
  valid token for ALL TCP requests, loopback included -- the
  proxy-fronted VPS/cloud deployment, where the TLS-terminating proxy
  forwards to loopback and every proxied request would otherwise look
  local. Explicitly-bound unix sockets remain tokenless.
- Tokens are single-purpose: one line per user; rotation = re-mint +
  restart (the file is read once at startup; no config reload).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

TOKEN_FILE_DEFAULT = "serve-tokens.txt"


def hash_token(token: str) -> str:
    """sha256 hex of a token -- the only form stored on disk."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint_token() -> str:
    """A new random token (256 bits of entropy, URL-safe)."""
    return secrets.token_urlsafe(32)


def load_token_file(path) -> dict[str, str]:
    """Parse ``user:sha256hex`` lines into ``{user: hash}``.

    Blank lines and ``#`` comments are skipped; a malformed line (no colon,
    empty user, or a non-64-hex hash) refuses loudly -- a silently-dropped
    line would make a token stop working with no diagnosis, the same class
    of state-that-lies this project keeps rejecting.
    """
    tokens: dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            user, sep, digest = stripped.partition(":")
            if not sep or not user.strip():
                raise ValueError(
                    f"{path}:{lineno}: malformed token line (expected user:sha256hex)")
            user = user.strip()
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest.lower()):
                raise ValueError(
                    f"{path}:{lineno}: token hash for {user!r} is not 64 hex chars")
            if user in tokens:
                raise ValueError(f"{path}:{lineno}: duplicate user {user!r}")
            tokens[user] = digest.lower()
    return tokens


def verify_bearer(authorization: str | None, tokens: dict[str, str]) -> str | None:
    """Return the matched user for a valid ``Authorization: Bearer <token>``
    header, else None. Constant-time: the presented token is hashed and the
    digest compared with ``hmac.compare_digest`` against every stored hash
    (never against a plaintext token, which we do not keep)."""
    if not authorization or not tokens:
        return None
    scheme, _, credential = authorization.partition(" ")
    if scheme.lower() != "bearer" or not credential.strip():
        return None
    presented = hash_token(credential.strip())
    for user, stored in tokens.items():
        if hmac.compare_digest(presented, stored):
            return user
    return None
