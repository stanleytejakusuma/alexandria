"""#51: ingest ANY artifact as a memory, without disturbing retrieval.

The pensieve principle: a PDF, a screenshot, a research paper or a resume must
all be storable and recallable. The architectural constraint that makes this
cheap is a strict separation:

    the ORIGINAL binary is preserved      -> assets/<sha256[:2]>/<sha256>.<ext>
    a COMPANION markdown carries the text -> sources/assets/<slug>-<sha8>.md
    ONLY the markdown is indexed

Because the indexer and /health both walk ``rglob("*.md")`` (cli.py, serve.py),
a stored binary is invisible to them by construction -- so the vector space,
manifest, Embedder protocol, LanceDB search, fusion and reranker are all
untouched. That is the whole reason this does not need a ColPali-style
multi-vector rewrite (see backlog #53 for the trigger that would justify one).

Extraction routes by artifact kind:
  born-digital PDF -> pdftotext (lossless, offline, no new dependency)
  image            -> a vision model, injected as ``describe_image``

Every failure mode refuses loudly. An ingest that produced an empty companion
would index cleanly and then answer nothing forever -- the exact
"reported success while doing nothing" class this project keeps finding.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .corpus import Doc, slugify

__all__ = [
    "ExtractionFailed",
    "IngestResult",
    "UnsupportedArtifact",
    "ingest_path",
]

# Artifacts whose text lives in a PDF text layer.
PDF_SUFFIXES = frozenset({".pdf"})
# Artifacts with no text layer: they need a vision model to become words.
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"})

ASSET_ROOT = "assets"
COMPANION_ROOT = "sources/assets"


class UnsupportedArtifact(Exception):
    """The file type has no extraction route yet."""


class ExtractionFailed(Exception):
    """Extraction produced nothing usable, so nothing was written.

    Deliberately fatal rather than a degraded write: a companion with no text
    is a memory that can never be recalled, and it would pass every downstream
    health check while doing so.
    """


@dataclass(frozen=True)
class IngestResult:
    """Corpus-relative paths produced by one ingest."""

    asset_path: str      # the preserved original, e.g. assets/ab/<sha>.pdf
    doc_path: str        # the indexed companion, e.g. sources/assets/x-ab12cd34.md
    extraction: str      # "pdftotext" | "vision"
    sha256: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _extract_pdf_text(path: Path) -> str:
    """Text layer via pdftotext. Lossless, offline, and already installed.

    A born-digital PDF is exactly the case the literature says a text pipeline
    wins (vs. rendering pages for a vision model), so this is both the cheaper
    and the higher-quality route -- when a text layer exists at all.
    """
    try:
        out = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            capture_output=True, text=True, timeout=120,
        )
    except FileNotFoundError as exc:
        raise ExtractionFailed(
            f"pdftotext is not installed, so {path.name} cannot be read") from exc
    except subprocess.SubprocessError as exc:
        raise ExtractionFailed(f"pdftotext failed on {path.name}: {exc}") from exc
    if out.returncode != 0:
        # A damaged PDF can emit partial stdout AND a non-zero code. Storing
        # that fragment would preserve a truncated memory as if it were whole.
        raise ExtractionFailed(
            f"pdftotext failed on {path.name}: {(out.stderr or '').strip()[:200]}")
    return out.stdout.strip()


def _describe_image_via_skill(path: Path) -> str:  # pragma: no cover - needs a gateway
    """Default vision route: the installed image-digest skill.

    Imported lazily and called through a subprocess boundary so the engine
    keeps no hard dependency on a harness skill -- Alexandria stays usable from
    any harness (see docs/HTTP-API.md), and a missing skill degrades to a clean
    ExtractionFailed instead of an import error at module load.
    """
    try:
        out = subprocess.run(
            ["image-digest", str(path)],
            capture_output=True, text=True, timeout=300,
        )
    except FileNotFoundError as exc:
        raise ExtractionFailed(
            f"no vision extractor available for {path.name}") from exc
    except subprocess.SubprocessError as exc:
        raise ExtractionFailed(f"vision extraction failed on {path.name}: {exc}") from exc
    if out.returncode != 0:
        raise ExtractionFailed(
            f"vision extraction failed on {path.name}: {out.stderr.strip()[:200]}")
    return out.stdout.strip()


def ingest_path(
    source: str | Path,
    corpus: str | Path,
    *,
    describe_image: Callable[[Path], str] | None = None,
    title: str | None = None,
) -> IngestResult:
    """Preserve one artifact and write its indexed companion.

    ``describe_image`` is injectable so tests stay offline and so any harness
    can supply its own vision route.
    """
    source, corpus = Path(source).expanduser(), Path(corpus).expanduser()
    if not source.is_file():
        raise UnsupportedArtifact(f"{source}: not a file")

    suffix = source.suffix.lower()
    if suffix in PDF_SUFFIXES:
        extraction = "pdftotext"
    elif suffix in IMAGE_SUFFIXES:
        extraction = "vision"
    else:
        raise UnsupportedArtifact(
            f"{source.name}: no extraction route for {suffix or 'files without a suffix'}")

    # EXTRACT FIRST, write nothing until there is something worth recalling.
    # Ordering matters: a failure here must leave no stranded asset behind.
    if extraction == "pdftotext":
        text = _extract_pdf_text(source)
    else:
        describe = describe_image or _describe_image_via_skill
        try:
            text = (describe(source) or "").strip()
        except ExtractionFailed:
            raise
        except Exception as exc:  # any transport/gateway failure is a refusal
            raise ExtractionFailed(
                f"vision extraction failed on {source.name}: {exc}") from exc

    if not text:
        raise ExtractionFailed(
            f"{source.name}: extraction produced no text, so nothing was indexed "
            f"(a companion with no body is a memory that can never be recalled)")

    digest = _sha256(source)
    # Content-addressed: identical bytes are one memory, however many times or
    # under whatever name they are handed to us.
    asset_rel = f"{ASSET_ROOT}/{digest[:2]}/{digest}{suffix}"
    asset_abs = corpus / asset_rel
    asset_abs.parent.mkdir(parents=True, exist_ok=True)
    # ATOMIC, and verified on the dedup path. A torn copy straight to the
    # content-addressed name would be PERMANENT: every later ingest of the same
    # bytes sees the file exists and skips the repair forever, while the
    # companion asserts a sha256 those bytes do not have. Preservation is this
    # component's whole job, so it writes to a sibling temp and renames, and
    # re-copies anything already there whose digest does not match.
    if not asset_abs.exists() or _sha256(asset_abs) != digest:
        tmp_asset = asset_abs.with_name(f".{asset_abs.name}.partial")
        try:
            shutil.copy2(source, tmp_asset)
            tmp_asset.replace(asset_abs)
        finally:
            tmp_asset.unlink(missing_ok=True)

    # One artifact is ONE memory: the same bytes handed in under a second name
    # must reuse the existing companion rather than create a twin carrying an
    # identical (source, source_id). Two docs claiming one identity is the
    # duplicate shape that produced a phantom shortfall before.
    companion_dir = corpus / COMPANION_ROOT
    existing = sorted(companion_dir.glob(f"*-{digest[:8]}.md")) if companion_dir.is_dir() else []
    if existing:
        doc_rel = existing[0].relative_to(corpus).as_posix()
    else:
        stem = slugify(source.stem) or "artifact"
        doc_rel = f"{COMPANION_ROOT}/{stem}-{digest[:8]}.md"
    Doc(
        path=doc_rel,
        frontmatter={
            "type": "doc",
            "title": title or source.stem,
            "source": "ingest",
            "source_id": digest[:16],
            "generated": {"by": "connector/ingest",
                          "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")},
            # Provenance is what makes an ingested memory auditable later, and
            # `asset` is the pointer that lets a search hit open the original.
            "ingest": {
                "original_name": source.name,
                "original_path": str(source),
                "sha256": digest,
                "extraction": extraction,
                "asset": asset_rel,
                "bytes": source.stat().st_size,
            },
        },
        body=text if text.endswith("\n") else text + "\n",
    ).write(corpus)

    return IngestResult(asset_path=asset_rel, doc_path=doc_rel,
                        extraction=extraction, sha256=digest)
