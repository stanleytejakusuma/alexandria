# Synthetic corpus — a fictional public library

Sixteen invented documents. Nothing here describes any real organisation,
system, or person. They exist so the eval harness has something to measure that
can live in a public repo (BACKLOG #20); the real golden set names private
projects and cannot be published.

`README.md` sits outside `sources/` deliberately: `is_indexable_source()` only
walks `sources/` and `wiki/`, so this file is never chunked and cannot become a
retrieval target.

## What it is designed to do: fail

A fixture where every query lands its answer at rank 1 proves nothing — there is
no room below the answer for a broken scorer to push it into. Measured on this
corpus, 29% of hits are at rank 1 and recall@k is 0.950, with two golden entries
missing outright. That headroom is the point, and
`test_fixture_retains_discriminating_power` fails if someone closes it by
rewriting queries to quote their targets.

Three traps are built in:

- **A near-duplicate pair.** `policy/renewals-main.md` and
  `policy/renewals-annex.md` state the same five rules with different numbers and
  share nearly all their vocabulary; only a branch name separates them. A scorer
  that degrades toward document length or term frequency alone still clears the
  recall floor while confidently returning the wrong branch's rules.
- **Shared vocabulary across distinct documents.** `overdue-fines`,
  `fine-appeals` and `lost-item-replacement` all discuss charges, thresholds and
  ceilings; `renewals-*` and `borrowing-limits` all state loan periods and
  unpaid-charge blocks. Several golden queries can only be resolved by which
  document *defines* a thing rather than merely mentions it.
- **Contradiction rather than absence.** The annex document says there is no
  telephone renewal line. A query about the telephone line therefore has a
  strong lexical match in exactly the wrong document — and today it wins. See
  the KNOWN WEAKNESS note in `tests/test_synthetic_gate.py`.

## Negatives

`../synthetic-negative-v1.jsonl` is 10-of-12 **in-domain**: questions a library
plausibly answers that these sixteen documents happen not to cover (the
acquisitions budget, the receipt-paper vendor, bank-holiday hours). Two
out-of-domain controls are kept for contrast.

This is a deliberate correction to BACKLOG #22: the private negative set is 21 of
22 out-of-domain brand queries, which makes its negative score median a
measurement of topic distance rather than of precision.

## What a green run means

That the instrument works. Not that retrieval is good — the embedder used here is
`HashEmbedder`, which is deterministic and semantically empty, so the dense leg
of the hybrid is noise by construction and every point of recall is earned by
BM25 and fusion. See `src/alexandria/eval/synthetic.py` for the full boundary.
