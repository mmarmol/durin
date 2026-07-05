from datetime import datetime, timezone

from durin.memory.entity_page import EntityPage
from durin.memory.field_patch import FieldPatch, apply_field_patch

NOW = datetime(2026, 6, 5, tzinfo=timezone.utc)
LATER = datetime(2026, 6, 6, tzinfo=timezone.utc)


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


def test_body_append_records_body_provenance():
    # body_append now stamps prov["body"] so body_replace has authority to
    # arbitrate against (no precedence was tracked for the body before).
    p = _page()
    apply_field_patch(p, FieldPatch(kind="body_append", value="First fact.",
                                    author="agent", source_ref="s#t1", at=NOW))
    assert p.provenance["body"]["author"] == "agent"
    assert p.provenance["body"]["source_ref"] == "s#t1"


def test_body_replace_on_empty_body_sets_it():
    # No prior body / no prov → incoming wins (mirrors attribute semantics).
    p = _page()
    changed = apply_field_patch(p, FieldPatch(kind="body_replace", value="Canonical prose.",
                                              author="agent", source_ref="s#t1", at=NOW))
    assert changed is True
    assert p.body == "Canonical prose."
    assert p.provenance["body"]["author"] == "agent"


def test_body_replace_overwrites_agents_own_prior_body_cleanly():
    # The common case: the agent re-authors prose it wrote earlier. Replace
    # drops the old body entirely and carries no append marker.
    p = _page()
    apply_field_patch(p, FieldPatch(kind="body_append", value="Old, partly wrong prose.",
                                    author="agent", source_ref="s#t1", at=NOW))
    changed = apply_field_patch(p, FieldPatch(kind="body_replace", value="New corrected prose.",
                                              author="agent", source_ref="s#t2", at=LATER))
    assert changed is True
    assert p.body == "New corrected prose."
    assert "Old, partly wrong prose." not in p.body
    assert "<!--" not in p.body  # clean canonical prose, no provenance marker


def test_agent_body_replace_cannot_clobber_user_body_falls_back_to_append():
    # Precedence: an agent replace must not wipe a higher-authority (user) body.
    # Instead of dropping the agent's new prose, it degrades to a lossless append.
    p = _page()
    apply_field_patch(p, FieldPatch(kind="body_append", value="User's own careful notes.",
                                    author="user", source_ref="manual", at=NOW))
    changed = apply_field_patch(p, FieldPatch(kind="body_replace", value="Agent rewrite attempt.",
                                              author="agent", source_ref="s#t9", at=LATER))
    assert changed is True
    assert "User's own careful notes." in p.body      # not clobbered
    assert "Agent rewrite attempt." in p.body         # appended, not lost
    assert p.provenance["body"]["author"] == "user"   # authority not downgraded


def test_body_append_does_not_downgrade_body_authority():
    # A later agent append over a user-authored body must not lower the
    # recorded authority from user to agent (else a replace could then win).
    p = _page()
    apply_field_patch(p, FieldPatch(kind="body_append", value="User note.",
                                    author="user", source_ref="manual", at=NOW))
    apply_field_patch(p, FieldPatch(kind="body_append", value="Agent addendum.",
                                    author="agent", source_ref="s#t1", at=LATER))
    assert p.provenance["body"]["author"] == "user"


def test_apply_requires_resolved_author():
    import pytest
    p = _page()
    with pytest.raises(ValueError):
        apply_field_patch(p, FieldPatch(kind="attribute", key="hq", value="SF",
                                        author=None, source_ref="a", at=NOW))


# --- relation-type normalization (write-time prevention) ---------------------

from durin.memory.field_patch import normalize_relation_type


def test_normalize_relation_type_collapses_surface_variants():
    assert normalize_relation_type("occurs-in") == "occurs_in"
    assert normalize_relation_type("Occurs In") == "occurs_in"
    assert normalize_relation_type("occurs_in") == "occurs_in"
    assert normalize_relation_type("  Diagnosed--By ") == "diagnosed_by"
    # inverses are NOT touched (different direction/meaning)
    assert normalize_relation_type("treats") != normalize_relation_type("treated_by")
    assert normalize_relation_type("") == ""


def test_relation_patch_normalizes_type_and_dedups():
    p = _page()
    apply_field_patch(p, FieldPatch(kind="relation", value={"to": "x:y", "type": "occurs-in"},
                                    author="agent", source_ref="a", at=NOW))
    # a second write with a surface variant of the same edge is a no-op (deduped)
    changed = apply_field_patch(p, FieldPatch(kind="relation", value={"to": "x:y", "type": "Occurs_In"},
                                              author="agent", source_ref="b", at=NOW))
    assert changed is False
    assert len(p.relations) == 1
    assert p.relations[0]["type"] == "occurs_in"          # stored canonical
    assert "x:y\x1foccurs_in" in p.provenance["relations"]  # provenance keyed on canonical
