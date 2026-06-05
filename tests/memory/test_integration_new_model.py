"""Cross-module integration + edge-case tests for the new memory model.

Composes the Phase 1-7 modules and the existing readers on shared state, plus
the edge cases the per-phase unit tests didn't cover. Surfaced two real fixes
(attribute search, the _merge_pages data loss) during the pre-Phase-8 review.
"""
import threading
from datetime import datetime, timezone

import pytest

from durin.memory.entity_page import EntityPage
from durin.memory.extract_dream import extract_entity
from durin.memory.field_patch import FieldPatch
from durin.memory.graph import build_memory_graph
from durin.memory.memory_writer import write_entity
from durin.memory.reference import chunk_by_tokens, ingest_reference
from durin.memory.search import search_memory

NOW = datetime(2026, 6, 5, tzinfo=timezone.utc)

_OLD_FORMAT = (
    "---\n"
    "type: company\n"
    "name: OldCo\n"
    "attributes:\n"
    "  hq: Boston\n"
    "provenance:\n"            # old shape: source_ref only, no author/extracted_at
    "  attributes:\n"
    "    hq:\n"
    "      source_ref: '[[old]]'\n"
    "author: user_authored\n"
    "---\n\n"
    "OldCo is an old-format entity page.\n"
)


def _write_raw(ws, ref, text):
    type_, _, slug = ref.partition(":")
    p = ws / "memory" / "entities" / type_ / f"{slug}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_old_format_entity_readable_and_in_graph(tmp_path):
    _write_raw(tmp_path, "company:oldco", _OLD_FORMAT)
    page = EntityPage.from_file(tmp_path / "memory/entities/company/oldco.md")
    assert page is not None and page.attributes["hq"] == "Boston"
    g = build_memory_graph(tmp_path, include_sessions=False)
    assert any(n["id"] == "company:oldco" for n in g["nodes"])


def test_concurrent_same_field_precedence_user_wins(tmp_path):
    # Two writers race on the SAME field; the user value must win regardless of
    # commit order (precedence holds under optimistic CAS retry).
    write_entity(tmp_path, "company:x",
                 [FieldPatch(kind="body_append", value="seed", author="agent",
                             source_ref="s", at=NOW)], create=True)

    def w(author, val):
        write_entity(tmp_path, "company:x",
                     [FieldPatch(kind="attribute", key="hq", value=val,
                                 author=author, source_ref="s", at=NOW)])

    ts = [threading.Thread(target=w, args=("agent", "AgentVal")),
          threading.Thread(target=w, args=("user", "UserVal"))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    page = EntityPage.from_file(tmp_path / "memory/entities/company/x.md")
    assert page.attributes["hq"] == "UserVal"


def test_attribute_and_relation_searchable(tmp_path):
    # Dream-extracted attributes + relation targets must be findable by search
    # (the warm grep used to only see name/aliases/body).
    write_entity(tmp_path, "company:acme",
                 [FieldPatch(kind="attribute", key="hq", value="Boston",
                             author="dream", source_ref="s", at=NOW),
                  FieldPatch(kind="relation", value={"to": "person:alex", "type": "ceo"},
                             author="agent", source_ref="s", at=NOW)],
                 create=True, name="Acme")
    assert [r for r in search_memory(tmp_path, "Boston") if r.class_name == "entity_page"]
    assert [r for r in search_memory(tmp_path, "alex") if r.class_name == "entity_page"]


def test_reference_edge_cases(tmp_path):
    assert chunk_by_tokens("") == []
    assert len(chunk_by_tokens("a short note")) == 1
    assert ingest_reference(tmp_path, "Empty", "").chunk_count == 0
    assert ingest_reference(tmp_path, "OneWord", "supercalifragilistic").chunk_count == 1


def test_extract_handles_malformed_llm_output(tmp_path):
    write_entity(tmp_path, "company:x",
                 [FieldPatch(kind="body_append", value="b", author="agent",
                             source_ref="s", at=NOW)], create=True)
    for bad in ["not json", "[1,2,3]", "```\ngarbage\n```", '{"a":{"nested":1}}']:
        res = extract_entity(tmp_path, "company:x", "turns", llm_invoke=lambda p, **k: bad)
        assert res.committed is False


def test_uncommitted_on_disk_entity_errors_not_clobbers(tmp_path):
    # An entity present in the working tree but not committed (a manual / Obsidian
    # edit) is invisible to memory_writer (which reads HEAD). create=False must
    # ERROR rather than silently proceed — the safe failure. (The create=True
    # clobber is the human-edit phase; see checkpoint_prephase8_readiness.md.)
    _write_raw(tmp_path, "company:oldco", _OLD_FORMAT)
    with pytest.raises(FileNotFoundError):
        write_entity(tmp_path, "company:oldco",
                     [FieldPatch(kind="relation", value={"to": "person:x", "type": "ceo"},
                                 author="agent", source_ref="s", at=NOW)])
