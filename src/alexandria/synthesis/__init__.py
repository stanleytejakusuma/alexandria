"""Single-page synthesis pipeline: gather, write, judge, repair, and emit."""

from .gather import GatherResult, SourceChunk, gather
from .pipeline import PipelineResult, run_pipeline
from .write import Claim, Citation, SynthesisPage, write_page

__all__ = [
    "Claim",
    "Citation",
    "GatherResult",
    "PipelineResult",
    "SourceChunk",
    "SynthesisPage",
    "gather",
    "run_pipeline",
    "write_page",
]
