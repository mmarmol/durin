"""EntityPage v2 schema: `attributes`, `relations`, `provenance`.

- v2 extends v1 — v1 pages parse with `attributes={}` and `relations=[]`.
- `attributes`: dict[str, Any]. Free-form keys.
- `relations`: list[dict[str, Any]]. Each item must have `to` matching
  `<type>:<slug>` and `type` as non-empty string (write-side check).
- `provenance`: dict for traceability — preserved verbatim, no schema
  enforcement at read time.
- Round-trip must preserve all v2 fields.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from durin.memory.entity_page import EntityPage, EntityPageError

# ---------------------------------------------------------------------------
# Read path — v2 frontmatter parses into typed fields
# ---------------------------------------------------------------------------


class TestReadV2:
    def test_attributes_parsed_as_dict(self) -> None:
        text = (
            "---\n"
            "type: person\n"
            "name: Marcelo\n"
            "aliases: []\n"
            "attributes:\n"
            "  email: marcelo@mxhero.com\n"
            "  current_residence: Spain\n"
            "---\n\nbody\n"
        )
        page = EntityPage.from_text(text)
        assert page is not None
        assert page.attributes == {
            "email": "marcelo@mxhero.com",
            "current_residence": "Spain",
        }

    def test_relations_parsed_as_list(self) -> None:
        text = (
            "---\n"
            "type: person\n"
            "name: Marcelo\n"
            "aliases: []\n"
            "relations:\n"
            "  - to: person:susana\n"
            "    type: spouse\n"
            "    since: 2010\n"
            "  - to: project:durin\n"
            "    type: maintains\n"
            "    intensity: high\n"
            "---\n\nbody\n"
        )
        page = EntityPage.from_text(text)
        assert page is not None
        assert len(page.relations) == 2
        assert page.relations[0]["to"] == "person:susana"
        assert page.relations[0]["type"] == "spouse"
        assert page.relations[1]["to"] == "project:durin"
        assert page.relations[1]["intensity"] == "high"

    def test_provenance_parsed_as_dict(self) -> None:
        text = (
            "---\n"
            "type: person\n"
            "name: Marcelo\n"
            "aliases: []\n"
            "provenance:\n"
            "  attributes:\n"
            "    email:\n"
            "      source_ref: episodic/2026-05-23.md\n"
            "      extracted_at: 2026-05-23T10:30:00Z\n"
            "---\n\nbody\n"
        )
        page = EntityPage.from_text(text)
        assert page is not None
        assert page.provenance["attributes"]["email"]["source_ref"] == (
            "episodic/2026-05-23.md"
        )

    def test_v1_page_defaults_empty_attributes_relations(self) -> None:
        """A v1 page (no v2 fields) parses with empty defaults — no error."""
        text = (
            "---\n"
            "type: person\n"
            "name: Marcelo\n"
            "aliases: [m]\n"
            "---\n\nbody\n"
        )
        page = EntityPage.from_text(text)
        assert page is not None
        assert page.attributes == {}
        assert page.relations == []
        assert page.provenance == {}

    def test_attributes_not_dict_falls_back_to_empty(self) -> None:
        """If frontmatter has `attributes: [whatever]` (wrong shape), read
        leniently — preserve under a sensible default, don't crash.
        """
        text = (
            "---\n"
            "type: person\n"
            "name: Marcelo\n"
            "aliases: []\n"
            "attributes: not_a_dict\n"
            "---\n\nbody\n"
        )
        page = EntityPage.from_text(text)
        assert page is not None
        assert page.attributes == {}

    def test_relations_not_list_falls_back_to_empty(self) -> None:
        text = (
            "---\n"
            "type: person\n"
            "name: Marcelo\n"
            "aliases: []\n"
            "relations: not_a_list\n"
            "---\n\nbody\n"
        )
        page = EntityPage.from_text(text)
        assert page is not None
        assert page.relations == []

    def test_v2_fields_do_not_leak_into_extra(self) -> None:
        """attributes/relations/provenance are promoted out of extra."""
        text = (
            "---\n"
            "type: person\n"
            "name: Marcelo\n"
            "aliases: []\n"
            "attributes: {email: x}\n"
            "relations: [{to: person:y, type: knows}]\n"
            "provenance: {attributes: {}}\n"
            "identifiers: [um7]\n"  # emergent — should be in extra
            "---\n\nbody\n"
        )
        page = EntityPage.from_text(text)
        assert page is not None
        assert "attributes" not in page.extra
        assert "relations" not in page.extra
        assert "provenance" not in page.extra
        # But unknown emergent fields still land in extra.
        assert page.extra.get("identifiers") == ["um7"]


# ---------------------------------------------------------------------------
# Write path — round-trip preserves v2 fields + ordering
# ---------------------------------------------------------------------------


class TestWriteV2:
    def test_round_trip_preserves_v2_fields(self, tmp_path: Path) -> None:
        page = EntityPage(
            type="person",
            name="Marcelo",
            aliases=["m"],
            attributes={"email": "m@x.com"},
            relations=[{"to": "person:susana", "type": "spouse", "since": 2010}],
            provenance={
                "attributes": {
                    "email": {"source_ref": "episodic/foo.md"},
                },
            },
        )
        path = tmp_path / "marcelo.md"
        page.save(path)
        reloaded = EntityPage.from_file(path)
        assert reloaded is not None
        assert reloaded.attributes == {"email": "m@x.com"}
        assert reloaded.relations == [
            {"to": "person:susana", "type": "spouse", "since": 2010},
        ]
        assert reloaded.provenance["attributes"]["email"]["source_ref"] == (
            "episodic/foo.md"
        )

    def test_empty_v2_fields_omitted_from_output(self, tmp_path: Path) -> None:
        """Don't pollute v1 pages with empty `attributes: {}` blocks."""
        page = EntityPage(type="person", name="Marcelo", aliases=["m"])
        path = tmp_path / "marcelo.md"
        page.save(path)
        text = path.read_text(encoding="utf-8")
        # Spec: v1 pages must remain visually v1 — we don't add empty
        # `attributes:` / `relations:` / `provenance:` keys just because
        # the dataclass has them as defaults.
        assert "\nattributes:" not in text
        assert "\nrelations:" not in text
        assert "\nprovenance:" not in text


# ---------------------------------------------------------------------------
# Write-side validation
# ---------------------------------------------------------------------------


class TestWriteValidation:
    def test_attributes_must_be_dict(self, tmp_path: Path) -> None:
        page = EntityPage(
            type="person", name="Marcelo", aliases=[],
            attributes=["not", "a", "dict"],  # type: ignore[arg-type]
        )
        with pytest.raises(EntityPageError):
            page.to_markdown()

    def test_relations_must_be_list(self, tmp_path: Path) -> None:
        page = EntityPage(
            type="person", name="Marcelo", aliases=[],
            relations={"not": "a list"},  # type: ignore[arg-type]
        )
        with pytest.raises(EntityPageError):
            page.to_markdown()

    def test_relation_missing_to_rejected(self) -> None:
        page = EntityPage(
            type="person", name="Marcelo", aliases=[],
            relations=[{"type": "knows"}],  # no `to`
        )
        with pytest.raises(EntityPageError):
            page.to_markdown()

    def test_relation_to_must_match_entity_ref(self) -> None:
        page = EntityPage(
            type="person", name="Marcelo", aliases=[],
            relations=[{"to": "not-an-entity-ref", "type": "knows"}],
        )
        with pytest.raises(EntityPageError):
            page.to_markdown()

    def test_relation_type_must_be_non_empty_string(self) -> None:
        page = EntityPage(
            type="person", name="Marcelo", aliases=[],
            relations=[{"to": "person:x", "type": ""}],
        )
        with pytest.raises(EntityPageError):
            page.to_markdown()

    def test_well_formed_v2_passes(self, tmp_path: Path) -> None:
        page = EntityPage(
            type="person", name="Marcelo", aliases=[],
            attributes={"email": "m@x.com"},
            relations=[{"to": "person:susana", "type": "spouse"}],
        )
        # Should not raise.
        page.save(tmp_path / "marcelo.md")


class TestDerivedFrom:
    """`derived_from`: list of `reference:<slug>` source documents."""

    def test_round_trip_preserves_derived_from(self, tmp_path: Path) -> None:
        page = EntityPage(
            type="topic", name="Rabies",
            derived_from=["reference:rabies-investigation", "reference:vaccine-adr"],
        )
        path = tmp_path / "rabies.md"
        page.save(path)
        reloaded = EntityPage.from_file(path)
        assert reloaded is not None
        assert reloaded.derived_from == [
            "reference:rabies-investigation",
            "reference:vaccine-adr",
        ]

    def test_v1_page_defaults_empty_derived_from(self) -> None:
        page = EntityPage.from_text(
            "---\ntype: topic\nname: Rabies\naliases: []\n---\nbody\n"
        )
        assert page is not None
        assert page.derived_from == []

    def test_empty_derived_from_omitted_from_output(self, tmp_path: Path) -> None:
        page = EntityPage(type="topic", name="Rabies")
        page.save(tmp_path / "r.md")
        assert "derived_from" not in (tmp_path / "r.md").read_text(encoding="utf-8")

    def test_derived_from_not_list_falls_back_to_empty(self) -> None:
        page = EntityPage.from_text(
            "---\ntype: topic\nname: Rabies\nderived_from: nope\n---\nbody\n"
        )
        assert page is not None
        assert page.derived_from == []

    def test_derived_from_must_be_list(self) -> None:
        page = EntityPage(
            type="topic", name="Rabies",
            derived_from={"not": "a list"},  # type: ignore[arg-type]
        )
        with pytest.raises(EntityPageError):
            page.to_markdown()

    def test_derived_from_entry_must_be_reference_ref(self) -> None:
        # A valid entity ref but not a `reference:` → rejected (this field holds
        # only document refs, not arbitrary entities).
        page = EntityPage(
            type="topic", name="Rabies",
            derived_from=["person:marcelo"],
        )
        with pytest.raises(EntityPageError):
            page.to_markdown()
