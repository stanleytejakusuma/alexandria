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
import subprocess
import sys
from pathlib import Path

INDEXED_DIRS = ("sources", "wiki")


def docs_on_disk(corpus: Path) -> int:
    return sum(1 for d in INDEXED_DIRS for _ in (corpus / d).rglob("*.md"))


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
    docs = [p for d in INDEXED_DIRS for p in (corpus / d).rglob("*.md")]
    return sorted(docs, key=lambda p: p.stat().st_mtime, reverse=True)[:n]


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

    # A stale index fails EVERY probe, so a majority rule still catches the
    # failure this exists for, while tolerating an individual unfindable note.
    probes = newest_docs(corpus)
    if not probes:
        checks.append(("retrievable", False, "corpus contains no documents"))
    else:
        hits = [d for d in probes if is_retrievable(args.binary, corpus, d)[0]]
        misses = [d.name for d in probes if d not in hits]
        checks.append(("retrievable", len(hits) * 2 >= len(probes),
                       f"{len(hits)}/{len(probes)} of the newest documents found"
                       + (f"  (missed: {', '.join(m[:40] for m in misses[:2])})" if misses else "")))

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
