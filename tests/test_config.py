"""Retrieval configuration has local defaults and explicit env precedence."""

import pytest
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


def test_mlx_is_a_valid_embed_provider(monkeypatch):
    """MLX sidesteps the PyTorch MPS graph-cache leak; it must be selectable."""
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "mlx")
    assert load_config().embed_provider == "mlx"


def test_unknown_embed_provider_is_rejected(monkeypatch):
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "nonsense")
    with pytest.raises(ValueError, match="local, mlx, or hash"):
        load_config()


def test_rerank_prefetch_defaults_to_the_measured_knee():
    """20/12/8 all scored identical recall@5 and MRR on golden-v1; 5 degraded both.
    8 is the smallest prefetch with no measured quality cost, and the one that
    brings p50 inside the <500ms gate."""
    assert load_config().rerank_prefetch == 8


def test_mlx_is_the_default_embed_provider():
    """The live corpus index was built with MLX (3.18x faster, cosine 0.9994
    agreement on real golden-set docs, avoids a confirmed PyTorch MPS memory leak).
    'local' remains selectable but is no longer the default."""
    assert load_config().embed_provider == "mlx"
