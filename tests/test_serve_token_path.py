"""A minted token must land where serve will actually read it.

Two independent defects stacked here:

  1. Two constants for one file. `serve_auth.TOKEN_FILE_DEFAULT` was
     "serve-tokens.txt" while `serve._TOKEN_FILE_DEFAULT` was
     ".alexandria/serve-tokens.txt", so `--add-token` wrote one path and serve
     read another. Every request 401s, with no diagnosis.
  2. The wrong root. The path was built from `config.corpus_path` -- the
     resolved generation -- which staging does not copy, so even a matching
     path would be dropped by the next publish.

Auth is the one surface where a silent mismatch is indistinguishable from an
attack, so this is pinned rather than left to inspection.
"""
from __future__ import annotations

from pathlib import Path

from alexandria.config import load_config
from alexandria.generation_pointer import activate_generation
from alexandria.serve import _load_token_store
from alexandria.serve_auth import TOKEN_FILE_DEFAULT, hash_token


def _corpus_with_generation(tmp_path: Path) -> tuple[Path, Path]:
    control = tmp_path / "corpus"
    generation = control / ".alexandria" / "generations" / "gen-a"
    (generation / "sources").mkdir(parents=True)
    (generation / ".alexandria" / "state").mkdir(parents=True)
    activate_generation(control, generation)
    return control, generation


def test_the_mint_path_and_the_serve_read_path_are_the_same_file(
    tmp_path: Path,
) -> None:
    """The regression that matters most: mint, then load through serve's own path."""
    control, _ = _corpus_with_generation(tmp_path)
    config = load_config(corpus_override=control)

    minted = config.control_root / TOKEN_FILE_DEFAULT
    minted.parent.mkdir(parents=True, exist_ok=True)
    minted.write_text(f"operator:{hash_token('s3cret')}\n")

    # token_file=None exercises the DEFAULT path -- the one that silently
    # diverged from the mint path.
    store = _load_token_store(None, config.control_root)

    assert store, "serve must find the file --add-token wrote"
    assert store["operator"] == hash_token("s3cret")


def test_the_token_file_lives_outside_any_generation(tmp_path: Path) -> None:
    """A token inside a generation is dropped at the next publish."""
    control, generation = _corpus_with_generation(tmp_path)
    config = load_config(corpus_override=control)

    path = config.control_root / TOKEN_FILE_DEFAULT

    assert path.is_relative_to(control)
    assert not path.is_relative_to(generation)


def test_token_default_is_one_constant(tmp_path: Path) -> None:
    """One file, one constant. Two spellings is how the split happened."""
    from alexandria import serve

    assert serve._TOKEN_FILE_DEFAULT == TOKEN_FILE_DEFAULT, (
        "serve and serve_auth must agree on the token path, or a minted token "
        "silently never loads"
    )
