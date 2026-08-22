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
import subprocess
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
    answer_pipeline_fingerprint,
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
from .index.chunker import (
    chunk_doc_records,
    chunk_document,
    is_appledouble_metadata,
    is_indexable_source,
)
from .index.embedder import CachedEmbedder, HashEmbedder, LocalEmbedder, MLXEmbedder
from .index.manifest import (ManifestCorrupt, ManifestMismatch, ManifestMissing,
                             verify_manifest, verify_manifest_for_write, write_manifest)
from .index.releases import (ActiveReleaseMissing, ReleaseCorrupt, activate_release,
                              active_release_id, checksum_release, list_releases,
                              new_release_dir, resolve_active_index_dir, verify_checksums)
from .index.store import VectorStore
from . import liveness
from .backup import backup_state, restore_state
from .llm import LLMClient, RequestDeadline
from .migrate import migrate_kg_sync
from .monitor import QueryLogger
from .pending import create_pending, list_pending, oldest_pending_age
from .promote import promote_pending
from .reconcile import reconcile_inbox
from .retrieval.rerank import CrossEncoderReranker
from .retrieval.search import SearchConfig, SearchEngine
from .schema import Severity, validate
from .synthesis.gather import MAX_FOLLOW_UP_QUERIES
from .synthesis.judge import MAX_AUDIT_CONCURRENCY
from .writelock import (DEFAULT_LOCK_TIMEOUT, IndexReadUnavailable, index_read_lock,
                        rebuild_marker, write_lock)


def _bounded_non_negative_int(value: str, *, maximum: int) -> int:
    """Argparse converter for a bounded count whose zero is meaningful."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if not 0 <= parsed <= maximum:
        raise argparse.ArgumentTypeError(f"must be between 0 and {maximum}")
    return parsed


def _follow_up_query_count(value: str) -> int:
    return _bounded_non_negative_int(value, maximum=MAX_FOLLOW_UP_QUERIES)


def _audit_concurrency_count(value: str) -> int:
    return _bounded_non_negative_int(value, maximum=MAX_AUDIT_CONCURRENCY)


def cmd_migrate(args) -> int:
    report = migrate_kg_sync(args.vault, args.corpus, dry_run=args.dry_run)
    print(report.render())
    if not report.reconciles:
        print("\nFAIL: counts do not reconcile", file=sys.stderr)
        return 1
    return 0


def cmd_ingest(args) -> int:
    """#51: preserve artifacts (PDF/image) and write their indexed companions.

    Accepts a file, a directory, or a glob, because nothing calls this for you:
    the weekly loop drives connectors, so ingest is a deliberate human/bridge
    action. A batch keeps going past a bad artifact but still exits non-zero --
    silently dropping one file out of twenty is how a memory disappears.
    """
    from .ingest import ExtractionFailed, UnsupportedArtifact, ingest_path, refresh_ingest

    config = _config_for(args)
    corpus = config.corpus_path

    # Every corpus MUTATION holds WriteLock exclusively -- that is the
    # invariant IndexReadLock (used by lint and every reader) depends on to
    # classify "writer active" vs "at rest" (Red review, 2026-08-20). Ingest
    # is a writer; without the lock, `alexandria lint` running concurrently
    # could see a mid-transaction .partial or an asset-before-companion
    # moment and report a transient state as corruption.
    lock = write_lock(corpus)
    if not lock.acquire(blocking=True, timeout=DEFAULT_LOCK_TIMEOUT):
        holder = lock.holder_pid()
        raise SystemExit(
            f"alexandria: ingest could not acquire the corpus write lock within "
            f"{DEFAULT_LOCK_TIMEOUT:.0f}s (held by {holder or 'an unknown process'}). "
            f"Refusing to run a racing ingest -- retry after the current writer finishes.")
    try:
        return _cmd_ingest_locked(args, config, corpus)
    finally:
        lock.release()


def _cmd_ingest_locked(args, config: AppConfig, corpus: Path) -> int:
    """The body of `ingest`, run while the corpus write lock is held."""
    from .ingest import ExtractionFailed, UnsupportedArtifact, ingest_path, refresh_ingest

    if getattr(args, "refresh", False):
        refreshed = failed = 0
        for raw in args.paths:
            try:
                result = refresh_ingest(corpus, raw, re_extract=getattr(args, "re_extract", False))
            except (UnsupportedArtifact, ExtractionFailed) as exc:
                print(f"ingest: refresh failed for {raw}: {exc}", file=sys.stderr)
                failed += 1
                continue
            print(f"ingest: refreshed {result.doc_path} (via {result.extraction})")
            refreshed += 1
        print(f"ingest: {refreshed} refreshed, {failed} failed")
        if refreshed:
            print("ingest: run `alexandria index` to re-embed the updated text")
        return 1 if failed and not refreshed else (2 if not refreshed and not failed else 0)

    from .ingest import IMAGE_SUFFIXES, PDF_SUFFIXES

    supported = PDF_SUFFIXES | IMAGE_SUFFIXES
    # An explicitly named file is an instruction and must fail loudly if it
    # cannot be honored. A file merely SWEPT UP by a directory/glob walk is
    # not: a folder always holds .DS_Store and notes.txt, and if every stray
    # forced a non-zero exit the operator would learn to ignore the code --
    # destroying the signal for the failure that actually matters.
    targets: list[tuple[Path, bool]] = []   # (path, explicitly_named)
    for raw in args.paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            targets.extend((p, False) for p in sorted(path.rglob("*"))
                           if p.is_file() and p.suffix.lower() in supported)
        elif path.exists():
            targets.append((path, True))
        else:
            # Unexpanded glob or a typo. Path().glob() raises on absolute
            # patterns, so split the pattern from its anchor explicitly.
            pattern = Path(raw).expanduser()
            root = Path(pattern.anchor) if pattern.anchor else Path()
            rel = str(pattern.relative_to(pattern.anchor)) if pattern.anchor else str(pattern)
            try:
                matches = sorted(root.glob(rel))
            except (NotImplementedError, ValueError, OSError):
                matches = []
            if not matches:
                print(f"ingest: no such path: {raw}", file=sys.stderr)
                return 2
            targets.extend((p, False) for p in matches
                           if p.is_file() and p.suffix.lower() in supported)

    if not targets:
        print("ingest: nothing to ingest", file=sys.stderr)
        return 2

    ingested = failed = 0
    for path, explicit in targets:
        try:
            result = ingest_path(path, corpus)
        except (UnsupportedArtifact, ExtractionFailed) as exc:
            print(f"ingest: skipped {path.name}: {exc}", file=sys.stderr)
            failed += explicit or isinstance(exc, ExtractionFailed)
            continue
        except OSError as exc:
            # Unreadable file, disk full, permission denied: a per-file skip,
            # never an abandoned batch. Counted as a real failure.
            print(f"ingest: skipped {path.name}: {exc}", file=sys.stderr)
            failed += 1
            continue
        ingested += 1
        print(f"ingest: {path.name} -> {result.doc_path} "
              f"(asset {result.asset_path}, via {result.extraction})")

    print(f"ingest: {ingested} artifact(s) stored, {failed} failed")
    if ingested:
        print("ingest: run `alexandria index` to make them searchable")
    # Non-zero only for REAL failures (explicitly named, or extraction broke on
    # a supported artifact) -- a partial batch reporting success is the
    # "reported success while doing nothing" class this project keeps finding.
    return 1 if failed else 0


def cmd_lint(args) -> int:
    corpus = _config_for(args).corpus_path
    # Lint is a READER: take the shared read lock so a concurrent writer
    # (index, ingest, promote) cannot produce transient false positives --
    # a mid-write .partial or an asset-before-companion moment (Red review,
    # 2026-08-20). IndexReadLock is non-blocking with a short bounded retry,
    # so a busy writer yields a clear "retry" instead of a wrong report.
    try:
        with index_read_lock(corpus):
            return _cmd_lint_locked(args, corpus)
    except IndexReadUnavailable as exc:
        print(f"alexandria: lint deferred: {exc}", file=sys.stderr)
        return 3


def _cmd_lint_locked(args, corpus) -> int:
    """The body of `lint`, run while the shared read lock is held (so the
    scan sees a stable, at-rest corpus)."""
    errors = checked = 0
    for path in sorted(corpus.rglob("*.md")):
        rel = path.relative_to(corpus)
        # This is an independent validation walk, but Finder's ``._`` sidecars
        # have the same central metadata exclusion as indexing/health: they
        # are not malformed corpus documents to report.
        if ("_unparsed" in rel.parts or ".alexandria" in rel.parts
                or "inbox" == rel.parts[0] or is_appledouble_metadata(rel)):
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
    from .ingest import lint_assets
    asset_findings = lint_assets(corpus)
    for finding in asset_findings:
        print(f"ERROR (assets): {finding}")
        errors += 1

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
    # #8 residual, found while fixing the CLI attribution gap: --caller has
    # existed on this verb's argparse but was never actually read here --
    # a silently dead flag, not a security gap, but wrong all the same.
    logger.sync(connector=conn.name, duration_ms=int((time.time() - t0) * 1000),
                discovered=total, normalized=total - failed,
                committed=written, skipped=len(skipped), errors=conn.errors[:20],
                caller=caller_label(getattr(args, "caller", "cli")), user=cli_identity())
    return 0


@dataclass
class RememberResult:
    """Shared outcome type for both `cmd_remember` (CLI) and serve's
    /remember handler (§5), so the §7.1 write-ordering contract (marker
    written BEFORE success is reported) is implemented exactly once."""
    entry: InboxEntry | None
    status: str  # "written" | "duplicate" | "empty" | "inbox_write_failed"
    #             | "marker_failed" | "invalid"
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
        # A short write leaves a torn entry. `marker_failed` would be a lie --
        # the marker was never attempted -- and reconcile keys off that status,
        # so mislabelling here sends recovery down the wrong path.
        return RememberResult(None, "inbox_write_failed", path=path,
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
    # #30 P2a: write into whatever is CURRENTLY active, same resolution as
    # every read -- once a release is active, a promoted fact must land
    # where search actually looks, not the abandoned legacy path.
    try:
        index_dir = resolve_active_index_dir(corpus)
    except (ActiveReleaseMissing, ReleaseCorrupt) as exc:
        raise SystemExit(f"alexandria: {exc}") from exc
    store = VectorStore(index_dir)
    lexical = BM25Index(index_dir / "fts.sqlite")
    try:
        result = promote_pending(corpus, config, embedder, store, lexical)
    except ManifestMissing as exc:
        # A corpus indexed before the declared L2 policy has stored vectors
        # whose representation cannot be proven. The guard is intentional; do
        # not offer an assertion-only escape hatch over a non-empty store.
        raise SystemExit(
            f"alexandria: {exc}\n"
            "  this index predates declared normalization policy and must be rebuilt:\n"
            "    alexandria --corpus <path> index --rebuild") from exc
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
    requeued; so is one whose marker is older than 2x the drain interval (F6:
    a marker downgrades severity only while it is young enough to still be
    plausible). An unpromoted entry with a FRESH marker is normal backlog,
    not a fault."""
    corpus = _config_for(args).corpus_path
    report = reconcile_inbox(corpus)
    print(f"reconcile: {report.total_entries} entries in {report.total_files} files, "
          f"{len(report.stranded)} stranded, {len(report.already_pending)} pending, "
          f"{len(report.unreadable_files)} unreadable")
    for name in report.unreadable_files:
        print(f"reconcile: unreadable inbox file {name}", file=sys.stderr)
    for entry_id in report.stranded:
        note = "requeued" if entry_id in report.requeued else "marker stale, left as is"
        print(f"reconcile: stranded entry {entry_id} ({note})", file=sys.stderr)
    return 0 if report.healthy else 1


def _doc_path_for(corpus: Path, doc_id: str) -> Path:
    """doc_id -> corpus-relative markdown path, inverting corpus.doc_id().
    Lenient about a caller passing the trailing `.md` either way.

    Refuses to name anything outside the indexable tree. `delete` writes
    frontmatter, so a doc_id that resolves outside `sources/`/`wiki/` -- via an
    absolute path, `.`/`..` traversal, or a symlink escaping the corpus -- would
    mutate a file `delete` has no business touching. `is_indexable_source` is
    the same single source of truth the indexer and /health use, so a delete
    target and an indexable document can never disagree."""
    stem = doc_id[:-3] if doc_id.endswith(".md") else doc_id
    rel = Path(stem)
    if rel.is_absolute():
        raise ValueError(f"not a corpus-relative document id: {doc_id!r}")
    if not rel.parts or any(part in (".", "..") for part in rel.parts):
        raise ValueError(f"document id must not contain path traversal: {doc_id!r}")
    if not is_indexable_source(rel):
        raise ValueError(
            f"document id is not indexable (must be under sources/ or wiki/): {doc_id!r}")
    path = corpus / f"{rel}.md"
    if not path.resolve().is_relative_to(corpus.resolve()):
        raise ValueError(f"document id resolves outside the corpus: {doc_id!r}")
    return path


def _cmd_list_deleted(corpus: Path) -> int:
    """Source of truth is frontmatter (SPEC §D4a-style: the index is a
    rebuildable projection), so this walks `sources/`+`wiki/` directly rather
    than querying the index -- it is correct even before the corpus has ever
    been indexed, and it can never disagree with what a rebuild would derive.
    """
    found: list[str] = []
    for path in sorted(corpus.rglob("*.md")):
        rel = path.relative_to(corpus)
        if not is_indexable_source(rel):
            continue
        try:
            doc = Doc.read(path, root=corpus)
        except ValueError:
            continue  # cmd_lint's job to report malformed frontmatter, not this one's
        if doc.frontmatter.get("deleted") is True:
            found.append(doc.doc_id)
    if not found:
        print("delete --list: no documents are flagged deleted")
        return 0
    for doc_id in found:
        print(doc_id)
    print(f"\n{len(found)} document(s) flagged deleted")
    return 0


class _DeleteRefused(Exception):
    """A delete/erase domain refusal whose message is safe to print verbatim."""


class _DeleteProjectionFailed(Exception):
    """SOL-03: the dense/lexical reprojection failed partway under the lock.

    The durable frontmatter is written either way, so a later delete/index
    converges; the lock holder must report the partial state accurately.
    """

    def __init__(self, cause: Exception) -> None:
        super().__init__(str(cause))
        self.cause = cause


def _apply_delete(corpus: Path, doc_id: str, *, want_deleted: bool) -> tuple[int, bool, Path]:
    """Tombstone (or undelete) one document under an already-held write lock.

    Lock-held domain primitive shared by ``cmd_delete`` and ``cmd_erase``:
    the caller MUST hold the corpus write lock.  ``cmd_erase`` needs the
    tombstone, cache invalidation, history rewrite, and target
    synchronization to be ONE critical section; ``cmd_delete`` acquires the
    lock and immediately delegates here, so no path exists that writes the
    tombstone outside the lock.

    Returns ``(updated_rows, already_in_state, index_dir)``.  Raises
    ``_DeleteRefused`` for an operator error (bad id, missing file,
    malformed frontmatter, unusable active release) and
    ``_DeleteProjectionFailed`` when the index projection fails partway.
    """
    try:
        path = _doc_path_for(corpus, doc_id)
    except ValueError as exc:
        raise _DeleteRefused(str(exc)) from exc
    if not path.is_file():
        raise _DeleteRefused(f"no such document: {doc_id}")
    try:
        doc = Doc.read(path, root=corpus)
    except ValueError as exc:
        raise _DeleteRefused(str(exc)) from exc
    already = doc.frontmatter.get("deleted") is True
    doc.frontmatter["deleted"] = want_deleted
    doc.write(corpus)

    try:
        # #30 P2a: same active-index resolution as promote/index -- a
        # tombstone must land where search actually looks.
        try:
            index_dir = resolve_active_index_dir(corpus)
        except (ActiveReleaseMissing, ReleaseCorrupt) as exc:
            raise SystemExit(f"alexandria: {exc}") from exc
        store = VectorStore(index_dir)
        lexical = BM25Index(index_dir / "fts.sqlite")
        # Dense first. If the dense flip commits and the lexical flip then
        # fails, the dense record's deleted='true' is still enforced at
        # hydration in search.py, so a stale lexical candidate cannot surface
        # the document.
        dense_n = store.mark_deleted(doc.doc_id, want_deleted)
        lexical_n = lexical.mark_deleted(doc.doc_id, want_deleted)
        updated = max(dense_n, lexical_n)
        write_index_generation(corpus)
    except Exception as exc:
        # SOL-03: the two stores are separate databases; there is no shared
        # transaction. If the dense flip committed before the lexical flip
        # raised, invalidate the generation anyway so a cached pre-delete
        # result cannot resurrect the document. The durable frontmatter plus
        # idempotent mark_deleted make the next `delete`/`index` converge.
        try:
            write_index_generation(corpus)
        except Exception:
            pass
        raise _DeleteProjectionFailed(exc) from exc
    return updated, already, index_dir


def cmd_delete(args) -> int:
    """Soft-delete (or --undelete) one document, and reproject the flag into
    both indexes immediately -- never return 0 having only touched
    frontmatter. SPEC §D4a's blocker is exactly a frontmatter-only tombstone:
    it survives `store.upsert`'s field projection by being silently dropped,
    so the chunk stays fully retrievable. `deleted` is a SCALAR_FIELDS member
    (index/store.py) and a real chunk_metadata column (index/bm25.py) precisely
    so that cannot happen here. Note it is deliberately NOT in bm25's
    METADATA_COLUMNS: that tuple is the user-facing filter whitelist, and this
    flag is enforced unconditionally by not_deleted_clause on every query
    rather than being opt-in per request.

    The durable half of the flag is the frontmatter write: `deleted` is a
    document property re-derived by `doc_frontmatter_metadata` on every
    reindex (index/chunker.py), so a `--rebuild` or a later `promote` can
    never resurrect a tombstoned document by accident -- it will just derive
    the same `deleted: true` fresh from disk. The index write below is purely
    an optimization so the effect is immediate instead of waiting on the next
    `alexandria index`.

    The tombstone runs entirely under the corpus write lock (the same lock
    index/promote/erase use), so a concurrent index or a racing erase can
    never interleave between the frontmatter write and the index projection.
    """
    config = _config_for(args)
    corpus = config.corpus_path
    if args.list:
        return _cmd_list_deleted(corpus)
    if not args.doc_id:
        print("alexandria: delete requires a doc_id (or --list)", file=sys.stderr)
        return 1
    want_deleted = not args.undelete
    verb = "undeleted" if args.undelete else "deleted"

    lock = write_lock(corpus)
    if not lock.acquire(blocking=True, timeout=DEFAULT_LOCK_TIMEOUT):
        holder = lock.holder_pid()
        print(f"alexandria: could not acquire the corpus write lock within "
              f"{DEFAULT_LOCK_TIMEOUT:.0f}s (held by {holder or 'an unknown process'}) "
              f"-- nothing was touched; retry once it is free.", file=sys.stderr)
        return 1
    try:
        updated, already, index_dir = _apply_delete(corpus, args.doc_id, want_deleted=want_deleted)
    except _DeleteRefused as exc:
        print(f"alexandria: {exc}", file=sys.stderr)
        return 1
    except _DeleteProjectionFailed as exc:
        print(f"alexandria: frontmatter updated ({args.doc_id} is now {verb}) but "
              f"the index projection failed partway ({exc}); re-run `alexandria delete "
              f"{args.doc_id}` or `alexandria index` to converge.", file=sys.stderr)
        return 1
    finally:
        lock.release()

    # #6 erasure-core, Red review 2026-08-21 (finding #3): a tombstoned
    # document's cached enrichment payload is invalidated as CLEANUP here
    # (best-effort, deliberately outside the lock and the tombstone's own
    # try/except so an invalidation failure gets its OWN diagnosis, never
    # conflated with the SOL-03 dense/lexical-projection narrative above).
    # The load-bearing guard is now at the point of USE
    # (enrich_docs_for_index skips deleted=True records outright), so a
    # failure here is a missed cleanup opportunity, not a correctness gap --
    # the document cannot be re-enriched regardless of whether this
    # succeeds. Delete only; --undelete leaves a still-valid payload alone.
    if want_deleted:
        try:
            from .enrich import EnrichmentStore
            EnrichmentStore(index_dir).invalidate(args.doc_id)
        except Exception as exc:
            print(f"alexandria: {args.doc_id} {verb}, but could not invalidate its "
                  f"cached enrichment payload ({exc}) -- harmless: "
                  f"enrich_docs_for_index refuses to re-enrich a tombstoned "
                  f"document regardless, so this is a missed cache cleanup, "
                  f"not a correctness issue.", file=sys.stderr)

    note = ", 0 chunk(s) not yet indexed (will apply on the next `index`)" if updated == 0 else ""
    if already == want_deleted:
        print(f"delete: {args.doc_id} was already {verb}{note}")
    else:
        print(f"delete: {args.doc_id} {verb} -- {updated} chunk(s) updated{note}")
    return 0


def cmd_erase(args) -> int:
    """#6 erasure-core, tail: remove a document's raw text from the corpus
    git repository's history, not just from the retrievable surface.

    Per docs/DECISION-erasure-scope-q1.md's ratified answer: the audit
    trail and backups are NOT touched by this command -- only git history
    and the (whole, rebuildable) embedding cache, invalidated BEFORE the
    rewrite: the cache is content-addressed with no document identity, so
    no targeted key list can prove it covers historical revisions,
    transformed text, or prior chunking, and the cache can only be rebuilt
    while the corpus remains usable.

    A deliberate, separate, confirmation-gated operation -- never bundled
    into `alexandria delete`, matching the exact instruction that shaped
    this design. Irreversible in the sense that the ORIGINAL git objects
    are gone from the corpus's live .git after this succeeds (the pre-erase
    .git is retained at .alexandria/erase-backups/<generation>/git as a
    manual-recovery copy -- never overwritten or deleted by this tool,
    matching the "never silently delete the rollback path" idiom of #30
    P2a's staged releases).

    Holds the corpus write lock across the WHOLE operation (tombstone +
    cache invalidation + git history rewrite + target reconciliation), not
    just the tombstone half -- per this item's own failure-frame note
    (docs/FAILURE-FRAME-erase-git-history.md): a concurrent index/promote/
    second-erase racing against a git history rewrite must never be
    possible. The authoritative document resolution and Git preflight run
    UNDER that lock; the unlocked preview that prints the blast radius is
    read-only and non-authoritative by design.

    OUT OF SCOPE (named explicitly, not silently skipped, per this item's
    own failure-frame note): a dangling wikilink or cross-reference from
    ANOTHER, non-erased document pointing at this one is NOT this
    command's job to detect or fix. `impact_report()` above only surfaces
    citations recorded in the #9 audit trail (a read-only informational
    warning), not a scan of every other document's body text.
    """
    from .erasure import GitEraseError, erase_from_git_history, impact_report, preflight_git_erase

    config = _config_for(args)
    corpus = config.corpus_path
    # Unlocked, read-only preview of the blast radius. The lock is taken
    # later, and every check below is re-run authoritatively under it.
    try:
        path = _doc_path_for(corpus, args.doc_id)
    except ValueError as exc:
        print(f"alexandria: {exc}", file=sys.stderr)
        return 1
    if not path.is_file():
        print(f"alexandria: no such document: {args.doc_id}", file=sys.stderr)
        return 1
    rel_path = str(path.relative_to(corpus))

    # Impact report FIRST -- an operator sees what this document has been
    # cited in before committing to an irreversible action, per #9's
    # citation tuples (audit trail; NOT touched by this command).
    citing_answers = impact_report(corpus, args.doc_id)

    try:
        preview = preflight_git_erase(corpus, rel_path)
    except GitEraseError as exc:
        print(f"alexandria: {exc}", file=sys.stderr)
        return 1

    if not args.yes:
        print(f"alexandria: about to permanently erase {args.doc_id!r} from git "
              f"history ({rel_path}, {preview.path_touching_commits} commit(s) "
              f"affected) and invalidate the whole rebuildable embedding cache. "
              f"THIS CANNOT BE UNDONE from within this tool.")
        if citing_answers:
            print(f"alexandria: WARNING -- this document was cited in "
                  f"{len(citing_answers)} past answer(s) (audit trail is NOT "
                  f"erased, per the ratified decision -- those records stay, "
                  f"but the cited document's own history will be gone).")
        print("alexandria: re-run with --yes to proceed.", file=sys.stderr)
        return 3

    # Authoritative phase: lock FIRST, then resolve and preflight under the
    # lock. Nothing is mutated before this point.
    lock = write_lock(corpus)
    if not lock.acquire(blocking=True, timeout=DEFAULT_LOCK_TIMEOUT):
        holder = lock.holder_pid()
        print(f"alexandria: could not acquire the corpus write lock within "
              f"{DEFAULT_LOCK_TIMEOUT:.0f}s (held by {holder or 'an unknown process'}) "
              f"-- nothing was touched; retry once it is free.", file=sys.stderr)
        return 1
    try:
        # Re-resolve and re-preflight under the lock: the document (or the
        # repository shape) can legitimately have changed since the preview.
        try:
            path = _doc_path_for(corpus, args.doc_id)
        except ValueError as exc:
            print(f"alexandria: {exc}", file=sys.stderr)
            return 1
        if not path.is_file():
            print(f"alexandria: no such document: {args.doc_id}", file=sys.stderr)
            return 1
        rel_path = str(path.relative_to(corpus))
        try:
            preflight = preflight_git_erase(corpus, rel_path)
        except GitEraseError as exc:
            print(f"alexandria: {exc}", file=sys.stderr)
            return 1

        # Tombstone first (a document must be unretrievable before its
        # history is scrubbed, not after). The tombstone deliberately makes
        # rel_path dirty; erase_from_git_history receives the lock-captured
        # preflight and allow_target_dirty=True so that single intentional
        # change is tolerated and every OTHER tracked change still fails.
        try:
            _apply_delete(corpus, args.doc_id, want_deleted=True)
        except _DeleteRefused as exc:
            print(f"alexandria: erase aborted -- tombstoning {args.doc_id} failed "
                  f"({exc}); nothing else was touched.", file=sys.stderr)
            return 1
        except _DeleteProjectionFailed as exc:
            print(f"alexandria: erase aborted -- tombstoning {args.doc_id} failed "
                  f"partway ({exc}); the durable frontmatter is written, so "
                  f"re-run `alexandria erase {args.doc_id} --yes` (or `alexandria "
                  f"index`) to converge; git history was NOT rewritten.",
                  file=sys.stderr)
            return 1

        # Whole-cache invalidation (not a targeted purge): the cache is
        # content-addressed with no doc_id, so a key list computed from the
        # CURRENT text cannot prove it covers historical revisions,
        # transformed text, or prior chunking. The cache is rebuildable; a
        # deliberate erase clears it entirely before rewriting history.
        # The invalidation must be PROVEN, not assumed: fail closed if any
        # durable row survives (Red review round 2, finding 3).
        embedder = _cached_embedder(config, corpus)
        try:
            remaining = embedder.cache_row_count()
            purged = embedder.purge_all()
            if remaining and embedder.cache_row_count():
                # Fail closed BEFORE history is rewritten.  Roll the tombstone
                # back so a retry can pass the clean-state preflight.
                try:
                    _rollback_tombstone_after_failed_erase(corpus, args.doc_id, rel_path)
                except Exception as rollback_exc:
                    print(f"alexandria: {args.doc_id} is tombstoned, but the embedding "
                          f"cache still contains rows after invalidation and rolling "
                          f"the tombstone back ALSO failed ({rollback_exc}); restore "
                          f"the document manually or re-run `alexandria delete "
                          f"{args.doc_id} --undelete`.", file=sys.stderr)
                    return 1
                print(f"alexandria: the embedding cache could not be invalidated "
                      f"({remaining} row(s) remained after purge_all); the tombstone "
                      f"was rolled back, so the corpus is UNCHANGED -- re-run "
                      f"`alexandria erase {args.doc_id} --yes` once the cache is "
                      f"writable.", file=sys.stderr)
                return 1
        finally:
            embedder.close()

        try:
            result = erase_from_git_history(
                corpus, rel_path, preflight=preflight, allow_target_dirty=True
            )
        except GitEraseError as exc:
            if exc.history_changed:
                print(f"alexandria: {args.doc_id} is tombstoned and its cache is "
                      f"invalidated, and the rewritten git directory is ALREADY "
                      f"ACTIVE, but the operation did not finish: {exc}. Re-run "
                      f"`alexandria erase {args.doc_id} --yes` to recover and "
                      f"complete the transaction; the pre-erase repository is "
                      f"retained under .alexandria/erase-backups/ for manual "
                      f"recovery.", file=sys.stderr)
            else:
                # Pre-swap failure: rewritten history never became active, so
                # HEAD still contains the original file.  Roll the tombstone
                # back so the corpus is left exactly as the operator found it
                # and a retry can pass the clean-state preflight (Red review
                # round 2, finding 2 -- the old code told the operator to
                # retry but the retry would be refused by the dirty target).
                try:
                    _rollback_tombstone_after_failed_erase(corpus, args.doc_id, rel_path)
                    print(f"alexandria: {args.doc_id} is tombstoned and its cache is "
                          f"invalidated, but the git-history rewrite failed before "
                          f"rewritten history became active: {exc}. The tombstone "
                          f"was rolled back, so the corpus is UNCHANGED -- re-run "
                          f"`alexandria erase {args.doc_id} --yes` to retry.",
                          file=sys.stderr)
                except Exception as rollback_exc:
                    print(f"alexandria: {args.doc_id} is tombstoned and its cache is "
                          f"invalidated, but the git-history rewrite failed before "
                          f"rewritten history became active: {exc}. Rolling the "
                          f"tombstone back ALSO failed ({rollback_exc}); restore "
                          f"the document manually (git show HEAD:{rel_path}) or "
                          f"re-run `alexandria delete {args.doc_id} --undelete`.",
                          file=sys.stderr)
            return 1
    finally:
        lock.release()

    print(f"erase: {args.doc_id} tombstoned, {purged} cache row(s) invalidated, "
          f"{result.path_touching_commits} commit(s) rewritten in git history.")
    if result.backup_git_dir is not None:
        print(f"erase: pre-erase git history retained at {result.backup_git_dir} "
              f"for manual recovery (never overwritten or deleted by this tool).")
    return 0


def _rollback_tombstone_after_failed_erase(corpus: Path, doc_id: str, rel_path: str) -> None:
    """Undo the tombstone half of a failed PRE-SWAP erase.

    Called only when ``erase_from_git_history`` raised with
    ``history_changed=False`` (rewritten history never became active), while
    the corpus write lock is held.  HEAD still contains the original file:
    restore its exact bytes and un-flag the index rows, so the corpus matches
    its pre-erase state and a retry can pass the clean-state preflight.
    """
    show = subprocess.run(["git", "show", f"HEAD:{rel_path}"], cwd=str(corpus),
                          capture_output=True, text=True, timeout=60)
    if show.returncode != 0:
        raise RuntimeError(
            f"could not restore {rel_path} from HEAD: {show.stderr.strip()}")
    path = corpus / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(show.stdout, encoding="utf-8")
    # Un-flag the index rows WITHOUT rewriting frontmatter: the restored file
    # is byte-identical to HEAD, so the durable half needs no change.
    index_dir = resolve_active_index_dir(corpus)
    VectorStore(index_dir).mark_deleted(doc_id, False)
    BM25Index(index_dir / "fts.sqlite").mark_deleted(doc_id, False)
    write_index_generation(corpus)


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
    # #6 erasure-core item 4, Red review 2026-08-21 (finding #1): the
    # generation counter can no longer regress at all -- restore_state()
    # structurally skips a regressing generation.json member rather than
    # writing it (see backup.py's restore_state docstring). This is
    # informational, not a warning about exposure: nothing is at risk.
    if result.generation_preserved:
        archive_gen, corpus_gen = result.generation_regression
        print(f"restore: this archive's saved generation ({archive_gen}) is older "
              f"than the corpus's current generation ({corpus_gen}) -- kept the "
              f"current, newer generation instead of restoring the older one, so "
              f"no cache entry from before this backup can become reachable "
              f"again. Every other backed-up path was restored normally.",
              file=sys.stderr)
    print(f"restore: {verb} {len(result.restored)} paths from {args.archive}")
    if result.skipped:
        # Every one, not a preview: this list is the evidence that an archive
        # was tampered with or truncated, and a "... and N more" tail is the
        # part an attacker would want hidden.
        print(f"restore: SKIPPED {len(result.skipped)} member(s) outside the state allowlist:")
        for name in result.skipped:
            print(f"  - {name}")
    for name in result.restored:
        print(f"restore: {verb} {name}")
    return 0


def _guarded_write_embedder(config: AppConfig, corpus: Path) -> CachedEmbedder:
    """F4 on the write path: refuse BEFORE embedding, or the run writes foreign
    vectors into the existing column and then rewrites the manifest to match,
    after which the read-path guard passes forever over a mixed vector space."""
    embedder = _cached_embedder(config, corpus)
    # #30 P2a: guard the index this write will ACTUALLY land in (the active
    # release once one exists), not always the legacy path.
    try:
        index_dir = resolve_active_index_dir(corpus)
    except (ActiveReleaseMissing, ReleaseCorrupt) as exc:
        raise SystemExit(f"alexandria: {exc}") from exc
    try:
        verify_manifest_for_write(corpus, embedder, config.embed_provider,
                                  VectorStore(index_dir))
    except (ManifestMissing, ManifestMismatch, ManifestCorrupt) as exc:
        raise SystemExit(f"alexandria: {exc}") from exc
    return embedder


def cmd_index(args) -> int:
    """Chunk, embed, and persist the corpus in deterministic batches."""
    config = _config_for(args)
    corpus = config.corpus_path

    # BACKLOG #50: promote_pending is the only other writer of the FTS/vector
    # tables this command drops and rebuilds (--rebuild) or otherwise writes
    # into. `promote_pending` uses non-blocking acquisition while this command
    # waits for the same lock -- so a promote (CLI, or serve's 600s drain)
    # can land its FTS insert after this run's chunk-record snapshot is taken
    # but before --rebuild's lexical.drop() wipes the table, which then never
    # gets that promote's row back (the rebuild only re-adds the pre-drop
    # snapshot). The promote still unlinks its pending marker regardless, so
    # the entry is permanently promoted-but-unsearchable in a corpus with no
    # deletion path. Unlike the drain, an index run that silently skipped or
    # silently raced would be exactly the "reported success while doing
    # nothing" failure this project keeps finding -- so index WAITS for the
    # lock (bounded, DEFAULT_LOCK_TIMEOUT -- see writelock.py for why 30s) and
    # fails loudly, non-zero, naming the holder, rather than skip or race.
    lock = write_lock(corpus)
    if not lock.acquire(blocking=True, timeout=DEFAULT_LOCK_TIMEOUT):
        holder = lock.holder_pid()
        holder_desc = f"pid {holder}" if holder else "an unknown process"
        raise SystemExit(
            f"alexandria: index could not acquire the corpus write lock within "
            f"{DEFAULT_LOCK_TIMEOUT:.0f}s (held by {holder_desc}). Refusing to run "
            f"a partial or racing index -- wait for the current writer (promote, "
            f"or serve's drain) to finish and retry.")
    try:
        return _cmd_index_locked(args, config, corpus)
    finally:
        lock.release()


def _cmd_index_locked(args, config: AppConfig, corpus: Path) -> int:
    """The body of `index`, run while BACKLOG #50's write lock is held."""
    if getattr(args, "list_releases", False):
        releases = list_releases(corpus)
        if not releases:
            print("index: no releases staged yet (corpus uses the legacy layout)")
            return 0
        for r in releases:
            marker = "ACTIVE" if r["active"] else ""
            print(f"{r['release_id']}  {marker}")
        return 0

    if getattr(args, "rollback", False):
        releases = list_releases(corpus)
        active_id = active_release_id(corpus)
        # list_releases returns ids sorted (YYYYMMDDTHHMMSS-<uuid8>), so the
        # most recent NON-active release is simply the last one that is not
        # the current active id.
        prior = [r["release_id"] for r in releases if r["release_id"] != active_id]
        if not prior:
            print("index: no previous release to roll back to", file=sys.stderr)
            return 1
        target = prior[-1]
        activate_release(corpus, target)
        print(f"index: rolled back to release {target}")
        return 0

    if getattr(args, "gc", False):
        from pathlib import Path as _P
        import shutil as _shutil
        releases = list_releases(corpus)
        active_id = active_release_id(corpus)
        keep = set()
        if active_id:
            keep.add(active_id)
            prior = [r["release_id"] for r in releases if r["release_id"] != active_id]
            if prior:
                keep.add(prior[-1])  # the most recent non-active release
        removed = 0
        for r in releases:
            if r["release_id"] not in keep:
                _shutil.rmtree(_P(corpus) / ".alexandria" / "index" / "releases" / r["release_id"])
                removed += 1
        print(f"index: gc removed {removed} release(s); keeping {sorted(keep)}")
        return 0

    if getattr(args, "enrich_invalidate", None):
        # #5/F3d escape hatch: force a specific doc_id's stored enrichment
        # payload to be dropped, independent of content/recipe -- for an
        # operator who has judged a payload bad (e.g. a hostile hypothetical
        # that slipped past the F3c filter) on a document whose content and
        # recipe have not otherwise changed.
        #
        # Red review 2026-08-20 (finding #2): this clears the CACHE, not the
        # already-served poison. Any synthetic rows and the enrichment
        # summary already written into the ACTIVE index release keep serving
        # until a full --rebuild replaces that release -- incremental writes
        # (upsert) only touch chunk_ids present in the new batch, they never
        # delete rows absent from it, so a stale synthetic ::hq1/::hq2/::hq3
        # chunk from the rejected payload is not removed by re-enriching
        # alone. Say so explicitly rather than implying more remediation
        # than actually occurs.
        from .enrich import EnrichmentStore
        index_dir = resolve_active_index_dir(corpus)
        store = EnrichmentStore(index_dir)
        removed = store.invalidate(args.enrich_invalidate)
        if removed:
            print(f"index: invalidated cached enrichment for {args.enrich_invalidate!r}. "
                  "This clears the CACHE only -- any synthetic vectors/summary text "
                  "already written into the active index release keep serving until "
                  "you run `index --enrich --rebuild` to fully purge and replace them.")
            return 0
        print(f"index: {args.enrich_invalidate!r} had no stored enrichment "
              "to invalidate", file=sys.stderr)
        return 1

    if getattr(args, "backfill_manifest", False):
        # A pre-policy manifest cannot establish that every persisted vector
        # crossed CachedEmbedder's L2 boundary. Never relabel non-empty legacy
        # storage as l2 by operator assertion: rebuild is the only safe migration.
        # Empty storage has no vector representation to attest, so it may receive
        # an initial manifest for a new writer.
        try:
            index_dir = resolve_active_index_dir(corpus)
        except (ActiveReleaseMissing, ReleaseCorrupt) as exc:
            raise SystemExit(f"alexandria: {exc}") from exc
        store = VectorStore(index_dir)
        if store.count() != 0:
            raise SystemExit(
                "alexandria: refusing to backfill normalization policy over a non-empty "
                "legacy index; its stored vectors cannot be proven L2-normalized. "
                "Rebuild explicitly: alexandria --corpus "
                f"{corpus} index --rebuild")
        embedder = _cached_embedder(config, corpus)
        manifest = write_manifest(corpus, embedder, config.embed_provider,
                                  index_dir=index_dir if active_release_id(corpus) else None)
        print(f"index: manifest backfilled -> provider={manifest['provider']} "
              f"model={manifest['model']} dim={manifest['dim']} "
              f"normalized={manifest['normalized']} dtype={manifest['dtype']}")
        return 0

    if getattr(args, "backfill_meta", False):
        from .index.chunker import backfill_meta
        from .index.store import VectorStore as _VS
        try:
            index_dir = resolve_active_index_dir(corpus)
        except (ActiveReleaseMissing, ReleaseCorrupt) as exc:
            raise SystemExit(f"alexandria: {exc}") from exc
        store = _VS(index_dir)
        stats = backfill_meta(corpus, store, config)
        gen = write_index_generation(corpus)
        print(f"index: backfilled meta on {stats.chunks} chunks across {stats.docs} docs "
              f"({stats.chunks_updated} updated, 0 embedded); "
              f"generation {gen} (query/response caches invalidated)")
        liveness.record_success(corpus, promoted_count=0, generation=gen)
        return 0

    records, errors = _load_chunk_records(corpus, config, args.limit, args.workers)
    for error in errors:
        print(f"skip: {error}", file=sys.stderr)
    # F4 must run before ANY embedding, and enrichment embeds too (it calls
    # _cached_embedder itself). Guarding only just before the main pipeline
    # made "refuse before embedding" true on the non-enrich path only.
    # --rebuild drops the table first (below), so there is no existing vector
    # space to mix with; guarding it would refuse the very provider switch that
    # --rebuild is the documented way to perform.
    if args.rebuild:
        embedder = _cached_embedder(config, corpus)
    else:
        embedder = _guarded_write_embedder(config, corpus)
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
    if args.rebuild:
        # #30 P2a: build a COMPLETE new release beside whatever is currently
        # active, validate it, then atomically publish one pointer. A crash
        # or failure at any point before activate_release() leaves the OLD
        # release serving, untouched -- see docs/DECISION-staged-releases-p2a.md.
        # This replaces the old drop-in-place behavior (store.drop() +
        # lexical.drop() before refilling), which destroyed the only index
        # copy the instant a rebuild started.
        release_dir = new_release_dir(corpus)
        release_dir.mkdir(parents=True)
        store = VectorStore(release_dir)
        lexical = BM25Index(release_dir / "fts.sqlite")
        # append_only is safe here: release_dir is BRAND NEW and empty by
        # construction (never reused -- new_release_dir() guarantees a fresh
        # path), so every id is new by definition. The duplicate check still
        # runs because a bug in _load_chunk_records producing two rows for
        # one chunk_id is a real, distinct failure append_only would silently
        # multiply, not a property of reusing storage.
        ids = [record["chunk_id"] for record in records]
        if len(ids) != len(set(ids)):
            from collections import Counter
            dupes = [cid for cid, n in Counter(ids).most_common(5) if n > 1]
            raise ValueError(
                "rebuild set contains duplicate chunk_id(s); append would insert "
                f"every copy: {dupes}")
        started = time.monotonic()
        stats = _run_index_pipeline(records, embedder, store, lexical,
                                    batch_size=config.embed_batch_size,
                                    progress_every=config.index_progress_every,
                                    progress_stream=sys.stdout,
                                    write_batch=config.index_write_batch,
                                    append_only=True)
        elapsed = time.monotonic() - started
        print(f"index: {len(records)} chunks from {len({record['doc_id'] for record in records})} documents "
              f"in {elapsed:.2f}s (cache {stats.cache_hits} hit/"
              f"{stats.cache_misses} miss)")
        write_manifest(corpus, embedder, config.embed_provider, index_dir=release_dir)
        checksum_release(release_dir)
        verify_checksums(release_dir)  # fail loudly on our own write, before anyone can read it
        activate_release(corpus, release_dir.name)
        gen = write_index_generation(corpus)
        print(f"index: staged release {release_dir.name} activated; "
              f"corpus generation {gen} (query/response caches invalidated)")
        liveness.record_success(corpus, promoted_count=0, generation=gen)
        return 0

    # Incremental path (no --rebuild): write into whatever is CURRENTLY
    # active -- the legacy layout before any release exists, or the active
    # release once one does. This must resolve the SAME way reads do, or a
    # fact indexed after a staged rebuild would silently land somewhere no
    # reader ever looks.
    try:
        index_dir = resolve_active_index_dir(corpus)
    except (ActiveReleaseMissing, ReleaseCorrupt) as exc:
        raise SystemExit(f"alexandria: {exc}") from exc
    store = VectorStore(index_dir)
    lexical = BM25Index(index_dir / "fts.sqlite")
    started = time.monotonic()
    stats = _run_index_pipeline(records, embedder, store, lexical,
                                batch_size=config.embed_batch_size,
                                progress_every=config.index_progress_every,
                                progress_stream=sys.stdout,
                                write_batch=config.index_write_batch,
                                append_only=False)
    elapsed = time.monotonic() - started
    print(f"index: {len(records)} chunks from {len({record['doc_id'] for record in records})} documents "
          f"in {elapsed:.2f}s (cache {stats.cache_hits} hit/"
          f"{stats.cache_misses} miss)")
    # Red release change 1: bind every cache to this corpus generation so a
    # reindex invalidates stale query/response cache entries.
    gen = write_index_generation(corpus)
    print(f"index: corpus generation {gen} (query/response caches invalidated)")
    write_manifest(corpus, embedder, config.embed_provider, index_dir=index_dir if active_release_id(corpus) else None)
    liveness.record_success(corpus, promoted_count=0, generation=gen)
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
                  hits=len(results), caller=caller_label(args.caller), user=cli_identity(),
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
    # #48: True when this text is a write-stage draft no judge ever saw.
    salvaged: bool = False



# #47: wall-clock ceiling for ONE /answer, shared by every LLM stage in it.
# Sized from measurement, not guesswork: a healthy cold answer is ~3.3 min
# (200s) and the bridge measured 122s typical with >300s under concurrent
# writes, so 900s leaves ample headroom for a slow-but-alive gateway while
# bounding a dead one to minutes instead of the ~1.8h an unbounded chain cost.
DEFAULT_ANSWER_TIMEOUT = 900.0

def run_answer(config: AppConfig, corpus: Path, question: str, *, engine, k: int,
              llm_model: str, grader_a_model: str, grader_b_model: str,
              base_url: str | None, api_key_env: str | None, prompt_version: str,
              save_dir: str | None = None, caller: str | None = None,
              user: str | None = None, max_follow_up_queries: int = 2,
              audit_concurrency: int = 4,
              answer_timeout: float = DEFAULT_ANSWER_TIMEOUT) -> AnswerOutcome:
    """Run the full gather -> write -> judge -> repair pipeline (or replay a
    cached page) for one question against an already-built `engine`. No
    printing; side effects are exactly what the spec requires (response
    cache, audit log, cost ledger) and nothing else."""
    from .cache import ResponseCache
    from .llm import LLMClient
    from .synthesis.pipeline import run_pipeline

    if (not isinstance(max_follow_up_queries, int) or isinstance(max_follow_up_queries, bool)
            or not 0 <= max_follow_up_queries <= MAX_FOLLOW_UP_QUERIES):
        raise ValueError(
            f"max_follow_up_queries must be between 0 and {MAX_FOLLOW_UP_QUERIES}")
    if (not isinstance(audit_concurrency, int) or isinstance(audit_concurrency, bool)
            or not 0 <= audit_concurrency <= MAX_AUDIT_CONCURRENCY):
        raise ValueError(
            f"audit_concurrency must be between 0 and {MAX_AUDIT_CONCURRENCY}")
    answer_id = str(uuid.uuid4())
    response_cache = ResponseCache(corpus)
    generation = read_index_generation(corpus)
    # These settings change either evidence selection (follow-ups) or which
    # page passes the native judges. They are answer semantics, not diagnostics.
    answer_pipeline = answer_pipeline_fingerprint(
        grader_a_model=grader_a_model,
        grader_b_model=grader_b_model,
        base_url=base_url,
        api_key_env=api_key_env,
        retrieval=_answer_retrieval_fingerprint(engine),
        prompt_version=prompt_version,
        max_follow_up_queries=max_follow_up_queries,
        audit_concurrency=audit_concurrency,
    )
    rkey = response_cache.key(question, llm_model, k, prompt_version, generation,
                              pipeline=answer_pipeline)

    # RESPONSE CACHE: a previously-emitted answer for the same question/model
    # config is replayed verbatim (TTL 7d); the pipeline is skipped entirely.
    # No LLM call happens on this path, so no ledger row is written -- there is
    # nothing to cost (SPEC F5).
    cached_page = response_cache.get(rkey)
    if cached_page is not None:
        logger = AuditLogger(corpus)
        # #9, Red review 2026-08-20 (finding #3): "linkage already exists in
        # the original row" was an ASSERTED claim with no way to check it --
        # the original answer may predate this feature (guaranteed for the
        # first 7 days after deploy), its logger.answer() write may have
        # failed, or answers.jsonl may have rotated. Store a REAL back-pointer
        # instead of asserting one: the ORIGINAL answer_id that populated this
        # cache entry, so a future learning loop can look up that row
        # mechanically rather than trusting a comment. Requires cached_page to
        # carry it (see the `put()` call below, which now stores it).
        logger.answer(query=question, total_ms=0, emitted=True,
                      model=llm_model, n_claims=cached_page.get("n_claims", 0),
                      stages={}, caller=caller, user=user,
                      trace={"cache_hit": True,
                            "source_answer_id": cached_page.get("answer_id")},
                      id=answer_id, query_id=None, citations=[])
        return AnswerOutcome(True, cached_page["text"], cached_page.get("n_claims", 0),
                             answer_id, cached=True)

    save_path = Path(save_dir).expanduser() if save_dir else None
    emit_root = save_path if save_path else Path(tempfile.mkdtemp(prefix="alexandria-answer-"))

    # #47: ONE wall-clock budget shared by every stage of this answer. Capping
    # each call individually did not compose -- a single answer chains ~15
    # sequential stages, so a dead gateway still cost ~1.8h even with every call
    # bounded. All three clients draw down the same deadline, and once it is
    # spent the remaining stages fail fast instead of each paying an attempt.
    # Single normalization point for the "<=0 disables" convention, so CLI,
    # serve and direct callers cannot drift into three different meanings of 0.
    deadline = RequestDeadline(
        None if answer_timeout is not None and answer_timeout <= 0 else answer_timeout)
    writer = LLMClient(model=llm_model, base_url=base_url, api_key_env=api_key_env,
                       deadline=deadline)
    grader_a = LLMClient(model=grader_a_model, base_url=base_url, api_key_env=api_key_env,
                         deadline=deadline)
    grader_b = LLMClient(model=grader_b_model, base_url=base_url, api_key_env=api_key_env,
                         deadline=deadline)
    _t_answer0 = time.time()
    result = run_pipeline(
        engine, question,
        gather_llm=writer, writer_llm=writer, repair_llm=writer,
        audit_llm=grader_a, coverage_llm_a=grader_a, coverage_llm_b=grader_b,
        corpus_root=emit_root, seed_k=k, writer_model=llm_model,
        prompt_version=prompt_version,
        max_follow_up_queries=max_follow_up_queries,
        audit_concurrency=audit_concurrency,
    )
    total_ms = int((time.time() - _t_answer0) * 1000)
    logger = AuditLogger(corpus)

    # #48 salvage: the write stage finished but the budget died before any
    # judge saw the draft. This is a DISTINCT outcome, not a variant of the
    # success path: emitted stays FALSE (nothing was verified or emitted to
    # disk) with an explicit salvaged flag, and it is NEVER written to the
    # response cache -- an unaudited draft replayed as a full answer would be
    # worse than no answer. Branch FIRST, before any result.repair access.
    if getattr(result, "budget_exhausted", False) and result.salvaged_page is not None:
        draft = result.salvaged_page
        if writer.last_usage:
            engine.logger.log_usage(query_id=answer_id, model=llm_model, **writer.last_usage)
        logger.answer(query=question, total_ms=total_ms, emitted=False,
                      model=llm_model, n_claims=len(draft.claims),
                      error="budget exhausted: UNAUDITED draft (no judge ran)",
                      stages=getattr(result, "timings_ms", {}),
                      caller=caller, user=user,
                      trace={"salvaged": True}, id=answer_id,
                      # #9: no judge ran, so no claim_verdict exists to record --
                      # citations must stay empty here, never fabricated from an
                      # unaudited draft's raw (unverified) citation claims. The
                      # seed query_id IS known (gather always completes before
                      # write/judge -- see pipeline.py's salvage branch), so it
                      # is still recorded even with zero citations.
                      query_id=getattr(getattr(result, "gathered", None), "seed_query_id", None),
                      citations=[])
        return AnswerOutcome(False, draft.text, len(draft.claims), answer_id,
                             error="budget exhausted: unaudited draft",
                             salvaged=True)

    verdict = getattr(result.repair, "verdict", None)
    failed_ids = list(getattr(verdict, "failed_claim_ids", ()) or ())
    page = getattr(result.repair, "page", None)
    n_claims = len(page.claims) if page else 0
    trace = _answer_trace(result)
    # #9/C1: citations are built even on the NOT-emitted path -- a claim that
    # failed judging is a valuable NEGATIVE signal (requirement 4), and this
    # is the only point where the verdict that judged it is still available.
    # Red review 2026-08-20: query_id/rank/source_round now come from
    # gathered.chunk_provenance, captured synchronously inside gather() at
    # each search call -- never read from ambient engine state after the
    # fact (the original design silently joined citations to the wrong
    # QueryLogger row; see gather.py's ChunkProvenance docstring).
    gathered = getattr(result, "gathered", None)
    citations = _citation_records(gathered, verdict)
    seed_query_id = getattr(gathered, "seed_query_id", None)
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
                      caller=caller, user=user, trace=trace, id=answer_id,
                      query_id=seed_query_id, citations=citations)
        return AnswerOutcome(False, None, n_claims, answer_id,
                             error="synthesis failed its native checks", failed_claims=failed_ids)
    page_text = result.page_path.read_text(encoding="utf-8")
    logger.answer(query=question, total_ms=total_ms, emitted=True,
                  model=llm_model, n_claims=n_claims,
                  stages=getattr(result, "timings_ms", {}),
                  caller=caller, user=user, trace=trace, id=answer_id,
                  query_id=seed_query_id, citations=citations)
    # #9, Red review finding #3: store the real answer_id alongside the cache
    # payload, so a later cache-hit's audit row can carry a genuine
    # back-pointer instead of an asserted-but-unverifiable claim.
    response_cache.put(rkey, {"text": page_text, "n_claims": n_claims, "answer_id": answer_id})
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
        max_follow_up_queries=getattr(args, "max_follow_up_queries", 2),
        audit_concurrency=getattr(args, "audit_concurrency", 4),
        api_key_env=args.api_key_env, prompt_version=args.prompt_version,
        answer_timeout=getattr(args, "answer_timeout", DEFAULT_ANSWER_TIMEOUT),
        save_dir=args.save_dir, caller=caller_label(args.caller), user=cli_identity())
    if outcome.cached:
        print("[cached] " + outcome.text)
        return 0
    if getattr(outcome, "salvaged", False):
        # Distinct, NONZERO status: a caller checking only the exit code must
        # not mistake an unverified draft for a real answer. The draft is still
        # printed -- it may contain useful work -- but the status says "partial".
        print("answer: BUDGET EXHAUSTED -- verification incomplete; draft below "
              "was NOT approved by any judge. Do not treat as verified.",
              file=sys.stderr)
        print(outcome.text)
        return 4
    if not outcome.emitted:
        print("answer: synthesis failed its native checks; no page emitted.",
              file=sys.stderr)
        for claim_id in outcome.failed_claims or ():
            print(f"  failed claim {claim_id}", file=sys.stderr)
        return 1
    print(outcome.text)
    return 0


def _answer_retrieval_fingerprint(engine) -> dict[str, object]:
    """Stable retrieval policy that determines the evidence an answer sees."""
    config = getattr(engine, "config", None)
    reranker = getattr(engine, "reranker", None)
    return {
        "embedder": str(getattr(getattr(engine, "embedder", None), "name", "unknown")),
        "reranker": {
            "model": str(getattr(reranker, "model_name", type(reranker).__name__)),
            "half_precision": getattr(reranker, "half_precision", None),
        },
        "search": {
            "depth": getattr(config, "depth", None),
            "prefetch": getattr(config, "prefetch", None),
            "top_k": getattr(config, "top_k", None),
            "rrf_k": getattr(config, "rrf_k", None),
            "wiki_boost": getattr(config, "wiki_boost", None),
        },
    }


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


def _citation_records(gathered, verdict, *, schema_version: str = "citation-v2") -> list[dict]:
    """#9/C1: durable (query_id, claim_id, doc_id, chunk_id, rank, claim_verdict,
    source_round) tuples -- the actual precondition for the learning loop
    (spec section C1). Built from `judge_page`'s VERDICT (per-claim entailment,
    audit.Verdict.verdict in {supported, unsupported, fabricated}), not a
    was-cited boolean -- a chunk cited for a claim that later fails judging is
    a NEGATIVE relevance signal, and collapsing to a boolean would discard
    exactly what makes this better than click-through data (requirement 4).

    query_id/rank/source_round now come from `gathered.chunk_provenance`
    (gather.py), captured SYNCHRONOUSLY at the moment each search ran -- Red
    review 2026-08-20 found the original design (reading
    SearchEngine.last_query_id from run_answer, AFTER the whole pipeline
    completed) joined most citations to the wrong QueryLogger row, and could
    cross-link a DIFFERENT request's id under serve's shared engine instance.

    A citation whose chunk_id has NO provenance entry (Red finding #4: could
    be a genuine seed_chunk, OR a writer-fabricated chunk_id that never came
    from any search) gets query_id=None, rank=None, source_round="unknown" --
    never silently labeled "seed", which would misattribute a hallucination
    to the retrieval system. schema_version lets a future learning loop
    exclude rows written under an earlier, since-corrected linkage scheme
    (Red finding, blocking change 2).

    Returns [] when there is nothing to link (no gathered pool, no verdict)
    rather than raising: citation linkage is a durable SIGNAL, not a
    correctness gate, and must never be the reason an answer fails to emit."""
    if gathered is None or verdict is None:
        return []
    audit = getattr(verdict, "audit", None)
    verdicts_by_claim_id = {v.note_id: v.verdict for v in (getattr(audit, "verdicts", None) or ())}
    provenance = getattr(gathered, "chunk_provenance", None) or {}

    page = getattr(verdict, "page", None)
    claims = getattr(page, "claims", ()) or ()
    records: list[dict] = []
    for claim in claims:
        claim_verdict = verdicts_by_claim_id.get(claim.id, "ungraded")
        for citation in getattr(claim, "citations", ()) or ():
            prov = provenance.get(citation.chunk_id)
            records.append({
                "schema_version": schema_version,
                "query_id": prov.query_id if prov else None,
                "claim_id": claim.id,
                "doc_id": citation.doc_id,
                "chunk_id": citation.chunk_id,
                "rank": prov.rank if prov else None,
                "claim_verdict": claim_verdict,
                "source_round": prov.source_round if prov else "unknown",
            })
    return records



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
    # Compatibility shim for internal callers/tests. One shared lock module owns
    # the durable marker path so retrieval and mutation cannot drift apart.
    from .writelock import rebuild_marker
    return rebuild_marker(corpus)


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

    # Quality evaluation must observe present retrieval, not replay cached
    # results from before a ranking/config regression. Normal search keeps its
    # query cache; this is an evaluation-only isolation boundary.
    engine = _build_search_engine(config, corpus, query_cache=False)
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


# BACKLOG #8 residual (2026-08-20): the CLI half. serve's identity is already
# structurally verified (socket ownership, §5.2/§5.3) -- what remains is
# `--caller`, which any invocation can set to any string. Removing it (like
# `--user`) is not viable: real, documented, non-forgeable-in-PRACTICE callers
# genuinely depend on it -- scripts/demand-report.py's own methodology treats
# `caller=pi-extension` as the ONE positive-evidence value in the audit log,
# because the pi extension (~/.pi/agent/extensions/alexandria.ts, outside this
# repo) sets ALEXANDRIA_CALLER=pi-extension as a subprocess ENV VAR on every
# CLI-exec fallback call (confirmed live: `env: { ...process.env,
# ALEXANDRIA_CALLER: "pi-extension", ...env }`), which this CLI's --caller
# flag then defaults from (`os.environ.get("ALEXANDRIA_CALLER", "cli")`).
#
# RESIDUAL, documented rather than solved (Red review 2026-08-20, finding #2):
# because the channel is an inherited env var, a human who happens to have
# ALEXANDRIA_CALLER=pi-extension exported in their OWN shell (e.g. copied from
# a debugging session) would have every manual invocation pass through
# unmarked too. This is an ACCIDENTAL misattribution channel, not an
# adversarial one -- a real attacker types --caller pi-extension directly,
# which this fix explicitly does not and cannot stop either (see below). Both
# residuals exist because there is no cryptographic trust boundary on this
# path at all, which is the CLI's structural limit, not a bug in this fix.
#
# The fix cannot be "verify --caller" (there is no trust boundary on this path
# to verify against -- see cli_identity()'s reasoning above, which is why the
# OS-user half took the derived-not-accepted route instead). It CAN make the
# audit trail honest about what it knows: a value from this known set is
# recorded as CLAIMED, unchanged; any OTHER value is prefixed "unverified:".
#
# What this narrows, precisely (Red review finding #3 -- do not overclaim):
# it flags a NOVEL or MUTATED value someone did not bother to spell exactly
# right (a typo, a new unregistered script, an evolving convention). It does
# NOT and cannot catch exact-string forgery -- literally typing
# `--caller pi-extension` by hand still passes through clean, because this
# path has no way to distinguish that from the real extension. "Forged"
# claims about this fix must say "novel/mutated," never "any forged value."
KNOWN_CALLERS = frozenset({"cli", "pi-extension"})
_UNVERIFIED_PREFIX = "unverified:"
assert not any(name.startswith(_UNVERIFIED_PREFIX) for name in KNOWN_CALLERS), (
    "a known caller name must never collide with the unverified marker, or "
    "the labeling below would not be unambiguous")


def caller_label(raw) -> str:
    """Mark a --caller value that is NOT one of the known, documented values
    (see KNOWN_CALLERS above) so it cannot be mistaken for the one identity
    with any actual provenance. "cli" (nothing specified) and "pi-extension"
    (the one external, documented convention) pass through as CLAIMS, not
    proof -- see the module-level comment for exactly what this does and does
    not catch. None/empty/non-string inputs get distinct sentinels rather
    than collapsing to one bare "unverified:" (Red review finding #5): each
    names a different failure mode a log reader might need to tell apart --
    a caller genuinely calling with no value, vs a caller passing an empty
    string, vs a programmatic bug passing the wrong type entirely."""
    if raw is None:
        return f"{_UNVERIFIED_PREFIX}<none>"
    if not isinstance(raw, str):
        return f"{_UNVERIFIED_PREFIX}<non-string:{type(raw).__name__}>"
    if raw == "":
        return f"{_UNVERIFIED_PREFIX}<empty>"
    return raw if raw in KNOWN_CALLERS else f"{_UNVERIFIED_PREFIX}{raw}"


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
    try:
        index_dir = resolve_active_index_dir(corpus)
    except (ActiveReleaseMissing, ReleaseCorrupt) as exc:
        raise SystemExit(f"alexandria: {exc}") from exc
    if (index_dir / "chunks.lance").exists() or (index_dir / "fallback.sqlite").exists():
        return
    raise SystemExit(
        f"alexandria: no index at {index_dir} -- the corpus is missing or was "
        f"never indexed, so every query would return zero results. "
        f"Run: alexandria --corpus {corpus} index"
    )


def _build_search_engine(config: AppConfig, corpus: Path, query_cache: bool = True,
                         corpus_root: Path | None = None, client: str = "cli",
                         embedding_cache_read_only: bool = False) -> SearchEngine:
    # A rebuild writes its durable marker *before* dropping either projection.
    # Check it before `_require_index`: after drop, the old "never indexed"
    # error would otherwise hide a live/crashed writer and invite bad recovery.
    if rebuild_marker(corpus).exists():
        raise IndexReadUnavailable(
            "index rebuild is in progress or was interrupted; retry after a successful rebuild")
    _require_index(corpus)
    # Deliberately NOT holding the shared epoch lock across construction.
    # Construction binds no epoch: `VectorStore.search_vector` re-opens its
    # table per query and BM25 reads a live connection, so coherence is a
    # property of each search's own SH epoch, not of when the engine was built.
    # Locking here only added an outage -- `serve` (and every CLI read) would
    # refuse to start while an ordinary promote/drain held the writer lock for
    # a few seconds, which test_a_lock_skipped_drain_cycle_records_no_liveness_success
    # pins as required behavior. The durable marker above is the construction-time
    # guard, because a rebuild that has already dropped a projection leaves an
    # index no amount of waiting makes readable.
    return _build_search_engine_unlocked(
        config, corpus, query_cache=query_cache, corpus_root=corpus_root,
        client=client, embedding_cache_read_only=embedding_cache_read_only)


def _build_search_engine_unlocked(config: AppConfig, corpus: Path, query_cache: bool = True,
                                  corpus_root: Path | None = None, client: str = "cli",
                                  embedding_cache_read_only: bool = False) -> SearchEngine:
    # §7: every invocation performs the liveness check and prints one line to
    # stderr if stale -- never raises, never blocks results (gate W7).
    live = liveness.check(corpus)
    if live.stale:
        print(f"alexandria: stale -- {live.reason}", file=sys.stderr)
    embedder = _cached_embedder(config, corpus, read_only=embedding_cache_read_only)
    # #30 P2a: resolved ONCE, used for every store construction below, so a
    # concurrent activation cannot land mid-construction and mix legs from
    # two different releases -- the read-side half of "one pointer, one
    # source of truth" (the write side stages a COMPLETE release before this
    # function ever sees it).
    try:
        index_dir = resolve_active_index_dir(corpus)
    except (ActiveReleaseMissing, ReleaseCorrupt) as exc:
        raise SystemExit(f"alexandria: {exc}") from exc
    try:
        # #45: reads are permitted against a pre-policy (unverified_legacy)
        # manifest -- "servable without a forced rebuild" is the whole point
        # of this opt-in. Writes stay exactly as strict as before:
        # verify_manifest_for_write has no such parameter at all (pinned by
        # test_verify_manifest_for_write_never_accepts_the_opt_in). A safe
        # read against an unverified index also needs a scale-invariant
        # search metric (Red review, 2026-08-20): VectorStore now forces
        # cosine distance UNCONDITIONALLY for every LanceDB search, so that
        # half of the fix needs no wiring here at all -- see store.py.
        verify_manifest(corpus, embedder, config.embed_provider, index_dir=index_dir,
                        allow_unverified_legacy=True)
    except (ManifestMissing, ManifestMismatch, ManifestCorrupt) as exc:
        raise SystemExit(f"alexandria: {exc}") from exc
    return SearchEngine(
        embedder,
        VectorStore(index_dir),
        BM25Index(index_dir / "fts.sqlite"),
        CrossEncoderReranker(config.rerank_model),
        SearchConfig(prefetch=config.rerank_prefetch, top_k=config.rerank_top_k,
                     wiki_boost=config.wiki_boost, rrf_k=config.rrf_k),
        QueryLogger(corpus / ".alexandria" / "queries.sqlite"),
        query_cache=QueryCache(corpus) if query_cache else None,
        corpus_root=corpus_root or corpus,
        client=client,
    )


def _cached_embedder(config: AppConfig, corpus: Path, *, read_only: bool = False) -> CachedEmbedder:
    """Build the shared embedding cache; read-only callers never create or mutate it."""
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
                          progress_every=config.index_progress_every, read_only=read_only)


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
    low, high = summary.recall_ci
    print(f"\nrecall@k: {summary.recall_at_k:.1%} [{low:.1%}-{high:.1%}] ({summary.hits}/{scored})  "
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
        verdict = ("significant" if delta.significant
                   else "NOT significant -- within noise at this sample size")
        print(f"\nvs previous: recall {delta.recall_at_k:+.1%}, MRR {delta.mrr:+.3f}")
        print(f"  paired McNemar p={delta.p_value:.3f} "
              f"({len(delta.hit_to_miss)} hit->miss, {len(delta.miss_to_hit)} miss->hit): {verdict}")
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
    s.add_argument("--model", default="deepseek-v4-flash")
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

    delete = sub.add_parser("delete", help="soft-delete a document (or --undelete); --list shows what's deleted")
    delete.add_argument("doc_id", nargs="?", default=None,
                        help="corpus-relative doc id, e.g. sources/pi/note (required unless --list)")
    delete.add_argument("--undelete", action="store_true", help="clear the deleted flag instead of setting it")
    delete.add_argument("--list", action="store_true", help="list every document currently flagged deleted")
    delete.set_defaults(func=cmd_delete)

    erase = sub.add_parser("erase",
                           help="IRREVERSIBLE: scrub a document from git history "
                                "(tombstone + cache purge + history rewrite); "
                                "audit trail and backups are NOT touched -- see "
                                "docs/DECISION-erasure-scope-q1.md")
    erase.add_argument("doc_id", help="corpus-relative doc id, e.g. sources/pi/note")
    erase.add_argument("--yes", action="store_true",
                       help="confirm the irreversible history rewrite (otherwise "
                            "this prints the blast radius and exits 3 without "
                            "touching anything)")
    erase.set_defaults(func=cmd_erase)

    backup = sub.add_parser("backup", help="back up .alexandria state (never the rebuildable indexes)")
    backup.add_argument("dest", help="path to write the .tar.gz archive")
    backup.set_defaults(func=cmd_backup)

    restore = sub.add_parser("restore", help="restore .alexandria state from a backup archive")
    restore.add_argument("archive", help="path to a backup_state() .tar.gz archive")
    restore.add_argument("--dry-run", action="store_true", help="list what would be restored, write nothing")
    restore.set_defaults(func=cmd_restore)

    ingest = sub.add_parser("ingest",
                            help="store a PDF/image artifact and index its extracted text")
    ingest.add_argument("paths", nargs="+",
                        help="file(s), directory, or glob to ingest (or, with "
                             "--refresh, asset path(s) to re-extract)")
    ingest.add_argument("--refresh", action="store_true",
                        help="update the companion for an ALREADY-ingested "
                             "asset (#54); paths name assets, not source "
                             "files. Opt-in only -- the default ingest path "
                             "never rewrites a known memory.")
    ingest.add_argument("--re-extract", action="store_true",
                        help="with --refresh, DESTRUCTIVELY re-run the "
                             "extractor and rewrite the companion BODY "
                             "(operator edits are lost). Without this flag, "
                             "--refresh is metadata-only: it backfills "
                             "provenance fields like page count and preserves "
                             "the body verbatim.")
    ingest.set_defaults(func=cmd_ingest)

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
    index.add_argument("--backfill-meta", action="store_true",
                       help="annotate already-indexed chunks with meta (page anchors, "
                            "asset pointers) by re-running the chunker and updating only "
                            "the meta column; never re-embeds (#52)")
    index.add_argument("--list-releases", action="store_true",
                       help="list every staged release and which is active (#30 P2a)")
    index.add_argument("--rollback", action="store_true",
                       help="repoint the active pointer to the PREVIOUS release "
                            "(no file copy; the previous release is always retained) "
                            "(#30 P2a)")
    index.add_argument("--gc", action="store_true",
                       help="delete all but the active + immediately previous "
                            "release; never deletes active or previous (#30 P2a)")
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
    index.add_argument("--enrich-invalidate", default=None, metavar="DOC_ID",
                       help="force-drop DOC_ID's stored enrichment payload (#5/F3d); "
                            "the next --enrich run re-calls the LLM for it instead of "
                            "reattaching a payload judged bad since it was accepted")
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
    answer.add_argument("--llm-model", default="deepseek-v4-pro",
                        help="gather/write/repair model (the measurement-proven config)")
    answer.add_argument("--grader-a-model", default="deepseek-v4-flash")
    answer.add_argument("--grader-b-model", default="deepseek-v4-flash")
    answer.add_argument("--max-follow-up-queries", type=_follow_up_query_count, default=2,
                        help=f"follow-up cap, 0..{MAX_FOLLOW_UP_QUERIES} (0 disables follow-ups)")
    answer.add_argument("--audit-concurrency", type=_audit_concurrency_count, default=4,
                        help=f"grader workers, 0..{MAX_AUDIT_CONCURRENCY} (0 or 1 = sequential)")
    answer.add_argument("--answer-timeout", type=float, default=DEFAULT_ANSWER_TIMEOUT,
                        help=f"wall-clock budget in seconds for ALL LLM stages of one answer "
                             f"(default {DEFAULT_ANSWER_TIMEOUT:.0f}; 0 disables the budget). "
                             f"Bounds a dead/stalled gateway; a healthy cold answer is ~200s.")
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


# Deliberate, actionable exit code for "the index is momentarily unreadable"
# -- distinct from 1 (the command ran and failed) and 2 (bad input/unusable
# golden set), so a caller can retry rather than treat it as a hard error.
EXIT_INDEX_UNAVAILABLE = 3


def app(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except IndexReadUnavailable as exc:
        # Engine CONSTRUCTION reads the index too, so this must be caught for
        # the whole command, not just around the query call: catching narrowly
        # left `search`/`answer` raising an uncaught RuntimeError (exit 1 plus a
        # traceback) whenever a rebuild was already running at startup. One
        # boundary here also covers every future read verb by default.
        print(f"alexandria: index unavailable -- {exc}", file=sys.stderr)
        return EXIT_INDEX_UNAVAILABLE


if __name__ == "__main__":
    sys.exit(app())
