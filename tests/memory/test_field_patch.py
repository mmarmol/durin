from datetime import datetime, timezone

from durin.memory.entity_page import EntityPage
from durin.memory.field_patch import FieldPatch, apply_field_patch

NOW = datetime(2026, 6, 5, tzinfo=timezone.utc)


def _page():
    return EntityPage(type="company", name="mxHERO")


def test_agent_sets_attribute_records_provenance():
    p = _page()
    changed = apply_field_patch(p, FieldPatch(
        kind="attribute", key="hq", value="SF",
        author="agent", source_ref="[[sessions/s#turn-1]]", at=NOW))
    assert changed is True
    assert p.attributes["hq"] == "SF"
    assert p.provenance["attributes"]["hq"]["author"] == "agent"
    assert p.provenance["attributes"]["hq"]["source_ref"] == "[[sessions/s#turn-1]]"


def test_dream_overwrites_agent_same_field():
    p = _page()
    apply_field_patch(p, FieldPatch(kind="attribute", key="hq", value="SF",
                                    author="agent", source_ref="a", at=NOW))
    changed = apply_field_patch(p, FieldPatch(kind="attribute", key="hq", value="Boston",
                                              author="dream", source_ref="b", at=NOW))
    assert changed is True
    assert p.attributes["hq"] == "Boston"            # dream > agent
    assert p.provenance["attributes"]["hq"]["author"] == "dream"


def test_agent_cannot_overwrite_user_field():
    p = _page()
    apply_field_patch(p, FieldPatch(kind="attribute", key="hq", value="SF",
                                    author="user", source_ref="u", at=NOW))
    changed = apply_field_patch(p, FieldPatch(kind="attribute", key="hq", value="LA",
                                              author="agent", source_ref="a", at=NOW))
    assert changed is False                          # user > agent → no overwrite
    assert p.attributes["hq"] == "SF"


def test_relation_add_dedup_by_to_type():
    p = _page()
    rel = dict(to="company:carahsoft", type="partner")
    apply_field_patch(p, FieldPatch(kind="relation", value=rel,
                                    author="agent", source_ref="a", at=NOW))
    apply_field_patch(p, FieldPatch(kind="relation", value=dict(rel),
                                    author="agent", source_ref="a2", at=NOW))
    assert len([r for r in p.relations if r["to"] == "company:carahsoft"]) == 1


def test_alias_dedup_and_body_append():
    p = _page()
    assert apply_field_patch(p, FieldPatch(kind="alias", value="mxHERO Inc.",
                                           author="agent", source_ref="a", at=NOW)) is True
    assert apply_field_patch(p, FieldPatch(kind="alias", value="mxHERO Inc.",
                                           author="agent", source_ref="a", at=NOW)) is False
    apply_field_patch(p, FieldPatch(kind="body_append", value="Founded by Alex.",
                                    author="agent", source_ref="a", at=NOW))
    assert "Founded by Alex." in p.body


def test_apply_requires_resolved_author():
    import pytest
    p = _page()
    with pytest.raises(ValueError):
        apply_field_patch(p, FieldPatch(kind="attribute", key="hq", value="SF",
                                        author=None, source_ref="a", at=NOW))
