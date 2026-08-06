# Alexandria Pi extension

Two tools for Pi: `alexandria-search` (hybrid retrieval over the corpus) and
`alexandria-answer` (full synthesis pipeline, cited answer page).

**Do not install yet.** Per `docs/SPEC-phase3-harness.md` the extension is
inert until the blinded side-by-side gate passes — nothing switches without
the gate verdict and Stanley's call. For test-only sessions:

```bash
cp extensions/pi/alexandria.ts ~/.pi/agent/extensions/alexandria.ts
# then /reload in pi, or:
pi -e ./extensions/pi/alexandria.ts
```

Environment: `ALEXANDRIA_CORPUS` (default `~/alexandria-corpus`),
`ALEXANDRIA_BIN` (default `alexandria`). `alexandria-answer` needs the LLM
gateway config from `docs/QUICKSTART.md` (`--base-url` / `--api-key-env`
defaults are the local gateway).
