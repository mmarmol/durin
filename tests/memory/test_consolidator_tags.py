"""Tests for the consolidator response parser and tag persistence."""

from __future__ import annotations

import json
from pathlib import Path

from durin.memory.consolidator_tags import parse_consolidator_response

# ---------------------------------------------------------------------------
# Parser unit tests
# ---------------------------------------------------------------------------


def test_parses_valid_response_with_both_keys() -> None:
    raw = (
        "- user prefers terse\n"
        "- decided to drop cache\n"
        "---\n"
        "entities: [person:marcelo, project:durin]\n"
        "topics: [communication, architecture]\n"
    )
    summary, tags = parse_consolidator_response(raw)
    assert summary == "- user prefers terse\n- decided to drop cache"
    assert tags == {
        "entities": ["person:marcelo", "project:durin"],
        "topics": ["communication", "architecture"],
    }


def test_lenient_drops_malformed_entity_refs() -> None:
    """Per doc 14 §3.2: malformed entity refs are dropped silently on the
    read path; only well-formed `<type>:<value>` strings survive."""
    raw = (
        "- bullet\n"
        "---\n"
        "entities: [person:marcelo, just-name, project:durin, BadCase:Upper]\n"
    )
    summary, tags = parse_consolidator_response(raw)
    # 'just-name' (no type) and 'BadCase:Upper' (uppercase type) drop.
    assert tags["entities"] == ["person:marcelo", "project:durin"]


def test_handles_nothing_response() -> None:
    summary, tags = parse_consolidator_response("(nothing)")
    assert summary == "(nothing)"
    assert tags == {"entities": [], "topics": []}


def test_no_separator_returns_full_text_as_summary() -> None:
    raw = "- one bullet only"
    summary, tags = parse_consolidator_response(raw)
    assert summary == "- one bullet only"
    assert tags == {"entities": [], "topics": []}


def test_malformed_yaml_falls_back_to_summary() -> None:
    raw = "- bullet\n---\nentities: [unclosed\ntopics: bad"
    summary, tags = parse_consolidator_response(raw)
    assert summary == "- bullet"
    assert tags == {"entities": [], "topics": []}


def test_non_dict_yaml_falls_back() -> None:
    raw = "- bullet\n---\n- not a dict\n- still not a dict\n"
    summary, tags = parse_consolidator_response(raw)
    assert summary == "- bullet"
    assert tags == {"entities": [], "topics": []}


def test_only_entities_present() -> None:
    raw = "- bullet\n---\nentities: [topic:a, topic:b]\n"
    summary, tags = parse_consolidator_response(raw)
    assert tags == {"entities": ["topic:a", "topic:b"], "topics": []}


def test_only_topics_present() -> None:
    raw = "- bullet\n---\ntopics: [x]\n"
    summary, tags = parse_consolidator_response(raw)
    assert tags == {"entities": [], "topics": ["x"]}


def test_empty_lists() -> None:
    raw = "- bullet\n---\nentities: []\ntopics: []\n"
    summary, tags = parse_consolidator_response(raw)
    assert summary == "- bullet"
    assert tags == {"entities": [], "topics": []}


def test_non_list_values_are_coerced_to_empty() -> None:
    raw = "- bullet\n---\nentities: not-a-list\ntopics: 42\n"
    summary, tags = parse_consolidator_response(raw)
    assert tags == {"entities": [], "topics": []}


def test_drops_empty_and_none_items() -> None:
    raw = (
        "- bullet\n---\n"
        "entities: [topic:a, '', null, topic:b]\n"
        "topics: [' ', x]\n"
    )
    summary, tags = parse_consolidator_response(raw)
    assert tags == {"entities": ["topic:a", "topic:b"], "topics": ["x"]}


def test_empty_string_input() -> None:
    summary, tags = parse_consolidator_response("")
    assert summary == ""
    assert tags == {"entities": [], "topics": []}


def test_block_style_yaml_works() -> None:
    raw = (
        "- bullet\n"
        "---\n"
        "entities:\n"
        "  - topic:a\n"
        "  - topic:b\n"
        "topics:\n"
        "  - x\n"
    )
    summary, tags = parse_consolidator_response(raw)
    assert tags == {"entities": ["topic:a", "topic:b"], "topics": ["x"]}


def test_multiple_separators_uses_last_one() -> None:
    """If the body contains --- inside (e.g. user pasted YAML), use the LAST one."""
    raw = (
        "- bullet one\n"
        "---\n"
        "- bullet two pasted yaml above\n"
        "---\n"
        "entities: [person:marcelo]\n"
        "topics: []\n"
    )
    summary, tags = parse_consolidator_response(raw)
    assert "bullet one" in summary
    assert "bullet two pasted yaml above" in summary
    assert tags == {"entities": ["person:marcelo"], "topics": []}


# ---------------------------------------------------------------------------
# _merge_session_tags (static method on MemoryStore)
# ---------------------------------------------------------------------------


def test_merge_session_tags_into_empty(tmp_path: Path) -> None:
    from durin.agent.memory import Consolidator
    from durin.session.manager import Session

    session = Session(key="s1", created_at=__import__("datetime").datetime.now(), updated_at=__import__("datetime").datetime.now())
    Consolidator._merge_session_tags(session, {"entities": ["a"], "topics": ["x"]})
    assert session.metadata["_last_tags"] == {"entities": ["a"], "topics": ["x"]}


def test_merge_session_tags_union(tmp_path: Path) -> None:
    from durin.agent.memory import Consolidator
    from durin.session.manager import Session

    session = Session(key="s1", created_at=__import__("datetime").datetime.now(), updated_at=__import__("datetime").datetime.now())
    session.metadata["_last_tags"] = {"entities": ["a", "b"], "topics": ["x"]}
    Consolidator._merge_session_tags(session, {"entities": ["b", "c"], "topics": ["y"]})
    assert session.metadata["_last_tags"] == {
        "entities": ["a", "b", "c"],
        "topics": ["x", "y"],
    }


def test_merge_session_tags_noop_on_empty_new(tmp_path: Path) -> None:
    from durin.agent.memory import Consolidator
    from durin.session.manager import Session

    session = Session(key="s1", created_at=__import__("datetime").datetime.now(), updated_at=__import__("datetime").datetime.now())
    session.metadata["_last_tags"] = {"entities": ["existing"], "topics": []}
    Consolidator._merge_session_tags(session, {"entities": [], "topics": []})
    # Unchanged
    assert session.metadata["_last_tags"] == {"entities": ["existing"], "topics": []}


def test_merge_session_tags_handles_none_input(tmp_path: Path) -> None:
    from durin.agent.memory import Consolidator
    from durin.session.manager import Session

    session = Session(key="s1", created_at=__import__("datetime").datetime.now(), updated_at=__import__("datetime").datetime.now())
    Consolidator._merge_session_tags(session, None)
    assert "_last_tags" not in session.metadata


# ---------------------------------------------------------------------------
# End-to-end: _last_tags routes to .meta.json::derived (not to jsonl line 0)
# ---------------------------------------------------------------------------


def test_last_tags_routes_to_meta_derived(tmp_path: Path) -> None:
    from durin.session.manager import SessionManager

    mgr = SessionManager(workspace=tmp_path)
    session = mgr.get_or_create("tag-routing-test")
    session.metadata["_last_summary"] = {"text": "x", "last_active": "y"}
    session.metadata["_last_tags"] = {"entities": ["e1"], "topics": ["t1"]}
    mgr.save(session)

    # jsonl line 0 must NOT contain _last_tags or _last_summary
    jsonl_path = mgr.sessions_dir / "tag-routing-test.jsonl"
    line0 = json.loads(jsonl_path.read_text(encoding="utf-8").splitlines()[0])
    assert "_last_tags" not in line0["metadata"]
    assert "_last_summary" not in line0["metadata"]

    # .meta.json::derived must contain BOTH
    meta_path = mgr.sessions_dir / "tag-routing-test.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["derived"]["_last_summary"] == {"text": "x", "last_active": "y"}
    assert meta["derived"]["_last_tags"] == {"entities": ["e1"], "topics": ["t1"]}


def test_last_tags_loads_back_on_re_open(tmp_path: Path) -> None:
    """Round-trip: write tags, recreate SessionManager, load, tags survive."""
    from durin.session.manager import SessionManager

    mgr = SessionManager(workspace=tmp_path)
    session = mgr.get_or_create("round-trip-test")
    session.metadata["_last_tags"] = {"entities": ["x"], "topics": ["y"]}
    mgr.save(session)

    mgr2 = SessionManager(workspace=tmp_path)
    loaded = mgr2.get_or_create("round-trip-test")
    assert loaded.metadata.get("_last_tags") == {"entities": ["x"], "topics": ["y"]}
