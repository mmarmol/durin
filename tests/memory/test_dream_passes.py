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


def test_extract_pass_discovers_and_can_be_disabled(tmp_path):
    # a durable fact stated WITHOUT any upsert → discovered as a dream entity
    _session(tmp_path, "s1", [{"role": "user", "content": "My co-founder is Ana."}])
    out = run_extract_pass(tmp_path, llm_invoke=_stub(
        '[{"ref":"person:ana","name":"Ana","attributes":{"role":"co-founder"}}]'))
    assert out["discovered"] == 1
    page = EntityPage.from_file(tmp_path / "memory/entities/person/ana.md")
    assert page.attributes["role"] == "co-founder"

    # discover=False → stage 2 skipped entirely
    _session(tmp_path, "s2", [{"role": "user", "content": "My advisor is Bob."}])
    out2 = run_extract_pass(tmp_path, discover=False, llm_invoke=_stub(
        '[{"ref":"person:bob","name":"Bob","attributes":{"role":"advisor"}}]'))
    assert out2.get("discovered", 0) == 0
    assert not (tmp_path / "memory/entities/person/bob.md").exists()


def test_refine_pass(tmp_path):
    write_entity(tmp_path, "company:x", [FieldPatch(kind="alias", value="X",
                 author="agent", source_ref="s", at=NOW)], create=True, name="X Inc")
    write_entity(tmp_path, "company:x_inc", [FieldPatch(kind="alias", value="X",
                 author="agent", source_ref="s", at=NOW)], create=True, name="X Incorporated")
    out = run_refine_pass(tmp_path, llm_invoke=_stub(
        "===VERDICT===\nsame\n===CONFIDENCE===\n98\n===REASONING===\nx\n===END==="))
    assert out["merged"]
    assert not (tmp_path / "memory/entities/company/x_inc.md").exists()


def test_dreams_emit_telemetry(tmp_path, monkeypatch):
    # the new dreams must emit dashboard-compatible telemetry (the legacy did).
    import durin.agent.tools._telemetry as T
    events = []
    monkeypatch.setattr(T, "emit_tool_event", lambda ev, data: events.append(ev))
    write_entity(tmp_path, "company:a", [FieldPatch(kind="alias", value="A",
                 author="agent", source_ref="s", at=NOW)], create=True, name="A Inc")
    write_entity(tmp_path, "company:a2", [FieldPatch(kind="alias", value="A",
                 author="agent", source_ref="s", at=NOW)], create=True, name="A Incorporated")
    _session(tmp_path, "s1", [{"role": "user", "content": "A founded 2001"}, _upsert("company:a")])
    run_extract_pass(tmp_path, llm_invoke=_stub('{"founded_year":2001}'))
    run_refine_pass(tmp_path, llm_invoke=_stub(
        "===VERDICT===\nsame\n===CONFIDENCE===\n98\n===REASONING===\nx\n===END==="))
    assert "memory.dream.start" in events and "memory.dream.end" in events
    assert "memory.dream.patch_applied" in events           # extract
    assert "memory.absorb.judged" in events and "memory.absorb.auto_merged" in events  # refine


def test_skill_extract_no_sessions_is_noop(tmp_path):
    from durin.memory.dream_passes import run_skill_extract_pass
    out = run_skill_extract_pass(tmp_path)
    assert out["skills_touched"] == 0


def test_reactive_gate_serializes_and_throttles():
    # The reactive gate replaces the legacy DreamRunner lock + cooldown:
    # one pass at a time, and a burst within min_seconds is absorbed.
    from durin.memory.dream_passes import ReactiveDreamGate
    g = ReactiveDreamGate()
    assert g.try_begin(300) == ""           # first run allowed
    assert g.try_begin(300) == "locked"     # a pass is already in progress
    g.end()
    assert g.try_begin(300) == "throttled"  # one just ended (within the window)
    assert g.try_begin(0) == ""             # throttle disabled → allowed
    g.end()


def test_extract_pass_respects_max_seconds(tmp_path):
    import time as _t
    for i in range(3):
        _session(tmp_path, f"s{i}", [{"role": "user", "content": f"X{i}"},
                                     _upsert(f"company:c{i}")])

    def slow_stub(*a, **k):
        _t.sleep(0.05)
        return '{"founded_year": 2001}'

    out = run_extract_pass(tmp_path, llm_invoke=slow_stub, max_seconds=0.001)
    assert out["yielded"] is True
    assert out["sessions"] < 3             # yielded before processing all sessions


def test_refine_pass_respects_auto_absorb_enabled(tmp_path):
    # A1: the refine pass must honour auto_absorb.enabled. Disabled (the
    # conservative default) → no judge call, no merge; enabled → auto-merge.
    write_entity(tmp_path, "company:a", [FieldPatch(kind="alias", value="A",
                 author="agent", source_ref="s", at=NOW)], create=True, name="A Inc")
    write_entity(tmp_path, "company:a2", [FieldPatch(kind="alias", value="A",
                 author="agent", source_ref="s", at=NOW)], create=True, name="A Incorporated")
    calls = []

    def judge_stub(*a, **k):
        calls.append(1)
        return "===VERDICT===\nsame\n===CONFIDENCE===\n99\n===REASONING===\nx\n===END==="

    out = run_refine_pass(tmp_path, llm_invoke=judge_stub, enabled=False)
    assert out.get("disabled") is True
    assert out["merged"] == []
    assert calls == []  # the judge LLM is never invoked when disabled

    out2 = run_refine_pass(tmp_path, llm_invoke=judge_stub, enabled=True,
                           confidence_threshold=95)
    assert len(out2["merged"]) == 1
    assert calls  # judge ran when enabled
