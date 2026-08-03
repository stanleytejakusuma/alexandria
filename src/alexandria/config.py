"""Configuration loaded from the user file with explicit environment overrides."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["AppConfig", "load_config"]


@dataclass(frozen=True)
class AppConfig:
    corpus_path: Path
    embed_provider: str = "local"
    embed_model: str = "Qwen/Qwen3-Embedding-0.6B"
    embed_batch_size: int = 32
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_prefetch: int = 20
    rerank_top_k: int = 5
    chunk_tokens: int = 512
    chunk_overlap: float = 0.15
    index_progress_every: int = 250
    wiki_boost: float = 1.25
    rrf_k: int = 60


def load_config(*, corpus_override: str | Path | None = None,
                path: str | Path | None = None) -> AppConfig:
    config_path = Path(path or os.environ.get("ALEXANDRIA_CONFIG", "~/.config/alexandria/config.toml")).expanduser()
    raw = _read_toml(config_path)
    corpus = (corpus_override or os.environ.get("ALEXANDRIA_CORPUS_PATH")
              or _nested(raw, "corpus", "path") or "~/alexandria-corpus")
    provider = _env_or_file("ALEXANDRIA_EMBED_PROVIDER", raw, ("embed", "provider"), "local")
    if provider not in {"local", "hash", "mlx"}:
        raise ValueError("ALEXANDRIA_EMBED_PROVIDER must be local, mlx, or hash")
    return AppConfig(
        corpus_path=Path(corpus).expanduser(),
        embed_provider=provider,
        embed_model=_env_or_file("ALEXANDRIA_EMBED_MODEL", raw, ("embed", "model"),
                                 "Qwen/Qwen3-Embedding-0.6B"),
        embed_batch_size=_as_int(_env_or_file("ALEXANDRIA_EMBED_BATCH_SIZE", raw,
                                               ("embed", "batch_size"), 32)),
        rerank_model=_env_or_file("ALEXANDRIA_RERANK_MODEL", raw, ("rerank", "model"),
                                  "BAAI/bge-reranker-v2-m3"),
        rerank_prefetch=_as_int(_env_or_file("ALEXANDRIA_RERANK_PREFETCH", raw,
                                             ("rerank", "prefetch"), 20)),
        rerank_top_k=_as_int(_env_or_file("ALEXANDRIA_RERANK_TOP_K", raw,
                                          ("rerank", "top_k"), 5)),
        chunk_tokens=_as_int(_env_or_file("ALEXANDRIA_INDEX_CHUNK_TOKENS", raw,
                                          ("index", "chunk_tokens"), 512)),
        chunk_overlap=float(_env_or_file("ALEXANDRIA_INDEX_CHUNK_OVERLAP", raw,
                                         ("index", "chunk_overlap"), 0.15)),
        index_progress_every=_as_int(_env_or_file("ALEXANDRIA_INDEX_PROGRESS_EVERY", raw,
                                                  ("index", "progress_every"), 250)),
        wiki_boost=float(_env_or_file("ALEXANDRIA_SEARCH_WIKI_BOOST", raw,
                                      ("search", "wiki_boost"), 1.25)),
        rrf_k=_as_int(_env_or_file("ALEXANDRIA_SEARCH_RRF_K", raw, ("search", "rrf_k"), 60)),
    )


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        value = tomllib.load(handle)
    return value if isinstance(value, dict) else {}


def _nested(raw: dict[str, Any], *keys: str) -> Any:
    value: Any = raw
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _env_or_file(env: str, raw: dict[str, Any], keys: tuple[str, str], default: Any) -> Any:
    return os.environ.get(env, _nested(raw, *keys) if _nested(raw, *keys) is not None else default)


def _as_int(value: Any) -> int:
    parsed = int(value)
    if parsed < 1:
        raise ValueError("configuration counts must be positive")
    return parsed
