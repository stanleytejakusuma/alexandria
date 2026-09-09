"""R8 through the REAL CLI path.

The first R8 test called `bind()` with the raw control root, which is not what
`alexandria serve` does: `_config_for` resolves the generation pointer first, so
serve received an already-resolved generation and the drain could never follow a
later cutover. These tests pin the production shape instead.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from alexandria import serve as serve_mod
from alexandria.cli import app
from alexandria.generation_pointer import activate_generation


def _generation(root: Path, name: str, text: str) -> Path:
    generation = root / ".alexandria" / "generations" / name
    doc = generation / "sources" / "note.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(f"---\nsource: test\n---\n\n{text}\n")
    assert app(["--corpus", str(generation), "index"]) == 0
    return generation


def test_cli_serve_keeps_the_control_root_and_follows_a_cutover(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    control = tmp_path / "control"
    first = _generation(control, "first", "first generation text")
    activate_generation(control, first)

    captured: dict[str, object] = {}

    # cmd_serve imports `serve` from .serve at call time, so patch it there.
    monkeypatch.setattr(serve_mod, "serve",
                        lambda corpus, **kwargs: captured.setdefault("corpus_arg", Path(corpus)))
    assert app(["--corpus", str(control), "serve", "--port", "0"]) == 0

    # The CLI must hand serve the CONTROL ROOT, not the resolved generation.
    assert captured["corpus_arg"].resolve() == control.resolve()


def test_bind_from_control_root_follows_generation_switch(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    control = tmp_path / "control"
    first = _generation(control, "first", "first generation text")
    second = _generation(control, "second", "second generation text")
    activate_generation(control, first)

    from alexandria.config import load_config

    ctx, tcp, uds = serve_mod.bind(control, config=load_config(corpus_override=control),
                                   host="127.0.0.1", port=0)
    try:
        assert ctx.control_root.resolve() == control.resolve()
        assert ctx.corpus.resolve() == first.resolve()

        activate_generation(control, second)
        stop = serve_mod.start_drain(ctx, interval=0.01)
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and ctx.corpus.resolve() != second.resolve():
                time.sleep(0.01)
            assert ctx.corpus.resolve() == second.resolve()
        finally:
            stop.set()
    finally:
        tcp.server_close()
        for server in uds:
            server.server_close()


def test_a_broken_generation_pointer_does_not_kill_the_drain(tmp_path, monkeypatch) -> None:
    """SystemExit from engine construction must not silently end the thread."""
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    control = tmp_path / "control"
    first = _generation(control, "first", "first generation text")
    broken = _generation(control, "broken", "broken generation text")
    healthy = _generation(control, "healthy", "healthy generation text")
    activate_generation(control, first)
    (broken / ".alexandria" / "index" / "manifest.json").write_text("{not json")

    from alexandria.config import load_config

    ctx, tcp, uds = serve_mod.bind(control, config=load_config(corpus_override=control),
                                   host="127.0.0.1", port=0)
    stop = serve_mod.start_drain(ctx, interval=0.01)
    try:
        activate_generation(control, broken)
        time.sleep(0.3)
        assert ctx.corpus.resolve() == first.resolve()  # refused, stayed healthy

        activate_generation(control, healthy)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and ctx.corpus.resolve() != healthy.resolve():
            time.sleep(0.01)
        assert ctx.corpus.resolve() == healthy.resolve()  # drain still alive
    finally:
        stop.set()
        tcp.server_close()
        for server in uds:
            server.server_close()
