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
    # 8, not 20. Measured on golden-v1 against the real index: prefetch 20/12/8 all
    # score IDENTICAL recall@5 (64.3%) and IDENTICAL MRR (0.571), while p50 falls
    # 1071ms -> 437ms. Quality only degrades at 5 (57.1%, MRR 0.500). 8 is the knee,
    # and it is what brings p50 inside the spec's <500ms phase-1 gate.
    rerank_prefetch: int = 8
    rerank_top_k: int = 5
    chunk_tokens: int = 512
    chunk_overlap: float = 0.15
    index_progress_every: int = 250
    # Commit granularity, deliberately decoupled from embed_batch_size. Every
    # store write is one LanceDB commit, and a commit rewrites a manifest listing
    # every existing fragment -- so per-commit cost grows with the number of prior
    # commits and a full rebuild is O(n^2). Measured on the real corpus at 32:
    # 3,970 fragments, manifest grown 1.7KB -> 292KB, 561MB of manifest churn
    # against 683MB of actual data, throughput halved 480 -> 256 chunks/min.
    # 4096 buys ~130x fewer commits for ~25MB of buffered rows.
    index_write_batch: int = 4096
    wiki_boost: float = 1.25
    rrf_k: int = 60


def load_config(*, corpus_override: str | Path | None = None,
                path: str | Path | None = None) -> AppConfig:
    config_path = Path(path or os.environ.get("ALEXANDRIA_CONFIG", "~/.config/alexandria/config.toml")).expanduser()
    raw = _read_toml(config_path)
    corpus = (corpus_override or os.environ.get("ALEXANDRIA_CORPUS_PATH")
              or _nested(raw, "corpus", "path") or "~/alexandria-corpus")
    # The default must MATCH the index that ships with the corpus, not merely be
    # the fastest backend. It was "mlx" (3.18x faster than pytorch/mps, cosine
    # 0.9994 agreement, sidesteps the PyTorch MPS graph-cache leak
    # pytorch/pytorch#154329) because the live index had been built with MLX.
    #
    # The 2026-08-18 re-embed rebuilt that index with the torch/`local` provider
    # under the declared L2 normalization policy, so the manifest now records
    # provider=local, model=Qwen/Qwen3-Embedding-0.6B. Leaving the default at
    # "mlx" meant every CLI invocation without an explicit env var failed closed
    # against its own corpus -- the refusal was correct, the default impossible.
    # Kept identical to AppConfig's dataclass default so "what is the default?"
    # has ONE answer regardless of which construction path you read.
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
                                             ("rerank", "prefetch"), 8)),
        rerank_top_k=_as_int(_env_or_file("ALEXANDRIA_RERANK_TOP_K", raw,
                                          ("rerank", "top_k"), 5)),
        chunk_tokens=_as_int(_env_or_file("ALEXANDRIA_INDEX_CHUNK_TOKENS", raw,
                                          ("index", "chunk_tokens"), 512)),
        chunk_overlap=float(_env_or_file("ALEXANDRIA_INDEX_CHUNK_OVERLAP", raw,
                                         ("index", "chunk_overlap"), 0.15)),
        index_progress_every=_as_int(_env_or_file("ALEXANDRIA_INDEX_PROGRESS_EVERY", raw,
                                                  ("index", "progress_every"), 250)),
        index_write_batch=_as_int(_env_or_file("ALEXANDRIA_INDEX_WRITE_BATCH", raw,
                                               ("index", "write_batch"), 4096)),
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
