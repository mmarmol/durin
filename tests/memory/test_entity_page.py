"""Tests for `durin.memory.entity_page` — parser for entities/<type>/<slug>.md."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from durin.memory.entity_page import EntityPage, EntityPageError


# ---------------------------------------------------------------------------
# from_text — happy path
# ---------------------------------------------------------------------------


class TestFromText:
    def test_minimum_required_fields(self) -> None:
        text = (
            "---\n"
            "type: person\n"
            "name: Marcelo Marmol\n"
            "aliases: [Marcelo, marcelo]\n"
            "---\n"
            "\n"
            "# Marcelo Marmol\n"
        )
        page = EntityPage.from_text(text)
        assert page is not None
        assert page.type == "person"
        assert page.name == "Marcelo Marmol"
        assert page.aliases == ["Marcelo", "marcelo"]
        assert "Marcelo Marmol" in page.body

    def test_full_frontmatter_with_timestamps(self) -> None:
        text = (
            "---\n"
            "type: project\n"
            "name: durin\n"
            "aliases: []\n"
            "created_at: 2026-03-15T12:00:00\n"
            "updated_at: 2026-05-23T18:30:00\n"
            "---\n"
            "\n"
            "project body\n"
        )
        page = EntityPage.from_text(text)
        assert page is not None
        assert page.created_at == datetime(2026, 3, 15, 12, 0, 0)
        assert page.updated_at == datetime(2026, 5, 23, 18, 30, 0)

    def test_emergent_fields_preserved_in_extra(self) -> None:
        """Per doc 18: dream may add fields. They must round-trip."""
        text = (
            "---\n"
            "type: person\n"
            "name: Marcelo\n"
            "aliases: [marcelo]\n"
            "identifiers:\n"
            "  - mmarmol@mxhero.com\n"
            "  - UM7TCSZRN\n"
            "future_emergent_field: arbitrary_value\n"
            "---\n"
            "\n"
            "body\n"
        )
        page = EntityPage.from_text(text)
        assert page is not None
        assert page.extra["identifiers"] == [
            "mmarmol@mxhero.com",
            "UM7TCSZRN",
        ]
        assert page.extra["future_emergent_field"] == "arbitrary_value"


# ---------------------------------------------------------------------------
# from_text — lenient on bad input
# ---------------------------------------------------------------------------


class TestFromTextLenient:
    def test_no_frontmatter_returns_none(self) -> None:
        assert EntityPage.from_text("just markdown body") is None

    def test_malformed_yaml_returns_none(self) -> None:
        text = (
            "---\n"
            "type: person\n"
            "name: [unclosed\n"
            "---\n"
            "body\n"
        )
        assert EntityPage.from_text(text) is None

    def test_missing_required_type_returns_none(self) -> None:
        text = "---\nname: Marcelo\n---\nbody\n"
        assert EntityPage.from_text(text) is None

    def test_missing_required_name_returns_none(self) -> None:
        text = "---\ntype: person\n---\nbody\n"
        assert EntityPage.from_text(text) is None

    def test_aliases_not_a_list_falls_back_to_empty(self) -> None:
        text = (
            "---\n"
            "type: person\n"
            "name: Marcelo\n"
            "aliases: not-a-list\n"
            "---\n"
            "body\n"
        )
        page = EntityPage.from_text(text)
        assert page is not None
        assert page.aliases == []


# ---------------------------------------------------------------------------
# from_file
# ---------------------------------------------------------------------------


class TestFromFile:
    def test_reads_from_disk(self, tmp_path: Path) -> None:
        path = tmp_path / "marcelo.md"
        path.write_text(
            "---\n"
            "type: person\n"
            "name: Marcelo\n"
            "aliases: []\n"
            "---\n"
            "body\n",
            encoding="utf-8",
        )
        page = EntityPage.from_file(path)
        assert page is not None
        assert page.name == "Marcelo"

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            EntityPage.from_file(tmp_path / "nope.md")


# ---------------------------------------------------------------------------
# to_markdown + save
# ---------------------------------------------------------------------------


class TestToMarkdown:
    def test_minimum_serializes(self) -> None:
        page = EntityPage(type="person", name="Marcelo", aliases=["marcelo"])
        out = page.to_markdown()
        assert out.startswith("---\n")
        assert "type: person" in out
        assert "name: Marcelo" in out
        assert "- marcelo" in out  # block-style list

    def test_round_trip_preserves_all_fields(self) -> None:
        original = EntityPage(
            type="person",
            name="Marcelo Marmol",
            aliases=["Marcelo", "marcelo"],
            body="## Current state\n\nbody content here",
            created_at=datetime(2026, 3, 15, 12, 0, 0),
            updated_at=datetime(2026, 5, 23, 18, 30, 0),
            extra={
                "identifiers": ["mmarmol@mxhero.com", "UM7TCSZRN"],
                "custom_field": "value",
            },
        )
        text = original.to_markdown()
        round_trip = EntityPage.from_text(text)
        assert round_trip is not None
        assert round_trip.type == original.type
        assert round_trip.name == original.name
        assert round_trip.aliases == original.aliases
        assert round_trip.body.strip() == original.body.strip()
        assert round_trip.created_at == original.created_at
        assert round_trip.updated_at == original.updated_at
        assert round_trip.extra == original.extra

    def test_emergent_field_survives_round_trip(self) -> None:
        """A field the parser doesn't know about must round-trip."""
        page = EntityPage(
            type="person",
            name="X",
            extra={"a_brand_new_field": [1, 2, 3]},
        )
        text = page.to_markdown()
        parsed = EntityPage.from_text(text)
        assert parsed is not None
        assert parsed.extra["a_brand_new_field"] == [1, 2, 3]

    def test_save_writes_file(self, tmp_path: Path) -> None:
        page = EntityPage(type="topic", name="embeddings", aliases=[])
        path = tmp_path / "entities" / "topic" / "embeddings.md"
        page.save(path)
        assert path.exists()
        assert "embeddings" in path.read_text(encoding="utf-8")

    def test_validate_rejects_bad_type(self) -> None:
        page = EntityPage(type="Person", name="x")  # uppercase
        with pytest.raises(EntityPageError):
            page.to_markdown()

    def test_validate_rejects_empty_name(self) -> None:
        page = EntityPage(type="person", name="   ")
        with pytest.raises(EntityPageError):
            page.to_markdown()


# ---------------------------------------------------------------------------
# Derived methods
# ---------------------------------------------------------------------------


class TestDerived:
    def test_entity_ref(self) -> None:
        page = EntityPage(type="person", name="Marcelo Marmol")
        assert page.entity_ref == "person:marcelo_marmol"

    def test_slug_from_path(self, tmp_path: Path) -> None:
        path = tmp_path / "person" / "marcelo.md"
        assert EntityPage.slug_from_path(path) == "marcelo"

    def test_identifying_strings_combines_name_aliases_and_extras(self) -> None:
        page = EntityPage(
            type="person",
            name="Marcelo Marmol",
            aliases=["Marcelo", "marcelo"],
            extra={
                "identifiers": ["mmarmol@mxhero.com", "UM7TCSZRN"],
                "other_list": ["xx"],
            },
        )
        ids = page.identifying_strings()
        # Order: name first, then aliases, then extras lists, dedup'd.
        assert ids[0] == "Marcelo Marmol"
        assert "Marcelo" in ids
        assert "marcelo" in ids
        assert "mmarmol@mxhero.com" in ids
        assert "UM7TCSZRN" in ids
        assert "xx" in ids

    def test_identifying_strings_dedups(self) -> None:
        page = EntityPage(
            type="person",
            name="Marcelo",
            aliases=["Marcelo", "marcelo"],  # "Marcelo" same as name
        )
        ids = page.identifying_strings()
        assert ids.count("Marcelo") == 1

    def test_identifying_strings_handles_dict_one_level(self) -> None:
        """Per phase 0.3 + phase 2 e2e: LLM emits identifiers sometimes
        as dict ({email: foo, slack: bar}). identifying_strings extracts
        VALUES one level deep — keys are field names, values are identity."""
        page = EntityPage(
            type="person",
            name="X",
            extra={
                "identifiers": {
                    "email": "mmarmol@mxhero.com",
                    "slack": "UM7TCSZRN",
                    "phones": ["+5491234567", "+5499876543"],  # list value
                },
                "tags": ["topic-a", "topic-b"],
            },
        )
        ids = page.identifying_strings()
        assert "X" in ids
        # Dict VALUES become identifiers
        assert "mmarmol@mxhero.com" in ids
        assert "UM7TCSZRN" in ids
        # List values within a dict are also expanded
        assert "+5491234567" in ids
        assert "+5499876543" in ids
        # Keys are NOT identifiers
        assert "email" not in ids
        assert "slack" not in ids
        # Flat list still works
        assert "topic-a" in ids and "topic-b" in ids

    def test_identifying_strings_skips_nested_dicts(self) -> None:
        """Two-level nesting is intentionally skipped to avoid pulling
        metadata into the alias_index."""
        page = EntityPage(
            type="person",
            name="X",
            extra={
                "metadata": {
                    "deeply_nested": {"foo": "bar"},  # dict-of-dict
                },
            },
        )
        ids = page.identifying_strings()
        assert "X" in ids
        # The deeply nested dict is skipped — neither key nor value contribute
        assert "foo" not in ids
        assert "bar" not in ids
        assert "deeply_nested" not in ids

    def test_identifying_strings_scalar_string_extra(self) -> None:
        """A bare string in extra is also identity-relevant (e.g.,
        ``canonical_id: "marcelo-2026"``)."""
        page = EntityPage(
            type="person",
            name="X",
            extra={"canonical_id": "marcelo-2026"},
        )
        ids = page.identifying_strings()
        assert "marcelo-2026" in ids
