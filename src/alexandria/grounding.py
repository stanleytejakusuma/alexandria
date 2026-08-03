"""Deterministic grounding check -- does every hard specific in a note appear in its
transcript?

This exists because an LLM grader cannot be trusted unsupervised. In a real audit run
the grader marked a note "fabricated" for claiming links were *undirected* when both
the note and the transcript said *directed* -- it invented the discrepancy it was
punishing. A model-free check has no such failure mode, needs no sampling, and runs
over the whole corpus for nothing.

It deliberately checks only HARD SPECIFICS -- identifiers, paths, numbers, versions,
hostnames -- the things that cannot be paraphrased and must be copied to be correct.
Prose claims are not checked here; they are what the LLM grader is for. So:

    ungrounded specific  => strong evidence of invention
    everything grounded  => NOT proof of faithfulness (misattribution survives)

An asymmetric test, used in the direction where it is sound.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = ["Grounding", "check_note", "SPECIFIC_RE"]

# Tokens that must be copied, not paraphrased, to be correct.
SPECIFIC_RE = re.compile(
    r"""(?:
      \b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b          # ipv4, optional port
    | \b[\w.-]+\.(?:py|ts|js|md|json|toml|yaml|yml|sh|service|db|jsonl|cjs|plist)\b
    | \b[a-z][a-z0-9]*(?:[_-][a-z0-9]+){1,}\b       # snake_case / kebab-case
    | \b[a-z]+(?:[A-Z][a-z0-9]+){1,}\b              # camelCase
    | \bv?\d+\.\d+(?:\.\d+)*\b                      # versions
    | \b\d{3,}\b                                    # counts/ports/ids
    )""",
    re.VERBOSE,
)

# Words that look specific but are generic vocabulary, not claims about the session.
STOPLIST = frozenset({
    "read-only", "write-only", "end-to-end", "fail-safe", "fail-closed", "up-to-date",
    "self-check", "follow-up", "one-shot", "high-level", "low-level", "long-running",
    "well-designed", "over-engineered", "single-turn", "multi-step", "per-turn",
    "first-class", "side-effect", "round-trip", "human-readable", "machine-readable",
    "so-called", "on-disk", "in-place", "real-time", "open-source", "built-in",
})


@dataclass
class Grounding:
    note_id: str
    title: str = ""
    specifics: list[str] = field(default_factory=list)
    ungrounded: list[str] = field(default_factory=list)

    @property
    def checked(self) -> int:
        return len(self.specifics)

    @property
    def grounded(self) -> bool:
        return not self.ungrounded

    @property
    def rate(self) -> float:
        if not self.specifics:
            return 1.0
        return 1.0 - len(self.ungrounded) / len(self.specifics)


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def check_note(note_id: str, title: str, body: str, entities: list[str],
               transcript: str) -> Grounding:
    """Every hard specific in the note must appear somewhere in the transcript.

    Matching is punctuation-insensitive: a note may legitimately write `capital-gate.ts`
    where the transcript wrote `capital_gate.ts`, and penalising that would manufacture
    exactly the false positives this check exists to avoid.
    """
    haystack = _normalize(transcript)
    seen: set[str] = set()
    g = Grounding(note_id=note_id, title=title)

    candidates = list(entities or []) + SPECIFIC_RE.findall(f"{title}\n{body}")
    for raw in candidates:
        token = str(raw).strip()
        if not token or token.lower() in STOPLIST:
            continue
        norm = _normalize(token)
        if len(norm) < 4 or norm in seen:
            continue
        seen.add(norm)
        g.specifics.append(token)
        if norm not in haystack:
            g.ungrounded.append(token)
    return g
