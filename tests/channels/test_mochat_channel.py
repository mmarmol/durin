"""Unit tests for MoChat channel pure logic.

`durin/channels/mochat.py` (943 LOC) shipped with no test file (QA review
P3 #19). This suite covers the non-network, deterministic core the report
flagged: content/target/timestamp/mention parsing, the buffered-body
builder, the group mention-requirement resolver, and the two id-list /
group-id static helpers. The websocket / polling transport is intentionally
out of scope here — these are the units that carry behavioral risk and are
cheap to pin.
"""

from __future__ import annotations

import pytest

from durin.channels.mochat import (
    MochatBufferedEntry,
    MochatChannel,
    MochatConfig,
    MochatGroupRule,
    MochatMentionConfig,
    MochatTarget,
    _safe_dict,
    _str_field,
    build_buffered_body,
    extract_mention_ids,
    normalize_mochat_content,
    parse_timestamp,
    resolve_mochat_target,
    resolve_require_mention,
    resolve_was_mentioned,
)

# ---------------------------------------------------------------------------
# normalize_mochat_content
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("  hi  ", "hi"),
        ("", ""),
        (None, ""),
        ({"a": 1}, '{"a": 1}'),
        ([1, 2], "[1, 2]"),
    ],
)
def test_normalize_mochat_content(value, expected):
    assert normalize_mochat_content(value) == expected


def test_normalize_mochat_content_non_serializable_falls_back_to_str():
    obj = object()
    assert normalize_mochat_content(obj) == str(obj)


# ---------------------------------------------------------------------------
# resolve_mochat_target
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", MochatTarget(id="", is_panel=False)),
        ("   ", MochatTarget(id="", is_panel=False)),
        # bare non-session id → treated as a panel
        ("abc123", MochatTarget(id="abc123", is_panel=True)),
        # session_ prefix → DM, not a panel
        ("session_abc", MochatTarget(id="session_abc", is_panel=False)),
        # explicit panel-forcing prefixes
        ("group:g1", MochatTarget(id="g1", is_panel=True)),
        ("channel:c1", MochatTarget(id="c1", is_panel=True)),
        ("panel:p1", MochatTarget(id="p1", is_panel=True)),
        # mochat: prefix does NOT force panel; a session_ id stays a DM
        ("mochat:session_x", MochatTarget(id="session_x", is_panel=False)),
        # prefix with empty remainder
        ("group:", MochatTarget(id="", is_panel=False)),
    ],
)
def test_resolve_mochat_target(raw, expected):
    assert resolve_mochat_target(raw) == expected


# ---------------------------------------------------------------------------
# extract_mention_ids
# ---------------------------------------------------------------------------


def test_extract_mention_ids_non_list_returns_empty():
    assert extract_mention_ids("nope") == []
    assert extract_mention_ids(None) == []
    assert extract_mention_ids({"id": "x"}) == []


def test_extract_mention_ids_strings_stripped_and_filtered():
    assert extract_mention_ids(["a", " b ", "", "  "]) == ["a", "b"]


def test_extract_mention_ids_dicts_by_priority_key():
    value = [
        {"id": "x"},
        {"userId": "y"},
        {"_id": "z"},
        {"other": "ignored"},
        {"id": "  w  "},
    ]
    assert extract_mention_ids(value) == ["x", "y", "z", "w"]


def test_extract_mention_ids_mixed():
    assert extract_mention_ids(["a", {"userId": "b"}, 42]) == ["a", "b"]


# ---------------------------------------------------------------------------
# resolve_was_mentioned
# ---------------------------------------------------------------------------


def test_was_mentioned_meta_flags():
    assert resolve_was_mentioned({"meta": {"mentioned": True}}, "agent") is True
    assert resolve_was_mentioned({"meta": {"wasMentioned": True}}, "agent") is True
    # flags don't require an agent id
    assert resolve_was_mentioned({"meta": {"mentioned": True}}, "") is True


def test_was_mentioned_meta_id_lists():
    payload = {"meta": {"mentions": ["other", "agent"]}}
    assert resolve_was_mentioned(payload, "agent") is True
    assert resolve_was_mentioned({"meta": {"mentionIds": [{"id": "agent"}]}}, "agent") is True
    assert resolve_was_mentioned({"meta": {"mentions": ["other"]}}, "agent") is False


def test_was_mentioned_text_fallback():
    assert resolve_was_mentioned({"content": "hey <@agent> there"}, "agent") is True
    assert resolve_was_mentioned({"content": "hey @agent"}, "agent") is True
    assert resolve_was_mentioned({"content": "no mention here"}, "agent") is False


def test_was_mentioned_no_agent_id_without_flag_is_false():
    assert resolve_was_mentioned({"content": "@someone"}, "") is False
    assert resolve_was_mentioned({"meta": {"mentions": ["x"]}}, "") is False


# ---------------------------------------------------------------------------
# resolve_require_mention
# ---------------------------------------------------------------------------


def test_require_mention_group_rule_by_group_id():
    cfg = MochatConfig(groups={"g1": MochatGroupRule(require_mention=True)})
    assert resolve_require_mention(cfg, session_id="s1", group_id="g1") is True


def test_require_mention_group_rule_by_session_id():
    # group_id is checked first, but it isn't in groups here; the
    # session_id key matches and supplies the rule.
    cfg = MochatConfig(groups={"s1": MochatGroupRule(require_mention=True)})
    assert resolve_require_mention(cfg, session_id="s1", group_id="unknown") is True


def test_require_mention_wildcard_rule():
    cfg = MochatConfig(groups={"*": MochatGroupRule(require_mention=True)})
    assert resolve_require_mention(cfg, session_id="s1", group_id="g1") is True


def test_require_mention_specific_overrides_wildcard():
    cfg = MochatConfig(
        groups={
            "g1": MochatGroupRule(require_mention=False),
            "*": MochatGroupRule(require_mention=True),
        }
    )
    # g1 matches before "*", so its False wins
    assert resolve_require_mention(cfg, session_id="s1", group_id="g1") is False


def test_require_mention_falls_back_to_mention_config():
    cfg = MochatConfig(mention=MochatMentionConfig(require_in_groups=True))
    assert resolve_require_mention(cfg, session_id="s1", group_id="g1") is True
    cfg2 = MochatConfig(mention=MochatMentionConfig(require_in_groups=False))
    assert resolve_require_mention(cfg2, session_id="s1", group_id="g1") is False


# ---------------------------------------------------------------------------
# build_buffered_body
# ---------------------------------------------------------------------------


def test_build_buffered_body_empty():
    assert build_buffered_body([], is_group=False) == ""


def test_build_buffered_body_single_returns_raw():
    entry = MochatBufferedEntry(raw_body="hello", author="u1")
    assert build_buffered_body([entry], is_group=True) == "hello"
    assert build_buffered_body([entry], is_group=False) == "hello"


def test_build_buffered_body_multi_dm_joins_plain():
    entries = [
        MochatBufferedEntry(raw_body="first", author="u1"),
        MochatBufferedEntry(raw_body="second", author="u1"),
    ]
    assert build_buffered_body(entries, is_group=False) == "first\nsecond"


def test_build_buffered_body_multi_group_labels_by_sender():
    # Label fallback chain: sender_name -> sender_username -> author.
    entries = [
        MochatBufferedEntry(raw_body="hi", author="u1", sender_name="Alice"),
        MochatBufferedEntry(raw_body="yo", author="u2", sender_username="bob"),
        MochatBufferedEntry(raw_body="x", author="u3"),  # falls back to author
    ]
    body = build_buffered_body(entries, is_group=True)
    assert body == "Alice: hi\nbob: yo\nu3: x"


def test_build_buffered_body_multi_group_no_label_when_all_empty():
    # When sender_name, sender_username, AND author are all empty there is
    # no label, so the raw body is appended bare.
    entries = [
        MochatBufferedEntry(raw_body="hi", author="u1", sender_name="Alice"),
        MochatBufferedEntry(raw_body="anon", author=""),
    ]
    body = build_buffered_body(entries, is_group=True)
    assert body == "Alice: hi\nanon"


def test_build_buffered_body_skips_empty_raw():
    entries = [
        MochatBufferedEntry(raw_body="keep", author="u1"),
        MochatBufferedEntry(raw_body="", author="u2"),
    ]
    assert build_buffered_body(entries, is_group=False) == "keep"


# ---------------------------------------------------------------------------
# parse_timestamp
# ---------------------------------------------------------------------------


def test_parse_timestamp_valid_iso():
    assert parse_timestamp("2024-01-01T00:00:00+00:00") == 1_704_067_200_000


def test_parse_timestamp_z_suffix():
    assert parse_timestamp("2024-01-01T00:00:00Z") == 1_704_067_200_000


@pytest.mark.parametrize("value", ["", "   ", "not-a-date", None, 12345, []])
def test_parse_timestamp_invalid_returns_none(value):
    assert parse_timestamp(value) is None


# ---------------------------------------------------------------------------
# small helpers + static methods
# ---------------------------------------------------------------------------


def test_safe_dict():
    assert _safe_dict({"a": 1}) == {"a": 1}
    assert _safe_dict("x") == {}
    assert _safe_dict(None) == {}


def test_str_field_first_non_empty():
    src = {"a": "  ", "b": " val ", "c": "other"}
    assert _str_field(src, "a", "b", "c") == "val"
    assert _str_field(src, "missing") == ""
    assert _str_field({"a": 5}, "a") == ""  # non-str ignored


def test_normalize_id_list_dedups_sorts_and_detects_wildcard():
    cleaned, has_wildcard = MochatChannel._normalize_id_list(["b", " a ", "*", "a", ""])
    assert cleaned == ["a", "b"]
    assert has_wildcard is True


def test_normalize_id_list_no_wildcard():
    cleaned, has_wildcard = MochatChannel._normalize_id_list(["x"])
    assert cleaned == ["x"]
    assert has_wildcard is False


@pytest.mark.parametrize(
    "metadata,expected",
    [
        ({"group_id": " g1 "}, "g1"),
        ({"groupId": "g2"}, "g2"),
        ({"group_id": "", "groupId": "g3"}, "g3"),
        ({"group_id": "   "}, None),
        ({}, None),
        ("not-a-dict", None),
    ],
)
def test_read_group_id(metadata, expected):
    assert MochatChannel._read_group_id(metadata) == expected
