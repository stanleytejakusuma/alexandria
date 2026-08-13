"""Command line entry point.

argparse rather than a CLI framework: the surface is small, and stdlib means one
fewer dependency for a tool whose whole pitch is that your data outlives the engine.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import getpass
import json
import os
import re
import sys
import shutil
import tarfile
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path

from .auditlog import AuditLogger, audit_summary
from .cache import (
    QueryCache,
    ResponseCache,
    read_index_generation,
    write_index_generation,
)
from .config import AppConfig, load_config
from .corpus import Doc
from .connectors.inbox import INBOX_META_RE, InboxConnector, InboxEntry, parse_inbox_file
from .connectors.journal import JournalConnector
from .connectors.knowledge_graph import KnowledgeGraphConnector
from .connectors.md_memory import META_RE, SEPARATOR, MarkdownMemoryConnector
from .connectors.pi_sessions import PiSessionsConnector
from .eval.golden import load_golden, verify_targets
from .eval.negative import load_negative, run_negative, separation
from .eval.history import append_run, compare, load_runs, regressions
from .eval.metrics import by_overlap_band
from .eval.runner import EvalReport, run_eval
from .index.bm25 import BM25Index, searchable_text
from .index.chunker import chunk_doc_records, chunk_document, is_indexable_source
from .index.embedder import CachedEmbedder, HashEmbedder, LocalEmbedder, MLXEmbedder
from .index.manifest import (ManifestCorrupt, ManifestMismatch, ManifestMissing, verify_manifest,
                             verify_manifest_for_write, write_manifest)
from .index.store import VectorStore
from . import liveness
from .backup import backup_state, restore_state
from .llm import LLMClient
from .migrate import migrate_kg_sync
from .monitor import QueryLogger
from .pending import create_pending, list_pending, oldest_pending_age
from .promote import promote_pending
from .reconcile import reconcile_inbox
from .retrieval.rerank import CrossEncoderReranker
from .retrieval.search import SearchConfig, SearchEngine
from .schema import Severity, validate

def cmd_migrate(args) -> int:
    report = migrate_kg_sync(args.vault, args.corpus, dry_run=args.dry_run)
    print(report.render())
    if not report.reconciles:
        print("\nFAIL: counts do not reconcile", file=sys.stderr)
        return 1
    return 0


def cmd_lint(args) -> int:
    corpus = _config_for(args).corpus_path
    errors = checked = 0
    for path in sorted(corpus.rglob("*.md")):
        rel = path.relative_to(corpus)
        if "_unparsed" in rel.parts or ".alexandria" in rel.parts or "inbox" == rel.parts[0]:
            continue
        try:
            doc = Doc.read(path, root=corpus)
        except ValueError as exc:
            print(f"ERROR {rel}: {exc}")
            errors += 1
            continue
        checked += 1
        for issue in validate(doc.frontmatter, doc.path):
            if issue.severity is Severity.ERROR:
                errors += 1
                print(f"ERROR {doc.path}: {issue.code} {issue.field} -- {issue.message}")
    print(f"\nlint: {checked} documents, {errors} error(s)")
    return 1 if errors else 0


def _sync_connector(args):
    """Build the requested connector (no-LLM connectors skip the gateway)."""
    corpus = _config_for(args).corpus_path
    state_dir = corpus / ".alexandria" / "state"
    if args.connector == "pi-sessions":
        return PiSessionsConnector(
            sessions_dir=args.sessions_dir,
            state_dir=state_dir,
            llm=LLMClient(base_url=args.base_url, model=args.model),
        )
    if args.connector == "markdown-memory":
        return MarkdownMemoryConnector(
            memory_dir=args.memory_dir or str(Path.home() / ".pi/agent/memory"),
            projects_dir=args.projects_dir or None,
        )
    if args.connector == "knowledge-graph":
        return KnowledgeGraphConnector(vault_dir=args.vault_dir)
    if args.connector == "inbox":
        return InboxConnector(inbox_dir=corpus / "inbox")
    if args.connector == "journal":
        return JournalConnector(journal_path=args.journal_path)
    print(f"unknown connector: {args.connector}", file=sys.stderr)
    return None


def cmd_sync(args) -> int:

    conn = _sync_connector(args)
    if conn is None:
        return 2

    logger = AuditLogger(_config_for(args).corpus_path)
    corpus = _config_for(args).corpus_path
    items = conn.discover()
    if args.limit:
        items = items[: args.limit]
    skipped = getattr(conn, "skip_log", lambda: [])()
    print(f"discovered {len(items)} burst(s); {len(skipped)} skipped")
    if args.dry_run:
        for item in items[:20]:
            print(f"  {item.source_id}  {len(item.content):>7,}ch")
        return 0

    # Distillation is network-bound (pi-sessions), so a small pool turns hours
    # into minutes; no-LLM connectors are trivially fast under the same loop.
    # normalize() is pure (call + parse); writes and state stay on the main thread
    # because StateStore is not thread-safe and correctness beats a few more workers.
    # `empty` and `failed` are counted apart on purpose. A burst that yields no
    # note is the COMMON, CORRECT outcome -- most sessions contain nothing worth
    # keeping -- while a burst that raised is a real failure. Reporting them as
    # one number hid a 3% error rate inside an apparent 33% "failure" rate.
    written = done = empty = failed = 0
    total = len(items)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(conn.normalize, item): item for item in items}
        for future in as_completed(futures):
            item = futures[future]
            done += 1
            raised = False
            try:
                docs = future.result()
            except Exception as exc:                     # never lose the batch to one item
                conn.errors.append(f"{item.source_id}: {type(exc).__name__}: {exc}")
                docs = []
                raised = True
            for doc in docs:
                doc.write(corpus)
                written += 1
            # A connector may RECORD a failure instead of raising -- pi-sessions
            # catches its own LLM/JSON errors so one bad burst cannot kill the
            # batch. "No exception" therefore does not mean "succeeded", and
            # treating it that way would consume genuinely failed bursts and
            # destroy the retry. conn.errors holds only failures, so this scan is
            # proportional to the error count, not the item count.
            errored = raised or any(e.startswith(f"{item.source_id}:") for e in conn.errors)
            # Commit unless the item failed. Committing only when docs were
            # produced left every legitimately-empty burst permanently
            # unconsumed, so each weekly run re-distilled every session that had
            # nothing to say -- forever, at ~33% of the corpus.
            if not errored:
                conn.commit([item])
            if errored:
                failed += 1
            elif not docs:
                empty += 1
            if done % 10 == 0 or done == total:
                rate = done / max(time.time() - t0, 1e-6)
                eta = (total - done) / rate if rate else 0
                print(f"  {done}/{total}  notes={written}  empty={empty}  failed={failed}  "
                      f"{rate*60:.1f}/min  eta={eta/60:.1f}m", flush=True)

    print(f"wrote {written} note(s) from {total} burst(s); "
          f"{empty} had nothing durable, {failed} failed")
    for err in conn.errors[:10]:
        print(f"  error: {err}", file=sys.stderr)
    if len(conn.errors) > 10:
        print(f"  ... and {len(conn.errors)-10} more", file=sys.stderr)
    logger.sync(connector=conn.name, duration_ms=int((time.time() - t0) * 1000),
                discovered=total, normalized=total - failed,
                committed=written, skipped=len(skipped), errors=conn.errors[:20])
    return 0


@dataclass
class RememberResult:
    """Shared outcome type for both `cmd_remember` (CLI) and serve's
    /remember handler (§5), so the §7.1 write-ordering contract (marker
    written BEFORE success is reported) is implemented exactly once."""
    entry: InboxEntry | None
    status: str  # "written" | "duplicate" | "empty" | "marker_failed" | "invalid"
    path: Path | None = None
    error: str | None = None


# Inbox entries carry their structure in-band: entries are separated by a line
# containing SEPARATOR, and each one's identity lives in a trailing HTML comment
# that the parser finds with INBOX_META_RE.search() -- i.e. the FIRST match in
# the chunk, while the genuine comment is appended LAST. Unescaped, a payload
# could therefore (a) emit its own separator line and forge additional entries,
# and (b) emit its own metadata comment that outranks the real one, choosing its
# own `from=` -- and an omitted `from=` defaults to "pi", the trusted identity.
# Reached from BOTH the CLI and serve's unauthenticated /remember, and the
# corpus has no deletion path, so a forged entry is permanent.
#
# The guard rejects rather than escapes: escaping would change the on-disk
# format that already-written entries are parsed with. It uses the real parser
# regexes as its oracle so it cannot drift from what the parser will honour --
# an ordinary `<!-- TODO -->` stays legal, only a metadata-shaped comment is
# refused.
_META_FIELD_RE = re.compile(r"^[\w.-]+$")


def _reject_inbox_injection(text: str, *, from_: str | None, session: str | None,
                            corrects: str | None) -> str | None:
    """Return a reason string if this entry could forge inbox structure, else None.

    EVERY field that reaches the metadata comment is validated, including
    `from_`. An earlier revision exempted it on the reasoning that serve always
    computes it from the socket -- true of serve, but `--from` is a plain CLI
    flag, and a guard that holds only while every caller behaves is not a
    guard. Validate at the sink; trust no caller's discipline.
    """
    if f"\n{SEPARATOR}\n" in f"\n{text}\n":
        return (f"text contains a line consisting solely of {SEPARATOR!r}, which is the "
                f"inbox entry separator -- it would be read back as multiple entries")
    if INBOX_META_RE.search(text) or META_RE.search(text):
        return ("text contains an inbox metadata comment (<!-- created=..., last=... -->), "
                "which would override this entry's own recorded identity")
    for name, value in (("from", from_), ("session", session), ("corrects", corrects)):
        if value and not _META_FIELD_RE.match(value):
            return (f"{name}={value!r} contains characters that are not permitted in a "
                    f"metadata field (allowed: letters, digits, '.', '-', '_')")
    return None


def append_inbox_entry(corpus: Path, text: str, *, from_: str | None = None,
                       session: str | None = None, corrects: str | None = None) -> RememberResult:
    """Append a user-confirmed memory to the inbox (the only explicit write
    surface; promoted by the drain or inline by serve's /remember)."""
    text = text.strip()
    if not text:
        return RememberResult(None, "empty")
    injection = _reject_inbox_injection(text, from_=from_, session=session, corrects=corrects)
    if injection:
        return RememberResult(None, "invalid", error=injection)
    inbox_dir = corpus / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    today = _dt.date.today().isoformat()
    path = inbox_dir / f"{today}.md"

    existing = parse_inbox_file(path) if path.exists() else []
    if any(e.text == text for e in existing):
        return RememberResult(None, "duplicate", path=path)

    meta = f"created={today}, last={today}"
    if from_:
        meta += f", from={from_}"
    if session:
        meta += f", session={session}"
    if corrects:
        meta += f", corrects={corrects}"
    entry_obj = InboxEntry(text, today, today, from_ or "pi", session or "", corrects or "")
    entry = f"{text}\n\n<!-- {meta} -->"
    # One atomic O_APPEND write, not a size check plus two writes. serve is a
    # ThreadingHTTPServer and this append is not under the write lock (taking
    # it would fail every remember that races a drain, breaking W1's <500ms),
    # so an interleave here permanently fuses two facts into one entry in a
    # corpus with no deletion path. The separator is written unconditionally --
    # deciding on file size is the race -- and the empty leading chunk that
    # produces on a fresh file is dropped by _parse_inbox_text.
    payload = f"\n{SEPARATOR}\n{entry}\n".encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        written = os.write(fd, payload)
    finally:
        os.close(fd)
    if written != len(payload):
        # A short write leaves a torn entry that a concurrent append can
        # interleave into; say so rather than reporting success.
        return RememberResult(None, "marker_failed", path=path,
                              error=f"short write to {path}: {written}/{len(payload)} bytes")
    # SPEC §7.1 mitigation 1: the pending marker is written BEFORE reporting
    # success. An entry that reaches the inbox but not the pending directory
    # must surface as a failure to the caller, not a silent success -- the
    # marker's absence is otherwise indistinguishable from "nothing to do".
    try:
        create_pending(corpus, entry_obj.entry_id)
    except OSError as exc:
        return RememberResult(entry_obj, "marker_failed", path=path, error=str(exc))
    return RememberResult(entry_obj, "written", path=path)


def cmd_remember(args) -> int:
    corpus = _config_for(args).corpus_path
    result = append_inbox_entry(corpus, args.text, from_=args.from_, session=args.session,
                                corrects=args.corrects)
    if result.status == "empty":
        print("remember: empty text", file=sys.stderr)
        return 2
    if result.status == "invalid":
        print(f"remember: refused -- {result.error}", file=sys.stderr)
        return 2
    if result.status == "duplicate":
        print("already in inbox; nothing appended")
        return 0
    if result.status == "marker_failed":
        print(f"remember: wrote inbox/{result.path.name} but failed to mark it "
              f"pending ({result.error}) -- it will NOT be auto-promoted; run "
              f"`alexandria reconcile` to recover it", file=sys.stderr)
        return 1
    print(f"remembered -> inbox/{result.path.name} (pending {result.entry.entry_id})")
    return 0


def cmd_promote(args) -> int:
    """The drain (§4): promote every currently-pending `remember` entry through
    the crash-safe write-ordered pipeline in promote.py. Serve's /remember
    handler calls promote_pending() directly (inline, scoped to one entry);
    this subcommand is the offline fallback -- run on a timer, or by hand."""
    config = _config_for(args)
    corpus = config.corpus_path
    embedder = _cached_embedder(config, corpus)
    store = VectorStore(corpus / ".alexandria" / "index")
    lexical = BM25Index(corpus / ".alexandria" / "index" / "fts.sqlite")
    result = promote_pending(corpus, config, embedder, store, lexical)
    if result.skipped_locked:
        print("promote: write lock held by another process, skipped this run")
        return 0
    # Telemetry (§7): a completed cycle counts even with nothing to promote --
    # that is still evidence the drain ran, which is what liveness.json proves.
    liveness.record_success(corpus, promoted_count=len(result.promoted),
                            generation=read_index_generation(corpus))
    if not result.promoted and not result.errors:
        print("promote: nothing pending")
        return 0
    for error in result.errors:
        print(f"promote: {error}", file=sys.stderr)
    if result.promoted:
        plural = "y" if len(result.promoted) == 1 else "ies"
        print(f"promote: {len(result.promoted)} entr{plural} promoted, "
              f"{result.chunks_written} chunks written")
    return 1 if result.errors else 0


def cmd_serve(args) -> int:
    """§5: run the warm HTTP server. Blocks until interrupted; the CLI itself
    stays fully functional whether this is running or not (S6)."""
    from .serve import NonLoopbackRefused, serve as serve_forever

    config = _config_for(args)
    unix_sockets: dict[str, str] = {}
    for spec in args.unix_socket:
        if "=" not in spec:
            print(f"serve: --unix-socket expects IDENTITY=PATH, got {spec!r}", file=sys.stderr)
            return 2
        identity, sock_path = spec.split("=", 1)
        unix_sockets[identity] = sock_path
    try:
        serve_forever(config.corpus_path, config=config, host=args.host, port=args.port,
                      unix_sockets=unix_sockets)
    except NonLoopbackRefused as exc:
        print(f"serve: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        pass
    return 0


def cmd_reconcile(args) -> int:
    """§7.1's independent observer: every inbox/*.md entry must have a
    matching document under sources/inbox/. An unpromoted entry with no
    pending marker is genuinely stranded (nothing is tracking it) and gets
    requeued; an unpromoted entry that IS correctly marked pending is normal
    backlog, not a fault."""
    corpus = _config_for(args).corpus_path
    report = reconcile_inbox(corpus)
    print(f"reconcile: {report.total_entries} entries in {report.total_files} files, "
          f"{len(report.stranded)} stranded, {len(report.already_pending)} pending, "
          f"{len(report.unreadable_files)} unreadable")
    for name in report.unreadable_files:
        print(f"reconcile: unreadable inbox file {name}", file=sys.stderr)
    for entry_id in report.stranded:
        print(f"reconcile: stranded entry {entry_id} (requeued)", file=sys.stderr)
    return 0 if report.healthy else 1


def cmd_backup(args) -> int:
    """§6: back up `.alexandria` STATE only -- never the rebuildable indexes
    (chunks.lance, fts.sqlite). See backup.STATE_PATHS for the exact list."""
    corpus = _config_for(args).corpus_path
    result = backup_state(corpus, Path(args.dest))
    print(f"backup: wrote {result.archive_path} ({len(result.included)} paths)")
    for rel in result.included:
        print(f"backup: included {rel}")
    for rel in result.missing:
        print(f"backup: skipped {rel} (not present)", file=sys.stderr)
    return 0


def cmd_restore(args) -> int:
    """§6/B1: restore `.alexandria` STATE from a backup_state() archive.
    Overwrites in place unless --dry-run. Refuses to touch anything outside
    the fixed STATE_PATHS allowlist regardless of what the archive contains."""
    corpus = _config_for(args).corpus_path
    try:
        result = restore_state(corpus, Path(args.archive), dry_run=args.dry_run)
    except (tarfile.TarError, OSError) as exc:
        # A hostile or corrupt archive must be a diagnosable refusal, not a
        # traceback -- tarfile's data filter raises for symlink/hardlink/
        # absolute-path escapes, and those reach here as-is otherwise.
        print(f"restore: refused {args.archive}: {exc}", file=sys.stderr)
        return 1
    verb = "would restore" if args.dry_run else "restored"
    print(f"restore: {verb} {len(result.restored)} paths from {args.archive}")
    for name in result.restored:
        print(f"restore: {verb} {name}")
    return 0


def cmd_index(args) -> int:
    """Chunk, embed, and persist the corpus in deterministic batches."""
    config = _config_for(args)
    corpus = config.corpus_path
    if getattr(args, "backfill_manifest", False):
        # SPEC F4 one-time backfill: an index built before manifests existed
        # has no way to (re)derive which model produced its vectors from the
        # vectors themselves, so this trusts the operator's assertion that
        # --embed-provider matches what was actually used, without paying to
        # re-embed the whole corpus.
        embedder = _cached_embedder(config, corpus)
        manifest = write_manifest(corpus, embedder, config.embed_provider)
        print(f"index: manifest backfilled -> provider={manifest['provider']} "
              f"model={manifest['model']} dim={manifest['dim']} "
              f"normalized={manifest['normalized']} dtype={manifest['dtype']}")
        return 0
    records, errors = _load_chunk_records(corpus, config, args.limit, args.workers)
    for error in errors:
        print(f"skip: {error}", file=sys.stderr)
    if args.enrich:
        from .enrich import EnrichmentStore, enrich_docs_for_index, recipe_signature
        store = EnrichmentStore(corpus / ".alexandria" / "index")
        if args.reattach_only:
            # Rebuilding the index over an already-enriched corpus is pure replay
            # from EnrichmentStore -- requiring a live gateway for it turns every
            # reindex into a network dependency for work that needs no network.
            # Any document that genuinely still needs a call is counted as failed
            # and stays retryable, exactly as a gateway error would leave it.
            class _NoLLM:
                def complete(self, *args, **kwargs):
                    raise RuntimeError(
                        "--reattach-only: document is not in the enrichment store "
                        "and no LLM gateway is configured")

            llm = _NoLLM()
        else:
            from .llm import LLMClient
            llm = LLMClient(model=args.enrich_model, base_url=args.base_url,
                            api_key_env=args.api_key_env)
            # Preflight: one tiny call so a degraded gateway fails the run in
            # seconds, not in a 25k-doc retry cascade (observed live 2026-08-10:
            # gateway queue stall -> every enrichment call timed out -> the whole
            # run churned ~2h then finished as 100% failed).
            try:
                llm.complete("Reply with the single word: ok", "preflight",
                             temperature=0.1)
            except Exception as exc:
                print(f"enrich: preflight failed ({exc}); aborting "
                      f"--enrich run", file=sys.stderr)
                return 3
        recipe = recipe_signature(args.enrich_model, args.enrich_prompt_version)
        estats = enrich_docs_for_index(
            records, llm=llm, embedder=_cached_embedder(config, corpus),
            store=store, recipe=recipe, limit=args.enrich_limit,
            workers=args.enrich_workers, progress_every=100)
        print(f"enrich: {estats}")
    store = VectorStore(corpus / ".alexandria" / "index")
    lexical = BM25Index(corpus / ".alexandria" / "index" / "fts.sqlite")
    if args.rebuild:
        # SPEC C6: --rebuild drops the table, so from here until the pipeline
        # finishes the index is partial. The marker makes that state visible to
        # anything that measures the index; it is deliberately NOT removed on
        # failure, because a crashed rebuild leaves exactly the partial index
        # the marker is warning about.
        marker = _rebuild_marker(corpus)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            f"rebuild of {len(records)} chunks started "
            f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} (pid {os.getpid()})\n")
        store.drop()
        lexical.drop()
        # store.append() cannot deduplicate the way merge_insert does, so the
        # uniqueness it assumes is verified once here rather than trusted. Checking
        # per batch would miss a collision that spans two batches.
        ids = [record["chunk_id"] for record in records]
        if len(ids) != len(set(ids)):
            from collections import Counter
            dupes = [cid for cid, n in Counter(ids).most_common(5) if n > 1]
            raise ValueError(
                "rebuild set contains duplicate chunk_id(s); append would insert "
                f"every copy: {dupes}")
    embedder = _cached_embedder(config, corpus)
    # F4 on the write path: refuse BEFORE embedding, or this run writes foreign
    # vectors into the existing column and then rewrites the manifest to match,
    # after which the read-path guard passes forever over a mixed vector space.
    try:
        verify_manifest_for_write(corpus, embedder, config.embed_provider, store)
    except (ManifestMissing, ManifestMismatch, ManifestCorrupt) as exc:
        raise SystemExit(f"alexandria: {exc}") from exc
    started = time.monotonic()
    stats = _run_index_pipeline(records, embedder, store, lexical,
                                batch_size=config.embed_batch_size,
                                progress_every=config.index_progress_every,
                                progress_stream=sys.stdout,
                                write_batch=config.index_write_batch,
                                append_only=args.rebuild)
    elapsed = time.monotonic() - started
    print(f"index: {len(records)} chunks from {len({record['doc_id'] for record in records})} documents "
          f"in {elapsed:.2f}s (cache {stats.cache_hits} hit/"
          f"{stats.cache_misses} miss)")
    # Red release change 1: bind every cache to this corpus generation so a
    # reindex invalidates stale query/response cache entries.
    gen = write_index_generation(corpus)
    print(f"index: corpus generation {gen} (query/response caches invalidated)")
    write_manifest(corpus, embedder, config.embed_provider)
    liveness.record_success(corpus, promoted_count=0, generation=gen)
    if args.rebuild:
        _rebuild_marker(corpus).unlink(missing_ok=True)
    return 0


@dataclass(frozen=True)
class IndexStats:
    indexed: int = 0
    cache_hits: int = 0
    cache_misses: int = 0


def _run_index_pipeline(records: list[dict], embedder, store, lexical, *, batch_size: int,
                        progress_every: int, progress_stream,
                        write_batch: int = 0, append_only: bool = False) -> IndexStats:
    """Overlap embedding (GPU-bound) with store writes (I/O-bound).

    The naive loop -- embed batch, write batch, embed next batch -- leaves the GPU
    idle for the full duration of every LanceDB upsert and FTS5 index write. Measured
    on the real corpus this was the dominant cost, not the model itself. Two threads:
    one calls the embedder (the only thing touching the shared MPS device, so there is
    never GPU contention), the other persists finished batches. A bounded queue
    (maxsize=2) caps how far the embedder can run ahead, so this does not trade I/O
    wait for unbounded memory growth -- the exact failure mode a memory-constrained
    machine cannot afford.

    `last_cache_stats` is a mutable attribute on the embedder that the NEXT embed()
    call overwrites. It is snapshotted immediately after each call, on the producer
    side, and carried through the queue -- reading it later on the writer thread would
    silently attribute one batch's cache stats to another under real overlap.
    """
    if not records:
        return IndexStats()

    import queue as _queue

    work: _queue.Queue = _queue.Queue(maxsize=2)
    SENTINEL = object()
    failure: list[BaseException] = []

    def produce() -> None:
        try:
            for start in range(0, len(records), batch_size):
                batch = records[start:start + batch_size]
                # Enrichment synthetic records carry a precomputed vector
                # (query-space embeddings, see enrich.py). Partition the
                # batch so only the rest goes through the embedder; order
                # is preserved on reassembly. Dimension/finiteness of
                # precomputed vectors is validated below.
                needs_embed = [r for r in batch if "vector" not in r]
                pre_vec = [r for r in batch if "vector" in r]
                if needs_embed:
                    vectors = embedder.embed(
                        [searchable_text(record) for record in needs_embed])
                    # snapshot NOW, not later
                    cache_stats = dict(embedder.last_cache_stats)
                else:
                    vectors = []
                    # last_cache_stats is only refreshed BY an embed() call, so a
                    # batch of entirely precomputed vectors would otherwise re-report
                    # the previous batch's numbers and count them twice. Observed
                    # 2026-08-11: a 124,751-chunk rebuild reported 89,902 hits/0
                    # misses when only 38,963 chunks were ever embedded.
                    cache_stats = {"hits": 0, "misses": 0}
                dim = getattr(embedder, "dim", None)
                indexed_records: list[dict] = []
                it = iter(vectors)
                for record in batch:
                    if "vector" in record:
                        vector = record["vector"]
                        if dim is not None and len(vector) != dim:
                            raise ValueError(
                                f"precomputed vector for {record['chunk_id']} has "
                                f"dim {len(vector)}, embedder dim {dim}")
                        if any(not isinstance(v, (int, float))
                               or v != v for v in vector):
                            raise ValueError(
                                f"precomputed vector for {record['chunk_id']} "
                                f"contains NaN/non-numeric values")
                    else:
                        vector = next(it)
                    indexed_records.append(record | {"vector": vector})
                work.put((indexed_records, cache_stats))
        except BaseException as exc:  # noqa: BLE001 -- must reach the main thread
            failure.append(exc)
        finally:
            work.put(SENTINEL)

    producer = threading.Thread(target=produce, daemon=True)
    producer.start()

    indexed = cache_hits = cache_misses = 0
    next_report = progress_every
    started = time.monotonic()

    # Commit granularity is NOT embed granularity. Each store write is one LanceDB
    # commit whose manifest lists every prior fragment, so committing per embed
    # batch (32 rows) makes a rebuild O(n^2). Buffer to write_batch rows first.
    # write_batch=0 preserves the old commit-per-batch behaviour for callers that
    # do not set it.
    write = store.append if append_only else store.upsert
    pending: list[dict] = []

    def flush() -> None:
        if pending:
            write(pending)
            lexical.index(pending, append_only=append_only)
            pending.clear()

    while True:
        item = work.get()
        if item is SENTINEL:
            break
        indexed_records, cache_stats = item
        pending.extend(indexed_records)
        if len(pending) >= write_batch:        # I/O overlaps the NEXT batch's embed()
            flush()
        indexed += len(indexed_records)
        cache_hits += cache_stats["hits"]
        cache_misses += cache_stats["misses"]
        if progress_stream is not None and (indexed >= next_report or indexed == len(records)):
            rate = indexed / max(time.monotonic() - started, 1e-9)
            eta = (len(records) - indexed) / rate if rate else 0.0
            print(f"index: {indexed}/{len(records)} chunks  {rate * 60:.1f}/min  eta={eta / 60:.1f}m",
                  file=progress_stream, flush=True)
            while next_report <= indexed:
                next_report += progress_every

    producer.join()
    if failure:
        raise failure[0]
    flush()                                    # never leave a partial buffer unwritten
    return IndexStats(indexed=indexed, cache_hits=cache_hits, cache_misses=cache_misses)


def cmd_search(args) -> int:
    config = _config_for(args)
    corpus = config.corpus_path
    engine = _build_search_engine(config, corpus, client="search")
    filters = {field: value for field, value in {
        "type": args.type, "project": args.project, "layer": args.layer,
    }.items() if value is not None}
    _t0 = time.time()
    results = engine.search(args.query, k=args.k, filters=filters)
    from .auditlog import AuditLogger
    logger = AuditLogger(corpus)
    logger.search(query=args.query, k=args.k,
                  latency_ms=int((time.time() - _t0) * 1000),
                  hits=len(results), caller=args.caller, user=cli_identity(),
                  cache_hit=engine.last_cache_hit)
    for result in results:
        print(f"{result.rank}. {result.chunk_id}  score={result.score:.6f}\n"
              f"   {result.heading_path}\n   {result.text[:400].replace(chr(10), ' ')}")
    if args.trace:
        print(json.dumps(engine.last_trace, indent=2, sort_keys=True))
    return 0


@dataclass
class AnswerOutcome:
    """Shared result type for both `cmd_answer` (CLI, prints it) and serve's
    /answer handler (§5, JSON-encodes it) -- the response cache, cost ledger,
    and audit-trail logic live in `run_answer` exactly once so the two paths
    cannot silently diverge."""
    emitted: bool
    text: str | None
    n_claims: int
    answer_id: str
    cached: bool = False
    error: str | None = None
    failed_claims: list = None  # type: ignore[assignment]


def run_answer(config: AppConfig, corpus: Path, question: str, *, engine, k: int,
              llm_model: str, grader_a_model: str, grader_b_model: str,
              base_url: str | None, api_key_env: str | None, prompt_version: str,
              save_dir: str | None = None, caller: str | None = None,
              user: str | None = None) -> AnswerOutcome:
    """Run the full gather -> write -> judge -> repair pipeline (or replay a
    cached page) for one question against an already-built `engine`. No
    printing; side effects are exactly what the spec requires (response
    cache, audit log, cost ledger) and nothing else."""
    from .cache import ResponseCache
    from .llm import LLMClient
    from .synthesis.pipeline import run_pipeline

    answer_id = str(uuid.uuid4())
    response_cache = ResponseCache(corpus)
    generation = read_index_generation(corpus)
    rkey = response_cache.key(question, llm_model, k, prompt_version, generation)

    # RESPONSE CACHE: a previously-emitted answer for the same question/model
    # config is replayed verbatim (TTL 7d); the pipeline is skipped entirely.
    # No LLM call happens on this path, so no ledger row is written -- there is
    # nothing to cost (SPEC F5).
    cached_page = response_cache.get(rkey)
    if cached_page is not None:
        logger = AuditLogger(corpus)
        logger.answer(query=question, total_ms=0, emitted=True,
                      model=llm_model, n_claims=cached_page.get("n_claims", 0),
                      stages={}, caller=caller, user=user,
                      trace={"cache_hit": True}, id=answer_id)
        return AnswerOutcome(True, cached_page["text"], cached_page.get("n_claims", 0),
                             answer_id, cached=True)

    save_path = Path(save_dir).expanduser() if save_dir else None
    emit_root = save_path if save_path else Path(tempfile.mkdtemp(prefix="alexandria-answer-"))

    writer = LLMClient(model=llm_model, base_url=base_url, api_key_env=api_key_env)
    grader_a = LLMClient(model=grader_a_model, base_url=base_url, api_key_env=api_key_env)
    grader_b = LLMClient(model=grader_b_model, base_url=base_url, api_key_env=api_key_env)
    _t_answer0 = time.time()
    result = run_pipeline(
        engine, question,
        gather_llm=writer, writer_llm=writer, repair_llm=writer,
        audit_llm=grader_a, coverage_llm_a=grader_a, coverage_llm_b=grader_b,
        corpus_root=emit_root, seed_k=k, writer_model=llm_model,
        prompt_version=prompt_version,
    )
    total_ms = int((time.time() - _t_answer0) * 1000)
    logger = AuditLogger(corpus)
    verdict = getattr(result.repair, "verdict", None)
    failed_ids = list(getattr(verdict, "failed_claim_ids", ()) or ())
    page = getattr(result.repair, "page", None)
    n_claims = len(page.claims) if page else 0
    trace = _answer_trace(result)
    # COST LEDGER (SPEC F5): the writer client is the primary answer-generation
    # model and the one cost worth tracking without inventing multi-model
    # attribution the pipeline doesn't expose. Logged on BOTH outcomes below --
    # a failed synthesis still spent real tokens.
    if writer.last_usage:
        engine.logger.log_usage(query_id=answer_id, model=llm_model, **writer.last_usage)
    if not result.emitted:
        logger.answer(query=question, total_ms=total_ms, emitted=False,
                      model=llm_model, n_claims=n_claims, failed_claims=failed_ids,
                      error="synthesis failed its native checks",
                      stages=getattr(result, "timings_ms", {}),
                      caller=caller, user=user, trace=trace, id=answer_id)
        return AnswerOutcome(False, None, n_claims, answer_id,
                             error="synthesis failed its native checks", failed_claims=failed_ids)
    page_text = result.page_path.read_text(encoding="utf-8")
    logger.answer(query=question, total_ms=total_ms, emitted=True,
                  model=llm_model, n_claims=n_claims,
                  stages=getattr(result, "timings_ms", {}),
                  caller=caller, user=user, trace=trace, id=answer_id)
    response_cache.put(rkey, {"text": page_text, "n_claims": n_claims})
    if not save_path:
        shutil.rmtree(emit_root, ignore_errors=True)
    return AnswerOutcome(True, page_text, n_claims, answer_id)


def cmd_answer(args) -> int:
    """Synthesize a cited answer page for a question (phase-4 answer endpoint).

    Runs the full gather -> write -> judge -> repair pipeline over the indexed
    corpus and prints the emitted page. The page is written to --save-dir (or a
    temp dir) -- never into the private corpus wiki implicitly.
    """
    config = _config_for(args)
    corpus = config.corpus_path
    engine = _build_search_engine(config, corpus, corpus_root=corpus, client="answer")
    outcome = run_answer(
        config, corpus, args.question, engine=engine, k=args.k,
        llm_model=args.llm_model, grader_a_model=args.grader_a_model,
        grader_b_model=args.grader_b_model, base_url=args.base_url,
        api_key_env=args.api_key_env, prompt_version=args.prompt_version,
        save_dir=args.save_dir, caller=args.caller, user=cli_identity())
    if outcome.cached:
        print("[cached] " + outcome.text)
        return 0
    if not outcome.emitted:
        print("answer: synthesis failed its native checks; no page emitted.",
              file=sys.stderr)
        for claim_id in outcome.failed_claims or ():
            print(f"  failed claim {claim_id}", file=sys.stderr)
        return 1
    print(outcome.text)
    return 0


def _answer_trace(result) -> dict:
    """The route one answer took: retrieval rounds -> augmentation pool ->
    synthesis outcome. Stored in the audit row so the route can be mapped."""
    gathered = getattr(result, "gathered", None)
    repair = getattr(result, "repair", None)
    trace: dict = {"rounds": [], "pool": [], "cited": [], "claims": 0,
                   "iterations": 0}
    if gathered is not None:
        for name in ("round_one", "round_two"):
            chunks = getattr(gathered, name, ()) or ()
            trace["rounds"].append(
                [[c.chunk_id, round(c.score, 4)] for c in chunks[:8]])
        trace["pool"] = [c.doc_id for c in (gathered.chunks or ())][:16]
        trace["follow_ups"] = list(getattr(gathered, "follow_up_queries", ()) or ())[:4]
    if repair is not None:
        rpage = getattr(repair, "page", None)
        if rpage is not None:
            cited: list[str] = []
            for claim in rpage.claims:
                for citation in getattr(claim, "citations", ()) or ():
                    if citation.doc_id not in cited:
                        cited.append(citation.doc_id)
            trace["cited"] = cited[:16]
            trace["claims"] = len(rpage.claims)
            trace["iterations"] = getattr(repair, "iterations", 0)
    return trace


def cmd_wiki_site(args) -> int:
    """Render a wiki dir (the shape run_pipeline emits) into a static site."""
    from .wiki_site import render_site

    config = _config_for(args)
    corpus = config.corpus_path
    wiki = Path(args.wiki).expanduser() if args.wiki else corpus / "wiki"
    if not wiki.is_dir():
        print(f"wiki-site: no wiki dir at {wiki}", file=sys.stderr)
        return 2
    slugs = render_site(wiki, args.out,
                        audit_dir=config.corpus_path / ".alexandria" / "audit")
    print(f"wiki-site: {len(slugs)} page(s) rendered to {args.out}")
    return 0


def _rebuild_marker(corpus: Path) -> Path:
    return corpus / ".alexandria" / "index" / ".rebuild-in-progress"


def cmd_eval(args) -> int:
    """Measure current retrieval against the private golden set without changing it."""
    if args.k is not None and args.k < 0:
        print("eval: --k must be non-negative", file=sys.stderr)
        return 2
    config = _config_for(args)
    corpus = config.corpus_path
    # SPEC C6: a measurement must assert its preconditions. An eval run against a
    # half-written index measures a state that never coherently existed, and
    # append_run() then makes it previous[-1] -- corrupting not just that run's
    # verdict but every future gate comparison. Observed 2026-08-11: a killed
    # rebuild left the table at 90,304 of 124,751 chunks; the eval reported a
    # -4.1% recall "regression" that was pure artifact and had to be quarantined.
    marker = _rebuild_marker(corpus)
    if marker.exists() and not getattr(args, "allow_partial_index", False):
        print(f"eval: an index rebuild is in progress or was interrupted "
              f"({marker}); refusing to measure a partial index. Finish the "
              f"rebuild, or pass --allow-partial-index to override.", file=sys.stderr)
        return 2
    golden_path = (Path(args.golden).expanduser() if args.golden else
                   corpus / ".alexandria" / "golden" / "golden-v1.jsonl")
    try:
        entries = load_golden(golden_path)
    except ValueError as exc:
        _print_eval_error(str(exc), as_json=args.json)
        return 2
    target_errors = verify_targets(entries, corpus)
    if target_errors:
        if args.json:
            print(json.dumps({"target_errors": target_errors}, ensure_ascii=False))
        else:
            print("UNUSABLE golden set: missing target document(s): "
                  + ", ".join(target_errors), file=sys.stderr)
        return 2

    engine = _build_search_engine(config, corpus)
    report = run_eval(engine, entries, k_override=args.k)

    # Negative cases (BACKLOG #21): queries the corpus cannot answer. Without
    # them the gate is recall-only and an engine that grows confidently wrong
    # scores exactly as well as one that stays right. Run when the set exists so
    # a corpus without one still evaluates, rather than failing on absence.
    negative_path = (Path(args.negative).expanduser() if args.negative else
                     corpus / ".alexandria" / "golden" / "negative-v1.jsonl")
    if negative_path.exists():
        try:
            negative_entries = load_negative(negative_path)
        except ValueError as exc:
            _print_eval_error(str(exc), as_json=args.json)
            return 2
        negative_rows = run_negative(engine, negative_entries, k=args.k or 5)
        try:
            separation_report = separation(report.results, negative_rows).to_dict()
        except ValueError:
            # Nothing scored on one side; record the rows, claim no separation.
            separation_report = None
        report = replace(report, negatives=negative_rows, separation=separation_report)

    history_path = corpus / ".alexandria" / "eval_runs.jsonl"
    previous = load_runs(history_path)
    delta = compare(previous[-1], report) if previous and (args.compare_last or args.fail_on_regression) else None
    append_run(history_path, report)

    if args.json:
        payload = report.to_dict()
        if delta is not None:
            payload["comparison"] = delta.to_dict()
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        _print_eval_report(report, delta)

    if report.summary.errors:
        return 1
    return 1 if args.fail_on_regression and delta is not None and regressions(delta) else 0


def cmd_cache(args) -> int:
    """Cache stats + maintenance (query cache, response cache, embedding cache)."""
    from .cache import QueryCache, ResponseCache, read_index_generation
    config = _config_for(args)
    corpus = config.corpus_path
    embed_path = corpus / ".alexandria" / "cache" / "embeddings.sqlite"
    if not args.clear:
        for name, cache in (("query", QueryCache(corpus)),
                            ("response", ResponseCache(corpus))):
            st = cache.stats()
            print(f"{name} cache: {st.size} row(s), ttl={cache.ttl // 3600}h")
            print(f"  errors: {len(cache.errors)}")
        if embed_path.exists():
            import sqlite3
            con = sqlite3.connect(embed_path)
            rows = con.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
            con.close()
            print(f"embedding cache: {rows} row(s)")
        else:
            print("embedding cache: not built yet")
        return 0
    total = QueryCache(corpus).clear() + ResponseCache(corpus).clear()
    print(f"cache --clear: removed {total} row(s) (embedding cache kept; "
          "it is content-hash keyed and self-invalidating)")
    return 0


def cli_identity() -> str:
    """The identity recorded for a CLI invocation. Derived, never accepted.

    BACKLOG #8: the old `--user` flag (default `ALEXANDRIA_USER` or "local") was
    written verbatim into the audit trail, so any caller could name themselves
    anyone. That is worse than recording nothing -- an absent field is obviously
    absent, while a forged one is a plausible-looking audit trail that reads as
    evidence. Nothing consumed the flag (not serve, not the extension, not any
    script), so it was removed rather than validated.

    The OS user is the honest answer on this path: a caller can only "forge" it
    by actually being that user, at which point it is true. This is the same
    claim serve makes in §5.3 -- identity equals filesystem access -- reached by
    the local equivalent of serve's socket ownership.

    `--caller` survives because it labels the invoking *tool*, not a person, and
    its help text now says it is unverified.
    """
    try:
        return getpass.getuser()
    except Exception:  # no passwd entry (some containers); never fail a query
        return "unknown"


def _config_for(args) -> AppConfig:
    return load_config(corpus_override=getattr(args, "corpus", None))


def _require_index(corpus: Path) -> None:
    """Refuse to search a corpus that was never indexed.

    ``VectorStore.__init__`` does ``mkdir(parents=True, exist_ok=True)``, so
    building an engine over a missing corpus CREATES an empty index and returns
    exit 0 with zero hits -- indistinguishable from "this knowledge does not
    exist". A wrong ``--corpus`` path or an unprovisioned host therefore becomes
    a confident false negative rather than a loud failure. Checked here because
    this is the single chokepoint for search, answer, and eval.
    """
    index_dir = corpus / ".alexandria" / "index"
    if (index_dir / "chunks.lance").exists() or (index_dir / "fallback.sqlite").exists():
        return
    raise SystemExit(
        f"alexandria: no index at {index_dir} -- the corpus is missing or was "
        f"never indexed, so every query would return zero results. "
        f"Run: alexandria --corpus {corpus} index"
    )


def _build_search_engine(config: AppConfig, corpus: Path, query_cache: bool = True,
                         corpus_root: Path | None = None, client: str = "cli") -> SearchEngine:
    _require_index(corpus)
    # §7: every invocation performs the liveness check and prints one line to
    # stderr if stale -- never raises, never blocks results (gate W7).
    live = liveness.check(corpus)
    if live.stale:
        print(f"alexandria: stale -- {live.reason}", file=sys.stderr)
    embedder = _cached_embedder(config, corpus)
    try:
        verify_manifest(corpus, embedder, config.embed_provider)
    except (ManifestMissing, ManifestMismatch, ManifestCorrupt) as exc:
        raise SystemExit(f"alexandria: {exc}") from exc
    return SearchEngine(
        embedder,
        VectorStore(corpus / ".alexandria" / "index"),
        BM25Index(corpus / ".alexandria" / "index" / "fts.sqlite"),
        CrossEncoderReranker(config.rerank_model),
        SearchConfig(prefetch=config.rerank_prefetch, top_k=config.rerank_top_k,
                     wiki_boost=config.wiki_boost, rrf_k=config.rrf_k),
        QueryLogger(corpus / ".alexandria" / "queries.sqlite"),
        query_cache=QueryCache(corpus) if query_cache else None,
        corpus_root=corpus_root or corpus,
        client=client,
    )


def _cached_embedder(config: AppConfig, corpus: Path) -> CachedEmbedder:
    if config.embed_provider == "hash":
        provider = HashEmbedder()
    elif config.embed_provider == "mlx":
        # Measured on this corpus: 3.18x faster than PyTorch/MPS at cosine 0.9995
        # agreement and identical top-5 ranking, while avoiding the MPS graph-cache
        # leak (pytorch/pytorch#154329) that grew system swap by ~10GB per full run.
        provider = MLXEmbedder(batch_size=config.embed_batch_size)
    else:
        provider = LocalEmbedder(config.embed_model, config.embed_batch_size)
    return CachedEmbedder(provider, corpus / ".alexandria" / "cache" / "embeddings.sqlite",
                          progress_every=config.index_progress_every)


def _load_chunk_records(corpus: Path, config: AppConfig, limit: int, workers: int) -> tuple[list[dict], list[str]]:
    paths = []
    for path in sorted(corpus.rglob("*.md")):
        if is_indexable_source(path.relative_to(corpus)):
            paths.append(path)
    if limit:
        paths = paths[:limit]
    records: list[dict] = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for chunk_records, error in pool.map(lambda path: chunk_doc_records(path, corpus, config), paths):
            records.extend(chunk_records)
            if error:
                errors.append(error)
    return records, errors


def _print_eval_error(message: str, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"error": message}, ensure_ascii=False))
    else:
        print(f"UNUSABLE golden set: {message}", file=sys.stderr)


def _print_eval_report(report: EvalReport, delta) -> None:
    print(f"{'id':<36} result  rank  latency")
    for result in report.results:
        status = "ERROR" if result.error else "HIT" if result.hit else "MISS"
        print(f"{result.id:<36} {status:<6} {result.rank:>4}  {result.latency_ms:>8.3f}ms")
        if result.error:
            print(f"  error: {result.error}")
    summary = report.summary
    scored = summary.n - len(summary.target_errors)
    print(f"\nrecall@k: {summary.recall_at_k:.1%} ({summary.hits}/{scored})  "
          f"MRR: {summary.mrr:.3f}  errors: {summary.errors}")
    if summary.misses:
        print("misses: " + ", ".join(summary.misses))
    if summary.target_errors:
        print("target errors: " + ", ".join(summary.target_errors))
    if summary.error_ids:
        print("query errors: " + ", ".join(summary.error_ids))
    bands = by_overlap_band(report.results)
    if bands:
        print(f"\n{'overlap band':<12} {'n':>4} {'recall@k':>10} {'MRR':>7}")
        for band in ("literal", "partial", "zero"):
            if band in bands:
                b = bands[band]
                print(f"{band:<12} {b.n:>4} {b.recall_at_k:>9.1%} {b.mrr:>7.3f}")
    if report.separation:
        sep = report.separation
        print(f"\nprecision (negative set, n={sep['n_negative']}):")
        print(f"  positive top-1 median {sep['positive_top1_median']:.4f}  "
              f"negative top-1 median {sep['negative_top1_median']:.4f}")
        print(f"  clean floor {sep['clean_floor']:.4f} retains "
              f"{sep['clean_floor_recall']:.1%} of answerable queries"
              f"{'' if sep['separable'] else '  [NOT SEPARABLE]'}")
    elif report.negatives:
        print(f"\nprecision: {len(report.negatives)} negatives ran, separation not computable")
    if delta is not None:
        print(f"\nvs previous: recall {delta.recall_at_k:+.1%}, MRR {delta.mrr:+.3f}")
        if delta.negative_confidence_rose:
            print("PRECISION REGRESSION -- more confident on unanswerable queries: "
                  + ", ".join(delta.negative_confidence_rose))
        if delta.hit_to_miss:
            print("HIT->MISS: " + ", ".join(delta.hit_to_miss))
        if delta.miss_to_hit:
            print("MISS->HIT: " + ", ".join(delta.miss_to_hit))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="alexandria", description=__doc__)
    p.add_argument("--corpus", default=None, help="corpus repo path (overrides config)")
    sub = p.add_subparsers(dest="command", required=True)

    m = sub.add_parser("migrate", help="one-shot transform-copy of a flat vault")
    m.add_argument("kind", choices=["kg-sync"])
    m.add_argument("vault")
    m.add_argument("--dry-run", action="store_true")
    m.set_defaults(func=cmd_migrate)

    s = sub.add_parser("sync", help="pull + distil from a connector")
    s.add_argument("connector")
    s.add_argument("--sessions-dir", default=str(Path.home() / ".pi/agent/sessions"))
    s.add_argument("--memory-dir", default=os.environ.get("ALEXANDRIA_MEMORY_DIR", ""),
                  help="harness memory store dir (markdown-memory)")
    s.add_argument("--projects-dir", default="")
    s.add_argument("--vault-dir", default=str(Path.home() / "knowledge-graph"),
                   help="Obsidian knowledge-graph vault (knowledge-graph connector)")
    s.add_argument("--journal-path",
                   default=str(Path.home() / "citadel/personal-finance/accountability.md"))
    s.add_argument("--base-url", default="http://127.0.0.1:20128/v1")
    s.add_argument("--model", default="claude-haiku-4-5")
    s.add_argument("--limit", type=int, default=0)
    s.add_argument("--workers", type=int, default=6)
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--caller", default=os.environ.get("ALEXANDRIA_CALLER", "cli"),
                   help="UNVERIFIED tool label recorded in the audit trail; not an identity")
    s.set_defaults(func=cmd_sync)

    remember = sub.add_parser("remember",
                              help="append a user-confirmed memory to the inbox")
    remember.add_argument("text")
    remember.add_argument("--from", dest="from_", default="pi",
                          help="harness provenance (default pi)")
    remember.add_argument("--session", default="",
                          help="session id for provenance")
    remember.add_argument("--corrects", default="",
                          help="source_id this entry corrects/supersedes")
    remember.set_defaults(func=cmd_remember)

    promote = sub.add_parser("promote",
                             help="drain: promote every currently-pending remember entry")
    promote.set_defaults(func=cmd_promote)

    reconcile = sub.add_parser("reconcile",
                               help="independent check: every inbox entry has a promoted doc")
    reconcile.set_defaults(func=cmd_reconcile)

    backup = sub.add_parser("backup", help="back up .alexandria state (never the rebuildable indexes)")
    backup.add_argument("dest", help="path to write the .tar.gz archive")
    backup.set_defaults(func=cmd_backup)

    restore = sub.add_parser("restore", help="restore .alexandria state from a backup archive")
    restore.add_argument("archive", help="path to a backup_state() .tar.gz archive")
    restore.add_argument("--dry-run", action="store_true", help="list what would be restored, write nothing")
    restore.set_defaults(func=cmd_restore)

    lint = sub.add_parser("lint", help="validate every document against the schema")
    lint.set_defaults(func=cmd_lint)

    serve = sub.add_parser("serve", help="stdlib HTTP server: /health /search /answer /remember")
    serve.add_argument("--host", default=os.environ.get("ALEXANDRIA_SERVE_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("ALEXANDRIA_SERVE_PORT", "8420")))
    serve.add_argument("--unix-socket", action="append", default=[],
                       metavar="IDENTITY=PATH",
                       help="one Unix socket per client identity (§5.3); repeatable")
    serve.set_defaults(func=cmd_serve)

    index = sub.add_parser("index", help="chunk, embed, and index the corpus")
    index.add_argument("--rebuild", action="store_true", help="recreate index tables (retain embedding cache)")
    index.add_argument("--backfill-manifest", action="store_true",
                       help="write only the index manifest for the current --embed-provider "
                            "config, without re-indexing (one-time fix for an index built "
                            "before manifests existed; see gate F4)")
    index.add_argument("--limit", type=int, default=0, help="maximum documents to index")
    index.add_argument("--workers", type=int, default=1, help="parallel document chunking workers")
    index.add_argument("--enrich", action="store_true",
                       help="enrich documents (summary/keywords/hypotheticals) before indexing")
    index.add_argument("--enrich-model", default="deepseek-v4-flash",
                       help="LLM for document enrichment")
    index.add_argument("--enrich-limit", type=int, default=100,
                       help="max documents that need enrichment this run (0=all)")
    index.add_argument("--enrich-workers", type=int, default=1,
                       help="threads for the enrichment LLM calls (default 1)")
    index.add_argument("--enrich-prompt-version", default="v1",
                       help="enrichment recipe version; a change forces re-enrichment")
    index.add_argument("--reattach-only", action="store_true",
                       help="replay stored enrichment without an LLM gateway "
                            "(for rebuilding an index over an already-enriched corpus)")
    index.add_argument("--base-url", default="http://127.0.0.1:20128/v1",
                       help="gateway base URL for the enrichment LLM")
    index.add_argument("--api-key-env", default="ALEXANDRIA_LLM_KEY",
                       help="environment variable holding the gateway API key")
    index.set_defaults(func=cmd_index)

    search = sub.add_parser("search", help="hybrid retrieval over indexed chunks")
    search.add_argument("query")
    search.add_argument("--k", type=int, default=None)
    search.add_argument("--type")
    search.add_argument("--project")
    search.add_argument("--layer", choices=["sources", "wiki"])
    search.add_argument("--trace", action="store_true")
    search.add_argument("--caller", default=os.environ.get("ALEXANDRIA_CALLER", "cli"),
                       help="UNVERIFIED tool label recorded in the audit trail; not an identity")
    search.set_defaults(func=cmd_search)

    evaluate = sub.add_parser("eval", help="score retrieval against the private golden set")
    evaluate.add_argument("--golden", help="path to the private golden JSONL file")
    evaluate.add_argument("--negative", help="path to the negative (unanswerable-query) JSONL file")
    evaluate.add_argument("--k", type=int, help="override every entry's retrieval depth")
    evaluate.add_argument("--json", action="store_true", help="emit a machine-readable report")
    evaluate.add_argument("--compare-last", action="store_true", help="show transitions from the prior run")
    evaluate.add_argument("--allow-partial-index", action="store_true",
                          help="measure even if a rebuild is in progress or was "
                               "interrupted (the result is not a valid baseline)")
    evaluate.add_argument("--fail-on-regression", action="store_true",
                          help="exit 1 when a prior hit becomes a miss")
    evaluate.set_defaults(func=cmd_eval)

    answer = sub.add_parser("answer", help="synthesize a cited answer page for a question")
    answer.add_argument("question")
    answer.add_argument("--k", type=int, default=8, help="gather seed depth")
    answer.add_argument("--base-url", default="http://127.0.0.1:20128/v1")
    answer.add_argument("--api-key-env", default="ALEXANDRIA_LLM_KEY")
    answer.add_argument("--llm-model", default="openrouter/anthropic/claude-sonnet-5",
                        help="gather/write/repair model (the measurement-proven config)")
    answer.add_argument("--grader-a-model", default="openrouter/anthropic/claude-sonnet-5")
    answer.add_argument("--grader-b-model", default="deepseek-v4-pro")
    answer.add_argument("--prompt-version", default="v1")
    answer.add_argument("--caller", default=os.environ.get("ALEXANDRIA_CALLER", "cli"),
                       help="UNVERIFIED tool label recorded in the audit trail; not an identity")
    answer.add_argument("--save-dir", default=None,
                        help="emit the page here (default: temp dir, page printed only)")
    answer.set_defaults(func=cmd_answer)

    ws = sub.add_parser("wiki-site", help="render a wiki dir into a static site")
    ws.add_argument("--wiki", default=None, help="wiki dir (default: <corpus>/wiki)")
    ws.add_argument("--out", type=Path, required=True)
    ws.set_defaults(func=cmd_wiki_site)

    au = sub.add_parser("audit", help="summarize the pipeline audit logs")
    au.add_argument("--last", type=int, default=200)
    au.set_defaults(func=lambda a: print(audit_summary(_config_for(a).corpus_path, a.last)) or 0)

    c = sub.add_parser("cache", help="cache stats and maintenance")
    c.add_argument("--clear", action="store_true")
    c.set_defaults(func=cmd_cache)

    d = sub.add_parser("decay", help="propose eviction from a capped memory store")
    d.add_argument("stores", nargs="+")
    d.add_argument("--apply", action="store_true")
    d.add_argument("--ingested-ok", action="store_true")
    d.add_argument("--target", type=float, default=0.80)
    d.set_defaults(func=lambda a: __import__("alexandria.decay", fromlist=["_run"])._run(a))

    return p


def app(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(app())
