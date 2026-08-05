"""Skip-log audit -- does an omitted chunk contradict or qualify a synthesized claim?

The Judge-2 coverage grader, per docs/RUBRIC-skip-log-audit.md and
docs/SPEC-phase2-eval.md. Where audit.py catches a page saying something its
sources don't support (fabrication), this catches the mirror failure: a page
that's technically faithful in everything it states, while silently dropping a
gathered-but-uncited fact that contradicts or materially qualifies a claim it
does make (the "Goodhart gap" -- citation lint only punishes saying something
false, never omitting something true that mattered).

Same two rules as audit.py, for the same reason: a different model grades than
wrote the page, and the falsifiability mechanism from the rubric's section 0
carries over directly -- a verdict of LB (load-bearing omission) is only valid
if the grader exhibits the specific (claim, fact, relation) triple. No triple,
no LB.

Verdicts: LB (load-bearing omission, a real miss) / SS (safe skip, coded per
Appendix A) / borderline (the rubric's own designated escape hatch for the
Tier-2/3 materiality boundary -- calibration section 5 tracks this as its own
category, not folded into either side).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .eval.calibration_cases import LABEL_CODES
from .llm import LLMError

__all__ = ["SkipVerdict", "grade_skip", "GRADER_SYSTEM"]

LABELS = frozenset({"LB", "SS", "borderline"})

GRADER_SYSTEM = """You are auditing whether a page silently omitted a load-bearing fact.

You will see PAGE_CLAIMS (a synthesized page's stated, cited claims) and a
SKIPPED_CHUNK (a real source chunk the synthesis sweep gathered but did not cite).
Decide: does the skipped chunk contain a fact that CONTRADICTS or MATERIALLY
QUALIFIES a claim actually stated on the page?

Decision procedure:
1. Does any claim on the page address the same subject as the skipped chunk?
   If no -- label SS, label_code "SS:no_target_claim". This audit only judges
   faithfulness to what the page actually says, never completeness of what it
   could have said (that is a separate recall check).
2. If yes, does the chunk state the opposite of a claim, or a fact making it
   false under standard reading (direct negation, mutual exclusivity, or an
   explicit correction/supersession)? If yes -- label LB, one of
   "LB:contradiction:direct", "LB:contradiction:mutual_exclusive", or
   "LB:contradiction:superseded".
3. If not a contradiction, does the chunk add a necessary condition, scope
   bound, exception, severity modifier, temporal bound, dependency, or
   confidence caveat that changes what a reasonable reader would infer from
   the claim alone? If yes -- label LB, one of "LB:qualification:scope",
   "LB:qualification:exception", "LB:qualification:severity",
   "LB:qualification:temporal", "LB:qualification:dependency", or
   "LB:qualification:confidence".
4. Otherwise -- label SS. Use "SS:near_duplicate" if the chunk just restates a
   cited claim with no added condition; "SS:trivial" if it's true and attached
   but doesn't change what a reader would decide or believe; "SS:superseded"
   if a conflict exists but the corpus contains an explicit, dated correction
   already reflected on the page; "SS:tangential" otherwise.

Two rules that keep this honest:
- Present-tense claims describing an ongoing/active state ("is blocked",
  "undermines") DO need a later resolution surfaced if the chunk reports one --
  this is not "no forward permanence claim," it is "the reader has no signal
  this claim is stale." Claims explicitly anchored to a named historical event
  ("the audit on [date] found X") are genuinely unresolved by this rubric --
  if you cannot confidently resolve which way that cuts, say "borderline"
  rather than guess.
- credibility is judged by CONTENT alone. Do not discount a skipped fact as
  "probably the stale one" just because it looks older or less polished --
  that reintroduces exactly the silent source-preference this audit exists to
  catch. Only treat a conflict as discharged if the chunk or page explicitly
  documents a correction, not if you merely suspect one exists.

Your LB verdict is only valid if you can exhibit the specific triple: the exact
page claim (claim), the exact fact from the skipped chunk (fact), and the
relation between them (relation) -- one sentence naming which rule above fired.
If you cannot write down a concrete claim and fact, the label is SS, not LB.

Reply with ONLY a JSON object: {"label": "LB"|"SS"|"borderline",
"label_code": "<one of the codes above>", "claim": "...", "fact": "...",
"relation": "..."}. No prose outside the JSON."""

USER_TEMPLATE = """<page_claims>
{page_claims}
</page_claims>

<skipped_chunk>
{skipped_chunk}
</skipped_chunk>"""


def _unfence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    return text.strip()


@dataclass(frozen=True)
class SkipVerdict:
    """One grader verdict on a (page_claims, skipped_chunk) pair."""

    case_id: str
    label: str
    label_code: str
    claim: str
    fact: str
    relation: str


def grade_skip(llm, page_claims: str, skipped_chunk: str, case_id: str) -> SkipVerdict:
    """Grade one (page_claims, skipped_chunk) pair. A grader failure is recorded, never
    silently counted as a pass -- same discipline as audit.py's grade_note: an eval
    that cannot fail correctly is worse than no eval.
    """
    prompt = USER_TEMPLATE.format(page_claims=page_claims, skipped_chunk=skipped_chunk)
    try:
        raw = llm.complete(GRADER_SYSTEM, prompt)
        data = json.loads(_unfence(raw))
        label = str(data["label"]).strip()
        label_code = str(data["label_code"]).strip()
        claim = str(data["claim"])
        fact = str(data["fact"])
        relation = str(data["relation"])
        if label not in LABELS:
            raise ValueError(f"bad label {label!r}")
        if label_code not in LABEL_CODES:
            raise ValueError(f"bad label_code {label_code!r}")
        code_parent = label_code.split(":", 1)[0]
        if label == "SS" and code_parent != "SS":
            raise ValueError(f"label_code {label_code!r} does not match label {label!r}")
        if label == "LB" and code_parent != "LB":
            raise ValueError(f"label_code {label_code!r} does not match label {label!r}")
        return SkipVerdict(case_id, label, label_code, claim, fact, relation)
    except (LLMError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise LLMError(f"grader failed on {case_id}: {type(exc).__name__}: {exc}") from exc
