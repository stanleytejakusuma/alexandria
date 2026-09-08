#!/usr/bin/env python3
"""Commit staged source history, then atomically activate the generation."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from alexandria.generation_history import snapshot_and_commit_history
from alexandria.generation_publisher import publish_generation


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--control-root", required=True)
    parser.add_argument("--staged", required=True)
    args = parser.parse_args()
    control, staged = Path(args.control_root), Path(args.staged)
    active = publish_generation(
        control,
        staged,
        snapshot=lambda candidate: snapshot_and_commit_history(
            control, candidate, generation_id=staged.name
        ),
    )
    print(active)
    return 0


if __name__ == "__main__":
    sys.exit(main())
