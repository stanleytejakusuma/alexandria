#!/usr/bin/env python3
"""Human-fired, non-publishing reconciliation staging command."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from alexandria.generation_pointer import resolve_generation
from alexandria.generation_validation import validate_generation
from alexandria.reconciliation_import import plan_from_json
from alexandria.reconciliation_runner import stage_and_verify

_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _remote_fetcher(host: str, corpus: str, source_ids: set[str]):
    if not _HOST.fullmatch(host):
        raise RuntimeError(f"invalid remote host: {host!r}")
    code = r'''
import base64, json, sys
from pathlib import Path
root = Path(sys.argv[1]).resolve()
ids = json.load(sys.stdin)
out = {}
for source_id in ids:
    relative = Path(source_id)
    if not relative.parts or relative.is_absolute() or relative.parts[0] not in ('sources', 'wiki') or any(p in ('.', '..') for p in relative.parts):
        raise SystemExit('unsafe source ID')
    path = (root / (source_id + '.md')).resolve()
    allowed = (root / relative.parts[0]).resolve()
    if allowed not in path.parents:
        raise SystemExit('source ID escapes source root')
    out[source_id] = base64.b64encode(path.read_bytes()).decode('ascii')
json.dump(out, sys.stdout)
'''
    command = f"python3 -c {shlex.quote(code)} {shlex.quote(corpus)}"
    completed = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", host, command],
        input=json.dumps(sorted(source_ids)), text=True, capture_output=True, timeout=90,
    )
    if completed.returncode:
        raise RuntimeError(f"remote read failed: {completed.stderr.strip()[:300]}")
    payloads = json.loads(completed.stdout)
    if set(payloads) != source_ids:
        raise RuntimeError("remote read returned a different source-ID set")
    decoded = {source_id: base64.b64decode(value, validate=True) for source_id, value in payloads.items()}
    return lambda source_id: decoded[source_id]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-root", required=True)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--remote-host", required=True)
    parser.add_argument("--remote-corpus", required=True)
    parser.add_argument("--generation-id", required=True)
    parser.add_argument("--review-packet", required=True, type=Path,
                        help="human-reviewed packet whose sha256 is recorded with this staging run")
    args = parser.parse_args()
    if not args.review_packet.is_file():
        parser.error(f"review packet not found: {args.review_packet}")
    plan = plan_from_json(json.loads(args.plan.read_text()))
    review_hash = hashlib.sha256(args.review_packet.read_bytes()).hexdigest()
    remote_ids = set(plan.add_to_local) | {source_id for source_id, _local, _remote in plan.conflicts}
    fetch = _remote_fetcher(args.remote_host, args.remote_corpus, remote_ids)
    active = resolve_generation(args.control_root)
    baseline = validate_generation(active, docs_before=0, generation_before=0)
    pythonpath = str(REPO / "src") + os.pathsep + os.environ.get("PYTHONPATH", "")

    # verify-loop-run executes its --binary directly. The editable venv's
    # console entrypoint may belong to a different checkout, so make a tiny
    # ephemeral wrapper that invokes THIS checkout through its interpreter.
    with tempfile.TemporaryDirectory(prefix="alexandria-reconcile-") as temporary:
        cli = Path(temporary) / "alexandria"
        cli.write_text(
            "#!/bin/sh\n"
            f"export PYTHONPATH={shlex.quote(pythonpath)}\n"
            f"exec {shlex.quote(sys.executable)} -m alexandria.cli \"$@\"\n"
        )
        cli.chmod(0o755)

        def run_index(stage: Path) -> None:
            subprocess.run([str(cli), "--corpus", str(stage), "index"], check=True)

        def verify(stage: Path) -> None:
            subprocess.run([sys.executable, str(REPO / "scripts" / "verify-loop-run.py"),
                            "--corpus", str(stage), "--binary", str(cli),
                            "--docs-before", str(baseline["documents"]),
                            "--generation-before", str(baseline["generation"]),
                            "--skip-commit-check"], check=True)

        staged = stage_and_verify(args.control_root, args.generation_id, plan,
                                  fetch=fetch, run_index=run_index, verify=verify)
    print(f"STAGED AND VERIFIED (NOT PUBLISHED): {staged}")
    print(f"Reviewed packet sha256: {review_hash}")
    print("Human-fired publish command:")
    print(f"  {sys.executable} {REPO / 'scripts' / 'publish-staged-generation.py'} --control-root {args.control_root} --staged {staged}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
