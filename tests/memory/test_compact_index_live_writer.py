"""The nightly index compaction survives a live writer.

The dream worker compacts the LanceDB table in its own process while the
gateway's file watcher is still indexing the pages the dream just wrote.
Lance refuses to commit a compaction over a commit it did not see
("Retryable commit conflict"), and ``compact_index`` reported that as a
failure and moved on — every night since 2026-09-18 on the box, where the
table had piled up 510 versions again. Compaction now waits for the table
to go quiet and retries a bounded number of times when a write lands anyway.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from durin.memory import vector_index
from durin.memory.embedding import EmbeddingProvider
from durin.memory.vector_index import VectorIndex, vector_index_available

pytestmark = pytest.mark.skipif(not vector_index_available(), reason="lancedb not installed")


class _FakeEmbeddingProvider(EmbeddingProvider):
    """Deterministic 8-dim embeddings keyed off the text's first character."""

    DIM = 8

    @property
    def model_name(self) -> str:
        return "fake/test-embed"

    @property
    def dimensions(self) -> int:
        return self.DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(ord(t[0])) if t else 0.0] + [0.0] * (self.DIM - 1) for t in texts]

_CONFLICT = RuntimeError(
    "lance error: Retryable commit conflict for version 35748: This Rewrite "
    "transaction was preempted by concurrent transaction Append at version 35747."
)


def _churned_workspace(tmp_path: Path) -> tuple[Path, VectorIndex]:
    ws = tmp_path / "ws"
    vi = VectorIndex(ws, _FakeEmbeddingProvider())
    for i in range(6):
        vi.upsert_entity_page(
            entity_ref=f"topic:t{i}", name=f"T{i}", aliases=[],
            body=f"body {i}",
            path=ws / "memory" / "entities" / "topic" / f"t{i}.md",
        )
    return ws, vi


def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vector_index, "_sleep", lambda s: None)


def test_a_conflict_with_a_live_writer_is_retried_and_the_table_compacts(tmp_path, monkeypatch):
    from lancedb.table import LanceTable

    ws, vi = _churned_workspace(tmp_path)
    _no_sleep(monkeypatch)
    real_optimize = LanceTable.optimize
    calls: list[int] = []

    def flaky_optimize(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise _CONFLICT
        return real_optimize(self, *args, **kwargs)

    monkeypatch.setattr(LanceTable, "optimize", flaky_optimize)

    stats = vector_index.compact_index(ws)

    assert stats["compacted"] is True, stats
    assert stats["attempts"] == 2
    assert stats["versions_before"] > stats["versions_after"]
    assert vi.search("body 3", top_k=3)


def test_a_conflict_that_never_clears_is_reported_after_bounded_attempts(tmp_path, monkeypatch):
    from lancedb.table import LanceTable

    ws, vi = _churned_workspace(tmp_path)
    _no_sleep(monkeypatch)
    calls: list[int] = []

    def always_conflict(self, *args, **kwargs):
        calls.append(1)
        raise _CONFLICT

    monkeypatch.setattr(LanceTable, "optimize", always_conflict)

    stats = vector_index.compact_index(ws)

    assert stats["compacted"] is False
    assert "commit conflict" in stats["reason"]
    assert stats["attempts"] == vector_index._COMPACT_ATTEMPTS == len(calls)
    assert vi.search("body 3", top_k=3), "the table must still be searchable after giving up"


def test_an_error_that_is_not_a_conflict_is_not_retried(tmp_path, monkeypatch):
    from lancedb.table import LanceTable

    ws, _ = _churned_workspace(tmp_path)
    _no_sleep(monkeypatch)
    calls: list[int] = []

    def broken_optimize(self, *args, **kwargs):
        calls.append(1)
        raise ValueError("boom")

    monkeypatch.setattr(LanceTable, "optimize", broken_optimize)

    stats = vector_index.compact_index(ws)

    assert stats["compacted"] is False and "boom" in stats["reason"]
    assert len(calls) == 1


def test_wait_for_quiet_returns_once_the_version_stops_moving():
    versions = iter([10, 11, 12, 12, 12, 12])
    opened: list[int] = []

    def open_table():
        v = next(versions)
        opened.append(v)
        return SimpleNamespace(version=v)

    clock = {"t": 0.0}

    def sleep(s: float) -> None:
        clock["t"] += s

    table = vector_index._wait_for_quiet(
        open_table, quiet_s=5.0, max_wait_s=120.0, sleep=sleep, clock=lambda: clock["t"],
    )
    assert table.version == 12
    # Two consecutive reads at 12 across a full quiet window is what "quiet" means.
    assert opened[-2:] == [12, 12]


def test_wait_for_quiet_gives_up_at_the_deadline_with_the_latest_table():
    counter = {"v": 0}

    def open_table():
        counter["v"] += 1
        return SimpleNamespace(version=counter["v"])

    clock = {"t": 0.0}

    def sleep(s: float) -> None:
        clock["t"] += s

    table = vector_index._wait_for_quiet(
        open_table, quiet_s=5.0, max_wait_s=30.0, sleep=sleep, clock=lambda: clock["t"],
    )
    assert clock["t"] <= 35.0
    assert table.version == counter["v"]
