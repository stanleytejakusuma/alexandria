from pathlib import Path

from alexandria.config import load_config
from alexandria.generation_pointer import activate_generation


def test_config_resolves_a_control_root_to_its_active_generation(tmp_path: Path) -> None:
    generation = tmp_path / ".alexandria" / "generations" / "g-1"
    generation.mkdir(parents=True)
    activate_generation(tmp_path, generation)

    assert load_config(corpus_override=tmp_path).corpus_path == generation


def test_config_keeps_legacy_corpus_path_without_a_generation_pointer(tmp_path: Path) -> None:
    assert load_config(corpus_override=tmp_path).corpus_path == tmp_path
