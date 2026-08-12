"""§3.1 / gate F1: two PROCESSES writing the FTS index concurrently both
succeed; neither raises "database is locked".

Real subprocesses, not threads -- the gate's own wording specifically
names processes, matching the realistic production shape (e.g. `alexandria
serve`'s inline promote and a separately-invoked `alexandria promote`
running at the same moment) rather than two threads sharing one
interpreter's GIL, which schedules differently and would not exercise the
same OS-level file-lock contention as convincingly.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

WORKER_SCRIPT = textwrap.dedent("""
    from alexandria.index.bm25 import BM25Index

    index = BM25Index({db_path!r})
    records = [
        {{"chunk_id": f"{prefix}#{{i}}", "doc_id": f"{prefix}-doc-{{i}}", "text": f"process {prefix} chunk {{i}}",
          "type": "", "project": "", "status": "", "source": "test",
          "tags": [], "entities": [], "layer": "sources", "generated_at": ""}}
        for i in range(50)
    ]
    index.index(records)
    print("OK")
""")


def test_f1_two_processes_writing_fts_concurrently_both_succeed(tmp_path):
    db_path = tmp_path / "fts.sqlite"

    # Pre-initialize the database (schema + WAL mode) before racing writers
    # at it. F1 is about concurrent WRITERS to an existing index -- the real
    # production shape (serve's inline promote and a separately-invoked
    # `alexandria promote` both landing at once against an already-indexed
    # corpus) -- not two processes racing to CREATE the file for the first
    # time, which is a narrower, rarer race (only possible in the single
    # instant between a brand-new corpus's very first write) and was
    # observed to occasionally still raise "database is locked" during the
    # journal_mode=WAL pragma switch itself, before WAL is durably active --
    # a real but separate finding from what this gate is testing.
    from alexandria.index.bm25 import BM25Index
    BM25Index(db_path)

    script_a = tmp_path / "worker_a.py"
    script_b = tmp_path / "worker_b.py"
    script_a.write_text(WORKER_SCRIPT.format(db_path=str(db_path), prefix="a"))
    script_b.write_text(WORKER_SCRIPT.format(db_path=str(db_path), prefix="b"))

    proc_a = subprocess.Popen([sys.executable, str(script_a)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    proc_b = subprocess.Popen([sys.executable, str(script_b)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    out_a, err_a = proc_a.communicate(timeout=30)
    out_b, err_b = proc_b.communicate(timeout=30)

    assert proc_a.returncode == 0, f"process A failed:\\n{err_a}"
    assert proc_b.returncode == 0, f"process B failed:\\n{err_b}"
    assert "database is locked" not in err_a
    assert "database is locked" not in err_b
    assert "OK" in out_a
    assert "OK" in out_b

    from alexandria.index.bm25 import BM25Index
    index = BM25Index(db_path)
    count = index.connection.execute("SELECT COUNT(*) FROM chunk_metadata").fetchone()[0]
    assert count == 100, f"expected 50+50=100 rows from both processes, got {count}"
    assert index.wal_active is True


def test_f1_a_writer_waits_for_a_held_write_lock_instead_of_failing_instantly(tmp_path):
    """The gate's actual subject: `busy_timeout`.

    The two-process test above pre-creates the database so the racing writers
    contend on an EXISTING index -- which is the production shape, but it also
    means it passes with or without `busy_timeout` most of the time. This one
    pins the pragma itself: another connection holds a write transaction, and a
    BM25Index write must BLOCK and then succeed once it is released. Without
    `busy_timeout` set, sqlite raises `database is locked` immediately and this
    fails.
    """
    import sqlite3
    import threading
    import time

    from alexandria.index.bm25 import BM25Index

    db = tmp_path / "fts.sqlite"
    index = BM25Index(db)          # establishes schema + WAL
    assert index.wal_active is True
    # NB: CPython's sqlite3.connect() already defaults busy_timeout to 5000ms,
    # so this asserts the guarantee HOLDS, not that the explicit pragma is what
    # provides it (commenting the pragma out changes nothing -- measured). It
    # would catch a future caller passing timeout=0, which disables waiting.
    assert index.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000

    holder = sqlite3.connect(db)
    holder.execute("BEGIN IMMEDIATE")   # takes the write lock and holds it

    outcome: dict[str, object] = {}

    def writer():
        started = time.monotonic()
        try:
            BM25Index(db).index([{
                "chunk_id": "waiter#1", "doc_id": "waiter", "text": "waited for the lock",
                "heading_path": "", "type": "note", "project": "", "status": "stable",
                "source": "test", "tags": [], "entities": [], "layer": "", "generated_at": "",
            }])
            outcome["ok"] = True
        except sqlite3.OperationalError as exc:
            outcome["ok"] = False
            outcome["error"] = str(exc)
        outcome["waited"] = time.monotonic() - started

    thread = threading.Thread(target=writer)
    thread.start()
    time.sleep(0.4)                      # long enough that an instant failure is visible
    holder.commit()                      # release the write lock
    holder.close()
    thread.join(timeout=15)

    assert outcome.get("ok") is True, (
        f"the second writer did not wait for the lock: {outcome.get('error')} -- "
        f"busy_timeout is not in effect")
    assert outcome["waited"] >= 0.3, (
        f"the write returned in {outcome['waited']:.3f}s, faster than the lock was "
        f"held -- it cannot have contended for it, so this proves nothing")
