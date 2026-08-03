"""Deterministic grounding: catches invented specifics without an LLM."""

from alexandria.grounding import check_note


def g(body, transcript, entities=None, title="T"):
    return check_note("n1", title, body, entities or [], transcript)


def test_invented_specifics_are_caught():
    r = g("The service runs on 192.0.2.99 and uses capital_gate.ts",
          "The service was discussed but no address was given.")
    assert not r.grounded
    assert "192.0.2.99" in r.ungrounded


def test_grounded_specifics_pass():
    r = g("It runs on 127.0.0.1:8377 via lancedb_store.py",
          "we bound it to 127.0.0.1:8377 in lancedb_store.py yesterday")
    assert r.grounded, r.ungrounded


def test_punctuation_variants_are_not_false_positives():
    """A note may write capital-gate.ts where the transcript wrote capital_gate.ts.
    Penalising that manufactures the false positives this check exists to avoid."""
    assert g("touched capital-gate.ts", "we edited capital_gate.ts").grounded


def test_generic_hyphenated_vocabulary_is_not_treated_as_a_specific():
    r = g("The agent is read-only and fail-safe, an end-to-end check.",
          "nothing relevant here at all")
    assert r.checked == 0
    assert r.grounded


def test_entities_are_checked_too():
    r = g("body text", "transcript mentions alpha only", entities=["alpha", "omega"])
    assert "omega" in r.ungrounded
    assert "alpha" not in r.ungrounded


def test_rate_is_reported():
    r = g("uses foo_bar.py and baz_qux.py", "only foo_bar.py appears")
    assert 0.0 < r.rate < 1.0


def test_note_with_no_specifics_is_vacuously_grounded():
    r = g("A purely prose observation about the design.", "unrelated transcript")
    assert r.grounded and r.checked == 0


def test_the_okf_false_positive_the_llm_grader_produced():
    """Regression on the real case: the grader claimed the note said 'undirected'
    while both note and transcript said 'directed'. A model-free check cannot
    invent a discrepancy."""
    note = "Link graph: untyped, all links treated as directed edges of untyped relationship"
    transcript = "Consumers that build a graph view typically treat all links as directed edges of an untyped relationship."
    assert g(note, transcript).grounded
