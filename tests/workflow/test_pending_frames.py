from durin.workflow.progress import pending_frames
from durin.workflow.spec import parse_workflow


def _wf(nodes):
    return parse_workflow({"name": "wf", "start": nodes[0]["id"], "nodes": nodes})


def test_a_linear_tail_is_listed():
    wf = _wf([
        {"id": "a", "kind": "work", "next": "b"},
        {"id": "b", "kind": "work", "next": "c"},
        {"id": "c", "kind": "work", "next": None},
    ])
    assert [f["id"] for f in pending_frames(wf, "a")] == ["b", "c"]
    assert all(f["status"] == "pending" for f in pending_frames(wf, "a"))


def test_the_tail_stops_at_the_first_fork():
    """A router picks one of several successors; listing all of them would show
    a path that cannot happen, and listing one would be a guess."""
    wf = _wf([
        {"id": "a", "kind": "work", "next": "gate"},
        {"id": "gate", "kind": "work", "on_pass": "b", "on_fail": "c"},
        {"id": "b", "kind": "work", "next": None},
        {"id": "c", "kind": "work", "next": None},
    ])
    assert [f["id"] for f in pending_frames(wf, "a")] == ["gate"]


def test_a_loop_does_not_repeat_forever():
    wf = _wf([
        {"id": "a", "kind": "work", "next": "b"},
        {"id": "b", "kind": "work", "next": "a"},
    ])
    assert [f["id"] for f in pending_frames(wf, "a")] == ["b"]


def test_the_last_node_has_no_tail():
    wf = _wf([{"id": "a", "kind": "work", "next": None}])
    assert pending_frames(wf, "a") == []
