import json
from datetime import datetime, timezone

from durin.memory.dream_passes import run_extract_pass, run_refine_pass
from durin.memory.entity_page import EntityPage
from durin.memory.field_patch import FieldPatch
from durin.memory.memory_writer import write_entity

NOW = datetime(2026, 6, 5, tzinfo=timezone.utc)


def _session(ws, key, msgs):
    sdir = ws / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"_type": "metadata", "key": key})] + [json.dumps(m) for m in msgs]
    (sdir / f"{key}.jsonl").write_text("\n".join(lines), encoding="utf-8")


def _upsert(ref):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c", "type": "function", "function": {
            "name": "memory_upsert_entity", "arguments": json.dumps({"ref": ref})}}]}


def _stub(text):
    return lambda p, **k: text


def test_extract_pass_over_sessions(tmp_path):
    write_entity(tmp_path, "company:a", [FieldPatch(kind="body_append", value="b",
                 author="agent", source_ref="s", at=NOW)], create=True, name="A")
    write_entity(tmp_path, "company:b", [FieldPatch(kind="body_append", value="b",
                 author="agent", source_ref="s", at=NOW)], create=True, name="B")
    _session(tmp_path, "s1", [{"role": "user", "content": "A founded 2001"}, _upsert("company:a")])
    _session(tmp_path, "s2", [{"role": "user", "content": "B founded 2002"}, _upsert("company:b")])
    out = run_extract_pass(tmp_path, llm_invoke=_stub('{"founding_year":2000}'))
    assert out["sessions"] == 2 and out["entities"] == 2 and not out["errors"]
    assert EntityPage.from_file(tmp_path / "memory/entities/company/a.md").attributes.get("founding_year") == 2000
    # idempotent: a second pass finds no new turns
    out2 = run_extract_pass(tmp_path, llm_invoke=_stub('{"x":1}'))
    assert out2["entities"] == 0


def test_refine_pass(tmp_path):
    write_entity(tmp_path, "company:x", [FieldPatch(kind="alias", value="X",
                 author="agent", source_ref="s", at=NOW)], create=True, name="X Inc")
    write_entity(tmp_path, "company:x_inc", [FieldPatch(kind="alias", value="X",
                 author="agent", source_ref="s", at=NOW)], create=True, name="X Incorporated")
    out = run_refine_pass(tmp_path, llm_invoke=_stub(
        "===VERDICT===\nsame\n===CONFIDENCE===\n98\n===REASONING===\nx\n===END==="))
    assert out["merged"]
    assert not (tmp_path / "memory/entities/company/x_inc.md").exists()
