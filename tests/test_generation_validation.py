from pathlib import Path


def test_staged_generation_validation_does_not_require_a_git_checkout(tmp_path: Path) -> None:
    """Generation-local evidence must work in a deliberately Git-free stage."""
    from alexandria.generation_validation import validate_generation

    stage = tmp_path / "stage"
    (stage / "sources").mkdir(parents=True)
    (stage / "wiki").mkdir()
    (stage / "sources" / "canary.md").write_text("---\ntitle: A searchable staging canary\n---\nbody\n")
    (stage / ".alexandria" / "index").mkdir(parents=True)
    (stage / ".alexandria" / "index" / "generation.json").write_text('{"generation": 2}')

    evidence = validate_generation(stage, docs_before=0, generation_before=1)

    assert evidence["documents"] == 1
    assert evidence["generation"] == 2
    assert ".git" not in str(evidence)
