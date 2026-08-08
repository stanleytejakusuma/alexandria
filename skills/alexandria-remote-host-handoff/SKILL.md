---
name: alexandria-remote-host-handoff
description: Install and wire Alexandria (CLI + corpus + doctrine) on a remote agent host so the assistant session there can use it. Use when a host has no Alexandria yet, or when asked to "hand off Alexandria to the assistant" / "install the CLI on the remote host".
---

# Alexandria on a remote agent host — handoff skill

Goal: give the assistant agent session on a remote host its own Alexandria
installation: CLI on PATH, a local corpus seeded from that host's own memory
stores, gateway wiring, and the usage doctrine. Executed by the assistant
session itself; this skill is its checklist.

Host-specific values (the session substitutes its own):
- `$GATEWAY` — the LLM gateway base URL the host already uses for LLM calls
  (the CLI default `http://127.0.0.1:20128/v1` is the LOCAL gateway and is
  usually wrong on a remote host).
- `$KEY_ENV` — the env var name holding the gateway API key the host already
  uses; never print the key, never commit it.

## When to Use

- The host has no `alexandria` command, or `alexandria --help` shows an
  old/partial subcommand list.
- The assistant asks "what is Alexandria / how do I query it" — after this
  skill runs, the answer is: use the CLI + doctrine below.

## Prerequisites (verify first)

- `python3.12` available (`python3.12 --version`).
- `git` available.
- The gateway is reachable from the host: `curl -s -m 5 $GATEWAY/models`
  returns a JSON model list.
- A gateway API key is available to the session (its usual key source).

## Procedure

1. **Install the package**
   ```bash
   git clone https://github.com/stanleytejakusuma/alexandria ~/codebase/alexandria
   cd ~/codebase/alexandria
   python3.12 -m venv .venv
   .venv/bin/pip install -e .
   mkdir -p ~/.local/bin
   ln -sf "$HOME/codebase/alexandria/.venv/bin/alexandria" ~/.local/bin/alexandria
   ```
2. **Verify the CLI**
   ```bash
   alexandria --help   # expect: migrate,sync,remember,lint,index,search,eval,answer,wiki-site,audit,decay
   ```
   If a subcommand is missing, the checkout is stale — `git -C
   ~/codebase/alexandria pull` and reinstall.
3. **Create the corpus and seed it from the host's own memory**
   ```bash
   mkdir -p ~/alexandria-corpus/sources
   alexandria --corpus ~/alexandria-corpus sync markdown-memory \
     --memory-dir "$(ls -d ~/.pi/agent/pi-*-memory)" \
     --base-url $GATEWAY --model deepseek-v4-flash
   alexandria --corpus ~/alexandria-corpus sync inbox
   alexandria --corpus ~/alexandria-corpus sync journal
   alexandria --corpus ~/alexandria-corpus lint
   alexandria --corpus ~/alexandria-corpus index
   ```
   (markdown-memory reads the host's own memory store and distils it into
   corpus notes; inbox/journal are the curated write + accountability
   surfaces. `lint` must report 0 errors before indexing.)
4. **Verify end to end**
   ```bash
   alexandria --corpus ~/alexandria-corpus search "gateway api key" --k 2
   alexandria --corpus ~/alexandria-corpus audit
   ```
   Search must return ranked hits; `audit` must show the search row and the
   sync rows with caller tags.
5. **Usage doctrine (this is the contract, not a suggestion)**
   - **Search before asking**: for any question about past work, decisions,
     or project history, run `alexandria --corpus ~/alexandria-corpus search
     "<query>" --k 6` before answering from memory alone.
   - **Answer for synthesis**: `alexandria --corpus ~/alexandria-corpus
     answer "<question>" --base-url $GATEWAY --api-key-env $KEY_ENV` for
     cited, synthesized answers.
   - **Never auto-write**: the only write surface is the inbox:
     `alexandria --corpus ~/alexandria-corpus remember "<user-confirmed text>"
     --from assistant [--corrects <source_id>]` — only when the user
     explicitly asks to remember something; use their wording.
   - **Never persist generated content** (answers, digests, summaries) back
     into the corpus.
   - **Check the host's native memory too**: Alexandria recall is
     uncertified; a search miss does NOT excuse skipping the harness's own
     memory store (the store `markdown-memory` reads).
   - **Audit**: every search/answer/sync logs to
     `~/alexandria-corpus/.alexandria/audit/`; review with `alexandria audit`.
6. **Weekly loop (optional but recommended)**: sync markdown-memory + journal
   weekly, then review `alexandria audit` + the query log
   (`scripts/query-log-review.py --corpus ~/alexandria-corpus` from the
   checkout).

## Pitfalls

- The CLI's default base URL is `http://127.0.0.1:20128/v1` (the LOCAL
  gateway) — on a remote host every LLM call must pass `--base-url $GATEWAY`
  explicitly. This has bitten every installer.
- The fast-tier model guard: graders must run at temperature 0.1 (the CLI
  already does this) — do not "simplify" by removing it.
- `index` downloads the embedding model once (Qwen3-Embedding-0.6B via MLX);
  the first run is slow, later runs use the cache.
- Do not clone into a path with spaces; do not symlink the venv itself.
- Never paste the API key into a chat; read it from the session's usual key
  source (keychain/vault) into `$KEY_ENV`.

## Verification

- `alexandria --help` lists all 11 subcommands.
- `alexandria --corpus ~/alexandria-corpus lint` reports 0 errors.
- `alexandria --corpus ~/alexandria-corpus search "gateway api key" --k 2`
  returns ranked hits.
- `alexandria --corpus ~/alexandria-corpus audit` shows sync + search rows
  with caller tags.
- One live `answer` call emits a page with citations (takes a few minutes;
  do it once).
