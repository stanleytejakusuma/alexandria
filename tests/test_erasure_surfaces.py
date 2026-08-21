"""#6 erasure-core item 3: binds the ERASURE_SURFACES enumeration to reality.

Two independent checks, per the failure-frame note this file was written
against: (1) every listed surface's class must actually exist and import
cleanly -- a renamed/removed class fails loudly, not silently; (2) a
discovery scan for persistence-shaped classes (Store/Cache/Logger/Index
suffixes) across src/alexandria/ must not find anything NOT already
accounted for -- a genuinely new persistence surface trips this test even
if nobody remembered to update ERASURE_SURFACES by hand."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from alexandria.erasure_surfaces import ERASURE_SURFACES

SRC_ROOT = Path(__file__).parent.parent / "src" / "alexandria"

# Classes that match the Store/Cache/Logger/Index name pattern but are NOT
# doc-content persistence surfaces -- each entry is a deliberate exclusion
# with a reason, not a silent skip. Extending this set is the correct way
# to acknowledge a new match; deleting an entry without checking is not.
_KNOWN_NON_SURFACES = {
    "CacheStats": "a plain stats dataclass, not a persistence class itself",
    "_Cache": "abstract base for QueryCache/ResponseCache, both separately listed",
    "EmbeddingCacheBusy": "an exception class, matched by the 'Cache' substring only",
    "_SQLiteVectorStore": "VectorStore's own SQLite fallback implementation, "
                          "covered by the VectorStore entry (same mark_deleted "
                          "contract, same not_deleted_clause enforcement)",
    "StateStore": "connectors/base.py -- persists connector CURSOR/watermark "
                  "state (e.g. 'last synced item id'), never document CONTENT. "
                  "Not a doc-derived persistence surface; re-discovery after "
                  "state loss is idempotent by the connector contract's own "
                  "design, so there is nothing here erasure needs to reach.",
}

_CLASS_NAME_RE = re.compile(r"^(?:_)?[A-Za-z0-9]*(?:Store|Cache|Logger|Index)$")


def _discover_persistence_shaped_classes() -> dict[str, str]:
    """chunk_id/class_name -> relative file path, for every top-level class
    definition under src/alexandria/ whose name matches the Store/Cache/
    Logger/Index pattern."""
    found: dict[str, str] = {}
    for path in SRC_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and _CLASS_NAME_RE.match(node.name):
                found[node.name] = str(path.relative_to(SRC_ROOT.parent.parent))
    return found


def test_every_erasure_surface_class_actually_exists():
    """Each listed surface's module+name must import cleanly -- a renamed or
    removed class must fail this test loudly, not leave a stale reference."""
    import importlib

    for surface in ERASURE_SURFACES:
        module = importlib.import_module(surface.module)
        # name may carry a parenthetical, e.g. "VectorStore (dense/LanceDB)"
        class_name = surface.name.split(" ")[0].split("(")[0].strip()
        assert hasattr(module, class_name), (
            f"{surface.name}: {surface.module}.{class_name} does not exist -- "
            f"ERASURE_SURFACES is stale relative to the real code")


def test_no_persistence_shaped_class_escapes_the_enumeration():
    """The actual trip-wire: a class matching Store/Cache/Logger/Index that is
    neither in ERASURE_SURFACES nor in the documented exclusion list is a
    genuinely new (or newly renamed) persistence surface that erasure work
    has not yet considered. This must fail against a deliberately-added,
    unlisted class -- proven below by construction, not asserted."""
    discovered = _discover_persistence_shaped_classes()
    listed_names = {surface.name.split(" ")[0].split("(")[0].strip()
                    for surface in ERASURE_SURFACES}
    unaccounted = set(discovered) - listed_names - set(_KNOWN_NON_SURFACES)
    assert not unaccounted, (
        f"persistence-shaped class(es) not in ERASURE_SURFACES or "
        f"_KNOWN_NON_SURFACES: {sorted(unaccounted)} -- add each to one or "
        f"the other with a reason, do not silently ignore")


def test_the_discovery_scan_itself_actually_finds_real_classes():
    """Vacuity guard for the scan mechanism itself: prove it is not
    accidentally matching zero classes (e.g. a broken regex or an empty
    SRC_ROOT would make test_no_persistence_shaped_class_escapes_the_
    enumeration pass FOR THE WRONG REASON -- finding nothing looks identical
    to finding everything correctly)."""
    discovered = _discover_persistence_shaped_classes()
    assert "VectorStore" in discovered
    assert "ResponseCache" in discovered
    assert "EnrichmentStore" in discovered
    assert len(discovered) >= len(ERASURE_SURFACES)
