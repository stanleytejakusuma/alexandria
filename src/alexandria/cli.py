"""Command line entry point.

argparse rather than a CLI framework: the surface is small, and stdlib means one
fewer dependency for a tool whose whole pitch is that your data outlives the engine.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .corpus import Doc
from .migrate import migrate_kg_sync
from .schema import Severity, validate

DEFAULT_CORPUS = Path.home() / "alexandria-corpus"


def cmd_migrate(args) -> int:
    report = migrate_kg_sync(args.vault, args.corpus, dry_run=args.dry_run)
    print(report.render())
    if not report.reconciles:
        print("\nFAIL: counts do not reconcile", file=sys.stderr)
        return 1
    return 0


def cmd_lint(args) -> int:
    corpus = Path(args.corpus)
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
    from .connectors.pi_sessions import PiSessionsConnector
    from .llm import LLMClient

    if args.connector != "pi-sessions":
        print(f"unknown connector: {args.connector}", file=sys.stderr)
        return 2

    corpus = Path(args.corpus)
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

    written = 0
    for item in items:
        docs = conn.normalize(item)
        for doc in docs:
            doc.write(corpus)
            written += 1
        if docs:                       # fail-safe: only consume on success
            conn.commit([item])
    print(f"wrote {written} note(s)")
    for err in conn.errors[:10]:
        print(f"  error: {err}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="alexandria", description=__doc__)
    p.add_argument("--corpus", default=str(DEFAULT_CORPUS), help="corpus repo path")
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
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_sync)

    lint = sub.add_parser("lint", help="validate every document against the schema")
    lint.set_defaults(func=cmd_lint)

    return p


def app(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(app())
