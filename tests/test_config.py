"""Retrieval configuration has local defaults and explicit env precedence."""

from pathlib import Path

from alexandria.config import load_config


def test_config_file_loads_retrieval_defaults_and_environment_wins(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[corpus]\npath = 'configured-corpus'\n"
        "[embed]\nprovider = 'hash'\nbatch_size = 7\n"
        "[search]\nwiki_boost = 1.5\n"
    )
    monkeypatch.setenv("ALEXANDRIA_EMBED_BATCH_SIZE", "9")
    monkeypatch.setenv("ALEXANDRIA_CORPUS_PATH", "environment-corpus")

    config = load_config(path=config_path)

    assert config.corpus_path == Path("environment-corpus")
    assert config.embed_provider == "hash"
    assert config.embed_batch_size == 9
    assert config.wiki_boost == 1.5
