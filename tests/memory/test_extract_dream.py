import json
from datetime import datetime, timezone
from pathlib import Path

from durin.memory.entity_page import EntityPage
from durin.memory.extract_dream import discover_entities, extract_entity, parse_attributes, parse_discoveries
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
