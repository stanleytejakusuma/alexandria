#!/usr/bin/env python3
"""Post-run verification for the weekly loop: did it DO anything?

Every significant loop bug found on 2026-08-11 shared one shape -- a step
reported success while doing nothing, and every exit code was 0 throughout:

  - a missing `mkdir` made every sync abort its own output redirect
  - the loop synced documents and never indexed them, so nothing became findable
  - `git add notes ...` matched no pathspec, staged NOTHING, and `--allow-empty`
    committed anyway, manufacturing a snapshot that captured zero files
  - bursts that produced no note were never marked consumed
  - a truncated LLM response surfaced as "Unterminated string at column 30745"

Exit codes and log lines cannot catch that class of failure, because the failing
step is the one that writes the log line. So this checks OBSERVABLE OUTCOMES
instead: documents on disk, the index generation counter, whether the newest
document can actually be RETRIEVED, and whether the commit contains files.

Retrievability is the load-bearing check. It is the only one that exercises the
whole chain -- sync wrote a file, index chunked and embedded it, search can find
it -- and it is the exact failure that went unnoticed for three days.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

INDEXED_DIRS = ("sources", "wiki")


def _is_document(p: Path) -> bool:
    """A real corpus document: a .md file that is not an AppleDouble transfer
    artifact. Netatalk/SMB sidecars ("._*.md") are binary metadata that
    is_indexable_source ignores, so counting them as documents inflates the
    count and, as the newest files, fails the indexed check every run."""
    return p.suffix == ".md" and not p.name.startswith("._")


def docs_on_disk(corpus: Path) -> int:
    return sum(1 for d in INDEXED_DIRS for _ in (corpus / d).rglob("*.md")
               if _is_document(_))


def index_generation(corpus: Path) -> int:
    try:
        data = json.loads((corpus / ".alexandria/index/generation.json").read_text())
        return int(data.get("generation", 0))
    except (OSError, ValueError, TypeError):
        return 0


def newest_docs(corpus: Path, n: int = 5) -> list[Path]:
    """The most recently written documents -- the ones most likely to be missing
    from a stale index, which is precisely what we want to probe.

    Several, not one: after a bulk sync thousands of files share a timestamp, so
    "the newest" is arbitrary, and a single degenerate note decides the verdict.
    The vault really does contain one whose title is `[[none]]`; searching for
    that finds nothing, and probing only it failed the whole run.
    """
    docs = [p for d in INDEXED_DIRS for p in (corpus / d).rglob("*.md")
            if _is_document(p)]
    return sorted(docs, key=lambda p: p.stat().st_mtime, reverse=True)[:n]


def chunks_for(corpus: Path, doc: Path) -> int:
    """How many chunks the index holds for this document.

    Deterministic membership, with no ranking involved. "Is it in the index" and
    "does search rank it" are different questions, and conflating them made a
    healthy index look broken: two vault notes titled `[[none]]` and `[[TOOL]]`
    hold 420 and 103 chunks respectively, yet neither title is a query that can
    find anything.
    """
    key = str(doc.relative_to(corpus).with_suffix(""))
    fts = _active_fts_path(corpus)
    if fts is None:
        return 0
    try:
        con = sqlite3.connect(f"file:{fts}?mode=ro", uri=True)
        with con:
            return con.execute("SELECT count(*) FROM chunk_metadata WHERE chunk_id LIKE ?",
                               (f"{key}#%",)).fetchone()[0]
    except sqlite3.Error:
        return 0


def _active_fts_path(corpus: Path) -> Path | None:
    """The fts.sqlite of the ACTIVE index, wherever it lives.

    Since the staged-release cutover (DECISION-staged-releases-p2a) the live
    store lives at .alexandria/index/releases/<id>/ -- the legacy flat
    .alexandria/index/fts.sqlite is a frozen snapshot from before the first
    release and never receives new chunks. Probing it made the indexed check
    false-FAIL every run once a release existed (observed on the NAS
    2026-08-24: the two newest documents had chunks in the active release but
    the verify reported 0/5 because it queried the legacy file). Resolution
    mirrors index/releases.py's resolve_active_index_dir: active.json wins;
    a pointer to a missing release fails loud (returns None -> 0 chunks ->
    the run fails, which is correct because the index is genuinely broken).
    """
    active = corpus / ".alexandria/index/active.json"
    if not active.exists():
        return corpus / ".alexandria/index/fts.sqlite"
    try:
        data = json.loads(active.read_text())
        release_id = data.get("release_id")
    except (OSError, ValueError):
        return None
    if not isinstance(release_id, str):
        return None
    release_dir = corpus / ".alexandria/index/releases" / release_id
    fts = release_dir / "fts.sqlite"
    return fts if fts.exists() else None


def is_probeable(title: str) -> bool:
    """Whether a title is usable as a search query at all. A bare wikilink or a
    one-word label cannot rank against tens of thousands of documents, so failing
    it would say nothing about retrieval."""
    cleaned = title.strip().strip("[]").strip()
    return len(cleaned) >= 15 and len(cleaned.split()) >= 3


def title_of(doc: Path) -> str:
    for line in doc.read_text(encoding="utf-8", errors="replace").splitlines()[:20]:
        if line.startswith("title:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    return doc.stem.replace("-", " ")


def committed_file_count(corpus: Path) -> int:
    out = subprocess.run(["git", "-C", str(corpus), "show", "--numstat", "--format=", "HEAD"],
                         capture_output=True, text=True)
    return len([l for l in out.stdout.splitlines() if l.strip()])


def is_retrievable(binary: str, corpus: Path, doc: Path, k: int = 10) -> tuple[bool, str]:
    """Search for the document by its own title and look for its path in the hits.
    A document that cannot be found by its exact title is not indexed, whatever
    the row counts say."""
    # Match the chunk id shape `sources/<source>/<stem>#<hash>`, not a bare
    # substring. `stem in output` passes whenever the stem occurs anywhere --
    # including inside a longer document name, or in the echoed query itself. A
    # one-character stem made a no-op binary report 1/1 found.
    needle = f"/{doc.stem}#"
    try:
        out = subprocess.run([binary, "--corpus", str(corpus), "search", title_of(doc), "--k", str(k)],
                             capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"search failed to run: {type(exc).__name__}"
    if out.returncode != 0:
        return False, f"search exited {out.returncode}: {out.stderr.strip()[:120]}"
    hit = needle in out.stdout
    return hit, ("found" if hit else f"not in top-{k}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--binary", required=True, help="path to the alexandria CLI")
    ap.add_argument("--docs-before", type=int, required=True)
    ap.add_argument("--generation-before", type=int, required=True)
    args = ap.parse_args()

    corpus = Path(args.corpus).expanduser()
    docs_after, gen_after = docs_on_disk(corpus), index_generation(corpus)
    new_docs = docs_after - args.docs_before
    checks: list[tuple[str, bool, str]] = []

    # Documents may legitimately not change -- a quiet week syncs nothing. A DROP
    # never legitimately happens, and would mean the loop destroyed corpus content.
    checks.append(("documents", docs_after >= args.docs_before,
                   f"{args.docs_before} -> {docs_after} ({new_docs:+d})"))

    # The index step must run even when nothing new arrived, because the previous
    # run may have left the index behind. If documents DID arrive, a generation
    # that did not move means they are on disk and invisible -- the exact failure
    # that hid for three days.
    gen_ok = gen_after > args.generation_before if new_docs > 0 else gen_after >= args.generation_before
    checks.append(("index generation", gen_ok,
                   f"{args.generation_before} -> {gen_after}"
                   + ("" if gen_ok else "  <-- documents arrived but the index did not move")))

    # Two separate questions, deliberately not merged.
    #
    # (a) INDEXED: are the newest documents in the index at all? Exact, so it is
    #     required of every probe -- this is the three-day-freeze detector.
    probes = newest_docs(corpus)
    if not probes:
        checks.append(("indexed", False, "corpus contains no documents"))
        checks.append(("retrievable", False, "corpus contains no documents"))
    else:
        missing = [d.name for d in probes if chunks_for(corpus, d) == 0]
        checks.append(("indexed", not missing,
                       f"{len(probes) - len(missing)}/{len(probes)} newest documents have chunks"
                       + (f"  (absent: {', '.join(m[:40] for m in missing[:2])})" if missing else "")))

        # (b) RETRIEVABLE: can search actually surface one end to end? Only a
        #     document whose title is a meaningful query can answer that.
        probeable = next((d for d in probes if is_probeable(title_of(d))), None)
        if probeable is None:
            checks.append(("retrievable", True,
                           "skipped: none of the newest documents has a searchable title"))
        else:
            found, why = is_retrievable(args.binary, corpus, probeable)
            checks.append(("retrievable", found, f"{probeable.name[:56]}: {why}"))

    # A commit is only meaningful if it captured something. An empty commit after
    # new documents arrived is the --allow-empty failure resurfacing.
    files = committed_file_count(corpus)
    commit_ok = files > 0 if new_docs > 0 else True
    checks.append(("corpus snapshot", commit_ok,
                   f"HEAD contains {files} file(s)"
                   + ("" if commit_ok else "  <-- new documents were not committed")))

    width = max(len(n) for n, _, _ in checks)
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name.ljust(width)}  {detail}")
    failed = [n for n, ok, _ in checks if not ok]
    if failed:
        print(f"LOOP VERIFY: FAIL ({', '.join(failed)})")
        return 1
    print("LOOP VERIFY: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
