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


def test_relation_provenance_keyed_by_to_type():
    # Q1: relation provenance is a {(to,type)-key: entry} dict carrying to/type.
    p = _page()
    apply_field_patch(p, FieldPatch(kind="relation",
                                    value=dict(to="company:carahsoft", type="partner"),
                                    author="agent", source_ref="s#t1", at=NOW))
    rel_prov = p.provenance["relations"]
    assert isinstance(rel_prov, dict)
    (entry,) = rel_prov.values()
    assert entry["to"] == "company:carahsoft"
    assert entry["type"] == "partner"
    assert entry["author"] == "agent"
    assert "index" not in entry


def test_relation_provenance_lenient_migrates_legacy_index_list():
    # A page persisted with the legacy index-keyed list is migrated to the
    # (to,type)-keyed dict on the next relation write, preserving the old entry.
    p = _page()
    p.relations = [dict(to="topic:a", type="about")]
    p.provenance = {"relations": [
        {"index": 0, "source_ref": "old", "extracted_at": "2026-01-01T00:00:00+00:00",
         "author": "agent"},
    ]}
    apply_field_patch(p, FieldPatch(kind="relation",
                                    value=dict(to="topic:b", type="about"),
                                    author="agent", source_ref="s#t2", at=NOW))
    rel_prov = p.provenance["relations"]
    assert isinstance(rel_prov, dict)
    # both the migrated legacy entry and the new one are present, keyed by ref+type
    tos = {e["to"] for e in rel_prov.values()}
    assert tos == {"topic:a", "topic:b"}
    migrated = next(e for e in rel_prov.values() if e["to"] == "topic:a")
    assert migrated["source_ref"] == "old"
    assert "index" not in migrated


def test_derived_from_add_dedup_and_ref_keyed_provenance():
    p = _page()
    ref = "reference:rabies-investigation"
    assert apply_field_patch(p, FieldPatch(kind="derived_from", value=ref,
                                           author="agent", source_ref="sessions/x.md#turn-8", at=NOW)) is True
    # dedup: same ref by same-rank author with no newer time → no change
    assert apply_field_patch(p, FieldPatch(kind="derived_from", value=ref,
                                           author="agent", source_ref="sessions/x.md#turn-8", at=NOW)) is False
    assert p.derived_from == [ref]
    # provenance keyed by the ref string (merge-safe)
    assert p.provenance["derived_from"][ref]["source_ref"] == "sessions/x.md#turn-8"
    assert p.provenance["derived_from"][ref]["author"] == "agent"


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
