from __future__ import annotations

import shutil
import time
from pathlib import Path

from alexandria.cli import app
from alexandria.config import load_config
from alexandria.generation_pointer import activate_generation


def test_drain_follows_current_generation_without_a_restart(tmp_path, monkeypatch) -> None:
    """R8: an outer generation cutover must not leave serve on old data."""
    from alexandria import serve as serve_mod

    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    control = tmp_path / "control"
    generations = control / ".alexandria" / "generations"
    first = generations / "first"
    doc = first / "sources" / "note.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("---\nsource: test\n---\n\nfirst generation searchable text\n")
    assert app(["--corpus", str(first), "index"]) == 0
    activate_generation(control, first)

    config = load_config(corpus_override=control)
    ctx, tcp, uds = serve_mod.bind(control, config=config, host="127.0.0.1", port=0)
    assert ctx.corpus.resolve() == first.resolve()

    second = generations / "second"
    shutil.copytree(first, second)
    activate_generation(control, second)

    stop = serve_mod.start_drain(ctx, interval=0.01)
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and ctx.corpus.resolve() != second.resolve():
            time.sleep(0.01)
        assert ctx.corpus.resolve() == second.resolve()
        assert Path(ctx.store.path).resolve().is_relative_to(second.resolve())
    finally:
        stop.set()
        tcp.server_close()
        for server in uds:
            server.server_close()
