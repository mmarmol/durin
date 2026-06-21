"""Cross-process FTS write contention test.

Four worker processes each hammer 30 distinct upserts into the same
fts.sqlite concurrently.  After all workers finish:
  - every uri (4 × 30 = 120) must be present in the index, and
  - no unretried "database is locked" error must have escaped.

Uses multiprocessing with the "spawn" start-method to avoid fd/lock
inheritance issues from a fork.
"""

from __future__ import annotations

import multiprocessing
import tempfile
from pathlib import Path


def _worker(workspace_str: str, worker_id: int, n_upserts: int, result_queue) -> None:  # type: ignore[type-arg]
    """Upsert *n_upserts* distinct uris; put (worker_id, uris_written) or exc into queue."""
    try:
        from durin.memory.fts_index import FTSIndex
        ws = Path(workspace_str)
        written: list[str] = []
        with FTSIndex.open(ws) as idx:
            for i in range(n_upserts):
                uri = f"uri:worker{worker_id}:item{i}"
                idx.upsert(
                    uri=uri,
                    path=f"/fake/{uri}",
                    type_="memory",
                    entity_type=None,
                    text=f"content for {uri}",
                    mtime=float(i),
                )
                written.append(uri)
        result_queue.put((worker_id, written))
    except Exception as exc:  # noqa: BLE001
        result_queue.put(exc)


def test_concurrent_upsert_no_lost_writes() -> None:
    """4 processes × 30 upserts concurrently; all 120 rows must be indexed."""
    n_workers = 4
    n_upserts = 30
    ctx = multiprocessing.get_context("spawn")

    with tempfile.TemporaryDirectory() as tmpdir:
        ws = Path(tmpdir)
        result_queue: multiprocessing.Queue = ctx.Queue()

        procs = [
            ctx.Process(target=_worker, args=(str(ws), wid, n_upserts, result_queue))
            for wid in range(n_workers)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=60)

        results = [result_queue.get(timeout=10) for _ in procs]

        errors = [r for r in results if isinstance(r, Exception)]
        assert not errors, f"Worker errors: {errors}"

        expected_uris: set[str] = set()
        for _wid, uris in results:
            expected_uris.update(uris)

        assert len(expected_uris) == n_workers * n_upserts, (
            f"expected {n_workers * n_upserts} distinct uris, got {len(expected_uris)}"
        )

        from durin.memory.fts_index import FTSIndex
        with FTSIndex.open(ws) as idx:
            indexed_uris = {u for u, _ in idx.known_uris()}

        missing = expected_uris - indexed_uris
        assert not missing, (
            f"{len(missing)} uris lost under contention: {sorted(missing)[:10]}"
        )
