import json
from datetime import datetime, timezone
from pathlib import Path

from durin.memory.entities import SUGGESTED_TYPES_ORDERED
from durin.memory.entity_page import EntityPage
from durin.memory.extract_dream import (
    build_discover_prompt,
    discover_entities,
    extract_entity,
    mine_learnings,
    parse_attributes,
    parse_discoveries,
)
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


def test_parse_attributes_strips_fences_and_filters():
    raw = '```json\n{"hq": "SF", "products": ["a","b"], "bio": {"x": 1}}\n```'
    assert parse_attributes(raw) == {"hq": "SF", "products": ["a", "b"]}  # nested dropped


def test_extract_applies_attributes_as_dream(tmp_path):
    # agent authored the entity first (body + name), via the writer
    write_entity(tmp_path, "company:mxhero",
                 [FieldPatch(kind="body_append", value="mxHERO is a US company.",
                             author="agent", source_ref="s", at=NOW)],
                 create=True, name="mxHERO Inc.")
    r = extract_entity(
        tmp_path, "company:mxhero",
        "USER: mxHERO is headquartered in the US and makes mail2cloud.",
        llm_invoke=_stub('{"hq_country":"US","products":["mail2cloud"]}'))
    assert r.committed
    page = EntityPage.from_file(_page_path(tmp_path, "company:mxhero"))
    assert page.attributes["hq_country"] == "US"
    assert page.attributes["products"] == ["mail2cloud"]
    # field author is "dream"; the agent's body + name are untouched
    assert page.provenance["attributes"]["hq_country"]["author"] == "dream"
    assert page.name == "mxHERO Inc."
    assert "mxHERO is a US company." in page.body


def test_extract_does_not_overwrite_user_attribute(tmp_path):
    write_entity(tmp_path, "company:x",
                 [FieldPatch(kind="attribute", key="hq", value="Boston",
                             author="user", source_ref="u", at=NOW)], create=True)
    extract_entity(tmp_path, "company:x", "turns", llm_invoke=_stub('{"hq":"SF"}'))
    page = EntityPage.from_file(_page_path(tmp_path, "company:x"))
    assert page.attributes["hq"] == "Boston"   # user > dream


def test_extract_idempotent_no_duplicate(tmp_path):
    write_entity(tmp_path, "company:x",
                 [FieldPatch(kind="body_append", value="x", author="agent",
                             source_ref="s", at=NOW)], create=True)
    stub = _stub('{"hq":"SF"}')
    extract_entity(tmp_path, "company:x", "t", llm_invoke=stub)
    extract_entity(tmp_path, "company:x", "t", llm_invoke=stub)
    page = EntityPage.from_file(_page_path(tmp_path, "company:x"))
    assert page.attributes["hq"] == "SF"
    assert list(page.attributes.keys()) == ["hq"]   # no duplicate key


def test_extract_empty_output_is_noop(tmp_path):
    write_entity(tmp_path, "company:x",
                 [FieldPatch(kind="body_append", value="x", author="agent",
                             source_ref="s", at=NOW)], create=True)
    r = extract_entity(tmp_path, "company:x", "t", llm_invoke=_stub("no json here"))
    assert not r.committed


def test_parse_discoveries_captures_rich_fields():
    raw = '''[
      {"ref": "place:torrent", "name": "Torrent",
       "aliases": ["Torrente", ""], "turn": 16,
       "relations": [{"to": "place:valencia", "type": "located_in"},
                     {"to": "", "type": "bad"}],
       "significance": "A place the user tracks the weather for.",
       "attributes": {"region": "Comunidad Valenciana"}}
    ]'''
    [p] = parse_discoveries(raw)
    assert p["ref"] == "place:torrent"
    assert p["aliases"] == ["Torrente"]                 # empty alias dropped
    assert p["relations"] == [{"to": "place:valencia", "type": "located_in"}]  # malformed dropped
    assert p["significance"] == "A place the user tracks the weather for."
    assert p["turn"] == 16
    assert p["attributes"] == {"region": "Comunidad Valenciana"}


def test_parse_discoveries_rich_fields_optional():
    raw = '[{"ref": "topic:x", "name": "X", "attributes": {"a": 1}}]'
    [p] = parse_discoveries(raw)
    assert p["aliases"] == [] and p["relations"] == []
    assert p["significance"] is None and p["turn"] is None


class _Resp:
    def __init__(self, text): self.text = text


def _page(ws, ref):
    t, _, s = ref.partition(":")
    return EntityPage.from_file(Path(ws) / "memory" / "entities" / t / f"{s}.md")


# ---------------------------------------------------------------------------
# Task 3: per-entity provenance precision
# ---------------------------------------------------------------------------


def test_discover_provenance_uses_fact_turn(tmp_path):
    proposals = json.dumps([{
        "ref": "place:torrent", "name": "Torrent", "turn": 12,
        "attributes": {"region": "Comunidad Valenciana"},
    }])
    discover_entities(tmp_path, "[turn-12] USER: torrent is in valencia",
                      existing_refs=[],
                      llm_invoke=lambda *a, **k: _Resp(proposals), model="m",
                      source_ref="[[sessions/abc.md#turn-40]]")  # window-end fallback
    page = EntityPage.from_file(tmp_path / "memory" / "entities" / "place" / "torrent.md")
    sr = page.provenance["attributes"]["region"]["source_ref"]
    assert sr == "[[sessions/abc.md#turn-12]]"   # fact turn, NOT the turn-40 watermark


def test_discover_provenance_falls_back_when_no_turn(tmp_path):
    proposals = json.dumps([{
        "ref": "place:valencia", "name": "Valencia",
        "attributes": {"country": "Spain"},
    }])
    discover_entities(tmp_path, "[turn-5] USER: valencia is in spain",
                      existing_refs=[],
                      llm_invoke=lambda *a, **k: _Resp(proposals), model="m",
                      source_ref="[[sessions/abc.md#turn-40]]")
    page = EntityPage.from_file(tmp_path / "memory" / "entities" / "place" / "valencia.md")
    sr = page.provenance["attributes"]["country"]["source_ref"]
    assert sr == "[[sessions/abc.md#turn-40]]"   # fallback to window-end


def test_parse_discoveries_accepts_integer_float_turn():
    raw = '[{"ref": "topic:x", "name": "X", "turn": 16.0, "attributes": {"a": 1}}]'
    [p] = parse_discoveries(raw)
    assert p["turn"] == 16                         # integer-valued float accepted


def test_parse_discoveries_rejects_fractional_float_turn():
    raw = '[{"ref": "topic:x", "name": "X", "turn": 16.5, "attributes": {"a": 1}}]'
    [p] = parse_discoveries(raw)
    assert p["turn"] is None                       # fractional float rejected


def test_parse_discoveries_rejects_bool_turn():
    raw = '[{"ref": "topic:x", "name": "X", "turn": true, "attributes": {"a": 1}}]'
    [p] = parse_discoveries(raw)
    assert p["turn"] is None                       # bool rejected (True is an int subclass)


def test_discover_writes_aliases_relations_significance(tmp_path):
    proposals = json.dumps([{
        "ref": "place:torrent", "name": "Torrent",
        "aliases": ["Torrente"],
        "relations": [{"to": "place:valencia", "type": "located_in"}],
        "significance": "A place the user tracks the weather for.",
        "turn": 5,
        "attributes": {"region": "Comunidad Valenciana"},
    }])
    discover_entities(tmp_path, "USER: torrent stuff", existing_refs=[],
                      llm_invoke=lambda *a, **k: _Resp(proposals), model="m")
    page = _page(tmp_path, "place:torrent")
    assert page is not None
    assert "Torrente" in page.aliases
    assert page.attributes.get("region") == "Comunidad Valenciana"
    assert "weather" in (page.body or "")
    assert any(r.get("to") == "place:valencia" for r in page.relations)
    # significance is idempotent across a re-run (body_replace, not append)
    discover_entities(tmp_path, "USER: torrent stuff", existing_refs=[],
                      llm_invoke=lambda *a, **k: _Resp(proposals), model="m")
    assert (_page(tmp_path, "place:torrent").body or "").count("weather") == 1


# ---------------------------------------------------------------------------
# mine_learnings: extract + write feedback/stance/practice entities
# ---------------------------------------------------------------------------


class _LResp:
    def __init__(self, text): self.text = text


def test_mine_learnings_writes_feedback_and_skips_principal(tmp_path):
    out = json.dumps([
        {"ref": "feedback:spanish", "name": "Reply in Spanish",
         "body": "User prefers Spanish. Why: works in Spanish. How: converse in Spanish."},
        {"ref": "person:marcelo", "name": "Marcelo",
         "body": "the user"},   # must be SKIPPED — never write the principal
    ])
    res = mine_learnings(tmp_path, "[turn-1] USER: contestame en español",
                         llm_invoke=lambda *a, **k: _LResp(out), model="m")
    fb = EntityPage.from_file(tmp_path / "memory/entities/feedback/spanish.md")
    assert fb is not None and "Spanish" in (fb.body or "")
    assert not (tmp_path / "memory/entities/person/marcelo.md").exists()
    assert [r["ref"] for r in res] == ["feedback:spanish"]


def test_mine_learnings_empty_is_noop(tmp_path):
    res = mine_learnings(tmp_path, "[turn-1] USER: hi",
                         llm_invoke=lambda *a, **k: _LResp("[]"), model="m")
    assert res == []


def test_mine_learnings_emits_telemetry_event(tmp_path, monkeypatch):
    """mine_learnings must emit memory.dream.learnings with correct counts."""
    out = json.dumps([
        {"ref": "feedback:pref-a", "name": "Pref A", "body": "body a"},
        {"ref": "stance:pref-b", "name": "Pref B", "body": "body b"},
        {"ref": "person:bad", "name": "Bad", "body": "filtered out"},
    ])
    captured: list[tuple[str, dict]] = []
    import durin.memory.extract_dream as _ed
    monkeypatch.setattr(_ed, "_mine_emit_tool_event",
                        lambda name, payload: captured.append((name, payload)))

    mine_learnings(tmp_path, "some text", llm_invoke=lambda *a, **k: _LResp(out), model="m")

    assert len(captured) == 1
    event_name, payload = captured[0]
    assert event_name == "memory.dream.learnings"
    # 2 accepted (feedback+stance), 1 filtered (person)
    assert payload["proposed"] == 3
    assert payload["written"] == 2
    assert set(payload["refs"]) == {"feedback:pref-a", "stance:pref-b"}


# ---------------------------------------------------------------------------
# discover prompt wiring: SUGGESTED_TYPES_ORDERED is the single source
# ---------------------------------------------------------------------------


def test_discover_prompt_type_list_is_wired_from_suggested_types_ordered() -> None:
    """The discover prompt's type list must equal '/'.join(SUGGESTED_TYPES_ORDERED).

    This guards against re-hardcoding a divergent list: if SUGGESTED_TYPES_ORDERED
    changes, the prompt changes with it automatically. The exact substring check
    proves zero behavior change from the wiring refactor.
    """
    canonical = "/".join(SUGGESTED_TYPES_ORDERED)
    # Exact substring: behavior is unchanged from before the wiring refactor
    assert canonical == "person/place/project/topic/organization/event/artifact/stance/practice"
    prompt = build_discover_prompt("dummy turns")
    assert canonical in prompt


def test_parse_attributes_none_on_unparseable():
    assert parse_attributes("I could not produce JSON, sorry.") is None


def test_parse_attributes_none_on_wrong_top_level():
    assert parse_attributes("[1, 2, 3]") is None


def test_parse_attributes_empty_dict_is_empty_not_none():
    assert parse_attributes("{}") == {}


def test_parse_discoveries_none_on_unparseable():
    assert parse_discoveries("no json here") is None


def test_parse_discoveries_empty_list_is_empty_not_none():
    assert parse_discoveries("[]") == []


def test_parse_learnings_none_on_unparseable():
    from durin.memory.extract_dream import _parse_learnings
    assert _parse_learnings("plain prose answer") is None


def test_parse_learnings_empty_list_is_empty_not_none():
    from durin.memory.extract_dream import _parse_learnings
    assert _parse_learnings("[]") == []
