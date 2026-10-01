"""N2: the reactive index path (file watcher) re-embeds entity pages into the
vector index. Previously NOTHING embedded them reactively (memory_upsert_entity /
the extract dream never did; reindex_one_file is FTS-only), so new/edited entities
were vector-stale until a merge or full reindex."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from durin.memory.embedding import FastembedProvider
from durin.memory.field_patch import FieldPatch
from durin.memory.file_watcher import MemoryFileWatcher
from durin.memory.memory_writer import write_entity
from durin.memory.vector_index import VectorIndex, vector_index_available

# The vector index needs lancedb (the `memory` extra); the model is the
# fastembed stand-in, in this process and in the embedding pool's workers.
pytestmark = [
    pytest.mark.skipif(
        not vector_index_available(),
        reason="vector deps (memory extra: fastembed/lancedb) absent",
    ),
    pytest.mark.usefixtures("fastembed_stand_in"),
]

NOW = datetime(2026, 6, 5, tzinfo=timezone.utc)
MODEL = "intfloat/multilingual-e5-small"


def _body(text):
    return [FieldPatch(kind="body_append", value=text, author="agent", source_ref="s", at=NOW)]


# Other pages in the index, written and embedded the same way, so a search has
# to rank the right row first instead of returning the only row there is. Each
# shares words with one of the queries below, so a row embedded from stale
# text, or not embedded at all, does not come first.
_OTHERS = {
    "ana": "Ana teaches quantum physics at the university.",
    "bob": "Bob leads the billing team.",
    "eve": "Eve keeps the team database backups.",
    "ivo": "Ivo carries a sword and a shield into battle.",
}


def _index_others(tmp_path):
    watcher = MemoryFileWatcher(tmp_path, embedding_model=MODEL)
    for slug, text in _OTHERS.items():
        write_entity(tmp_path, f"person:{slug}", _body(text), create=True, name=slug.title())
        watcher._reindex_path(tmp_path / f"memory/entities/person/{slug}.md")


def _ranked(tmp_path, query):
    vi = VectorIndex(tmp_path, FastembedProvider(MODEL))
    return [str(h.get("id", "")) for h in vi.search(query, top_k=10)]


def test_watcher_embeds_authored_entity(tmp_path):
    _index_others(tmp_path)
    query = "who leads the platform infrastructure team"
    # write_entity does NOT embed at author time → baseline vector miss (the gap)
    write_entity(tmp_path, "person:zoe", _body("Zoe leads the platform infrastructure team."),
                 create=True, name="Zoe")
    md = tmp_path / "memory/entities/person/zoe.md"
    assert "person:zoe" not in _ranked(tmp_path, query)
    # the reactive path (watcher) embeds it
    MemoryFileWatcher(tmp_path, embedding_model=MODEL)._reindex_path(md)
    assert _ranked(tmp_path, query)[:1] == ["person:zoe"]


def test_watcher_reembeds_edited_entity_body(tmp_path):
    _index_others(tmp_path)
    query = "quantum cryptography research"
    write_entity(tmp_path, "person:zoe", _body("Zoe works on billing."), create=True, name="Zoe")
    md = tmp_path / "memory/entities/person/zoe.md"
    w = MemoryFileWatcher(tmp_path, embedding_model=MODEL)
    w._reindex_path(md)  # embed v1
    assert _ranked(tmp_path, query)[:1] == ["person:ana"]
    # user hand-edits the page body (Obsidian)
    md.write_text(md.read_text(encoding="utf-8").replace(
        "billing", "quantum cryptography research"), encoding="utf-8")
    w._reindex_path(md)  # re-embed v2 → search reflects the edit
    assert _ranked(tmp_path, query)[:1] == ["person:zoe"]


def test_watcher_vector_disabled_without_model(tmp_path):
    # no embedding model → FTS only, vector half is a no-op (must not crash)
    write_entity(tmp_path, "person:zoe", _body("Zoe."), create=True, name="Zoe")
    w = MemoryFileWatcher(tmp_path)  # no embedding_model
    w._reindex_path(tmp_path / "memory/entities/person/zoe.md")
    assert w._get_vector_index() is None


def test_watcher_embeds_stored_memory_entry(tmp_path):
    from durin.memory.store import store_memory

    _index_others(tmp_path)
    result = store_memory(
        tmp_path, content="Bruenor prefers his double-bladed battle-axe.",
        class_name="episodic",
    )
    md = Path(result["path"])
    query = "which battle-axe does Bruenor carry"
    assert result["id"] not in _ranked(tmp_path, query)
    MemoryFileWatcher(tmp_path, embedding_model=MODEL)._reindex_path(md)
    assert _ranked(tmp_path, query)[:1] == [result["id"]]


def test_watcher_embeds_session_summary(tmp_path):
    from durin.memory.session_summary_store import write_session_summary
    from durin.memory.storage import load_entry

    _index_others(tmp_path)
    path = write_session_summary(
        tmp_path, "session-1", "The team decided to migrate the database to Postgres.",
    )
    entry_id = load_entry(path).id
    query = "which database did the team migrate to"
    assert entry_id not in _ranked(tmp_path, query)
    MemoryFileWatcher(tmp_path, embedding_model=MODEL)._reindex_path(path)
    assert _ranked(tmp_path, query)[:1] == [entry_id]
