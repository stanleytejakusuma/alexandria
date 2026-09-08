"""Read-only corpus parity evidence for storage migration decisions."""
from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_DOCUMENT_DIRS = ("sources", "wiki")
_SIZE_DIRS = ("sources", "wiki", ".alexandria/state", ".alexandria/index", ".alexandria/cache")


class ParityError(RuntimeError):
    """A parity probe could not produce trustworthy read-only evidence."""


@dataclass(frozen=True)
class CorpusSnapshot:
    corpus: str
    document_ids: frozenset[str]
    generation: int
    active_generation: str | None
    git_head: str | None
    logical_bytes: Mapping[str, int]

    def to_json(self) -> dict[str, object]:
        return {
            "corpus": self.corpus,
            "document_ids": sorted(self.document_ids),
            "generation": self.generation,
            "active_generation": self.active_generation,
            "git_head": self.git_head,
            "logical_bytes": dict(self.logical_bytes),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> "CorpusSnapshot":
        try:
            corpus = data["corpus"]
            ids = data["document_ids"]
            generation = data["generation"]
            sizes = data["logical_bytes"]
        except KeyError as exc:
            raise ParityError(f"remote snapshot missing {exc.args[0]}") from exc
        if not isinstance(corpus, str) or not isinstance(ids, list) or not isinstance(generation, int):
            raise ParityError("remote snapshot has invalid required fields")
        if not all(isinstance(item, str) for item in ids):
            raise ParityError("remote snapshot document_ids must be strings")
        if not isinstance(sizes, dict) or not all(isinstance(k, str) and isinstance(v, int) for k, v in sizes.items()):
            raise ParityError("remote snapshot logical_bytes must be string/integer pairs")
        active = data.get("active_generation")
        head = data.get("git_head")
        if active is not None and not isinstance(active, str):
            raise ParityError("remote snapshot active_generation must be a string or null")
        if head is not None and not isinstance(head, str):
            raise ParityError("remote snapshot git_head must be a string or null")
        return cls(corpus, frozenset(ids), generation, active, head, sizes)


@dataclass(frozen=True)
class ParityReport:
    local: CorpusSnapshot
    remote: CorpusSnapshot
    local_only: tuple[str, ...]
    remote_only: tuple[str, ...]
    document_delta: int
    generation_delta: int

    @property
    def in_sync(self) -> bool:
        return not self.local_only and not self.remote_only and self.generation_delta == 0


def _document_ids(root: Path) -> frozenset[str]:
    found: set[str] = set()
    for dirname in _DOCUMENT_DIRS:
        directory = root / dirname
        if not directory.exists():
            continue
        for path in directory.rglob("*.md"):
            if path.name.startswith("._"):
                continue
            found.add(str(path.relative_to(root).with_suffix("")))
    return frozenset(found)


def _logical_bytes(path: Path, *, ignore_appledouble: bool = False) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return 0 if ignore_appledouble and path.name.startswith("._") else path.stat().st_size
    return sum(
        entry.stat().st_size
        for entry in path.rglob("*")
        if entry.is_file() and not (ignore_appledouble and entry.name.startswith("._"))
    )


def _pointer_generation(root: Path) -> str | None:
    pointer = root / ".alexandria" / "current-generation.json"
    try:
        value = json.loads(pointer.read_text()).get("generation")
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) else None


def _index_generation(root: Path, active_generation: str | None) -> int:
    index_root = root
    if active_generation is not None:
        index_root = root / ".alexandria" / "generations" / active_generation
    try:
        data = json.loads((index_root / ".alexandria" / "index" / "generation.json").read_text())
        return int(data.get("generation", 0))
    except (OSError, ValueError, TypeError):
        return 0


def _git_head(root: Path) -> str | None:
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def snapshot_corpus(corpus: str | Path) -> CorpusSnapshot:
    """Collect deterministic evidence without modifying the corpus."""
    root = Path(corpus).expanduser().resolve()
    active = _pointer_generation(root)
    return CorpusSnapshot(
        corpus=str(root),
        document_ids=_document_ids(root),
        generation=_index_generation(root, active),
        active_generation=active,
        git_head=_git_head(root),
        logical_bytes={
            name.removeprefix(".alexandria/"): _logical_bytes(
                root / name, ignore_appledouble=name in _DOCUMENT_DIRS
            )
            for name in _SIZE_DIRS
        },
    )


def compare_snapshots(local: CorpusSnapshot, remote: CorpusSnapshot) -> ParityReport:
    return ParityReport(
        local=local,
        remote=remote,
        local_only=tuple(sorted(local.document_ids - remote.document_ids)),
        remote_only=tuple(sorted(remote.document_ids - local.document_ids)),
        document_delta=len(local.document_ids) - len(remote.document_ids),
        generation_delta=local.generation - remote.generation,
    )


_REMOTE_PROBE = """import json, pathlib, subprocess, sys
root = pathlib.Path(sys.argv[1]).expanduser().resolve()
documents = set()
for name in ('sources', 'wiki'):
    directory = root / name
    if directory.exists():
        documents.update(str(path.relative_to(root).with_suffix('')) for path in directory.rglob('*.md') if not path.name.startswith('._'))
def size(path, ignore_appledouble=False):
    return sum(item.stat().st_size for item in path.rglob('*') if item.is_file() and not (ignore_appledouble and item.name.startswith('._'))) if path.exists() else 0
try:
    pointer = json.loads((root / '.alexandria/current-generation.json').read_text()).get('generation')
except Exception:
    pointer = None
index_root = root / '.alexandria/generations' / pointer if isinstance(pointer, str) else root
try:
    generation = int(json.loads((index_root / '.alexandria/index/generation.json').read_text()).get('generation', 0))
except Exception:
    generation = 0
head = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True, text=True)
print(json.dumps({'corpus': str(root), 'document_ids': sorted(documents), 'generation': generation, 'active_generation': pointer, 'git_head': head.stdout.strip() if head.returncode == 0 else None, 'logical_bytes': {name: size(root / path, name in ('sources', 'wiki')) for name, path in {'sources':'sources', 'wiki':'wiki', 'state':'.alexandria/state', 'index':'.alexandria/index', 'cache':'.alexandria/cache'}.items()}}))"""


def probe_remote_snapshot(
    host: str,
    corpus: str,
    *,
    run: Callable[[list[str]], str] | None = None,
) -> CorpusSnapshot:
    """Run the self-contained, read-only probe over SSH and decode its JSON."""
    if not _HOST.fullmatch(host):
        raise ParityError(f"invalid remote host: {host!r}")
    command = f"python3 -c {shlex.quote(_REMOTE_PROBE)} {shlex.quote(corpus)}"
    if run is None:
        completed = subprocess.run(["ssh", host, command], capture_output=True, text=True)
        if completed.returncode != 0:
            raise ParityError(f"remote probe failed: {completed.stderr.strip()[:200]}")
        output = completed.stdout
    else:
        output = run(["ssh", host, command])
    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ParityError("remote probe returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise ParityError("remote probe returned a non-object JSON value")
    return CorpusSnapshot.from_json(data)
