"""LanceDB row schema must NOT carry a `body` column (audit A4).

The `.md` file on disk is the single source of truth for entry
content. LanceDB stores metadata + vector for retrieval; the body
is read on demand from disk via
`memory_search.MemorySearchTool._enrich_body`.

P2.5 (commit a266344) briefly introduced a body column for a
cold-tier latency micro-optimisation; audit A4 reverted it because
the optimisation was prematurely introduced (no benchmark showed
the file reads as bottleneck) and the duplication opened a drift
window between disk edits and LanceDB reads.

These tests enforce the post-A4 invariant. If a future change
re-introduces a body column, this test fails loudly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from durin.memory.vector_index import vector_index_available

pytestmark = pytest.mark.skipif(
    not vector_index_available(),
    reason="lancedb not installed; install durin[memory]",
)


def _build_index(workspace: Path):
    """Build a real VectorIndex with a deterministic fake provider so
    we can inspect the row layout without depending on fastembed."""
    from durin.memory.embedding import EmbeddingProvider
    from durin.memory.vector_index import VectorIndex

    class _FakeProvider(EmbeddingProvider):
        def embed(self, texts):
            return [[float(len(t))] * 4 for t in texts]

        def dimensions(self) -> int:
            return 4

        def model_name(self) -> str:
            return "fake-test-model"

    return VectorIndex(workspace, _FakeProvider())


def test_entry_record_has_no_body_column(tmp_path: Path) -> None:
    """Storing an entry persists 8 columns + `vector` — NOT 9."""
    from durin.memory.store import store_memory

    idx = _build_index(tmp_path)
    stored = store_memory(
        tmp_path,
        content="this is the body text — should NOT be in LanceDB",
        class_name="episodic",
        headline="example",
    )
    # Build the record via the same code path used by upsert.
    from durin.memory.storage import load_entry

    entry = load_entry(Path(stored["path"]))
    record = idx._record_for(entry, "episodic", Path(stored["path"]))

    assert "body" not in record, (
        "regression: P2.5 was reverted in A4. The `.md` on disk is "
        "the single source of truth for body content; LanceDB stores "
        "metadata + vector only. If a body column is needed for a "
        "specific consumer, implement an on-demand disk read inside "
        "that consumer's module (see search_pipeline._cross_encoder_rerank "
        "for the rationale)."
    )
    # H5 (audit 2026-05-29): ``body_length`` (int) was added so the
    # renderer can emit the per-hit completeness qualifier. This is
    # metadata about the body, NOT the body itself — A4 prohibited
    # the latter, not the former.
    expected_columns = {
        "id", "class_name", "summary", "headline", "vector",
        "valid_from", "entities", "path", "body_length",
    }
    assert set(record.keys()) == expected_columns, (
        f"row schema drifted from doc 02 §3.1 — expected "
        f"{expected_columns}, got {set(record.keys())}"
    )


def test_entity_page_record_has_no_body_column(tmp_path: Path) -> None:
    """The entity-page write path (upsert_entity_page) must also be
    body-free. The same _connect/list_tables flow can't run in a
    unit test without a real lancedb file, so we inspect the dict
    that upsert_entity_page builds by patching out the DB call."""
    idx = _build_index(tmp_path)

    captured: dict = {}

    def _fake_connect():
        class _FakeTable:
            def delete(self, *_a, **_kw):
                return None

            def add(self, rows):
                captured["rows"] = rows

        class _FakeTables:
            tables = ["memory_entries"]

        class _FakeDB:
            @staticmethod
            def list_tables():
                return _FakeTables()

            @staticmethod
            def open_table(_name):
                return _FakeTable()

            @staticmethod
            def create_table(_name, *, data):
                captured["rows"] = data

        return _FakeDB()

    idx._connect = _fake_connect  # type: ignore[assignment]
    idx._guard_dim_match = lambda *_a, **_kw: None  # type: ignore[assignment]
    idx.upsert_entity_page(
        entity_ref="person:marcelo",
        name="Marcelo",
        aliases=["Marcelo Marmol"],
        body="this is the entity body — should NOT be in LanceDB",
        path=tmp_path / "memory" / "entities" / "person" / "marcelo.md",
    )

    row = captured["rows"][0]
    assert "body" not in row, (
        "regression: entity-page row gained a body column. Audit A4 "
        "removed it. The `.md` on disk is the source of truth."
    )
