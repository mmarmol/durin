import json
from pathlib import Path

from durin.memory.derived_from_dream import (
    entities_missing_derived_from,
    link_derived_from_for_session,
    parse_links,
    reference_refs_in_session,
)
from durin.memory.entity_page import EntityPage
from durin.memory.llm_invoke import LLMResponse
from durin.memory.reference import ingest_reference


def _session(tmp_path: Path, messages: list[dict]) -> Path:
    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    p = sessions / "s1.jsonl"
    lines = [json.dumps({"_type": "metadata"})] + [json.dumps(m) for m in messages]
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def _upsert_call(ref: str) -> dict:
    return {
        "role": "assistant",
        "tool_calls": [{
            "function": {"name": "memory_upsert_entity",
                         "arguments": json.dumps({"ref": ref})},
        }],
    }


def test_reference_refs_in_session_confirms_on_disk(tmp_path: Path) -> None:
    ingest_reference(tmp_path, "Rabies Investigation", "notes")
    msgs = [
        {"role": "tool", "content": json.dumps(
            {"id": "x", "reference": "reference:rabies-investigation"})},
        {"role": "assistant", "content": "I also saw reference:ghost-doc somewhere"},
    ]
    refs = reference_refs_in_session(tmp_path, msgs)
    # the real one is kept; the non-existent one is dropped
    assert refs == ["reference:rabies-investigation"]


def test_entities_missing_derived_from_filters_linked(tmp_path: Path) -> None:
    EntityPage(type="topic", name="Linked",
               derived_from=["reference:doc"]).save(
        tmp_path / "memory" / "entities" / "topic" / "linked.md")
    EntityPage(type="topic", name="Unlinked").save(
        tmp_path / "memory" / "entities" / "topic" / "unlinked.md")
    pages = entities_missing_derived_from(
        tmp_path, ["topic:linked", "topic:unlinked", "topic:ghost"])
    assert [ref for ref, _page in pages] == ["topic:unlinked"]


def test_uses_on_disk_ref_not_name_derived_slug(tmp_path: Path) -> None:
    # Regression: a page whose name slugifies differently from its filename
    # (e.g. a renamed page) must be addressed by its on-disk ref, not
    # EntityPage.entity_ref (name-derived) — else the write misses the file.
    EntityPage(type="topic", name="Reacción Adversa Vacuna Rabia en Caninos").save(
        tmp_path / "memory" / "entities" / "topic" / "reaccion-adversa-canino.md")
    pages = entities_missing_derived_from(tmp_path, ["topic:reaccion-adversa-canino"])
    assert len(pages) == 1
    ref, page = pages[0]
    assert ref == "topic:reaccion-adversa-canino"          # the filename slug
    assert page.entity_ref != ref                          # name-derived diverges


def test_parse_links_drops_unknown_refs_and_entities() -> None:
    raw = json.dumps({
        "topic:rabies": ["reference:doc-a", "reference:made-up"],
        "topic:not-asked": ["reference:doc-a"],
    })
    links = parse_links(
        raw,
        valid_refs={"reference:doc-a"},
        valid_entities={"topic:rabies"},
    )
    assert links == {"topic:rabies": ["reference:doc-a"]}


def test_link_derived_from_for_session_writes_patch(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from durin.memory.field_patch import FieldPatch
    from durin.memory.memory_writer import write_entity

    ingest_reference(tmp_path, "Rabies Investigation", "viral disease notes")
    ref = "reference:rabies-investigation"
    # Author the page through write_entity so it is committed at HEAD — exactly
    # how memory_upsert_entity leaves it in production (the dream reads HEAD).
    write_entity(
        tmp_path, "topic:rabies",
        [FieldPatch(kind="body_append",
                    value="A viral disease distilled from the investigation.",
                    author="agent", source_ref="setup",
                    at=datetime(2026, 6, 1, tzinfo=timezone.utc))],
        create=True, name="Rabies",
    )
    msgs = [
        {"role": "tool", "content": json.dumps({"id": "x", "reference": ref})},
        _upsert_call("topic:rabies"),
    ]
    jsonl = _session(tmp_path, msgs)

    calls: list[str] = []

    def fake_llm(prompt: str, **_kw):
        calls.append(prompt)
        return json.dumps({"topic:rabies": [ref]})

    out = link_derived_from_for_session(tmp_path, jsonl, llm_invoke=fake_llm)
    assert out["linked"] == [
        {"ref": "topic:rabies", "derived_from": [ref], "committed": True},
    ]
    page = EntityPage.from_file(
        tmp_path / "memory" / "entities" / "topic" / "rabies.md")
    assert page.derived_from == [ref]

    # Idempotent: a second run skips (already linked) WITHOUT an LLM call.
    calls.clear()
    out2 = link_derived_from_for_session(tmp_path, jsonl, llm_invoke=fake_llm)
    assert out2["skipped"] == "all_linked"
    assert calls == []


def test_link_skips_when_no_references(tmp_path: Path) -> None:
    EntityPage(type="topic", name="Rabies").save(
        tmp_path / "memory" / "entities" / "topic" / "rabies.md")
    jsonl = _session(tmp_path, [_upsert_call("topic:rabies")])

    def fake_llm(prompt: str, **_kw):  # pragma: no cover — must not be called
        raise AssertionError("LLM should not be called when no references")

    out = link_derived_from_for_session(tmp_path, jsonl, llm_invoke=fake_llm)
    assert out["skipped"] == "no_references"


def test_parse_links_none_on_unparseable():
    assert parse_links("not json", valid_refs=set(), valid_entities=set()) is None


def test_parse_links_empty_map_is_empty_not_none():
    assert parse_links("{}", valid_refs=set(), valid_entities=set()) == {}


def test_link_derived_from_emits_parse_failure(tmp_path: Path, monkeypatch) -> None:
    from datetime import datetime, timezone

    import durin.agent.tools._telemetry as tel
    from durin.memory.field_patch import FieldPatch
    from durin.memory.memory_writer import write_entity

    ingest_reference(tmp_path, "Rabies Investigation", "viral disease notes")
    ref = "reference:rabies-investigation"
    write_entity(
        tmp_path, "topic:rabies",
        [FieldPatch(kind="body_append",
                    value="A viral disease distilled from the investigation.",
                    author="agent", source_ref="setup",
                    at=datetime(2026, 6, 1, tzinfo=timezone.utc))],
        create=True, name="Rabies",
    )
    msgs = [
        {"role": "tool", "content": json.dumps({"id": "x", "reference": ref})},
        _upsert_call("topic:rabies"),
    ]
    jsonl = _session(tmp_path, msgs)

    events = []
    monkeypatch.setattr(tel, "emit_tool_event",
                        lambda name, data: events.append((name, data)))

    def fake_llm(prompt: str, **_kw):
        return LLMResponse(text="not json at all", finish_reason="length")

    link_derived_from_for_session(tmp_path, jsonl, llm_invoke=fake_llm)
    failures = [d for n, d in events if n == "memory.dream.parse_failure"]
    assert failures and failures[0]["stage"] == "derived_from"
    # A cut answer reads apart from a malformed one.
    assert failures[0]["finish_reason"] == "length"


def _unlinked_session(tmp_path: Path) -> Path:
    """A session that ingested a document and authored a page the model then
    finds unrelated to it: the question has a definite empty answer."""
    from datetime import datetime, timezone

    from durin.memory.field_patch import FieldPatch
    from durin.memory.memory_writer import write_entity

    ingest_reference(tmp_path, "Rabies Investigation", "viral disease notes")
    write_entity(
        tmp_path, "topic:billing",
        [FieldPatch(kind="body_append", value="Invoices and payment terms.",
                    author="agent", source_ref="setup",
                    at=datetime(2026, 6, 1, tzinfo=timezone.utc))],
        create=True, name="Billing",
    )
    msgs = [
        {"role": "tool", "content": json.dumps({"id": "x", "reference": "reference:rabies-investigation"})},
        _upsert_call("topic:billing"),
    ]
    return _session(tmp_path, msgs)


def test_an_unchanged_question_answered_with_no_links_is_not_asked_again(tmp_path: Path) -> None:
    """The pass runs nightly over every session. A session whose authored
    pages the model found unrelated to its documents kept its pages unlinked,
    so the same prompt went out again every night (15 identical calls a night
    on one install). The answer is remembered for exactly that question."""
    from durin.memory.dream_passes import run_derived_from_pass

    jsonl = _unlinked_session(tmp_path)
    calls: list[str] = []

    def fake_llm(prompt: str, **_kw):
        calls.append(prompt)
        return "{}"

    run_derived_from_pass(tmp_path, llm_invoke=fake_llm)
    run_derived_from_pass(tmp_path, llm_invoke=fake_llm)
    assert len(calls) == 1

    # The session moves on: a new question, asked once.
    with jsonl.open("a", encoding="utf-8") as f:
        f.write("\n" + json.dumps({"role": "user", "content": "billing is unrelated to that doc"}))
    run_derived_from_pass(tmp_path, llm_invoke=fake_llm)
    run_derived_from_pass(tmp_path, llm_invoke=fake_llm)
    assert len(calls) == 2


def test_an_unreadable_answer_is_asked_again(tmp_path: Path) -> None:
    from durin.memory.dream_passes import run_derived_from_pass

    _unlinked_session(tmp_path)
    calls: list[str] = []

    def fake_llm(prompt: str, **_kw):
        calls.append(prompt)
        return "not json at all"

    run_derived_from_pass(tmp_path, llm_invoke=fake_llm)
    run_derived_from_pass(tmp_path, llm_invoke=fake_llm)
    assert len(calls) == 2
