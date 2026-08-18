# WORK ORDER — Credential-isolated remote serve

**Repo:** `~/codebase/alexandria` · **Branch:** `security/credential-isolated-serve`, not `main`

**Venv:** `.venv` (`.venv/bin/python`, never system Python) · Python 3.12

**Baseline:** `main` at **755 collected tests** (local collection at authoring time; passing count must be established and recorded with §9 before implementation). Do not regress it.

---

> **Execution authority and safety boundary — read before anything else.**
>
> This is a **design-and-authoring work order**, not authorization to operate a
> remote host. An implementer may create the code, tests, generic example, and
> documentation on the named branch and run local offline tests. They must **not**
> read, print, copy, mint, revoke, rotate, decrypt, transmit, or otherwise handle
> any production credential; inspect a process environment; contact a remote
> gateway; modify a remote unit or filesystem; run `systemctl`; kill the legacy
> process; or restart/deploy a service. Do not put a credential literal in source,
> test fixtures, documentation, commit messages, shell history, command line,
> configuration, or logs.
>
> The human-run procedure in §8 is deliberately separated and labeled
> **HUMAN-ADMIN / HUMAN-OPERATOR ONLY**. It is a reviewed future trigger, not a
> command sequence to execute while implementing this work order. The administrator
> with gateway authority and the operator authorized for the remote host must each
> explicitly approve their respective steps. Stop at each hold point.

## 0. Why this exists / why scoped this way

The remote `alexandria serve` process currently has its gateway credential in a
same-user process environment. That makes the value available through ordinary
process-environment inspection by that account and descendants, and makes an
orphan/nohup-style process difficult to inventory and replace safely. A staged
remote user systemd unit is inactive, does not supply the credential, is reported
as mode `0777` in the host mapping, and has no service hardening. The remote user
manager supports systemd 252 and `systemd-creds`.

The target is a **dedicated remote service identity and credential transport**:
systemd decrypts an encrypted named credential for one service invocation; the
application reads that value into memory once and passes it explicitly to its LLM
clients. It must not put the credential back into `os.environ`, an argument vector,
a config file, a runbook, a log, or a repository artifact. The product requirement
for a dedicated, scoped fleet-gateway virtual key remains in force. Gateway-admin
mint authority is unavailable to the implementer, so obtaining that key is a
human-admin-gated operation, not an implementation task.

This work order intentionally fixes **credential injection for `serve` only**. The
existing env-name fallback remains the backwards-compatible local CLI behavior.
The macOS `scripts/serve-launchd.sh` bridge is a keychain-to-environment pattern
for macOS; it is neither a Linux design nor a source to adapt or copy. Do not
modify it.

This work does not claim to make a secret unknowable to code that runs as the
service identity. A live Python process needs the plaintext long enough to make an
HTTPS request. The goal is to remove the broad, inherited environment exposure and
at-rest plaintext while minimizing which process and filesystem surface can access
it, with clear ownership and rollback controls.

## 1. Where things live

- Engine implementation: `src/alexandria/` in this repository.
- Engine tests: `tests/`, all synthetic/offline. The new credential tests must not
  call a real gateway or read any actual credential.
- Generic deployment example: `deploy/systemd/alexandria-serve.service.example`.
  This is deliberately a template, never an installed production unit. It contains
  no private host, codename, account, corpus path, gateway URL, credential value,
  or real inventory identifier.
- This work order: `docs/WORK-ORDER-credential-isolated-serve.md`.
- Corpus content/index/state and site-specific deployment notes are outside this
  repository. Never add them here. Do not call the remote deployment a local
  machine or host in repository text; call it the **remote service**.

The generic `deploy/systemd/` example may name the fixed system credential
`alexandria-llm-key`; that is a non-secret interface name, not a credential value.
The name binds the encrypted blob to its intended consumer and must remain the
single source of credential material in the remote unit.

## 2. What already exists — call these, do not rebuild them

- `src/alexandria/llm.py::LLMClient`: a dataclass whose request path currently
  obtains its bearer value only with `os.environ.get(self.api_key_env, "none")`.
  Preserve its retry policy, timeout behavior, prompt-cache nonce, usage accounting,
  and the temperature-zero refusal guard for known-bad models.
- `src/alexandria/cli.py::run_answer`: creates the writer and graders. Its current
  `api_key_env` parameter is the local/CLI compatibility boundary.
- `src/alexandria/serve.py::build_serve_context`: builds a long-lived
  `ServeContext`; `/answer` calls `run_answer`. It currently stores non-secret LLM
  defaults from environment variables. It is the only remote serve injection point
  for this work order.
- `tests/test_llm.py`: uses mocked `_open_with_deadline` and can assert the outbound
  `urllib.request.Request` headers without a network call.
- `tests/test_serve.py`: already creates a synthetic corpus and patches local
  dependencies. Use it for serve-context propagation, never a remote systemd test.
- `scripts/serve-launchd.sh`: macOS-only keychain bridge. **Do not change, source,
  invoke, port, reference from the Linux example, or use as a model for Linux.**
- systemd 252 `LoadCredentialEncrypted=` plus `systemd-creds encrypt`: systemd
  decrypts and authenticates a named encrypted blob at activation, exposes its
  plaintext as a read-only file under the per-unit `$CREDENTIALS_DIRECTORY`, and
  exposes that directory only to the unit `User=` (and privileged administrators).
  The encrypted input is not required to be readable by the service process.

## 3. The shape this work order builds

```text
human administrator mints scoped remote gateway key (outside this repo)
        │  value entered only through an approved secret-safe local channel
        ▼
systemd-creds encrypt --name=alexandria-llm-key ... → protected encrypted credential store
        │  LoadCredentialEncrypted=alexandria-llm-key (named system credential only)
        ▼
systemd creates $CREDENTIALS_DIRECTORY/alexandria-llm-key (read-only, per unit)
        │  service startup reads exact regular file once; no os.environ export
        ▼
ServeContext.api_key (repr=False) → run_answer(..., api_key=...) → LLMClient(api_key=...)
        │                                             │
        └──── no value in HTTP response, error, repr, argv, config, or log ────┘
```

### 3.1 Explicit-key client contract

Add an optional `api_key: str | None = field(default=None, repr=False)` to
`LLMClient`. The effective bearer token is:

1. `api_key` when it is not `None` (including an intentionally empty explicit
   value, which must not fall back), otherwise
2. the existing `os.environ.get(api_key_env, "none")` compatibility path.

Do not add an API that accepts a credential path, environment variable name,
callable, mapping, arbitrary file descriptor, or command-line value. The only
new public input is already-read, in-memory text. Never serialize this field.
`repr(client)`, dataclass field representation, exception arguments, diagnostics,
and request error messages must not contain the explicit value.

Centralize the selection in one small private helper (for example,
`_effective_api_key`) so every request path has exactly one precedence decision.
Use the selected value solely to form the `Authorization` request header. Never
persist it on a request result, usage record, cache key, audit entry, or `LLMError`.
The existing environment fallback is not a validation mechanism; retain its current
`"none"` behavior for callers without an explicit key.

### 3.2 Serve-only credential load and propagation

In `serve.py`, define a non-configurable constant such as
`SYSTEM_CREDENTIAL_NAME = "alexandria-llm-key"` and a small private loader. At
serve-context construction, it must:

1. require `CREDENTIALS_DIRECTORY` to be set; otherwise raise a named,
   credential-free startup exception (for example `ServeCredentialError`),
2. form only `Path(os.environ["CREDENTIALS_DIRECTORY"]) / SYSTEM_CREDENTIAL_NAME`,
3. reject an empty directory string, a non-directory, a missing file, a symlink,
   a non-regular file, a group/world-readable file, invalid UTF-8, embedded NUL or
   control characters, or an empty/whitespace-only credential,
4. use `lstat()` before open and an `O_NOFOLLOW` / regular-file check where the
   platform supports it, then read once with a bounded maximum compatible with the
   systemd credential limit; mitigate TOCTOU by treating any unsafe open/read result
   as a failure rather than resolving or following a path,
5. strip only one final `\n` or `\r\n` introduced by credential storage (do not
   broadly normalize the value), and
6. return the plaintext in memory without assigning it to `os.environ`, a global,
   a printed error, a config object intended for serialization, or an exception.

Do **not** impose a gateway-vendor prefix or an arbitrary token length: the scoped
fleet gateway controls the token format and a guessed format turns a credential
transport change into an availability risk. UTF-8/non-control/nonblank validation is
format-neutral and sufficient to reject accidental multiline or binary input.

The loader must not accept a caller-controlled filename, and `serve` must not
fallback to `ALEXANDRIA_LLM_KEY`, `ALEXANDRIA_LLM_KEY_ENV`, any environment name,
or any local-keychain mechanism. A remote service without the named system
credential must fail closed before it binds a socket. The remaining non-secret
LLM defaults can retain their present compatibility behavior. Update
`ServeContext` with `api_key: str = field(repr=False)` and update
`run_answer` to accept an optional keyword-only `api_key`; pass it to every writer
and grader `LLMClient`. Direct CLI `answer` and other existing callers leave it
as `None`, retaining their env fallback.

Do not expose a credential key ID, token fingerprint, or credential-loaded status
on the unauthenticated `/health` endpoint. The current gateway protocol does not
provide an established safe token-identity field. A human-admin validation must use
a provider-side activity/key-status view correlated by the administrator's recorded
non-secret inventory identifier, not a new remote API or a guessed header format.

### 3.3 Error and redaction contract

The change must preserve useful failures without reflecting a credential. The
credential loader error may name the stable credential **name** and a failure class
(missing, unsafe type/mode, unreadable, invalid encoding, blank); it must not print
its path, contents, byte count, source file text, exception repr if that can include
the path/value, or the surrounding environment. `cmd_serve` must catch the named
startup error alongside `NonLoopbackRefused`, print a fixed safe failure message,
and exit nonzero. Do not use `traceback.print_exc()` for credential-loader
exceptions.

For outbound LLM failures, treat gateway-provided HTTP response bodies as
untrusted: today `HTTPError` detail is copied into `LLMError`. A provider can echo
an authorization value or request data. Add a single small redaction function used
before constructing *every* `LLMError` from external text (HTTP error detail,
`URLError`, JSON/shape diagnostics, and advisory usage exception). It must replace
all occurrences of the current explicit key with a fixed marker and avoid echoing
request headers. Keep existing useful bounded status/message semantics. Do not
install a broad logging framework or a speculative secret scanner; this project has
no application logging configuration, and a generic filter cannot reliably protect
arbitrary caller logging. The reliable contract here is no credential in values that
this engine creates or returns, plus a targeted regression test for every constructed
error path above.

## 4. Deliverables

### 4.1 `src/alexandria/llm.py` — minimal explicit in-memory credential API

- Add `api_key` as an optional, `repr=False` dataclass field.
- Implement explicit-over-environment effective-key precedence in one helper.
- Use it in the `Authorization` header only.
- Add the narrow error-redaction helper described in §3.3. It must never mutate
  caller strings in place or log either input.
- Do not modify the known temperature-zero refusal guard, retry status set,
  retry/backoff, cache-buster behavior, defaults, provider URL shape, or
  `ScriptedClient` contract.

### 4.2 `src/alexandria/cli.py` and `src/alexandria/serve.py` — one remote path

- Add an optional keyword-only `api_key` to `run_answer` and propagate it to all
  three constructed LLM clients. Its default is `None` to preserve CLI behavior.
- Add the private, fixed-name credential loader and named safe error in `serve.py`.
  Make `build_serve_context` load it before expensive warm-up/binding and store it
  only in the `repr=False` `ServeContext.api_key` field.
- Make `/answer` pass `ctx.api_key` to `run_answer`; no request body may override
  it.
- Make `cmd_serve` safely report the named loader failure and return nonzero.
- Extend `__all__` only if the tests need to import a named public failure type;
  otherwise keep the loader private.
- Do not add a generic "credential provider", an endpoint, a CLI `--api-key`, a
  service reload API, a rotation daemon, credential files under the corpus, or
  environment re-export.

### 4.3 `tests/test_llm.py` and `tests/test_serve.py` — offline mutation tests

Use only synthetic sentinel strings invented in the test. Do not use a
production-shaped prefix, a copied value, or a real gateway.

`tests/test_llm.py` must add:

- an outbound-header test that monkeypatches `_open_with_deadline`, sets the legacy
  environment variable to a hostile sentinel, constructs `LLMClient(api_key=chosen
  sentinel)`, and proves the captured `Authorization` header contains only the
  explicit sentinel;
- a negative guard that patches the environment lookup (or uses a mapping that
  raises) and proves explicit mode never reads the legacy variable at all;
- an environment-fallback compatibility test when `api_key is None`;
- a representation/error-hygiene test: `repr(LLMClient(api_key=sentinel))`, an
  HTTP error body that deliberately echoes the sentinel, and a transport/advisory
  exception whose text includes the sentinel must all omit it from resulting
  output/error text while retaining useful non-secret failure context.

`tests/test_serve.py` must add fixture-based loader tests that create a temporary
credential directory under pytest control and verify:

- the valid fixed-name file is read and placed in `ServeContext.api_key` but does
  not appear in `repr(ctx)`; this focused unit test is the application-side
  defense-in-depth check on the per-unit read-only credential materialization, not
  a replacement for systemd ownership enforcement;
- a hostile `ALEXANDRIA_LLM_KEY` and hostile `ALEXANDRIA_LLM_KEY_ENV` cannot win;
  capture `run_answer` from `/answer` and assert it receives only the credential
  file sentinel;
- missing directory/file, symlink, directory/non-regular file, loose permission,
  invalid UTF-8, and blank credential all refuse before binding and produce only
  safe error text; and
- no payload field can supply or replace the service credential.

Keep tests platform-aware: permission-mode checks may be skipped only when the test
platform cannot enforce POSIX modes, with an explicit reason. No test should invoke
systemd, `systemd-creds`, a subprocess that contains a sentinel in argv, or a real
HTTP gateway.

### 4.4 `deploy/systemd/alexandria-serve.service.example` — generic locked-down example

Create a new tracked `deploy/systemd/` directory and this **generic example only**.
It must contain conspicuous comments: copy and fill it only in private operator
notes; do not commit an installed unit, drop-in, real path, account, address,
gateway setting, corpus location, or credential ciphertext.

The example must use a dedicated non-login service account placeholder:
`User=alexandria-svc`, `Group=alexandria-svc`, and a `StateDirectory=alexandria`
managed state directory (the human operator supplies correct state/corpus ownership
outside the template). It must start the installed `alexandria serve` via an
operator-supplied absolute executable path placeholder only; no shell wrapper,
`bash -c`, `EnvironmentFile=`, `Environment=ALEXANDRIA_LLM_KEY=...`, `PassEnvironment=`,
`SetCredential=`, `SetCredentialEncrypted=`, command substitution, or credential
path/value appears in `ExecStart=`.

The only credential directive must be exactly:

```ini
LoadCredentialEncrypted=alexandria-llm-key
```

Do not specify a path: systemd 252 searches the named encrypted system credential
store and authenticates/decrypts it at activation. This deliberately requires one
named system credential and avoids a repository-tracked ciphertext or local
path. The plaintext credential is then provided to the process only at
`$CREDENTIALS_DIRECTORY/alexandria-llm-key` for the application loader.

Include, with short rationale comments, at least the following hardening baseline;
validate each against the target systemd 252 **and the target's Python/model/gateway
runtime** before the human trigger:

```ini
NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=yes
ProtectControlGroups=yes
ProtectKernelModules=yes
ProtectKernelTunables=yes
ProtectKernelLogs=yes
ProtectClock=yes
ProtectHostname=yes
ProtectProc=invisible
ProcSubset=pid
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
RestrictNamespaces=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
RestrictRealtime=yes
SystemCallArchitectures=native
UMask=0077
```

Do **not** enable `DynamicUser=yes` in this first migration: the remote corpus/state
ownership and any tunnel/socket ACLs must first be explicitly reconciled with the
new fixed service account. `ProtectSystem=strict` requires deliberate writable
allow-lists. `StateDirectory=alexandria` is writable by the service; add only
reviewed, site-specific `ReadWritePaths=` entries in the private installation
notes if corpus/index state lives elsewhere. Do not weaken the sandbox merely to
make an unknown path work. Confirm that the unit's required AFs are sufficient;
for a loopback gateway and local Unix sockets they should be, but do not guess.

`ProtectProc=invisible` plus `ProcSubset=pid` is intentional defense in depth, but
can affect libraries that inspect `/proc`; a private staging start must exercise the
actual Python imports and answer path before cutover. Likewise, test name resolution
and TLS in the staging service: a resolver configuration concealed by the filesystem
namespace is an availability issue to solve with the narrowest reviewed read-only
resolver visibility, not by removing `ProtectSystem` or passing a credential in an
environment variable.

Use `Restart=on-failure`, conservative restart delay, and a bounded stop timeout.
Do not include `ExecStartPre=` that reads, tests, cats, hashes, copies, or prints
the plaintext credential: it would add another process and exposure point. The
application's named loader is the sole reader. Do not add `Environment=` for
non-secret host-specific values in this generic template; private operator notes
must supply only reviewed non-secret configuration through the approved site method.

### 4.5 This work order and deployment safety documentation

Keep this work order as the reviewable source for the design, evidence commands,
limitations, test plan, and explicit human trigger. It must not gain any actual host
or provider identity. Update existing broad deployment docs only if needed to link
this work order; do not revise the already-ratified topology decision or migrate
corpus material.

## 5. THE TEST THAT MATTERS MOST

**A hostile legacy environment credential must never be used by the remote serve
path when a named system credential is present.**

The mutation-proof test must simultaneously set `ALEXANDRIA_LLM_KEY` and the
custom env-name selector to different hostile sentinels, construct a synthetic
serve context from a temporary `$CREDENTIALS_DIRECTORY/alexandria-llm-key`, and
intercept the `/answer` → `run_answer` call. It passes only if the in-memory
credential-file sentinel arrives as `api_key`, neither environment sentinel is
consulted, and neither appears in `repr` or safe errors.

Temporarily remove the `api_key` propagation or change precedence to environment
first and prove this test fails. A merely successful answer, a test that asserts
only that a file was read, or a test that never sets hostile environment values is
not adequate: those all permit the current exposure to return unnoticed.

## 6. Constraints

- TDD: write the focused tests before implementation. Every source change gets a
  failing test first, then the smallest code that makes it pass.
- All LLM/retrieval tests are offline. Use `ScriptedClient` / existing fake-engine
  patterns and monkeypatch stdlib request opening. Never make a real LLM call.
- Credentials may exist in memory only for the current client/service lifetime.
  Never write them to fixture snapshots, recordings, `tmp_path` beyond a synthetic
  test's controlled credential file, cache, audit log, traceback, or test assertion
  failure message.
- Do not change `scripts/serve-launchd.sh`, its keychain behavior, `llm.py`'s
  known-bad-model temperature guard, retrieval behavior, corpus/index code,
  gateway/server configuration, or external services.
- Do not place private remote hostnames, codenames, addresses, usernames, account
  identifiers, key IDs, key names other than the generic fixed system credential
  name, credential ciphertext, or credential literals in the repository. The leak
  scanner is a gate; fix a blocked string rather than weakening the scanner.
- All normal `serve` startup errors must be safe under `systemctl status` / journal
  capture. They may reveal a stable error class and generic component name; they
  must never reveal the credential, an absolute credential path, the process
  environment, or an exception repr derived from a credential read.
- Do not invent a gateway key-ID API or return token metadata via an unauthenticated
  Alexandria endpoint. Provider-side inventory/status is the rotation proof.
- This is a system service example because systemd encrypted credential storage and
  host/TPM key material are typically administered at the system level. If the
  actual deployment must remain a user unit, stop and obtain a separate reviewed
  design: user-manager credential search paths, host-key access, and ownership
  differ. Do not silently translate these instructions to `systemctl --user`.

## 7. Known traps and security limits

1. **Environment variables are the wrong secret transport.** They are inherited by
   descendants and can be exposed through process inspection, debug/support
   collection, or manager interfaces. Never "solve" a missing credential by adding
   `Environment=`, `EnvironmentFile=`, `PassEnvironment=`, an export, or an
   `ExecStart` shell wrapper.
2. **`LoadCredentialEncrypted=` is not magic process isolation.** systemd decrypts
   plaintext into a read-only per-unit credential directory. The service `User=`
   (and root) can read it; arbitrary code execution, a debugger, `/proc` access
   permitted to the same UID, or a compromised dependency running as that account
   can still exfiltrate it. The hardening profile narrows but does not eliminate
   that risk. Root/host compromise can access the running process or host decryption
   material. State this limitation honestly in the change report.
3. **A service account changes filesystem access.** Least privilege requires the
   dedicated `alexandria-svc` account to own only necessary state and have minimal
   read access to corpus material. Group ACLs are an explicit authorization grant,
   not a substitute for user separation. Inventory and remove broad same-UID/group
   access before cutover. Do not make credential files, unit files, or corpus trees
   world-writable; the reported `0777` staged-unit mode is a release blocker.
4. **Encrypted at rest is host-bound by a key choice.** On systemd 252,
   `systemd-creds encrypt` can use TPM2, a root-only host key, or both. The chosen
   `--with-key=` mode is an operator decision: `auto` commonly binds to host+TPM2
   when available; `host` is recoverable with host filesystem/root access; TPM2
   binding can make host replacement/recovery impossible without a planned
   re-encryption. `tpm2-absent` provides neither confidentiality nor authenticity
   and is forbidden. Record the non-secret chosen protection mode and recovery owner
   in private inventory; never record the plaintext or ciphertext in this repo.
5. **Name binding matters.** `systemd-creds encrypt --name=alexandria-llm-key`
   embeds the credential purpose name. An encrypted blob with another name must
   fail rather than be repurposed. The unit and loader must use the exact same fixed
   name. Do not use an empty name.
6. **Rotation proof comes before destruction.** A 200 from `/health` proves neither
   that `/answer` uses the intended credential nor which gateway key was accepted.
   Do not stop the legacy process or revoke its key based on liveness alone. Require
   a controlled real authenticated request and provider-side confirmation of the
   new inventory key's identifier/status before retirement/revocation.
7. **Do not print the wrong evidence.** Prohibited commands include `env`,
   `printenv`, `ps e`, `/proc/<pid>/environ`, `systemctl show --all`, `systemctl cat`
   when private drop-ins might be included, `journalctl -o verbose`, `cat`/`stat` of
   the plaintext credential path, shell `set -x`, heredocs containing the value,
   history expansion, `curl -v`, and any request/header dump. A command that might
   disclose values is not made safe by piping to `grep` afterward.
8. **Hardening needs test mode.** A directive can break Python, model loading,
   corpus paths, sockets, DNS/TLS resolution, or the gateway. Use a private
   staging/replacement unit and an approved rollback plan. `ProtectProc=invisible`
   plus `ProcSubset=pid` must be exercised with the actual Python imports, and any
   resolver visibility needed for a non-loopback gateway must be added only as a
   narrow reviewed read-only exception. Do not remove directives as a first
   response. Inspect denial summaries and make the smallest reviewed allow-list
   change without revealing credentials.
9. **No automatic reload/rotation.** `LoadCredentialEncrypted=` is materialized at
   service start. Re-encrypting/replacing the stored blob does not safely rotate an
   already-running Python process. This work order intentionally has no watcher or
   hot reload. A reviewed human restart of the replacement is the activation event.

## 8. Human-only deployment and rotation procedure (DO NOT EXECUTE AS PART OF THIS WORK ORDER)

This section is a future runbook skeleton for a specifically authorized human
administrator and human remote operator. Replace angle-bracket placeholders only
in private, access-controlled operator material, never in this repository. All
commands below are **examples to be reviewed and triggered by humans**, not commands
for an implementer or automation to run. Never use shell tracing; begin the private
session with history-safe controls per the organization's procedure.

### 8.1 Approval, ownership, and preconditions — HUMAN REVIEW HOLD

1. **Name owners.** Record the human gateway administrator, remote system
   administrator, service owner/on-call, security approver, and rollback owner.
   Confirm the gateway administrator can mint/revoke but the service operator
   cannot view unrelated keys. Confirm systemd **252** and `systemd-creds` are
   installed on the remote system.
2. **Inventory non-secret metadata only.** Before minting, record in a protected
   key inventory: purpose `alexandria remote serve`; environment; service account;
   approved gateway audience/base URL; allowed model scope; rate/quota ceiling;
   creation/expiry/review time; human owners; and a provider-assigned key
   identifier/fingerprint once minted. Do **not** record the token, ciphertext,
   local credential path, process environment, or a remote host name in this repo.
3. **Confirm least privilege.** The gateway administrator mints a new dedicated
   remote service virtual key with only the required inference endpoint/model scope,
   a bounded quota/rate, explicit expiration/review, and no mint/admin, model-admin,
   cross-tenant, or unrelated service privileges. This is **HUMAN GATEWAY-ADMIN
   ONLY**. The administrator transfers the new value only through an approved
   one-time secret entry channel directly to the encryption step; neither the
   implementer nor a terminal transcript should receive it.
4. **Preflight the unit safely.** The remote system administrator creates a new
   private staging/replacement unit from the reviewed generic example, owned by
   root with mode `0644` (or stricter site policy); configuration directories must
   not be group/world writable. Create/reconcile the dedicated non-login service
   account, corpus/state ownership, and minimum ACLs. The existing staged `0777`
   unit is not eligible for use until replaced with restrictive ownership/mode and
   independently reviewed. Do not change the legacy process yet.
5. **Agree rollback.** Rollback means stop/disable only the new replacement unit
   and preserve the legacy credential/key; it does **not** mean copying the old
   key into the new unit or reintroducing environment injection. Set an expiry for
   the new key long enough for validation, but do not revoke any predecessor at
   this point.

### 8.2 Encrypt and install the named system credential — HUMAN SYSTEM-ADMIN ONLY

The remote system administrator uses an approved secret-safe input method (for
example, a privileged password agent that does not echo and is not captured in
shell history) to supply the new value to `systemd-creds`. The intended semantics
are: encrypt with an explicit embedded name and write the ciphertext directly to
the protected **system encrypted credential store**. The service only names it via
`LoadCredentialEncrypted=alexandria-llm-key`; no `PATH` is put in the generic unit.

A private, reviewed command may have the following **shape** (the exact protected
store path and privilege wrapper are site-specific and must not be copied from this
repository):

```sh
# HUMAN-ONLY shape; values/placeholders are private. Do not paste credential text.
systemd-ask-password --no-tty --echo=masked '<private prompt>' \
  | systemd-creds encrypt --name=alexandria-llm-key --with-key=<approved-host-or-tpm-mode> \
      - <protected-encrypted-credential-store>/alexandria-llm-key
```

Before activation, the system administrator must verify **without reading contents**:

- the encrypted blob is root-owned and not group/world writable or readable;
- the parent credential-store directories are root-owned and not group/world
  writable;
- the blob filename and embedded `--name` match `alexandria-llm-key` exactly;
- the approved `--with-key=` protection mode is neither `tpm2-absent` nor an
  unreviewed recovery compromise; and
- the staging unit's parsed `LoadCredentialEncrypted=` contains only the fixed
  name, with no `Environment*`, `PassEnvironment=`, `SetCredential*`, shell wrapper,
  or secret-bearing drop-in.

**Safe evidence examples (HUMAN ONLY):** use metadata-only commands such as
`systemd-creds --version`, `systemd-analyze verify <private-unit-path>`,
`systemctl show <replacement-unit> -p LoadState -p ActiveState -p SubState -p User
-p Group -p FragmentPath`, and `namei -l <protected-encrypted-credential-store>`.
Review command output before retaining it; record only ownership/mode verdicts and
non-secret inventory IDs. Do not run `systemd-creds decrypt`, `cat` the blob or
plaintext credential, `ls -l`/`stat` on a path whose output includes sensitive
private locations in a shared transcript, or any command that prints an environment
or request header.

### 8.3 Activate and validate a replacement — HUMAN OPERATOR + GATEWAY ADMIN ONLY

1. Validate the unit syntax and hardening in a private staging context. Resolve
   sandbox denials by a reviewed minimal change; never turn off the profile broadly.
2. Start the **new replacement** only after the administrator confirms the encrypted
   credential installation. Do not stop, signal, or restart the legacy orphan.
3. Observe only safe unit state (`LoadState`, `ActiveState`, `SubState`, `MainPID`,
   restart count) and bounded service logs with a format that does not include
   environment, headers, or verbose properties. A credential-loader failure should
   show only its stable safe class.
4. Send one controlled, ordinary authenticated request through the approved private
   client route. Do not use verbose HTTP, dump headers, or send a credential on a
   command line. It must demonstrate the replacement's actual answer path, not only
   its unauthenticated health endpoint.
5. The gateway administrator checks the provider-side activity/audit/key-status view
   for the **new non-secret inventory identifier**: it must show the expected key
   as active and used by that controlled request with the approved scope. Record
   only time, result, key ID/fingerprint, and status—not request authorization or
   raw request data. This is the check-then-act gate that proves the new process is
   using the intended replacement credential.
6. Repeat a safe service health/read-path check and assess errors/denials. On any
   failure or ambiguity, stop the replacement, preserve the legacy key/process, and
   investigate under human authority. Do not rotate, revoke, or fall back to an env
   variable.

### 8.4 Retire only after validation — HUMAN CHANGE-APPROVAL HOLD

Only after §8.3(4–5) shows both a working replacement and provider-side use of the
new inventory key may the approved human operator retire the legacy orphan/nohup
process using the site's approved change procedure. Confirm that only the intended
replacement unit remains active via safe unit/process identity metadata; do not
print a process environment.

After a defined observation window with the replacement healthy and no need to
roll back, the gateway administrator disables/revokes the prior gateway key. Record
only its non-secret inventory identifier, time, approver, and revocation result.
Never revoke the existing key first, never reuse it in the new service, and never
claim rotation complete merely because a process is live.

### 8.5 Rollback and incident handling — HUMAN ONLY

- **Before predecessor revocation:** stop/disable the replacement; keep the prior
  key and legacy service untouched while the authorized owners diagnose.
- **After predecessor revocation:** recovery requires a newly minted least-privilege
  credential and the same controlled installation/validation flow. Do not retrieve
  or resurrect an old value from logs, environments, shell history, backups, or
  repository history.
- Suspected disclosure (including accidental environment/header/log output) is a
  security incident: restrict evidence circulation, notify the security/key owners,
  revoke the suspected key under human authority, mint a distinct replacement, and
  record only non-secret incident metadata.

## 9. Verification before reporting done

### 9.1 Local implementation verification

Run these from the named branch using the repository environment. They are local
and must make no real corpus, remote-host, systemd, or gateway call:

```bash
.venv/bin/python -m pytest tests/test_llm.py tests/test_serve.py -q
.venv/bin/python -m pytest tests/ -q
.venv/bin/python scripts/precommit-scan.py --all
```

If `.venv` is not present in an isolated worktree, create/use the project-managed
local environment deliberately (for example `uv run --locked --extra dev ...`) and
report the exact equivalent used; never silently use system Python. The baseline
header's count is a collection snapshot, not permission to skip the passing suite.

Before claiming completion, review the diff and prove all of the following:

```bash
# Metadata/source review only: does not read a credential or process environment.
git diff --check
git grep -nE 'ALEXANDRIA_LLM_KEY=|EnvironmentFile=|PassEnvironment=|SetCredential=' -- \
  deploy/systemd docs/WORK-ORDER-credential-isolated-serve.md
```

The grep must find no secret-injection directive in the new Linux example. Also
verify the fixture-only sentinel strings do not appear in generated artifacts or
committed documentation. Do not use broad secret scanners that print matching lines
from a potentially secret-bearing file.

### 9.2 Human validation evidence (future, not implementation work)

The authorized human report must contain only:

- non-secret unit ownership/mode and hardening-verification verdicts;
- unit state transitions and replacement process identity metadata;
- controlled-request success time/result;
- provider-side new key inventory identifier/status and controlled-use confirmation;
- legacy retirement and predecessor-revocation approval/time/result; and
- the systemd protection mode/recovery owner.

It must explicitly state that no environment dump, credential content, credential
path, header, process argument, ciphertext, or secret-adjacent log was collected.

## 10. Report back

Report all of the following, even if implementation stops early:

1. exact changed files and the minimal API/data-flow change;
2. baseline and final test commands/counts, plus the result of the focused §5
   mutation test (including how removing propagation made it fail);
3. proof that `repr`, HTTP-error detail, transport/advisory errors, loader errors,
   and service context do not emit test sentinels;
4. the generic systemd hardening/credential decisions and explicit limitations
   (service UID and root can still access a live credential; host/TPM recovery
   trade-off; no automatic rotation);
5. whether a human-admin deployment was **not** performed (it must be `not
   performed` for an implementation-only session);
6. every deviation, platform caveat, failed sandbox directive, or unresolved
   ownership/ACL question; and
7. the commit SHA on the named branch. Do not push, merge, deploy, restart, or
   trigger the human procedure from this work order branch.
