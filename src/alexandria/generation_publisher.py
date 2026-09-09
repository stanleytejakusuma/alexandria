"""Build and publish complete corpus generations under a stable control root."""
from __future__ import annotations

import fcntl
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from .generation_pointer import GenerationPointerError, activate_generation, resolve_generation
from .generation_validation import validate_generation

__all__ = ["PublishError", "publish_generation", "stage_generation"]


class PublishError(RuntimeError):
    """A staged generation could not be safely made live."""


def _generations_root(control_root: Path) -> Path:
    return control_root / ".alexandria" / "generations"


def _copy_missing(source: Path, destination: Path) -> None:
    """Copy only what `destination` lacks, leaving existing files untouched."""
    destination.mkdir(parents=True, exist_ok=True)
    for entry in source.rglob("*"):
        target = destination / entry.relative_to(source)
        if entry.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry, target)


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
    # Reader inputs are the UNION of the selected generation and the control
    # root, in that order of precedence.
    #
    # 2026-09-09: copying from the generation alone silently dropped four
    # standing-convention documents that had been written to the control root --
    # on disk, in no generation, in no index, unreachable by search. Copying
    # from the control root alone is worse: 897 reconciled documents existed
    # only in the active generation and would have been dropped instead.
    # Neither side is authoritative on its own, so take both.
    #
    # The generation wins a collision: it is what readers currently see, and
    # promotion rewrites a document in place there.
    pointer_exists = (control_root / ".alexandria" / "current-generation.json").exists()
    source_roots: tuple[Path, ...] = (
        (resolve_generation(control_root), control_root) if pointer_exists else (control_root,)
    )
    try:
        staged.mkdir(parents=True)
        # `inbox` and `.alexandria/pending` travel too: an entry that has been
        # appended but not yet promoted must survive a cutover, or /remember
        # silently loses writes that arrive mid-publish.
        for relative in (
            Path("sources"),
            Path("wiki"),
            Path("inbox"),
            Path(".alexandria") / "state",
            Path(".alexandria") / "pending",
        ):
            for index, source_root in enumerate(source_roots):
                source = source_root / relative
                if not source.exists():
                    continue
                if index == 0:
                    shutil.copytree(source, staged / relative)
                    continue
                # Lower-precedence roots may only FILL GAPS. shutil.copytree
                # with dirs_exist_ok overwrites, which would let a stale
                # control-root copy clobber a promoted generation document.
                _copy_missing(source, staged / relative)
        # The rebuilt staged index is independent, but its generation must be
        # monotonic relative to the live generation so liveness checks can
        # distinguish a successful staged rebuild from a reset counter.
        # The derived index/cache are large (15GB/6.9GB on the Mac) but needed
        # for incremental staging. APFS clones give private copy-on-write
        # mutations without a multi-hour byte copy. Fail closed elsewhere: a
        # normal recursive copy defeats the loop's bounded-liveness contract.
        for name in ("index", "cache"):
            source = source_roots[0] / ".alexandria" / name
            if source.exists():
                destination = staged / ".alexandria" / name
                try:
                    subprocess.run(["/bin/cp", "-cR", str(source), str(destination)], check=True)
                except (OSError, subprocess.CalledProcessError) as exc:
                    raise PublishError(f"cannot clone staged {name}; refusing an unbounded byte copy") from exc
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
    lock_path = control_root / ".alexandria" / "generation-publish.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
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
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
