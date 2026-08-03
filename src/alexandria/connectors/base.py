"""Connector contract + shared state handling.

A connector is pull-based, batch, and idempotent. State lives in
`.alexandria/state/<name>.json` (cheap, corruption-tolerant -- re-runs are no-ops).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

__all__ = ["RawItem", "Connector", "StateStore"]


@dataclass
class RawItem:
    """One unit of upstream material, ready to normalize."""

    source_id: str
    content: str
    meta: dict = field(default_factory=dict)


class Connector(Protocol):
    name: str

    def discover(self) -> list[RawItem]: ...
    def normalize(self, item: RawItem) -> list: ...


class StateStore:
    """JSON-backed connector state. Corruption is survivable: a lost state file
    means re-discovery, and re-discovery is idempotent by design."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.data = {}

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        self.data[key] = value

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)          # atomic: never leave a half-written state file
