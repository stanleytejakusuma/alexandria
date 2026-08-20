"""Heading-aware chunking.

Three decisions worth stating, because each has a common wrong answer:

1. **Tokens, not words.** A `words = tokens * 0.75` fudge is off by a lot on code,
   markdown tables and CJK -- exactly the content this corpus is full of. We count
   real tokens, falling back to a conservative estimator only if no tokenizer is
   installed.
2. **Heading path travels with the chunk.** "Payments service > Retry behaviour" is
   free structural provenance: it improves embedding quality, gives the reranker
   context, and renders directly into a citation. Losing it and trying to recover the
   parent later is strictly harder.
3. **Split on paragraphs, never mid-sentence.** A chunk that begins halfway through a
   clause embeds badly and reads worse when cited.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from ..corpus import Doc

if TYPE_CHECKING:
    from ..config import AppConfig

__all__ = ["Chunk", "Section", "chunk_document", "chunk_doc_records",
           "doc_frontmatter_metadata", "count_tokens", "split_headings"]

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
FENCE_RE = re.compile(r"^\s*(?:```|~~~)")


@dataclass
class Section:
    heading_path: str
    text: str


@dataclass
class Chunk:
    doc_id: str
    chunk_id: str
    text: str
    heading_path: str = ""          # primary breadcrumb (first section in the chunk)
    heading_paths: list[str] = field(default_factory=list)   # every section covered
    ordinal: int = 0
    meta: dict = field(default_factory=dict)


@lru_cache(maxsize=1)
def _encoder():
    try:                                   # real tokenizer when available
        import tiktoken
        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


def count_tokens(text: str) -> int:
    """Token count, or a deliberately conservative estimate when no tokenizer exists.

    The estimate over-counts rather than under-counts: overshooting the budget
    truncates content at embed time, which loses data silently.
    """
    if not text:
        return 0
    enc = _encoder()
    if enc is not None:
        return len(enc.encode(text))
    words = len(text.split())
    return max(words, len(text) // 3)      # ~3 chars/token on dense technical text


def split_headings(markdown: str) -> list[Section]:
    """Split into sections, each tagged with its full heading breadcrumb.

    Headings inside fenced code blocks are text, not structure -- a `# comment` in a
    shell snippet must not open a section.
    """
    body = FRONTMATTER_RE.sub("", markdown)
    sections: list[Section] = []
    stack: list[str] = []
    buf: list[str] = []
    current = ""
    in_fence = False

    def flush():
        text = "".join(buf)
        if text.strip() or "\f" in text:
            # A whitespace-only buffer that contains a form feed is a page
            # break that splitlines detached from the surrounding content
            # (e.g. "\f" alone on a line, or glued to a heading line as
            # "\f# Chapter 2"). Dropping it would silently lose the page for
            # everything after it -- the #52 annotation's whole reason to
            # exist. The section carries no units (chunk_document counts the
            # feed via its dropped-token branch), so chunk text and ids are
            # unaffected.
            sections.append(Section(current, text))
        buf.clear()

    for line in body.splitlines(keepends=True):
        bare = line.rstrip("\n")
        if FENCE_RE.match(bare):
            in_fence = not in_fence
        m = None if in_fence else HEADING_RE.match(bare)
        if m:
            flush()
            depth = len(m.group(1))
            stack[:] = stack[: depth - 1]
            while len(stack) < depth - 1:
                stack.append("")
            stack.append(m.group(2).strip())
            current = " > ".join(p for p in stack if p)
        else:
            buf.append(line)
    flush()
    return sections


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def _hard_split(paragraph: str, max_tokens: int) -> list[str]:
    """Break one oversized paragraph on word boundaries. A 5k-token wall of text must
    still be indexed, so this never drops the remainder."""
    words = paragraph.split()
    if not words:
        return []
    out, cur = [], []
    for w in words:
        # A single "word" can exceed the whole budget -- base64 blobs, hashes and CJK
        # runs tokenize at many tokens per whitespace-delimited unit. Falling through
        # would hand the embedder an over-length chunk to truncate silently.
        if count_tokens(w) > max_tokens:
            if cur:
                out.append(" ".join(cur))
                cur = []
            out.extend(_split_long_token(w, max_tokens))
            continue
        cur.append(w)
        if count_tokens(" ".join(cur)) >= max_tokens:
            out.append(" ".join(cur))
            cur = []
    if cur:
        out.append(" ".join(cur))
    return out


def _split_long_token(token: str, max_tokens: int) -> list[str]:
    """Character-level split of one unbreakable token. Last resort, never lossy."""
    approx = max(1, len(token) * max_tokens // max(count_tokens(token), 1))
    return [token[i:i + approx] for i in range(0, len(token), approx)]


def _raw_paragraphs(text: str) -> list[tuple[str, str]]:
    r"""Split on blank lines keeping the separators, WITHOUT stripping.

    Returns (token, separator) pairs where separator is the whitespace run
    BETWEEN tokens (possibly empty for the first token). Two properties make
    this the single source of truth for both paragraphing and page counting:

    1. TOKEN EQUIVALENCE: the tokens are exactly `_paragraphs`' inputs --
       the same `re.split(r"\n\s*\n")` boundary. The page annotation must
       never change chunk text or ids, so the split that produces units must
       stay byte-identical to the pre-#52 splitter (CRLF, unicode whitespace
       and odd whitespace runs included). Only the SEPARATORS are new.
    2. FEED CONSERVATION: every \f in the text is either inside a token
       (counted by the caller via the raw token) or inside a separator
       (counted here). A standalone feed between blank lines is eaten by
       `\s*` in the separator, never by a strip() -- so the page cursor
       never loses a page break the way _paragraphs()' empty-filter did.
    """
    parts = re.split(r"(\n\s*\n)", text)
    pairs: list[tuple[str, str]] = []
    for i in range(0, len(parts), 2):
        token = parts[i]
        separator = parts[i + 1] if i + 1 < len(parts) else ""
        if token or separator:
            pairs.append((token, separator))
    return pairs


def _hard_split_with_ff(paragraph: str, max_tokens: int) -> list[tuple[str, int]]:
    """Same pieces as _hard_split, each tagged with the form feeds in the
    whitespace BEFORE its first word.

    _hard_split works on paragraph.split() words, and str.split() treats \f
    as whitespace -- so a form feed inside an oversized paragraph vanishes
    from the pieces. The page cursor would then never advance past it. The
    raw count is recovered from the whitespace-run stream aligned to the same
    word sequence (re.split with a capture group keeps the separators):
    ff_before[i] = form feeds in whitespace strictly before word i, so a
    piece starting at word `start` begins on page
        (paragraph base) + ff_before[start] + 1.
    The piece boundaries replicate _hard_split EXACTLY (including
    _split_long_token for unbreakable over-budget words); only the tagging is
    added, so chunk text and ids are byte-identical to a non-annotated run.
    """
    words = paragraph.split()
    if not words:
        return []
    ff_before = [0] * (len(words) + 1)
    running = 0
    wi = 0
    for part in re.split(r"(\s+)", paragraph):
        if part and not part.isspace():
            ff_before[wi] = running
            wi += 1
        else:
            running += part.count("\f")
    ff_before[len(words)] = running

    out: list[tuple[str, int]] = []
    cur: list[str] = []
    cur_start = 0
    for idx, w in enumerate(words):
        if count_tokens(w) > max_tokens:
            if cur:
                out.append((" ".join(cur), ff_before[cur_start]))
                cur = []
            for sub in _split_long_token(w, max_tokens):
                # A form feed can never sit INSIDE a word (str.split dropped
                # it), so sub-tokens carry no feeds of their own; their page
                # offset is the feeds before the word itself.
                out.append((sub, ff_before[idx]))
            cur_start = idx + 1
        else:
            cur.append(w)
            if count_tokens(" ".join(cur)) >= max_tokens:
                out.append((" ".join(cur), ff_before[cur_start]))
                cur = []
                cur_start = idx + 1
    if cur:
        out.append((" ".join(cur), ff_before[cur_start]))
    return out




def chunk_document(doc_id: str, markdown: str, max_tokens: int = 512,
                   overlap: float = 0.15) -> list[Chunk]:
    """Chunk one document. Chunk ids are content-derived, so a reindex of unchanged
    content produces identical ids and the embedding cache stays warm."""
    chunks: list[Chunk] = []
    if not markdown or not markdown.strip():
        return chunks

    overlap_tokens = int(max_tokens * max(0.0, min(overlap, 0.5)))

    # Sections are PACKED, not one-chunk-each. Real notes carry many short sections
    # (bullet lists, one-line headings); emitting a chunk per section produced a median
    # of 15 tokens on this corpus -- fragments too small to embed meaningfully, and a
    # 160k-chunk index for 23k documents. A heading boundary is a good place to split
    # when the budget is full, not a reason to split when it is nearly empty.
    cur: list[str] = []
    cur_path = ""
    cur_paths: list[str] = []
    # #52 page annotation. `page` is an annotation computed from source
    # offsets, never an input to segmentation: the cursor advances by counting
    # form feeds in the RAW text (before stripping/splitting can drop them)
    # while the unit stream -- and therefore every chunk_id -- stays
    # byte-identical with and without the annotation.
    has_pages = "\f" in markdown
    ff_seen = 0
    cur_pages: list[int] = []

    def flush() -> None:
        nonlocal cur, cur_paths, cur_pages
        if cur:
            chunks.append(_make(doc_id, cur_path, cur, len(chunks), cur_paths,
                                page=cur_pages[0] if has_pages else None))
            cur, cur_paths, cur_pages = [], [], []

    for section in split_headings(markdown):
        # A separator belongs to the token AFTER it: the feeds it carries sit
        # between this token and the next, so they advance the page before the
        # NEXT token's units -- never before this one's. `pending` carries the
        # previous separator across iterations (and out of the loop, so a
        # trailing separator still advances the page for anything that follows
        # the section).
        pending_ff = 0
        for raw_para, sep in _raw_paragraphs(section.text):
            ff_seen += pending_ff
            pending_ff = sep.count("\f")
            para = raw_para.strip()
            if not para:
                # A dropped paragraph still advances the page: its form feeds
                # are real page breaks even though nothing is indexed from it.
                ff_seen += raw_para.count("\f")
                continue
            para_ff = raw_para.count("\f")
            para_base = ff_seen
            if count_tokens(para) > max_tokens:
                # Feed the RAW paragraph, not the stripped one: str.strip()
                # removes edge form feeds, and split_headings' splitlines()
                # already consumed every line-boundary feed, so what is left
                # sits at paragraph edges -- exactly what stripping would drop.
                # The word stream is identical either way (split() treats \f
                # as whitespace), so chunk text and ids stay byte-identical.
                units = _hard_split_with_ff(raw_para, max_tokens)  # (unit, ff-before)
            else:
                leading = re.match(r"\s*", raw_para).group().count("\f")
                units = [(para, leading)]
            if not units:
                continue
            if not cur:
                cur_path = section.heading_path
            # Packing spans headings, so every breadcrumb the chunk covers is recorded --
            # keeping only the first would silently drop structural provenance.
            if section.heading_path and section.heading_path not in cur_paths:
                cur_paths.append(section.heading_path)
            for unit, unit_lead in units:
                # Start page = feeds before the paragraph + feeds before this
                # unit's first word + 1 (Red: page = where the cited text
                # begins; a unit's own leading feeds put it on the next page).
                unit_page = para_base + unit_lead + 1
                candidate = cur + [unit]
                if cur and count_tokens("\n\n".join(candidate)) > max_tokens:
                    # Snapshot the overlap tail's pages BEFORE flush(): flush
                    # resets cur_pages, and the tail units belong to the NEW
                    # chunk -- its start page is the tail's start page, not the
                    # incoming unit's (Red: page = where the cited text begins).
                    tail = _tail(candidate[:-1], overlap_tokens) if overlap_tokens else []
                    tail_pages = cur_pages[-len(tail):] if tail else []
                    flush()
                    cur_path = section.heading_path
                    cur_paths = [section.heading_path] if section.heading_path else []
                    cur = tail + [unit]
                    cur_pages = tail_pages + [unit_page]
                else:
                    cur = candidate
                    cur_pages = cur_pages + [unit_page]
            ff_seen = para_base + para_ff
        ff_seen += pending_ff
    flush()
    if has_pages:
        # END-TO-END CONSERVATION (Red review, 2026-08-19): the final cursor
        # must equal every form feed in the document except those hidden in
        # the frontmatter (which split_headings strips before sectioning).
        # Monotonicity cannot detect a silently dropped feed; this equality
        # can. A mismatch means a page break is invisible to the annotation,
        # which is exactly the failure this feature exists to prevent.
        frontmatter_ff = 0
        fm = FRONTMATTER_RE.match(markdown)
        if fm:
            frontmatter_ff = fm.group(0).count("\f")
        if ff_seen != markdown.count("\f") - frontmatter_ff:
            raise ValueError(
                f"page cursor conservation failed: {ff_seen} feeds seen, "
                f"{markdown.count('\f') - frontmatter_ff} present in {doc_id}; "
                "a form feed was dropped by sectioning -- fix the seam, do not "
                "mask it")
    return chunks


def _tail(units: list[str], budget: int) -> list[str]:
    """Trailing units that fit the overlap budget -- context, not duplication."""
    out: list[str] = []
    for unit in reversed(units):
        if count_tokens("\n\n".join([unit, *out])) > budget:
            break
        out.insert(0, unit)
    return out


def _make(doc_id: str, heading_path: str, units: list[str], ordinal: int,
          heading_paths: list[str] | None = None, page: int | None = None) -> Chunk:
    text = "\n\n".join(units)
    # Ordinal is in the digest so two identical passages in one document stay distinct;
    # without it a repeated boilerplate line collapses two chunks into one id.
    # `page` is deliberately NOT in the digest: it is an annotation of the
    # text, never an input to segmentation, so its presence (or absence) must
    # not change chunk identity -- that is the whole of the #52 additive rule.
    digest = hashlib.sha256(
        f"{doc_id}\n{ordinal}\n{heading_path}\n{text}".encode()).hexdigest()[:10]
    meta = {"page": page} if page is not None else {}
    return Chunk(doc_id=doc_id, chunk_id=f"{doc_id}#{digest}", text=text,
                 heading_path=heading_path, heading_paths=list(heading_paths or []),
                 ordinal=ordinal, meta=meta)


def doc_frontmatter_metadata(frontmatter: dict, doc_id: str) -> dict:
    """Flatten a document's frontmatter into the scalar fields every chunk record
    carries (see index/store.py SCALAR_FIELDS and index/bm25.py's chunk_metadata
    columns -- this is the one place both indexes' shared metadata shape is
    derived).

    This is also the SOLE place `deleted` is read out of frontmatter, and the
    reason a soft-delete survives a reindex: `deleted` is not index state, it
    is a document property like `type` or `project`, re-derived fresh from
    `sources/`/`wiki/` on every call (full rebuild, `alexandria index`, or
    single-document `promote`/`cmd_delete`). Marking a document deleted means
    writing `deleted: true` into ITS frontmatter -- never poking the index
    tables directly -- so the flag is exactly as durable as the document
    itself and a `--rebuild` can never un-delete it by accident.
    """
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
        # Strict identity, not truthiness: `deleted: "false"` is a quoted
        # YAML string a human might hand-write believing it clears the flag,
        # and Python's bool("false") is True. `is True` treats any non-bool
        # value (including that typo) as NOT deleted, which is the safer
        # misread for an authoring mistake -- `alexandria lint` (schema.py)
        # separately flags a non-bool `deleted` as bad_type so the mistake is
        # still visible, but it does not block indexing on its own.
        "deleted": frontmatter.get("deleted") is True,
    }


# The two top-level trees that get indexed, and the parts that are quarantined
# out of them. `_unparsed/` holds files migrate.py could not parse (no
# frontmatter) -- documented as deliberately skipped in
# docs/WORK-ORDER-phase1-retrieval.md.
INDEX_ROOTS = frozenset({"sources", "wiki"})
# NB: top-level `inbox/` (the un-promoted staging file) is excluded by the
# INDEX_ROOTS check alone. It must NOT be listed here -- `sources/inbox/` holds
# the PROMOTED inbox documents, which are indexed and retrievable.
QUARANTINED_PARTS = frozenset({".alexandria", "_unparsed"})


def is_appledouble_metadata(path: Path) -> bool:
    """Whether ``path`` is a macOS AppleDouble sidecar, not user content.

    Finder writes resource-fork metadata as a sibling whose *final basename*
    starts ``._``. The final-name rule deliberately leaves ordinary documents
    below a similarly named directory alone.
    """
    return path.name.startswith("._")


def is_indexable_source(relative: Path) -> bool:
    """Whether a corpus-relative path is one the indexer will actually ingest.

    Single source of truth, because anything that COUNTS documents has to agree
    with what INDEXES them or it reports a permanent phantom shortfall. Serve's
    /health once walked `sources/`+`wiki/` itself and reported
    `source_documents_agree: false` forever, because it counted the 25
    quarantined files in `sources/_unparsed/` that the indexer skips by design.
    A health signal that is always false is not a signal. AppleDouble sidecars
    are excluded here too: they are filesystem metadata, not malformed source
    documents to index, count, or lint.
    """
    parts = relative.parts
    if not parts or parts[0] not in INDEX_ROOTS:
        return False
    if is_appledouble_metadata(relative):
        return False
    return not (QUARANTINED_PARTS & set(parts))


def chunk_doc_records(path: Path, corpus: Path, config: "AppConfig") -> tuple[list[dict], str | None]:
    """Read one document off disk and return its fully-formed chunk records --
    the same dict shape VectorStore.upsert() and BM25Index.index() both expect.

    Shared by cli.py's whole-corpus `_load_chunk_records` walk and promote.py's
    single-document promotion path, so the two can never drift on how a chunk
    record is built (moved out of cli.py 2026-08-12 for exactly that reason --
    promote.py must not import from cli.py, which would invert the module
    layering cli.py sits above).
    """
    try:
        document = Doc.read(path, root=corpus)
        markdown = path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        return [], f"{path.relative_to(corpus)}: {exc}"
    metadata = doc_frontmatter_metadata(document.frontmatter, document.doc_id)
    chunks = chunk_document(document.doc_id, markdown, config.chunk_tokens, config.chunk_overlap)
    # #52: the persisted meta annotation = the chunker's page anchor plus the
    # asset pointer from ingest frontmatter (so a search result can OPEN the
    # original, not just name it).
    ingest = (document.frontmatter or {}).get("ingest") or {}
    records = []
    for chunk in chunks:
        meta = dict(chunk.meta)
        if ingest:
            # Explicit, not setdefault: an ingest block WITHOUT these keys must
            # not pollute meta with null placeholders ("asset": null).
            if ingest.get("asset"):
                meta["asset"] = ingest["asset"]
            if ingest.get("original_name"):
                meta["original_name"] = ingest["original_name"]
        # metadata spread FIRST: if doc_frontmatter_metadata ever grew a "meta"
        # key, the explicit annotation below must win (Red review finding).
        records.append({
            "chunk_id": chunk.chunk_id,
            "doc_id": chunk.doc_id,
            "text": chunk.text,
            "heading_path": chunk.heading_path,
            **metadata,
            "meta": meta,
        })
    return records, None



def _frontmatter_has_ingest(text: str) -> bool:
    """Whether the document's FRONTMATTER (not its body) carries an `ingest:`
    block -- i.e. the doc is an ingest companion. Scoped to the frontmatter so
    a body that merely mentions the word is not treated as an artifact."""
    if not text.startswith("---"):
        return False
    fm_end = text.find("\n---", 3)
    fm_block = text[:fm_end + 4] if fm_end > 0 else text
    return "ingest:" in fm_block


@dataclass
class BackfillStats:
    """Outcome of one `index --backfill-meta` pass."""

    docs: int = 0
    chunks: int = 0
    chunks_updated: int = 0


def backfill_meta(corpus: Path, store: "VectorStore", config: AppConfig) -> BackfillStats:
    """Annotate ALREADY-indexed chunks with meta, without re-embedding.

    Red's #52 gates, verbatim:
    - re-run the chunker, then upsert meta keyed by the EXISTING stable
      chunk_ids (chunk_id is content-derived and meta is not in the digest,
      so re-chunking reproduces every id);
    - assert 100% chunk_id match against existing rows -- a mismatch means
      chunker nondeterminism or index drift, which must abort loudly, not
      half-annotate;
    - zero embedding calls: this pass takes no embedder and writes only the
      meta column.
    """
    stats = BackfillStats()
    # #30 P2a: the CORRECT index dir is wherever resolve_active_index_dir
    # says it is -- the active release once one exists, the legacy path
    # otherwise -- never a hardcoded literal that would drift out of sync
    # the moment a corpus adopts staged releases.
    from .releases import resolve_active_index_dir
    store_path = resolve_active_index_dir(corpus)
    if Path(store_path).resolve() != Path(store.path).resolve():
        # A mismatched store is a caller bug (a test or script pointing at a
        # different index than the corpus it asked to annotate); silently
        # re-opening would write to an index the caller never inspected.
        raise ValueError(
            f"backfill store path {store.path} does not match the active "
            f"index {store_path}; refusing to annotate the wrong index")
    existing = set(store.chunk_ids())
    meta_by_chunk: dict[str, dict] = {}
    new_ids: set[str] = set()
    for path in sorted((corpus / "sources").rglob("*.md")) + sorted((corpus / "wiki").rglob("*.md")):
        rel = path.relative_to(corpus)
        if not is_indexable_source(rel):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        # cheap pre-filter: only docs that can carry an annotation. Scoped to
        # the frontmatter block (a body that merely MENTIONS "ingest:" is not
        # an ingest artifact -- Red review finding), with no length truncation
        # so a >2KB frontmatter cannot silently lose its annotation.
        if "\f" not in text and not _frontmatter_has_ingest(text):
            continue
        records, err = chunk_doc_records(path, corpus, config)
        if err or not records:
            continue
        stats.docs += 1
        for record in records:
            meta_by_chunk[record["chunk_id"]] = record["meta"]
            new_ids.add(record["chunk_id"])
    stats.chunks = len(meta_by_chunk)
    missing = new_ids - existing
    if missing:
        shown = ", ".join(sorted(missing)[:3])
        raise RuntimeError(
            f"backfill chunk_id mismatch: {len(missing)} regenerated chunk(s) "
            f"not present in the index (e.g. {shown}); the chunker changed or "
            f"the index drifted -- aborting without writing anything")
    updated = store.update_meta(meta_by_chunk)
    if updated != len(meta_by_chunk):
        raise RuntimeError(
            f"backfill updated {updated} of {len(meta_by_chunk)} rows; refusing "
            f"to report success on a partial annotation")
    stats.chunks_updated = updated
    return stats
