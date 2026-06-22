"""Tests for typed entity references."""

from __future__ import annotations

import pytest

from durin.memory.entities import (
    SUGGESTED_TYPES,
    InvalidEntityRefError,
    is_valid_entity_ref,
    normalize_entity_ref,
    parse_entity_ref,
    split_valid_invalid,
)


class TestIsValidEntityRef:
    """Format validation — shape only, not vocabulary."""

    @pytest.mark.parametrize(
        "ref",
        [
            "person:marcelo",
            "project:durin",
            "topic:autocompaction",
            "artifact:settings.py",
            "stance:prefer-pytest",
            "practice:tdd",
            # types outside the suggested 8 — should still validate
            "agent:sam",
            "org:henngo",
            "model:glm-5.1",
            # values with rich punctuation
            "file:durin/agent/loop.py",
            "event:release-v0.1.0a7",
            "incident:webui-crash-2026-05-15",
            # underscore in type
            "user_role:architect",
            # numbers in type (after first letter)
            "tool2:foo",
            # value with colons (only first ':' separates)
            "url:https://example.com/path",
            # value with spaces (allowed)
            "topic:context window management",
        ],
    )
    def test_accepts_well_formed_refs(self, ref: str) -> None:
        assert is_valid_entity_ref(ref) is True

    @pytest.mark.parametrize(
        "ref",
        [
            "",
            "marcelo",  # no type prefix
            ":value",  # empty type
            "person:",  # empty value
            "Person:Marcelo",  # uppercase in type
            "person :marcelo",  # space in type
            "123type:value",  # type starts with digit
            "my-type:value",  # hyphen in type
            "person:  ",  # only whitespace value
            "person: starts with space",  # value starts with whitespace
            "_underscore:value",  # type starts with underscore
        ],
    )
    def test_rejects_malformed_refs(self, ref: str) -> None:
        assert is_valid_entity_ref(ref) is False

    def test_rejects_non_string(self) -> None:
        assert is_valid_entity_ref(None) is False  # type: ignore[arg-type]
        assert is_valid_entity_ref(42) is False  # type: ignore[arg-type]
        assert is_valid_entity_ref(["person:x"]) is False  # type: ignore[arg-type]


class TestParseEntityRef:
    def test_splits_on_first_colon(self) -> None:
        parsed = parse_entity_ref("person:marcelo")
        assert parsed.type == "person"
        assert parsed.value == "marcelo"

    def test_value_can_contain_colons(self) -> None:
        parsed = parse_entity_ref("url:https://example.com/x")
        assert parsed.type == "url"
        assert parsed.value == "https://example.com/x"

    def test_value_can_have_punctuation(self) -> None:
        parsed = parse_entity_ref("file:durin/agent/loop.py")
        assert parsed.type == "file"
        assert parsed.value == "durin/agent/loop.py"

    def test_raises_on_invalid(self) -> None:
        with pytest.raises(InvalidEntityRefError):
            parse_entity_ref("malformed")
        with pytest.raises(InvalidEntityRefError):
            parse_entity_ref("Person:upper")

    def test_str_roundtrip(self) -> None:
        original = "topic:embeddings"
        assert str(parse_entity_ref(original)) == original


class TestSplitValidInvalid:
    def test_partitions_preserving_order(self) -> None:
        refs = ["person:marcelo", "bad", "project:durin", "Bad:Upper"]
        valid, invalid = split_valid_invalid(refs)
        assert valid == ["person:marcelo", "project:durin"]
        assert invalid == ["bad", "Bad:Upper"]

    def test_empty_input(self) -> None:
        valid, invalid = split_valid_invalid([])
        assert valid == []
        assert invalid == []

    def test_all_valid(self) -> None:
        refs = ["person:a", "person:b"]
        valid, invalid = split_valid_invalid(refs)
        assert valid == refs
        assert invalid == []

    def test_all_invalid(self) -> None:
        refs = ["a", "b"]
        valid, invalid = split_valid_invalid(refs)
        assert valid == []
        assert invalid == refs


class TestNormalizeEntityRef:
    def test_lowercases_type_only(self) -> None:
        assert normalize_entity_ref("Person:Marcelo") == "person:Marcelo"
        assert normalize_entity_ref("PROJECT:Durin") == "project:Durin"

    def test_passes_through_when_already_valid(self) -> None:
        assert normalize_entity_ref("person:marcelo") == "person:marcelo"

    def test_raises_when_missing_colon(self) -> None:
        with pytest.raises(InvalidEntityRefError):
            normalize_entity_ref("nocolon")

    def test_raises_when_value_empty(self) -> None:
        with pytest.raises(InvalidEntityRefError):
            normalize_entity_ref("Person:")


class TestSuggestedTypes:
    """8 broad cross-profession types — these are hints, NOT enforced."""

    def test_contains_doc_18_set(self) -> None:
        # The 8 broad cross-profession types
        assert SUGGESTED_TYPES == frozenset(
            {
                "person",
                "place",
                "project",
                "topic",
                "event",
                "artifact",
                "stance",
                "practice",
            }
        )

    def test_open_vocabulary_principle(self) -> None:
        # Suggested types validate, but so does anything else well-formed.
        # This documents the open-vocabulary stance — refs outside the
        # suggested set MUST still pass validation. If this assertion
        # ever fails, the architecture has drifted to closed enum.
        for t in SUGGESTED_TYPES:
            assert is_valid_entity_ref(f"{t}:example")
        assert is_valid_entity_ref("agent:sam")  # not in suggested
        assert is_valid_entity_ref("org:foo")  # not in suggested
        assert is_valid_entity_ref("model:glm-5.1")  # not in suggested
