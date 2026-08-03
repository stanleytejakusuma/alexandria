#!/usr/bin/env python3
"""Propose (and optionally apply) eviction from a capped flat-markdown memory store.

Written for the flat-markdown store shape: entries separated by a line containing only
`§`, each ending with `<!-- created=YYYY-MM-DD, last=YYYY-MM-DD -->`.

THREE RULES, in the order they matter:

1. **Eviction is whole-entry, explicit, and logged.** Never partial. The observed
   defect in the existing auto-consolidation is not its policy but its mangling: it
   merged entries and dropped a clause with no warning. Policy is secondary to never
   silently editing a survivor.

2. **Pinned entries are never evicted.** An entry is pinned when it must be in front
   of the model *before anyone knows it is relevant* -- a standing rule, preference or
   correction. Those are delivered by injection, not retrieval, so a copy existing in
   a retrieval system does not make the original redundant. Episodic entries (what
   happened, incidents, discovered quirks) are pull-shaped and evict freely.

3. **LRU is NOT usable on this store shape.** Every entry is injected into every
   prompt, so `last=` records last *injection*, not last usefulness -- measured on the
   real store, all 15 entries read the same date. LRU here degenerates to arbitrary
   order while looking principled. Ranking is by explicit classification instead.

Eviction requires prior ingestion into a durable store; --apply refuses without
--ingested-ok, because a capped store cannot be pruned safely until its contents
exist somewhere else.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SEPARATOR = "§"
META_RE = re.compile(r"<!--\s*created=([0-9-]+),\s*last=([0-9-]+)\s*-->")
SPLIT_RE = re.compile(rf"\n\s*{SEPARATOR}\s*\n")
JOIN = f"\n\n{SEPARATOR}\n\n"   # canonical separator; round-trips exactly

# Entries matching these read as standing rules: they constrain future behaviour
# rather than recording a past event. Deliberately generous -- a false pin costs
# space, a false evict costs a rule.
PINNED_MARKERS = re.compile(
    r"\b(never|always|must|do not|don't|prefer|rule|doctrine|convention|policy|"
    r"discipline|before (?:publishing|any|starting)|stays? local|treat .* as)\b", re.I)
EPISODIC_MARKERS = re.compile(
    r"\b(incident|today|this morning|root[- ]caused?|shipped a live bug|"
    r"was fixed|turned out|observed (?:on|at)|happened)\b", re.I)

CAPS = {"USER.md": 5000, "MEMORY.md": 5000, "failures.md": 10000}
DEFAULT_CAP = 5000


CLAUSE_RE = re.compile(r"(?=\((\d)\)\s)")


def split_clauses(text: str) -> list[str]:
    """Split a multi-topic blob on its own numbered clauses.

    Entries became 1500-3000 char multi-topic blobs BECAUSE the cap made atomic ones
    impossible to add -- so the cap manufactured the shape that now makes deletion
    collateral-lossy. Splitting restores atomicity, which is what makes eviction
    surgical instead of a choice between keeping four lessons or losing four.

    Returns [text] unchanged when there is no clear clause structure: a bad split is
    worse than no split, because it mangles a survivor.
    """
    parts = [p.strip() for p in CLAUSE_RE.split(text) if p and p.strip()]
    # CLAUSE_RE captures the digit, so drop bare-digit fragments
    parts = [p for p in parts if not p.isdigit()]
    if len(parts) < 3:
        return [text]
    head = parts[0] if not parts[0].startswith("(") else ""
    body = [p for p in parts if p.startswith("(")]
    if len(body) < 3:
        return [text]
    return [f"{head} {b}".strip() if head else b for b in body]


@dataclass
class Entry:
    text: str
    created: str
    last: str
    raw: str

    @property
    def chars(self) -> int:
        return len(self.raw)

    @property
    def title(self) -> str:
        head = re.split(r"(?<=[.:!?])\s", self.text.strip(), maxsplit=1)[0]
        return " ".join(head.split())[:96]

    def classify(self) -> tuple[str, str]:
        pins = len(PINNED_MARKERS.findall(self.text))
        eps = len(EPISODIC_MARKERS.findall(self.text))
        if pins and pins >= eps:
            return "PINNED", f"{pins} standing-rule marker(s)"
        if eps:
            return "EPISODIC", f"{eps} episodic marker(s), {pins} rule marker(s)"
        return "UNCLASSIFIED", "no clear markers -- review by hand"


def parse(path: Path) -> list[Entry]:
    raw = path.read_text(encoding="utf-8")
    chunks = SPLIT_RE.split(raw) if SPLIT_RE.search(raw) else [raw]
    out = []
    for chunk in chunks:
        text = chunk.strip()
        if not text:
            continue
        m = META_RE.search(text)
        out.append(Entry(META_RE.sub("", text).strip(),
                         m.group(1) if m else "", m.group(2) if m else "", text))
    return out


def render(entries: list[Entry]) -> str:
    return JOIN.join(e.raw for e in entries) + "\n"


def run(args) -> int:
    return _run(args)


def _build() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("stores", nargs="+", help="paths to memory store .md files")
    ap.add_argument("--apply", action="store_true", help="write changes (default: propose)")
    ap.add_argument("--ingested-ok", action="store_true",
                    help="assert contents are already durable elsewhere")
    ap.add_argument("--target", type=float, default=0.80,
                    help="target fill fraction of cap (default 0.80)")
    ap.add_argument("--propose-splits", action="store_true",
                    help="show how multi-topic blobs would split into atomic entries")
    return ap


def _run(args) -> int:

    if args.apply and not args.ingested_ok:
        print("REFUSING: --apply requires --ingested-ok. Ingest the stores into a "
              "durable system first; a capped store cannot be pruned safely until "
              "its contents exist somewhere else.", file=sys.stderr)
        return 2

    grand_before = grand_after = 0
    log_lines = []

    for raw_path in args.stores:
        path = Path(raw_path).expanduser()
        if not path.is_file():
            print(f"skip (missing): {path}", file=sys.stderr)
            continue
        cap = CAPS.get(path.name, DEFAULT_CAP)
        entries = parse(path)
        size = sum(e.chars for e in entries)
        target = int(cap * args.target)

        print(f"\n{'='*74}\n{path}  ({size}/{cap} chars, {len(entries)} entries)")
        print(f"target {target} chars ({args.target:.0%} of cap)")
        print("-"*74)

        if getattr(args, "propose_splits", False):
            for e in entries:
                pieces = split_clauses(e.text)
                if len(pieces) > 1:
                    print(f"  SPLIT  {e.chars:>5}ch -> {len(pieces)} atomic entries"
                          f"  [{e.classify()[0]}] {e.title[:52]}")
                    for i, piece in enumerate(pieces, 1):
                        print(f"      {i}. ({len(piece)}ch) {piece[:96]}...")
            print()

        keep, evict = [], []
        # Evict only EPISODIC, largest first, until under target. Pinned and
        # unclassified are never touched by the machine.
        episodic = sorted([e for e in entries if e.classify()[0] == "EPISODIC"],
                          key=lambda e: -e.chars)
        running = size
        chosen = set()
        for e in episodic:
            if running <= target:
                break
            chosen.add(id(e))
            running -= e.chars

        for e in entries:
            kind, why = e.classify()
            if id(e) in chosen:
                evict.append(e)
                print(f"  EVICT   {e.chars:>5}ch  [{kind}] {e.title}")
                print(f"          reason: {why}; created={e.created}")
                log_lines.append(f"{datetime.now().isoformat(timespec='seconds')}\t"
                                 f"{path.name}\tEVICT\t{e.chars}\t{e.title}")
            else:
                keep.append(e)
                print(f"  keep    {e.chars:>5}ch  [{kind}] {e.title[:60]}")

        after = sum(e.chars for e in keep)
        grand_before += size
        grand_after += after
        print(f"  -> {size} to {after} chars ({len(evict)} evicted)"
              f"{'  STILL OVER TARGET' if after > target else ''}")

        if args.apply and evict:
            backup = path.with_suffix(path.suffix + f".pre-decay-{int(datetime.now().timestamp())}")
            shutil.copy2(path, backup)
            path.write_text(render(keep), encoding="utf-8")
            print(f"  applied; backup at {backup.name}")

    print(f"\n{'='*74}\nTOTAL {grand_before} -> {grand_after} chars")
    if args.apply and log_lines:
        log = Path(args.stores[0]).expanduser().parent / ".decay-log.tsv"
        with log.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(log_lines) + "\n")
        print(f"logged {len(log_lines)} eviction(s) to {log}")
    elif not args.apply:
        print("PROPOSAL ONLY -- nothing written. Re-run with --apply --ingested-ok.")
    return 0


if __name__ == "__main__":
    sys.exit(_run(_build().parse_args()))
