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

__all__ = ["Chunk", "Section", "chunk_document", "count_tokens", "split_headings"]

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
        if text.strip():
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

    def flush() -> None:
        nonlocal cur, cur_paths
        if cur:
            chunks.append(_make(doc_id, cur_path, cur, len(chunks), cur_paths))
            cur, cur_paths = [], []

    for section in split_headings(markdown):
        units: list[str] = []
        for para in _paragraphs(section.text):
            units.extend(_hard_split(para, max_tokens)
                         if count_tokens(para) > max_tokens else [para])
        if not units:
            continue
        if not cur:
            cur_path = section.heading_path
        # Packing spans headings, so every breadcrumb the chunk covers is recorded --
        # keeping only the first would silently drop structural provenance.
        if section.heading_path and section.heading_path not in cur_paths:
            cur_paths.append(section.heading_path)
        for unit in units:
            candidate = cur + [unit]
            if cur and count_tokens("\n\n".join(candidate)) > max_tokens:
                flush()
                cur_path = section.heading_path
                cur_paths = [section.heading_path] if section.heading_path else []
                cur = (_tail(candidate[:-1], overlap_tokens) + [unit]
                       if overlap_tokens else [unit])
            else:
                cur = candidate
    flush()
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
          heading_paths: list[str] | None = None) -> Chunk:
    text = "\n\n".join(units)
    # Ordinal is in the digest so two identical passages in one document stay distinct;
    # without it a repeated boilerplate line collapses two chunks into one id.
    digest = hashlib.sha256(
        f"{doc_id}\n{ordinal}\n{heading_path}\n{text}".encode()).hexdigest()[:10]
    return Chunk(doc_id=doc_id, chunk_id=f"{doc_id}#{digest}", text=text,
                 heading_path=heading_path, heading_paths=list(heading_paths or []),
                 ordinal=ordinal)
