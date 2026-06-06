"""N5: the index records which embedding model built it (so a later model swap
is detected). reindex writes it; ensure_index_fresh detects a change and rebuilds
the vector (a same-dimension swap would otherwise silently return stale results)."""
from datetime import datetime, timezone

from durin.memory.field_patch import FieldPatch
from durin.memory.index_meta import (
    CURRENT_SCHEMA_VERSION,
    IndexMeta,
    load_index_meta,
    record_built_model,
    save_index_meta,
)
from durin.memory.indexer import ensure_index_fresh
from durin.memory.memory_writer import write_entity

NOW = datetime(2026, 6, 5, tzinfo=timezone.utc)
MODEL = "intfloat/multilingual-e5-small"


def _entity(ws):
    write_entity(ws, "person:zoe", [FieldPatch(kind="body_append",
                 value="Zoe leads the platform team.", author="agent",
                 source_ref="s", at=NOW)], create=True, name="Zoe")


def test_record_built_model_writes_and_tracks_previous(tmp_path):
    # N5a: reindex records the model it built with (preserving schema + history).
    save_index_meta(tmp_path, IndexMeta(
        schema_version=CURRENT_SCHEMA_VERSION, embedding_model_id="old-x"))
    record_built_model(tmp_path, "new-y")
    meta = load_index_meta(tmp_path)
    assert meta.embedding_model_id == "new-y"
    assert "old-x" in meta.previous_models
    assert meta.schema_version == CURRENT_SCHEMA_VERSION


def test_ensure_index_fresh_detects_model_change(tmp_path):
    # N5b: stored model differs from the configured one → rebuild + record.
    _entity(tmp_path)
    save_index_meta(tmp_path, IndexMeta(
        schema_version=CURRENT_SCHEMA_VERSION, embedding_model_id="old-model-x"))
    out = ensure_index_fresh(tmp_path, embedding_model=MODEL)
    assert out["rebuilt"] and "model" in out["reason"]
    meta = load_index_meta(tmp_path)
    assert meta.embedding_model_id == MODEL                  # new model recorded
    assert "old-model-x" in meta.previous_models             # audit trail


def test_ensure_index_fresh_no_rebuild_when_model_matches(tmp_path):
    _entity(tmp_path)
    save_index_meta(tmp_path, IndexMeta(
        schema_version=CURRENT_SCHEMA_VERSION, embedding_model_id=MODEL))
    out = ensure_index_fresh(tmp_path, embedding_model=MODEL)
    assert not out["rebuilt"] and out["reason"] == "current"
