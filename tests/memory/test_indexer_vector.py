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

    done = backfill_missing_vectors(ws, vi).done

    assert done == {"episodic": 1}
    assert vi.ids_by_class(["episodic"]) == {r1["id"], r2["id"]}


def test_backfill_migrates_a_legacy_entities_column_before_embedding(
    tmp_path: Path, provider: _FakeEmbeddingProvider
) -> None:
    """A workspace whose vector table predates the explicit schema
    (``entities`` typed ``list<null>``) must be repaired by the very
    first backfill run, so an entry carrying real entity tags can be
    embedded into it (F-A)."""
    import lancedb

    from durin.memory.indexer import backfill_missing_vectors, reindex_one_file
    from durin.memory.store import store_memory
    from durin.memory.vector_index import _INDEX_PATH, _TABLE_NAME

    ws = tmp_path / "ws"
    r1 = store_memory(
        ws, content="nota con entidad", class_name="episodic",
        entities=["person:ada"],
    )
    reindex_one_file(ws, Path(r1["path"]))

    # Hand-create a legacy table: entities: [] on the only record, so
    # LanceDB infers list<null> -- the pre-fix shape.
    uri = str(ws.joinpath(*_INDEX_PATH))
    Path(uri).mkdir(parents=True, exist_ok=True)
    db = lancedb.connect(uri)
    db.create_table(_TABLE_NAME, data=[{
        "id": "legacy-1", "class_name": "entity_page", "summary": "s",
        "headline": "h", "path": "p", "valid_from": "", "body_length": 0,
        "vector": [1.0] + [0.0] * (provider.DIM - 1), "entities": [],
    }])

    vi = VectorIndex(ws, provider)
    done = backfill_missing_vectors(ws, vi).done

    assert done == {"episodic": 1}
    hits = vi.search("entidad", top_k=5)
    assert any(
        h["id"] == r1["id"] and h["entities"] == ["person:ada"] for h in hits
    )


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
    assert first.done == {"episodic": 1}
    assert first.cursor is None

    second = backfill_missing_vectors(ws, vi)
    assert second.done == {}
    assert second.cursor is None
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

    done = backfill_missing_vectors(ws, vi).done

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

    done = backfill_missing_vectors(ws, vi).done

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

    result = backfill_missing_vectors(ws, vi, classes=("episodic", "stable"))

    # Stops after the first entry (episodic) with count 0; "stable" is
    # never reached, and there is nothing to resume from.
    assert result.done == {"episodic": 0}
    assert result.cursor is None


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

    done = backfill_missing_vectors(ws, vi).done

    assert done == {"episodic": 0}
    # Exactly five attempts, not all six missing entries.
    assert len(calls) == 5


def test_backfill_stops_at_the_limit_and_the_cursor_resumes_across_classes(
    tmp_path: Path, provider: _FakeEmbeddingProvider
) -> None:
    """`limit` bounds one call; the returned cursor lets the next call
    continue after the last id attempted — into the next class when the
    first one is exhausted — until nothing is missing."""
    from durin.memory.indexer import backfill_missing_vectors, reindex_one_file
    from durin.memory.store import store_memory

    ws = tmp_path / "ws"
    episodic = [
        store_memory(ws, content=f"nota {i}", class_name="episodic")
        for i in range(3)
    ]
    stable = store_memory(ws, content="hecho estable", class_name="stable")
    for r in [*episodic, stable]:
        reindex_one_file(ws, Path(r["path"]))
    vi = VectorIndex(ws, provider)
    classes = ("episodic", "stable")

    first = backfill_missing_vectors(ws, vi, classes=classes, limit=2)
    assert first.done == {"episodic": 2}
    assert first.cursor == ("episodic", sorted(r["id"] for r in episodic)[1])

    second = backfill_missing_vectors(
        ws, vi, classes=classes, cursor=first.cursor, limit=2,
    )
    assert second.done == {"episodic": 1, "stable": 1}
    assert second.cursor is None
    assert vi.ids_by_class(["episodic"]) == {r["id"] for r in episodic}
    assert vi.ids_by_class(["stable"]) == {stable["id"]}


def test_backfill_cursor_never_retries_an_entry_that_failed(
    tmp_path: Path, provider: _FakeEmbeddingProvider, monkeypatch
) -> None:
    """An entry that failed to embed is behind the cursor like an
    embedded one: the next chunk moves on instead of failing on it
    again (a persistently broken file must not pin the backfill)."""
    import durin.memory.indexer as indexer_mod
    from durin.memory.indexer import backfill_missing_vectors, reindex_one_file
    from durin.memory.store import store_memory

    ws = tmp_path / "ws"
    results = [
        store_memory(ws, content=f"nota {i}", class_name="episodic")
        for i in range(2)
    ]
    for r in results:
        reindex_one_file(ws, Path(r["path"]))
    first_id, second_id = sorted(r["id"] for r in results)
    vi = VectorIndex(ws, provider)
    attempted: list[str] = []

    def _fail_first(workspace, path, vi):
        attempted.append(path.stem)
        return path.stem != first_id

    monkeypatch.setattr(indexer_mod, "reindex_one_file_vector", _fail_first)

    first = backfill_missing_vectors(ws, vi, limit=1)
    assert first.done == {"episodic": 0}
    assert first.cursor == ("episodic", first_id)

    second = backfill_missing_vectors(ws, vi, cursor=first.cursor, limit=1)
    assert second.done == {"episodic": 1}
    assert second.cursor is None
    assert attempted == [first_id, second_id]


def test_backfill_cursor_from_another_class_set_starts_from_the_first_id(
    tmp_path: Path, provider: _FakeEmbeddingProvider
) -> None:
    from durin.memory.indexer import backfill_missing_vectors, reindex_one_file
    from durin.memory.store import store_memory

    ws = tmp_path / "ws"
    r = store_memory(ws, content="nota", class_name="episodic")
    reindex_one_file(ws, Path(r["path"]))
    vi = VectorIndex(ws, provider)

    result = backfill_missing_vectors(
        ws, vi, classes=("episodic",), cursor=("stable", "zzz"),
    )
    assert result.done == {"episodic": 1}
    assert result.cursor is None
