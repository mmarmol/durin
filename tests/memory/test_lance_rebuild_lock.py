"""Verify that concurrent rebuild_from_workspace calls are serialized.

Two threads each call rebuild_from_workspace on the same VectorIndex.
Without the cross-process lock the non-atomic drop+create leaves a
table-missing window; with the lock they are serialized so only one
rebuild runs at a time.

Cross-process lock ordering: rebuild_from_workspace holds the rebuild lock
to prevent concurrent drop+create from leaving a table-missing window.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from durin.memory.embedding import EmbeddingProvider
from durin.memory.store import store_memory
from durin.memory.vector_index import VectorIndex, vector_index_available

pytestmark = pytest.mark.skipif(
    not vector_index_available(),
    reason="lancedb is not installed; install durin[memory] to run these tests",
)


class _FixedEmbeddingProvider(EmbeddingProvider):
    """Returns fixed 4-dim vectors so rebuild runs without fastembed."""

    DIM = 4

    @property
    def model_name(self) -> str:
        return "fixed/test-embed"

    @property
    def dimensions(self) -> int:
        return self.DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0, 0.0]] * len(texts)


def test_concurrent_rebuilds_are_serialized(tmp_path: Path) -> None:
    """Two threads calling rebuild_from_workspace must not corrupt the table.

    Strategy: hold the rebuild lock from a background thread while the
    second thread calls rebuild_from_workspace. The second rebuild must
    block until the lock is released, then complete — the table is
    intact and contains the expected rows.
    """
    import fcntl

    provider = _FixedEmbeddingProvider()
    workspace = tmp_path

    store_memory(workspace, content="alpha", headline="Alpha")
    store_memory(workspace, content="beta", headline="Beta")

    index = VectorIndex(workspace, provider)
    # Warm the index with an initial rebuild.
    initial = index.rebuild_from_workspace()
    assert initial == 2

    # Compute the lock file path that rebuild_from_workspace will use.
    # VectorIndex._uri == str(workspace / ".durin" / "index" / "lance")
    index_dir = Path(index._uri)
    lock_path = Path(f"{index_dir}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # State shared between threads.
    lock_held = threading.Event()
    lock_released = threading.Event()
    errors: list[str] = []

    def hold_lock() -> None:
        """Acquire the OS flock, signal, wait, then release."""
        try:
            with open(lock_path, "a+", encoding="utf-8") as fp:
                fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
                lock_held.set()
                # Hold until the test says to release.
                lock_released.wait(timeout=10)
                fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
            lock_held.set()  # unblock the main thread on failure

    holder = threading.Thread(target=hold_lock, daemon=True)
    holder.start()

    # Wait for the background thread to hold the lock.
    lock_held.wait(timeout=5)
    assert not errors, f"lock-holder failed: {errors}"

    # Start a rebuild in another thread; it must block while the lock is held.
    rebuild_started = threading.Event()
    rebuild_done = threading.Event()
    rebuild_result: list[int] = []

    def run_rebuild() -> None:
        rebuild_started.set()
        try:
            count = index.rebuild_from_workspace()
            rebuild_result.append(count)
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
        finally:
            rebuild_done.set()

    rebuilder = threading.Thread(target=run_rebuild, daemon=True)
    rebuilder.start()

    rebuild_started.wait(timeout=2)

    # The rebuild should be blocked — not done while the lock is held.
    is_blocked = not rebuild_done.wait(timeout=0.3)
    assert is_blocked, "rebuild did not block while lock was held — lock missing"

    # Release the external lock; the rebuild should now complete.
    lock_released.set()
    rebuild_done.wait(timeout=10)
    holder.join(timeout=5)
    rebuilder.join(timeout=5)

    assert not errors, f"unexpected errors: {errors}"
    assert rebuild_result == [2], f"unexpected rebuild count: {rebuild_result}"

    # Final sanity: table is intact.
    hits = index.search("alpha", top_k=10)
    assert len(hits) == 2
