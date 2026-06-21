"""Cross-process FTS write contention test.

Two worker processes upsert DIFFERENT uris concurrently into the same
fts.sqlite.  After both finish, both uris must be present and no
unretried "database is locked" error must have escaped.

Uses multiprocessing with the "spawn" start-method to avoid fd/lock
inheritance issues from a fork.
"""

from __future__ import annotations

import multiprocessing
import tempfile
from pathlib import Path


def _worker(workspace_str: str, uri: str, result_queue) -> None:  # type: ignore[type-arg]
    """Upsert one uri into the FTS index; put True/exc into result_queue."""
    try:
        from durin.memory.fts_index import FTSIndex
        ws = Path(workspace_str)
        with FTSIndex.open(ws) as idx:
            idx.upsert(
                uri=uri,
                path=f"/fake/{uri}",
                type_="memory",
                entity_type=None,
                text=f"content for {uri}",
                mtime=1.0,
            )
        result_queue.put(True)
    except Exception as exc:  # noqa: BLE001
        result_queue.put(exc)


def test_concurrent_upsert_no_lost_writes() -> None:
    """Two processes upsert different uris concurrently; both must be indexed."""
    ctx = multiprocessing.get_context("spawn")

    with tempfile.TemporaryDirectory() as tmpdir:
        ws = Path(tmpdir)
        result_queue: multiprocessing.Queue = ctx.Queue()

        p1 = ctx.Process(target=_worker, args=(str(ws), "uri:first", result_queue))
        p2 = ctx.Process(target=_worker, args=(str(ws), "uri:second", result_queue))

        p1.start()
        p2.start()
        p1.join(timeout=30)
        p2.join(timeout=30)

        r1 = result_queue.get(timeout=5)
        r2 = result_queue.get(timeout=5)

        errors = [r for r in (r1, r2) if isinstance(r, Exception)]
        assert not errors, f"Worker errors: {errors}"

        from durin.memory.fts_index import FTSIndex
        with FTSIndex.open(ws) as idx:
            uris = {u for u, _ in idx.known_uris()}

        assert "uri:first" in uris, f"uri:first missing; found: {uris}"
        assert "uri:second" in uris, f"uri:second missing; found: {uris}"
