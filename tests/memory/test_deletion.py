from datetime import datetime, timezone

from durin.memory.deletion import (
    clear_delete_tombstone,
    delete_entity,
    delete_reference,
    is_deleted,
    unmerge,
)
from durin.memory.extract_dream import extract_entity
from durin.memory.field_patch import FieldPatch
from durin.memory.memory_writer import write_entity
from durin.memory.reference import ingest_reference
from durin.memory.refine_dream import is_tombstoned, run_refine

NOW = datetime(2026, 6, 5, tzinfo=timezone.utc)


def _stub(text):
    def inv(prompt, **kw):
        return text
    return inv


def _judge_same(prompt, **kw):
    return "===VERDICT===\nsame\n===CONFIDENCE===\n98\n===REASONING===\nstub\n===END==="


def _entity(ws, ref, name="X"):
    write_entity(ws, ref, [FieldPatch(kind="body_append", value="b", author="agent",
                                      source_ref="s", at=NOW)], create=True, name=name)


def test_delete_entity_archives_and_tombstones(tmp_path):
    _entity(tmp_path, "company:globex")
    dest = delete_entity(tmp_path, "company:globex", reason="user_delete")
    assert is_deleted(tmp_path, "company:globex")
    assert not (tmp_path / "memory/entities/company/globex.md").exists()
    assert dest.exists() and "deleted: true" in dest.read_text()


def test_extract_respects_delete_tombstone(tmp_path):
    _entity(tmp_path, "company:globex")
    delete_entity(tmp_path, "company:globex")
    r = extract_entity(tmp_path, "company:globex", "Globex makes reactors.",
                       llm_invoke=_stub('{"product":"reactors"}'))
    assert r.committed is False                                  # tombstone respected
    assert not (tmp_path / "memory/entities/company/globex.md").exists()


def test_clear_tombstone_overrides(tmp_path):
    _entity(tmp_path, "company:globex")
    delete_entity(tmp_path, "company:globex")
    clear_delete_tombstone(tmp_path, "company:globex")
    assert not is_deleted(tmp_path, "company:globex")
    r = extract_entity(tmp_path, "company:globex", "Globex makes reactors.",
                       llm_invoke=_stub('{"product":"reactors"}'))
    assert r.committed is True                                   # re-creation allowed


def test_delete_reference(tmp_path):
    res = ingest_reference(tmp_path, "Big Doc", "para one.\n\npara two.")
    dest = delete_reference(tmp_path, res.ref)
    assert is_deleted(tmp_path, res.ref)
    assert not (tmp_path / "memory/references/big-doc.md").exists()
    assert dest.exists() and "deleted: true" in dest.read_text()


def test_unmerge_restores_and_tombstones(tmp_path):
    write_entity(tmp_path, "company:mxhero",
                 [FieldPatch(kind="alias", value="mxHERO", author="agent",
                             source_ref="s", at=NOW)], create=True, name="mxHERO Inc.")
    write_entity(tmp_path, "company:mxhero_inc",
                 [FieldPatch(kind="alias", value="mxHERO", author="agent",
                             source_ref="s", at=NOW)], create=True, name="mxHERO Incorporated")
    out = run_refine(tmp_path, llm_invoke=_judge_same)
    assert out["merged"]
    assert not (tmp_path / "memory/entities/company/mxhero_inc.md").exists()

    restored = unmerge(tmp_path, "company:mxhero", "company:mxhero_inc")
    assert restored is True
    assert (tmp_path / "memory/entities/company/mxhero_inc.md").exists()      # back
    assert is_tombstoned(tmp_path, "company:mxhero", "company:mxhero_inc")
    # the refine never re-merges the pair
    out2 = run_refine(tmp_path, llm_invoke=_judge_same)
    assert not out2["merged"]
    assert any(s["reason"] == "tombstoned" for s in out2["skipped"])
