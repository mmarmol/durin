"""N2: the reactive index path (file watcher) re-embeds entity pages into the
vector index. Previously NOTHING embedded them reactively (memory_upsert_entity /
the extract dream never did; reindex_one_file is FTS-only), so new/edited entities
were vector-stale until a merge or full reindex."""
from datetime import datetime, timezone

from durin.memory.embedding import FastembedProvider
from durin.memory.field_patch import FieldPatch
from durin.memory.file_watcher import MemoryFileWatcher
from durin.memory.memory_writer import write_entity
from durin.memory.vector_index import VectorIndex

NOW = datetime(2026, 6, 5, tzinfo=timezone.utc)
MODEL = "intfloat/multilingual-e5-small"


def _body(text):
    return [FieldPatch(kind="body_append", value=text, author="agent", source_ref="s", at=NOW)]


def _vector_has(tmp_path, query, ref):
    vi = VectorIndex(tmp_path, FastembedProvider(MODEL))
    hits = vi.search(query, top_k=10)
    return any(ref in str(h.get("id", "")) for h in hits)


def test_watcher_embeds_authored_entity(tmp_path):
    # write_entity does NOT embed at author time → baseline vector miss (the gap)
    write_entity(tmp_path, "person:zoe", _body("Zoe leads the platform infrastructure team."),
                 create=True, name="Zoe")
    md = tmp_path / "memory/entities/person/zoe.md"
    assert not _vector_has(tmp_path, "who leads the platform infrastructure team", "person:zoe")
    # the reactive path (watcher) embeds it
    MemoryFileWatcher(tmp_path, embedding_model=MODEL)._reindex_path(md)
    assert _vector_has(tmp_path, "who leads the platform infrastructure team", "person:zoe")


def test_watcher_reembeds_edited_entity_body(tmp_path):
    write_entity(tmp_path, "person:zoe", _body("Zoe works on billing."), create=True, name="Zoe")
    md = tmp_path / "memory/entities/person/zoe.md"
    w = MemoryFileWatcher(tmp_path, embedding_model=MODEL)
    w._reindex_path(md)  # embed v1
    # user hand-edits the page body (Obsidian)
    md.write_text(md.read_text(encoding="utf-8").replace(
        "billing", "quantum cryptography research"), encoding="utf-8")
    w._reindex_path(md)  # re-embed v2 → search reflects the edit
    assert _vector_has(tmp_path, "quantum cryptography", "person:zoe")


def test_watcher_vector_disabled_without_model(tmp_path):
    # no embedding model → FTS only, vector half is a no-op (must not crash)
    write_entity(tmp_path, "person:zoe", _body("Zoe."), create=True, name="Zoe")
    w = MemoryFileWatcher(tmp_path)  # no embedding_model
    w._reindex_path(tmp_path / "memory/entities/person/zoe.md")
    assert w._get_vector_index() is None
