"""Mention-based entity discovery — the dream grows the entity graph from
conversation, not only from the agent's explicit memory_upsert_entity calls
(design: .workdocs/superpowers/specs/2026-06-18-memory-entity-building-design.md)."""
import json
from datetime import datetime, timezone

from durin.memory.deletion import delete_entity, is_deleted
from durin.memory.entity_page import EntityPage
from durin.memory.extract_dream import discover_entities, parse_discoveries
from durin.memory.extract_runner import run_extract_for_session
from durin.memory.field_patch import FieldPatch
from durin.memory.memory_writer import write_entity

NOW = datetime(2026, 6, 5, tzinfo=timezone.utc)


def _stub(text):
    def inv(prompt, **kw):
        return text
    return inv


def _page_path(ws, ref):
    t, _, s = ref.partition(":")
    return ws / "memory/entities" / t / f"{s}.md"


def _write_session(ws, key, messages):
    sdir = ws / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    p = sdir / f"{key}.jsonl"
    lines = [json.dumps({"_type": "metadata", "key": key})]
    lines += [json.dumps(m) for m in messages]
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


# --- parse_discoveries ------------------------------------------------------

def test_parse_discoveries_validates_and_filters():
    raw = (
        '```json\n'
        '[{"ref":"person:ana","name":"Ana","attributes":{"role":"co-founder","x":{"y":1}}},'
        ' {"ref":"no-colon","name":"X","attributes":{}},'
        ' {"name":"missing ref","attributes":{}}]\n'
        '```'
    )
    # nested attr dropped (parse_attributes filter); invalid/absent ref dropped
    assert parse_discoveries(raw) == [
        {"ref": "person:ana", "name": "Ana", "attributes": {"role": "co-founder"}},
    ]


def test_parse_discoveries_non_list_is_empty():
    assert parse_discoveries('{"hq":"SF"}') == []
    assert parse_discoveries("no json here") == []


# --- discover_entities ------------------------------------------------------

def test_discover_creates_new_entity_as_dream(tmp_path):
    turns = "USER: my co-founder is Ana Pérez, she leads design."
    out = discover_entities(
        tmp_path, turns,
        llm_invoke=_stub(
            '[{"ref":"person:ana_perez","name":"Ana Pérez",'
            '"attributes":{"role":"co-founder"}}]'),
    )
    assert out == [{"ref": "person:ana_perez", "committed": True}]
    page = EntityPage.from_file(_page_path(tmp_path, "person:ana_perez"))
    assert page.name == "Ana Pérez"
    assert page.attributes["role"] == "co-founder"
    # discovered attributes are dream-authored (user/agent override later)
    assert page.provenance["attributes"]["role"]["author"] == "dream"


def test_discover_skips_refs_already_handled_in_stage1(tmp_path):
    out = discover_entities(
        tmp_path, "turns",
        existing_refs=["person:ana_perez"],
        llm_invoke=_stub(
            '[{"ref":"person:ana_perez","name":"Ana","attributes":{"role":"x"}}]'),
    )
    assert out == []
    assert not _page_path(tmp_path, "person:ana_perez").exists()


def test_discover_respects_delete_tombstone(tmp_path):
    write_entity(tmp_path, "person:ana_perez",
                 [FieldPatch(kind="body_append", value="Ana", author="agent",
                             source_ref="s", at=NOW)], create=True, name="Ana")
    delete_entity(tmp_path, "person:ana_perez")
    assert is_deleted(tmp_path, "person:ana_perez")
    out = discover_entities(
        tmp_path, "turns",
        llm_invoke=_stub(
            '[{"ref":"person:ana_perez","name":"Ana","attributes":{"role":"x"}}]'),
    )
    assert out == []  # a user-deleted entity is never re-created


def test_discover_empty_output_is_noop(tmp_path):
    out = discover_entities(tmp_path, "turns", llm_invoke=_stub("nothing here"))
    assert out == []


# --- wired into run_extract_for_session (stage 2) ---------------------------

def test_run_discovers_non_upserted_facts(tmp_path):
    # the user states a durable fact; the agent never calls memory_upsert_entity
    p = _write_session(tmp_path, "s1", [
        {"role": "user", "content": "My co-founder is Ana Pérez."},
    ])
    out = run_extract_for_session(
        tmp_path, p,
        llm_invoke=_stub(
            '[{"ref":"person:ana_perez","name":"Ana Pérez",'
            '"attributes":{"role":"co-founder"}}]'),
    )
    assert out["extracted"] == []  # nothing upserted -> stage 1 finds nothing
    assert {"ref": "person:ana_perez", "committed": True} in out["discovered"]
    page = EntityPage.from_file(_page_path(tmp_path, "person:ana_perez"))
    assert page.attributes["role"] == "co-founder"


def test_run_discover_disabled_skips_stage2(tmp_path):
    p = _write_session(tmp_path, "s1", [
        {"role": "user", "content": "My co-founder is Ana Pérez."},
    ])
    out = run_extract_for_session(
        tmp_path, p, discover=False,
        llm_invoke=_stub(
            '[{"ref":"person:ana_perez","name":"Ana Pérez",'
            '"attributes":{"role":"co-founder"}}]'),
    )
    assert out.get("discovered", []) == []
    assert not _page_path(tmp_path, "person:ana_perez").exists()
