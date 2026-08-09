"""Command line entry point.

argparse rather than a CLI framework: the surface is small, and stdlib means one
fewer dependency for a tool whose whole pitch is that your data outlives the engine.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from .auditlog import AuditLogger, audit_summary
from .cache import QueryCache, ResponseCache
from .config import AppConfig, load_config
from .corpus import Doc
from .connectors.inbox import INBOX_META_RE, InboxConnector, parse_inbox_file
from .connectors.journal import JournalConnector
from .connectors.md_memory import SEPARATOR, MarkdownMemoryConnector
from .connectors.pi_sessions import PiSessionsConnector
from .eval.golden import load_golden, verify_targets
from .eval.history import append_run, compare, load_runs, regressions
from .eval.metrics import by_overlap_band
from .eval.runner import EvalReport, run_eval
from .index.bm25 import BM25Index, searchable_text
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
    logger.sync(connector=conn.name, duration_ms=int((time.time() - t0) * 1000),
                discovered=total, normalized=total - failed,
                committed=written, skipped=len(skipped), errors=conn.errors[:20])
    return 0


def cmd_remember(args) -> int:
    """Append a user-confirmed memory to the inbox (the only explicit write
    surface; promoted into sources/ by `sync inbox`)."""
    corpus = _config_for(args).corpus_path
    inbox_dir = corpus / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    today = _dt.date.today().isoformat()
    path = inbox_dir / f"{today}.md"

    text = args.text.strip()
    if not text:
        print("remember: empty text", file=sys.stderr)
        return 2
    existing = parse_inbox_file(path) if path.exists() else []
    if any(e.text == text for e in existing):
        print("already in inbox; nothing appended")
        return 0

    meta = f"created={today}, last={today}"
    if args.from_:
        meta += f", from={args.from_}"
    if args.session:
        meta += f", session={args.session}"
    if args.corrects:
        meta += f", corrects={args.corrects}"
    entry = f"{text}\n\n<!-- {meta} -->"
    with path.open("a", encoding="utf-8") as fh:
        if path.stat().st_size > 0:
            fh.write(f"\n{SEPARATOR}\n")
        fh.write(entry + "\n")
    print(f"remembered -> inbox/{path.name}")
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
                # Embed the heading breadcrumb alongside the body, matching what
                # BM25 indexes. Without it a chunk's structural context ("Payments
                # service > Retry behaviour") is invisible to BOTH retrievers.
                vectors = embedder.embed([searchable_text(record) for record in batch])
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
    engine = _build_search_engine(config, corpus)
    filters = {field: value for field, value in {
        "type": args.type, "project": args.project, "layer": args.layer,
    }.items() if value is not None}
    _t0 = time.time()
    results = engine.search(args.query, k=args.k, filters=filters)
    from .auditlog import AuditLogger
    logger = AuditLogger(corpus)
    logger.search(query=args.query, k=args.k,
                  latency_ms=int((time.time() - _t0) * 1000),
                  hits=len(results), caller=args.caller, user=args.user,
                  cache_hit=engine.last_cache_hit)
    for result in results:
        print(f"{result.rank}. {result.chunk_id}  score={result.score:.6f}\n"
              f"   {result.heading_path}\n   {result.text[:400].replace(chr(10), ' ')}")
    if args.trace:
        print(json.dumps(engine.last_trace, indent=2, sort_keys=True))
    return 0


def cmd_answer(args) -> int:
    """Synthesize a cited answer page for a question (phase-4 answer endpoint).

    Runs the full gather -> write -> judge -> repair pipeline over the indexed
    corpus and prints the emitted page. The page is written to --save-dir (or a
    temp dir) -- never into the private corpus wiki implicitly.
    """
    from .cache import ResponseCache
    from .llm import LLMClient
    from .synthesis.pipeline import run_pipeline

    config = _config_for(args)
    corpus = config.corpus_path
    engine = _build_search_engine(config, corpus)
    response_cache = ResponseCache(corpus)
    rkey = response_cache.key(args.question, args.llm_model, args.k,
                              args.prompt_version)

    # RESPONSE CACHE: a previously-emitted answer for the same question/model
    # config is replayed verbatim (TTL 7d); the pipeline is skipped entirely.
    cached_page = response_cache.get(rkey)
    if cached_page is not None:
        logger = AuditLogger(config.corpus_path)
        logger.answer(query=args.question, total_ms=0, emitted=True,
                      model=args.llm_model, n_claims=cached_page.get("n_claims", 0),
                      stages={}, caller=args.caller, user=args.user,
                      trace={"cache_hit": True})
        print("[cached] " + cached_page["text"])
        return 0

    save_dir = Path(args.save_dir).expanduser() if args.save_dir else None
    emit_root = save_dir if save_dir else Path(tempfile.mkdtemp(prefix="alexandria-answer-"))

    writer = LLMClient(model=args.llm_model, base_url=args.base_url, api_key_env=args.api_key_env)
    grader_a = LLMClient(model=args.grader_a_model, base_url=args.base_url, api_key_env=args.api_key_env)
    grader_b = LLMClient(model=args.grader_b_model, base_url=args.base_url, api_key_env=args.api_key_env)
    _t_answer0 = time.time()
    result = run_pipeline(
        engine,
        args.question,
        gather_llm=writer,
        writer_llm=writer,
        repair_llm=writer,
        audit_llm=grader_a,
        coverage_llm_a=grader_a,
        coverage_llm_b=grader_b,
        corpus_root=emit_root,
        seed_k=args.k,
        writer_model=args.llm_model,
        prompt_version=args.prompt_version,
    )
    total_ms = int((time.time() - _t_answer0) * 1000)
    logger = AuditLogger(config.corpus_path)
    verdict = getattr(result.repair, "verdict", None)
    failed_ids = getattr(verdict, "failed_claim_ids", ()) or ()
    page = getattr(result.repair, "page", None)
    n_claims = len(page.claims) if page else 0
    trace = _answer_trace(result)
    if not result.emitted:
        logger.answer(query=args.question, total_ms=total_ms, emitted=False,
                      model=args.llm_model, n_claims=n_claims,
                      failed_claims=list(failed_ids),
                      error="synthesis failed its native checks",
                      stages=getattr(result, "timings_ms", {}),
                      caller=args.caller, user=args.user, trace=trace)
        print("answer: synthesis failed its native checks; no page emitted.",
              file=sys.stderr)
        for claim in (page.claims if page else []):
            if claim.id in failed_ids:
                print(f"  failed claim {claim.id}: {claim.text[:200]}", file=sys.stderr)
        return 1
    page_text = result.page_path.read_text(encoding="utf-8")
    logger.answer(query=args.question, total_ms=total_ms, emitted=True,
                  model=args.llm_model, n_claims=n_claims,
                  stages=getattr(result, "timings_ms", {}),
                  caller=args.caller, user=args.user, trace=trace)
    response_cache.put(rkey, {"text": page_text, "n_claims": n_claims})
    print(page_text)
    if not save_dir:
        shutil.rmtree(emit_root, ignore_errors=True)
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


def cmd_eval(args) -> int:
    """Measure current retrieval against the private golden set without changing it."""
    if args.k is not None and args.k < 0:
        print("eval: --k must be non-negative", file=sys.stderr)
        return 2
    config = _config_for(args)
    corpus = config.corpus_path
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

    report = run_eval(_build_search_engine(config, corpus), entries, k_override=args.k)
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
    from .cache import QueryCache, ResponseCache
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


def _config_for(args) -> AppConfig:
    return load_config(corpus_override=getattr(args, "corpus", None))


def _build_search_engine(config: AppConfig, corpus: Path, query_cache: bool = True) -> SearchEngine:
    return SearchEngine(
        _cached_embedder(config, corpus),
        VectorStore(corpus / ".alexandria" / "index"),
        BM25Index(corpus / ".alexandria" / "index" / "fts.sqlite"),
        CrossEncoderReranker(config.rerank_model),
        SearchConfig(prefetch=config.rerank_prefetch, top_k=config.rerank_top_k,
                     wiki_boost=config.wiki_boost, rrf_k=config.rrf_k),
        QueryLogger(corpus / ".alexandria" / "queries.sqlite"),
        query_cache=QueryCache(corpus) if query_cache else None,
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
    if delta is not None:
        print(f"\nvs previous: recall {delta.recall_at_k:+.1%}, MRR {delta.mrr:+.3f}")
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
    s.add_argument("--journal-path",
                   default=str(Path.home() / "citadel/personal-finance/accountability.md"))
    s.add_argument("--base-url", default="http://127.0.0.1:20128/v1")
    s.add_argument("--model", default="claude-haiku-4-5")
    s.add_argument("--limit", type=int, default=0)
    s.add_argument("--workers", type=int, default=6)
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--caller", default=os.environ.get("ALEXANDRIA_CALLER", "cli"),
                   help="consumer identity recorded in the audit trail")
    s.add_argument("--user", default=os.environ.get("ALEXANDRIA_USER", "local"))
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
    search.add_argument("--caller", default=os.environ.get("ALEXANDRIA_CALLER", "cli"),
                       help="consumer identity recorded in the audit trail")
    search.add_argument("--user", default=os.environ.get("ALEXANDRIA_USER", "local"))
    search.set_defaults(func=cmd_search)

    evaluate = sub.add_parser("eval", help="score retrieval against the private golden set")
    evaluate.add_argument("--golden", help="path to the private golden JSONL file")
    evaluate.add_argument("--k", type=int, help="override every entry's retrieval depth")
    evaluate.add_argument("--json", action="store_true", help="emit a machine-readable report")
    evaluate.add_argument("--compare-last", action="store_true", help="show transitions from the prior run")
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
                       help="consumer identity recorded in the audit trail")
    answer.add_argument("--user", default=os.environ.get("ALEXANDRIA_USER", "local"))
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
