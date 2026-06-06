from datetime import datetime, timezone

from durin.memory.entity_page import EntityPage
from durin.memory.extract_dream import extract_entity, parse_attributes
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
