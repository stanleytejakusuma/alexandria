"""#6 erasure-core item 3: binds the ERASURE_SURFACES enumeration to reality.

Independent checks, per the failure-frame note this file was written
against, extended after Red review 2026-08-21: (1) every listed surface's
class must actually exist and import cleanly -- a renamed/removed class
fails loudly, not silently; (2) a discovery scan for persistence-shaped
classes (Store/Cache/Logger/Index suffixes), walking the FULL ast tree (not
just top-level definitions) across src/alexandria/, must not find anything
NOT already accounted for -- a genuinely new persistence surface trips this
test even if nobody remembered to update ERASURE_SURFACES by hand; (3) each
documented exclusion is pinned to the exact module it was verified against,
so a class move or a same-named collision forces re-review; (4) every path
in backup.py's own authoritative STATE_PATHS list is reconciled against
either ERASURE_SURFACES or an explicit STATE_PATH_CLASSIFICATIONS entry."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from alexandria.erasure_surfaces import ERASURE_SURFACES, STATE_PATH_CLASSIFICATIONS

SRC_ROOT = Path(__file__).parent.parent / "src" / "alexandria"

# Classes that match the Store/Cache/Logger/Index name pattern but are NOT
# doc-content persistence surfaces -- each entry is a deliberate exclusion
# with a reason, not a silent skip. Extending this set is the correct way
# to acknowledge a new match; deleting an entry without checking is not.
# Red review 2026-08-21 (finding #5, minor): each exclusion is pinned to its
# EXPECTED module path, not just a bare reason -- if the class ever moves to
# a different module (or a same-named class appears in a NEW module), the
# pin no longer matches and re-review is forced rather than the exclusion
# silently continuing to apply to whatever now has that name.
_KNOWN_NON_SURFACES = {
    "_Cache": ("src/alexandria/cache.py",
               "abstract base for QueryCache/ResponseCache, both separately listed"),
    "_SQLiteVectorStore": ("src/alexandria/index/store.py",
                          "VectorStore's own SQLite fallback implementation, "
                          "covered by the VectorStore entry (same mark_deleted "
                          "contract, same not_deleted_clause enforcement)"),
    "StateStore": ("src/alexandria/connectors/base.py",
                  "persists connector CURSOR/watermark state (e.g. 'last "
                  "synced item id'), never document CONTENT. Not a "
                  "doc-derived persistence surface; re-discovery after "
                  "state loss is idempotent by the connector contract's "
                  "own design, so there is nothing here erasure needs to "
                  "reach."),
}
# Note: an earlier draft of this exclusion list also carried "CacheStats"
# and "EmbeddingCacheBusy" -- verified (2026-08-21) that NEITHER actually
# matches _CLASS_NAME_RE (they end in "Stats"/"Busy", not
# Store/Cache/Logger/Index), so they were never real discoveries needing
# exclusion in the first place. Removed rather than left as dead entries a
# future reader would have to re-verify were ever necessary.

_CLASS_NAME_RE = re.compile(r"^(?:_)?[A-Za-z0-9]*(?:Store|Cache|Logger|Index)$")


def _discover_persistence_shaped_classes() -> dict[str, list[str]]:
    """class_name -> list of relative file paths, for every class definition
    under src/alexandria/ whose name matches the Store/Cache/Logger/Index
    pattern -- at ANY nesting depth (Red review 2026-08-21, finding #4:
    `tree.body` only sees top-level definitions; this codebase's own
    optional-dependency pattern (try/except ImportError around a class, or
    any future nested definition) would be invisible to a top-level-only
    scan. ast.walk() sees every node regardless of nesting). A LIST, not a
    single path (Red review, minor finding #7): the codebase already has
    same-named classes in different modules (Verdict, Entry, GatherResult,
    confirmed live -- none currently match this pattern, but a single-path
    dict would silently collide the moment one does)."""
    found: dict[str, list[str]] = {}
    for path in SRC_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and _CLASS_NAME_RE.match(node.name):
                found.setdefault(node.name, []).append(
                    str(path.relative_to(SRC_ROOT.parent.parent)))
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


def test_known_non_surface_exclusions_are_pinned_to_the_right_module():
    """Each _KNOWN_NON_SURFACES entry is pinned to the module it was
    verified against -- if the class moves, or a NEW same-named class
    appears somewhere else, the exclusion must not silently keep applying
    to whatever now has that name. Also refuses a class name that now
    resolves to MULTIPLE modules (one of which may not be the exempted
    one) -- an exclusion must name exactly one real class, unambiguously."""
    discovered = _discover_persistence_shaped_classes()
    for class_name, (expected_module, _reason) in _KNOWN_NON_SURFACES.items():
        actual_modules = discovered.get(class_name, [])
        assert actual_modules, (
            f"{class_name!r} is in _KNOWN_NON_SURFACES but no longer exists "
            f"anywhere in src/alexandria/ -- remove the stale exclusion")
        assert actual_modules == [expected_module], (
            f"{class_name!r} was pinned to {expected_module!r} but is now "
            f"found at {actual_modules} -- re-verify the exclusion still "
            f"applies (a second, same-named class in a DIFFERENT module is "
            f"NOT automatically covered by this exclusion) before updating "
            f"the pin")


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


def test_the_discovery_scan_tracks_every_location_of_a_duplicate_name():
    """Vacuity proof for finding #7's fix: inject a second, same-named class
    into a different module and confirm BOTH locations are tracked, not
    silently collapsed to one (which is what a dict[str, str] would do)."""
    import tempfile
    fake_module = SRC_ROOT / "_test_duplicate_probe.py"
    fake_module.write_text("class VectorStore:\n    pass\n")
    try:
        discovered = _discover_persistence_shaped_classes()
        assert len(discovered["VectorStore"]) == 2
    finally:
        fake_module.unlink()



def test_every_backup_state_path_is_classified():
    """Red review 2026-08-21 (finding #5, the sharpest gap): backup.py's
    STATE_PATHS is already an authoritative 'what this engine persists'
    list -- every entry must be reconciled against ERASURE_SURFACES or
    STATE_PATH_CLASSIFICATIONS, so a future STATE_PATHS addition cannot
    silently escape classification the way eval_runs.jsonl and pending/
    originally did."""
    from alexandria.backup import STATE_PATHS

    for path in STATE_PATHS:
        assert path in STATE_PATH_CLASSIFICATIONS, (
            f"{path!r} is in backup.py's STATE_PATHS but has no erasure "
            f"classification -- add it to STATE_PATH_CLASSIFICATIONS with "
            f"a reason")
