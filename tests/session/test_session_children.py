"""Tests for SessionManager.children_of lineage discovery."""

from durin.session.lineage import build_lineage
from durin.session.manager import Session, SessionManager


def _branch(key: str, parent: str, task_id: str) -> Session:
    s = Session(key=key, messages=[{"role": "user", "content": "x"}])
    s.metadata.update(
        build_lineage(
            parent_session_id=parent, root_id=parent,
            origin_type="subagent", origin_id=task_id,
        )
    )
    return s


def test_children_of_returns_only_matching_branch_sessions(tmp_path):
    sm = SessionManager(workspace=tmp_path)
    sm.save(Session(key="websocket:abc"))                 # the parent
    sm.save(_branch("subagent:t1", "websocket:abc", "t1"))
    sm.save(Session(key="websocket:zzz"))                 # unrelated, no lineage

    kids = sm.children_of("websocket:abc")

    assert [k["key"] for k in kids] == ["subagent:t1"]
    assert kids[0]["origin_type"] == "subagent"
    assert kids[0]["origin_id"] == "t1"


def test_children_of_is_empty_for_session_with_no_children(tmp_path):
    sm = SessionManager(workspace=tmp_path)
    sm.save(Session(key="websocket:abc"))
    assert sm.children_of("websocket:abc") == []


def test_children_of_reads_from_disk_not_cache(tmp_path):
    # A fresh manager (cold cache) must still find children via line-0 headers.
    SessionManager(workspace=tmp_path).save(_branch("subagent:t9", "websocket:abc", "t9"))
    fresh = SessionManager(workspace=tmp_path)
    assert [k["key"] for k in fresh.children_of("websocket:abc")] == ["subagent:t9"]
