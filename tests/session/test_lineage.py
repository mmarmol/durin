"""Tests for the session lineage metadata contract."""

from durin.session import lineage


def test_build_lineage_populates_all_fields():
    block = lineage.build_lineage(
        parent_session_id="websocket:abc",
        root_id="websocket:abc",
        origin_type="subagent",
        origin_id="t1",
    )
    assert block == {
        lineage.PARENT_SESSION_ID: "websocket:abc",
        lineage.ROOT_ID: "websocket:abc",
        lineage.ORIGIN_TYPE: "subagent",
        lineage.ORIGIN_ID: "t1",
    }


def test_parent_of_returns_none_for_non_branch_session():
    assert lineage.parent_of({}) is None
    assert lineage.parent_of({"title": "hi"}) is None


def test_parent_of_returns_parent_when_present():
    meta = lineage.build_lineage(
        parent_session_id="websocket:abc", root_id="websocket:abc",
        origin_type="subagent", origin_id="t1",
    )
    assert lineage.parent_of(meta) == "websocket:abc"


def test_root_of_falls_back_to_default_when_unset():
    assert lineage.root_of({}, default="websocket:abc") == "websocket:abc"


def test_root_of_returns_stored_root_for_nested_branch():
    meta = {lineage.ROOT_ID: "websocket:root"}
    assert lineage.root_of(meta, default="websocket:parent") == "websocket:root"
