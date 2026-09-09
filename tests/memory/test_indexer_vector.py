"""Tests for the entry branch of ``reindex_one_file_vector``.

The reactive index path (file watcher) already embedded entity pages;
this covers the other half — ``memory/<class>/<id>.md`` entries
(episodic, stable, corpus, session_summary). Uses the same fake
embedding provider as ``test_vector_index.py`` so search results stay
deterministic and CI doesn't pull the real fastembed model.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from durin.memory.embedding import EmbeddingProvider
from durin.memory.vector_index import VectorIndex, vector_index_available


class _FakeEmbeddingProvider(EmbeddingProvider):
    """Deterministic embeddings keyed off the first character of the text.

    Identical to the provider in ``test_vector_index.py``: 8-dim vectors,
    first dimension derived from the input's first character, so a query
    sharing its first char with a stored text retrieves it.
    """

    DIM = 8

    @property
    def model_name(self) -> str:
        return "fake/test-embed"

    @property
    def dimensions(self) -> int:
        return self.DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            seed = float(ord(text[0])) if text else 0.0
            out.append([seed] + [0.0] * (self.DIM - 1))
        return out


@pytest.fixture
def provider() -> _FakeEmbeddingProvider:
    return _FakeEmbeddingProvider()


pytestmark = pytest.mark.skipif(
    not vector_index_available(),
    reason="lancedb is not installed; install durin[memory] to run these tests",
)


def test_an_episodic_entry_is_embedded_by_the_reactive_path(
    tmp_path: Path, provider: _FakeEmbeddingProvider
) -> None:
    from durin.memory.indexer import reindex_one_file_vector
    from durin.memory.store import store_memory

    ws = tmp_path / "ws"
    result = store_memory(
        ws, content="Bruenor prefiere el hacha de doble filo.", class_name="episodic"
    )
    vi = VectorIndex(ws, provider)
    assert reindex_one_file_vector(ws, Path(result["path"]), vi) is True
    rows = vi.search("hacha", top_k=3)
    assert rows and rows[0]["class_name"] == "episodic"
    assert rows[0]["id"] == result["id"]


def test_a_session_summary_is_embedded_too(
    tmp_path: Path, provider: _FakeEmbeddingProvider
) -> None:
    from durin.memory.indexer import reindex_one_file_vector
    from durin.memory.session_summary_store import write_session_summary
    from durin.memory.storage import load_entry

    ws = tmp_path / "ws"
    path = write_session_summary(
        ws, "session-1", "Bruenor discute su colección de hachas en detalle."
    )
    assert path is not None
    vi = VectorIndex(ws, provider)
    assert reindex_one_file_vector(ws, path, vi) is True
    rows = vi.search("hachas", top_k=3)
    assert rows and rows[0]["class_name"] == "session_summary"
    assert rows[0]["id"] == load_entry(path).id


def test_pending_and_archive_entries_are_never_embedded(
    tmp_path: Path, provider: _FakeEmbeddingProvider
) -> None:
    from durin.memory.indexer import reindex_one_file_vector
    from durin.memory.store import store_memory

    ws = tmp_path / "ws"

    # Create a pending entry and verify it is not embedded
    pending_result = store_memory(
        ws, content="Bruenor tiene un hacha mágica guardada.", class_name="pending"
    )
    vi = VectorIndex(ws, provider)
    assert reindex_one_file_vector(ws, Path(pending_result["path"]), vi) is False

    # Create an archive entry and verify it is not embedded. Real archive
    # layout is memory/archive/<class>/<id>.md (three parts) — it never
    # reaches the pending/archive guard, it fails the len(parts) == 2
    # layout check first.
    archive_result = store_memory(
        ws, content="Bruenor vendió una hacha antigua al mercado.", class_name="stable"
    )
    # Move the file to archive
    archive_path = ws / "memory" / "archive" / "stable" / f"{archive_result['id']}.md"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    original_path = Path(archive_result["path"])
    original_path.rename(archive_path)

    assert reindex_one_file_vector(ws, archive_path, vi) is False

    # Verify the vector index contains no rows for either text
    rows_hacha_magica = vi.search("hacha mágica", top_k=3)
    assert not rows_hacha_magica or all(r["id"] != pending_result["id"] for r in rows_hacha_magica)

    rows_hacha_antigua = vi.search("hacha antigua", top_k=3)
    assert not rows_hacha_antigua or all(r["id"] != archive_result["id"] for r in rows_hacha_antigua)


def test_backfill_embeds_entries_that_have_an_fts_row_and_no_vector_row(
    tmp_path: Path, provider: _FakeEmbeddingProvider
) -> None:
    from durin.memory.indexer import backfill_missing_vectors, reindex_one_file
    from durin.memory.storage import load_entry
    from durin.memory.store import store_memory

    ws = tmp_path / "ws"
    r1 = store_memory(ws, content="primera nota", class_name="episodic")
    r2 = store_memory(ws, content="segunda nota", class_name="episodic")
    reindex_one_file(ws, Path(r1["path"]))
    reindex_one_file(ws, Path(r2["path"]))  # FTS rows only, no vector rows yet

    vi = VectorIndex(ws, provider)
    vi.upsert(load_entry(Path(r1["path"])), "episodic", Path(r1["path"]))  # one already embedded

    done = backfill_missing_vectors(ws, vi)

    assert done == {"episodic": 1}
    assert vi.ids_by_class(["episodic"]) == {r1["id"], r2["id"]}


def test_backfill_is_idempotent(
    tmp_path: Path, provider: _FakeEmbeddingProvider
) -> None:
    from durin.memory.indexer import backfill_missing_vectors, reindex_one_file
    from durin.memory.store import store_memory

    ws = tmp_path / "ws"
    r1 = store_memory(ws, content="tercera nota", class_name="episodic")
    reindex_one_file(ws, Path(r1["path"]))

    vi = VectorIndex(ws, provider)
    first = backfill_missing_vectors(ws, vi)
    assert first == {"episodic": 1}

    second = backfill_missing_vectors(ws, vi)
    assert second == {}
    assert vi.ids_by_class(["episodic"]) == {r1["id"]}


def test_backfill_does_not_emit_event_when_count_is_zero(
    tmp_path: Path, provider: _FakeEmbeddingProvider, monkeypatch
) -> None:
    import durin.memory.indexer as indexer_mod
    from durin.memory.indexer import backfill_missing_vectors, reindex_one_file
    from durin.memory.store import store_memory

    ws = tmp_path / "ws"
    r1 = store_memory(ws, content="cuarta nota", class_name="episodic")
    reindex_one_file(ws, Path(r1["path"]))

    vi = VectorIndex(ws, provider)
    # Patch reindex_one_file_vector to return False for all entries,
    # so count stays 0.
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        indexer_mod, "reindex_one_file_vector",
        lambda workspace, path, vi: False,
    )
    monkeypatch.setattr(
        indexer_mod, "_emit_backfill",
        lambda class_name, count, duration_ms: events.append(("backfill", {"class": class_name, "count": count})),
    )

    done = backfill_missing_vectors(ws, vi)

    # count is 0 because reindex_one_file_vector returned False
    assert done == {"episodic": 0}
    # No backfill event should have been emitted
    assert events == []


def test_backfill_emits_telemetry_with_class_count_and_duration(
    tmp_path: Path, provider: _FakeEmbeddingProvider, monkeypatch
) -> None:
    from durin.memory.indexer import backfill_missing_vectors, reindex_one_file
    from durin.memory.store import store_memory

    ws = tmp_path / "ws"
    r1 = store_memory(ws, content="quinta nota", class_name="episodic")
    reindex_one_file(ws, Path(r1["path"]))

    vi = VectorIndex(ws, provider)

    events: list[tuple[str, dict]] = []
    import durin.agent.tools._telemetry as _tel
    monkeypatch.setattr(_tel, "emit_tool_event", lambda t, d: events.append((t, d)))

    done = backfill_missing_vectors(ws, vi)

    assert done == {"episodic": 1}
    backfills = [e for e in events if e[0] == "memory.index.backfill"]
    assert len(backfills) == 1
    payload = backfills[0][1]
    assert payload["class"] == "episodic"
    assert payload["count"] == 1
    assert isinstance(payload["duration_ms"], (int, float))


def test_backfill_stops_on_dimension_mismatch(
    tmp_path: Path, provider: _FakeEmbeddingProvider, monkeypatch
) -> None:
    from durin.memory.indexer import backfill_missing_vectors, reindex_one_file
    from durin.memory.store import store_memory
    from durin.memory.vector_index import VectorIndexDimensionMismatchError

    ws = tmp_path / "ws"
    r1 = store_memory(ws, content="nota episodic", class_name="episodic")
    r2 = store_memory(ws, content="nota stable", class_name="stable")
    reindex_one_file(ws, Path(r1["path"]))
    reindex_one_file(ws, Path(r2["path"]))

    vi = VectorIndex(ws, provider)

    def _raise(*args, **kwargs):
        raise VectorIndexDimensionMismatchError("dimension mismatch")

    monkeypatch.setattr(vi, "upsert", _raise)

    done = backfill_missing_vectors(ws, vi, classes=("episodic", "stable"))

    # Stops after the first entry (episodic) with count 0; "stable" is
    # never reached.
    assert done == {"episodic": 0}


def test_backfill_abandons_class_after_five_consecutive_failures(
    tmp_path: Path, provider: _FakeEmbeddingProvider, monkeypatch
) -> None:
    from durin.memory.indexer import backfill_missing_vectors, reindex_one_file
    from durin.memory.store import store_memory

    ws = tmp_path / "ws"
    results = [
        store_memory(ws, content=f"nota {i}", class_name="episodic")
        for i in range(6)
    ]
    for r in results:
        reindex_one_file(ws, Path(r["path"]))

    vi = VectorIndex(ws, provider)
    calls: list[str] = []

    def _raise(entry, class_name, path):
        calls.append(entry.id)
        raise RuntimeError("boom")

    monkeypatch.setattr(vi, "upsert", _raise)

    done = backfill_missing_vectors(ws, vi)

    assert done == {"episodic": 0}
    # Exactly five attempts, not all six missing entries.
    assert len(calls) == 5
