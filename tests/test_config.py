"""Retrieval configuration has local defaults and explicit env precedence."""

import pytest
import re
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


def test_the_default_embed_provider_matches_what_the_live_index_was_built_with():
    """The default exists to MATCH the shipped index, not to be fastest.

    It was "mlx" because the live index was built with MLX (3.18x faster,
    cosine 0.9994 agreement, avoids a PyTorch MPS leak). The 2026-08-18
    re-embed rebuilt that index with the torch/`local` provider under the
    declared L2 policy, so the manifest now says provider=local,
    model=Qwen/Qwen3-Embedding-0.6B.

    Leaving the default at "mlx" made every CLI invocation with no explicit
    env var fail closed against the corpus it ships with -- correct refusal,
    impossible default. This asserts the two agree; a future provider change
    must move BOTH the index and this default, or deliberately re-pin it.
    """
    assert load_config().embed_provider == "local"


def test_the_dataclass_default_and_the_loader_fallback_do_not_disagree():
    """The two defaults drifted apart, which is how this shipped unnoticed.

    AppConfig.embed_provider defaulted to "local" while load_config()'s own
    fallback passed "mlx", so the answer to "what is the default provider?"
    depended on which one you read. Anything constructing AppConfig directly
    (tests, library callers) got a different provider from anything going
    through load_config().
    """
    import ast
    import inspect
    import textwrap

    from alexandria.config import AppConfig, load_config

    # AST, not regex: the call's arguments include a ("embed", "provider")
    # tuple, so any pattern matching quoted text picks up a tuple element and
    # silently compares the wrong string.
    tree = ast.parse(textwrap.dedent(inspect.getsource(load_config)))
    call = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "_env_or_file"
        and node.args
        and getattr(node.args[0], "value", None) == "ALEXANDRIA_EMBED_PROVIDER")
    fallback = call.args[-1].value

    assert fallback == AppConfig(corpus_path=Path("/tmp")).embed_provider
