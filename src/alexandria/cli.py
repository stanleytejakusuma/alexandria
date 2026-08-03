"""Command line entry point.

argparse rather than a CLI framework: the surface is small, and stdlib means one
fewer dependency for a tool whose whole pitch is that your data outlives the engine.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from .config import AppConfig, load_config
from .corpus import Doc
from .connectors.pi_sessions import PiSessionsConnector
from .index.bm25 import BM25Index
from .index.chunker import chunk_document
from .index.embedder import CachedEmbedder, HashEmbedder, LocalEmbedder, MLXEmbedder
from .index.store import VectorStore
from .llm import LLMClient
from .migrate import migrate_kg_sync
from .monitor import QueryLogger
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
        if "_unparsed" in rel.parts or ".alexandria" in rel.parts:
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


def cmd_sync(args) -> int:

    if args.connector != "pi-sessions":
        print(f"unknown connector: {args.connector}", file=sys.stderr)
        return 2

    corpus = _config_for(args).corpus_path
    conn = PiSessionsConnector(
        sessions_dir=args.sessions_dir,
        state_dir=corpus / ".alexandria" / "state",
        llm=LLMClient(base_url=args.base_url, model=args.model),
    )
    items = conn.discover()
    if args.limit:
        items = items[: args.limit]
    print(f"discovered {len(items)} burst(s); {len(conn.skip_log())} skipped")
    if args.dry_run:
        for item in items[:20]:
            print(f"  {item.source_id}  {len(item.content):>7,}ch  "
                  f"part {item.meta['part']}/{item.meta['parts']}")
        return 0

    # Distillation is network-bound, so a small pool turns hours into minutes.
    # normalize() is pure (call + parse); writes and state stay on the main thread
    # because StateStore is not thread-safe and correctness beats a few more workers.
    written = done = failed = 0
    total = len(items)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(conn.normalize, item): item for item in items}
        for future in as_completed(futures):
            item = futures[future]
            done += 1
            try:
                docs = future.result()
            except Exception as exc:                     # never lose the batch to one item
                conn.errors.append(f"{item.source_id}: {type(exc).__name__}: {exc}")
                docs = []
            for doc in docs:
                doc.write(corpus)
                written += 1
            if docs:
                conn.commit([item])     # fail-safe: a failed burst stays unconsumed
            else:
                failed += 1
            if done % 10 == 0 or done == total:
                rate = done / max(time.time() - t0, 1e-6)
                eta = (total - done) / rate if rate else 0
                print(f"  {done}/{total}  notes={written}  empty/failed={failed}  "
                      f"{rate*60:.1f}/min  eta={eta/60:.1f}m", flush=True)

    print(f"wrote {written} note(s) from {total} burst(s); {failed} produced none")
    for err in conn.errors[:10]:
        print(f"  error: {err}", file=sys.stderr)
    if len(conn.errors) > 10:
        print(f"  ... and {len(conn.errors)-10} more", file=sys.stderr)
    return 0


def cmd_index(args) -> int:
    """Chunk, embed, and persist the corpus in deterministic batches."""
    config = _config_for(args)
    corpus = config.corpus_path
    records, errors = _load_chunk_records(corpus, config, args.limit, args.workers)
    for error in errors:
        print(f"skip: {error}", file=sys.stderr)
    store = VectorStore(corpus / ".alexandria" / "index")
    lexical = BM25Index(corpus / ".alexandria" / "index" / "fts.sqlite")
    if args.rebuild:
        store.drop()
        lexical.drop()
    embedder = _cached_embedder(config, corpus)
    started = time.monotonic()
    stats = _run_index_pipeline(records, embedder, store, lexical,
                                batch_size=config.embed_batch_size,
                                progress_every=config.index_progress_every,
                                progress_stream=sys.stdout)
    elapsed = time.monotonic() - started
    print(f"index: {len(records)} chunks from {len({record['doc_id'] for record in records})} documents "
          f"in {elapsed:.2f}s (cache {stats.cache_hits} hit/"
          f"{stats.cache_misses} miss)")
    return 0


@dataclass(frozen=True)
class IndexStats:
    indexed: int = 0
    cache_hits: int = 0
    cache_misses: int = 0


def _run_index_pipeline(records: list[dict], embedder, store, lexical, *, batch_size: int,
                        progress_every: int, progress_stream) -> IndexStats:
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
                vectors = embedder.embed([record["text"] for record in batch])
                cache_stats = dict(embedder.last_cache_stats)   # snapshot NOW, not later
                indexed_records = [record | {"vector": vector}
                                   for record, vector in zip(batch, vectors, strict=True)]
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
    while True:
        item = work.get()
        if item is SENTINEL:
            break
        indexed_records, cache_stats = item
        store.upsert(indexed_records)          # I/O overlaps the NEXT batch's embed()
        lexical.index(indexed_records)
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
    return IndexStats(indexed=indexed, cache_hits=cache_hits, cache_misses=cache_misses)


def cmd_search(args) -> int:
    config = _config_for(args)
    corpus = config.corpus_path
    embedder = _cached_embedder(config, corpus)
    engine = SearchEngine(
        embedder,
        VectorStore(corpus / ".alexandria" / "index"),
        BM25Index(corpus / ".alexandria" / "index" / "fts.sqlite"),
        CrossEncoderReranker(config.rerank_model),
        SearchConfig(prefetch=config.rerank_prefetch, top_k=config.rerank_top_k,
                     wiki_boost=config.wiki_boost, rrf_k=config.rrf_k),
        QueryLogger(corpus / ".alexandria" / "queries.sqlite"),
    )
    filters = {field: value for field, value in {
        "type": args.type, "project": args.project, "layer": args.layer,
    }.items() if value is not None}
    results = engine.search(args.query, k=args.k, filters=filters)
    for result in results:
        print(f"{result.rank}. {result.chunk_id}  score={result.score:.6f}\n"
              f"   {result.heading_path}\n   {result.text[:400].replace(chr(10), ' ')}")
    if args.trace:
        print(json.dumps(engine.last_trace, indent=2, sort_keys=True))
    return 0


def _config_for(args) -> AppConfig:
    return load_config(corpus_override=getattr(args, "corpus", None))


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
        relative = path.relative_to(corpus)
        if not relative.parts or relative.parts[0] not in {"sources", "wiki"}:
            continue
        if ".alexandria" not in relative.parts and "_unparsed" not in relative.parts:
            paths.append(path)
    if limit:
        paths = paths[:limit]
    records: list[dict] = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for chunk_records, error in pool.map(lambda path: _chunk_path(path, corpus, config), paths):
            records.extend(chunk_records)
            if error:
                errors.append(error)
    return records, errors


def _chunk_path(path: Path, corpus: Path, config: AppConfig) -> tuple[list[dict], str | None]:
    try:
        document = Doc.read(path, root=corpus)
        markdown = path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        return [], f"{path.relative_to(corpus)}: {exc}"
    metadata = _chunk_metadata(document.frontmatter, document.doc_id)
    chunks = chunk_document(document.doc_id, markdown, config.chunk_tokens, config.chunk_overlap)
    return [{
        "chunk_id": chunk.chunk_id,
        "doc_id": chunk.doc_id,
        "text": chunk.text,
        "heading_path": chunk.heading_path,
        **metadata,
    } for chunk in chunks], None


def _chunk_metadata(frontmatter: dict, doc_id: str) -> dict:
    generated = frontmatter.get("generated")
    generated_at = frontmatter.get("generated_at")
    if generated_at is None and isinstance(generated, dict):
        generated_at = generated.get("at")
    return {
        "type": frontmatter.get("type"),
        "project": frontmatter.get("project"),
        "status": frontmatter.get("status"),
        "source": frontmatter.get("source"),
        "tags": list(frontmatter.get("tags") or []),
        "entities": list(frontmatter.get("entities") or []),
        "layer": "wiki" if doc_id.startswith("wiki/") else "sources",
        "generated_at": generated_at,
    }


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
    s.add_argument("--base-url", default="http://127.0.0.1:20128/v1")
    s.add_argument("--model", default="claude-haiku-4-5")
    s.add_argument("--limit", type=int, default=0)
    s.add_argument("--workers", type=int, default=6)
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_sync)

    lint = sub.add_parser("lint", help="validate every document against the schema")
    lint.set_defaults(func=cmd_lint)

    index = sub.add_parser("index", help="chunk, embed, and index the corpus")
    index.add_argument("--rebuild", action="store_true", help="recreate index tables (retain embedding cache)")
    index.add_argument("--limit", type=int, default=0, help="maximum documents to index")
    index.add_argument("--workers", type=int, default=1, help="parallel document chunking workers")
    index.set_defaults(func=cmd_index)

    search = sub.add_parser("search", help="hybrid retrieval over indexed chunks")
    search.add_argument("query")
    search.add_argument("--k", type=int, default=None)
    search.add_argument("--type")
    search.add_argument("--project")
    search.add_argument("--layer", choices=["sources", "wiki"])
    search.add_argument("--trace", action="store_true")
    search.set_defaults(func=cmd_search)

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
