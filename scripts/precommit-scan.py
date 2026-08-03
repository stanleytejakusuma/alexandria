#!/usr/bin/env python3
"""Leak scanner for the public engine repo.

Blocks commits containing private corpus content, secrets, or machine-local paths.
Runs on staged files by default; pass --all to scan the whole worktree.

Structural patterns (secrets, absolute home paths, key shapes) are baked in below.
Project-specific codenames must NOT be listed here -- a deny list of private names,
committed to a public repo, leaks the very names it protects. Put those in
`.leakpatterns.local` (gitignored), one regex per line, `#` for comments.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# (name, regex) -- structural shapes only. Nothing project-specific.
PATTERNS: list[tuple[str, str]] = [
    ("absolute home path", r"/(?:Users|home)/[a-z][a-z0-9_-]{2,}/"),
    ("AWS access key", r"\bAKIA[0-9A-Z]{16}\b"),
    ("generic api key assignment", r"(?i)\b(?:api[_-]?key|secret|passwd|password|token)\b\s*[:=]\s*['\"][^'\"]{16,}['\"]"),
    ("bearer token", r"(?i)\bbearer\s+[A-Za-z0-9._-]{24,}"),
    ("private key block", r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
    ("github token", r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    ("openai-style key", r"\bsk-[A-Za-z0-9]{20,}\b"),
    ("slack token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    ("EVM address", r"\b0x[a-fA-F0-9]{40}\b"),
    ("base58 wallet address", r"\b[1-9A-HJ-NP-Za-km-z]{43,44}\b"),
    # Real host addresses. RFC 5737 documentation ranges (192.0.2.x, 198.51.100.x,
    # 203.0.113.x) and loopback are excluded: they are reserved for examples and
    # can never route to a real machine, so flagging them only trains people to
    # ignore the scanner.
    ("host address", r"\b(?!127\.0\.0\.1)(?!192\.0\.2\.)(?!198\.51\.100\.)(?!203\.0\.113\.)\d{1,3}(?:\.\d{1,3}){3}\b"),
]

# Files the scanner never flags (it would flag its own pattern list).
SELF = {"scripts/precommit-scan.py", "tests/test_precommit_scan.py"}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".webp", ".ico", ".lock"}


def load_local_patterns() -> list[tuple[str, str]]:
    """Project-specific deny patterns from the gitignored local file."""
    f = REPO / ".leakpatterns.local"
    if not f.exists():
        return []
    out = []
    for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if line and not line.startswith("#"):
            out.append((f"local pattern (line {i})", line))
    return out


def staged_files() -> list[str]:
    r = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        cwd=REPO, capture_output=True, text=True, check=False,
    )
    return [f for f in r.stdout.splitlines() if f.strip()]


def all_files() -> list[str]:
    r = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=False
    )
    return [f for f in r.stdout.splitlines() if f.strip()]


def scan_text(text: str, patterns: list[tuple[str, str]]) -> list[tuple[int, str, str]]:
    """Return (lineno, pattern_name, matched_snippet) for every hit."""
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for name, rx in patterns:
            m = re.search(rx, line)
            if m:
                hits.append((lineno, name, m.group(0)[:60]))
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="scan tracked files, not just staged")
    args = ap.parse_args()

    patterns = PATTERNS + load_local_patterns()
    files = all_files() if args.all else staged_files()
    findings = []

    for rel in files:
        if rel in SELF or Path(rel).suffix.lower() in SKIP_SUFFIXES:
            continue
        p = REPO / rel
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, name, snippet in scan_text(text, patterns):
            findings.append(f"  {rel}:{lineno}  [{name}]  {snippet}")

    if findings:
        print("LEAK SCAN FAILED -- commit blocked:", file=sys.stderr)
        print("\n".join(findings), file=sys.stderr)
        print(f"\n{len(findings)} finding(s). Remove them or add a justified "
              f"exception before committing.", file=sys.stderr)
        return 1

    print(f"leak scan clean ({len(files)} file(s), {len(patterns)} patterns)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
