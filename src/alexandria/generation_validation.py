"""Git-free evidence for a staged generation."""
from __future__ import annotations

import json
from pathlib import Path


def validate_generation(generation: str | Path, *, docs_before: int, generation_before: int) -> dict[str, int]:
    root = Path(generation)
    marker = root / ".alexandria" / "reconciliation-required.json"
    if marker.exists():
        raise ValueError("staged reconciliation requires reindex before publication")
    documents = sum(1 for directory in ("sources", "wiki") for path in (root / directory).rglob("*.md") if not path.name.startswith("._"))
    try:
        generation_number = int(json.loads((root / ".alexandria" / "index" / "generation.json").read_text()).get("generation", 0))
    except (OSError, ValueError, TypeError):
        generation_number = 0
    if documents < docs_before or generation_number <= 0 or generation_number < generation_before:
        raise ValueError("staged generation failed local validation")
    return {"documents": documents, "generation": generation_number}
