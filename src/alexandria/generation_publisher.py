"""Build and publish complete corpus generations under a stable control root."""
from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from .generation_pointer import GenerationPointerError, activate_generation, resolve_generation
from .generation_validation import validate_generation

__all__ = ["PublishError", "publish_generation", "stage_generation"]


class PublishError(RuntimeError):
    """A staged generation could not be safely made live."""


def _generations_root(control_root: Path) -> Path:
    return control_root / ".alexandria" / "generations"


def stage_generation(control_root: str | Path, generation_id: str) -> Path:
    """Copy only reader inputs into a fresh, unpublished generation.

    Git history remains at the control root. Caches and locks are runtime
    implementation detail, not a reader-visible generation input.
    """
    control_root = Path(control_root)
    if not generation_id or Path(generation_id).name != generation_id:
        raise PublishError("generation id must be one path component")
    staged = _generations_root(control_root) / generation_id
    if staged.exists():
        raise PublishError(f"generation already exists: {generation_id}")
    try:
        staged.mkdir(parents=True)
        for relative in (Path("sources"), Path("wiki"), Path(".alexandria") / "state"):
            source = control_root / relative
            if source.exists():
                shutil.copytree(source, staged / relative)
        return staged
    except Exception:
        shutil.rmtree(staged, ignore_errors=True)
        raise


def publish_generation(
    control_root: str | Path,
    staged: str | Path,
    *,
    snapshot: Callable[[Path], None],
) -> Path:
    """Snapshot a complete staged generation, then atomically select it.

    The callback is deliberately injected: Git policy belongs to the stable
    control root and will be integrated only after its operator contract is
    tested. A failed snapshot leaves the current pointer untouched.
    """
    control_root = Path(control_root)
    staged = Path(staged).resolve()
    try:
        try:
            previous = resolve_generation(control_root)
        except GenerationPointerError:
            docs_before = generation_before = 0
        else:
            previous_evidence = validate_generation(previous, docs_before=0, generation_before=0)
            docs_before = previous_evidence["documents"]
            generation_before = previous_evidence["generation"]
        validate_generation(staged, docs_before=docs_before, generation_before=generation_before)
        snapshot(staged)
        return activate_generation(control_root, staged)
    except (GenerationPointerError, ValueError) as exc:
        raise PublishError(f"generation validation failed; staged generation was not published: {exc}") from exc
    except Exception as exc:
        raise PublishError(f"snapshot failed; staged generation was not published: {exc}") from exc
