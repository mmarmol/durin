import json
from datetime import datetime, timezone

from durin.memory.entity_page import EntityPage
from durin.memory.extract_runner import (
    entity_refs_in_messages,
    get_extract_cursor,
    run_extract_for_session,
    set_extract_cursor,
)
from durin.memory.field_patch import FieldPatch
from durin.memory.memory_writer import write_entity

NOW = datetime(2026, 6, 5, tzinfo=timezone.utc)


def _write_session(ws, key, messages):
    sdir = ws / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    p = sdir / f"{key}.jsonl"
    lines = [json.dumps({"_type": "metadata", "key": key})]
    lines += [json.dumps(m) for m in messages]
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def _upsert_call(ref):
    return {
        "role": "assistant", "content": "",
        "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {
                "name": "memory_upsert_entity",
                "arguments": json.dumps({"ref": ref, "name": ref.split(":")[1]}),
            },
        }],
    }


def _stub(text):
    def inv(prompt, **kw):
        return text
    return inv


def test_discovery_from_upsert_calls():
    msgs = [
        {"role": "user", "content": "about mxhero"},
        _upsert_call("company:mxhero"),
        {"role": "user", "content": "more"},
    ]
    assert entity_refs_in_messages(msgs) == ["company:mxhero"]


def test_cursor_roundtrip(tmp_path):
    p = _write_session(tmp_path, "s1", [{"role": "user", "content": "hi"}])
    assert get_extract_cursor(p) == 0
    set_extract_cursor(p, 5)
    assert get_extract_cursor(p) == 5


def test_run_extracts_and_advances_cursor(tmp_path):
    write_entity(tmp_path, "company:mxhero",
                 [FieldPatch(kind="body_append", value="mxhero", author="agent",
                             source_ref="s", at=NOW)], create=True, name="mxHERO")
    p = _write_session(tmp_path, "s1", [
        {"role": "user", "content": "mxHERO was founded in 2012 and makes mail2cloud."},
        _upsert_call("company:mxhero"),
    ])
    out = run_extract_for_session(
        tmp_path, p, llm_invoke=_stub('{"founding_year":2012,"product":"mail2cloud"}'))
    assert out["extracted"] == [{"ref": "company:mxhero", "committed": True}]
    assert out["cursor"] == 2
    page = EntityPage.from_file(tmp_path / "memory/entities/company/mxhero.md")
    assert page.attributes["founding_year"] == 2012
    assert page.provenance["attributes"]["founding_year"]["author"] == "dream"
    # re-run is a no-op (cursor at end of session)
    out2 = run_extract_for_session(tmp_path, p, llm_invoke=_stub('{"x":1}'))
    assert out2.get("skipped") == "no_new_turns"


def test_run_no_upsert_no_extraction(tmp_path):
    p = _write_session(tmp_path, "s1", [{"role": "user", "content": "just chatting"}])
    out = run_extract_for_session(tmp_path, p, llm_invoke=_stub('{"x":1}'))
    assert out["extracted"] == []        # nothing authored -> nothing extracted
    assert out["cursor"] == 1


def test_run_extract_calls_learnings_when_enabled(tmp_path, monkeypatch):
    calls = {}
    import durin.memory.extract_runner as er
    def _mine(ws, text, **k):
        calls["text"] = text
        return [{"ref": "feedback:x", "committed": True}]
    monkeypatch.setattr(er, "mine_learnings", _mine)
    monkeypatch.setattr(er, "extract_entity", lambda *a, **k: type("R", (), {"committed": False})())
    monkeypatch.setattr(er, "discover_entities", lambda *a, **k: [])
    monkeypatch.setattr(er, "entity_refs_in_messages", lambda m: [])
    monkeypatch.setattr(er, "load_session", lambda p: ({}, [{"role": "user", "content": "hola"}]))
    monkeypatch.setattr(er, "get_extract_cursor", lambda p: 0)
    monkeypatch.setattr(er, "set_extract_cursor", lambda p, n: None)
    r = er.run_extract_for_session(tmp_path, tmp_path / "s.jsonl",
                                   skill_signals=False, discover=False, learnings=True)
    assert "[turn-1] [?] USER: hola" in calls["text"]
    assert r["learnings"] == [{"ref": "feedback:x", "committed": True}]
    calls.clear()
    er.run_extract_for_session(tmp_path, tmp_path / "s.jsonl",
                               skill_signals=False, discover=False, learnings=False)
    assert "text" not in calls   # disabled -> not called


def test_extract_text_numbers_turns(tmp_path, monkeypatch):
    # Build a 2-turn session at cursor 0; capture the text passed to discover.
    captured = {}
    import durin.memory.extract_runner as er
    monkeypatch.setattr(er, "discover_entities",
                        lambda ws, text, **k: captured.setdefault("text", text) or [])
    monkeypatch.setattr(er, "extract_entity", lambda *a, **k: None)
    monkeypatch.setattr(er, "entity_refs_in_messages", lambda m: [])
    monkeypatch.setattr(er, "load_session",
                        lambda p: ({}, [{"role": "user", "content": "hi"},
                                        {"role": "assistant", "content": "yo"}]))
    monkeypatch.setattr(er, "get_extract_cursor", lambda p: 0)
    monkeypatch.setattr(er, "set_extract_cursor", lambda p, n: None)
    er.run_extract_for_session(tmp_path, tmp_path / "s.jsonl",
                               skill_signals=False, discover=True)
    assert "[turn-1] [?] USER: hi" in captured["text"]
    assert "[turn-2] [?] ASSISTANT: yo" in captured["text"]


def test_cursor_past_the_end_restarts_the_conversation(tmp_path):
    """A cursor past the end of the file means the file was rewritten.

    `/new` empties the session file and the file cap trims it; neither resets
    the cursor. The turns in the file now are a new conversation, so the
    extract pass must process them instead of skipping until the file
    outgrows the stale index.
    """
    p = _write_session(tmp_path, "s1", [
        {"role": "user", "content": "fresh question"},
        {"role": "assistant", "content": "fresh answer"},
        {"role": "user", "content": "second question"},
        {"role": "assistant", "content": "second answer"},
    ])
    set_extract_cursor(p, 10)

    out = run_extract_for_session(tmp_path, p, llm_invoke=_stub("[]"))

    assert out.get("skipped") != "no_new_turns"
    assert out["new_turns"] == 4
    assert out["cursor"] == 4
    assert get_extract_cursor(p) == 4


def test_skill_loads_are_numbered_like_the_turns_the_pass_reads(tmp_path):
    """After a cursor, the pass reads the rest of the session with whole-session
    turn numbers. A skill load in that rest must carry the same numbering, or
    "loaded at turn N" points at a different line than ``[turn-N]``."""
    ws = tmp_path / "ws"
    view = {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "v1", "type": "function", "function": {
            "name": "skill_view", "arguments": json.dumps({"name": "deploy-flow"})}}],
    }
    p = _write_session(ws, "s1", [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "deploy it"},
        view,
        {"role": "tool", "tool_call_id": "v1", "content": "# deploy-flow"},
        {"role": "assistant", "content": "deployed"},
        {"role": "user", "content": "no, staging first"},
    ])
    set_extract_cursor(p, 2)
    prompts: list[str] = []

    def llm(prompt, **kw):
        prompts.append(prompt)
        return "[]"

    run_extract_for_session(ws, p, llm_invoke=llm, discover=False, learnings=False,
                            skill_signals=True)

    signal_prompt = next(pr for pr in prompts if "SKILLS LOADED" in pr)
    # The view is the session's 4th message; the text labels it the same.
    assert "deploy-flow@4" in signal_prompt
    assert "[turn-3]" in signal_prompt and "[turn-4]" not in signal_prompt.split("TURNS:")[1]
