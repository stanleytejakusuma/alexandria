# State of play — 2026-08-13

Written immediately before a context compaction, to survive it. This is the
handoff: what is built, what is paper, what is open, and the operational
knowledge that is expensive to rediscover.

Repo HEAD at time of writing: `75aa135`. Tests: **654 passing**.

---

## 1. What is BUILT (shipped code, tests behind every claim)

`SPEC-write-path-and-serve.md` is **fully implemented**. Every §9 gate maps to a
real, mutation-verified test.

| Module | Purpose |
|---|---|
| `pending.py` | zero-length markers, `O_CREAT\|O_EXCL` create / `unlink` consume |
| `writelock.py` | `fcntl.flock` write lock + local-filesystem refusal (NFS/SMB) |
| `promote.py` | the ordered five-step write; `test_hook` allows crash injection |
| `reconcile.py` | independent stranded-entry check; never consults the pending list |
| `liveness.py` | oldest-pending-age staleness; **not** a heartbeat, deliberately |
| `serve.py` | stdlib `http.server`: `/health` `/search` `/answer` `/remember` |
| `backup.py` | state backup/restore with a normalised-path allowlist |
| `index/manifest.py` | provider/model/dim/normalized/dtype; refuses on mismatch |
| `eval/negative.py` | negative cases, separation report, precision gate |
| `connectors/knowledge_graph.py` | the vault connector (predates this session) |

Session totals: **35+ commits, 10 new source modules, 17 new test files,
~9,500 insertions, tests 501 → 654.**

### Real bugs found and fixed this session

1. **The weekly loop had never run successfully once.** `scripts/run-weekly-loop.sh`
   referenced `$CORPUS/.alexandria/loop/weekly-digest.md` with no `mkdir -p`.
   Bash resolves redirects *before* running the command, so every `>>` aborted
   its own sync — while `git commit --allow-empty` succeeded, manufacturing
   evidence of success. Present since the script's first commit (Aug 8). This is
   why the corpus sat frozen for three days.
2. **Enrichment scoring ranked its own hits last.** `retrieval/search.py:208`
   `missing_targets` was a list of ids with no scores, so recovery computed
   `max(0.0, 0.0)` and a synthetic hypothetical-question hit landed dead last —
   neutralised in exactly the zero-overlap band it exists for.
3. **A warm server would serve pre-reindex results forever.** `SearchEngine._generation`
   was captured in `__init__` and keyed every cache entry. Now a property re-read
   per access (commit `500cd9e`).
4. **Inbox structure forgery.** `remember` interpolated caller-supplied fields
   into the meta comment unvalidated; `from=` defaulted to the trusted identity
   when absent. A forged `§` separator planted a permanently-trusted memory.
   Fixed by `_reject_inbox_injection` in `cli.py`, validating against the *real*
   parser regexes so it cannot drift. **`from_` was missed in the first fix and
   caught by audit 2** — I had reasoned about who *calls* it rather than what the
   *sink accepts*.
5. **Silent empty-index answerability.** `alexandria --corpus <missing> search`
   returned exit 0 with no output, because `VectorStore.__init__` creates an
   empty index. Fixed with `_require_index` at `_build_search_engine` — one
   chokepoint covering search, answer and eval.
6. **Separation metric read the wrong score** (see §4).
7. LanceDB null-schema inference crash; a live `AttributeError` on the inbox read
   path (operator-precedence bug at `connectors/inbox.py:79`); a cross-encoder
   segfault under torch/MPS.

---

## 2. What is PAPER (specified, not built)

`SPEC-data-model-and-ambient-capture.md` — 1,019 lines, two adversarial review
rounds, **review budget spent**.

- Phase 1 — typed observations, `entity_id`/`entity_rev`/`supersedes`, tombstones,
  `corpus.sqlite` as a **projection** (D4a), field-weighted FTS5
- Phase 2 — compaction, retention, observability
- then the predecessor migration (~14.5k documents)
- Phase 3 — ambient write (periodic sweep + shutdown hook, idle gate, cost bound)
- Phase 4 — ambient read (`serve` under launchd, optional, degradable)
- Phase 5 — erasure, **blocked on Q1**

`SPEC-versioning-and-supersession.md` is also unbuilt; absorbed as §3.2 of the above.

### The three requirements Phase 1 must not ship without

1. **`deleted` and `entity_id` must be indexed columns** (`SCALAR_FIELDS`,
   `METADATA_COLUMNS`). A frontmatter-only tombstone leaves body text unchanged →
   identical `chunk_id` → `store.upsert` overwrites in place → the document stays
   fully retrievable. Excluding it via a `corpus.sqlite` join puts a rebuildable
   index on the read path where a stale one **fails open and serves deleted
   content**.
2. **Revision documents must be path-disjoint.** `source_filename()` is
   deterministic on `(source, source_id, title)` and `Doc.write` is an
   unconditional `write_text` — so rev 2 silently destroys rev 1. `body_hash` is
   advertised in `corpus.py`'s docstring as "the immutability tripwire" but its
   only non-test caller is `migrate.py`: documented, not wired.
3. **`burst_id` must be derived from `(session path, first-message timestamp,
   window ordinal)`.** See §4 — demonstrated live, not theoretical.

---

## 3. The finding that drives everything

**Zero retrieval queries during the 13-hour session that built this system, by
its author, on the day he built it.** From `queries.sqlite`: of 442 queries on
2026-08-12, ~430 were the golden-set eval gate, 5 were `serve` smoke tests, and
the ~8 genuine ones were all from the *previous* session.

Four causes, all verifiable:
1. Latency — 50.2s / 36.6s / 33.5s / 28.6s measured that day, plus one `ETIMEDOUT`,
   against `rg` at ~200ms.
2. The corpus cannot answer questions about the session in progress.
3. The trigger is prose in a project block; `memory_search` gets called because it
   arrives as a **structured policy block**. Structure beats prose under pressure.
4. The substrate was faster — reading `~/.pi/agent/sessions/*.jsonl` and `git log`
   directly, the same data Alexandria indexes.

**Memory that depends on being invoked does not get invoked.** This is the
argument for ambient capture and it is empirical, not rhetorical.

---

## 4. Measurements worth not re-deriving

**Write path** (scratch corpus, `CASE-STUDY-the-write-path.md`)
- index 2 docs: 18.0s, of which ~16s is the embedding model load
- `remember`: sub-second (never loads the model)
- cold search via serve: **29.17s** → warm: **0.427s**
- `/remember` over HTTP promotes inline; retrievable in the same request cycle

**Ambient capture, on this session's real transcript** (`CASE-STUDY-ambient-capture.md`)
- 21,383 events → 12,379 after telemetry stripping
- 16 bursts, 15 substantive, **82 distillation calls, ~2,448,472 input tokens**
- `burst_id` instability demonstrated: `3a862d788848` → `4f7cf01aaf04` after one
  appended turn. At an hourly sweep, a 13-hour open session yields **13 permanent
  near-identical document sets** in a corpus with no delete.
- Real read-side query: **36.26s**, and every result was from old sessions —
  none of the last three days' work is in the corpus.

**Retrieval quality** (49 golden, 22 negative, 46,021 chunks)
- positive median 0.9785, **minimum 0.0274**; negative median 0.0238, max 0.4409
- floor 0.12 → 90.3% retained, 2 known-bad admitted
- floor 0.4409 → 83.9% retained, 1 admitted
- **All five positives below 0.4409 are `overlap_band: zero`.** No score floor
  separates the zero-overlap band from unanswerable queries — a floor is the
  wrong instrument, not a mistuned one.
- Band recall: literal 75.0%, partial 82.6%, **zero 33.3%** (weakest surface)
- `separation()` originally read `scores[0]` while `hit` means "target somewhere
  in top-k" — so a rank-3 hit was scored by a *wrong* document. 23 of 31 hits are
  at rank 1, so the median barely moved while the **minimum collapsed 0.1190 →
  0.0274**. The floor decision depends entirely on the minimum.

**Storage**
- LanceDB `chunks.lance` 2.6G, 74 fragments, 75 versions, `_indices/` **empty** —
  there is **no ANN index**; every query is exact brute-force KNN
- flat scan: 75.7ms @k=10, 118.4ms @k=50, 257.5ms @k=200
- ANN re-entry trigger: >250ms at working k, ≈150–200k chunks (3–4× current)
- Corpus: ~33,150 source docs, 46,021 chunks, generation 14+

---

## 5. Open work

| # | Item | Blocked on |
|---|---|---|
| **13** | second-host real-path canary — proven by `curl` from the second host, never wired into its actual skill | user approval; capital-bearing host |
| **18** | Opus review pass on the write-path **code** (rounds 1–2 covered the spec) | nothing |
| **20** | Golden set n=49 has no significance bar — small recall moves indistinguishable from noise | nothing; blocks BACKLOG #29 |
| **21** | Negatives decay as the corpus grows; this session's own distillation adds documents containing Kafka/MongoDB/Stripe | re-verify when golden set is reviewed |
| **22** | ≥10 **in-domain** negatives; 21 of 22 current ones are out-of-domain brand queries, so the negative set is easier than reality | nothing; keeps gate R3 PROVISIONAL |

**Q1 (erasure scope) is no longer treated as a blocker.** Decision taken:
tombstone-first, because invisible-is-a-prerequisite-for-destroyed, so it
forecloses nothing. The only thing genuinely needing user input, and only when it
becomes real: *does anyone other than the operator ever get access to a corpus
this system holds?* That converts right-to-erasure from optional to legal.

BACKLOG.md carries a verified "Status of the Top 10 after the write-path package"
table. Still open there: #5 enrichment injection framing, #6 deletion path,
#9 citation linkage, #10 procurement floor, and P1 #20 (golden set not in repo).

---

## 6. Operational knowledge (expensive to rediscover)

**Testing**
- `unset ALEXANDRIA_EMBED_PROVIDER && .venv/bin/python3 -m pytest tests/ -q`
- The bare `.venv/bin/pytest` binary fails with `ModuleNotFoundError: No module
  named 'tests'` — must use `-m pytest`.
- A leftover `ALEXANDRIA_EMBED_PROVIDER=hash` in the shell fails
  `test_mlx_is_the_default_embed_provider`. Not a regression.
- The production test suite exercises the **SQLite fallback** store, not LanceDB,
  by design (network-free). LanceDB *is* installed, so tests that need real
  LanceDB behaviour must construct it explicitly.

**Committing**
- Pre-commit runs a leak scan (`.leakpatterns.local`, 23 patterns) then an eval
  gate (60–90s). Use `timeout: 120`+ on the Bash call or it dies mid-run.
- **Always `git commit -F <tmpfile>`** for multi-paragraph messages. Heredoc
  inside `-m "$(cat <<'EOF'...)"` breaks on embedded backticks/parens.
- The leak scanner only sees **staged** files. `git add -N` scans zero.
- **A zero-width joiner (U+200D) defeats the scanner while still leaking the name
  to a human reader.** A ZWJ is not a redaction. Strip with
  `perl -i -CSD -pe 's/\x{200D}//g'`.
- Private names (hosts, agent identities) must not enter this repo. Use the
  private companion doc outside it.

**Embedding providers**
- Default is **MLX** (Apple-Silicon only). `ALEXANDRIA_EMBED_PROVIDER` ∈
  {`local`, `mlx`, `hash`}.
- The cache key includes the **model name**, so switching providers correctly
  invalidates every cached vector. An MLX-built index **cannot** be copied to a
  Linux host — it is a different vector space, requiring a full re-embed.
- Any retrieval-relevant commit needs the real corpus `manifest.json` to exist.
  It does now (`index --backfill-manifest`).

**Hard constraints**
- **Never run a corpus index build on the second host.** It carries live capital
  services and a 45k-chunk CPU embed took the host down on 2026-08-11.
- Never pass `--enrich`. Measured −6.1 pts recall (0.673 → 0.612); see
  `docs/DECISION-enrichment-2026-08-11.md`.
- `sync` alone makes nothing retrievable — you must then `index`.
- **There is no deletion path.** Anything written is permanent.

**Trust outcomes, not exit codes.** The recurring theme of this entire session —
the weekly loop, the enrichment scorer, three tests that passed against broken
code including one I wrote myself, and a job that reported "done" while never
writing its log. When a step claims success, confirm the observable result
changed.

---

## 7. Standing obligations

- **Remind Stanley to do an Opus review pass** after any Sonnet-executed goal
  completes (todo #18 — still owed on the write-path *code*).
- Two review rounds per artifact is the cap. Both are spent on the data-model
  spec; remaining risk is carried as named requirements and provisional gates
  (R3, H2), not further review.
